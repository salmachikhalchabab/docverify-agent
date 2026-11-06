"""Write data/p1/registered_emails.csv: every P1 person with the registered email the agent uses.

The emails are fictional and deterministic (agent/reference_p1.registered_email); P1 itself has
none. The CSV is UTF-8 with a BOM so Excel shows the Arabic names correctly.

    python data/p1/export_emails.py
"""

from __future__ import annotations

import csv
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent.reference_p1 import registered_email  # noqa: E402

SNAPSHOT = ROOT / "data" / "p1" / "registry.snapshot.db"
OUT = ROOT / "data" / "p1" / "registered_emails.csv"


def main() -> None:
    with closing(sqlite3.connect(SNAPSHOT.resolve().as_uri() + "?mode=ro", uri=True)) as c:
        rows = c.execute(
            "SELECT national_id, full_name_ar, full_name_en, status FROM students ORDER BY full_name_en, national_id"
        ).fetchall()
    with OUT.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["national_id", "full_name_ar", "full_name_en", "status", "registered_email"])
        for nid, ar, en, status in rows:
            w.writerow([nid, ar, en, status, registered_email(en, nid)])
    print(f"wrote {OUT.relative_to(ROOT)} ({len(rows)} people)")


if __name__ == "__main__":
    main()
