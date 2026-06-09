"""Hard daily cap on the support AI's HubSpot API usage.

HubSpot enforces a daily limit on API requests. To make sure this support
service never burns through the account's quota, every HubSpot HTTP request in
the backend must pass through this budget gate. The support AI is allowed at
most a configurable fraction (default 25%) of the daily provider limit.

The counter is persisted per UTC day in SQLite so the budget is shared across:
- live ticket creation (end of a support session),
- bulk ticket sync + enrichment (the dominant consumer),
- KB sync and dashboard pipeline lookups,
- and the background auto-sync loop,

and it survives process restarts. Each HubSpot request counts as 1 against the
daily limit, matching how HubSpot itself meters usage.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger(__name__)

# HubSpot's daily API request limit for this account. Override if the plan
# changes. The support AI is capped at BUDGET_FRACTION of this.
DEFAULT_PROVIDER_DAILY_LIMIT = 625_000
DEFAULT_BUDGET_FRACTION = 0.25

_lock = threading.Lock()
_warned_today = ""


class HubSpotBudgetExceeded(RuntimeError):
    """Raised when a HubSpot call would exceed the support AI's daily budget."""

    def __init__(self, used: int, cap: int, requested: int = 1) -> None:
        self.used = used
        self.cap = cap
        self.requested = requested
        super().__init__(
            f"HubSpot daily budget reached: {used}/{cap} calls used today "
            f"(requested {requested} more)."
        )


def _is_serverless() -> bool:
    return os.environ.get("SUPPORT_SERVERLESS", "").strip().lower() in ("1", "true", "yes")


def _repo_root() -> Path:
    env = os.environ.get("SUPPORT_REPO_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[3]


def _data_dir() -> Path:
    override = os.environ.get("SUPPORT_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if _is_serverless():
        return Path("/tmp/realtime-support-demo")
    return _repo_root() / "knowledge_support" / "data"


def _db_path() -> Path:
    override = os.environ.get("SUPPORT_HUBSPOT_BUDGET_DB", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return _data_dir() / "hubspot_budget.sqlite"


def daily_cap() -> int:
    """Maximum HubSpot calls the support AI may make in a single UTC day."""
    explicit = os.environ.get("SUPPORT_HUBSPOT_DAILY_CALL_BUDGET", "").strip()
    if explicit:
        try:
            return max(0, int(float(explicit)))
        except ValueError:
            _log.warning("Invalid SUPPORT_HUBSPOT_DAILY_CALL_BUDGET=%r; using default", explicit)

    limit = DEFAULT_PROVIDER_DAILY_LIMIT
    raw_limit = os.environ.get("HUBSPOT_DAILY_API_LIMIT", "").strip()
    if raw_limit:
        try:
            limit = int(float(raw_limit))
        except ValueError:
            pass

    fraction = DEFAULT_BUDGET_FRACTION
    raw_fraction = os.environ.get("SUPPORT_HUBSPOT_BUDGET_FRACTION", "").strip()
    if raw_fraction:
        try:
            fraction = float(raw_fraction)
        except ValueError:
            pass
    fraction = min(max(fraction, 0.0), 1.0)
    return int(limit * fraction)


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS hubspot_daily_usage ("
        " day TEXT PRIMARY KEY,"
        " calls INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    return conn


def usage_today() -> int:
    """Number of HubSpot calls already counted for the current UTC day."""
    try:
        with _lock:
            conn = _connect()
            try:
                row = conn.execute(
                    "SELECT calls FROM hubspot_daily_usage WHERE day = ?",
                    (_today_key(),),
                ).fetchone()
                return int(row[0]) if row else 0
            finally:
                conn.close()
    except Exception as exc:  # never let bookkeeping break a request path
        _log.warning("hubspot_budget usage read failed: %s", exc)
        return 0


def remaining_today() -> int:
    return max(0, daily_cap() - usage_today())


def consume(n: int = 1) -> int:
    """Reserve `n` HubSpot calls against today's budget.

    Atomically increments the daily counter when the call fits under the cap and
    returns the new usage total. Raises HubSpotBudgetExceeded (without
    incrementing) when it would push usage over the cap, so callers can stop
    before actually hitting HubSpot.
    """
    if n <= 0:
        return usage_today()
    cap = daily_cap()
    day = _today_key()
    with _lock:
        try:
            conn = _connect()
        except Exception as exc:
            # If we cannot open the ledger, fail closed: do not allow the call.
            raise HubSpotBudgetExceeded(0, cap, n) from exc
        try:
            row = conn.execute(
                "SELECT calls FROM hubspot_daily_usage WHERE day = ?", (day,)
            ).fetchone()
            used = int(row[0]) if row else 0
            if used + n > cap:
                _maybe_warn(day, used, cap)
                raise HubSpotBudgetExceeded(used, cap, n)
            new_used = used + n
            conn.execute(
                "INSERT INTO hubspot_daily_usage (day, calls) VALUES (?, ?) "
                "ON CONFLICT(day) DO UPDATE SET calls = ?",
                (day, new_used, new_used),
            )
            conn.commit()
            return new_used
        finally:
            conn.close()


def _maybe_warn(day: str, used: int, cap: int) -> None:
    global _warned_today
    if _warned_today != day:
        _warned_today = day
        _log.warning(
            "HubSpot daily budget reached for %s: %s/%s calls used; further "
            "HubSpot calls are blocked until tomorrow (UTC).",
            day,
            used,
            cap,
        )


def snapshot() -> dict[str, object]:
    """Current budget state for status endpoints/dashboards."""
    cap = daily_cap()
    used = usage_today()
    return {
        "day_utc": _today_key(),
        "used": used,
        "cap": cap,
        "remaining": max(0, cap - used),
        "fraction_of_limit": round(used / cap, 4) if cap else 0.0,
        "provider_daily_limit": DEFAULT_PROVIDER_DAILY_LIMIT,
        "exhausted": used >= cap,
    }
