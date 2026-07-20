"""Tests for the desktop agent's tool-calling loop and confirmation flow."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services import desktop_agent, task_state, tools

USER = "U-admin"


@pytest.fixture(autouse=True)
def _clean_state():
    desktop_agent.reset_history(USER)
    yield
    desktop_agent.reset_history(USER)


async def test_plain_text_reply_needs_no_tool_call() -> None:
    with (
        patch(
            "app.services.desktop_agent.chat_completion",
            new=AsyncMock(return_value="午安,有什麼我可以幫忙的嗎?"),
        ),
        patch("app.services.desktop_agent.line_client.send_text", new=AsyncMock()) as send,
    ):
        await desktop_agent.handle_message(USER, "reply-1", "哈囉")

    send.assert_awaited_once_with("reply-1", USER, "午安,有什麼我可以幫忙的嗎?")
    assert task_state.get(USER) is None


async def test_safe_tool_call_executes_without_confirmation(tmp_path, monkeypatch) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "hermes_desktop_root", str(tmp_path))
    (tmp_path / "note.txt").write_text("秘密內容", encoding="utf-8")

    responses = [
        json.dumps({"tool": "read_file", "args": {"path": "note.txt"}}),
        "檔案內容是:秘密內容",
    ]

    async def fake_chat_completion(_messages):
        return responses.pop(0)

    with (
        patch("app.services.desktop_agent.chat_completion", side_effect=fake_chat_completion),
        patch("app.services.desktop_agent.line_client.push_confirm", new=AsyncMock()) as confirm,
        patch("app.services.desktop_agent.line_client.send_text", new=AsyncMock()) as send,
    ):
        await desktop_agent.handle_message(USER, "reply-1", "幫我看看 note.txt 寫什麼")

    confirm.assert_not_awaited()
    send.assert_awaited_once_with("reply-1", USER, "檔案內容是:秘密內容")
    assert task_state.get(USER) is None


async def test_dangerous_tool_call_waits_for_confirmation_then_resumes(
    tmp_path, monkeypatch
) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "hermes_desktop_root", str(tmp_path))

    responses = [
        json.dumps({"tool": "write_file", "args": {"path": "out.txt", "content": "hi"}}),
        "已經幫你寫好檔案了。",
    ]

    async def fake_chat_completion(_messages):
        return responses.pop(0)

    with (
        patch("app.services.desktop_agent.chat_completion", side_effect=fake_chat_completion),
        patch("app.services.desktop_agent.line_client.push_confirm", new=AsyncMock()) as confirm,
        patch("app.services.desktop_agent.line_client.send_text", new=AsyncMock()) as send,
    ):
        task = asyncio.create_task(
            desktop_agent.handle_message(USER, "reply-1", "幫我建立 out.txt")
        )
        for _ in range(20):
            await asyncio.sleep(0.01)
            state = task_state.get(USER)
            if state and state.status == "awaiting_confirmation":
                break
        assert state is not None
        assert state.status == "awaiting_confirmation"
        confirm.assert_awaited_once()

        await desktop_agent.handle_message(USER, "reply-2", task_state.CONFIRM_YES)
        await task

    send.assert_awaited_once_with("reply-1", USER, "已經幫你寫好檔案了。")
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hi"
    assert task_state.get(USER) is None


async def test_dangerous_tool_call_cancelled_on_no(tmp_path, monkeypatch) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "hermes_desktop_root", str(tmp_path))

    responses = [
        json.dumps({"tool": "write_file", "args": {"path": "out.txt", "content": "hi"}}),
        "好的,我取消了這個動作。",
    ]

    async def fake_chat_completion(_messages):
        return responses.pop(0)

    with (
        patch("app.services.desktop_agent.chat_completion", side_effect=fake_chat_completion),
        patch("app.services.desktop_agent.line_client.push_confirm", new=AsyncMock()),
        patch("app.services.desktop_agent.line_client.send_text", new=AsyncMock()) as send,
    ):
        task = asyncio.create_task(
            desktop_agent.handle_message(USER, "reply-1", "幫我建立 out.txt")
        )
        for _ in range(20):
            await asyncio.sleep(0.01)
            state = task_state.get(USER)
            if state and state.status == "awaiting_confirmation":
                break

        await desktop_agent.handle_message(USER, "reply-2", task_state.CONFIRM_NO)
        await task

    send.assert_awaited_once_with("reply-1", USER, "好的,我取消了這個動作。")
    assert not (tmp_path / "out.txt").exists()


async def test_status_query_when_idle() -> None:
    with patch("app.services.desktop_agent.line_client.send_text", new=AsyncMock()) as send:
        await desktop_agent.handle_message(USER, "reply-1", "進度")
    send.assert_awaited_once()
    assert "沒有任務" in send.await_args.args[2]


async def test_new_message_while_task_running_is_rejected() -> None:
    task_state.start(USER)
    with patch("app.services.desktop_agent.line_client.send_text", new=AsyncMock()) as send:
        await desktop_agent.handle_message(USER, "reply-2", "還在忙嗎?")
    send.assert_awaited_once()
    assert "執行中" in send.await_args.args[2]
    task_state.clear(USER)


def test_parse_tool_call_rejects_non_json() -> None:
    assert desktop_agent._parse_tool_call("這只是普通回覆") is None
    assert desktop_agent._parse_tool_call('{"no_tool_key": 1}') is None
    parsed = desktop_agent._parse_tool_call('{"tool": "read_file", "args": {"path": "a"}}')
    assert parsed == {"tool": "read_file", "args": {"path": "a"}}


def test_is_dangerous_unknown_tool_defaults_true() -> None:
    assert tools.is_dangerous("delete_everything", {})
