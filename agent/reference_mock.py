"""TEMPORARY: layout of the mock registry (data/mock_registry.sqlite).

Mock test data only, for end-to-end smoke tests until the real registries
arrive. Selected with REFERENCE_BACKEND=mock_sqlite. Delete this module and
data/mock_registry.sqlite when the real data is connected.
"""

from agent.reference_sqlite import RegistrySpec, SQLiteLayout

MOCK_LAYOUT = SQLiteLayout(
    registries={
        "civil_registry": RegistrySpec(
            "civil_registry", "national_number",
            "National civil registry (ID card data). Key: national number (11 digits).",
        ),
        "registered_emails": RegistrySpec(
            "registered_emails", "national_number",
            "The email address officially registered for a person. Key: national number (11 digits).",
        ),
        "non_employment_certificates": RegistrySpec(
            "non_employment_certificates", "certificate_number",
            "Issued non-employment certificates. Key: certificate number (الرقم) as printed, digits only.",
        ),
        "students": RegistrySpec(
            "students", "national_number",
            "University student records, including the student_id used by get_transcript. "
            "Key: national number (11 digits).",
        ),
        "qr_tokens": RegistrySpec(
            "qr_tokens", "qr_token",
            "The registry record behind a document's QR code: which document it is (serial, kind, issue date), "
            "whether it is revoked, and its holder. Key: the token, i.e. the last part of the QR URL after /verify/.",
        ),
        "graduates": RegistrySpec(
            "graduates", "national_number",
            "Graduates: degree, major, GPA, academic year and the serial number of their issued "
            "graduation document (certificate or notice). Key: national number (11 digits).",
        ),
    },
    student_sql="SELECT student_id, national_number, full_name, faculty, major, first_registration_year, "
    "status, gpa, grade_label, courses_completed FROM students WHERE student_id = ?",
    courses_sql="SELECT course_code, course_name, academic_year, semester, credit_hours, grade, result "
    "FROM grades WHERE student_id = ? ORDER BY academic_year, CASE semester WHEN 'الأول' THEN 1 ELSE 2 END, course_code",
)
