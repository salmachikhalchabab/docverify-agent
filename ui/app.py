"""Small local web UI for the agent: start a run, watch it step by step, browse past runs.

    python ui/app.py                 # serves on http://127.0.0.1:8000 (this computer only)
    PORT=8080 python ui/app.py
    HOST=0.0.0.0 python ui/app.py    # other computers too: only on a trusted network

It calls the same run_agent() as the CLI and the evaluation; it adds no logic
of its own to the agent. One run at a time (free-tier rate limits).

Who may do what (UI_AUTH=on, the default):
  requester  logs in with national number + the e-mail registered for it (both must match the
             registry). Their runs always use that address (the form cannot change it), and they
             see only their own runs. Note: knowing someone's number and e-mail is enough to log
             in as them; a real system would add a second factor (e.g. a code sent by e-mail).
  staff      logs in with the staff key (STAFF_KEY in .env; if unset, one is created once in
             runs/.staff_key and printed at start). May replay evaluation cases with any requester
             address, sees every run, and approves or rejects actions waiting for a human (G16).
UI_AUTH=off restores the old open behaviour (research use on your own machine only).
"""

from __future__ import annotations

import hmac
import os
import secrets
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
from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response  # noqa: E402

from agent.approvals import pending_runs, review_run  # noqa: E402
from agent.datastore import SQLiteStateStore, get_reference_store  # noqa: E402
from agent.documents import IMAGE_SUFFIXES, PDF_SUFFIXES, load_document  # noqa: E402
from agent.privacy import pseudonym, secret_key  # noqa: E402
from agent.run import run_agent  # noqa: E402
from agent.trace import read_traces  # noqa: E402
from config import get_settings  # noqa: E402
from evaluation.run_eval import CASES_PATH  # noqa: E402
from evaluation.scoring import cases_backend, load_cases  # noqa: E402

app = FastAPI(title="docverify UI")
STATIC = Path(__file__).resolve().parent / "static"
DOC_DIRS = {"p1 documents": ROOT / "data" / "p1" / "docs", "test documents": ROOT / "data" / "documents",
            "evaluation documents": ROOT / "data" / "eval_documents"}
ALLOWED_SUFFIXES = PDF_SUFFIXES | IMAGE_SUFFIXES
MAX_UPLOADS = 5
MAX_UPLOAD_BYTES = 15 * 1024 * 1024

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


# ---------------------------------------------------------------------------
# Authentication (demo): national number + registered e-mail for requesters, a key for staff
# ---------------------------------------------------------------------------

AUTH_ON = os.getenv("UI_AUTH", "on").strip().lower() != "off"
SESSION_MINUTES = 30
FAIL_MINUTES = 10
MAX_FAILS = 5  # wrong logins per national number per FAIL_MINUTES, then blocked for a while
# Separate logins for the two pages, so a student and a staff member can be logged in at the
# same time in one browser. The page says which one it is in the X-Portal header.
STUDENT_COOKIE, STAFF_COOKIE = "dv_session", "dv_staff"
SESSIONS: dict[str, dict[str, Any]] = {}  # token -> {role, email, national_number, expires}
FAILS: dict[str, list[float]] = {}  # national number -> times of wrong logins
AUTH_LOCK = threading.Lock()


def _staff_key() -> str:
    key = os.getenv("STAFF_KEY", "").strip()
    if key:
        return key
    path = _settings().runs_dir / ".staff_key"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_urlsafe(18), encoding="utf-8")
    return path.read_text(encoding="utf-8").strip()


def _new_session(role: str, email: str = "", national_number: str = "") -> str:
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {"role": role, "email": email, "national_number": national_number,
                       "expires": time.time() + SESSION_MINUTES * 60}
    return token


def _cookie_names(request: Request) -> list[str]:
    portal = request.headers.get("x-portal", "")
    if portal == "staff":
        return [STAFF_COOKIE]
    if portal == "student":
        return [STUDENT_COOKIE]
    return [STUDENT_COOKIE, STAFF_COOKIE]  # images and file links carry no header: either login


def _token(request: Request) -> str:
    for name in _cookie_names(request):
        tok = request.cookies.get(name, "")
        if tok in SESSIONS:
            return tok
    return ""


