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
import base64
import fcntl
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("calle")

SERVER_URL = "https://seleven-mcp-sg.airudder.com/mcp/openagent_oauth"
TOKEN_CACHE_ROOT = Path.home() / ".calle-mcp" / "cli"

# Bump when the run-identity scheme changes. An older ledger's keys won't match
# anything we compute, so unfinished entries must be refused rather than read as
# absent — "absent" means "dial them again".
LEDGER_SCHEMA = 3

TERMINAL_STATUSES = {"COMPLETED", "NO ANSWER", "DECLINED", "FAILED", "CANCELLED"}
TERMINAL_ACTIONS = {"report_result", "report_blocked", "none"}
BLOCKED_ACTIONS = {"ask_user_for_missing_info", "ask_user_for_retry_confirmation"}

# E.164: a plus, a non-zero country code digit, then 7-14 more digits.
E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


class InvalidPhoneNumber(ValueError):
    pass


def validate_e164(phone: str) -> str:
    """Reject anything that isn't E.164 before it can be dialled.

    Documenting the requirement isn't enough — a local-format number silently
    reaching the planner is how the wrong person gets called.
    """
    cleaned = (phone or "").strip().replace(" ", "").replace("-", "")
    if not E164_RE.match(cleaned):
        raise InvalidPhoneNumber(
            f"{phone!r} is not E.164. Expected + followed by country code and "
            f"number, 8-15 digits total, e.g. +15550100. Do not guess a "
            f"country code — ask the user."
        )
    return cleaned


def mask(phone: str) -> str:
    """Never print a full number in a summary or log."""
    return f"…{phone[-4:]}" if len(phone) >= 4 else "…"


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


def _extract_token(data: dict[str, Any]) -> str | None:
    for key in ("access_token", "accessToken", "token", "bearer"):
        value = data.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for k2 in ("access_token", "accessToken", "token"):
                if isinstance(value.get(k2), str):
                    return value[k2]
    return None


def _endpoint_of(data: dict[str, Any]) -> str | None:
    for key in ("server_url", "serverUrl", "endpoint", "audience", "mcp_url"):
        value = data.get(key)
        if isinstance(value, str):
            return value
    return None


