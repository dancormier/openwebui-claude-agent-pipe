# ── Secret redaction ────────────────────────────────────────────────────────
# Everything this pipe emits is persisted in webui.db chat history and synced
# to mobile clients, so a secret that reaches the stream is a secret on disk
# indefinitely. Redaction lives here — at the pipe's own output boundary —
# rather than in any single tool, because the agent runs under
# bypassPermissions and can read a credential through paths nobody enumerated
# in advance (a `cat`, a `sqlite3 SELECT *`, an env dump, a stack trace).
# Guarding individual tools is opt-in and fails open when a new path appears;
# guarding the boundary fails closed regardless of source.
#
# Patterns are prefix-anchored on purpose: a token's fixed prefix plus a
# length floor is specific enough that false positives are rare, and the
# failure mode of over-matching (a redacted non-secret) is far cheaper than
# under-matching (a rotation).
_SECRET_PATTERNS: List[tuple] = [
    ("anthropic-token", re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}")),
    # [_\-] included: modern sk-proj- keys contain separators, so a pure
    # alnum floor of 32 never matched them (audit 2026-08-14).
    ("openai-key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{32,}")),
    ("slack-token", re.compile(r"xox[baprse]-[A-Za-z0-9\-]{10,}")),
    # xapp- (Slack app-level token) is NOT covered by xox[baprse].
    ("slack-app-token", re.compile(r"xapp-[0-9]-[A-Za-z0-9\-]{10,}")),
    ("google-client-secret", re.compile(r"GOCSPX-[A-Za-z0-9_\-]{20,}")),
    ("google-refresh-token", re.compile(r"1//0[A-Za-z0-9_\-]{20,}")),
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("github-pat", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    # 1Password service-account token (the op-secrets loader's own credential).
    ("onepassword-service-token", re.compile(r"ops_[A-Za-z0-9_\-]{20,}")),
    ("google-api-key", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    # The BODY is the secret, not the header. Matching only the header emitted
    # «redacted:private-key» followed by the entire key -- worse than a miss,
    # because the logged hit reads as "handled" and suppresses rotation.
    # Full block first so it consumes the body; the bare header stays as a
    # backstop for a block whose END never arrives.
    ("private-key", re.compile(
        r"-----BEGIN[A-Z ]*PRIVATE KEY-----.*?-----END[A-Z ]*PRIVATE KEY-----",
        re.S)),
    # NOTE: no bare-header pattern. It would replace the header and strand the
    # body, and would also hide the run from _scrub_open_private_key below,
    # which is what actually handles a block whose END never arrives.
]

# A private key spans many chunks, and its body matches no prefix rule in
# _SECRET_TAIL_RX, so the stream redactor needs an explicit hold-until-END.
_PK_BEGIN_RX = re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----")
_PK_END_RX = re.compile(r"-----END[A-Z ]*PRIVATE KEY-----")
# RSA-4096 armor runs ~3.2KB; past this we stop waiting for END and redact.
_MAX_HELD_KEY = 16384


def _scrub_open_private_key(text: str) -> tuple:
    """Redact a BEGIN-with-no-END run through end of `text`.

    Releasing such a run raw is what let key bodies escape one chunk at a time.
    """
    m = _PK_BEGIN_RX.search(text)
    if not m or _PK_END_RX.search(text, m.end()):
        return text, []
    return text[: m.start()] + "«redacted:private-key»", ["private-key"]

# A secret split across two stream chunks defeats a per-chunk regex, so the
# redactor holds back any tail that could still grow into a match. These
# match a *truncated* secret anchored at end-of-buffer; the held tail is
# released once the next chunk proves it harmless.
_SECRET_TAIL_RX = re.compile(
    r"(?:"
    r"s|sk|sk-|sk-a(?:n(?:t(?:-[A-Za-z0-9_\-]*)?)?)?|sk-(?:proj-?)?[A-Za-z0-9]*"
    r"|x|xo|xox|xox[baprse](?:-[A-Za-z0-9\-]*)?|xa|xap|xapp(?:-[A-Za-z0-9\-]*)?"
    r"|G|GO|GOC|GOCS|GOCSP|GOCSPX(?:-[A-Za-z0-9_\-]*)?"
    r"|1|1/|1//|1//0[A-Za-z0-9_\-]*"
    r"|g|gh|gh[pousr](?:_[A-Za-z0-9]*)?|github(?:_(?:p(?:a(?:t(?:_[A-Za-z0-9_]*)?)?)?)?)?"
    r"|A|AK|AKI|AKIA[0-9A-Z]*|AI|AIz|AIza[0-9A-Za-z_\-]*"
    r"|e|ey|eyJ[A-Za-z0-9_\-]*(?:\.[A-Za-z0-9_\-]*){0,2}"
    r"|o|op|ops(?:_[A-Za-z0-9_\-]*)?"
    r"|-{1,5}(?:BEGIN[A-Z ]*)?"
    r")\Z"
)

# Upper bound on the held-back tail. This was 512, which was wrong for JWTs:
# a Google id_token runs ~1-2KB, so its still-growing prefix blew the cap, took
# the release-raw branch below, and streamed out verbatim -- and because no
# COMPLETE pattern ever matched, hits was empty and even the warning log stayed
# silent. Sized for the longest secret we recognise, not the shortest.
_MAX_HELD_TAIL = 4096


def _redact_secrets(text: str) -> tuple:
    """Replace complete secrets in `text`. Returns (clean_text, hit_labels)."""
    if not text:
        return text, []
    hits: List[str] = []
    for label, rx in _SECRET_PATTERNS:
        text, n = rx.subn(f"«redacted:{label}»", text)
        if n:
            hits.extend([label] * n)
    # Complete blocks are gone by now, so any surviving BEGIN is unterminated:
    # a key still streaming, or output cut off mid-armor. Either way the body
    # after it must not be released. Runs last so it cannot mask a full block.
    text, pk_hits = _scrub_open_private_key(text)
    hits.extend(pk_hits)
    return text, hits


class _StreamRedactor:
    """Chunk-boundary-safe secret scrubber for a single response stream.

    `feed()` returns the portion of the stream that is safe to emit now,
    withholding any trailing fragment that could still become a secret once
    more text arrives. `flush()` releases whatever is left at end of stream.
    """

    def __init__(self) -> None:
        self._held = ""
        self.hits: List[str] = []

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        buf = self._held + chunk
        # An unterminated private key holds from BEGIN: its base64 body matches
        # no prefix rule below, so without this the body streams out in pieces.
        pk = _PK_BEGIN_RX.search(buf)
        if pk and not _PK_END_RX.search(buf, pk.end()):
            if len(buf) - pk.start() <= _MAX_HELD_KEY:
                self._held = buf[pk.start() :]
                buf = buf[: pk.start()]
            else:
                buf, pk_hits = _scrub_open_private_key(buf)
                self.hits.extend(pk_hits)
                self._held = ""
            safe, hits = _redact_secrets(buf)
            self.hits.extend(hits)
            return safe
        # Split off the still-growing tail BEFORE redacting. Redacting first
        # would fire on a partially-arrived secret the moment it cleared the
        # pattern's length floor, releasing the rest of it verbatim as the
        # remaining characters streamed in.
        tail = _SECRET_TAIL_RX.search(buf)
        if tail and len(buf) - tail.start() <= _MAX_HELD_TAIL:
            self._held = buf[tail.start() :]
            buf = buf[: tail.start()]
        elif tail:
            # Over cap. Releasing raw is exactly how an oversized JWT escaped:
            # no complete pattern matches a still-arriving secret, so nothing
            # redacts it and nothing logs it. A run this long that still looks
            # like a secret prefix gets redacted instead -- consistent with the
            # module's rule that over-matching is far cheaper than a rotation.
            buf = buf[: tail.start()] + "«redacted:truncated»"
            self.hits.append("truncated")
            self._held = ""
        else:
            self._held = ""
        safe, hits = _redact_secrets(buf)
        self.hits.extend(hits)
        return safe

    def flush(self) -> str:
        # A stream can end mid-key (truncated output, killed tool). Scrub the
        # open BEGIN run first, or flush would release the held body verbatim.
        pending, pk_hits = _scrub_open_private_key(self._held)
        self.hits.extend(pk_hits)
        out, hits = _redact_secrets(pending)
        self.hits.extend(hits)
        self._held = ""
        return out


def _redact_event(value: Any, hits: List[str]) -> Any:
    """Recursively scrub secrets from an event payload.

    Status descriptions carry tool previews and error text, so they leak the
    same way response text does. Events arrive whole rather than streamed, so
    no held-tail machinery is needed here.
    """
    if isinstance(value, str):
        clean, found = _redact_secrets(value)
        hits.extend(found)
        return clean
    if isinstance(value, dict):
        return {k: _redact_event(v, hits) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_event(v, hits) for v in value]
    return value


def _redacting_emitter(
    emitter: Optional[Callable], hits: List[str]
) -> Optional[Callable]:
    """Wrap an OpenWebUI event emitter so every payload is scrubbed first."""
    if emitter is None:
        return None

    async def _emit(event: Any) -> Any:
        return await emitter(_redact_event(event, hits))

    return _emit


