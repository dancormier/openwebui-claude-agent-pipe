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


def _owui_usage(usage: Dict[str, Any]) -> Dict[str, int]:
    """OWUI-normalized usage dict from one API call's usage block."""
    out_tok = int(usage.get("output_tokens") or 0)
    total = _context_tokens(usage)
    return {
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


def _fmt_duration(ms: int) -> str:
    s = max(0, round(ms / 1000))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


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


