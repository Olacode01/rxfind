# RxFind

**Someone has to ring round the pharmacies. It doesn't have to be you.**

You need a specific medication today. Which pharmacy actually has it, at what
price, and will they hold it? The only way to find out is to call them one at a
time and ask the same four questions.

RxFind does the calling. It returns a ranked list with a confidence score and
the transcript as evidence.

Built for the **CALL-E hackathon**. The Agent Skill is submitted as
[PR #49](https://github.com/CALLE-AI/awesome-phone-call-agents/pull/49) to
`awesome-phone-call-agents`.

```
medication + pharmacy list
        │
        ▼
  one CALL-E run per pharmacy, concurrently
        │
        ▼
  plan_call → run_call → poll get_call_run
        │
        ▼
  parse · normalise · rank
        │
        ▼
  where to go, with the evidence
```

---

## Why this, and not an AI receptionist

CALL-E's own docs describe it as built for *"low-frequency, personalized phone
tasks that were previously too expensive or custom to automate"* — explicitly
not high-volume call centres.

Most phone-agent projects build receptionists and outbound sales bots. Those
fight both the platform's positioning and every incumbent in the space. This
goes the other way: one person, one urgent errand, a handful of calls that
nobody would ever build a call centre for.

---

## What it produces

```
amoxicillin 500mg — 21 capsules

Pharmacy               Stock    Qty  Price   Rx   Hold  Conf
---------------------  -------  ---  ------  ---  ----  ----
Oakhill Chemist        yes       40  6.20 £  yes  24h   0.91
Riverside Pharmacy     partial   10  5.00 £  yes  48h   0.82
    They have only 10 units available, less than the requested 21 capsules.
Station Road Pharmacy  no         —       —    —     —  0.88
    Suggested trying their branch on Mill Lane.
```

Every row is backed by a transcript. Nothing is asserted that can't be checked
against what the pharmacist actually said.

---

## Three decisions worth explaining

### 1. One run per pharmacy, not one batched run

`to_phones` accepts an array, so batching looks like the obvious choice. But
`result.summary` and the confidence block come back **aggregated for the whole
batch** — `result.batch` gives you counts, not per-callee extraction.

Per-pharmacy attribution is the entire product here. Knowing "someone has 10
units at £5" is useless; you need to know *which one*. So RxFind places one run
per pharmacy and runs them concurrently. Identical call cost, far better data.

### 2. Extraction is parsed out of prose, because that's where it lands

CALL-E's MCP surface has no `result_schema` parameter — the REST docs advertise
one, MCP doesn't have it. So the field names have to be specified in the `goal`
text.

CALL-E honours them reliably. But they arrive in `result.summary` as a
formatted string, while `result.extracted` — the field documented as "Structured
extracted data" — carries an echo of the request:

```json
{"goal": "...", "region": "GB", "to_phones": ["+44…"], "calling": {...}}
```

So RxFind parses the summary, splitting on `key=` boundaries rather than commas
(free-text fields contain commas), then normalises and coerces client-side.

### 3. Confidence changes the ranking, not just the display

The ranking is in-stock first, then cheapest, then whoever will hold it — but
low-confidence results sink below high-confidence ones.

A reliable "no" is more useful to a patient than an unreliable "yes" that sends
them across town while unwell. Anything below 0.6 is flagged for verification
rather than presented as fact.

---

## Running it

```bash
git clone <your-repo-url> && cd rxfind
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

npm install -g @call-e/cli && calle auth login   # one-time
```

### Replay mode — explore without spending calls

```bash
RXFIND_REPLAY=1 uvicorn server:app --reload --port 8001
```

Serves saved runs from `runs/`. No MCP connection is made, no call is placed,
and the button says so. The entire interface was built this way.

### Live

```bash
uvicorn server:app --reload --port 8001
```

Reads `pharmacies.csv`. Each search places one real call per pharmacy.

### CLI

```bash
python rxfind.py --replay                       # free, from fixtures
python rxfind.py --dry-run                      # plans only, free
python rxfind.py --live --max-calls 3           # dials
```

---

## Layout

| File | Role |
|---|---|
| `calle_driver.py` | Generic CALL-E MCP driver. No pharmacy knowledge. |
| `rxfind.py` | The app — goal, parsing, normalisation, ranking, CLI. |
| `server.py` | FastAPI backend. Searches run as background tasks. |
| `static/index.html` | Single-page frontend with live call progress. |
| `pharmacies.csv` | The list to call. |
| `runs/` | Saved API responses — fixtures and evidence. |
| `FEEDBACK.md` | Integration findings reported to CALL-E. |
| `contrib/` | The Agent Skill submitted as PR #49. |

`calle_driver.py` is domain-agnostic and reusable for any CALL-E workflow.

---

## Cost discipline

The free tier is 20 calls. This project was built end to end on **one**.

- **Budget counts phones, not plans.** `to_phones` is an array, so one command
  can spend N calls. The guard charges `len(to_phones)`.
- **Checked up front.** Five pharmacies against three remaining calls fails
  before anything is dialled, not halfway through.
- **Reserve and refund.** A `run_call` that fails to start returns the credit.
- **`plan_id` is the idempotency key.** Never run the same plan twice.
- **Everything persists immediately.** No re-dialling to recover data you
  already paid for.
- **Replay by default.** UI and ranking work runs on fixtures.

---

## Safety

This calls real pharmacies about real medication, so:

- The agent identifies itself as automated in its opening line
- It gathers information only — no ordering, reserving, or committing
- It gives no medical advice; pharmacist suggestions are relayed as reported
  speech, never as recommendations
- It is not for emergencies, and says so rather than starting a call queue
- Prescription requirements are reported, never worked around
- Phone numbers are masked in summaries
- No recurring schedules are created as a side effect

Full detail in
[`contrib/skills/pharmacy-stock-check/references/safety.md`](contrib/skills/pharmacy-stock-check/references/safety.md).

---

## Notes for anyone integrating CALL-E over MCP

Collected in [`FEEDBACK.md`](FEEDBACK.md) and
[`contrib/skills/pharmacy-stock-check/references/calle-mcp-integration.md`](contrib/skills/pharmacy-stock-check/references/calle-mcp-integration.md).
The short version:

- **`plan_call` is free.** Iterate on goal wording at no cost; only `run_call`
  spends.
- **Extraction lands in `result.summary`**, not `result.extracted`.
- **`next_step.action` is a state machine** and the cleanest way to drive the
  flow — but it's a structured object on `run_call`/`get_call_run` and a plain
  string on `plan_call`.
- **The activity feed is cumulative** on every poll; `next_cursor` wasn't
  populated, so dedupe client-side.
- **`confirm_token` expires in ~24h.** Check before spending.
- **`ttl_seconds: 0`** retains run records permanently — useful when those
  records are your evidence.

---

## Limitations

- The pharmacy list is a curated CSV. Production would resolve pharmacies by
  location from a places API or a formulary database.
- Extraction quality depends on call quality. The confidence score is the
  honest signal, which is why it's surfaced rather than smoothed away.
- CALL-E supports a finite set of regions (US, SG, MY, IN, AE, AU, CA, GB, VN,
  DE, JP, FR, MX, BR, ID, PH, KE at time of writing).

---

## Licence

MIT
