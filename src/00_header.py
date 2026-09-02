"""
title: Claude Code
description: Run Claude Code's agent loop from inside OpenWebUI chats via the Claude Agent SDK.
author: Dan Cormier, Thomas Friedel
version: 0.1.0
license: MIT
requirements: claude-agent-sdk>=0.2.116
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

