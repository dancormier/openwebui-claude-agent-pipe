# ---------------------------------------------------------------------------
# Remote MCP servers: the pure half. Parses the REMOTE_MCP_SERVERS valve (a
# JSON object, name -> {"url": ..., "headers": {...}}) into the SDK's http
# server configs plus the allowed-tools entries that let the agent call them.
# No SDK or Open WebUI imports so the head-slice suites reach it.
# ---------------------------------------------------------------------------

_REMOTE_MCP_NAME_RX = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_REMOTE_MCP_LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def _remote_mcp_url_ok(url: str) -> bool:
    # A bearer header travels with every request, so plain http is allowed
    # only to the local machine.
    if url.startswith("https://"):
        return len(url) > len("https://")
    if url.startswith("http://"):
        host = url[len("http://"):].split("/", 1)[0].split(":", 1)[0]
        return host in _REMOTE_MCP_LOCAL_HOSTS
    return False


def _parse_remote_mcp_servers(raw: str) -> tuple:
    """Return (servers, allowed_tools, errors) for the REMOTE_MCP_SERVERS valve.

    servers: {name: {"type": "http", "url": str, "headers": {str: str}}}
    allowed_tools: ["mcp__<name>", ...] — Claude Code's whole-server allow.
    errors: one line per entry dropped and why; the valve as a whole is never
    fatal, a bad entry costs only itself.
    """
    text = (raw or "").strip()
    if not text:
        return {}, [], []
    try:
        data = json.loads(text)
    except ValueError as e:
        return {}, [], [f"not valid JSON: {e}"]
    if not isinstance(data, dict):
        return {}, [], ["must be a JSON object: name -> {url, headers}"]
    servers = {}
    tools = []
    errors = []
    for name, cfg in data.items():
        if not isinstance(name, str) or not _REMOTE_MCP_NAME_RX.match(name):
            errors.append(f"{name!r}: name must match {_REMOTE_MCP_NAME_RX.pattern}")
            continue
        if not isinstance(cfg, dict):
            errors.append(f"{name}: entry must be an object with url and optional headers")
            continue
        url = cfg.get("url")
        if not isinstance(url, str) or not _remote_mcp_url_ok(url):
            errors.append(f"{name}: url must be https:// (or http:// to localhost)")
            continue
        headers = cfg.get("headers", {})
        if headers is None:
            headers = {}
        if not isinstance(headers, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
        ):
            errors.append(f"{name}: headers must be an object of string values")
            continue
        extra = set(cfg) - {"url", "headers"}
        if extra:
            errors.append(f"{name}: unknown keys {sorted(extra)}")
            continue
        server = {"type": "http", "url": url}
        if headers:
            server["headers"] = dict(headers)
        servers[name] = server
        tools.append(f"mcp__{name}")
    return servers, tools, errors