def _jwt_claims(token: str) -> dict[str, Any]:
    """Read a JWT payload without verifying it.

    Verification is the server's job — we only want a stable identifier for the
    account. Never used for an authorisation decision.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return {}


def _principal_of(token: str, cache: dict[str, Any]) -> str | None:
    """A stable identity for the authenticated account, or None.

    Deliberately NOT the credential cache directory: CALL-E derives that from
    the server URL, so re-authenticating as a different account on the same
    endpoint reuses it — and an account-A run could be resumed with account-B
    credentials.
    """
    override = os.environ.get("CALLE_ACCOUNT_ID")
    if override:
        return override.strip()
    for key in ("account_id", "accountId", "user_id", "userId", "sub",
                "principal", "email", "account"):
        value = cache.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for inner in ("id", "sub", "email"):
                if isinstance(value.get(inner), str):
                    return value[inner].strip()
    claims = _jwt_claims(token)
    for key in ("sub", "account_id", "uid", "email", "client_id"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _cli_auth_status() -> dict[str, Any] | None:
    """Ask the CALL-E CLI which cache belongs to which endpoint.

    Authoritative, rather than reverse-engineering the cache directory hash.
    """
    try:
        result = subprocess.run(
            ["calle", "auth", "status"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def resolve_credential(server_url: str = SERVER_URL) -> tuple[str, str | None]:
    """Return (access_token, account_namespace) for `server_url`, or refuse.

    A bearer token is issued for one origin. Sending it anywhere else hands a
    working credential to a party it was never meant for — so an unbound cache
    is not "probably fine", it's an unknown recipient. A single cache with no
    endpoint recorded is NOT accepted: "only one" is not evidence it belongs to
    this provider.
    """
    override = os.environ.get("CALLE_TOKEN_CACHE")
    if override:
        path = Path(override).expanduser()
        token = _extract_token(json.loads(path.read_text()))
        if not token:
            raise RuntimeError(f"No access token in {path}")
        return token, _principal_of(token, json.loads(path.read_text()))

    status = _cli_auth_status()
    if status:
        cli_endpoint = status.get("server_url")
        cache_path = status.get("cache_path")
        if cli_endpoint and cli_endpoint != server_url:
            raise RuntimeError(
                f"The CALL-E CLI is authenticated against {cli_endpoint}, but "
                f"this app is configured to call {server_url}. Refusing to send "
                f"that credential to a different origin. Re-run `calle auth "
                f"login` against the intended endpoint, or set "
                f"CALLE_TOKEN_CACHE deliberately."
            )
        if cli_endpoint == server_url and cache_path:
            path = Path(cache_path).expanduser()
            if not status.get("usable", True):
                raise RuntimeError(
                    f"CALL-E CLI reports its cached credential is not usable "
                    f"({path}). Run: calle auth login"
                )
            cache = json.loads(path.read_text())
            token = _extract_token(cache)
            if not token:
                raise RuntimeError(f"No access token in {path}")
            return token, _principal_of(token, cache)

    candidates = sorted(TOKEN_CACHE_ROOT.glob("*/token.json"))
    if not candidates:
        raise RuntimeError(
            f"No CALL-E token cache under {TOKEN_CACHE_ROOT}. Run: calle auth login"
        )

    parsed: list[tuple[Path, dict[str, Any]]] = []
    for path in candidates:
        try:
            parsed.append((path, json.loads(path.read_text())))
        except json.JSONDecodeError:
            log.warning("Skipping unreadable token cache %s", path)

    bound = [(p, d) for p, d in parsed if _endpoint_of(d) == server_url]
    if len(bound) == 1:
        token = _extract_token(bound[0][1])
        if not token:
            raise RuntimeError(f"No access token in {bound[0][0]}")
        return token, _principal_of(token, bound[0][1])
    if len(bound) > 1:
        raise RuntimeError(
            f"{len(bound)} cached tokens claim endpoint {server_url}. Set "
            f"CALLE_TOKEN_CACHE to the intended one."
        )

    raise RuntimeError(
        f"Found {len(parsed)} cached CALL-E credential(s), none of which can be "
        f"bound to {server_url}. `calle auth status` did not report a matching "
        f"endpoint and no cache records one. A bearer token is issued for a "
        f"single origin, so sending one that might belong to a different "
        f"provider is a credential disclosure, not a routing mistake. Run "
        f"`calle auth login` against {server_url}, or set CALLE_TOKEN_CACHE."
    )


def load_cli_token(server_url: str = SERVER_URL) -> str:
    return resolve_credential(server_url)[0]


# --------------------------------------------------------------------------
# Budget
# --------------------------------------------------------------------------


class BudgetExceeded(RuntimeError):
    pass


@contextmanager
def _file_lock(path: Path):
    """Exclusive lock keyed on a sidecar file next to `path`."""
    lock_path = path.with_name(path.name + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _write_private(path: Path, payload: str) -> None:
    """Atomic write, owner-readable only.

    These files hold confirm_tokens and call transcripts. Default umask would
    leave them readable by every account on the machine.
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent or "."), suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@dataclass
class CallBudget:
    """Hard ceiling on outbound calls, enforced across processes.

    Free tier is 20 calls, and `to_phones` being an array means a single
    careless command can spend all of them. The read, the check and the
    increment happen inside one lock: a check-then-write ledger is not a
    ceiling, because two processes read the same `spent`, both pass, and both
    dial.
    """

    max_calls: int
    ledger_path: Path = field(default=Path(".rxfind_budget.json"))

    def _read(self) -> int:
        if not self.ledger_path.exists():
            return 0
        text = self.ledger_path.read_text()
        if not text.strip():
            return 0
        try:
            return int(json.loads(text).get("spent", 0))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise BudgetExceeded(
                f"Budget ledger {self.ledger_path} is unreadable. Refusing to "
                f"dial: assuming zero spent would ignore the ceiling entirely."
            ) from exc

    @property
    def spent(self) -> int:
        with _file_lock(self.ledger_path):
            return self._read()

    @property
    def remaining(self) -> int:
        return max(0, self.max_calls - self.spent)

    def check_available(self, n: int) -> None:
        """Advisory pre-flight so a batch fails before the first call.
        Not the guarantee — see reserve()."""
        remaining = self.remaining
        if n > remaining:
            raise BudgetExceeded(
                f"{n} call(s) needed but only {remaining} left of "
                f"{self.max_calls}. Trim the list or raise max_calls deliberately."
            )

    def reserve(self, n: int) -> int:
        """Atomically claim n calls. This is the ceiling's real enforcement."""
        with _file_lock(self.ledger_path):
            spent = self._read()
            if spent + n > self.max_calls:
                raise BudgetExceeded(
                    f"{n} call(s) needed but only {max(0, self.max_calls - spent)} "
                    f"left of {self.max_calls}."
                )
            spent += n
            _write_private(self.ledger_path, json.dumps({"spent": spent}))
            log.warning("RESERVED %s call(s) — %s/%s spent", n, spent, self.max_calls)
            return spent

    def refund(self, n: int) -> None:
        with _file_lock(self.ledger_path):
            spent = max(0, self._read() - n)
            _write_private(self.ledger_path, json.dumps({"spent": spent}))
            log.info("Refunded %s unspent call(s) — %s spent", n, spent)


