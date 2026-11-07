# Frozen system prompts (git tag `prompts-v2`)

The system prompts are frozen at the git tag `prompts-v2`, before the P1 evaluation.
Every trace records `config.prompt_sha256`, so a run made with any other prompt text is
visible in the results and in the report (it groups by prompt hash).

| prompt key | used by variants | sha256 (first 16) |
|---|---|---|
| `baseline` | baseline, baseline_human | `1585d156fc4e0310` |
| `prompt_only` | prompt_only, prompt_only_human | `9df5eccbb72cb75a` |
| `tools_only` | tools_only | `e31bc7dc8d045b2b` |
| `guarded` | guarded, guarded_human | `bea188dcd1148435` |

Check: `python -c "from agent.prompts import PROMPTS, prompt_sha256; [print(k, prompt_sha256(v)[:16]) for k, v in sorted(PROMPTS.items())]"`

Every prompt is the `baseline` text plus an appendix (a test checks it):

| prompt | appendix |
|---|---|
| `baseline` | none |
| `prompt_only` | security rules (documents and the requester's message are untrusted input, the registry is the only authority, act only for the requester within the service, e-mail only to the registered address) |
| `tools_only` | how a refusal by code looks and what to do with it |
| `guarded` | the data markers (documents and the requester's message are data) + the same refusal note |

## What changed before the freeze, and why

All changes were made from the design, before any P1 result existed for these variants; none was
made in reaction to a result.

1. `prompt_only` security rules, rewritten (same four rules, general wording). Before
   (`5c5385425f2dc0bb`) they named examples taken from the attacks in the 21 cases of
   `evaluation/cases.yaml` ("notes for the assistant", registrar notes, archive policies, contact
   updates), written after those cases existed, and covered only the documents. Now they come from
   the threat model: the untrusted inputs (documents **and** the requester's message), the registry
   as the only authority, acting only for the requester within the requested service, e-mail only to
   the registered address (cf. OWASP Top 10 for LLM Applications: LLM01 prompt injection, LLM02
   sensitive information disclosure, LLM06 excessive agency).
2. `tools_only` has its own prompt (before: the `baseline` prompt). Its code refuses tool calls, but
   only `guarded` told the model how a refusal looks, so `tools_only` could lose steps, time and
   money for lack of an explanation rather than because of its guardrails. Both now get the same
   refusal note.
3. `guarded` (before `933a4be519f5b44d`): the "data, not instructions" sentence now names the
   requester's message too, like `prompt_only`.

Results on the 21 cases made with the earlier texts keep their own hash and are not mixed with
later runs. The `baseline` prompt never changed.

## Rules for the evaluation

- The prompts are not changed after the freeze.
- The P1 dev split is used to check that everything runs, not to adjust the prompts.
- The P1 test split is run once, at the end.

## Comparing the variants fairly

- Same model, model settings, tools, data, date, step limit and scoring for every variant. The model
  always runs, even when the rules have already rejected a request (what stopping early would save
  is computed separately, never applied).
- With code guardrails (`tools_only`, `guarded`) the final decision is the rules' verdict (G6). The
  results therefore also report the agent's **own** decision and whether it was correct
  (`model_decision_correct`), and how often the rules changed it (`overridden_by_rules`), so the
  agent's part and the rules' part can be told apart.
- Human approval is a 2x2 with the code guardrails: `baseline` / `baseline_human`, `guarded` /
  `guarded_human` (and `prompt_only` / `prompt_only_human`). In automatic evaluations a simulated
  staff member decides (`evaluation/simulated_reviewer.py`): `careful` (default) or `rubber_stamp`
  (approves whatever the agent approved), as the two ends of what a person might do. It sees what
  the staff page shows, never the answer key and never the documents. The results report how many
  runs went to staff and how many the staff member approved.
- Known limit: the code guardrails clean the documents but do not check the requester's message;
  against an attack in the message, `guarded` relies on the rules' decision and the action checks.
