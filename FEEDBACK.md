# CALL-E Feedback Log — RxFind

Running log for the Most Valuable Feedback prize ($200 × 5).
Every entry: what I expected, what happened, why it matters, what would fix it.

Started: 2026-07-26
Account: teetoheeb@gmail.com
Surface: MCP (`https://seleven-mcp-sg.airudder.com/mcp/openagent_oauth`), Python client

---

## 1. MCP has no `result_schema`, but REST advertises one

**Severity:** High — architectural

**Expected:** The README's REST example and the TypeScript SDK sample both show a
`result_schema` field with JSON Schema, plus a `resultValidation` field on the
response. I designed my extraction layer around it.

**Actual:** `plan_call` on the MCP surface accepts only `plan_id`, `to_phones`,
`region`, `language`, `goal`, `scheduled_at`, `retry_confirmation_action`,
`user_input`, `ttl_seconds`. No `result_schema`. Output gives
`result.extracted` typed as `additionalProperties: true` — free-form.

**Why it matters:** Structured, machine-actionable results are CALL-E's core
pitch versus generic voice platforms. On MCP, the caller has no way to declare
the contract, so key names vary per conversation and every consumer has to write
a normalisation and alias-matching layer before the data is safe to use. That
work is identical for every integrator, which means everyone will write it
slightly differently and slightly wrong.

**Workaround:** Enumerate the exact key names inside the `goal` prose, then
alias-match and coerce client-side. Improves consistency but doesn't guarantee it,
and provides no validation signal equivalent to `resultValidation`.

**Suggested fix:** Add optional `result_schema` to `plan_call` and surface
`result.validation` on `get_call_run`. Failing that, document clearly that the
MCP and REST surfaces have different extraction contracts — right now the README
implies they're the same product surface.

---

## 2. Batch calling has no cost preview, which makes quota loss easy

**Severity:** High — safety / cost

**Expected:** Some indication of how many calls a plan will consume before
committing to it.

**Actual:** `to_phones` is an array with no documented cap. `plan_call` returns
`confirm_summary` and `confirm_token`, but nothing states "this will place N
calls." A new user on the 20-call free tier can spend their entire quota in a
single `run_call` without an explicit number in front of them.

**Why it matters:** Calls cost real money and the free tier is small. The
confirmation step exists precisely to prevent mistakes, but it confirms *intent*
without confirming *cost*.

**Suggested fix:** Add `estimated_call_count` and remaining-quota to the
`plan_call` output so `confirm_summary` can state the spend. Optionally a
`max_calls` guard parameter that fails the plan rather than truncating silently.

---

## 3. Token cache shape is undocumented

**Severity:** Low — DX

**Expected:** A documented way for a Python client to reuse `calle auth login`
state. The README points at `examples/mcp-broker-client` and
`examples/python-batch-runner` for "CLI auth state" reuse.

**Actual:** `calle auth status` reveals `cache_path`
(`~/.calle-mcp/cli/<hash>/token.json`) but the JSON shape isn't documented, so a
client has to probe key names defensively.

**Suggested fix:** Either document the token file schema, or add
`calle auth token --json` that emits a stable `{access_token, expires_at}`.

---

## 4. `next_step` is excellent and deserves louder documentation

**Severity:** Positive

The `next_step.action` enum on `run_call` / `get_call_run`
(`poll_get_call_run`, `ask_user_for_missing_info`,
`ask_user_for_retry_confirmation`, `plan_call_same_plan_id`, `report_result`,
`report_blocked`) makes the whole flow drivable as a clean state machine, with
`poll_after_seconds` for backoff. This is the single best-designed part of the
API and it isn't mentioned in the README at all — I only found it by running
`calle mcp tools`.

**Suggested fix:** Put a `next_step` state diagram in the integration guide. It
would have saved me a design iteration.

---

## 5. `result.extracted` contains the request echo, not the extracted data

**Severity:** High — the single biggest integration surprise so far

**Expected:** `result.extracted` is documented as "Structured extracted data"
with `additionalProperties: true`. I expected the fields the agent gathered
during the conversation.

**Actual:** On a successful run (`run_id` 4BJPgjIFv3gZwCXbyFn7dA, GB, 107s,
`task_completed: true`, confidence 0.82) `extracted` contained only an echo of
the request plus telephony metadata:

