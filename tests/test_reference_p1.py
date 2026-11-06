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
