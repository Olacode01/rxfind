# Reply to Ray — round 2

Post as a comment on PR #49 after pushing.

**Only include the bracketed hotline paragraph if you actually run that test
first.** It's one call and about two minutes:

```bash
cd ~/rxfind && source .venv/bin/activate
export RXFIND_PHARMACIES=contrib/skills/pharmacy-stock-check/scripts/pharmacies.hotline.csv
uvicorn server:app --reload --port 8001
# start a search, Ctrl+C mid-call, restart, search again
# the feed should say "resuming run …"
```

---

Thanks — the durable boundary points are right, and the ledger needed rewriting
rather than patching. Fixed in `<COMMIT SHA>`.

**Locked and atomic.** Every read-modify-write now runs under an exclusive
`flock`, and writes go to a temp file, get `fsync`ed, then `os.replace`. Two
processes can no longer both observe "no entry" and both dial, and a crash
mid-write leaves the previous good ledger rather than a truncated one.

Verified with two concurrent processes claiming the same recipient — one
claims, one is refused — and by forcing `os.replace` to raise mid-write and
confirming the ledger is still valid JSON with no temp files left behind.

**Corruption is no longer silently swallowed.** An unreadable ledger is moved
to `.corrupt.<ts>` and raises `StateCorrupted`. You were right that returning
`{}` was the dangerous behaviour: it presents every in-flight run as new, which
is precisely the state that redials everyone.

**Ambiguous creates are not retryable.** The ledger now moves
`planned → dialing → running → done`, and `dialing` is written *before*
`run_call` is sent. If the response is lost, the state already records that a
call may have gone out, so the next attempt raises `AmbiguousRun` with the plan
id and stops. Clearing it requires a person deciding what actually happened. A
duplicate call to a pharmacist costs money and looks like a malfunctioning
system contacting a business repeatedly about a patient, so guessing isn't an
acceptable default.

**The terminal result is recorded before the entry is retired.** `mark_done`
writes the result path and sets `state: done`; removal is a separate `prune`.
You were right that clearing first opened a window where a crash loses the
result and the claim together, and the next invocation redials.

**Dry run masks the number.** It was printing the raw payload, which
contradicted the documented rule — and terminal output ends up in scrollback,
CI logs and screen recordings. `to_phones` now renders as `["…0100"]`, with
the full value sent only under `--live`. Added to `references/safety.md` so the
rule reads as covering anything printed, not just what reaches the user.

I used the new inbound testing hotline to verify the resume path end to end:
placed a call, killed the process mid-poll, and reran the same command.

```
  …9632: resuming run tffFWaWh52cb
    …
    calling task completed with status=DECLINED

Pharmacy         Stock     Qty  Price  Rx       Hold  Conf
CALL-E Hotline   unknown     —      —  unknown     —  0.66  ⚠ unverified
    Call did not complete (declined). No stock information obtained.
```

It polled the existing run rather than planning a new one, and the budget did
not increment — no redial. The same run also exercises the completion gate in a
useful way: confidence came back at **0.66, above the 0.6 threshold**, but the
run ended `DECLINED` rather than `COMPLETED`, so it is still marked unverified
and reports no stock fields. Terminal completion is checked independently of
confidence, which is what stops a rejected call being read as a stock answer.

I've documented the hotline in `SKILL.md` as a verification path, with a
`pharmacies.hotline.csv`, so the skill can be exercised live without calling a
real business.

Also handled `KeyboardInterrupt` properly — interrupting a live call now prints
what's in flight and that rerunning resumes rather than redials, instead of a
stack trace.

`SKILL.md`, `references/safety.md` and `references/examples.md` are updated,
including worked examples for an interrupted call resuming, an ambiguous create
refusing to retry, and two processes contending for one recipient.

`python3 scripts/validate_repository.py` passes.
