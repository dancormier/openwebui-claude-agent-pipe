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


_RATE_LIMIT_KEYS = {"five_hour": "session", "seven_day": "weekly", "overage": "extra_usage"}


def _model_short_name(model_id: Optional[str]) -> str:
    """`claude-fable-5-1` → `fable`: the family name is what the usage widget
    labels a model window with, and the version digits would only churn the
    popover key between releases."""
    parts = (model_id or "").strip().split("-")
    if len(parts) >= 2 and parts[0] == "claude" and parts[1]:
        return parts[1]
    # A bare alias (`opus`) must keep its own key, or two aliased models
    # would overwrite each other's numbers under one shared name.
    alias = re.sub(r"[^a-z0-9]+", "", (model_id or "").lower())
    return alias or "model"


def _fmt_resets_in(resets_at: int, now: Optional[float] = None) -> str:
    now = time.time() if now is None else now
    left = int(resets_at - now)
    if left < 60:
        return "<1m"
    d, rem = divmod(left, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return " ".join(f"{n}{unit}" for n, unit in ((d, "d"), (h, "h"), (m, "m")) if n)


def _note_rate_limit(
    cache: Dict[str, Dict[str, Any]],
    raw: Optional[Dict[str, Any]],
    model_key: str = "model",
) -> None:
    """Fold one rate_limit_event into the cache, keyed by popover name. The
    CLI sends the event only when a window's state changes, so the latest per
    window is kept for every later turn in this process. `unifiedWindows`
    carries the session and weekly windows on every event; the top-level
    fields are the window the CLI considers representative, which for a
    `seven_day_<model>` type is the model's own weekly window — the event
    does not name the model, so the caller passes the turn's."""
    if not isinstance(raw, dict):
        return
    windows = raw.get("unifiedWindows")
    if isinstance(windows, dict):
        for rl_type, key in (("five_hour", "session"), ("seven_day", "weekly")):
            w = windows.get(rl_type)
            if isinstance(w, dict):
                _merge_window(cache, key, w.get("utilization"), w.get("resetsAt"))
    rl_type = raw.get("rateLimitType")
    if isinstance(rl_type, str) and rl_type:
        if rl_type in _RATE_LIMIT_KEYS:
            key = _RATE_LIMIT_KEYS[rl_type]
        elif rl_type.startswith("seven_day_"):
            key = model_key
        else:
            key = rl_type
        # The top level can name a window without repeating its figures (a
        # five_hour claim with only resetsAt); never blank what unifiedWindows
        # just filled in.
        _merge_window(cache, key, raw.get("utilization"), raw.get("resetsAt"))
    if raw.get("isUsingOverage") is True:
        cache.setdefault("extra_usage", {})["in_use"] = True
    elif raw.get("isUsingOverage") is False and "extra_usage" in cache:
        cache["extra_usage"].pop("in_use", None)


def _merge_window(
    cache: Dict[str, Dict[str, Any]], key: str,
    utilization: Any, resets_at: Any,
) -> None:
    """Only overwrite with figures the event actually carries: a window
    claim can name itself with just resetsAt and must not blank a known
    utilization."""
    fresh = {
        k: v for k, v in (("utilization", utilization), ("resets_at", resets_at))
        if v is not None
    }
    if fresh:
        cache.setdefault(key, {}).update(fresh)


def _usage_limits(
    cache: Dict[str, Dict[str, Any]], now: Optional[float] = None
) -> Dict[str, Any]:
    now = time.time() if now is None else now
    out: Dict[str, Any] = {}
    for key, info in list(cache.items()):
        # A window past its reset has no fresh event behind it yet; showing
        # the old figure would be wrong, so say nothing until the CLI does.
        if info.get("resets_at") and int(info["resets_at"]) < now:
            del cache[key]
    ordered = ["session", "weekly"]
    ordered += [k for k in cache if k not in ("session", "weekly", "extra_usage")]
    ordered.append("extra_usage")
    for key in ordered:
        info = cache.get(key)
        if not info:
            continue
        if info.get("utilization") is not None:
            out[f"{key}_used"] = round(100 * float(info["utilization"]))
        if info.get("resets_at"):
            out[f"{key}_resets_in"] = _fmt_resets_in(int(info["resets_at"]), now)
        if info.get("in_use"):
            out[f"{key}_in_use"] = True
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


