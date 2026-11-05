"""Settings, paths and the fallback price table.

All settings come from environment variables (optionally loaded from .env).
Nothing here assumes anything about the layout of the reference data.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env", override=False)


# Fallback prices for models LiteLLM's cost map does not know.
# Used ONLY when litellm.completion_cost() fails for a response.
# Format: model string exactly as in LLM_MODEL ->
#   {"input_per_mtok": USD per 1M input tokens, "output_per_mtok": USD per 1M output tokens}
# Left empty on purpose: fill in from the provider's price page for the model
# you choose. If a model is in neither LiteLLM nor this table, its cost is
# recorded as null (and the run is marked cost_complete=false), never as 0.
PRICE_TABLE: dict[str, dict[str, float]] = {
    # "my-provider/my-model": {"input_per_mtok": 0.0, "output_per_mtok": 0.0},
    # OpenRouter ":free" model variants cost nothing (rate-limited instead).
    "openrouter/google/gemma-4-26b-a4b-it:free": {"input_per_mtok": 0.0, "output_per_mtok": 0.0},
    "openrouter/nvidia/nemotron-3.5-super-vl:free": {"input_per_mtok": 0.0, "output_per_mtok": 0.0},
}


# Fonts tried (in order) for the generated notice PDF when NOTICE_FONT_PATH is unset.
# PyMuPDF's built-in fonts have no Arabic glyphs; these do.
_NOTICE_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
)


def _notice_font() -> Path | None:
    v = os.getenv("NOTICE_FONT_PATH", "").strip()
    if v:
        return Path(v)
    return next((Path(p) for p in _NOTICE_FONT_CANDIDATES if Path(p).exists()), None)


def _opt_float(name: str) -> float | None:
    v = os.getenv(name, "").strip()
    return float(v) if v else None


def _opt_int(name: str) -> int | None:
    v = os.getenv(name, "").strip()
    return int(v) if v else None


def _int(name: str, default: int) -> int:
    v = os.getenv(name, "").strip()
    return int(v) if v else default


def _path(name: str, default: Path) -> Path:
    v = os.getenv(name, "").strip()
    p = Path(v) if v else default
    return p if p.is_absolute() else PROJECT_ROOT / p


@dataclass(frozen=True)
class Settings:
    # model
    llm_model: str = ""
    llm_temperature: float | None = None
    llm_max_tokens: int | None = 16000
    llm_timeout_s: int = 120
    llm_num_retries: int = 2
    # agent
    agent_variant: str = "baseline"
    max_steps: int = 25
    # documents
    page_render_dpi: int = 150
    max_image_side: int = 2000
    max_pages_per_document: int = 10
    ocr_engine: str = "none"
    ocr_lang: str = "eng"
    # output / state
    runs_dir: Path = PROJECT_ROOT / "runs"
    traces_path: Path = PROJECT_ROOT / "runs" / "traces.jsonl"
    state_db_path: Path = PROJECT_ROOT / "runs" / "state.sqlite"
    artifacts_dir: Path = PROJECT_ROOT / "runs" / "artifacts"
    notice_font_path: Path | None = None
    # reference data
    reference_backend: str = ""
    reference_db_path: Path | None = None
    price_table: dict[str, dict[str, float]] = field(default_factory=lambda: dict(PRICE_TABLE))

    @classmethod
    def from_env(cls) -> "Settings":
        runs_dir = _path("RUNS_DIR", PROJECT_ROOT / "runs")
        return cls(
            llm_model=os.getenv("LLM_MODEL", "").strip(),
            llm_temperature=_opt_float("LLM_TEMPERATURE"),
            llm_max_tokens=_opt_int("LLM_MAX_TOKENS"),
            llm_timeout_s=_int("LLM_TIMEOUT_S", 120),
            llm_num_retries=_int("LLM_NUM_RETRIES", 2),
            agent_variant=os.getenv("AGENT_VARIANT", "baseline").strip() or "baseline",
            max_steps=_int("MAX_STEPS", 25),
            page_render_dpi=_int("PAGE_RENDER_DPI", 150),
            max_image_side=_int("MAX_IMAGE_SIDE", 2000),
            max_pages_per_document=_int("MAX_PAGES_PER_DOCUMENT", 10),
            ocr_engine=os.getenv("OCR_ENGINE", "none").strip().lower() or "none",
            ocr_lang=os.getenv("OCR_LANG", "eng").strip() or "eng",
            runs_dir=runs_dir,
            traces_path=_path("TRACES_PATH", runs_dir / "traces.jsonl"),
            state_db_path=_path("STATE_DB_PATH", runs_dir / "state.sqlite"),
            artifacts_dir=_path("ARTIFACTS_DIR", runs_dir / "artifacts"),
            notice_font_path=_notice_font(),
            reference_backend=os.getenv("REFERENCE_BACKEND", "").strip(),
            reference_db_path=_path("REFERENCE_DB_PATH", PROJECT_ROOT / "data" / "mock_registry.sqlite")
            if os.getenv("REFERENCE_BACKEND", "").strip() else None,
        )


def get_settings() -> Settings:
    """Read settings fresh from the environment (cheap; no caching surprises)."""
    return Settings.from_env()
