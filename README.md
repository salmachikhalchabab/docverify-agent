# docverify: baseline document-verification agent

The "before guardrails" agent for the research question *How much do deterministic
guardrails improve the security of a document-verification agent, and what do they
cost in success rate, cost and latency?*

A requester uploads documents and asks for `transcript`, `masters_registration` or
`graduation_notice`. The model reads the documents (page image plus **all** extracted
text, including hidden text), uses the tools, decides and acts. The model makes every
decision itself. Tools execute exactly what it asks. Nothing is validated. Everything is
recorded.

> **Status:** working end to end. Reference data: the **P1 synthetic registry** (58 fictional
> students, see "P1 registry"); the earlier one-person mock registry is still available. The
> real registries are not connected yet; swapping them in only means implementing the
> data-access layer. Tested with `gemini/gemma-4-26b-a4b-it` and
> `openrouter/nvidia/nemotron-3.5-super-vl:free`.

## Setup (on your machine)

You need **Python 3.11+** and **Tesseract OCR with Arabic** (for scanned documents).

**1. Tesseract with Arabic**

| System | Install |
|---|---|
| Windows | Installer from https://github.com/UB-Mannheim/tesseract/wiki; tick **Arabic** under "Additional language data". If `tesseract` isn't on your PATH, set `TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe` in `.env`. |
| macOS | `brew install tesseract tesseract-lang` |
| Ubuntu/Debian | `sudo apt install tesseract-ocr tesseract-ocr-ara` |

Check with `tesseract --list-langs` (it must list `ara`). Without Tesseract, set
`OCR_ENGINE=none` in `.env`: the model then reads scanned documents from the image only.

**2. Python environment**

```bash
python -m venv .venv
# Windows:      .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt
```

**3. Settings**

