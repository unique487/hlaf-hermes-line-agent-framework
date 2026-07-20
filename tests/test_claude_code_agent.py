"""Tests for the Claude Code-backed desktop agent.

These stub out `asyncio.create_subprocess_exec` with a fake process object
(there's no real `claude` CLI available in CI/dev sandboxes) so we can
exercise the NDJSON `stream-json` parsing, session id bookkeeping, and the
surrounding handle_message control flow without actually invoking Claude Code.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.config import get_settings
from app.services import claude_code_agent, task_state

USER = "U-admin"


@pytest.fixture(autouse=True)
def _clean_state():
    claude_code_agent.reset_history(USER)
    yield
    claude_code_agent.reset_history(USER)


class _FakeStdout:
    def __init__(self, lines: list[str]):
        self._iter = iter(lines)

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        try:
            line = next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None
        return line.encode("utf-8")


class _FakeStderr:
    def __init__(self, data: bytes = b""):
        self._data = data

    async def read(self) -> bytes:
        return self._data


class _FakeProcess:
    def __init__(self, lines: list[str], returncode: int = 0, stderr: bytes = b""):
        self.stdout = _FakeStdout(lines)
        self.stderr = _FakeStderr(stderr)
        self._returncode = returncode
        self.killed = False

    async def wait(self) -> int:
        return self._returncode

    def kill(self) -> None:
        self.killed = True


def _result_event(text: str) -> str:
    return json.dumps({"type": "result", "result": text})


def _tool_use_event(name: str, tool_input: dict) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": name, "input": tool_input}]},
        }
    )


async def test_status_query_when_idle() -> None:
    with patch("app.services.claude_code_agent.line_client.send_text", new=AsyncMock()) as send:
        await claude_code_agent.handle_message(USER, "reply-1", "進度")
    send.assert_awaited_once()
    assert "沒有任務" in send.await_args.args[2]


async def test_new_message_while_task_running_is_rejected() -> None:
    task_state.start(USER)
    with patch("app.services.claude_code_agent.line_client.send_text", new=AsyncMock()) as send:
        await claude_code_agent.handle_message(USER, "reply-2", "還在忙嗎?")
    send.assert_awaited_once()
    assert "執行中" in send.await_args.args[2]
    task_state.clear(USER)


async def test_confirm_reply_without_pending_task_is_ignored() -> None:
    with patch("app.services.claude_code_agent.line_client.send_text", new=AsyncMock()) as send:
        await claude_code_agent.handle_message(USER, "reply-1", task_state.CONFIRM_YES)
    send.assert_awaited_once()
    assert "沒有待確認" in send.await_args.args[2]


async def test_run_task_happy_path_streams_tool_use_and_result(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "hermes_desktop_root", str(tmp_path))
    lines = [
        _tool_use_event("Read", {"file_path": "note.txt"}),
        _result_event("讀完了,內容是 hello"),
        "",  # blank lines should be ignored
        "not json at all",  # malformed lines should be ignored
    ]
    fake_proc = _FakeProcess(lines)

    with (
        patch(
            "app.services.claude_code_agent.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=fake_proc),
        ) as spawn,
        patch("app.services.claude_code_agent.line_client.send_text", new=AsyncMock()) as send,
    ):
        await claude_code_agent.handle_message(USER, "reply-1", "幫我看看 note.txt")

    spawn.assert_awaited_once()
    send.assert_awaited_once_with("reply-1", USER, "讀完了,內容是 hello")
    assert task_state.get(USER) is None
    # A session id should now be tracked for this user, resumable next turn.
    session_id = claude_code_agent._sessions[USER]
    assert claude_code_agent.get_user_for_session(session_id) == USER


async def test_run_task_resumes_existing_session_on_second_turn(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "hermes_desktop_root", str(tmp_path))
    fake_proc = _FakeProcess([_result_event("第一句回覆")])

    with (
        patch(
            "app.services.claude_code_agent.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=fake_proc),
        ),
        patch("app.services.claude_code_agent.line_client.send_text", new=AsyncMock()),
    ):
        await claude_code_agent.handle_message(USER, "reply-1", "哈囉")

    first_session = claude_code_agent._sessions[USER]

    fake_proc_2 = _FakeProcess([_result_event("第二句回覆")])
    with (
        patch(
            "app.services.claude_code_agent.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=fake_proc_2),
        ) as spawn2,
        patch("app.services.claude_code_agent.line_client.send_text", new=AsyncMock()),
    ):
        await claude_code_agent.handle_message(USER, "reply-2", "再問一次")

    # Same session id reused ("--resume") across turns until /reset.
    assert claude_code_agent._sessions[USER] == first_session
    call_args = spawn2.await_args.args
    assert "--resume" in call_args
    assert first_session in call_args


async def test_reset_history_drops_session_mapping(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "hermes_desktop_root", str(tmp_path))
    fake_proc = _FakeProcess([_result_event("嗨")])
    with (
        patch(
            "app.services.claude_code_agent.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=fake_proc),
        ),
        patch("app.services.claude_code_agent.line_client.send_text", new=AsyncMock()),
    ):
        await claude_code_agent.handle_message(USER, "reply-1", "哈囉")

    session_id = claude_code_agent._sessions[USER]
    claude_code_agent.reset_history(USER)
    assert USER not in claude_code_agent._sessions
    assert claude_code_agent.get_user_for_session(session_id) is None


async def test_run_task_claude_cli_missing_reports_friendly_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "hermes_desktop_root", str(tmp_path))

    async def _raise_not_found(*_args, **_kwargs):
        raise FileNotFoundError

    with (
        patch(
            "app.services.claude_code_agent.asyncio.create_subprocess_exec",
            side_effect=_raise_not_found,
        ),
        patch("app.services.claude_code_agent.line_client.send_text", new=AsyncMock()) as send,
    ):
        await claude_code_agent.handle_message(USER, "reply-1", "哈囉")

    send.assert_awaited_once()
    assert "找不到 claude 指令" in send.await_args.args[2]


async def test_run_task_no_result_event_reports_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "hermes_desktop_root", str(tmp_path))
    fake_proc = _FakeProcess([], returncode=1, stderr=b"boom")

    with (
        patch(
            "app.services.claude_code_agent.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=fake_proc),
        ),
        patch("app.services.claude_code_agent.line_client.send_text", new=AsyncMock()) as send,
    ):
        await claude_code_agent.handle_message(USER, "reply-1", "哈囉")

    send.assert_awaited_once()
    assert "任務執行失敗" in send.await_args.args[2]


def test_describe_tool_use_variants() -> None:
    assert "執行指令" in claude_code_agent._describe_tool_use("Bash", {"command": "dir"})
    assert "寫入檔案" in claude_code_agent._describe_tool_use("Write", {"file_path": "a.txt"})
    assert "讀取檔案" in claude_code_agent._describe_tool_use("Read", {"file_path": "a.txt"})
    assert "LS" in claude_code_agent._describe_tool_use("LS", {"path": "."})
    assert "Unknown" in claude_code_agent._describe_tool_use("Unknown", {"x": 1})


def test_build_command_uses_session_id_or_resume() -> None:
    fresh = claude_code_agent._build_command("claude", r"C:\root", "sess-1", resume=False)
    assert "--session-id" in fresh
    assert "sess-1" in fresh
    assert "--resume" not in fresh

    resumed = claude_code_agent._build_command("claude", r"C:\root", "sess-1", resume=True)
    assert "--resume" in resumed
    assert "sess-1" in resumed
    assert "--session-id" not in resumed
