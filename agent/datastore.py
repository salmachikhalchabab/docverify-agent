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

from agent.sqlite_util import session


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
    if backend == "p1_sqlite":  # P1 synthetic registry, see agent/reference_p1.py
        from agent.reference_p1 import P1_LAYOUT, ensure_p1_db
        from agent.reference_sqlite import SQLiteReferenceStore

        if db_path is None:
            raise DataNotConfiguredError("REFERENCE_DB_PATH is not set")
        # The working copy is built from the untouched P1 snapshot next to it, on first use.
        ensure_p1_db(db_path.parent / "registry.snapshot.db", db_path)
        return SQLiteReferenceStore(db_path, P1_LAYOUT)
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

    # -- used only by the guarded variant (the baseline never calls these) --------------------

    def claim_idempotency_key(self, key: str, run_id: str, max_age_s: float | None = None) -> dict[str, Any] | None:
        """Record `key` for this run. Returns None if it was new (or its earlier claim is older than
        max_age_s, which is then replaced), else the earlier claim (key already used)."""
        ...

    def get_idempotency_key(self, key: str) -> dict[str, Any] | None:
        """The earlier claim of `key`, or None (read only)."""
        ...

    def add_pending_action(self, run_id: str, seq: int, tool: str, arguments: dict[str, Any], reason: str) -> str: ...

    def pending_actions(self, run_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]: ...

    def set_pending_status(self, action_id: str, status: str, reviewer: str, result: Any = None) -> None: ...

    # one case per run that a staff member must decide (human-in-the-loop versions)
    def open_staff_case(self, run_id: str, service: str, recommendation: str | None, reasons: list[str],
                        agent_decision: str | None) -> None: ...

    def staff_cases(self, status: str | None = None) -> list[dict[str, Any]]: ...

    def staff_case(self, run_id: str) -> dict[str, Any] | None: ...

    def close_staff_case(self, run_id: str, decision: str, reviewer: str, note: str = "") -> None: ...


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
-- guarded variant: one row per (service, person, action) ever performed -> no duplicate requests
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT PRIMARY KEY, run_id TEXT NOT NULL, created_at TEXT NOT NULL
);
-- guarded variant with human approval: actions the agent asked for, waiting for a staff member
CREATE TABLE IF NOT EXISTS pending_actions (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, created_at TEXT NOT NULL, seq INTEGER NOT NULL,
    tool TEXT NOT NULL, arguments TEXT NOT NULL, reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected
    reviewer TEXT, reviewed_at TEXT, result TEXT
);
CREATE INDEX IF NOT EXISTS ix_pending_run ON pending_actions(run_id);
-- human-in-the-loop versions: every request waits here for a staff member's final decision
CREATE TABLE IF NOT EXISTS staff_cases (
    run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, service TEXT,
    recommendation TEXT,          -- what the rules recommend (approved / needs_review / rejected; null = no rules)
    reasons TEXT,                 -- JSON list: the problems the rules found
    agent_decision TEXT,          -- what the AI said
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | decided
    decision TEXT,                -- the staff member's final decision: approved | rejected
    reviewer TEXT, reviewed_at TEXT, note TEXT
);
"""

SIDE_EFFECT_TABLES = ("outbox", "masters_registrations", "graduation_notices")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_s(iso: str) -> float:
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()


def _as_text(v: Any) -> str | None:
    """Store whatever the model passed. Non-strings are kept as JSON, not rejected."""
    if v is None or isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False)


class SQLiteStateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with session(self._conn) as c:
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
        with session(self._conn) as c:
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

    def claim_idempotency_key(self, key: str, run_id: str, max_age_s: float | None = None) -> dict[str, Any] | None:
        with session(self._conn) as c:
            cur = c.execute("INSERT OR IGNORE INTO idempotency_keys (key, run_id, created_at) VALUES (?, ?, ?)",
                            (key, run_id, _now()))
            if cur.rowcount == 1:
                return None
            row = c.execute("SELECT * FROM idempotency_keys WHERE key = ?", (key,)).fetchone()
            if row and max_age_s is not None and _age_s(row["created_at"]) > max_age_s:
                c.execute("UPDATE idempotency_keys SET run_id = ?, created_at = ? WHERE key = ?", (run_id, _now(), key))
                return None
            return dict(row) if row else None

    def get_idempotency_key(self, key: str) -> dict[str, Any] | None:
        with session(self._conn) as c:
            row = c.execute("SELECT * FROM idempotency_keys WHERE key = ?", (key,)).fetchone()
        return dict(row) if row else None

    def add_pending_action(self, run_id: str, seq: int, tool: str, arguments: dict[str, Any], reason: str) -> str:
        return self._insert("pending_actions", "pending", {
            "run_id": run_id, "seq": seq, "tool": tool,
            "arguments": json.dumps(arguments, ensure_ascii=False), "reason": reason, "status": "pending"})

    def pending_actions(self, run_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM pending_actions WHERE 1=1", []
        if run_id is not None:
            q, args = q + " AND run_id = ?", [*args, run_id]
        if status is not None:
            q, args = q + " AND status = ?", [*args, status]
        with session(self._conn) as c:
            rows = [dict(r) for r in c.execute(q + " ORDER BY created_at, seq", args).fetchall()]
        for r in rows:
            r["arguments"] = json.loads(r["arguments"])
            r["result"] = json.loads(r["result"]) if r.get("result") else None
        return rows

    def open_staff_case(self, run_id: str, service: str, recommendation: str | None, reasons: list[str],
                        agent_decision: str | None) -> None:
        with session(self._conn) as c:
            c.execute("INSERT OR IGNORE INTO staff_cases (run_id, created_at, service, recommendation, reasons, "
                      "agent_decision, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                      (run_id, _now(), service, recommendation, json.dumps(reasons, ensure_ascii=False), agent_decision))

    def staff_cases(self, status: str | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM staff_cases", []
        if status:
            q, args = q + " WHERE status = ?", [status]
        with session(self._conn) as c:
            rows = [dict(r) for r in c.execute(q + " ORDER BY created_at", args).fetchall()]
        for r in rows:
            r["reasons"] = json.loads(r["reasons"] or "[]")
        return rows

    def staff_case(self, run_id: str) -> dict[str, Any] | None:
        with session(self._conn) as c:
            row = c.execute("SELECT * FROM staff_cases WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["reasons"] = json.loads(out["reasons"] or "[]")
        return out

    def close_staff_case(self, run_id: str, decision: str, reviewer: str, note: str = "") -> None:
        with session(self._conn) as c:
            c.execute("UPDATE staff_cases SET status = 'decided', decision = ?, reviewer = ?, reviewed_at = ?, note = ? "
                      "WHERE run_id = ?", (decision, reviewer, _now(), note, run_id))

    def set_pending_status(self, action_id: str, status: str, reviewer: str, result: Any = None) -> None:
        with session(self._conn) as c:
            c.execute("UPDATE pending_actions SET status = ?, reviewer = ?, reviewed_at = ?, result = ? WHERE id = ?",
                      (status, reviewer, _now(), json.dumps(result, ensure_ascii=False, default=str), action_id))

    def side_effects(self, run_id: str) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        with session(self._conn) as c:
            for t in SIDE_EFFECT_TABLES:
                rows = c.execute(f"SELECT * FROM {t} WHERE run_id = ? ORDER BY created_at", (run_id,)).fetchall()
                out[t] = [dict(r) for r in rows]
        return out
