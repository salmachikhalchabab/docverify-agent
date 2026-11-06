"""Layout of the P1 synthetic registry (data/p1/), selected with REFERENCE_BACKEND=p1_sqlite.

P1 is a separate project that generated a synthetic Damascus University registry and documents
(all data fictional, marked "نموذج تجريبي - بيانات وهمية"). This agent uses ONLY its data: a copy
of its registry snapshot, read directly (no P1 server, keys or QR secret needed).

The original snapshot (data/p1/registry.snapshot.db) is never modified. ensure_p1_db() builds the
working copy the agent reads (data/p1/registry_agent.sqlite): the snapshot plus one table P1 does
not have, `registered_emails` (one deterministic, fictional address per person, needed because
the agent emails results).
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from pathlib import Path

from agent.reference_sqlite import RegistrySpec, SQLiteLayout
from agent.sqlite_util import session

EMAIL_DOMAIN = "student.example"  # reserved TLD: can never be a real mailbox


def registered_email(full_name_en: str, national_id: str) -> str:
    """e.g. ("Sara Al-Halabi", "11490564657") -> sara.alhalabi.657@student.example"""
    parts = [re.sub(r"[^a-z]", "", p) for p in full_name_en.lower().split()]
    local = ".".join(p for p in parts if p)
    return f"{local}.{national_id[-3:]}@{EMAIL_DOMAIN}"


def ensure_p1_db(snapshot: Path, out: Path, force: bool = False) -> Path:
    """Build the agent's working copy from the P1 snapshot (idempotent)."""
    if out.exists() and not force:
        return out
    if not snapshot.exists():
        raise FileNotFoundError(f"P1 registry snapshot not found: {snapshot}")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    shutil.copyfile(snapshot, tmp)
    with session(lambda: sqlite3.connect(tmp)) as c:  # closed before the rename (Windows)
        c.execute("CREATE TABLE registered_emails (national_id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE)")
        rows = c.execute("SELECT national_id, full_name_en FROM students").fetchall()
        c.executemany("INSERT INTO registered_emails VALUES (?, ?)",
                      [(nid, registered_email(name, nid)) for nid, name in rows])
        c.execute("CREATE TABLE _agent_meta (key TEXT PRIMARY KEY, value TEXT)")
        c.executemany("INSERT INTO _agent_meta VALUES (?, ?)", [
            ("source", "copy of P1 registry.snapshot.db (synthetic data)"),
            ("added", "registered_emails: deterministic fictional addresses, not part of P1"),
        ])
    tmp.replace(out)
    return out


P1_LAYOUT = SQLiteLayout(
    registries={
        "civil_registry": RegistrySpec(
            "students", "national_id",
            "Civil registry data of a person (name, parents, birth date and place, nationality). "
            "Key: national number (11 digits).",
            sql="SELECT national_id AS national_number, full_name_ar AS full_name, first_name_ar AS first_name, "
                "last_name_ar AS family_name, full_name_en, father_name, mother_name, gender, birth_date, "
                "birth_place, nationality FROM students WHERE national_id = ?",
        ),
        "registered_emails": RegistrySpec(
            "registered_emails", "national_id",
            "The email address officially registered for a person. Key: national number (11 digits).",
            sql="SELECT national_id AS national_number, email FROM registered_emails WHERE national_id = ?",
        ),
        "students": RegistrySpec(
            "students", "national_id",
            "University student records: student number (the student_id used by get_transcript), faculty, "
            "specialization, status (active / graduated / dropped), GPA, registry remarks. "
            "Key: national number (11 digits).",
            sql="SELECT national_id AS national_number, student_number AS student_id, full_name_ar AS full_name, "
                "faculty, specialization, degree, status, gpa, rating, academic_year, exam_session, "
                "graduation_date, remarks FROM students WHERE national_id = ?",
        ),
        "employment_registry": RegistrySpec(
            "employment_registry", "national_id",
            "State employment register: whether a person is employed by the state, by whom, and when this "
            "was last checked. A person with no record is not employed. Key: national number (11 digits).",
            sql="SELECT national_id AS national_number, employed, employer, checked_at "
                "FROM employment_registry WHERE national_id = ?",
        ),
        "graduates": RegistrySpec(
            "students", "national_id",
            "Graduates: degree, specialization, GPA, graduation date, and every graduation document issued "
            "to them (serial number, kind, issue date, revoked or not). Key: national number (11 digits).",
            sql="SELECT s.national_id AS national_number, s.full_name_ar AS full_name, s.faculty, s.specialization, "
                "s.degree, s.gpa, s.rating, s.academic_year, s.exam_session, s.graduation_date, "
                "(SELECT json_group_array(json_object('serial', c.serial, 'document_kind', c.document_kind, "
                "'issued_at', c.issued_at, 'revoked', json(CASE WHEN c.revoked THEN 'true' ELSE 'false' END), "
                "'council_decision_number', c.council_decision_number, "
                "'council_decision_date', c.council_decision_date)) "
                "FROM certificates c WHERE c.student_id = s.id) AS issued_documents "
                "FROM students s WHERE s.national_id = ? AND s.status = 'graduated'",
            json_columns=("issued_documents",),
        ),
        "graduation_documents": RegistrySpec(
            "certificates", "serial",
            "Issued graduation documents (certificates and notices) by serial number: kind, issue date, "
            "revoked or not, and the holder. Key: the serial number printed on the document.",
            sql="SELECT c.serial, c.document_kind, c.issued_at, c.revoked, c.council_decision_number, "
                "c.council_decision_date, s.national_id AS holder_national_number, s.full_name_ar AS holder_name "
                "FROM certificates c JOIN students s ON s.id = c.student_id WHERE c.serial = ?",
        ),
        "qr_tokens": RegistrySpec(
            "certificates", "qr_token",
            "The registry record behind a document's QR code: which document it is (serial, kind, issue date), "
            "whether it is revoked, how many times its QR was already used, and its holder. "
            "Key: the token, i.e. the last part of the QR URL after /verify/.",
            sql="SELECT c.serial, c.document_kind, c.issued_at, c.revoked, c.qr_used_count, "
                "s.national_id AS holder_national_number, s.full_name_ar AS holder_name, s.faculty, "
                "s.specialization, s.gpa, s.graduation_date "
                "FROM certificates c JOIN students s ON s.id = c.student_id WHERE c.qr_token = ?",
        ),
        "masters_accepted": RegistrySpec(
            "master_accepted", "national_id",
            "Master's admission list: the program a person was accepted to and their status "
            "(accepted / enrolled). Key: national number (11 digits).",
            sql="SELECT a.national_id AS national_number, a.program_id, p.name AS program_name, "
                "p.name_en AS program_name_en, a.status FROM master_accepted a "
                "JOIN master_programs p ON p.id = a.program_id WHERE a.national_id = ?",
        ),
    },
    student_sql="SELECT student_number AS student_id, national_id AS national_number, full_name_ar AS full_name, "
    "faculty, specialization, status, gpa, rating FROM students WHERE student_number = ?",
    courses_sql="SELECT c.code AS course_code, c.name_ar AS course_name, c.name_en AS course_name_en, "
    "g.semester, g.credits AS credit_hours, g.mark AS grade FROM grades g "
    "JOIN courses c ON c.id = g.course_id JOIN students s ON s.id = g.student_id "
    "WHERE s.student_number = ? ORDER BY g.semester, c.code",
)
