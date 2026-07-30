"""
calle_driver — a Python driver for the CALL-E MCP surface.

Domain-agnostic. Knows nothing about pharmacies. This is the piece intended for
contribution back to CALLE-AI/awesome-phone-call-agents.

The CALL-E MCP flow is a state machine, not a request/response:

    plan_call     -> ready_to_run + confirm_token   (free, no call placed)
    run_call      -> run_id                          (spends len(to_phones))
    get_call_run  -> poll until terminal, guided by next_step.action

What this module handles that a naive client won't:

  * Budget: `to_phones` is an array, so ONE plan can spend N calls. Charged
    per phone, with a persistent ledger.
  * confirm_token expiry (~24h) checked before spending anything.
  * next_step is a structured object on run/get_call_run but a plain STRING on
    plan_call. Type-guarded.
  * Activity feed is cumulative on every poll — deduped.
  * Extraction lands in `result.summary` as a key=value STRING, not in
    `result.extracted` (which carries a request echo). See parse_summary_fields.

    pip install fastmcp
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

log = logging.getLogger("calle")

SERVER_URL = "https://seleven-mcp-sg.airudder.com/mcp/openagent_oauth"
TOKEN_CACHE_ROOT = Path.home() / ".calle-mcp" / "cli"

TERMINAL_STATUSES = {"COMPLETED", "NO ANSWER", "DECLINED", "FAILED", "CANCELLED"}
TERMINAL_ACTIONS = {"report_result", "report_blocked", "none"}
BLOCKED_ACTIONS = {"ask_user_for_missing_info", "ask_user_for_retry_confirmation"}


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


def load_cli_token() -> str:
    """Reuse the token the `calle` CLI already cached (`calle auth login`).

    The token file shape isn't documented, so probe common key names.
    """
    candidates = sorted(TOKEN_CACHE_ROOT.glob("*/token.json"))
    if not candidates:
        raise RuntimeError(
            f"No CALL-E token cache under {TOKEN_CACHE_ROOT}. Run: calle auth login"
        )
    data = json.loads(candidates[-1].read_text())
    for key in ("access_token", "accessToken", "token", "bearer"):
        value = data.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for k2 in ("access_token", "accessToken", "token"):
                if isinstance(value.get(k2), str):
                    return value[k2]
    raise RuntimeError(f"No access token in {candidates[-1]}. Keys: {list(data)}")


# --------------------------------------------------------------------------
# Budget
# --------------------------------------------------------------------------


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class CallBudget:
    """Hard ceiling on outbound calls, persisted across processes.

    Free tier is 20 calls. `to_phones` being an array means a single careless
    command can spend all of them, and nothing in `confirm_summary` states the
    cost. Reserve before dialling, refund if the dial never happened.
    """

    max_calls: int
    spent: int = 0
    ledger_path: Path = field(default=Path(".rxfind_budget.json"))

    def __post_init__(self) -> None:
        if self.ledger_path.exists():
            self.spent = json.loads(self.ledger_path.read_text()).get("spent", 0)

    @property
    def remaining(self) -> int:
        return max(0, self.max_calls - self.spent)

    def _flush(self) -> None:
        self.ledger_path.write_text(json.dumps({"spent": self.spent}))

    def check_available(self, n: int) -> None:
        """Fail before anything is dialled, not halfway through a batch."""
        if n > self.remaining:
            raise BudgetExceeded(
                f"{n} call(s) needed but only {self.remaining} left of "
                f"{self.max_calls}. Trim the list or raise max_calls deliberately."
            )

    def reserve(self, n: int) -> None:
        self.check_available(n)
        self.spent += n
        self._flush()
        log.warning(
            "RESERVED %s call(s) — %s/%s spent, %s remaining",
            n, self.spent, self.max_calls, self.remaining,
        )

    def refund(self, n: int) -> None:
        self.spent = max(0, self.spent - n)
        self._flush()
        log.info("Refunded %s unspent call(s) — %s remaining", n, self.remaining)


# --------------------------------------------------------------------------
# Summary parsing
# --------------------------------------------------------------------------


def parse_summary_fields(summary: str, keys: Iterable[str]) -> dict[str, str]:
    """Pull `key=value` pairs out of `result.summary`.

    This is where CALL-E's extraction actually lands. `result.extracted` holds
    an echo of the request (goal, region, to_phones, calling metadata) rather
    than the conversational fields, so the summary string is the real source.

    Values contain commas — free-text notes especially — so split on `key=`
    boundaries using the known field names as delimiters, never on commas.
    """
    if not summary:
        return {}
    ordered = sorted(set(keys), key=len, reverse=True)  # unit_price before price
    pattern = re.compile(r"\b(" + "|".join(map(re.escape, ordered)) + r")\s*=\s*", re.I)
    matches = list(pattern.finditer(summary))

    out: dict[str, str] = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(summary)
        # The separator between fields is not stable — observed both ", " and
        # "; " across runs. Strip any of them rather than assuming one.
        value = summary[m.end():end].strip().strip(",;").strip()
        if len(value) < 40:
            value = value.rstrip(".;,").strip()
        out[m.group(1).lower()] = value
    return out


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------


class CalleDriver:
    def __init__(
        self,
        budget: CallBudget,
        *,
        dry_run: bool = True,
        store_dir: Path = Path("runs"),
        server_url: str = SERVER_URL,
    ) -> None:
        self.budget = budget
        self.dry_run = dry_run
        self.store_dir = store_dir
        self.store_dir.mkdir(exist_ok=True)
        self._server_url = server_url
        self._token = load_cli_token()

    def _client(self) -> Client:
        # A fresh client per operation — sessions are cheap and this keeps
        # concurrent runs from sharing transport state.
        return Client(
            StreamableHttpTransport(
                self._server_url, headers={"Authorization": f"Bearer {self._token}"}
            )
        )

    @staticmethod
    def _unwrap(result: Any) -> dict[str, Any]:
        if getattr(result, "structured_content", None):
            return result.structured_content
        if getattr(result, "data", None):
            return result.data
        blocks = getattr(result, "content", None) or []
        if blocks and hasattr(blocks[0], "text"):
            return json.loads(blocks[0].text)
        return {}

    def _persist(self, name: str, payload: dict[str, Any]) -> Path:
        path = self.store_dir / f"{name}_{int(time.time() * 1000)}.json"
        path.write_text(json.dumps(payload, indent=2, default=str))
        return path

    # -- steps ------------------------------------------------------------

    async def plan(
        self,
        *,
        goal: str,
        to_phones: list[str],
        region: str,
        language: str = "English",
        user_input: str | None = None,
        plan_id: str | None = None,
        ttl_seconds: int = 0,  # 0 = retain permanently; useful as demo evidence
    ) -> dict[str, Any]:
        """Free. No call is placed. Iterate on goal prose as much as you like."""
        args: dict[str, Any] = {
            "goal": goal,
            "to_phones": to_phones,
            "region": region,
            "language": language,
            "ttl_seconds": ttl_seconds,
            "user_input": user_input or goal,
        }
        if plan_id:
            args["plan_id"] = plan_id

        async with self._client() as c:
            res = self._unwrap(await c.call_tool("plan_call", args))
        self._persist("plan", res)

        if not res.get("ready_to_run"):
            log.warning(
                "Plan not ready — %s",
                res.get("clarifying_questions") or res.get("questions"),
            )
        return res

    @staticmethod
    def _assert_token_fresh(confirm_expires_at: str | None) -> None:
        if not confirm_expires_at:
            return
        exp = datetime.fromisoformat(confirm_expires_at.replace("Z", "+00:00"))
        if exp < datetime.now(timezone.utc):
            raise RuntimeError(
                f"confirm_token expired at {confirm_expires_at}. Re-plan first."
            )

    async def run(self, plan: dict[str, Any], n_phones: int) -> dict[str, Any]:
        """Spends n_phones. plan_id is the idempotency key — never run twice."""
        self._assert_token_fresh(plan.get("confirm_expires_at"))

        if self.dry_run:
            log.info(
                "DRY RUN — plan %s would place %s call(s), %s remaining",
                plan.get("plan_id"), n_phones, self.budget.remaining,
            )
            return {"status": "DRY_RUN", "run_id": None}

        self.budget.reserve(n_phones)
        try:
            async with self._client() as c:
                res = self._unwrap(
                    await c.call_tool(
                        "run_call",
                        {
                            "plan_id": plan["plan_id"],
                            "confirm_token": plan["confirm_token"],
                        },
                    )
                )
        except Exception:
            self.budget.refund(n_phones)  # never dialled
            raise

        if not res.get("run_id"):
            self.budget.refund(n_phones)
        self._persist("run", res)
        return res

    async def poll(self, run_id: str, *, timeout: float = 900.0) -> dict[str, Any]:
        deadline = time.time() + timeout
        delay = 2.0
        seen: set[str] = set()  # activity feed is cumulative on every poll

        async with self._client() as c:
            while time.time() < deadline:
                res = self._unwrap(await c.call_tool("get_call_run", {"run_id": run_id}))
                status = (res.get("status") or "").upper()

                nxt = res.get("next_step")
                nxt = nxt if isinstance(nxt, dict) else {}  # string on plan_call
                action = nxt.get("action")

                for entry in res.get("activity", []):
                    key = f"{entry.get('ts')}|{entry.get('message')}"
                    if key not in seen:
                        seen.add(key)
                        log.info("  [%s] %s", entry.get("kind"), entry.get("message"))

                if status in TERMINAL_STATUSES or action in TERMINAL_ACTIONS:
                    self._persist("result", res)
                    return res

                if action in BLOCKED_ACTIONS:
                    self._persist("blocked", res)
                    log.warning("Human input needed: %s", nxt.get("instruction"))
                    return res

                delay = float(nxt.get("poll_after_seconds") or delay)
                await asyncio.sleep(min(delay, 15.0))

        raise TimeoutError(f"Run {run_id} did not settle within {timeout}s")

    async def execute(
        self, *, goal: str, phone: str, region: str, language: str = "English",
    ) -> dict[str, Any] | None:
        """plan -> run -> poll for a single recipient."""
        plan = await self.plan(
            goal=goal, to_phones=[phone], region=region, language=language
        )
        if not plan.get("ready_to_run"):
            return None
        run = await self.run(plan, 1)
        if not run.get("run_id"):
            return None
        return await self.poll(run["run_id"])
