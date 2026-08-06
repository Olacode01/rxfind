"""
RxFind — FastAPI backend.

    pip install fastapi uvicorn
    RXFIND_REPLAY=1 uvicorn server:app --reload    # fixtures, zero calls
    uvicorn server:app --reload                    # live, spends calls

Replay mode is the default posture during development. You have 20 free calls;
the interface should never be the thing that spends them.

The interesting design problem here is that a pharmacy search is not a request /
response — it's minutes of real phone conversations happening in parallel. The
UI has to show that as it unfolds, so search is a background task and the client
polls a snapshot of progress.
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from calle_driver import (
    AmbiguousRun, BudgetExceeded, CallBudget, CalleDriver,
    ConfigurationMismatch, InvalidPhoneNumber, RunLocked, StateCorrupted,
    validate_e164,
)
from rxfind import load_pharmacies, pharmacy_goal, rank, to_record

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("rxfind.server")

HERE = Path(__file__).parent
# Point at a gitignored local list for real runs, so real numbers never reach
# the repo: export RXFIND_PHARMACIES=pharmacies.local.csv
PHARMACIES = HERE / os.environ.get("RXFIND_PHARMACIES", "pharmacies.csv")
RUNS = HERE / "runs"

REPLAY = os.environ.get("RXFIND_REPLAY") == "1"
MAX_CALLS = int(os.environ.get("RXFIND_MAX_CALLS", "6"))

app = FastAPI(title="RxFind", version="1.0")


class SearchRequest(BaseModel):
    drug: str = "amoxicillin"
    dosage: str = "500mg"
    quantity: str = "21 capsules"
    region: str = "GB"
    pharmacy_phones: list[str] = []      # empty = every pharmacy in the CSV


@dataclass
class Search:
    """One search across N pharmacies, tracked while it runs."""

    id: str
    request: SearchRequest
    pharmacies: list[dict[str, str]]
    status: str = "planning"             # planning | calling | done | failed
    activity: list[dict[str, Any]] = field(default_factory=list)
    records: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    replay: bool = False
    # asyncio only holds a weak reference to running tasks. Without keeping a
    # strong one, a long search can be garbage-collected mid-call.
    task: Any = None

    def log(self, phone: str, kind: str, message: str) -> None:
        self.activity.append({"phone": phone, "kind": kind, "message": message})

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "replay": self.replay,
            "error": self.error,
            "pharmacies": self.pharmacies,
            "activity": self.activity,
            "results": rank(self.records),
            "pending": len(self.pharmacies) - len(self.records),
        }


SEARCHES: dict[str, Search] = {}


# --------------------------------------------------------------------------
# Replay
# --------------------------------------------------------------------------


def replay_records(pharmacies: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Rebuild results from saved runs. Free, and the UI can't tell the
    difference — which is the point."""
    by_phone = {p["phone"]: p for p in pharmacies}
    records = []
    for path in sorted(glob.glob(str(RUNS / "result_*.json"))):
        final = json.loads(Path(path).read_text())
        extracted = (final.get("result") or {}).get("extracted") or {}
        phones = extracted.get("to_phones") or []
        phone = phones[0] if phones else ""
        records.append(to_record(final, by_phone.get(phone, {"phone": phone})))
    return records


# --------------------------------------------------------------------------
# Search execution
# --------------------------------------------------------------------------


async def run_search(search: Search) -> None:
    if search.replay:
        search.status = "calling"
        for pharmacy in search.pharmacies:
            search.log(pharmacy["phone"], "replay", f"Replaying {pharmacy['name']}")
            await asyncio.sleep(0.4)          # let the UI show progress
        search.records = replay_records(search.pharmacies)
        search.status = "done"
        return

    driver = CalleDriver(budget=CallBudget(max_calls=MAX_CALLS), dry_run=False)
    goal = pharmacy_goal(
        search.request.drug, search.request.dosage, search.request.quantity
    )

    try:
        driver.budget.check_available(len(search.pharmacies))
    except BudgetExceeded as exc:
        search.status = "failed"
        search.error = str(exc)
        return

    search.status = "calling"
    semaphore = asyncio.Semaphore(3)

    async def one(pharmacy: dict[str, str]) -> None:
        async with semaphore:
            phone = pharmacy["phone"]
            search.log(phone, "start", f"Calling {pharmacy['name']}")
            try:
                plan = await driver.plan(
                    goal=goal, to_phones=[phone], region=search.request.region
                )
                if not plan.get("ready_to_run"):
                    search.log(phone, "blocked", "Planner needs more detail")
                    return

                run = await driver.run(plan, 1)
                run_id = run.get("run_id")
                if not run_id:
                    search.log(phone, "failed", "No run started")
                    return

                search.log(phone, "ringing", "Call placed")
                final = await driver.poll(run_id)
                record = to_record(final, pharmacy)
                search.records.append(record)
                search.log(
                    phone, "done",
                    f"{pharmacy['name']}: in_stock={record.get('in_stock')}",
                )
            except (AmbiguousRun, RunLocked, StateCorrupted,
                    ConfigurationMismatch) as exc:
                # These exist to prevent a redial. Surface them as-is rather
                # than as generic failures — the message tells the user what
                # to do.
                search.log(phone, "blocked", str(exc))
            except Exception as exc:
                log.exception("call failed")
                search.log(phone, "failed", f"{type(exc).__name__}: {exc}")

    await asyncio.gather(*(one(p) for p in search.pharmacies))
    search.status = "done"


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------