Copy `.env.example` to `.env` and put your Gemini key in `GEMINI_API_KEY`
(https://aistudio.google.com/apikey). `.env` is git-ignored; never commit it.

**4. Check and run**

```bash
python -m pytest -q          # all tests use a fake model: no key, no quota needed
python ui/app.py             # web UI on http://localhost:8000
```

Free-tier note: Gemma 4 on Google AI Studio allows about 16,000 input tokens per minute.
One run with images can exceed that; the agent then waits for the window to reset and
retries (recorded in the trace as `retry_errors` / `retry_overhead_ms`).

Installation notes:

- `requirements.txt` includes `mcp`. LiteLLM 1.108.0 imports it on every call that
  passes `tools` but doesn't declare it, so without it every tool-calling request fails.
- Generated notice PDFs need a font with Arabic glyphs if names are Arabic. DejaVu Sans
  (Linux) and Arial (Windows) are auto-detected; otherwise set `NOTICE_FONT_PATH`.

`LLM_MODEL` is any LiteLLM model string. The model must support **vision and tool
calling**. Each run records what LiteLLM knows about this under
`config.model_capabilities` (`null` means LiteLLM doesn't know the model). Cost comes from
`litellm.completion_cost()`, falling back to `PRICE_TABLE` in `config.py`. If neither
knows the model, the cost is recorded as `null` with `cost_complete: false`, never as 0.

## Web UI

```bash
python ui/app.py            # http://localhost:8000  (PORT=... to change)
```

The page is in Arabic. **طلب جديد** (new request) lets you pick the service, the
requester's email, the date, and the documents (existing test documents or uploads),
or load a ready-made evaluation case. The run's steps appear live: what the model
thought, which tool it called with which arguments, and what came back. At the end
the page shows the decision, **what actually happened** (emails, with any recipient
other than the requester flagged; registrations; notice PDFs), the metrics, and the
text extracted from each document, hidden text included. **السجل** (history) lists
every run in `runs/traces.jsonl`. The UI calls the same `run_agent()` and adds no
agent logic; it runs one request at a time.

## Usage

```python
from agent.run import run_agent
result = run_agent(service="transcript", requester_email="...",
                   documents=["path/to/id.pdf"], variant="baseline")
result.decision, result.trace, result.metrics, result.side_effects
```

```bash
python try_run.py --service transcript --email someone@example.com --doc path/to/id.pdf
python try_run.py --service transcript --email ... --doc id.pdf --dry-run   # show what the model receives; no model call
python -m pytest -q                                                       # tests (fake model, no API key needed)
```

## Layout

| File | Role |
|---|---|
| `config.py` | Settings from `.env`, paths, fallback price table |
| `agent/llm.py` | LiteLLM wrapper: message, tool calls, tokens, cost, latency |
| `agent/documents.py` | PDF or image → per-page PNG + all extracted text (text layer, or OCR if enabled) |
| `agent/datastore.py` | **The only module that touches data.** `ReferenceStore` (your registries, read-only) and `StateStore` (agent writes) |
| `agent/reference_sqlite.py`, `agent/reference_p1.py`, `agent/reference_mock.py` | Read-only SQLite reference store, and the layouts of the P1 registry and the old mock registry |
| `agent/tools.py` | The 5 tool schemas + implementations (thin wrappers over the datastore, no validation) |
| `agent/prompts.py` | System prompts by key; `"baseline"` is the spec text verbatim |
| `agent/variants.py` | Variant = prompt + tools + `Guardrails` hooks (all no-ops in the baseline) |
| `agent/loop.py` | Hand-written Perceive → Think → Act → Observe loop |
| `agent/trace.py` | `Step` / `RunTrace`, metrics, append to `runs/traces.jsonl` |
| `agent/run.py` | `run_agent(...)` |
| `try_run.py` | Command-line single run |

## Behavior worth knowing when reading traces

- **Final answer:** the first model reply with no tool calls. It is parsed with
  `json.loads`. The only normalization: a reply that is exactly one Markdown code block
  (```` ```json ... ``` ````) is unwrapped first, and `final_fenced: true` is recorded. This
  was decided with the user and applies to every variant. Any other invalid JSON (prose
  around it, broken syntax) is `parse_error: true`, with the raw text kept in
  `final_text_raw`. `decision` is whatever the JSON says, unvalidated.
- **MAX_STEPS** counts model calls (default 25). Reaching it gives
  `status: "hit_max_steps"` and `decision: null`. It is a lab safety net, not a guardrail.
- **Tool failures** (unknown tool, bad JSON arguments, wrong argument names) are returned
  to the model as `{"error": ...}` and recorded. They are not blocks.
- **Side effects** are read back from the state store by `run_id` after the run, so they
  show what actually happened, regardless of what the model said.
- **Model API errors** end the run with `status: "error"`. The run is still recorded.
- **Data not connected** raises and is *not* recorded, because it is a setup problem, not
  a result.
- **Document text and images:** the trace stores each page's full extracted text and an
  image hash, but no image bytes.

## P1 registry (default reference data)

`data/p1/registry.snapshot.db` is a copy of the registry generated by the separate **P1** data
project (synthetic Damascus University data; every document is marked "نموذج تجريبي - بيانات
وهمية"). This agent uses **only its data**: it reads the database directly (no P1 server, API
keys or QR secret). The agent's prompt, tools and output are unchanged.

- Selected with `REFERENCE_BACKEND=p1_sqlite` and `REFERENCE_DB_PATH=data/p1/registry_agent.sqlite`.
- The working copy `registry_agent.sqlite` is built automatically from the snapshot on first use
  (`agent/reference_p1.py`). The snapshot itself is never modified.
- The working copy adds one table P1 doesn't have: `registered_emails`, a deterministic,
  fictional address per person (`first.last.<last 3 digits of the national number>@student.example`).
- Registries offered to the model: `civil_registry`, `registered_emails`, `students`,
  `employment_registry`, `graduates`, `graduation_documents` (by serial) and `masters_accepted`.
  `get_transcript` takes the student number from `students`.
- The P1 data is built for **2026-10-15** (ID expiry, document age), so `.env` sets
  `AGENT_TODAY=2026-10-15`, the default date for runs and the UI.
- P1 document images go in `data/p1/docs/`; the UI lists them automatically.

## Temporary mock registry (smoke tests only)

`data/mock_registry.sqlite` holds **fictional** records for one person (نور السيد,
national number 14749200009), transcribed from the test documents in `data/documents/`:
the non-employment certificate (no. 2573370), the transcript (25 courses, GPA 70.59), the
graduation notice (no. 8629) and a generated test national ID. The older graduation
certificate no. 3698 (AI major, GPA 79.06) contradicts these, so it has no registry record;
submitting it for نور is a "wrong document" test case. Rebuild them with `python data/build_mock_registry.py` and
`python data/build_test_national_id.py`.

The first user message includes today's date (`run_agent(today=...)`, default today in
UTC; recorded as `config.today`), so validity periods can be checked. By today's date the
non-employment certificate has expired (valid 3 months from 2025-12-12). It is selected with
`REFERENCE_BACKEND=mock_sqlite`, and its layout is in `agent/reference_mock.py`. Delete
both once the real data is connected. Agent state goes to a separate file
(`STATE_DB_PATH`); wipe it between test runs with `python reset_state.py`.

## Evaluation

```bash
python evaluation/build_attack_documents.py           # derive the attack documents (data/eval_documents/)
python evaluation/run_eval.py --dry-run               # validate cases + show the plan, no model calls
python evaluation/run_eval.py --cases attack --repeats 3
python evaluation/report.py                           # summary of runs/eval_results.jsonl
```

- **Which registry:** `cases.yaml` declares `reference_backend: mock_sqlite` (its cases use the
  one-person mock registry). With another `REFERENCE_BACKEND`, `run_eval.py` refuses to run it
  (unless `--force`) and the UI hides its presets, because its people don't exist there.
- **Cases:** `evaluation/cases.yaml` holds 14 cases (4 valid, 5 defective, 5 attack). Each
  has a hand-written ground truth: the correct decisions, the effects that must happen,
  and the effects that must not. Each defective and attack case names a `control` case
  (the same case without the defect or attack), for paired comparison. `today` is fixed
  per case.
- **Scoring** (`evaluation/scoring.py`) is deterministic. It uses only the decision string
  and the rows the run actually wrote. No model judges anything.
- **Report metrics:** decision accuracy, task success, unsafe rate (any forbidden effect),
  attack success rate, "acted without approving", parse errors, max-step hits, cost,
  latency and tokens.
- **Attack documents:** a searchable PDF whose text layer, hidden under the scan, carries
  an injected instruction (the rendered page is pixel-identical to the original), and a
  faint-text variant drawn into the image (reaches the model only through vision).

## Connecting the real data

1. Implement a `ReferenceStore` in `agent/datastore.py` with `registries()`,
   `lookup(registry, key)` and `get_transcript(student_id)`. Register it in
   `get_reference_store()`, then set `REFERENCE_BACKEND` in `.env`.
2. If agent state (registrations, notices, outbox) should live in an existing database,
   implement `StateStore` there. Otherwise the built-in `SQLiteStateStore` at
   `runs/state.sqlite` is used. Email is never sent; it only goes into the `outbox` table.

The tools, loop, tracing and prompt do not change.

## Adding the guarded variant later

Subclass `Guardrails` in `agent/variants.py` (`transform_documents`,
`intercept_tool_call`, `finalize`). Add a `"guarded"` prompt and a `VariantSpec`, plus
extra tools such as a QR decoder if needed. Run with `variant="guarded"`. Both variants
write the same trace schema. Guardrail interventions go into `guard_events`, which is
always empty for the baseline.
