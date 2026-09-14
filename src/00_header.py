"""
title: Claude Code
description: Run Claude Code's agent loop from inside OpenWebUI chats via the Claude Agent SDK.
author: Thomas Friedel, Dan Cormier
version: 0.2.2
license: MIT
requirements: claude-agent-sdk>=0.2.152
"""

import asyncio
import base64
import hashlib
import inspect
import json
import logging
import mimetypes
import os
import re
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field


async def _resolve(value: Any) -> Any:
    """Open WebUI 0.11 made its model helpers (Files, Users, Knowledges, Chats)
    async; earlier releases are sync. Calling an async one without awaiting
    returns a coroutine and silently does nothing, so every such call goes
    through here."""
    if inspect.isawaitable(value):
        return await value
    return value

