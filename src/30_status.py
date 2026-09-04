def _fmt_tokens(n: int) -> str:
    if n < 1_000:
        return str(n)
    if n < 1_000_000:
        return f"{round(n / 1_000)}k"
    s = f"{n / 1_000_000:.2f}".rstrip("0").rstrip(".")
    return f"{s}M"


def _context_tokens(usage: Optional[Dict[str, Any]]) -> int:
    """Context size implied by one API call's usage: the input-side fields
    are the entire prompt (system + history + tool results), and adding
    output_tokens gives the session context after the reply."""
    if not usage:
        return 0
    return sum(
        int(usage.get(k) or 0)
        for k in (
            "input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "output_tokens",
        )
    )


def _context_status(usage: Optional[Dict[str, Any]], window: int) -> str:
    """Render '74k/200k (37%)', or '' when unknown/disabled."""
    used = _context_tokens(usage)
    if not used or window <= 0:
        return ""
    pct = round(100 * used / window)
    return f"{_fmt_tokens(used)}/{_fmt_tokens(window)} ({pct}%)"


def _owui_usage(
    usage: Dict[str, Any],
    duration_ms: Optional[int] = None,
    num_turns: Optional[int] = None,
    limits: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """OWUI-normalized usage dict from one API call's usage block. The token
    keys stay first: the ⓘ popover dumps the dict in insertion order."""
    out_tok = int(usage.get("output_tokens") or 0)
    total = _context_tokens(usage)
    out: Dict[str, Any] = {
        "prompt_tokens": total - out_tok,
        "completion_tokens": out_tok,
        "total_tokens": total,
        "cache_read_input_tokens": int(
            usage.get("cache_read_input_tokens") or 0
        ),
        "cache_creation_input_tokens": int(
            usage.get("cache_creation_input_tokens") or 0
        ),
    }
    if duration_ms:
        out["duration"] = _fmt_duration(duration_ms)
        out["duration_ms"] = int(duration_ms)
    if num_turns is not None:
        out["num_turns"] = int(num_turns)
    if limits:
        out.update(limits)
    return out


def _fmt_duration(ms: int) -> str:
    s = max(0, round(ms / 1000))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


_RATE_LIMIT_KEYS = {"five_hour": "session", "seven_day": "weekly"}


def _rate_limit_key(rate_limit_type: str) -> str:
    """Popover-friendly name for an SDK rate_limit_type: the session and
    weekly windows get plain words, model windows (seven_day_opus) keep just
    the model, anything unrecognised passes through as-is."""
    if rate_limit_type in _RATE_LIMIT_KEYS:
        return _RATE_LIMIT_KEYS[rate_limit_type]
    if rate_limit_type.startswith("seven_day_"):
        return rate_limit_type[len("seven_day_"):]
    return rate_limit_type


def _fmt_reset(ts: int, now: Optional[float] = None) -> str:
    """Local wall-clock of a reset, with the weekday only when it is not today."""
    now = time.time() if now is None else now
    at, today = time.localtime(ts), time.localtime(now)
    if at[:3] == today[:3]:
        return time.strftime("%H:%M", at)
    return time.strftime("%a %H:%M", at)


def _note_rate_limit(
    cache: Dict[str, Dict[str, Any]],
    rate_limit_type: Optional[str],
    utilization: Optional[float],
    resets_at: Optional[int],
) -> None:
    """The CLI sends a RateLimitEvent only when a window's state changes, so
    the latest one per type is kept for every later turn in this process."""
    if not rate_limit_type:
        return
    cache[rate_limit_type] = {"utilization": utilization, "resets_at": resets_at}


def _usage_limits(
    cache: Dict[str, Dict[str, Any]], now: Optional[float] = None
) -> Dict[str, Any]:
    now = time.time() if now is None else now
    out: Dict[str, Any] = {}
    for rl_type, info in list(cache.items()):
        # A window past its reset has no fresh event behind it yet; showing
        # the old figure would be wrong, so say nothing until the CLI does.
        if info.get("resets_at") and int(info["resets_at"]) < now:
            del cache[rl_type]
            continue
        key = _rate_limit_key(rl_type)
        if info.get("utilization") is not None:
            out[f"{key}_used"] = round(100 * float(info["utilization"]))
        if info.get("resets_at"):
            out[f"{key}_resets"] = _fmt_reset(int(info["resets_at"]), now)
    return out


_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
_EFFORT_PREFIX_RX = re.compile(
    r"/effort[:= ]\s*(low|medium|high|xhigh|max)\b", re.IGNORECASE
)


def _extract_effort_prefix(prompt: str) -> Tuple[Optional[str], str]:
    """`/effort <level>` at the very start of a message overrides the EFFORT
    valve for that turn. Returns (level or None, prompt without the prefix)."""
    stripped = prompt.lstrip()
    m = _EFFORT_PREFIX_RX.match(stripped)
    if not m:
        return None, prompt
    return m.group(1).lower(), stripped[m.end():].lstrip()


