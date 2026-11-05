"""The thin data-access layer. The ONLY module that touches stored data.

Two separate concerns:

1. ReferenceStore (read-only): the user's existing registries and grades.
   NOT IMPLEMENTED YET: it depends on the real data's format and schema.
   Connecting the real data = writing one class that implements
   ReferenceStore and registering it in `get_reference_store()`. Tools, loop
   and tracing do not change.

2. StateStore (write): what the agent changes in the world: Master's
   registrations, issued graduation notices, outgoing email (outbox).
   `SQLiteStateStore` is the spec's fallback ("if it doesn't exist, create
   simple SQLite tables"). If the real data already has a place for these,
   implement StateStore over it instead.

   Every row is tagged with the run_id that wrote it, so a run's side
   effects are read back from the store itself, i.e. what actually happened,
   not what the model or the tool claimed.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


class DataNotConfiguredError(RuntimeError):
    pass


class UnknownRegistryError(KeyError):
    pass


# ---------------------------------------------------------------------------
# 1. Reference data (read-only)
# ---------------------------------------------------------------------------


@runtime_checkable
class ReferenceStore(Protocol):
    def registries(self) -> dict[str, str]:
        """Registry name -> description shown to the model (what it holds, what the key is).

        The names become the `registry` enum of the lookup_registry tool.
        """
        ...

    def lookup(self, registry: str, key: str) -> dict[str, Any] | None:
        """Return the record for `key` in `registry`, or None if not found.

        Raise UnknownRegistryError for a registry name not in registries().
        Must never modify the data.
        """
        ...

    def get_transcript(self, student_id: str) -> dict[str, Any] | None:
        """Return the student's courses and grades, or None if not found."""
        ...


class UnconfiguredReferenceStore:
    """Placeholder until the real data is connected. Fails loudly, never returns fake data."""

    MESSAGE = (
        "Reference data is not connected yet. Implement a ReferenceStore for your "
        "registries in agent/datastore.py and select it with REFERENCE_BACKEND."
    )

    def registries(self) -> dict[str, str]:
        raise DataNotConfiguredError(self.MESSAGE)

    def lookup(self, registry: str, key: str) -> dict[str, Any] | None:
        raise DataNotConfiguredError(self.MESSAGE)

    def get_transcript(self, student_id: str) -> dict[str, Any] | None:
        raise DataNotConfiguredError(self.MESSAGE)


def get_reference_store(backend: str, db_path: Path | None = None) -> ReferenceStore:
    """Factory selected by REFERENCE_BACKEND. Add the real backend here."""
    if not backend:
        return UnconfiguredReferenceStore()
    if backend == "mock_sqlite":  # TEMPORARY mock registry, see agent/reference_mock.py
        from agent.reference_mock import MOCK_LAYOUT
        from agent.reference_sqlite import SQLiteReferenceStore

        if db_path is None:
            raise DataNotConfiguredError("REFERENCE_DB_PATH is not set")
        return SQLiteReferenceStore(db_path, MOCK_LAYOUT)
    raise DataNotConfiguredError(f"Unknown REFERENCE_BACKEND {backend!r}. {UnconfiguredReferenceStore.MESSAGE}")


# ---------------------------------------------------------------------------
# 2. Agent-written state
# ---------------------------------------------------------------------------


@runtime_checkable
class StateStore(Protocol):
    def add_masters_registration(
        self, run_id: str, national_number: Any, full_name: Any, major: Any, notes: Any
    ) -> str: ...

    def add_graduation_notice(
        self, run_id: str, national_number: Any, full_name: Any, major: Any, graduation_date: Any, pdf_path: str
    ) -> str: ...

    def add_outbox_message(self, run_id: str, to: Any, subject: Any, body: Any, attachment_id: Any) -> str: ...

    def side_effects(self, run_id: str) -> dict[str, list[dict[str, Any]]]:
        """All rows written by this run, keyed by table: outbox, masters_registrations, graduation_notices."""
        ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS masters_registrations (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, created_at TEXT NOT NULL,
    national_number TEXT, full_name TEXT, major TEXT, notes TEXT
);
CREATE TABLE IF NOT EXISTS graduation_notices (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, created_at TEXT NOT NULL,
    national_number TEXT, full_name TEXT, major TEXT, graduation_date TEXT, pdf_path TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, created_at TEXT NOT NULL,
    to_address TEXT, subject TEXT, body TEXT, attachment_id TEXT
);
CREATE INDEX IF NOT EXISTS ix_reg_run ON masters_registrations(run_id);
CREATE INDEX IF NOT EXISTS ix_notice_run ON graduation_notices(run_id);
CREATE INDEX IF NOT EXISTS ix_outbox_run ON outbox(run_id);
"""

SIDE_EFFECT_TABLES = ("outbox", "masters_registrations", "graduation_notices")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_text(v: Any) -> str | None:
    """Store whatever the model passed. Non-strings are kept as JSON, not rejected."""
    if v is None or isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False)


class SQLiteStateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def _insert(self, table: str, prefix: str, row: dict[str, Any]) -> str:
        rid = f"{prefix}-{uuid.uuid4().hex[:12]}"
        row = {"id": rid, "created_at": _now(), **{k: _as_text(v) for k, v in row.items()}}
        cols = ", ".join(row)
        qs = ", ".join("?" for _ in row)
        with self._conn() as c:
            c.execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", list(row.values()))
        return rid

    def add_masters_registration(self, run_id, national_number, full_name, major, notes) -> str:
        return self._insert(
            "masters_registrations",
            "reg",
            {"run_id": run_id, "national_number": national_number, "full_name": full_name, "major": major, "notes": notes},
        )

    def add_graduation_notice(self, run_id, national_number, full_name, major, graduation_date, pdf_path) -> str:
        return self._insert(
            "graduation_notices",
            "notice",
            {
                "run_id": run_id,
                "national_number": national_number,
                "full_name": full_name,
                "major": major,
                "graduation_date": graduation_date,
                "pdf_path": pdf_path,
            },
        )

    def add_outbox_message(self, run_id, to, subject, body, attachment_id) -> str:
        return self._insert(
            "outbox",
            "msg",
            {"run_id": run_id, "to_address": to, "subject": subject, "body": body, "attachment_id": attachment_id},
        )

    def side_effects(self, run_id: str) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        with self._conn() as c:
            for t in SIDE_EFFECT_TABLES:
                rows = c.execute(f"SELECT * FROM {t} WHERE run_id = ? ORDER BY created_at", (run_id,)).fetchall()
                out[t] = [dict(r) for r in rows]
        return out