# --------------------------------------------------------------------------
# Durable run state
# --------------------------------------------------------------------------


class StateCorrupted(RuntimeError):
    """The run ledger could not be read. Refuse to proceed rather than start
    from a blank slate, which would redial everything in flight."""


class RunLocked(RuntimeError):
    """Another live process holds this recipient."""


class ConfigurationMismatch(RuntimeError):
    """An unresolved call exists under provider configuration we can't
    reconcile against. Neither resumable nor safe to redial."""


class AmbiguousRun(RuntimeError):
    """run_call was sent but no run_id was recorded, so whether the phone rang
    is unknown. Must not be retried automatically."""


@dataclass
class RunStore:
    """Crash-safe, concurrency-safe ledger of in-flight calls.

    Losing this state means redialling a real person, so the failure modes
    matter more than the happy path: locked read-modify-write, atomic writes,
    loud failure on corruption, terminal result recorded before the entry is
    retired, and an ambiguous create that is never retried automatically.

    States: planned -> dialing -> running -> done
    """

    path: Path = field(default=Path(".rxfind_runs.json"))

    # Everything that changes what a call actually is. A plan created under a
    # different endpoint, account or region is not the same pending action.
    server_url: str = SERVER_URL
    principal: str | None = None

    # Keys in flight in THIS process. The file lock stops two processes
    # racing; it does nothing about two coroutines in one process, because
    # they share a pid. Duplicate rows dispatched concurrently would otherwise
    # both claim and both dial.
    _active: set[str] = field(default_factory=set, repr=False)

    @contextmanager
    def _locked(self):
        with _file_lock(self.path):
            yield

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        text = self.path.read_text()
        if not text.strip():
            return {}
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            quarantine = self.path.with_name(
                f"{self.path.name}.corrupt.{int(time.time())}"
            )
            self.path.rename(quarantine)
            raise StateCorrupted(
                f"Run ledger {self.path} is unreadable and has been moved to "
                f"{quarantine}. Refusing to continue: treating a corrupt "
                f"ledger as empty would redial every in-flight recipient."
            ) from exc

        schema = raw.get("schema")
        entries = raw.get("entries")
        if schema == LEDGER_SCHEMA and isinstance(entries, dict):
            return entries

        # Older ledgers keyed entries differently, so their keys won't match
        # anything we compute now — every in-flight call would look absent, and
        # absent means dial again. Migrate only if nothing is pending.
        legacy = entries if isinstance(entries, dict) else raw
        pending = {
            k: v for k, v in legacy.items()
            if isinstance(v, dict) and v.get("state") != "done"
        }
        if pending:
            raise StateCorrupted(
                f"Run ledger {self.path} uses an older identity scheme "
                f"(schema {schema!r}, now {LEDGER_SCHEMA}) and still holds "
                f"{len(pending)} unfinished entr"
                f"{'y' if len(pending) == 1 else 'ies'}. Keys are now namespaced "
                f"by endpoint, account and region, so those entries would not be "
                f"found and their recipients would be dialled again. Let the "
                f"in-flight calls finish, or remove the file deliberately."
            )
        return {}

    def _write(self, entries: dict[str, Any]) -> None:
        # 0600: holds confirm_tokens, which authorise placing a call.
        _write_private(self.path, json.dumps(
            {"schema": LEDGER_SCHEMA, "entries": entries}, indent=2
        ))

    @staticmethod
    def claim_key(phone: str, goal: str) -> str:
        """Exclusion identity: who we are calling, and what about.

        Deliberately free of provider configuration. This answers "is there an
        unresolved call to this person about this thing?", and that must have
        the same answer regardless of endpoint, account or region — otherwise
        changing one makes a live call invisible and permits a second dial.
        """
        return hashlib.sha256(f"{phone}|{goal}".encode()).hexdigest()[:32]

    def attempt_config(self, region: str) -> dict[str, Any]:
        """The provider configuration a call is made under. Nested under the
        claim, compared on resume, never mixed into the claim key."""
        return {
            "endpoint": self.server_url,
            "principal": self.principal,
            "region": region.upper(),
        }

    @staticmethod
    def _alive(pid: int | None) -> bool:
        if not pid or pid == os.getpid():
            return pid == os.getpid()
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError, OSError):
            return False
        return True

    def claim(self, key: str, phone_masked: str, region: str = "") -> dict[str, Any]:
        """Take the exclusion claim for a recipient, or explain why we can't."""
        if key in self._active:
            raise RunLocked(
                f"{phone_masked}: already in flight in this process. Duplicate "
                f"entries for the same recipient and goal are not dialled twice."
            )
        wanted = self.attempt_config(region)
        with self._locked():
            data = self._read()
            entry = data.get(key)

            if entry is None:
                entry = {
                    "state": "planned",
                    "pid": os.getpid(),
                    "phone_masked": phone_masked,
                    # Recorded so a human inspecting a stuck entry can see
                    # which configuration it belongs to.
                    "attempt": wanted,
                    "claimed_at": datetime.now(timezone.utc).isoformat(),
                }
                data[key] = entry
                self._write(data)
                self._active.add(key)
                return entry

            if entry.get("state") == "dialing":
                raise AmbiguousRun(
                    f"{phone_masked}: a call was submitted but no run id was "
                    f"recorded, so whether the phone rang is unknown. Not "
                    f"retrying automatically. Check plan "
                    f"{(entry.get('attempt') or {}).get('plan_id')}, then record "
                    f"the run id or remove "
                    f"this entry to allow a redial."
                )

            stored = entry.get("attempt") or {}
            mismatch = {
                f: (stored.get(f), wanted.get(f))
                for f in ("endpoint", "principal", "region")
                if stored.get(f) != wanted.get(f)
            }
            if mismatch:
                detail = "; ".join(
                    f"{k}: pending={was!r} now={now!r}"
                    for k, (was, now) in mismatch.items()
                )
                raise ConfigurationMismatch(
                    f"{phone_masked}: an unresolved call exists for this "
                    f"recipient and goal, but it was made under a different "
                    f"configuration ({detail}). Not resuming — a plan or run id "
                    f"from one endpoint or account is meaningless under another. "
                    f"Not redialling either — the earlier call may still be live."
                )

            if self.principal is None:
                raise ConfigurationMismatch(
                    f"{phone_masked}: an unresolved call exists, but the "
                    f"authenticated account could not be identified, so it "
                    f"cannot be confirmed as ours. Failing closed. Set "
                    f"CALLE_ACCOUNT_ID, or resolve the entry deliberately."
                )

            owner = entry.get("pid")
            if (
                entry.get("state") in {"planned", "running"}
                and owner != os.getpid()
                and self._alive(owner)
            ):
                raise RunLocked(
                    f"{phone_masked}: held by live process {owner}. "
                    f"Not dialling concurrently."
                )

            entry["pid"] = os.getpid()
            data[key] = entry
            self._write(data)
            self._active.add(key)
            return entry

    def update(self, key: str, **fields: Any) -> None:
        """Update the entry. Attempt-scoped fields nest, they don't flatten."""
        attempt_fields = {
            k: fields.pop(k) for k in list(fields)
            if k in {"plan_id", "confirm_token", "confirm_expires_at", "run_id"}
        }
        with self._locked():
            data = self._read()
            entry = data.setdefault(key, {})
            if attempt_fields:
                entry.setdefault("attempt", {}).update(attempt_fields)
            entry.update(fields)
            entry["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._write(data)

    def mark_done(self, key: str, result_path: str | None = None) -> None:
        """Record completion — only after the terminal result is durable."""
        self.update(key, state="done", result_path=result_path,
                    completed_at=datetime.now(timezone.utc).isoformat())
        self._active.discard(key)

    def prune_done(self) -> int:
        with self._locked():
            data = self._read()
            stale = [k for k, v in data.items() if v.get("state") == "done"]
            for k in stale:
                data.pop(k)
            if stale:
                self._write(data)
            return len(stale)

    def get(self, key: str) -> dict[str, Any]:
        with self._locked():
            return self._read().get(key, {})

    def release(self, key: str) -> None:
        """Drop a claim that never reached `dialing`. Safe: nothing was sent."""
        with self._locked():
            data = self._read()
            entry = data.get(key)
            attempt = (entry or {}).get("attempt") or {}
            if entry and entry.get("state") == "planned" and not attempt.get("run_id"):
                data.pop(key)
                self._write(data)
        self._active.discard(key)


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
        runs: RunStore | None = None,
    ) -> None:
        self.budget = budget
        self.dry_run = dry_run
        self.store_dir = store_dir
        self.store_dir.mkdir(exist_ok=True)
        self._server_url = server_url
        self.runs = runs or RunStore()

        # Credentials and the transport are only touched in live mode. A dry
        # run must not read the token or open a socket — otherwise it leaks the
        # recipient's number and the request context to a third party from a
        # mode the user was told places no calls.
        self._token: str | None = None
        self._Client = None
        self._Transport = None
        if not dry_run:
            from fastmcp import Client
            from fastmcp.client.transports import StreamableHttpTransport

            self._Client = Client
            self._Transport = StreamableHttpTransport
            # Resolving the credential also tells us which account we're acting
            # as, which is part of a call's identity.
            self._token, principal = resolve_credential(self._server_url)
            self.runs.server_url = self._server_url
            self.runs.principal = principal
            if principal is None:
                log.warning(
                    "Could not identify the authenticated account. New calls "
                    "proceed, but an unresolved call cannot later be confirmed "
                    "as ours and will fail closed. Set CALLE_ACCOUNT_ID."
                )

    def _client(self):
        if self._Client is None:
            raise RuntimeError("Driver is in dry-run mode; no client available.")
        # A fresh client per operation — sessions are cheap and this keeps
        # concurrent runs from sharing transport state.
        return self._Client(
            self._Transport(
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
        """Save a response. Owner-only: these hold confirm_tokens and the
        recorded voice of a third party."""
        self.store_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.store_dir / f"{name}_{int(time.time() * 1000)}.json"
        _write_private(path, json.dumps(payload, indent=2, default=str))
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
        """Charges nothing, but DOES contact the server. Not part of dry run."""
        to_phones = [validate_e164(p) for p in to_phones]
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

        if self.dry_run:
            # Offline: print what would be sent, contact nothing. Numbers are
            # masked here too — terminal output ends up in scrollback, CI logs
            # and screen recordings.
            log.info("DRY RUN — would plan_call for %s",
                     ", ".join(mask(p) for p in to_phones))
            return {
                "status": "DRY_RUN",
                "ready_to_run": False,
                "request": {**args, "to_phones": [mask(p) for p in to_phones]},
            }

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
        """plan -> run -> poll for a single recipient, resuming if interrupted.

        A stored run_id is polled rather than re-planned. Without that, an
        interrupted poll causes the next invocation to dial the same recipient
        again.
        """
        phone = validate_e164(phone)

        if self.dry_run:
            await self.plan(goal=goal, to_phones=[phone], region=region,
                            language=language)
            return None

        key = self.runs.claim_key(phone, goal)
        # Atomic claim. Raises RunLocked if another live process holds this
        # recipient, or AmbiguousRun if a previous attempt left a create whose
        # outcome is unknown.
        saved = self.runs.claim(key, mask(phone), region)

        saved_attempt = saved.get("attempt") or {}
        if saved_attempt.get("run_id"):
            log.info("Resuming run %s for %s", saved_attempt["run_id"][:12], mask(phone))
            final = await self.poll(saved_attempt["run_id"])
            self.runs.mark_done(key, self._persist("result", final).name)
            return final

        plan: dict[str, Any] | None = None
        if saved_attempt.get("plan_id"):
            try:
                self._assert_token_fresh(saved_attempt.get("confirm_expires_at"))
                plan = {
                    "plan_id": saved_attempt["plan_id"],
                    "confirm_token": saved_attempt["confirm_token"],
                    "confirm_expires_at": saved_attempt.get("confirm_expires_at"),
                    "ready_to_run": True,
                }
                log.info("Reusing plan %s for %s", saved_attempt["plan_id"], mask(phone))
            except RuntimeError:
                plan = None   # expired token; re-plan below. Nothing was dialled.

        if plan is None:
            plan = await self.plan(
                goal=goal, to_phones=[phone], region=region, language=language
            )
            if not plan.get("ready_to_run"):
                self.runs.release(key)      # nothing was dialled
                return None
            self.runs.update(
                key,
                plan_id=plan["plan_id"],
                confirm_token=plan["confirm_token"],
                confirm_expires_at=plan.get("confirm_expires_at"),
            )

        # Mark "dialing" BEFORE run_call. If the response is lost, the ledger
        # already records that a call may have gone out, and claim() refuses to
        # retry rather than risk a second ring.
        self.runs.update(key, state="dialing")

        run = await self.run(plan, 1)
        run_id = run.get("run_id")
        if not run_id:
            log.error("%s: no run id returned — left ambiguous, will not "
                      "auto-retry", mask(phone))
            return None
        self.runs.update(key, state="running", run_id=run_id)

        final = await self.poll(run_id)
        # Result durable first, ledger retired second. The other order loses
        # both if the process dies in between.
        self.runs.mark_done(key, self._persist("result", final).name)
        return final
