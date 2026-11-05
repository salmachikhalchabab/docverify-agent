"""Build data/mock_registry.sqlite: TEMPORARY MOCK TEST DATA.

One FICTIONAL person, نور السيد (the user's test documents are marked
"نموذج تجريبي - بيانات وهمية"). Records are transcribed from:
- data/documents/non_employment_certificate_test.webp  (no. 2573370)
- data/documents/transcript_test.png                   (25 courses, GPA 70.59)
- data/documents/graduation_notice_test.webp           (notice no. 8629, 2024/2025, التكميلية)
- data/documents/national_id_test.png (generated from the civil record by build_test_national_id.py)
Only the registered email is invented.

Not in this registry on purpose: graduation certificate no. 3698
(data/documents/graduation_certificate_test.webp; AI major, GPA 79.06). It
contradicts نور's documents, so it is not his and has no record here.

The "temporary test data" marker lives in the _meta table and the file
name only, NOT inside the records returned to the model.

Usage:  python data/build_mock_registry.py   (overwrites the file)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

OUT = Path(__file__).resolve().parent / "mock_registry.sqlite"

NN = "14749200009"
SID = "201400001"  # الرقم الجامعي on the transcript
NAME = "نور السيد"
FACULTY = "كلية الهندسة المعلوماتية - جامعة دمشق"
MAJOR = "الشبكات والنظم الحاسوبية"

SCHEMA = """
CREATE TABLE _meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE civil_registry (
    national_number TEXT PRIMARY KEY, first_name TEXT, family_name TEXT, full_name TEXT,
    father_name TEXT, mother_name TEXT, gender TEXT, birth_place TEXT, birth_date TEXT, nationality TEXT);
CREATE TABLE registered_emails (national_number TEXT PRIMARY KEY, email TEXT);
CREATE TABLE non_employment_certificates (
    certificate_number TEXT PRIMARY KEY, request_number TEXT, receipt_number TEXT,
    national_number TEXT, full_name TEXT, issuing_authority TEXT, request_date TEXT,
    issue_date TEXT, valid_until TEXT, status TEXT);
CREATE TABLE students (
    national_number TEXT PRIMARY KEY, student_id TEXT, full_name TEXT, faculty TEXT, major TEXT,
    first_registration_year TEXT, status TEXT, gpa REAL, grade_label TEXT, courses_completed INTEGER);
CREATE TABLE grades (
    student_id TEXT, course_code TEXT, course_name TEXT, academic_year TEXT, semester TEXT,
    credit_hours INTEGER, grade REAL, result TEXT, PRIMARY KEY (student_id, course_code));
CREATE TABLE graduates (
    graduation_document_number TEXT PRIMARY KEY, document_type TEXT, national_number TEXT,
    student_id TEXT, full_name TEXT, father_name TEXT, faculty TEXT, degree TEXT, major TEXT,
    gpa REAL, gpa_in_words TEXT, grade_label TEXT, academic_year TEXT, exam_session TEXT,
    document_date TEXT);
"""

# (code, name, academic_year, semester, hours, grade) - transcribed from transcript_test.png
COURSES = [
    ("IE101", "التحليل الرياضي 1", "2020/2021", "الأول", 4, 75),
    ("IE103", "أساسيات البرمجة", "2020/2021", "الأول", 5, 77),
    ("IE105", "اللغة الإنكليزية 1", "2020/2021", "الأول", 2, 83),
    ("IE102", "الجبر الخطي", "2020/2021", "الثاني", 4, 77),
    ("IE104", "الفيزياء", "2020/2021", "الثاني", 4, 69),
    ("IE106", "الثقافة القومية", "2020/2021", "الثاني", 2, 76),
    ("IE201", "بنى المعطيات", "2021/2022", "الأول", 5, 60),
    ("IE203", "الدارات المنطقية", "2021/2022", "الأول", 4, 74),
    ("IE205", "اللغة الإنكليزية 2", "2021/2022", "الأول", 2, 69),
    ("IE202", "الإحصاء والاحتمالات", "2021/2022", "الثاني", 4, 74),
    ("IE204", "البرمجة غرضية التوجه", "2021/2022", "الثاني", 5, 75),
    ("IE301", "قواعد المعطيات", "2022/2023", "الأول", 5, 71),
    ("IE303", "شبكات الحاسوب", "2022/2023", "الأول", 4, 84),
    ("IE305", "هندسة البرمجيات 1", "2022/2023", "الأول", 4, 77),
    ("IE302", "نظم التشغيل", "2022/2023", "الثاني", 5, 71),
    ("IE304", "الخوارزميات", "2022/2023", "الثاني", 5, 63),
    ("IE401", "الذكاء الصنعي", "2023/2024", "الأول", 5, 69),
    ("IE403", "أمن المعلومات", "2023/2024", "الأول", 4, 73),
    ("IE405", "المترجمات", "2023/2024", "الأول", 4, 68),
    ("IE402", "تعلم الآلة", "2023/2024", "الثاني", 5, 63),
    ("IE404", "الرؤية الحاسوبية", "2023/2024", "الثاني", 4, 64),
    ("IE501", "مشروع التخرج", "2024/2025", "الأول", 6, 66),
    ("IE503", "إدارة المشاريع البرمجية", "2024/2025", "الأول", 4, 74),
    ("IE502", "معالجة اللغات الطبيعية", "2024/2025", "الثاني", 4, 67),
    ("IE504", "النظم الموزعة", "2024/2025", "الثاني", 4, 60),
]

ROWS = {
    "_meta": [
        ("notice", "TEMPORARY MOCK TEST DATA. Fictional person. Not real records. Replace with real registries."),
        ("created", "2026-11-05"),
        ("person", "نور السيد, national number 14749200009 (fictional)"),
        ("sources", "non_employment_certificate_test.webp, transcript_test.png, graduation_notice_test.webp"),
    ],
    # gender: the graduation notice says "الطالب ... بن فايز"
    "civil_registry": [(NN, "نور", "السيد", NAME, "فايز", "رغداء الزعبي", "male", "اللاذقية", "1996-07-20", "عربية سورية")],
    "registered_emails": [(NN, "nour.alsayed@example.com")],  # invented
    "non_employment_certificates": [
        # valid 3 months from its date (per the document's note) -> expired by 2026-11
        ("2573370", "8629", "88", NN, NAME, "وزارة التنمية الإدارية - السجل العام للعاملين في الدولة",
         "2025-12-03", "2025-12-12", "2026-03-12", "not_employed"),
    ],
    "students": [(NN, SID, NAME, FACULTY, MAJOR, "2020/2021", "graduated", 70.59, "جيد", 25)],
    "grades": [(SID, c, n, y, s, h, g, "ناجح") for c, n, y, s, h, g in COURSES],
    "graduates": [
        ("8629", "إشعار تخرج", NN, SID, NAME, "فايز", FACULTY, "الإجازة", MAJOR, 70.59,
         "سبعون درجة وتسع وخمسون جزءاً من مائة", "جيد", "2024/2025", "التكميلية", "2025-12-12"),
    ],
}


def main() -> None:
    OUT.unlink(missing_ok=True)
    with sqlite3.connect(OUT) as c:
        c.executescript(SCHEMA)
        for table, rows in ROWS.items():
            qs = ",".join("?" * len(rows[0]))
            c.executemany(f"INSERT INTO {table} VALUES ({qs})", rows)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
