#!/usr/bin/env python3
"""Tests for the chat-search pure helpers (src/38_chats.py).

Run: python3 test_chats.py [<path-to-pipe.py>]

Same standalone pattern as test_turn.py: slice the module at the SDK import,
stub pydantic, exec the head. The rows here mirror the `chat` table's JSON
column in both shapes Open WebUI has used (history.messages map, flat list).
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


def history_blob(*turns):
    msgs = {}
    for i, (role, content) in enumerate(turns):
        msgs[f"m{i}"] = {"id": f"m{i}", "role": role, "content": content, "timestamp": 1000 + i}
    return json.dumps({"history": {"messages": msgs, "currentId": f"m{len(turns)-1}"}})


# ---- turns ----
blob = history_blob(("user", "Plan the solar install"), ("assistant", "Three options: A, B, C"), ("system", "hidden"))
turns = mod._chat_turns(blob)
check("history map → ordered user/assistant turns", turns == [("user", "Plan the solar install"), ("assistant", "Three options: A, B, C")], turns)
shuffled = json.loads(blob); shuffled["history"]["messages"] = dict(reversed(list(shuffled["history"]["messages"].items())))
check("order comes from timestamps, not dict order", mod._chat_turns(json.dumps(shuffled)) == turns)
flat = json.dumps({"messages": [{"role": "user", "content": [{"type": "text", "text": "multimodal"}, {"type": "image_url"}]}, {"role": "assistant", "content": ""}]})
check("flat list + multimodal parts; empty reply dropped", mod._chat_turns(flat) == [("user", "multimodal")])
check("garbage blob → no turns", mod._chat_turns("{not json") == [] and mod._chat_turns(None) == [])

# ---- transcript ----
t = mod._chat_transcript(turns)
check("transcript labels roles", t == "User: Plan the solar install\n\nAssistant: Three options: A, B, C", t)
check("transcript truncates with a marker", mod._chat_transcript(turns, 10).startswith("User: Plan") and "truncated" in mod._chat_transcript(turns, 10))

# ---- search ----
rows = [
    ("c1", "Solar install plan", 200, 0, history_blob(("user", "Plan the solar install"), ("assistant", "Three options"))),
    ("c2", "Weekend", 300, 1, history_blob(("user", "solar panels for the shed?"), ("assistant", "yes, and a battery"))),
    ("c3", "Groceries", 400, 0, history_blob(("user", "milk eggs"), ("assistant", "added"))),
    ("c4", "Solar again", 500, None, history_blob(("user", "solar solar solar solar solar solar solar"), ("assistant", "ok"))),
    ("me", "Current chat", 600, 0, history_blob(("user", "solar"), ("assistant", "…"))),
]
hits = mod._search_chat_rows(rows, "solar install", 10, exclude_id="me")
ids = [h["id"] for h in hits]
check("no-match chat excluded, current chat excluded", "c3" not in ids and "me" not in ids, ids)
check("both-terms + title match ranks first", ids[0] == "c1", ids)
check("frequency is capped so a stuffed chat does not win", ids.index("c4") > ids.index("c1"), ids)
check("hit carries title, turns, snippet", hits[0]["title"] == "Solar install plan" and hits[0]["turns"] == 2 and "solar" in hits[0]["snippet"].lower(), hits[0])
check("limit respected", len(mod._search_chat_rows(rows, "solar", 1)) == 1)
check("empty query → nothing", mod._search_chat_rows(rows, "  ", 5) == [])
check("one-letter noise ignored", mod._query_terms("a solar x") == ["solar"])
sub = [("s1", "Small parts", 100, 0, history_blob(("user", "normal small bins"))), ("s2", "Mal", 50, 0, history_blob(("user", "text Mal about plans")))]
check("terms match at word starts only ('mal' not in 'small')", [h["id"] for h in mod._search_chat_rows(sub, "Mal plan")] == ["s2"])
tie = [("t1", "Solar", 100, 0, history_blob(("user", "solar"))), ("t2", "Solar", 900, 0, history_blob(("user", "solar")))]
ph = [("p1", "Hot sauce", 900, 0, history_blob(("user", "hot sauce list"), ("assistant", "ones I like"))), ("p2", "Weekend", 100, 0, history_blob(("user", "the hot ones challenge")))]
check("exact phrase outranks scattered terms in the title", [h["id"] for h in mod._search_chat_rows(ph, "hot ones")][0] == "p2")
check("ties: newest first", [h["id"] for h in mod._search_chat_rows(tie, "solar")] == ["t2", "t1"])

# ---- snippet ----
long = history_blob(("user", "x" * 300 + " the battery sizing question " + "y" * 300))
snip = mod._chat_snippet(mod._chat_turns(long), ["battery"])
check("snippet centred on the hit with ellipses", snip.startswith("…") and "battery" in snip and snip.endswith("…"), snip)

# ---- rendering ----
out = mod._format_chat_hits(hits, "solar install")
check("results list chat_id and a read_chat hint", "chat_id=c1" in out and "read_chat" in out and "**Solar install plan**" in out, out)
check("archived chats are included and flagged", any(h["id"] == "c2" and h["archived"] for h in hits) and "turns · archived · chat_id=c2" in out, out)
check("no hits → a helpful line", "No earlier chats match" in mod._format_chat_hits([], "zzz"))

# ---- database backend ----
check("empty DATABASE_URL is sqlite", mod._db_is_sqlite(""))
check("sqlite URL is sqlite", mod._db_is_sqlite("sqlite:///data/webui.db"))
check("postgres URL is not", not mod._db_is_sqlite("postgresql://u:p@db/owui"))
check("postgres+psycopg URL is not", not mod._db_is_sqlite("postgres+psycopg://u:p@db/owui"))

if fails:
    print(f"\nFAILED: {len(fails)} — " + ", ".join(fails))
    sys.exit(1)
print("ok — chat search tests passed")
