class _InflightTurn:
    """One running turn in a chat, so a newer message can stop it."""

    def __init__(self) -> None:
        self.done = asyncio.Event()
        self.interrupt: Optional[Callable[[], Any]] = None
        self.superseded = False
        self.stopped_previous = False
        # Set by ask_user while its form is pending: a message typed into
        # the chat then resolves this instead of stopping the turn.
        self.pending_ask: Optional["asyncio.Future"] = None
        self.prompt = ""


# chat_id -> the turn currently running there. Open WebUI's native UI blocks
# sending while a reply streams, but Conduit and the API do not: without this
# a second message starts a second agent process on the same session, the two
# interleave in one transcript, and a form raised by the abandoned turn has no
# live response to deliver its answer into.
_inflight: Dict[str, _InflightTurn] = {}

_INFLIGHT_WAIT_S = 30.0


async def _claim_chat(
    chat_id: str, wait_s: float = _INFLIGHT_WAIT_S, prompt: str = ""
) -> Tuple[_InflightTurn, bool]:
    """Register the caller as the chat's live turn. A turn already running
    there is asked to stop and given `wait_s` to exit; the flag says whether
    one had to be stopped. `prompt` is the message that started this turn,
    kept so a regenerate of it is not mistaken for a typed answer."""
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
    entry.stopped_previous = superseded
    entry.prompt = prompt.strip()
    _inflight[chat_id] = entry
    return entry, superseded


_OVERLAP_NOTE = (
    "[Gateway note: the previous reply in this chat was still running when "
    "this message arrived and was stopped. The user may have seen it cut off; "
    "whatever it finished is in your session history. If it already answers "
    "what the user is asking now, restate its conclusion briefly instead of "
    "redoing the work.]"
)


def _with_overlap_note(prompt: str) -> str:
    return prompt + "\n\n" + _OVERLAP_NOTE


def _deliver_typed_answer(chat_id: str, text: str) -> bool:
    """Hand a typed message to the form waiting in the chat's live turn.
    False when nothing is waiting, so the caller runs it as a new turn.
    Regenerate and retry re-send the message that started the turn; that
    text is never an answer, it is the old interrupt-and-redo."""
    entry = _inflight.get(chat_id)
    text = text.strip()
    if entry is None or entry.done.is_set() or not text or text == entry.prompt:
        return False
    fut = entry.pending_ask
    if fut is None or fut.done():
        return False
    fut.set_result(text)
    return True


def _release_chat(chat_id: str, entry: _InflightTurn) -> None:
    entry.done.set()
    if _inflight.get(chat_id) is entry:
        del _inflight[chat_id]


