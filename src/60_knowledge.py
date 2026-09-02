def _knowledge_collections(
    metadata: Optional[Dict[str, Any]],
    files: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, str]]:
    """Extract attached knowledge collections.

    Priority order:
    1. `metadata["model"]["info"]["meta"]["knowledge"]` — the authoritative
       Workspace Model config, populated regardless of the `function_calling`
       gate. This is the only source that works when the Workspace Model
       sets `function_calling=native` (which disables OpenWebUI's auto-RAG).
    2. `files` (__files__) — populated by the middleware's auto-RAG branch
       when `function_calling != "native"`. Useful for non-native chats or
       as a fallback.
    """
    out: List[Dict[str, str]] = []
    seen: set = set()

    def _add(coll: Any, name: Any) -> None:
        if not coll:
            return
        cid = str(coll)
        if cid in seen:
            return
        seen.add(cid)
        out.append({"id": cid, "name": str(name or cid)})

    def _consume(item: Dict[str, Any]) -> None:
        """Mirror OpenWebUI's vector-collection naming
        (retrieval/utils.py:get_sources_from_items)."""
        name = item.get("name")
        # Old-style: explicit collection_name(s). Legacy KBs with multiple colls.
        if item.get("collection_name"):
            _add(item["collection_name"], name)
            return
        if item.get("collection_names"):
            for coll in item["collection_names"]:
                _add(coll, name)
            return
        # Modern: collection name derived from the knowledge row's id.
        item_type = item.get("type")
        if item_type == "collection" and item.get("id"):
            _add(item["id"], name)
        elif item_type == "file" and item.get("id"):
            # legacy single-file entries skip the prefix; modern ones prepend "file-".
            coll = item["id"] if item.get("legacy") else f"file-{item['id']}"
            _add(coll, name)

    model_knowledge = ((metadata or {}).get("model") or {}).get("info", {}).get(
        "meta", {}
    ).get("knowledge") or []
    for item in model_knowledge:
        if isinstance(item, dict):
            _consume(item)

    for f in files or []:
        if isinstance(f, dict):
            _consume(f)

    return out


def _knowledge_row_ids(metadata: Optional[Dict[str, Any]]) -> List[str]:
    """Return knowledge-table row ids for the attached Workspace-Model KBs.
    These are the IDs used to look up files via Knowledges.get_files_by_id().
    Only populated for `type: "collection"` entries — single-file entries
    aren't exposed to list/read/grep (search still works for those)."""
    ids: List[str] = []
    model_knowledge = ((metadata or {}).get("model") or {}).get("info", {}).get(
        "meta", {}
    ).get("knowledge") or []
    for item in model_knowledge:
        if (
            isinstance(item, dict)
            and item.get("type") == "collection"
            and item.get("id")
        ):
            ids.append(str(item["id"]))
    return ids


