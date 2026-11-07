# Guardrails v2: the full catalogue

Status: implemented in `agent/guards.py` (2026-11-07). Tests: `tests/test_guards.py` and
`tests/test_guards_v2.py`. Check what the rules decide for every case, without a model:

```
python evaluation/guard_check.py
```

A guardrail here is **plain code** that runs before the model, before each tool call, or after
the model. It never asks a model anything and never reads instructions from documents.

## How a request flows

1. The documents are loaded (text layer or OCR).
2. **Before the model**, the code checks the files, cleans the text, finds the person in the
   registry and decides a *policy verdict*: `approved`, `needs_review` or `rejected`.
3. The model works as usual, but **every tool call** passes through the code first.
4. **After the model**, the final decision is the policy verdict, not the model's opinion.

Hard problems (wrong person, expired certificate, forged QR...) give `rejected`.
Soft problems (hidden text, unreadable name, missing university name...) give `needs_review`.

## The rules

| Id | When | What it does | Protects against |
|---|---|---|---|
| G1 | each tool | E-mail only to the registered address of the identified person, one recipient, only this request's attachment, no other addresses or other people's national numbers in the body | Data leaks to an attacker |
| G2 | each tool | Each service may use only its own tools | Tool misuse ("also register a Master's") |
| G3 | before | PDF text that is not visible on the page is removed. Visible = drawn in the open (dark, readable size, not under a picture) or confirmed by OCR (normal and 2x). 4+ hidden words make the run `needs_review` | Hidden prompt injection |
| G4 | before | The person = the national number on the documents, if the registry knows it. The requester's e-mail must be that person's registered e-mail | Requests for someone else |
| G5 | each tool | Arguments must have the right names and types, and must equal the registry values (name, major, date, student number). Lookups only for the identified person or numbers printed on the documents | The model typing wrong or invented data; registry fishing |
| G6 | after | The final JSON is checked; the decision is the policy verdict | The model talked into "approved" |
| G7 | each tool | One e-mail, one registration, one notice per request; at most 12 lookups, 8 QR reads, 2 transcripts; the same call at most twice | Loops, cost explosions, repeated actions |
| gate | each tool | Actions only if the verdict is `approved` (or, with staff approval on, queued for staff) | Acting on a bad request |
| **G8** | before | Files must really be PDF/image (checked by content, not name), at most 15 MB; PDFs with JavaScript, auto-actions, embedded files, forms or encryption are rejected; the same file uploaded twice is a warning | Malicious files |
| **G9** | before | Visible lines that talk to an assistant ("note for the AI", "ignore", ...) or contain an e-mail address are removed from the text and make the run `needs_review` | Visible prompt injection; contact data planted in documents |
| **G10** | before | Every document must belong to the same person: linked by national number, certificate number, graduation document number or QR code; at least two words of the person's name must appear on it (a stronger 2x OCR is tried before giving up); a document carrying **another registered person's** national number rejects the request | Forged IDs (right number, other name), mixed documents |
| **G11** | before | Graduation documents must name Damascus University and no other university | Certificates from another university |
| **G13** | before | Every QR code must point to the official registry address, its token must exist, not be revoked, and belong to the same person | Forged or copied verification codes |
| **G15** | before + each tool | A Master's registration and a graduation notice happen once per person, a transcript once per day. Checked before the model (then `rejected`) and again when the action is about to run (a key is reserved in the state database) | Duplicate requests across runs |
| **G16** | each tool | Staff approval (`HUMAN_APPROVAL`): `actions` = every action waits for a staff member; `flagged` = only warning cases wait, and `needs_review` cases go to staff instead of being refused; `off` = automatic | Fully automatic decisions with no human |
| **Privacy** | when saving | Traces keep no document text; national numbers, names, student numbers, birth data, e-mails and phones become stable pseudonyms (`TRACE_PII=redact`) | Personal data in research logs |
| **Login** | web UI | Requesters log in with national number + the e-mail registered for it; their runs always use the verified address; staff use a staff key; the server listens only on this computer | Anyone typing someone else's e-mail |

Bold = new in v2. G12 and G14 were considered and not built (below).

## Considered, not built (and why)

| Idea | Why not |
|---|---|
| **G12 stamp check in code** | Tested on our documents with colour and circle detection: the ID card's red stripe scored *higher* than the real certificate stamp. A pixel check would give wrong answers. Authenticity is checked through the QR code and the registry numbers instead, and stamps are for the staff member (G16). |
| **G14 ID card expiry** | The mock registry has no expiry date. The P1 data has one; add the rule when the experiment moves to P1. |
| Faint text inside images (case A2) | OCR does not read text that is only a few shades darker than the background, so code cannot remove it. The model can still see it in the image. G1 and the gate still stop the leak; the case measures exactly this limit. |
| A second AI model as a judge | Not deterministic, costs tokens, and can itself be injected. The research question is about deterministic guardrails. |
| Real e-mail sending for the login code | Needs a mail server and real addresses. The demo prints the code in the server terminal and shows it in the staff's demo mailbox. |

## Settings

All in `.env` (see `.env.example`): `HUMAN_APPROVAL`, `TRACE_PII`, `TRACE_PSEUDONYM_KEY`,
`UI_AUTH`, `STAFF_KEY`, `HOST`, `HIDDEN_TEXT_THRESHOLD`. Rules that are business policy live in
`config.py` next to your friend's `POLICIES`: `NON_EMPLOYMENT_VALIDITY_DAYS`, `QR_VERIFY_PREFIX`,
`UNIVERSITY_NAMES`. The prompt states them and the code enforces them.

## Both registries

The rules read the registry through one small adapter (`RegistryFacts`), so they work with the
old one-person mock registry and with the P1 registry. With P1, the non-employment check uses the
employment register and the issue date printed on the certificate; graduation documents and QR
tokens come from P1's certificates table.

## Version keys

Each version has a key. The key alone says how a run was made, and it is saved with every result.

| Key | Prompt | Guardrails in code | Human approval |
|---|---|---|---|
| `baseline` | your friend's prompt and rules | no | no |
| `prompt_only` | the same + extra security sentences | no | no |
| `tools_only` | your friend's prompt + a note on refusals | action checks only (no document checks) | no |
| `guarded` | your friend's prompt + a note on the data markers and on refusals | yes | no |
| `baseline_human` | same as `baseline` | no | every action waits for staff |
| `prompt_only_human` | same as `prompt_only` | no | every action waits for staff |
| `guarded_human` | same as `guarded` | yes | every action waits for staff (`HUMAN_APPROVAL=flagged`: only warning cases) |


Website: set `$env:AGENT_VARIANT` in `start_website.ps1`. Evaluation: `--variant <key>`.
In an evaluation nobody approves; `evaluation/p1_eval.py` uses a simulated reviewer
(`--reviewer careful|rubber_stamp`, see `evaluation/simulated_reviewer.py`).