@app.get("/api/health")
async def health() -> dict[str, Any]:
    budget = CallBudget(max_calls=MAX_CALLS)
    pharmacies = load_pharmacies(PHARMACIES) if PHARMACIES.exists() else []
    return {
        "replay": REPLAY,
        "calls_spent": budget.spent,
        "local_cap": MAX_CALLS,
        "pharmacies": len(pharmacies),
        "fixtures": len(glob.glob(str(RUNS / "result_*.json"))),
    }


@app.get("/api/pharmacies")
async def pharmacies() -> list[dict[str, str]]:
    if not PHARMACIES.exists():
        raise HTTPException(404, "No pharmacies.csv")
    return load_pharmacies(PHARMACIES)


@app.post("/api/search")
async def start_search(request: SearchRequest) -> JSONResponse:
    """Kick off a search and return immediately.

    Phone calls take minutes. Blocking the request until they finish would time
    out on any sane proxy, and the user would see nothing while it happened.
    """
    everything = load_pharmacies(PHARMACIES) if PHARMACIES.exists() else []
    chosen = [
        p for p in everything
        if not request.pharmacy_phones or p["phone"] in request.pharmacy_phones
    ]
    if not chosen:
        raise HTTPException(400, "No pharmacies selected.")

    # Collapse duplicate recipients. Rows are dispatched concurrently, so the
    # same number listed twice would ring one pharmacist twice within a second.
    deduped: dict[str, dict[str, str]] = {}
    for pharmacy in chosen:
        deduped.setdefault(pharmacy["phone"], pharmacy)
    if len(deduped) < len(chosen):
        log.warning("Skipped %s duplicate row(s)", len(chosen) - len(deduped))
    chosen = list(deduped.values())

    # Validate before anything can be dialled. A local-format number reaching
    # the planner is how the wrong person gets called.
    try:
        for pharmacy in chosen:
            pharmacy["phone"] = validate_e164(pharmacy["phone"])
    except InvalidPhoneNumber as exc:
        raise HTTPException(400, str(exc)) from exc

    search = Search(
        id=uuid.uuid4().hex[:12],
        request=request,
        pharmacies=chosen,
        replay=REPLAY,
    )
    SEARCHES[search.id] = search
    search.task = asyncio.create_task(run_search(search))
    return JSONResponse({"id": search.id, "pharmacies": len(chosen)})


@app.get("/api/search/{search_id}")
async def search_status(search_id: str) -> JSONResponse:
    search = SEARCHES.get(search_id)
    if not search:
        raise HTTPException(404, "Unknown search.")
    return JSONResponse(search.snapshot())


@app.get("/api/search/{search_id}/transcript/{phone}")
async def transcript(search_id: str, phone: str) -> JSONResponse:
    """The transcript is the evidence behind a result. Anything the agent
    reports should be checkable against what was actually said."""
    search = SEARCHES.get(search_id)
    if not search:
        raise HTTPException(404, "Unknown search.")
    for record in search.records:
        if record.get("pharmacy_phone") == phone:
            return JSONResponse({
                "transcript": record.get("transcript"),
                "summary": record.get("raw_summary"),
                "evidence": record.get("evidence", []),
                "confidence": record.get("confidence_score"),
            })
    raise HTTPException(404, "No result for that pharmacy.")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(HERE / "static" / "index.html")


if (HERE / "static").exists():
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8001)))
