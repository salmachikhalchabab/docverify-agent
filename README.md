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
| `agent/tools.py` | The 6 tool schemas + implementations (thin wrappers over the datastore, no validation), including `read_qr` |
| `agent/prompts.py` | System prompts by key; `"baseline"` is the spec text plus a Policies section rendered from `config.POLICIES` |
| `agent/variants.py` | Variant = prompt + tools + `Guardrails` hooks (all no-ops in the baseline) |
| `agent/loop.py` | Hand-written Perceive → Think → Act → Observe loop |
| `agent/trace.py` | `Step` / `RunTrace`, metrics, append to `runs/traces.jsonl` |
| `agent/run.py` | `run_agent(...)` |
| `try_run.py` | Command-line single run |

## Policies: the same rules in the prompt now, in code later

The baseline prompt is the spec's text plus a **Policies** section. The agent must know the same
policies the guarded variant will enforce; only the enforcement differs (prompt vs code).

- The policies live in **one place**: `POLICIES` in `config.py` (with `NON_EMPLOYMENT_VALIDITY_DAYS`
  and `REQUIRED_DOCUMENTS`). `agent/prompts.py` renders them into the prompt; the guarded variant
  will import and enforce the same values. Change a rule there and nowhere else.
- Every run tells the model a fixed **today** from `config.TODAY` (2026-10-15, the P1 dataset date),
  so date checks are reproducible. A run may pass its own date (`run_agent(today=...)`, as the
  evaluation cases do), and `AGENT_TODAY` in the environment overrides the default.
- Every run records the prompt's `sha256`; evaluation results carry it and the report never pools
  results of different prompts.
- The non-employment window is **30 days**, the same as the P1 generator's answer key
  (`dataset.yaml`), so the policy and P1's labels agree.

## QR codes: a deliberate change from the original spec

