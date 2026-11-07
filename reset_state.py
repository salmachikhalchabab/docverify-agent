"""Reset agent-written state between test runs.

Deletes the state database (outbox, Master's registrations, graduation
notices) and generated notice PDFs. Never touches reference data or
runs/traces.jsonl.

    python reset_state.py
"""

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import get_settings  # noqa: E402

s = get_settings()
s.state_db_path.unlink(missing_ok=True)
shutil.rmtree(s.artifacts_dir / "notices", ignore_errors=True)
print(f"reset: {s.state_db_path} and {s.artifacts_dir / 'notices'}")
