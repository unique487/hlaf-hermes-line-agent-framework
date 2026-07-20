"""Tests for the internal PreToolUse-confirmation endpoint used by the Claude
Code hook script (scripts/claude_hermes_hook.py)."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.config import get_settings
from app.services import claude_code_agent, task_state

USER = "U-admin"
SESSION = "sess-abc"


class _FakeClient:
    host = "127.0.0.1"


class _FakeRequest:
    client = _FakeClient()


@pytest.fixture(autouse=True)
def _clean_state():
    claude_code_agent.reset_history(USER)
    task_state.clear(USER)
    yield
    claude_code_agent.reset_history(USER)
    task_state.clear(USER)


def test_require_loopback_rejects_non_loopback_client() -> None:
    from fastapi import HTTPException

    from app.api.internal import _require_loopback

    class _FakeClient:
        host = "203.0.113.5"

    class _FakeRequest:
        client = _FakeClient()

    with pytest.raises(HTTPException) as exc_info:
        _require_loopback(_FakeRequest())
    assert exc_info.value.status_code == 403


def test_require_loopback_accepts_127001() -> None:
    from app.api.internal import _require_loopback

    class _FakeClient:
        host = "127.0.0.1"

    class _FakeRequest:
        client = _FakeClient()

    _require_loopback(_FakeRequest())  # should not raise


async def test_unknown_session_id_is_rejected() -> None:
    from app.api.internal import ClaudeConfirmRequest, claude_confirm

    result = await claude_confirm(
        ClaudeConfirmRequest(session_id="no-such-session", description="測試"), _FakeRequest()
    )
    assert result == {"approved": False}


async def test_known_session_without_active_task_is_rejected() -> None:
    from app.api.internal import ClaudeConfirmRequest, claude_confirm

    claude_code_agent._sessions[USER] = SESSION
    claude_code_agent._session_to_user[SESSION] = USER

    result = await claude_confirm(
        ClaudeConfirmRequest(session_id=SESSION, description="測試"), _FakeRequest()
    )
    assert result == {"approved": False}


async def test_confirm_flow_approves_after_line_yes(monkeypatch) -> None:
    claude_code_agent._sessions[USER] = SESSION
    claude_code_agent._session_to_user[SESSION] = USER
    task = task_state.start(USER)

    async def _answer_yes_soon() -> None:
        await asyncio.sleep(0.02)
        task.confirm_result = True
        task.confirm_event.set()

    with patch("app.api.internal.line_client.push_confirm", new=AsyncMock()) as push:
        answer_task = asyncio.create_task(_answer_yes_soon())
        # Call the route function directly (in-process) so it shares the event
        # loop/task_state module with the background `_answer_yes_soon` coroutine.
        from app.api.internal import ClaudeConfirmRequest, claude_confirm

        result = await claude_confirm(
            ClaudeConfirmRequest(session_id=SESSION, description="要不要寫檔案?"),
            _FakeRequest(),
        )
        await answer_task

    push.assert_awaited_once()
    assert result == {"approved": True}
    assert task.status == "running"


async def test_confirm_flow_times_out_as_rejection(monkeypatch) -> None:
    claude_code_agent._sessions[USER] = SESSION
    claude_code_agent._session_to_user[SESSION] = USER
    task_state.start(USER)
    monkeypatch.setattr(get_settings(), "hermes_confirm_timeout_seconds", 0)

    from app.api.internal import ClaudeConfirmRequest, claude_confirm

    with (
        patch("app.api.internal.line_client.push_confirm", new=AsyncMock()),
        patch("app.api.internal.line_client.push_text", new=AsyncMock()) as push_text,
    ):
        result = await claude_confirm(
            ClaudeConfirmRequest(session_id=SESSION, description="危險動作"), _FakeRequest()
        )

    assert result == {"approved": False}
    push_text.assert_awaited_once()
