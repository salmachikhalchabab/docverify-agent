"""SQLite helper that always closes the connection.

`with sqlite3.connect(...) as c:` commits or rolls back but does NOT close the connection.
On Windows an open connection keeps the database file locked, so renaming, replacing or
deleting it fails (WinError 32). Use `session()` instead.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager


@contextmanager
def session(connect: Callable[[], sqlite3.Connection]) -> Iterator[sqlite3.Connection]:
    """Open with `connect()`, commit on success / roll back on error, and always close."""
    c = connect()
    try:
        with c:
            yield c
    finally:
        c.close()