def _user(request: Request) -> dict[str, Any] | None:
    if not AUTH_ON:
        return {"role": "staff", "email": "", "national_number": ""}
    s = SESSIONS.get(_token(request))
    if s is None or s["expires"] < time.time():
        return None
    s["expires"] = time.time() + SESSION_MINUTES * 60  # sliding expiry
    return s


def _require(request: Request, role: str | None = None) -> dict[str, Any]:
    u = _user(request)
    if u is None:
        raise HTTPException(401, "login required")
    if role and u["role"] != role:
        raise HTTPException(403, "not allowed for this account")
    return u


def _with_cookie(payload: dict[str, Any], token: str, name: str) -> JSONResponse:
    r = JSONResponse(payload)
    r.set_cookie(name, token, httponly=True, samesite="strict", max_age=SESSION_MINUTES * 60)
    return r


def _own(trace_email: str, user: dict[str, Any]) -> bool:
    """Does this (possibly pseudonymised) requester address belong to the logged-in requester?"""
    mine = user.get("email", "")
    if "@" not in mine:
        return False
    local, domain = mine.split("@", 1)
    own = {mine, f"user-{pseudonym(local, secret_key(_settings().runs_dir))}@{domain}"}
    return str(trace_email or "").strip().lower() in own


@app.get("/api/auth/me")
def auth_me(request: Request) -> dict[str, Any]:
    u = _user(request)
    return {"auth": AUTH_ON, "role": u["role"] if u else None, "email": (u or {}).get("email", "")}


@app.post("/api/auth/login")
def auth_login(national_number: str = Body(...), email: str = Body(...)) -> JSONResponse:
    """Requester login: the e-mail must be the one registered for this national number.
    One answer for every failure, so the page does not reveal who is registered."""
    nn, addr = national_number.strip(), email.strip().lower()
    with AUTH_LOCK:
        now = time.time()
        recent = [x for x in FAILS.get(nn, []) if x > now - FAIL_MINUTES * 60]
        if len(recent) >= MAX_FAILS:
            raise HTTPException(429, "too many wrong attempts; try again later")
        s = _settings()
        ref = get_reference_store(s.reference_backend, s.reference_db_path)
        rec = ref.lookup("registered_emails", nn) if nn.isdigit() else None
        if not rec or str(rec.get("email", "")).strip().lower() != addr:
            FAILS[nn] = recent + [now]
            raise HTTPException(401, "national number and e-mail do not match our records")
        FAILS.pop(nn, None)
        token = _new_session("requester", addr, nn)
    return _with_cookie({"role": "requester", "email": addr}, token, STUDENT_COOKIE)


@app.post("/api/auth/staff")
def auth_staff(key: str = Body(..., embed=True)) -> JSONResponse:
    time.sleep(0.5)  # slows down guessing
    if not hmac.compare_digest(key.strip().encode(), _staff_key().encode()):
        raise HTTPException(401, "wrong staff key")
    return _with_cookie({"role": "staff"}, _new_session("staff"), STAFF_COOKIE)


@app.post("/api/auth/logout")
def auth_logout(request: Request) -> JSONResponse:
    SESSIONS.pop(_token(request), None)
    r = JSONResponse({"status": "logged out"})
    for name in _cookie_names(request):
        r.delete_cookie(name)
    return r


# ---------------------------------------------------------------------------
# Pages and runs
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """Students' page: only the student login."""
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/staff", response_class=HTMLResponse)
def staff_page() -> str:
    """Staff page: same app, only the staff login. Access is still checked per request (the role)."""
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/staff/", include_in_schema=False)
def staff_page_slash() -> RedirectResponse:
    return RedirectResponse("/staff")  # keeps the page's relative "api/..." addresses working


@app.get("/api/config")
def config(request: Request) -> dict[str, Any]:
    user = _require(request)
    s = _settings()
    docs = []
    for group, d in DOC_DIRS.items():
        for f in sorted(d.glob("*")) if d.exists() else []:
            if f.suffix.lower() in ALLOWED_SUFFIXES:
                docs.append({"group": group, "name": f.name, "path": f.relative_to(ROOT).as_posix()})
    cases = []
    if user["role"] == "staff":  # evaluation presets use other people's addresses: staff only
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
        "cases_backend": cases_backend(CASES_PATH),
        "today": s.agent_today or date.today().isoformat(),
        "human_approval": s.human_approval, "role": user["role"], "email": user.get("email", ""), "auth": AUTH_ON,
    }


