"""Small local web UI for the agent: start a run, watch it step by step, browse past runs.

    python ui/app.py            # serves on http://0.0.0.0:8000
    PORT=8080 python ui/app.py

It calls the same run_agent() as the CLI and the evaluation; it adds no logic
of its own to the agent. One run at a time (free-tier rate limits).
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
import traceback
import uuid
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402
from fastapi import FastAPI, File, Form, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, Response  # noqa: E402

from agent.documents import IMAGE_SUFFIXES, PDF_SUFFIXES, load_document  # noqa: E402
from agent.run import run_agent  # noqa: E402
from agent.trace import read_traces  # noqa: E402
from config import get_settings  # noqa: E402
from evaluation.run_eval import CASES_PATH  # noqa: E402
from evaluation.scoring import load_cases  # noqa: E402

app = FastAPI(title="docverify UI")
STATIC = Path(__file__).resolve().parent / "static"
DOC_DIRS = {"test documents": ROOT / "data" / "documents", "evaluation documents": ROOT / "data" / "eval_documents"}
ALLOWED_SUFFIXES = PDF_SUFFIXES | IMAGE_SUFFIXES

JOBS: dict[str, dict[str, Any]] = {}
RUN_LOCK = threading.Lock()


def _settings():
    return get_settings()


def _upload_root() -> Path:
    return _settings().runs_dir / "uploads"


def _safe_path(rel: str, roots: list[Path]) -> Path:
    """Resolve a user-supplied path and refuse anything outside the allowed folders."""
    p = (ROOT / rel).resolve() if not Path(rel).is_absolute() else Path(rel).resolve()
    for r in roots:
        try:
            p.relative_to(r.resolve())
            if p.is_file():
                return p
        except ValueError:
            continue
    raise HTTPException(404, "file not found")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/api/config")
def config() -> dict[str, Any]:
    s = _settings()
    docs = []
    for group, d in DOC_DIRS.items():
        for f in sorted(d.glob("*")) if d.exists() else []:
            if f.suffix.lower() in ALLOWED_SUFFIXES:
                docs.append({"group": group, "name": f.name, "path": f.relative_to(ROOT).as_posix()})
    try:
        cases = [
            {"id": c.id, "category": c.category, "description": c.description, "service": c.service,
             "requester_email": c.requester_email, "documents": c.documents, "today": c.today,
             "expected_decisions": c.expected_decisions, "attack": c.attack}
            for c in load_cases(CASES_PATH)
        ]
    except Exception:
        cases = []
    return {
        "model": s.llm_model, "variant": s.agent_variant, "max_steps": s.max_steps,
        "reference_backend": s.reference_backend or None, "documents": docs, "cases": cases,
        "today": date.today().isoformat(),
    }


def _job_worker(job_id: str, kwargs: dict[str, Any]) -> None:
    job = JOBS[job_id]
    try:
        r = run_agent(**kwargs, on_step=lambda st: job["steps"].append(asdict(st)))
        job["run_id"] = r.run_id
        job["trace"] = r.trace.to_dict()
        job["status"] = "done"
    except Exception as e:  # configuration problems (e.g. data not connected) end up here
        job["status"] = "failed"
        job["error"] = f"{type(e).__name__}: {e}"
        job["traceback"] = traceback.format_exc(limit=5)
    finally:
        job["finished_at"] = time.time()
        RUN_LOCK.release()


@app.post("/api/run")
async def start_run(
    service: str = Form(...),
    requester_email: str = Form(...),
    today: str = Form(""),
    max_steps: str = Form(""),
    existing: list[str] = Form(default=[]),
    files: list[UploadFile] = File(default=[]),
) -> dict[str, Any]:
    if not RUN_LOCK.acquire(blocking=False):
        raise HTTPException(409, "a run is already in progress")
    try:
        job_id = uuid.uuid4().hex[:12]
        doc_paths: list[str] = []
        for rel in existing:
            doc_paths.append(str(_safe_path(rel, list(DOC_DIRS.values()))))
        up_dir = _upload_root() / job_id
        for f in files:
            if not f.filename:
                continue
            name = Path(f.filename).name
            if Path(name).suffix.lower() not in ALLOWED_SUFFIXES:
                raise HTTPException(400, f"unsupported file type: {name}")
            up_dir.mkdir(parents=True, exist_ok=True)
            dest = up_dir / f"{len(doc_paths) + 1:02d}_{name}"
            with dest.open("wb") as out:
                shutil.copyfileobj(f.file, out)
            doc_paths.append(str(dest))
        if not doc_paths:
            raise HTTPException(400, "no documents selected")
        settings = _settings()
        if max_steps.strip():
            from dataclasses import replace
            settings = replace(settings, max_steps=int(max_steps))
        kwargs = {
            "service": service, "requester_email": requester_email, "documents": doc_paths, "settings": settings,
            "today": date.fromisoformat(today) if today.strip() else None,
        }
        JOBS[job_id] = {"job_id": job_id, "status": "running", "started_at": time.time(), "steps": [],
                        "request": {"service": service, "requester_email": requester_email,
                                    "documents": [Path(p).relative_to(ROOT).as_posix() for p in doc_paths],
                                    "today": today or date.today().isoformat(), "model": settings.llm_model,
                                    "max_steps": settings.max_steps}}
    except Exception:
        RUN_LOCK.release()
        raise
    threading.Thread(target=_job_worker, args=(job_id, kwargs), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, since: int = 0) -> dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    out = {k: v for k, v in job.items() if k != "steps"}
    out["steps"] = job["steps"][since:]
    out["elapsed_s"] = (job.get("finished_at") or time.time()) - job["started_at"]
    return out


@app.get("/api/runs")
def runs() -> list[dict[str, Any]]:
    out = []
    for t in reversed(read_traces(_settings().traces_path)):
        out.append({
            "run_id": t["run_id"], "timestamp": t["timestamp"], "service": t["service"], "model": t["model"],
            "requester_email": t["requester_email"], "decision": t["decision"], "status": t["status"],
            "side_effects": t["side_effects"].get("counts", {}), "latency_s": t["metrics"].get("total_latency_ms", 0) / 1000,
            "documents": [Path(d["path"]).name for d in t["documents"]],
        })
    return out


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str) -> dict[str, Any]:
    for t in reversed(read_traces(_settings().traces_path)):
        if t["run_id"].startswith(run_id):
            return t
    raise HTTPException(404, "no such run")


@app.get("/api/preview")
def preview(path: str) -> Response:
    """First page of a document as PNG (for thumbnails)."""
    p = _safe_path(path, list(DOC_DIRS.values()) + [_upload_root()])
    doc = load_document(p, dpi=60, max_image_side=700, max_pages=1)
    return Response(doc.pages[0].image_png, media_type="image/png")


@app.get("/api/artifact")
def artifact(path: str) -> FileResponse:
    """A generated graduation notice PDF."""
    p = _safe_path(path, [_settings().artifacts_dir])
    return FileResponse(p, media_type="application/pdf", filename=p.name)


@app.post("/api/reset_state")
def reset_state() -> dict[str, str]:
    if RUN_LOCK.locked():
        raise HTTPException(409, "a run is in progress")
    s = _settings()
    s.state_db_path.unlink(missing_ok=True)
    shutil.rmtree(s.artifacts_dir / "notices", ignore_errors=True)
    return {"status": "reset"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")), log_level="warning")