```json
{"goal": "...", "region": "GB", "language": "English",
 "to_phones": ["+44..."], "repair": {...}, "calling": {...}}
```

None of the ten fields the agent had actually collected were present.

They were all in `result.summary`, serialised into a string:

```
Stock check completed. Result: in_stock=partial, form_available=both,
quantity_available=10, unit_price=5, currency=GBP, requires_prescription=yes,
can_hold=yes, hold_duration_hours=48, alternative_suggested=GPNHS/unclear,
pharmacist_notes=They have only 10 units available, less than the requested
21 capsules.
```

**Why it matters:** The agent honoured the key names given in the goal prose
perfectly — the extraction logic works well. But it emits them as a formatted
string in a field meant for humans, while the field meant for machines carries
the request back. Every integrator has to write a regex parser over prose to
get at data the system already has in structured form. That parser is also
fragile: values contain commas (`pharmacist_notes` above), so naive splitting
breaks, and there's no guarantee the summary sentence format is stable across
releases.

**Workaround:** Regex on `key=` boundaries rather than commas, using the known
field names as delimiters, with `extracted` checked as a fallback.

**Suggested fix:** Populate `result.extracted` with the conversational fields
and move the request echo to a separate `request` key. This is likely a small
change and would remove the most error-prone part of every integration.

---

## 6. Quality notes on the extraction itself (positive)

Worth saying plainly: the inference quality was better than expected.

- Caller said "10 units" against a request for 21 → `in_stock=partial`, not
  `yes`. Correct and non-obvious.
- "just two days" → `hold_duration_hours=48`. Unit conversion handled.
- Heavily disfluent ASR ("Depressed by units. £5 per unit") still yielded
  `unit_price=5, currency=GBP`.
- `completion_confidence: 0.82` with three specific evidence strings is an
  honest, useful reliability signal — and more actionable than the extracted
  fields themselves, because it tells the consumer when to trust them.

The confidence/evidence block deserves more prominence in the docs. It's the
feature that makes results safe to act on automatically.

---

## 7. Activity feed is cumulative, with no cursor advance on poll

**Severity:** Low — DX

**Expected:** Polling `get_call_run` returns new activity entries, or a
`next_cursor` that advances.

**Actual:** Each poll returns the entire activity history from the beginning.
Over a 107-second call polled every 2s, the same lines are re-emitted dozens of
times. `next_cursor` exists in the schema but wasn't populated.

**Workaround:** Client-side dedupe on `(ts, message)`.

**Suggested fix:** Honour `cursor` for incremental fetch, or populate
`next_cursor` on the response.

---

## 8. The credential cache records the endpoint but not the account

**Severity:** Medium — blocks safe resumption of interrupted work

**Observed cache contents** (`~/.calle-mcp/cli/<hash>/token.json`):

```
server_url, auth_base_url, issued_at, expires_at, token
```

**What works:** `server_url` being present is genuinely useful. It lets a client
bind a credential to the origin it was issued for and refuse to send it
elsewhere, without reverse-engineering the cache directory hash.

**What's missing:** nothing identifies the *account*. The cache directory is
derived from the server URL, so re-authenticating as a different account on the
same endpoint reuses the same directory. And the token is opaque — it contains
dots but the payload isn't base64 JSON, so there's no subject claim to fall back
on.

**Why it matters:** any workflow that persists in-flight state across a crash
has to answer "is this pending action mine?" before resuming it. Without an
account identifier there is no safe answer. Resuming could act on another
account's run; assuming no run exists could place a duplicate call to a real
person. The only correct behaviour is to fail closed — which means interrupted
work can't be resumed automatically at all.

In this skill that turns a working feature into one requiring a manual
`CALLE_ACCOUNT_ID` environment variable.

**Suggested fix:** record an account identifier alongside `server_url` in the
cache — an account id, user id, or the login email would each be sufficient.
Alternatively, expose it via `calle auth status`, which already reports
`server_url` and `cache_path`. Either would let clients namespace durable state
correctly instead of failing closed.

---

## Template for further entries

## N. <title>

**Severity:**
**Expected:**
**Actual:**
**Why it matters:**
**Workaround:**
**Suggested fix:**
