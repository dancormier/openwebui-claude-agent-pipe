#!/usr/bin/env python3
"""Tests for the system-prompt helpers (src/50_render.py).

Run: python3 test_render.py [<path-to-pipe.py>]

The helpers sit below the SDK import, so the module is exec'd against the
stub SDK; nothing here touches it.
"""

from _loader import check_eq, load_full, report

mod = load_full()
mod.log.disabled = True

NOTICE = "<context>The requested image has been created.</context>"
FAILED = "<context>Tool web_search failed.</context>"


def sys_msg(content):
    return {"role": "system", "content": content}


def user_msg(content):
    return {"role": "user", "content": content}


def text(t):
    return {"type": "text", "text": t}


# ---- no system prompt ----
check_eq("no messages key", mod._extract_system_prompt({}), None)
check_eq("messages is None", mod._extract_system_prompt({"messages": None}), None)
check_eq("only user/assistant turns", mod._extract_system_prompt(
    {"messages": [user_msg("hi"), {"role": "assistant", "content": "hello"}]}), None)
check_eq("blank system content", mod._extract_system_prompt({"messages": [sys_msg("   \n")]}), None)
check_eq("system content of an unknown shape is skipped",
         mod._extract_system_prompt({"messages": [sys_msg(None), sys_msg(42)]}), None)

# ---- string content ----
check_eq("single system string is stripped",
         mod._extract_system_prompt({"messages": [sys_msg("  You are Otis.\n"), user_msg("hi")]}),
         "You are Otis.")
check_eq("several system messages join with a blank line",
         mod._extract_system_prompt({"messages": [sys_msg("A"), user_msg("x"), sys_msg("B")]}),
         "A\n\nB")
check_eq("user content never leaks into the prompt",
         mod._extract_system_prompt({"messages": [user_msg("secret"), sys_msg("A")]}), "A")

# ---- multimodal list content ----
check_eq("text pieces of a list are collected",
         mod._extract_system_prompt({"messages": [sys_msg([text("A"), text("B")])]}), "A\n\nB")
check_eq("non-text pieces and non-dicts are ignored",
         mod._extract_system_prompt({"messages": [sys_msg([
             {"type": "image_url", "image_url": {"url": "data:"}}, "stray", text("A")])]}),
         "A")
check_eq("text piece missing its text is dropped",
         mod._extract_system_prompt({"messages": [sys_msg([{"type": "text"}, text("A")])]}), "A")

# ---- <context> notices are removed from the prompt ----
check_eq("notice before the prompt is removed",
         mod._extract_system_prompt({"messages": [sys_msg(NOTICE + "\nYou are Otis.")]}),
         "You are Otis.")
check_eq("several notices, including multi-line, are removed",
         mod._extract_system_prompt({"messages": [sys_msg(
             "<context>line one\nline two</context>\nYou are Otis.\n" + FAILED)]}),
         "You are Otis.")
check_eq("a message that is only a notice contributes nothing",
         mod._extract_system_prompt({"messages": [sys_msg(NOTICE), sys_msg("A")]}), "A")
check_eq("notice-only system prompt is None",
         mod._extract_system_prompt({"messages": [sys_msg(NOTICE)]}), None)
check_eq("notice inside a list piece is removed",
         mod._extract_system_prompt({"messages": [sys_msg([text(NOTICE + "A")])]}), "A")

# ---- _extract_turn_context ----
check_eq("no messages, no notices", mod._extract_turn_context({}), [])
check_eq("plain prompt has no notices",
         mod._extract_turn_context({"messages": [sys_msg("You are Otis.")]}), [])
check_eq("notices come back verbatim in order across messages and pieces",
         mod._extract_turn_context({"messages": [
             sys_msg(NOTICE + "\nYou are Otis."),
             sys_msg([text("x" + FAILED)]),
         ]}),
         [NOTICE, FAILED])
check_eq("a multi-line notice is one match",
         mod._extract_turn_context({"messages": [sys_msg("<context>a\nb</context>")]}),
         ["<context>a\nb</context>"])
check_eq("adjacent notices match separately, not greedily",
         mod._extract_turn_context({"messages": [sys_msg(NOTICE + FAILED)]}), [NOTICE, FAILED])
check_eq("notices in user messages are not turn context",
         mod._extract_turn_context({"messages": [user_msg(NOTICE)]}), [])

report("render tests passed")