def _build_kb_mcp_server(
    knowledge: List[Dict[str, str]],
    knowledge_row_ids: Optional[List[str]] = None,
    user_dict: Optional[Dict[str, Any]] = None,
    event_emitter: Optional[Callable] = None,
):
    """Return (mcp_config, tool_names) for a knowledge-base search tool Claude
    can invoke, or (None, []) if no KBs are attached.

    Result formatting follows OpenWebUI's native RAG shape (<source id=N ...>
    tags + citation event), so Claude's replies render with inline [N]
    citations and populate the sources side-panel. Access control is
    enforced implicitly: the closure captures only the collection_names
    that OpenWebUI's middleware already filtered by the user's grants.
    """
    if not knowledge:
        return None, []

    collection_names = [k["id"] for k in knowledge]
    display = ", ".join(k["name"] for k in knowledge)

    @tool(
        "search_knowledge",
        (
            f"Search the attached knowledge base(s): {display}. "
            "Call whenever you need internal facts, prior guidance, or "
            "documented product/process details. Reformulate and search "
            "multiple times if the first query misses."
        ),
        {"query": str, "top_k": int},
    )
    async def _search(args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            from open_webui.main import app
            from open_webui.models.users import Users
            from open_webui.retrieval.utils import query_collection
        except Exception as exc:
            return {
                "content": [
                    {"type": "text", "text": f"Knowledge search unavailable: {exc}"}
                ]
            }

        query = str(args.get("query") or "").strip()
        if not query:
            return {
                "content": [
                    {"type": "text", "text": "Empty query — nothing to search."}
                ]
            }
        # Default to OpenWebUI's configured RAG_TOP_K so the tool respects the
        # admin's retrieval setting. Fall back to 5 if unreadable.
        try:
            default_top_k = int(app.state.config.RAG_TOP_K.value)
        except Exception:
            default_top_k = 5
        top_k = int(args.get("top_k") or default_top_k)

        # Resolve a proper UserModel so embedding_function can attribute
        # usage / rate-limits per user (some backends require it).
        user_obj = None
        user_id = (user_dict or {}).get("id")
        if user_id:
            try:
                user_obj = Users.get_user_by_id(user_id)
            except Exception:
                pass

        async def _embed(queries, prefix=None):
            return await app.state.EMBEDDING_FUNCTION(
                queries, prefix=prefix, user=user_obj
            )

        if event_emitter:
            try:
                await event_emitter(
                    {
                        "type": "status",
                        "data": {
                            "description": f"🔎 Searching KB: {query[:80]}",
                            "done": False,
                        },
                    }
                )
            except Exception:
                pass

        try:
            results = await query_collection(
                collection_names=collection_names,
                queries=[query],
                embedding_function=_embed,
                k=top_k,
            )
        except Exception as exc:
            log.exception("KB search failed")
            return {"content": [{"type": "text", "text": f"Search failed: {exc}"}]}

        docs = (results.get("documents") or [[]])[0] or []
        metas = (results.get("metadatas") or [[]])[0] or []
        dists = (results.get("distances") or [[]])[0] or []

        if not docs:
            return {
                "content": [
                    {"type": "text", "text": f"No passages found for: {query!r}"}
                ]
            }

        # Surface citations in OpenWebUI's sources side-panel.
        # Emit one event per document so each source gets its own filename label
        # (a single event with a shared source.name collapses all sources into
        # one label in the UI).
        if event_emitter:
            dist_iter = dists or [None] * len(metas)
            for doc, meta, dist in zip(docs, metas, dist_iter):
                meta = meta or {}
                source_name = (
                    meta.get("name")
                    or meta.get("source")
                    or meta.get("title")
                    or "unknown"
                )
                try:
                    await event_emitter(
                        {
                            "type": "citation",
                            "data": {
                                "document": [doc],
                                "metadata": [
                                    {
                                        "source": source_name,
                                        "file_id": meta.get("file_id", ""),
                                        "relevance_score": (
                                            round(float(dist), 3)
                                            if dist is not None
                                            else None
                                        ),
                                    }
                                ],
                                "source": {"name": source_name},
                            },
                        }
                    )
                except Exception:
                    pass

        # XML <source> tags = OpenWebUI's native RAG format → renders as [N] citations.
        parts = [f"Found {len(docs)} passage(s) for {query!r}:\n"]
        for i, (doc, meta) in enumerate(zip(docs, metas), 1):
            meta = meta or {}
            name = (
                meta.get("source") or meta.get("name") or meta.get("title") or "unknown"
            )
            parts.append(f'<source id="{i}" name="{name}">{doc}</source>')
        parts.append(
            "\nCite these using [1], [2], … in your response. Do not include the XML tags themselves."
        )
        return {"content": [{"type": "text", "text": "\n\n".join(parts)}]}

    # ---------- Agentic helpers: list / read / grep ---------------------------
    # Scoped to `knowledge_row_ids` (type=collection entries only). Single-file
    # KBs (type=file) are searchable but not listable/readable by these tools.
    kb_ids: List[str] = list(knowledge_row_ids or [])

    async def _iter_scoped_files():
        from open_webui.models.knowledge import Knowledges

        for kid in kb_ids:
            try:
                files = Knowledges.get_files_by_id(kid) or []
            except Exception:
                continue
            for f in files:
                yield f

    async def _allowed_file_ids() -> Set[str]:
        return {f.id async for f in _iter_scoped_files()}

    @tool(
        "list_knowledge_documents",
        (
            f"List every document in the attached knowledge base(s): {display}. "
            "Returns file_id, filename, and size for each. Use before "
            "read_knowledge_document or grep_knowledge when you need to know "
            "what's there."
        ),
        {},
    )
    async def _list_docs(args: Dict[str, Any]) -> Dict[str, Any]:  # noqa: ARG001
        if not kb_ids:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "No enumerable knowledge collections attached.",
                    }
                ]
            }
        lines = [f"Documents in {display}:"]
        count = 0
        async for f in _iter_scoped_files():
            content = (f.data or {}).get("content", "") or ""
            lines.append(
                f"- file_id={f.id} · {f.filename} · {len(content) // 1024} KiB · {len(content)} chars"
            )
            count += 1
        if count == 0:
            lines.append("(no files found)")
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    @tool(
        "read_knowledge_document",
        (
            "Read the full content of a knowledge document (or a character "
            "range of it) by file_id. Use to zoom into a doc that "
            "search_knowledge found, or read it top-to-bottom if small. "
            "Omit start_char/end_char to read the whole file. "
            "Each call caps at 40 000 chars; page using start_char/end_char "
            "if the file is larger."
        ),
        {"file_id": str, "start_char": int, "end_char": int},
    )
    async def _read_doc(args: Dict[str, Any]) -> Dict[str, Any]:
        from open_webui.models.files import Files

        file_id = str(args.get("file_id") or "").strip()
        if not file_id:
            return {"content": [{"type": "text", "text": "file_id is required."}]}
        allowed = await _allowed_file_ids()
        if file_id not in allowed:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"file_id {file_id!r} is not in the attached knowledge base(s).",
                    }
                ]
            }

        try:
            file_obj = Files.get_file_by_id(file_id)
        except Exception as exc:
            return {"content": [{"type": "text", "text": f"Lookup failed: {exc}"}]}
        if file_obj is None:
            return {"content": [{"type": "text", "text": "File not found."}]}

        content = (file_obj.data or {}).get("content", "") or ""
        total = len(content)
        start = max(0, int(args.get("start_char") or 0))
        raw_end = args.get("end_char")
        end = total if raw_end in (None, 0) else min(total, max(start, int(raw_end)))
        # Hard cap per call to avoid flooding the context window.
        MAX_CHARS = 40_000
        if end - start > MAX_CHARS:
            end = start + MAX_CHARS
        slice_ = content[start:end]
        header = (
            f"# {file_obj.filename}\n"
            f"_chars {start}..{end} of {total}"
            f"{' (truncated — call again with a higher start_char to continue)' if end < total else ''}_\n\n"
        )
        return {"content": [{"type": "text", "text": header + slice_}]}

    @tool(
        "grep_knowledge",
        (
            "Regex/substring search across knowledge documents. Runs against "
            "pre-extracted plain text in the database (no PDF re-parsing), "
            "so it's fast. Use for exact keywords, product codes, "
            "acronyms where vector search struggles.\n\n"
            "- `pattern` (required): regex or literal string\n"
            "- `file_id` (optional): if set, grep only that file; omit or "
            "leave empty to grep the whole knowledge base\n"
            "- `case_insensitive` (default true)\n"
            "- `max_matches` (default 30)\n\n"
            "Returns each hit with 80 chars of surrounding context, the "
            "source filename, and the char offset — use that offset with "
            "read_knowledge_document to fetch more context."
        ),
        {
            "pattern": str,
            "file_id": str,
            "case_insensitive": bool,
            "max_matches": int,
        },
    )
    async def _grep(args: Dict[str, Any]) -> Dict[str, Any]:
        pattern = str(args.get("pattern") or "")
        if not pattern:
            return {"content": [{"type": "text", "text": "pattern is required."}]}
        flags = re.IGNORECASE if args.get("case_insensitive", True) else 0
        max_matches = int(args.get("max_matches") or 30)
        file_id_filter = str(args.get("file_id") or "").strip() or None
        try:
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            return {"content": [{"type": "text", "text": f"Invalid regex: {exc}"}]}

        if file_id_filter:
            allowed = await _allowed_file_ids()
            if file_id_filter not in allowed:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"file_id {file_id_filter!r} is not in the attached knowledge base(s).",
                        }
                    ]
                }

        hits: List[str] = []
        files_scanned = 0
        async for f in _iter_scoped_files():
            if file_id_filter and f.id != file_id_filter:
                continue
            files_scanned += 1
            content = (f.data or {}).get("content", "") or ""
            for m in compiled.finditer(content):
                ctx_start = max(0, m.start() - 80)
                ctx_end = min(len(content), m.end() + 80)
                ctx = content[ctx_start:ctx_end].replace("\n", " ")
                hits.append(
                    f"- **{f.filename}** (file_id={f.id}) @ char {m.start()}:\n  …{ctx}…"
                )
                if len(hits) >= max_matches:
                    break
            if len(hits) >= max_matches:
                break

        scope = (
            f"1 file ({file_id_filter})"
            if file_id_filter
            else f"{files_scanned} file(s)"
        )
        if not hits:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"No matches for /{pattern}/ across {scope}.",
                    }
                ]
            }
        header = (
            f"Found {len(hits)} match(es) for /{pattern}/ across {scope}"
            f"{' (capped at max_matches)' if len(hits) >= max_matches else ''}:\n"
        )
        return {"content": [{"type": "text", "text": header + "\n".join(hits)}]}

    tools_list = [_search]
    tool_names = ["mcp__helm-kb__search_knowledge"]
    if kb_ids:
        tools_list.extend([_list_docs, _read_doc, _grep])
        tool_names.extend(
            [
                "mcp__helm-kb__list_knowledge_documents",
                "mcp__helm-kb__read_knowledge_document",
                "mcp__helm-kb__grep_knowledge",
            ]
        )

    server = create_sdk_mcp_server("helm-kb", "0.1", tools=tools_list)
    return server, tool_names
