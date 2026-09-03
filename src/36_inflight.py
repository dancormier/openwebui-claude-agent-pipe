class _InflightTurn:
    """One running turn in a chat, so a newer message can stop it."""

    def __init__(self) -> None:
        self.done = asyncio.Event()
        self.interrupt: Optional[Callable[[], Any]] = None
        self.superseded = False


# chat_id -> the turn currently running there. Open WebUI's native UI blocks
# sending while a reply streams, but Conduit and the API do not: without this
# a second message starts a second agent process on the same session, the two
# interleave in one transcript, and a form raised by the abandoned turn has no
# live response to deliver its answer into.
_inflight: Dict[str, _InflightTurn] = {}

_INFLIGHT_WAIT_S = 30.0


async def _claim_chat(
    chat_id: str, wait_s: float = _INFLIGHT_WAIT_S
) -> Tuple[_InflightTurn, bool]:
    """Register the caller as the chat's live turn. A turn already running
    there is asked to stop and given `wait_s` to exit; the flag says whether
    one had to be stopped."""
    prev = _inflight.get(chat_id)
    superseded = False
    if prev is not None and not prev.done.is_set():
        prev.superseded = True
        superseded = True
        if prev.interrupt is not None:
            try:
                await prev.interrupt()
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "interrupt of the running turn failed: %s", exc
                )
        try:
            await asyncio.wait_for(prev.done.wait(), wait_s)
        except asyncio.TimeoutError:
            logging.getLogger(__name__).warning(
                "previous turn in chat %s did not exit within %ss", chat_id, wait_s
            )
    entry = _InflightTurn()
    _inflight[chat_id] = entry
    return entry, superseded


def _release_chat(chat_id: str, entry: _InflightTurn) -> None:
    entry.done.set()
    if _inflight.get(chat_id) is entry:
        del _inflight[chat_id]


