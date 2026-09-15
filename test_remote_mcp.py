#!/usr/bin/env python3
"""Tests for the REMOTE_MCP_SERVERS pure helper (src/37_remote_mcp.py).

Run: python3 test_remote_mcp.py [<path-to-pipe.py>]

Same standalone pattern as test_askuser.py: slice the module at the SDK
import, stub pydantic, exec the head. The SDK's own handling of an http
server is not under test here; what is tested is that the valve text turns
into exactly the configs the SDK is handed, and that nothing else does.
"""

import json
import pathlib
import sys
import types

PIPE = pathlib.Path(
    sys.argv[1] if len(sys.argv) > 1
    else pathlib.Path(__file__).with_name("claude_agent_pipe.py")
)
SPLIT = "from claude_agent_sdk import ("

if "pydantic" not in sys.modules:
    _stub = types.ModuleType("pydantic")
    _stub.BaseModel = type("BaseModel", (), {})
    _stub.Field = lambda *a, **k: None
    sys.modules["pydantic"] = _stub

src = PIPE.read_text(encoding="utf-8")
head = src.split(SPLIT, 1)[0]
mod = types.ModuleType("pipe_head")
mod.__dict__["__name__"] = "pipe_head"
exec(compile(head, str(PIPE), "exec"), mod.__dict__)

fails = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        fails.append(name)


parse = mod._parse_remote_mcp_servers

print("empty and malformed")
check("empty string -> nothing", parse("") == ({}, [], []))
check("whitespace -> nothing", parse("  \n") == ({}, [], []))
s, t, e = parse("{not json")
check("invalid JSON -> one error, no servers", s == {} and t == [] and len(e) == 1 and "JSON" in e[0])
s, t, e = parse("[1, 2]")
check("array -> rejected", s == {} and t == [] and len(e) == 1 and "object" in e[0])

print("one good server")
raw = json.dumps({"linear": {"url": "https://mcp.linear.app/mcp",
                             "headers": {"Authorization": "Bearer lin_api_x"}}})
s, t, e = parse(raw)
check("server config is the SDK http shape",
      s == {"linear": {"type": "http", "url": "https://mcp.linear.app/mcp",
                       "headers": {"Authorization": "Bearer lin_api_x"}}}, repr(s))
check("allowed tool is the whole-server form", t == ["mcp__linear"], repr(t))
check("no errors", e == [], repr(e))
s, t, e = parse(json.dumps({"ro": {"url": "https://mcp.linear.app/mcp/readonly"}}))
check("headers optional and omitted when absent", s == {"ro": {"type": "http", "url": "https://mcp.linear.app/mcp/readonly"}}, repr(s))
s, t, e = parse(json.dumps({"ro": {"url": "https://x.example/mcp", "headers": None}}))
check("headers null treated as absent", "ro" in s and "headers" not in s["ro"] and e == [], repr((s, e)))

print("bad entries cost only themselves")
raw = json.dumps({
    "Linear": {"url": "https://mcp.linear.app/mcp"},
    "good": {"url": "https://mcp.example/mcp"},
    "plain": {"url": "http://mcp.example/mcp"},
    "local": {"url": "http://127.0.0.1:8765/mcp"},
    "localhost": {"url": "http://localhost/mcp"},
    "nourl": {"headers": {}},
    "badhdr": {"url": "https://mcp.example/mcp", "headers": {"A": 1}},
    "extra": {"url": "https://mcp.example/mcp", "command": "npx"},
    "str": "https://mcp.example/mcp",
    "ftp": {"url": "ftp://mcp.example/mcp"},
})
s, t, e = parse(raw)
check("kept exactly the valid ones", sorted(s) == ["good", "local", "localhost"], repr(sorted(s)))
check("tools follow the kept servers", sorted(t) == ["mcp__good", "mcp__local", "mcp__localhost"], repr(t))
check("one error per dropped entry", len(e) == 7, repr(e))
check("uppercase name rejected by name rule", any(x.startswith("'Linear'") for x in e), repr(e))
check("plain http to a remote host rejected", any(x.startswith("plain:") and "https" in x for x in e), repr(e))
check("unknown keys rejected (no command/args smuggling)", any(x.startswith("extra:") and "command" in x for x in e), repr(e))

print("redaction covers Linear keys")
clean, hits = mod._redact_secrets("key lin_api_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3" + " end")
check("lin_api_ key redacted", "lin_api_a1B2" not in clean and "linear-api-key" in hits, repr((clean, hits)))
clean, hits = mod._redact_secrets("short lin_api_abc stays")
check("short lookalike untouched", clean == "short lin_api_abc stays", repr(clean))

print()
if fails:
    print(f"{len(fails)} failed: {fails}")
    sys.exit(1)
print("all passed")
