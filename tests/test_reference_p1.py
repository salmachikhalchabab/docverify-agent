"""The P1 registry layout, against the real P1 snapshot in data/p1/ (synthetic data, no model)."""

import hashlib
import sqlite3
from pathlib import Path

import pytest

from agent.reference_p1 import P1_LAYOUT, ensure_p1_db, registered_email
from agent.reference_sqlite import SQLiteReferenceStore

SNAPSHOT = Path(__file__).resolve().parent.parent / "data" / "p1" / "registry.snapshot.db"
pytestmark = pytest.mark.skipif(not SNAPSHOT.exists(), reason="P1 snapshot not present")

NID = "11490564657"  # سارة الحلبي (graduated, accepted to a master's program, has issued documents)


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    out = tmp_path_factory.mktemp("p1") / "registry_agent.sqlite"
    before = hashlib.sha256(SNAPSHOT.read_bytes()).hexdigest()
    ensure_p1_db(SNAPSHOT, out)
    assert hashlib.sha256(SNAPSHOT.read_bytes()).hexdigest() == before  # P1 snapshot never modified
    return SQLiteReferenceStore(out, P1_LAYOUT)


def test_registered_email_format():
    assert registered_email("Sara Al-Halabi", NID) == "sara.alhalabi.657@student.example"


def test_every_person_has_a_unique_registered_email(store):
    from agent.sqlite_util import session
    with session(lambda: sqlite3.connect(store.path)) as c:
        n_students = c.execute("SELECT count(*) FROM students").fetchone()[0]
        emails = [r[0] for r in c.execute("SELECT email FROM registered_emails")]
    assert len(emails) == n_students == len(set(emails))


def test_lookups(store):
    civil = store.lookup("civil_registry", NID)
    assert civil["full_name"] == "سارة الحلبي" and civil["father_name"] == "كمال"
    assert store.lookup("registered_emails", NID)["email"].endswith("@student.example")
    st = store.lookup("students", NID)
    assert st["status"] == "graduated" and st["student_id"]
    grad = store.lookup("graduates", NID)
    assert isinstance(grad["issued_documents"], list) and grad["issued_documents"]
    serial = grad["issued_documents"][0]["serial"]
    doc = store.lookup("graduation_documents", serial)
    assert doc["holder_national_number"] == NID
    assert store.lookup("masters_accepted", NID)["program_name"]
    assert store.lookup("civil_registry", "00000000000") is None


def test_transcript_by_student_number(store):
    sid = store.lookup("students", NID)["student_id"]
    t = store.get_transcript(sid)
    assert t["student"]["national_number"] == NID
    assert len(t["courses"]) >= 10 and {"course_code", "course_name", "grade", "credit_hours"} <= set(t["courses"][0])


def test_store_is_read_only(store):
    c = store._conn()
    try:
        with pytest.raises(sqlite3.OperationalError):
            c.execute("DELETE FROM students")
    finally:
        c.close()


def _open_handles(path: Path) -> int:
    """How many file descriptors of this process point at `path` (Linux /proc)."""
    fd_dir = Path("/proc/self/fd")
    n = 0
    for fd in fd_dir.iterdir():
        try:
            if Path(fd.readlink()) == path.resolve():
                n += 1
        except OSError:
            pass
    return n


@pytest.mark.skipif(not Path("/proc/self/fd").exists(), reason="needs /proc (Linux)")
def test_working_copy_is_closed_before_rename(tmp_path, monkeypatch):
    """On Windows a file with an open SQLite connection can't be renamed (WinError 32).
    Check, at the moment of the rename, that no connection to the file is still open."""
    seen = []
    real_replace = Path.replace

    def checked_replace(self, target):
        seen.append(_open_handles(Path(self)))
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", checked_replace)
    ensure_p1_db(SNAPSHOT, tmp_path / "registry_agent.sqlite")
    assert seen == [0]


def _qr_document(tmp_path, text):
    """A test page with a QR code encoding `text` (fixture only)."""
    import cv2
    import numpy as np
    from agent.documents import load_document
    qr = cv2.QRCodeEncoder.create().encode(text)
    qr = cv2.resize(qr, None, fx=6, fy=6, interpolation=cv2.INTER_NEAREST)
    page = np.full((900, 700), 255, np.uint8)
    page[500:500 + qr.shape[0], 420:420 + qr.shape[1]] = qr
    p = tmp_path / "certificate.png"
    cv2.imwrite(str(p), page)
    return load_document(p)


def test_read_qr_then_registry_lookup_by_token(store, tmp_path):
    import json
    from agent.datastore import SQLiteStateStore
    from agent.sqlite_util import session
    from agent.tools import ToolContext, execute_tool
    with session(lambda: sqlite3.connect(store.path)) as c:
        token, serial, holder = c.execute(
            "SELECT c.qr_token, c.serial, s.national_id FROM certificates c JOIN students s ON s.id = c.student_id "
            "LIMIT 1").fetchone()
    url = f"http://localhost:8001/verify/{token}"
    ctx = ToolContext("r1", store, SQLiteStateStore(tmp_path / "s.sqlite"), tmp_path,
                      documents=[_qr_document(tmp_path, url)])

    out = execute_tool(ctx, "read_qr", json.dumps({"document_number": 1}))
    assert out.error is None and out.result["found"] is True and out.result["codes"] == [url]

    rec = execute_tool(ctx, "lookup_registry", json.dumps({"registry": "qr_tokens", "key": token})).result
    assert rec["found"] and rec["record"]["serial"] == serial and rec["record"]["holder_national_number"] == holder
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert execute_tool(ctx, "lookup_registry", json.dumps({"registry": "qr_tokens", "key": tampered})).result["found"] is False


def test_read_qr_bad_document_number_is_an_error_result(store, tmp_path):
    import json
    from agent.datastore import SQLiteStateStore
    from agent.tools import ToolContext, execute_tool
    ctx = ToolContext("r1", store, SQLiteStateStore(tmp_path / "s.sqlite"), tmp_path)
    assert "no document" in execute_tool(ctx, "read_qr", json.dumps({"document_number": 3})).result["error"]


def test_read_qr_inside_a_run_sees_the_uploaded_documents(store, tmp_path, settings, make_llm):
    import json
    from agent.datastore import SQLiteStateStore
    from agent.run import run_agent
    from tests.fakes import ScriptedCompletion, text_reply, tool_reply
    doc = _qr_document(tmp_path, "http://localhost:8001/verify/abc")
    completion = ScriptedCompletion([
        tool_reply(("read_qr", {"document_number": 1})),
        text_reply(json.dumps({"decision": "needs_review", "reason": "", "extracted_fields": {}, "actions_taken": []})),
    ])
    r = run_agent("graduation_notice", "x@student.example", [doc.path], "baseline", settings=settings,
                  llm=make_llm(completion), reference_store=store, state_store=SQLiteStateStore(tmp_path / "st.sqlite"))
    qr_step = next(s for s in r.trace.steps if s.tool_name == "read_qr")
    assert qr_step.tool_result["codes"] == ["http://localhost:8001/verify/abc"]
    assert "read_qr" in {t["function"]["name"] for t in completion.calls[0]["tools"]}
