#!/usr/bin/env python3
"""Tests for the background-task helper (src/31_tasks.py).

Run: python3 test_tasks.py [<path-to-pipe.py>]
"""

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


TEMPLATE = "### Task:\nGenerate a title...\n<chat_history>\nUSER: hi\n</chat_history>"
body = {"messages": [{"role": "user", "content": TEMPLATE}]}

for task in ("title_generation", "tags_generation", "follow_up_generation"):
    got = mod._task_prompt(task, body)
    check(f"{task} answers the template", got == TEMPLATE, repr(got))

for task in ("query_generation", "autocomplete_generation", "emoji_generation",
             "function_calling", "image_prompt_generation", "moa_response_generation",
             "", None):
    check(f"{task!r} answers nothing", mod._task_prompt(task, body) == "")

parts = {"messages": [{"role": "user", "content": [
    {"type": "text", "text": "  title please  "},
    {"type": "image_url", "image_url": {"url": "data:"}},
]}]}
check("content parts: text only, stripped",
      mod._task_prompt("title_generation", parts) == "title please")

last = {"messages": [
    {"role": "user", "content": "old"},
    {"role": "assistant", "content": "reply"},
    {"role": "user", "content": "new"},
]}
check("last user message wins", mod._task_prompt("title_generation", last) == "new")

check("no messages", mod._task_prompt("title_generation", {}) == "")
check("assistant only",
      mod._task_prompt("title_generation", {"messages": [{"role": "assistant", "content": "x"}]}) == "")

print()
if fails:
    print(f"{len(fails)} FAILED: {', '.join(fails)}")
    sys.exit(1)
print("all passed")
