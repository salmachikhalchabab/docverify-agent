# Frozen system prompts (git tag `prompts-v1`)

The system prompts are frozen at the git tag `prompts-v1`, before the P1 evaluation.
Every trace records `config.prompt_sha256`, so a run made with any other prompt text is
visible in the results and in the report (it groups by prompt hash).

| prompt key | used by variants | sha256 (first 16) |
|---|---|---|
| `baseline` | baseline, tools_only | `1585d156fc4e0310` |
| `prompt_only` | prompt_only, prompt_only_human | `9df5eccbb72cb75a` |
| `guarded` | guarded, guarded_human | `933a4be519f5b44d` |

Check: `python -c "from agent.prompts import PROMPTS, prompt_sha256; [print(k, prompt_sha256(v)[:16]) for k, v in sorted(PROMPTS.items())]"`

## What changed before the freeze

`prompt_only` security rules, rewritten (same four rules, general wording):

- **Before** (sha256 `5c5385425f2dc0bb`): the rules named examples taken from the attacks in the
  21 cases of `evaluation/cases.yaml` ("notes for the assistant", registrar notes, archive policies,
  contact updates), written after those cases existed, and covered only the documents.
- **After** (`9df5eccbb72cb75a`): the rules come from the threat model, not from the test cases:
  the untrusted inputs (the uploaded documents **and** the requester's message), the registry as the
  only authority, acting only for the requester within the requested service, e-mail only to the
  registered address (cf. OWASP Top 10 for LLM Applications: LLM01 prompt injection, LLM02 sensitive
  information disclosure, LLM06 excessive agency). No wording is taken from a test case.

Results on the 21 cases that were made with the earlier `prompt_only` text keep their own hash and
are not mixed with later runs. The `baseline` and `guarded` prompts did not change.

Rules for the evaluation: the prompts are not changed after the freeze; the P1 dev split is used
to check that everything runs, not to adjust the prompts; the P1 test split is run once at the end.
