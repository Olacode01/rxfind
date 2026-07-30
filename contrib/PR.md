# PR #49 — corrected title and body

https://github.com/CALLE-AI/awesome-phone-call-agents/pull/49

Two edits needed:

1. **Title** — currently `add pharmacy-stock-check skill`. Their conventions
   require the `feat:` prefix. Click **Edit** beside the title and change it to:

```
feat: add pharmacy-stock-check skill
```

2. **Body** — currently the unfilled template. Click the `···` menu on your
   first comment → **Edit**, select all, and replace with everything below the
   line.

---

## Summary

Adds `skills/pharmacy-stock-check/` — a portable Agent Skill for finding which
pharmacy actually has a specific medication in stock, at what price, and whether
they will hold it.

The user supplies a medication and a list of pharmacy numbers. The agent calls
each one, asks the same set of questions, and returns a ranked answer with a
confidence score and the transcript as evidence.

This belongs here because it's the kind of work CALL-E is positioned for:
low-frequency, personal, high-stakes phone tasks that were never worth
automating with a traditional call platform. Someone with a prescription and no
time currently rings round themselves.

```
skills/pharmacy-stock-check/
├── SKILL.md                              workflow, safety boundaries, cost controls
├── references/
│   ├── calle-mcp-integration.md          MCP surface behaviour notes
│   ├── safety.md                         medical-adjacent calling boundaries
│   └── examples.md                       worked conversations
└── scripts/
    ├── pharmacy_search.py                reference implementation, dry-run by default
    └── pharmacies.csv                    fictional sample numbers
```

### Integration notes that may be useful beyond this skill

`references/calle-mcp-integration.md` documents behaviour I hit while building
this that isn't in the current docs. Sharing in case it's useful to other
contributors:

- **Extraction lands in `result.summary`, not `result.extracted`.** On a
  completed run, `result.extracted` contained an echo of the request (`goal`,
  `region`, `to_phones`, `calling` metadata) rather than the conversational
  fields. The collected fields were serialised as `key=value` pairs inside the
  summary string. The agent honours key names given in the `goal` prose
  reliably — they just arrive as prose. Parsing has to split on `key=`
  boundaries rather than commas, since free-text fields contain commas.
- **`next_step.action` is a clean state machine** and made the integration much
  simpler than polling on status strings alone. It isn't mentioned in the
  README. Note it's a structured object on `run_call` / `get_call_run` but a
  plain string on `plan_call`.
- **Batching aggregates results.** `to_phones` accepts an array, but
  `result.summary` and the confidence block come back for the batch as a whole.
  Workflows needing per-recipient attribution should place one run per recipient
  and run them concurrently — same call cost.
- **The activity feed is cumulative** on every poll and `next_cursor` wasn't
  populated, so clients need to dedupe on `(ts, message)`.

### Safety

The skill can place real calls, so it specifies explicit user confirmation
before dialling, E.164 numbers only with no guessed country codes, identifying
as an automated assistant in the opening line, masked numbers in all summaries
and logs, information gathering only with no ordering or reserving, no medical
advice with pharmacist suggestions relayed as reported speech, an explicit
decline path for urgent situations pointing to emergency services rather than
starting a call queue, and confidence surfaced rather than smoothed over.

`references/examples.md` includes a worked case where the agent declines to call.

### Cost controls

Charges `len(to_phones)` rather than 1, since one plan can spend N calls. Checks
the budget for the whole batch before dialling anything. Treats `plan_id` as the
idempotency key. Checks `confirm_token` expiry before spending. Persists every
response immediately, so there's no re-dialling to recover paid-for data.

## Type

- [x] New skill
- [ ] New runnable app
- [ ] New workflow plugin
- [ ] New provider adapter
- [ ] New scheduler recipe
- [ ] README awesome-list entry
- [ ] Safety or documentation update
- [ ] Validation or tooling update

## Checklist

- [x] Repository-facing content is written in English.
- [x] Branch name, commit messages, and PR title follow `docs/git-naming-conventions.md`.
- [x] No secrets, tokens, private phone numbers, call recordings, or private transcripts are included.
- [x] Real-world side effects are clearly described.
- [x] Phone numbers are masked in documentation and test fixtures unless they are clearly fictional.
- [x] Recurring workflows include cancellation behavior. *(N/A — this is a one-shot workflow, and `SKILL.md` explicitly forbids creating recurring schedules as a side effect.)*
- [x] Runnable code has a dry-run, fake-server, or no-call path by default.
- [x] `python3 scripts/validate_repository.py` passes.

### Verification

- `python3 scripts/validate_repository.py` passes.
- `scripts/pharmacy_search.py` is dry-run by default; `--live` is required to
  dial. The dry-run path exercises `plan_call` (free) and prints the exact
  payload it would send.
- Parsing, normalisation and ranking verified against a real completed run
  (GB, 107s, `task_completed: true`, confidence 0.82).
- All sample numbers are from the reserved fictional range +1 555 0100–0199.
