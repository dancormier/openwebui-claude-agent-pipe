_COLD_WARN_TTL_SECONDS = 30 * 60

# Base input $/MTok. A resume after the 1-hour cache TTL rewrites the whole
# context as a cache write, billed at 2x base input. Unknown models get no
# figure rather than a guessed one.
_INPUT_USD_PER_MTOK = {
    "claude-opus-5-5": 4,
    "claude-opus-5": 5,
    "claude-fable-5-1": 10,
    "claude-sonnet-5": 2,
    "claude-haiku-4-5": 1,
}

_RESUME_PREFIX_RX = re.compile(r"/resume(?:\s+|$)", re.IGNORECASE)
_COLD_ACK_RX = re.compile(r"(?:continue|go|yes|ok)[.!]*", re.IGNORECASE)


def _extract_resume_prefix(prompt: str) -> Tuple[bool, str]:
    """`/resume` at the very start of a message skips the cold-resume guard
    for that turn. Returns (present, prompt without the prefix)."""
    stripped = prompt.lstrip()
    m = _RESUME_PREFIX_RX.match(stripped)
    if not m:
        return False, prompt
    return True, stripped[m.end():]


def _is_cold_ack(prompt: str) -> bool:
    return bool(_COLD_ACK_RX.fullmatch(prompt.strip()))


def _cold_warning_pending(meta: Dict[str, Any], now: float) -> bool:
    warned_at = meta.get("cold_warned_at")
    if not isinstance(warned_at, (int, float)):
        return False
    return 0 <= now - warned_at <= _COLD_WARN_TTL_SECONDS


def _is_cold_and_large(
    meta: Dict[str, Any], now: float, idle_minutes: int, min_tokens: int
) -> bool:
    last_turn_at = meta.get("last_turn_at")
    tokens = meta.get("last_context_tokens")
    if not isinstance(last_turn_at, (int, float)) or not isinstance(tokens, int):
        return False
    return now - last_turn_at > idle_minutes * 60 and tokens >= min_tokens


def _cold_resume_cost(model: str, tokens: int) -> Optional[float]:
    # Model ids arrive as `claude-opus-5-5[1m]` or with a date suffix.
    key = re.sub(r"\[.*?\]$", "", (model or "").strip().lower())
    key = re.sub(r"-\d{8}$", "", key)
    price = _INPUT_USD_PER_MTOK.get(key)
    if price is None:
        return None
    return tokens * price * 2 / 1e6


def _code_fence(text: str) -> str:
    """A fence longer than any backtick run inside, so a pasted message that
    contains ``` cannot close the block early."""
    longest = max((len(r) for r in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"


def _cold_resume_warning(
    chat_id: str,
    tokens: int,
    idle_seconds: float,
    model: str,
    base_url: str,
    message: str,
) -> str:
    cost = _cold_resume_cost(model, tokens)
    price = f" (about ${cost:.2f} on {model})" if cost is not None else ""
    ref = f"chat {chat_id}"
    if base_url:
        ref += f" ({base_url.rstrip('/')}/c/{chat_id})"
    pickup = f"Continuing from {ref} — read it for context first. {message.strip()}"
    return (
        f"**This chat has gone cold.** It holds about {_fmt_tokens(tokens)} "
        f"tokens of context and has been idle "
        f"{_fmt_duration(int(idle_seconds * 1000))}, so the prompt cache has "
        "expired: continuing here re-processes the whole context at "
        f"cache-write price{price}.\n\n"
        "To start fresh instead, paste this into a new chat:\n\n"
        f"{_code_fence(pickup)}\n\n"
        "Reply `continue` to send your message here anyway, or send anything "
        "else to carry on in this chat.\n"
    )
