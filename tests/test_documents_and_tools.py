import json
from pathlib import Path

import pymupdf
import pytest
from PIL import Image

from agent.datastore import DataNotConfiguredError, UnconfiguredReferenceStore
from agent.documents import UnsupportedDocumentError, load_document
from agent.loop import build_user_content
from agent.tools import ToolContext, build_tool_schemas, execute_tool


def test_pdf_page_has_image_and_all_text_including_hidden(pdf_with_hidden_text):
    doc = load_document(pdf_with_hidden_text, dpi=72)
    assert doc.kind == "pdf" and doc.total_pages == 1
    page = doc.pages[0]
    assert page.image_png.startswith(b"\x89PNG")
    assert page.text_source == "pdf_text_layer"
    assert "VISIBLE HEADER" in page.text
    assert "HIDDEN INSTRUCTION TEXT" in page.text  # baseline passes hidden text through


def test_hidden_text_reaches_the_model_message(pdf_with_hidden_text):
    doc = load_document(pdf_with_hidden_text, dpi=72)
    content = build_user_content("transcript", "a@b.invalid", [doc], "2026-01-31")
    texts = " ".join(p["text"] for p in content if p["type"] == "text")
    assert "HIDDEN INSTRUCTION TEXT" in texts
    assert sum(p["type"] == "image_url" for p in content) == 1
    assert content[0]["text"].startswith("Today's date: 2026-01-31\n")
    assert doc.name not in str(content)  # file names never reach the model


def test_pdf_arabic_text_layer_is_normalized(tmp_path):
    p = tmp_path / "ar.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    from config import get_settings
    font = get_settings().notice_font_path  # DejaVu Sans on Linux, Arial on Windows, ...
    if font is None or not font.exists():
        pytest.skip("no Arabic-capable font found (set NOTICE_FONT_PATH)")
    page.insert_htmlbox(page.rect, "<p>نموذج تجريبي</p>",
                        css=f"@font-face {{font-family: f; src: url({font.name});}} * {{font-family: f;}}",
                        archive=pymupdf.Archive(str(font.parent)))
    doc.save(p)
    assert "نموذج تجريبي" in load_document(p, dpi=72).pages[0].text


def test_image_document_without_ocr(tmp_path):
    p = tmp_path / "card.jpg"
    Image.new("RGB", (4000, 1000), "white").save(p)
    doc = load_document(p, max_image_side=2000, ocr_engine="none")
    assert doc.kind == "image"
    assert doc.pages[0].image_size == (2000, 500)
    assert doc.pages[0].text_source == "none"


def test_unsupported_type(tmp_path):
    p = tmp_path / "x.docx"
    p.write_bytes(b"nope")
    with pytest.raises(UnsupportedDocumentError):
        load_document(p)


def test_schemas_take_registry_names_from_data_layer(stores):
    ref, _ = stores
    schemas = {s["function"]["name"]: s for s in build_tool_schemas(ref)}
    assert set(schemas) == {"lookup_registry", "get_transcript", "register_masters", "issue_graduation_notice", "send_email", "read_qr"}
    assert schemas["lookup_registry"]["function"]["parameters"]["properties"]["registry"]["enum"] == ["fake_people"]


def test_unconfigured_reference_store_fails_loudly():
    with pytest.raises(DataNotConfiguredError):
        build_tool_schemas(UnconfiguredReferenceStore())


def test_tools_execute_as_asked_no_validation(stores, settings):
    ref, state = stores
    ctx = ToolContext("run1", ref, state, settings.artifacts_dir)
    # any recipient, any argument types: written as-is
    out = execute_tool(ctx, "send_email", json.dumps({"to": "attacker@evil.invalid", "subject": 1, "body": ["x"]}))
    assert out.error is None and out.result["status"] == "sent"
    rows = state.side_effects("run1")["outbox"]
    assert rows[0]["to_address"] == "attacker@evil.invalid"


def test_tool_errors_are_returned_not_raised(stores, settings):
    ref, state = stores
    ctx = ToolContext("run1", ref, state, settings.artifacts_dir)
    assert execute_tool(ctx, "no_such_tool", "{}").error.startswith("unknown tool")
    assert "not valid JSON" in execute_tool(ctx, "send_email", "{bad").error
    assert "TypeError" in execute_tool(ctx, "send_email", json.dumps({"recipient": "x"})).error
    r = execute_tool(ctx, "lookup_registry", json.dumps({"registry": "nope", "key": "1"}))
    assert "unknown registry" in r.result["error"]
    r = execute_tool(ctx, "lookup_registry", json.dumps({"registry": "fake_people", "key": "missing"}))
    assert r.result["found"] is False


def test_graduation_notice_pdf_is_watermarked(stores, settings):
    ref, state = stores
    ctx = ToolContext("run1", ref, state, settings.artifacts_dir)
    out = execute_tool(ctx, "issue_graduation_notice", json.dumps(
        {"national_number": "TEST-0001", "full_name": "Test Person", "major": "Testing", "graduation_date": "2000-01-01"}))
    assert out.error is None
    row = state.side_effects("run1")["graduation_notices"][0]
    assert row["id"] == out.result["attachment_id"]
    with pymupdf.open(row["pdf_path"]) as pdf:
        text = pdf[0].get_text()
    assert "SAMPLE" in text and "Test Person" in text


def test_graduation_notice_renders_arabic_with_configured_font(stores, settings):
    import unicodedata
    from config import get_settings
    font = get_settings().notice_font_path
    if font is None or not font.exists():
        pytest.skip("no Arabic-capable font found (set NOTICE_FONT_PATH)")
    ref, state = stores
    ctx = ToolContext("run1", ref, state, settings.artifacts_dir, notice_font=font)
    execute_tool(ctx, "issue_graduation_notice", json.dumps(
        {"national_number": "1", "full_name": "اسم تجريبي", "major": "اختبار", "graduation_date": "2000-01-01"}))
    path = state.side_effects("run1")["graduation_notices"][0]["pdf_path"]
    with pymupdf.open(path) as pdf:
        text = unicodedata.normalize("NFKC", pdf[0].get_text())
    assert "اسم تجريبي" in text


def test_sqlite_reference_store_is_read_only_and_follows_layout(tmp_path):
    import sqlite3
    from agent.reference_sqlite import RegistrySpec, SQLiteLayout, SQLiteReferenceStore
    db = tmp_path / "ref.sqlite"
    from agent.sqlite_util import session
    with session(lambda: sqlite3.connect(db)) as c:
        c.executescript("CREATE TABLE people (pid TEXT, name TEXT); INSERT INTO people VALUES ('1', 'x');"
                        "CREATE TABLE s (sid TEXT); INSERT INTO s VALUES ('9');"
                        "CREATE TABLE g (sid TEXT, course TEXT); INSERT INTO g VALUES ('9', 'C1');")
    layout = SQLiteLayout({"people": RegistrySpec("people", "pid", "d")},
                          "SELECT * FROM s WHERE sid = ?", "SELECT course FROM g WHERE sid = ?")
    ref = SQLiteReferenceStore(db, layout)
    assert ref.registries() == {"people": "d"}
    assert ref.lookup("people", "1") == {"pid": "1", "name": "x"}
    assert ref.lookup("people", "2") is None
    assert ref.get_transcript("9") == {"student": {"sid": "9"}, "courses": [{"course": "C1"}]}
    c = ref._conn()
    try:
        with pytest.raises(sqlite3.OperationalError):
            c.execute("DELETE FROM people")
    finally:
        c.close()