The original spec gave the baseline **no** QR tool ("the model reads the QR from the image if it
wants to"). This was changed on purpose: the P1 documents carry signed QR codes, a competent first
version would let the agent read them, and leaving the tool out would make the baseline
artificially weak (the spec's own warning in section 2). So the baseline has:

- **`read_qr(document_number, page=1)`:** decodes the QR code(s) on an uploaded document with
  OpenCV and returns the encoded text exactly as is. It never opens, follows or judges the URL.
- **`qr_tokens` registry** (P1 data): the record behind a QR token (the last part of the URL after
  `/verify/`): serial, kind, issue date, revoked, how often the QR was used, holder. A tampered or
  invented token simply isn't found. No HMAC secret or P1 server is needed.

Whether to read the QR, whether to look the token up, and whether to notice a foreign domain, a
revoked document or another person's QR are all **the model's decisions**. The guarded variant
uses the same tools but enforces those checks in code.

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
keys or QR secret).

- Selected with `REFERENCE_BACKEND=p1_sqlite` and `REFERENCE_DB_PATH=data/p1/registry_agent.sqlite`.
- The working copy `registry_agent.sqlite` is built automatically from the snapshot on first use
  (`agent/reference_p1.py`). The snapshot itself is never modified.
- The working copy adds one table P1 doesn't have: `registered_emails`, a deterministic,
  fictional address per person (`first.last.<last 3 digits of the national number>@student.example`).
  The full list is in `data/p1/registered_emails.csv` (regenerate with `python data/p1/export_emails.py`).
- Registries offered to the model: `civil_registry`, `registered_emails`, `students`,
  `employment_registry`, `graduates`, `graduation_documents` (by serial) and `masters_accepted`.
  `get_transcript` takes the student number from `students`.
- The P1 data is built for **2026-10-15** (ID expiry, document age), which is `config.TODAY`.
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
extra tools if needed. Run with `variant="guarded"`. Both variants
write the same trace schema. Guardrail interventions go into `guard_events`, which is
always empty for the baseline.

## Guarded variant

The "after guardrails" agent: the same model, tools, loop, data and trace schema as the
baseline, plus deterministic rules in `agent/guards.py` that run in the three hook points of
`agent/variants.py`. A rule compares values and refuses, removes or overrides; it never asks a
model anything. Full specification: `docs/prompt_guarded_v1.md`.

| Rule | When | What it does |
|---|---|---|
| G3 hidden text | before the model | A PDF text-layer line that is not visible in the rendered page (OCR) is removed and logged; the remaining text is wrapped in `<<<DOCUMENT_TEXT>>>` markers. Without OCR the text layer is dropped entirely (unverifiable). |
| G4 identity | before the model | The national number on the documents is looked up; the requester's email must equal the registered one, or the request is rejected before the model reads anything. |
| policy | before the model | Documents are classified by registry cross-reference (ID, non-employment certificate, graduation document) and the verdict for the service is computed: approved / rejected (with reasons) / needs_review (hidden text found). |
| G2 allow-list | each tool call | Each service may use only its own tools. |
| gate | each tool call | `register_masters`, `issue_graduation_notice`, `send_email` run only if the verdict is approved. |
| G5 binding | each tool call | Arguments must match the registry record of the identified person (name, national number, major, date, student id). Lookups are limited to that person. |
| G1 email | each tool call | One recipient, the registered address only; attachments only from this run; no other addresses in the body. |
| G7 budget | each tool call | At most one email, one registration and one notice per run. |
| G6 decision | after the loop | The final JSON is validated; the run's decision is the policy verdict, the model's decision is recorded. |

Variants (`--variant`): `baseline` (unchanged), `prompt_only` (stricter prompt, no code rules),
`tools_only` (tool-call rules only, no document rules), `guarded` (everything).

```bash
python evaluation/run_eval.py --cases all --repeats 3 --variant guarded
python evaluation/report.py           # baseline / prompt_only / tools_only / guarded side by side
```

What is recorded per run: `guard_events` (one entry per refusal, removal, flag or override),
`documents_as_sent` (the text the model actually received), `guard_summary` (policy verdict,
model decision, hook latency) and the metrics `guard_blocks`, `guard_removals`, `guard_flags`,
`hidden_text_chars_removed`, `decision_overridden`, `guard_latency_ms` and the computed
`short_circuit_savings_*` (what stopping at a pre-model rejection would have saved; the model is
always run so cost and latency stay comparable).

Known limits:

- **OCR is required.** Identity binding reads the national number from the extracted text, and
  hidden-text detection compares the text layer with OCR of the picture. With `OCR_ENGINE=none`
  image documents have no text and PDF text layers cannot be verified, so the guarded variant
  ends every request as `needs_review` with no action. Run the evaluation with Tesseract.
- Faint text drawn inside the picture (case A2) survives G3 because it is part of the image; the
  leak is stopped by G1 instead.
- Document classification needs a registry number and a keyword ("بطاقة", "تخرج", ...) on the
  page; a document OCR'd badly may be classified as missing (a false rejection, visible as
  `fblock` / task failure in the report, never as an unsafe action).
- The mock registry has two fictional people only (`data/build_mock_registry.py`).

## Guardrails v2, staff approval, login and privacy (2026-11-07)

- Every guardrail, what it protects against, and what was considered but not built:
  `docs/guardrails_v2.md`. What changed and why, in plain words: `docs/what_changed_2026-11-07.md`.
- See what the rules decide for every case without a model: `python evaluation/guard_check.py`.
- Staff approval: `HUMAN_APPROVAL=off|actions|flagged` (`agent/approvals.py`, web UI tab "موافقات الموظف").
- Web UI login: requesters use national number + the e-mail registered for it; staff use
  `STAFF_KEY` (or the key printed at start). The server listens on 127.0.0.1 unless `HOST` is set.
- Traces keep no document text and pseudonymise personal data (`TRACE_PII=redact`, `agent/privacy.py`).
- Evaluation: `--workers N` runs cases in parallel; `--human-approval` (default `off`); every run has
  its own duplicate-request scope. Markdown comparison: `python evaluation/report.py --since <batch> --markdown out.md`.
- Version keys: `baseline`, `prompt_only`, `guarded`, and with staff approval of every action
  `prompt_only_human`, `guarded_human` (table in `docs/guardrails_v2.md`). The key decides approval, not `.env`.