def _job_worker(job_id: str, kwargs: dict[str, Any]) -> None:
    job = JOBS[job_id]
    try:
        r = run_agent(**kwargs, on_step=lambda st: job["steps"].append(asdict(st)))
        job["run_id"] = r.run_id
        # what the browser gets is what the trace file keeps (personal data redacted)
        saved = [t for t in read_traces(kwargs["settings"].traces_path) if t["run_id"] == r.run_id]
        job["trace"] = saved[-1] if saved else r.trace.to_dict()
        job["trace"]["staff_case"] = _case_view(SQLiteStateStore(kwargs["settings"].state_db_path).staff_case(r.run_id))
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
    request: Request,
    service: str = Form(...),
    requester_email: str = Form(""),
    today: str = Form(""),
    max_steps: str = Form(""),
    existing: list[str] = Form(default=[]),
    files: list[UploadFile] = File(default=[]),
) -> dict[str, Any]:
    user = _require(request)
    if user["role"] == "requester":
        requester_email = user["email"]  # the verified address, never what the form says
        today = ""  # only staff may move the agent's date (evaluation replays)
    if not requester_email.strip():
        raise HTTPException(400, "requester e-mail missing")
    if len([f for f in files if f.filename]) > MAX_UPLOADS:
        raise HTTPException(400, f"at most {MAX_UPLOADS} files")
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
            if dest.stat().st_size > MAX_UPLOAD_BYTES:
                raise HTTPException(400, f"file too large: {name}")
            doc_paths.append(str(dest))
        if not doc_paths:
            raise HTTPException(400, "no documents selected")
        settings = _settings()
        if max_steps.strip():
            from dataclasses import replace
            settings = replace(settings, max_steps=max(1, min(50, int(max_steps))))
        kwargs = {
            "service": service, "requester_email": requester_email, "documents": doc_paths, "settings": settings,
            "today": date.fromisoformat(today) if today.strip() else None,
        }
        JOBS[job_id] = {"job_id": job_id, "owner": _token(request), "status": "running",
                        "started_at": time.time(), "steps": [],
                        "request": {"service": service, "requester_email": requester_email,
                                    "documents": [Path(p).name for p in doc_paths],
                                    "today": today or settings.agent_today, "model": settings.llm_model,
                                    "max_steps": settings.max_steps}}
    except Exception:
        RUN_LOCK.release()
        raise
    threading.Thread(target=_job_worker, args=(job_id, kwargs), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(request: Request, job_id: str, since: int = 0) -> dict[str, Any]:
    user = _require(request)
    job = JOBS.get(job_id)
    if not job or (AUTH_ON and user["role"] != "staff" and job.get("owner") != _token(request)):
        raise HTTPException(404, "no such job")
    out = {k: v for k, v in job.items() if k not in ("steps", "owner")}
    out["steps"] = job["steps"][since:]
    out["elapsed_s"] = (job.get("finished_at") or time.time()) - job["started_at"]
    return out


def _case_view(case: dict[str, Any] | None) -> dict[str, Any] | None:
    """What a page may show about a staff case: status, final decision, when (not the reviewer's name for students)."""
    if not case:
        return None
    return {"status": case["status"], "decision": case.get("decision"), "reviewed_at": case.get("reviewed_at"),
            "recommendation": case.get("recommendation"), "reasons": case.get("reasons", []), "note": case.get("note") or ""}


@app.get("/api/runs")
def runs(request: Request) -> list[dict[str, Any]]:
    user = _require(request)
    cases = {c["run_id"]: c for c in SQLiteStateStore(_settings().state_db_path).staff_cases()}
    out = []
    for t in reversed(read_traces(_settings().traces_path)):
        if user["role"] != "staff" and not _own(t.get("requester_email", ""), user):
            continue
        out.append({
            "run_id": t["run_id"], "timestamp": t["timestamp"], "service": t["service"], "model": t["model"],
            "requester_email": t["requester_email"], "decision": t["decision"], "status": t["status"],
            "variant": t.get("variant"), "staff_case": _case_view(cases.get(t["run_id"])),
            "side_effects": t["side_effects"].get("counts", {}), "latency_s": t["metrics"].get("total_latency_ms", 0) / 1000,
            "documents": [Path(d["path"]).name for d in t["documents"]],
        })
    return out


@app.get("/api/runs/{run_id}")
def run_detail(request: Request, run_id: str) -> dict[str, Any]:
    user = _require(request)
    for t in reversed(read_traces(_settings().traces_path)):
        if t["run_id"].startswith(run_id) and (user["role"] == "staff" or _own(t.get("requester_email", ""), user)):
            return {**t, "staff_case": _case_view(SQLiteStateStore(_settings().state_db_path).staff_case(t["run_id"]))}
    raise HTTPException(404, "no such run")


@app.get("/api/preview")
def preview(request: Request, path: str) -> Response:
    """First page of a document as PNG (for thumbnails)."""
    _require(request)
    p = _safe_path(path, list(DOC_DIRS.values()) + [_upload_root()])
    doc = load_document(p, dpi=60, max_image_side=700, max_pages=1)
    return Response(doc.pages[0].image_png, media_type="image/png")


@app.get("/api/artifact")
def artifact(request: Request, path: str) -> FileResponse:
    """A generated graduation notice PDF."""
    _require(request, "staff")
    p = _safe_path(path, [_settings().artifacts_dir])
    return FileResponse(p, media_type="application/pdf", filename=p.name)


@app.post("/api/reset_state")
def reset_state(request: Request) -> dict[str, str]:
    _require(request, "staff")
    if RUN_LOCK.locked():
        raise HTTPException(409, "a run is in progress")
    s = _settings()
    s.state_db_path.unlink(missing_ok=True)
    shutil.rmtree(s.artifacts_dir / "notices", ignore_errors=True)
    return {"status": "reset"}


# ---------------------------------------------------------------------------
# Staff: actions waiting for a human (G16)
# ---------------------------------------------------------------------------


def _stores():
    s = _settings()
    return s, get_reference_store(s.reference_backend, s.reference_db_path), SQLiteStateStore(s.state_db_path)


@app.get("/api/staff/pending")
def staff_pending(request: Request) -> list[dict[str, Any]]:
    _require(request, "staff")
    s, _, state = _stores()
    traces = {t["run_id"]: t for t in read_traces(s.traces_path)}
    out = []
    for run_id, actions in pending_runs(state).items():
        t = traces.get(run_id, {})
        gs = t.get("guard_summary") or {}
        case = state.staff_case(run_id) or {}
        out.append({
            "run_id": run_id, "timestamp": t.get("timestamp"), "service": t.get("service"),
            "requester_email": t.get("requester_email"), "documents": [Path(d["path"]).name for d in t.get("documents", [])],
            # the rules' recommendation (null in prompt_only_human: no rules) and the AI's own answer
            "policy_verdict": case.get("recommendation", gs.get("policy_verdict")),
            "reasons": gs.get("reasons", []), "flags": gs.get("flags", []),
            "model_decision": case.get("agent_decision", gs.get("model_decision")),
            # the arguments are what will be performed: shown in full to the staff member who decides
            "actions": [{"id": a["id"], "tool": a["tool"], "arguments": a["arguments"], "why": a["reason"]} for a in actions],
        })
    return out


@app.post("/api/staff/review")
def staff_review(request: Request, run_id: str = Body(...), approve: bool = Body(...),
                 reviewer: str = Body(...), note: str = Body("")) -> dict[str, Any]:
    _require(request, "staff")
    s, ref, state = _stores()
    try:
        return {"results": review_run(run_id, approve, reviewer, settings=s, reference=ref, state=state, note=note)}
    except (LookupError, ValueError) as e:
        raise HTTPException(400, str(e)) from e


if __name__ == "__main__":
    if AUTH_ON and not os.getenv("STAFF_KEY", "").strip():
        print(f"staff key (stored in runs/.staff_key): {_staff_key()}", flush=True)
    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")), log_level="warning")
