# ---------------------------------------------------------------------------
# Chat search: the pure half. Open WebUI keeps a chat's messages inside the
# `chat` JSON column (history.messages keyed by id, or a flat messages list on
# older rows); these helpers turn that into turns, rank chats against a
# query, and render results. No SDK or database imports here so the
# head-slice suites reach them; the sqlite side lives below the SDK import.
# ---------------------------------------------------------------------------

_CHAT_SEARCH_MAX_ROWS = 2000


def _db_is_sqlite(database_url: str) -> bool:
    """Open WebUI's DATABASE_URL: empty means its default sqlite file; a
    sqlite:// URL is fine too; anything else (postgres, mysql) has no
    `webui.db` to open and the chat tools must say so instead of tracing."""
    url = (database_url or "").strip().lower()
    return not url or url.startswith("sqlite")
_CHAT_SEARCH_DEFAULT_LIMIT = 8
_CHAT_READ_MAX_CHARS = 40_000


def _chat_text(content: Any) -> str:
    if isinstance(content, list):
        return " ".join(
            str(p.get("text") or "") for p in content if isinstance(p, dict)
        ).strip()
    return str(content or "").strip()


def _chat_turns(blob: Any) -> List[Tuple[str, str]]:
    """(role, text) pairs in conversation order. Ordered by timestamp rather
    than by walking childrenIds: branches are rare and a flat order never
    drops a message."""
    if isinstance(blob, str):
        try:
            blob = json.loads(blob)
        except ValueError:
            return []
    if not isinstance(blob, dict):
        return []
    msgs = (blob.get("history") or {}).get("messages")
    if isinstance(msgs, dict) and msgs:
        items = sorted(
            (m for m in msgs.values() if isinstance(m, dict)),
            key=lambda m: (m.get("timestamp") or 0),
        )
    else:
        items = [m for m in (blob.get("messages") or []) if isinstance(m, dict)]
    out: List[Tuple[str, str]] = []
    for m in items:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _chat_text(m.get("content"))
        if text:
            out.append((role, text))
    return out


def _chat_transcript(turns: List[Tuple[str, str]], max_chars: int = _CHAT_READ_MAX_CHARS) -> str:
    lines = [f"{'User' if r == 'user' else 'Assistant'}: {t}" for r, t in turns]
    text = "\n\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[… truncated; the chat is longer]"
    return text


def _query_terms(query: str) -> List[str]:
    return [w for w in re.split(r"\W+", query.lower()) if len(w) > 1]


def _term_re(term: str):
    """Word-start match: 'plan' finds 'plans', 'mal' does not find 'small'."""
    return re.compile(r"\b" + re.escape(term))


def _chat_snippet(turns: List[Tuple[str, str]], terms: List[str], width: int = 160) -> str:
    for _, text in turns:
        low = text.lower()
        for term in terms:
            m = _term_re(term).search(low)
            i = m.start() if m else -1
            if i >= 0:
                start = max(0, i - width // 3)
                snip = text[start:start + width].replace("\n", " ")
                return ("…" if start else "") + snip + ("…" if start + width < len(text) else "")
    return ""


def _score_chat(title: str, turns: List[Tuple[str, str]], terms: List[str]) -> int:
    """Title hits weigh most, then how many distinct terms appear anywhere,
    then raw frequency capped so one long chat cannot swamp the rest."""
    if not terms:
        return 0
    title_low = (title or "").lower()
    body_low = "\n".join(t for _, t in turns).lower()
    res = [_term_re(t) for t in terms]
    in_title = [bool(r.search(title_low)) for r in res]
    counts = [len(r.findall(body_low)) for r in res]
    distinct = sum(1 for hit, n in zip(in_title, counts) if hit or n)
    if distinct == 0:
        return 0
    freq = sum(min(n, 3) for n in counts)
    phrase = 0
    if len(terms) > 1:
        phrase_re = re.compile(r"\b" + r"\W+".join(re.escape(t) for t in terms))
        phrase = 15 if phrase_re.search(title_low) or phrase_re.search(body_low) else 0
    return distinct * 10 + sum(in_title) * 5 + freq + phrase


def _search_chat_rows(
    rows: Any,
    query: str,
    limit: int = _CHAT_SEARCH_DEFAULT_LIMIT,
    exclude_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """rows: iterable of (id, title, updated_at, archived, chat_blob). Returns
    the best `limit` chats, highest score first, ties newest first."""
    terms = _query_terms(query)
    hits: List[Dict[str, Any]] = []
    for cid, title, updated_at, archived, blob in rows:
        if exclude_id and cid == exclude_id:
            continue
        turns = _chat_turns(blob)
        score = _score_chat(title or "", turns, terms)
        if score <= 0:
            continue
        hits.append(
            {
                "id": cid,
                "title": title or "(untitled)",
                "updated_at": int(updated_at or 0),
                "archived": bool(archived),
                "score": score,
                "turns": len(turns),
                "snippet": _chat_snippet(turns, terms),
            }
        )
    hits.sort(key=lambda h: (-h["score"], -h["updated_at"]))
    return hits[: max(1, int(limit or _CHAT_SEARCH_DEFAULT_LIMIT))]


def _format_chat_hits(hits: List[Dict[str, Any]], query: str) -> str:
    if not hits:
        return f"No earlier chats match {query!r}. Try fewer or different words."
    lines = [f"{len(hits)} earlier chat(s) matching {query!r}, best first:"]
    for h in hits:
        when = time.strftime("%Y-%m-%d", time.localtime(h["updated_at"])) if h["updated_at"] else "?"
        flag = " · archived" if h.get("archived") else ""
        lines.append(f"- {when} · **{h['title']}** · {h['turns']} turns{flag} · chat_id={h['id']}")
        if h["snippet"]:
            lines.append(f"  {h['snippet']}")
    lines.append("Call read_chat with a chat_id to read one in full.")
    return "\n".join(lines)


