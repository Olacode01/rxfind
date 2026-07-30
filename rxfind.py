"""
RxFind — find a medication that's actually in stock nearby.

The patient enters a drug, dosage and quantity. RxFind calls pharmacies,
asks a pharmacist about availability, price, prescription and hold, then
returns a ranked list of where to go.

One run PER PHARMACY, executed concurrently. CALL-E's `to_phones` accepts an
array, but the summary and confidence block come back aggregated for the whole
batch — and per-pharmacy attribution is the entire product here. Same call cost,
far better data.

    python rxfind.py --replay                      # rebuild from saved runs, free
    python rxfind.py --dry-run                     # plan only, free
    python rxfind.py --live --max-calls 3          # actually dial
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import glob
import json
import logging
import re
from pathlib import Path
from typing import Any

from calle_driver import CalleDriver, CallBudget, parse_summary_fields

log = logging.getLogger("rxfind")


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

ENUMS = {
    "in_stock": {"yes", "no", "partial", "unknown"},
    "form_available": {"brand", "generic", "both", "unknown"},
    "requires_prescription": {"yes", "no", "unknown"},
    "can_hold": {"yes", "no", "unknown"},
}

NUMERIC = {"quantity_available", "unit_price", "hold_duration_hours"}

ALIASES = {
    "in_stock": ("in_stock", "stock", "availability", "available"),
    "form_available": ("form_available", "brand_or_generic", "form"),
    "quantity_available": ("quantity_available", "quantity", "qty"),
    "unit_price": ("unit_price", "price_per_unit", "price"),
    "currency": ("currency", "currency_code"),
    "requires_prescription": ("requires_prescription", "prescription_required",
                              "requires_rx", "prescription"),
    "can_hold": ("can_hold", "will_hold", "can_reserve", "hold"),
    "hold_duration_hours": ("hold_duration_hours", "hold_hours", "hold_duration"),
    "alternative_suggested": ("alternative_suggested", "alternative", "substitute"),
    "pharmacist_notes": ("pharmacist_notes", "notes", "comments"),
}

ALL_KEYS = [a for aliases in ALIASES.values() for a in aliases]

NULLISH = {"", "unknown", "unclear", "n/a", "na", "none", "null",
           "not stated", "not specified", "not provided"}


def _enum(value: Any, allowed: set[str]) -> str:
    if value is None:
        return "unknown"
    s = str(value).strip().lower().rstrip(".")
    if s in NULLISH:
        return "unknown"
    if s in allowed:
        return s
    if s in {"true", "y", "available", "in stock"}:
        return "yes" if "yes" in allowed else "unknown"
    if s in {"false", "n", "unavailable", "out of stock"}:
        return "no" if "no" in allowed else "unknown"
    # "GPNHS/unclear" style compound answers — take a known token if present
    for token in re.split(r"[/,;]", s):
        if token.strip() in allowed:
            return token.strip()
    return "unknown"


def _number(value: Any) -> float | None:
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in NULLISH:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", s.replace(",", ""))
    return float(m.group()) if m else None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return None if s.lower() in NULLISH else s


# --------------------------------------------------------------------------
# Goal prompt
# --------------------------------------------------------------------------


def pharmacy_goal(drug: str, dosage: str, quantity: str) -> str:
    """Carries the extraction contract.

    MCP has no result_schema, so the field list has to live in the prose.
    Naming the keys explicitly works — CALL-E honours them and emits them into
    `result.summary` as key=value pairs.
    """
    return (
        "You are calling a pharmacy on behalf of a patient looking for a "
        "medication. Identify yourself as an automated assistant immediately, "
        "and keep the call brief and polite.\n\n"
        f"Find out whether they currently have {drug} {dosage} in stock. The "
        f"patient needs {quantity}.\n\n"
        "Capture these fields in the structured result, using exactly these key names:\n"
        "- in_stock: yes, no, partial, or unknown. Use partial if they have some "
        "but fewer than the patient needs.\n"
        "- form_available: brand, generic, both, or unknown\n"
        "- quantity_available: integer units they have\n"
        "- unit_price: number, price per unit\n"
        "- currency: three-letter currency code\n"
        "- requires_prescription: yes, no, or unknown\n"
        "- can_hold: yes, no, or unknown\n"
        "- hold_duration_hours: number of hours they will hold it\n"
        "- alternative_suggested: any alternative or other branch they mention\n"
        "- pharmacist_notes: anything else useful, one short sentence\n\n"
        "If they do not have it, still ask about alternatives or another branch. "
        "Do not place an order or commit to anything on the patient's behalf. "
        "Thank them and end the call. If nobody answers, report that the pharmacy "
        "could not be reached; do not treat that as a successful stock check."
    )


# --------------------------------------------------------------------------
# Result -> record
# --------------------------------------------------------------------------


def to_record(final: dict[str, Any], pharmacy: dict[str, str] | None = None) -> dict[str, Any]:
    """One terminal get_call_run response -> one ranked-list row."""
    result = final.get("result") or {}
    summary = result.get("summary") or result.get("post_summary") or ""

    fields = parse_summary_fields(summary, ALL_KEYS)

    # `extracted` is mostly a request echo, but check it for real domain keys
    # in case that gets fixed upstream.
    extracted = result.get("extracted") or {}
    if isinstance(extracted, dict):
        for k, v in extracted.items():
            lk = str(k).lower()
            if lk in ALL_KEYS and lk not in fields:
                fields[lk] = v

    record: dict[str, Any] = {}
    for canonical, aliases in ALIASES.items():
        value = next((fields[a] for a in aliases if a in fields), None)
        if canonical in ENUMS:
            record[canonical] = _enum(value, ENUMS[canonical])
        elif canonical in NUMERIC:
            record[canonical] = _number(value)
        else:
            record[canonical] = _text(value)

    outcome = result.get("outcome") or {}
    confidence = outcome.get("completion_confidence") or {}
    calling = extracted.get("calling") or {} if isinstance(extracted, dict) else {}

    record.update(
        pharmacy_name=(pharmacy or {}).get("name"),
        pharmacy_phone=(pharmacy or {}).get("phone"),
        run_status=final.get("status"),
        task_completed=outcome.get("task_completed"),
        confidence_score=confidence.get("score"),
        confidence_label=confidence.get("label"),
        evidence=outcome.get("evidence") or [],
        transcript=result.get("transcript"),
        call_id=result.get("call_id"),
        duration_seconds=calling.get("duration_seconds"),
        raw_summary=summary,
    )
    return record


# --------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------

_STOCK_ORDER = {"yes": 0, "partial": 1, "unknown": 2, "no": 3}


def rank(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """In-stock first, then cheapest, then whoever will hold it.

    Low-confidence results sink: a confident "no" is more useful to a patient
    than an unreliable "yes" that wastes a trip.
    """
    def key(r: dict[str, Any]):
        confidence = r.get("confidence_score")
        return (
            _STOCK_ORDER.get(r.get("in_stock"), 3),
            0 if (confidence is None or confidence >= 0.6) else 1,
            r["unit_price"] if r.get("unit_price") is not None else float("inf"),
            0 if r.get("can_hold") == "yes" else 1,
        )

    return sorted(records, key=key)


def render(records: list[dict[str, Any]]) -> str:
    headers = ["#", "Pharmacy", "Stock", "Qty", "Price", "Rx", "Hold", "Conf"]
    rows = []
    for i, r in enumerate(records, 1):
        price = r.get("unit_price")
        currency = r.get("currency") or ""
        hold = "—"
        if r.get("can_hold") == "yes":
            hours = r.get("hold_duration_hours")
            hold = f"{hours:.0f}h" if hours else "yes"
        rows.append([
            str(i),
            (r.get("pharmacy_name") or r.get("pharmacy_phone") or "?")[:24],
            r.get("in_stock") or "unknown",
            f"{r['quantity_available']:.0f}" if r.get("quantity_available") else "—",
            f"{price:g} {currency}".strip() if price is not None else "—",
            r.get("requires_prescription") or "unknown",
            hold,
            f"{r['confidence_score']:.2f}" if r.get("confidence_score") is not None else "—",
        ])

    widths = [max(len(h), *(len(row[i]) for row in rows)) if rows else len(h)
              for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    out = [line, "  ".join("-" * w for w in widths)]
    out += ["  ".join(c.ljust(w) for c, w in zip(row, widths)) for row in rows]

    for i, r in enumerate(records, 1):
        if r.get("pharmacist_notes"):
            out.append(f"  [{i}] {r['pharmacist_notes']}")
    return "\n".join(out)


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------


def load_pharmacies(path: Path) -> list[dict[str, str]]:
    with path.open() as fh:
        return [dict(row) for row in csv.DictReader(fh) if row.get("phone")]


async def search(
    driver: CalleDriver,
    *,
    drug: str,
    dosage: str,
    quantity: str,
    pharmacies: list[dict[str, str]],
    region: str,
    concurrency: int = 3,
) -> list[dict[str, Any]]:
    """One run per pharmacy, bounded concurrency.

    Budget is checked up front for the whole search, so a five-pharmacy request
    against three remaining calls fails before anything is dialled rather than
    halfway through.
    """
    if not driver.dry_run and len(pharmacies) > driver.budget.remaining:
        raise SystemExit(
            f"{len(pharmacies)} pharmacies but only {driver.budget.remaining} "
            f"calls left. Trim the list or raise --max-calls."
        )

    goal = pharmacy_goal(drug, dosage, quantity)
    semaphore = asyncio.Semaphore(concurrency)

    async def one(pharmacy: dict[str, str]) -> dict[str, Any] | None:
        async with semaphore:
            log.info("→ %s (%s)", pharmacy.get("name"), pharmacy["phone"])
            try:
                final = await driver.execute(
                    goal=goal, phone=pharmacy["phone"], region=region
                )
            except Exception as exc:
                log.error("  %s failed: %s", pharmacy.get("name"), exc)
                return None
            return to_record(final, pharmacy) if final else None

    results = await asyncio.gather(*(one(p) for p in pharmacies))
    return [r for r in results if r]


def replay(store_dir: Path, pharmacies: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Rebuild records from saved runs. Free. Use this for all UI work."""
    by_phone = {p["phone"]: p for p in pharmacies}
    records = []
    for path in sorted(glob.glob(str(store_dir / "result_*.json"))):
        final = json.loads(Path(path).read_text())
        extracted = (final.get("result") or {}).get("extracted") or {}
        phones = extracted.get("to_phones") or []
        pharmacy = by_phone.get(phones[0]) if phones else None
        records.append(to_record(final, pharmacy or {"phone": phones[0] if phones else "?"}))
    return records


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="RxFind — pharmacy stock search")
    parser.add_argument("--drug", default="amoxicillin")
    parser.add_argument("--dosage", default="500mg")
    parser.add_argument("--quantity", default="21 capsules")
    parser.add_argument("--region", default="GB")
    parser.add_argument("--pharmacies", type=Path, default=Path("pharmacies.csv"))
    parser.add_argument("--max-calls", type=int, default=2)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--replay", action="store_true",
                        help="Rebuild from saved runs. Spends nothing.")
    parser.add_argument("--live", action="store_true",
                        help="ACTUALLY PLACE CALLS. Without this, nothing is dialled.")
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.WARNING)

    pharmacies = load_pharmacies(args.pharmacies) if args.pharmacies.exists() else []

    if args.replay:
        records = replay(Path("runs"), pharmacies)
    else:
        driver = CalleDriver(
            budget=CallBudget(max_calls=args.max_calls), dry_run=not args.live
        )
        records = asyncio.run(
            search(
                driver,
                drug=args.drug, dosage=args.dosage, quantity=args.quantity,
                pharmacies=pharmacies, region=args.region,
                concurrency=args.concurrency,
            )
        )

    if not records:
        log.info("No results.")
        return

    ranked = rank(records)
    if args.json:
        print(json.dumps(ranked, indent=2, default=str))
    else:
        print(f"\n{args.drug} {args.dosage} — {args.quantity}\n")
        print(render(ranked))


if __name__ == "__main__":
    main()
