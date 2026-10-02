"""Shared harness for the standalone test_*.py scripts.

Every suite runs as `python3 test_x.py [<path-to-pipe.py>]` with only the
stdlib: neither claude_agent_sdk nor pydantic nor Open WebUI is installed
where these matter most (CI, and a host checking the copy deployed into
webui.db). `load_head` slices the built module at the SDK import and execs
the pure helpers above it; `load_full` execs the whole module against a
stub SDK so the turn loop in `class Pipe` can run with a fake client.

The leading underscore keeps this file out of CI's `test_*.py` glob.
"""

import pathlib
import sys
import types

SPLIT = "from claude_agent_sdk import ("

fails = []


def resolve_pipe(pipe_path=None):
    if pipe_path is not None:
        return pathlib.Path(pipe_path)
    if len(sys.argv) > 1:
        return pathlib.Path(sys.argv[1])
    return pathlib.Path(__file__).with_name("claude_agent_pipe.py")


def _stub_pydantic():
    if "pydantic" in sys.modules:
        return

    class BaseModel:
        def __init__(self, **kw):
            for key, value in kw.items():
                setattr(self, key, value)

    stub = types.ModuleType("pydantic")
    stub.BaseModel = BaseModel
    # Returning the default lets `Valves()` read every field as the class
    # attribute the real model would have given it.
    stub.Field = lambda default=None, **k: default
    sys.modules["pydantic"] = stub


def _exec_module(source, path, name):
    mod = types.ModuleType(name)
    mod.__dict__["__name__"] = name
    mod.__dict__["__file__"] = str(path)
    exec(compile(source, str(path), "exec"), mod.__dict__)
    return mod


def load_head(pipe_path=None):
    path = resolve_pipe(pipe_path)
    _stub_pydantic()
    head = path.read_text(encoding="utf-8").split(SPLIT, 1)[0]
    return _exec_module(head, path, "pipe_head")


def _stub_sdk():
    """The shapes the pipe touches at import time and in `isinstance`
    dispatch: distinct classes carrying whatever attributes the test gives
    them, a `tool` decorator that hands the function back, and a server
    factory returning the dict the pipe spreads `alwaysLoad` into."""
    stub = types.ModuleType("claude_agent_sdk")

    def message_class(name):
        def __init__(self, **kw):
            self.__dict__.update(kw)
        return type(name, (), {"__init__": __init__})

    for name in (
        "AssistantMessage", "ClaudeAgentOptions", "RateLimitEvent",
        "ResultMessage", "StreamEvent", "SystemMessage", "ToolResultBlock",
        "ToolUseBlock", "UserMessage",
    ):
        setattr(stub, name, message_class(name))

    class CLIJSONDecodeError(Exception):
        pass

    class ClaudeSDKClient:
        def __init__(self, options=None):
            raise RuntimeError("test must replace ClaudeSDKClient with a fake")

    def tool(name, description, schema):
        def decorate(fn):
            fn.tool_name = name
            return fn
        return decorate

    def create_sdk_mcp_server(name, version, tools=()):
        return {"type": "sdk", "name": name, "tools": list(tools)}

    stub.CLIJSONDecodeError = CLIJSONDecodeError
    stub.ClaudeSDKClient = ClaudeSDKClient
    stub.tool = tool
    stub.create_sdk_mcp_server = create_sdk_mcp_server
    sys.modules["claude_agent_sdk"] = stub


def load_full(pipe_path=None):
    path = resolve_pipe(pipe_path)
    _stub_pydantic()
    _stub_sdk()
    return _exec_module(path.read_text(encoding="utf-8"), path, "pipe_full")


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        fails.append(name)


def check_eq(name, got, want):
    check(name, got == want, f"got {got!r}, want {want!r}")


def report(label):
    if fails:
        print(f"\nFAILED: {len(fails)} — " + ", ".join(fails))
        sys.exit(1)
    print(f"ok — {label}")
