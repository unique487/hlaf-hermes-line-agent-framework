#!/usr/bin/env python
"""Claude Code `PreToolUse` hook: gate dangerous tool calls behind LINE confirmation.

Registered via `config/claude_hooks.json` (passed to `claude` with
`--settings`). The `claude` CLI runs this script as a child process before
every tool invocation, feeding it a JSON payload on stdin and reading a JSON
decision from stdout.

Verified against a real, logged-in `claude` CLI (v2.1.215): the stdin payload
shape (`{"session_id", "tool_name", "tool_input", ...}`) and the stdout
decision schema (`{"decision": "approve"|"block", "reason": "..."}`) both
match what's implemented here.

This script must be runnable standalone as a subprocess (no guarantee it
inherits the FastAPI process's Python environment beyond whatever `claude`
passes through), so it adds the repo root to `sys.path` itself and only
depends on `httpx`, already a project dependency.
"""

import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import httpx  # noqa: E402

from app.services.tools import is_dangerous  # noqa: E402

# Should stay comfortably above CONFIRM_TIMEOUT_SECONDS (default 600s) so the
# hook doesn't give up before the service itself times out the confirm.
_HTTP_TIMEOUT_SECONDS = 630
_DEFAULT_BASE_URL = "http://127.0.0.1:8000"


def _base_url() -> str:
    return os.environ.get("CLAUDE_INTERNAL_BASE_URL", _DEFAULT_BASE_URL)


def _describe(tool_name: str, tool_input: dict) -> str:
    if tool_name == "Bash":
        return f"執行指令:{tool_input.get('command', '')}"
    if tool_name in {"Write", "Edit", "MultiEdit"}:
        return f"寫入檔案:{tool_input.get('file_path', '')}"
    path = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("pattern") or ""
    return f"{tool_name}:{path}"


def _approve() -> None:
    print(json.dumps({"decision": "approve"}))


def _block(reason: str) -> None:
    print(json.dumps({"decision": "block", "reason": reason}))


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        # Can't parse the hook payload — fail open rather than stalling every
        # tool call on a malformed/unexpected stdin shape.
        _approve()
        return

    session_id = payload.get("session_id", "")
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}

    if not is_dangerous(tool_name, tool_input):
        _approve()
        return

    description = _describe(tool_name, tool_input)
    try:
        resp = httpx.post(
            f"{_base_url()}/internal/claude-confirm",
            json={"session_id": session_id, "description": description},
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        approved = bool(resp.json().get("approved"))
    except httpx.HTTPError as exc:
        _block(f"無法連上內部確認服務,已阻擋此動作:{exc}")
        return

    if approved:
        _approve()
    else:
        _block("使用者透過 LINE 拒絕了這個動作")


if __name__ == "__main__":
    main()
