import logging
import time
from datetime import datetime

from config import (
    COST_CAP_USD,
    _HAIKU_INPUT_COST_PER_TOKEN,
    _HAIKU_OUTPUT_COST_PER_TOKEN,
    _WEB_SEARCH_COST_PER_USE,
)

log = logging.getLogger(__name__)

_claude_call_count: int = 0
_estimated_cost_usd: float = 0.0

_THROTTLE_FILE = "/tmp/tour_dates_throttle.txt"
CLAUDE_RATE_LIMIT_BUFFER = 2  # extra seconds of padding after the API's reset timestamp


def _load_throttle() -> float:
    """Read persisted throttle timestamp from disk (survives process restarts)."""
    try:
        with open(_THROTTLE_FILE) as f:
            return float(f.read().strip())
    except Exception:
        return 0.0


def _save_throttle(t: float) -> None:
    try:
        with open(_THROTTLE_FILE, "w") as f:
            f.write(str(t))
    except Exception:
        pass


def _claude_throttle() -> None:
    """Sleep until the API's own rate-limit reset time (persisted across restarts)."""
    next_at = _load_throttle()
    wait = next_at - time.time()
    if wait > 0:
        log.info("Rate limit throttle: waiting %.0fs (from API reset header)...", wait)
        time.sleep(wait)


def _claude_call_done(headers: dict) -> None:
    """Parse rate-limit headers from a successful response and persist the next-call time."""
    reset_str = headers.get("anthropic-ratelimit-input-tokens-reset") or headers.get(
        "anthropic-ratelimit-tokens-reset"
    )
    if reset_str:
        try:
            reset_dt = datetime.fromisoformat(reset_str.replace("Z", "+00:00"))
            reset_epoch = reset_dt.timestamp()
            next_at = reset_epoch + CLAUDE_RATE_LIMIT_BUFFER
            _save_throttle(next_at)
            log.info(
                "Token reset at %s — next call allowed in %.0fs",
                reset_str,
                max(0, next_at - time.time()),
            )
            return
        except Exception:
            pass
    # Fallback if header missing: wait 90s from now
    _save_throttle(time.time() + 90)


def _track_cost(resp_msg) -> None:
    """Update _estimated_cost_usd from a parsed Claude response object."""
    global _estimated_cost_usd
    usage = getattr(resp_msg, "usage", None)
    if usage:
        _estimated_cost_usd += getattr(usage, "input_tokens", 0)  * _HAIKU_INPUT_COST_PER_TOKEN
        _estimated_cost_usd += getattr(usage, "output_tokens", 0) * _HAIKU_OUTPUT_COST_PER_TOKEN
    server_tool_use = getattr(getattr(resp_msg, "usage", None), "server_tool_use", None)
    searches = getattr(server_tool_use, "web_search_requests", 0) if server_tool_use else 0
    _estimated_cost_usd += searches * _WEB_SEARCH_COST_PER_USE
    log.debug("Est. run cost: $%.4f / $%.2f cap", _estimated_cost_usd, COST_CAP_USD)


def _under_cost_cap(label: str) -> bool:
    """Return False (and warn) if the estimated cost has reached COST_CAP_USD."""
    if _estimated_cost_usd >= COST_CAP_USD:
        log.warning(
            "Cost cap $%.2f reached (est. $%.4f) — skipping %s",
            COST_CAP_USD, _estimated_cost_usd, label,
        )
        return False
    return True
