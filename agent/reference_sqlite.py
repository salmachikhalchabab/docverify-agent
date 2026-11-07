"""Read-only ReferenceStore over a SQLite file, driven by a layout spec.

Generic: the spec says which table/key column backs each registry and how
to fetch a transcript. The temporary mock registry uses MOCK_LAYOUT
(agent/reference_mock.py). If the real data is also SQLite, it only needs
its own layout spec, not new code.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.datastore import DataNotConfiguredError, UnknownRegistryError
from agent.sqlite_util import session


@dataclass(frozen=True)
class RegistrySpec:
    table: str
    key_column: str
    description: str  # shown to the model in the lookup_registry tool schema
    # Optional: a SELECT with exactly one "?" (the key), for registries that join tables.
    # When set, it replaces `SELECT * FROM table WHERE key_column = ?`.
    sql: str | None = None
    # Columns that hold a JSON value (e.g. json_group_array) and are decoded before returning.
    json_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class SQLiteLayout:
    registries: dict[str, RegistrySpec]
    student_sql: str  # one row for the student, param: student_id
    courses_sql: str  # course rows, param: student_id


class SQLiteReferenceStore:
    def __init__(self, path: str | Path, layout: SQLiteLayout):
        self.path = Path(path)
        if not self.path.exists():
            raise DataNotConfiguredError(f"Reference database not found: {self.path}")
        self.layout = layout

    def _conn(self) -> sqlite3.Connection:
        # mode=ro: the agent can never modify reference data through this store.
        c = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)  # as_uri(): Windows-safe
        c.row_factory = sqlite3.Row
        return c

    def registries(self) -> dict[str, str]:
        return {name: spec.description for name, spec in self.layout.registries.items()}

    def lookup(self, registry: str, key: str) -> dict[str, Any] | None:
        spec = self.layout.registries.get(registry)
        if spec is None:
            raise UnknownRegistryError(registry)
        query = spec.sql or f'SELECT * FROM "{spec.table}" WHERE "{spec.key_column}" = ?'
        with session(self._conn) as c:
            row = c.execute(query, (key,)).fetchone()
        if not row:
            return None
        rec = dict(row)
        for col in spec.json_columns:
            if isinstance(rec.get(col), str):
                rec[col] = json.loads(rec[col])
        return rec

    def get_transcript(self, student_id: str) -> dict[str, Any] | None:
        with session(self._conn) as c:
            student = c.execute(self.layout.student_sql, (student_id,)).fetchone()
            if student is None:
                return None
            courses = c.execute(self.layout.courses_sql, (student_id,)).fetchall()
        return {"student": dict(student), "courses": [dict(r) for r in courses]}
