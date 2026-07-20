"""Internal-only endpoints for the Claude Code PreToolUse hook.

`scripts/claude_confirm_hook.py` runs as a child process of the `claude` CLI,
which is itself spawned by `app.services.claude_code_agent` on this same
machine. It POSTs here to turn a "dangerous tool call" into a blocking LINE
Yes/No round-trip. This router must never be reachable from anywhere but
127.0.0.1 — it has no auth beyond the loopback check below.
"""

import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.config import get_settings
from app.logger import logger
from app.services import line_client, task_state
from app.services.claude_code_agent import get_user_for_session

router = APIRouter(prefix="/internal", tags=["internal"])

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class ClaudeConfirmRequest(BaseModel):
    session_id: str
    description: str


def _require_loopback(request: Request) -> None:
    client = request.client
    if client is None or client.host not in _LOOPBACK_HOSTS:
        host = client.host if client else "unknown"
        logger.warning(f"Rejected /internal call from non-loopback host {host}")
        raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/claude-confirm")
async def claude_confirm(payload: ClaudeConfirmRequest, request: Request) -> dict[str, bool]:
    """Push a LINE confirm prompt for `payload.session_id`'s user and block until answered."""
    _require_loopback(request)

    user_id = get_user_for_session(payload.session_id)
    if user_id is None:
        logger.warning(f"claude-confirm: unknown session_id {payload.session_id!r}")
        return {"approved": False}

    task = task_state.get(user_id)
    if task is None:
        logger.warning(f"claude-confirm: no active task for user {user_id[:8]}…")
        return {"approved": False}

    settings = get_settings()
    task.status = "awaiting_confirmation"
    task.confirm_event = asyncio.Event()
    task.confirm_result = None
    await line_client.push_confirm(user_id, payload.description)
    try:
        await asyncio.wait_for(
            task.confirm_event.wait(), timeout=settings.confirm_timeout_seconds
        )
    except TimeoutError:
        await line_client.push_text(user_id, "確認逾時,已自動取消這個動作。")
        task.status = "running"
        return {"approved": False}

    task.status = "running"
    return {"approved": bool(task.confirm_result)}
