import sys
from dataclasses import replace
from pathlib import Path

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.datastore import SQLiteStateStore  # noqa: E402
from agent.llm import LLMClient  # noqa: E402
from config import Settings  # noqa: E402
from tests.fakes import FAKE_MODEL, FakeReferenceStore  # noqa: E402

TEST_PRICES = {FAKE_MODEL: {"input_per_mtok": 3.0, "output_per_mtok": 15.0}}


@pytest.fixture
def settings(tmp_path):
    runs = tmp_path / "runs"
    return replace(
        Settings(),
        llm_model=FAKE_MODEL,
        max_steps=25,
        runs_dir=runs,
        traces_path=runs / "traces.jsonl",
        state_db_path=runs / "state.sqlite",
        artifacts_dir=runs / "artifacts",
        price_table=TEST_PRICES,
    )


@pytest.fixture
def make_llm():
    def _make(completion):
        return LLMClient(FAKE_MODEL, max_tokens=1000, price_table=TEST_PRICES, completion_fn=completion)
    return _make


@pytest.fixture
def stores(settings):
    return FakeReferenceStore(), SQLiteStateStore(settings.state_db_path)


@pytest.fixture
def pdf_with_hidden_text(tmp_path):
    """A one-page PDF: visible text + white, 1pt text (invisible when rendered)."""
    p = tmp_path / "doc.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "VISIBLE HEADER", fontsize=20)
    page.insert_text((72, 700), "HIDDEN INSTRUCTION TEXT", fontsize=1, color=(1, 1, 1))
    doc.save(p)
    doc.close()
    return p
