"""Tests for the codex fallback-chain brain (spawns `codex exec` per rung)."""

from unittest.mock import AsyncMock, patch

import pytest

from app.config import get_settings
from app.services import codex_agent, conversation


# --------------------------------------------------------------------------
# should_suppress / (silent) protocol — identical contract to opencode_agent
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "(silent)",
        " (silent) ",
        "(silent)\n",
        '"(silent)"',
        "「(silent)」",
        "(silent)。",
        "(SILENT)",
        "",
        "   ",
        None,
    ],
)
def test_should_suppress_true_cases(text) -> None:
    assert codex_agent.should_suppress(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "收到,總務處會盡快派人檢修。",
        "請洽總機 (02)2296-3625 轉 412(水電技士-代聘)。",
        "silent",  # missing parens — not the exact protocol token
        "這不是 (silent) 的訊息內容，還有其他字",
    ],
)
def test_should_suppress_false_cases(text) -> None:
    assert codex_agent.should_suppress(text) is False


# --------------------------------------------------------------------------
# build_prompt
# --------------------------------------------------------------------------


def test_build_prompt_with_no_history() -> None:
    prompt = codex_agent.build_prompt([], "使用者A", "早安")
    assert "(尚無對話紀錄)" in prompt
    assert "[使用者A]: 早安" in prompt


def test_build_prompt_with_mention_appends_instruction() -> None:
    prompt = codex_agent.build_prompt([], "使用者A", "在嗎", was_mentioned=True)
    assert "不要輸出 (silent)" in prompt


def test_build_prompt_with_reply_to_bot_appends_instruction() -> None:
    prompt = codex_agent.build_prompt([], "使用者A", "還沒好嗎", is_reply_to_bot=True)
    assert "不要輸出 (silent)" in prompt
    assert "回覆" in prompt


def test_build_prompt_without_reference_context_omits_that_block() -> None:
    prompt = codex_agent.build_prompt([], "使用者A", "早安")
    assert "可查閱的參考資料" not in prompt


def test_build_prompt_with_reference_context_prepends_it() -> None:
    prompt = codex_agent.build_prompt([], "使用者A", "早安", reference_context="分機表內容...")
    assert "可查閱的參考資料" in prompt
    assert "分機表內容..." in prompt
    assert prompt.index("分機表內容") < prompt.index("最新訊息")


# --------------------------------------------------------------------------
# group_reference_context — inlines the group bot's reference docs into the
# prompt so the model never needs to invoke a shell/read tool for them
# (that nested spawn was one of the causes behind a recurring visible
# console window — found live, 2026-07-22).
# --------------------------------------------------------------------------


def test_group_reference_context_combines_both_docs(tmp_path) -> None:
    (tmp_path / "GA_EXTENSIONS.md").write_text("分機表內容", encoding="utf-8")
    (tmp_path / "SCHOOL_BUILDINGS.md").write_text("建築物清單", encoding="utf-8")

    ctx = codex_agent.group_reference_context(str(tmp_path))

    assert "分機表內容" in ctx
    assert "建築物清單" in ctx


def test_group_reference_context_tolerates_missing_files(tmp_path) -> None:
    ctx = codex_agent.group_reference_context(str(tmp_path))
    assert ctx == ""


# --------------------------------------------------------------------------
# build_admin_prompt — deliberately NOT build_prompt's group-relevance
# framing, which confused a small model into asking for "more context"
# instead of just answering (found live, 2026-07-22).
# --------------------------------------------------------------------------


def test_build_admin_prompt_with_no_history_is_just_the_message() -> None:
    prompt = codex_agent.build_admin_prompt([], "你現在使用什麼模型?")
    assert prompt == "你現在使用什麼模型?"
    assert "尚無對話紀錄" not in prompt
    assert "供判斷上下文用" not in prompt


def test_build_admin_prompt_with_history_includes_prior_turns() -> None:
    history = [("使用者A", "早安"), ("機器人", "早安,有什麼需要協助的嗎?")]
    prompt = codex_agent.build_admin_prompt(history, "幫我看一下狀態")
    assert "早安" in prompt
    assert "目前訊息:" in prompt
    assert "幫我看一下狀態" in prompt
    assert prompt.index("早安") < prompt.index("幫我看一下狀態")


# --------------------------------------------------------------------------
# handle_message: @-mention / reply-to-bot override the (silent) filter,
# and a real answer gets the "{display name}老師好," politeness prefix.
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_conversation_state() -> None:
    conversation.reset("C-group")


def _mock_display_name(name: str | None = "陳大文"):
    return patch(
        "app.services.codex_agent.line_client.get_group_member_display_name",
        new=AsyncMock(return_value=name),
    )


async def test_mentioned_message_falls_back_to_generic_reply_when_model_says_silent() -> None:
    with patch(
        "app.services.codex_agent.run_model_chain",
        new=AsyncMock(return_value=("(silent)", "gpt-5.5")),
    ), patch(
        "app.services.codex_agent.line_client.send_text", new=AsyncMock(return_value=[])
    ) as send_text, _mock_display_name("陳大文"):
        await codex_agent.handle_message(
            "C-group", "U-1", "reply-token", "@木木昌至秦 哈囉", was_mentioned=True
        )
    send_text.assert_awaited_once_with(
        "reply-token", "C-group", f"陳大文老師好,{codex_agent._MENTION_FALLBACK_REPLY}"
    )


async def test_unmentioned_silent_answer_is_still_suppressed() -> None:
    with patch(
        "app.services.codex_agent.run_model_chain",
        new=AsyncMock(return_value=("(silent)", "gpt-5.5")),
    ), patch(
        "app.services.codex_agent.line_client.send_text", new=AsyncMock(return_value=[])
    ) as send_text:
        await codex_agent.handle_message("C-group", "U-1", "reply-token", "哈哈哈", was_mentioned=False)
    send_text.assert_not_awaited()


async def test_mentioned_message_with_real_answer_is_sent_as_is() -> None:
    with patch(
        "app.services.codex_agent.run_model_chain",
        new=AsyncMock(return_value=("這是總務處的回覆", "gpt-5.5")),
    ), patch(
        "app.services.codex_agent.line_client.send_text", new=AsyncMock(return_value=[])
    ) as send_text, _mock_display_name("陳大文"):
        await codex_agent.handle_message(
            "C-group", "U-1", "reply-token", "@木木昌至秦 馬桶不通", was_mentioned=True
        )
    send_text.assert_awaited_once_with("reply-token", "C-group", "陳大文老師好,這是總務處的回覆")


async def test_reply_to_bot_message_overrides_silent_filter_like_a_mention() -> None:
    with patch(
        "app.services.codex_agent.run_model_chain",
        new=AsyncMock(return_value=("(silent)", "gpt-5.5")),
    ), patch(
        "app.services.codex_agent.line_client.send_text", new=AsyncMock(return_value=[])
    ) as send_text, _mock_display_name("陳大文"):
        await codex_agent.handle_message(
            "C-group", "U-1", "reply-token", "還沒好嗎", was_mentioned=False, is_reply_to_bot=True
        )
    send_text.assert_awaited_once_with(
        "reply-token", "C-group", f"陳大文老師好,{codex_agent._MENTION_FALLBACK_REPLY}"
    )


async def test_display_name_lookup_failure_sends_answer_without_greeting() -> None:
    with patch(
        "app.services.codex_agent.run_model_chain",
        new=AsyncMock(return_value=("這是總務處的回覆", "gpt-5.5")),
    ), patch(
        "app.services.codex_agent.line_client.send_text", new=AsyncMock(return_value=[])
    ) as send_text, _mock_display_name(None):
        await codex_agent.handle_message(
            "C-group", "U-1", "reply-token", "@木木昌至秦 馬桶不通", was_mentioned=True
        )
    send_text.assert_awaited_once_with("reply-token", "C-group", "這是總務處的回覆")


async def test_sent_message_ids_are_recorded_for_reply_to_bot_detection() -> None:
    with patch(
        "app.services.codex_agent.run_model_chain",
        new=AsyncMock(return_value=("這是總務處的回覆", "gpt-5.5")),
    ), patch(
        "app.services.codex_agent.line_client.send_text", new=AsyncMock(return_value=["msg-123"])
    ), _mock_display_name("陳大文"):
        await codex_agent.handle_message(
            "C-group", "U-1", "reply-token", "@木木昌至秦 馬桶不通", was_mentioned=True
        )
    assert conversation.is_reply_to_bot("C-group", "msg-123") is True


# --------------------------------------------------------------------------
# _looks_rate_limited
# --------------------------------------------------------------------------


def test_looks_rate_limited_matches_known_markers() -> None:
    assert codex_agent._looks_rate_limited("Error: 429 Too Many Requests")
    assert codex_agent._looks_rate_limited("quota exceeded for this model")
    assert not codex_agent._looks_rate_limited("some unrelated error")


# --------------------------------------------------------------------------
# _run_rung / run_model_chain: mocked subprocess transport
# --------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, returncode: int, stderr: bytes = b"", hang: bool = False, write_output=None):
        self.returncode = returncode
        self._stderr = stderr
        self._hang = hang
        self._write_output = write_output
        self.killed = False
        self.waited = False

    async def communicate(self):
        if self._hang:
            # Simulate a subprocess that never finishes within the timeout.
            import asyncio

            await asyncio.sleep(10)
        if self._write_output:
            self._write_output()
        return b"", self._stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> None:
        self.waited = True


def _fake_create_subprocess_exec_factory(procs: list[_FakeProc]):
    calls: list[list[str]] = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append(list(args))
        return procs.pop(0)

    return fake_create_subprocess_exec, calls


def _writer(path_index_in_args: int, text: str):
    """Build a callable that writes `text` to the -o path found in argv."""

    def _do_write(args: list[str]):
        idx = args.index("-o")
        with open(args[idx + 1], "w", encoding="utf-8") as fh:
            fh.write(text)

    return _do_write


@pytest.fixture(autouse=True)
def _fast_timeout(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "groupbot_codex_model_timeout_seconds", 0.3)
    monkeypatch.setattr(settings, "groupbot_codex_primary_model_timeout_seconds", 0.1)
    monkeypatch.setattr(settings, "groupbot_codex_model_chain", "gpt-5.5,gpt-5.4-mini")
    yield


async def test_first_rung_success_does_not_try_second_rung(monkeypatch) -> None:
    calls: list[list[str]] = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append(list(args))
        idx = args.index("-o")
        with open(args[idx + 1], "w", encoding="utf-8") as fh:
            fh.write("主要模型回覆")
        return _FakeProc(returncode=0)

    monkeypatch.setattr(codex_agent.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    answer, model_used = await codex_agent.run_model_chain("test prompt")

    assert "--ignore-user-config" in calls[0]

    assert answer == "主要模型回覆"
    assert model_used == "gpt-5.5"
    assert len(calls) == 1


async def test_first_rung_timeout_falls_back_to_second_rung_success(monkeypatch) -> None:
    procs = [_FakeProc(returncode=0, hang=True), _FakeProc(returncode=0)]
    write_targets = iter([None, "備援模型的回覆"])

    async def fake_create_subprocess_exec(*args, **kwargs):
        proc = procs.pop(0)
        text = next(write_targets)
        if text is not None:
            idx = args.index("-o")
            proc._write_output = lambda p=args[idx + 1], t=text: open(p, "w", encoding="utf-8").write(t)
        return proc

    monkeypatch.setattr(codex_agent.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    answer, model_used = await codex_agent.run_model_chain("test prompt")

    assert answer == "備援模型的回覆"
    assert model_used == "gpt-5.4-mini"
    assert procs == []  # both consumed


async def test_timed_out_rung_process_is_killed(monkeypatch) -> None:
    hung_proc = _FakeProc(returncode=0, hang=True)
    ok_proc = _FakeProc(returncode=0)

    def make_ok_writer(args):
        idx = args.index("-o")
        with open(args[idx + 1], "w", encoding="utf-8") as fh:
            fh.write("備援模型的回覆")

    procs_and_writers = [(hung_proc, None), (ok_proc, make_ok_writer)]

    async def fake_create_subprocess_exec(*args, **kwargs):
        proc, writer = procs_and_writers.pop(0)
        if writer:
            writer(args)
        return proc

    monkeypatch.setattr(codex_agent.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    await codex_agent.run_model_chain("test prompt")

    assert hung_proc.killed is True
    assert hung_proc.waited is True  # reaped, not just signalled — no zombie left behind


async def test_exec_error_counts_as_rung_failure(monkeypatch) -> None:
    async def fake_create_subprocess_exec(*args, **kwargs):
        return _FakeProc(returncode=1, stderr=b"boom, some internal error")

    monkeypatch.setattr(codex_agent.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    answer, model_used = await codex_agent.run_model_chain("test prompt")

    assert answer is None
    assert model_used is None


async def test_rate_limited_stderr_counts_as_rung_failure(monkeypatch) -> None:
    async def fake_create_subprocess_exec(*args, **kwargs):
        return _FakeProc(returncode=1, stderr=b"429 Too Many Requests")

    monkeypatch.setattr(codex_agent.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    answer, model_used = await codex_agent.run_model_chain("test prompt")

    assert answer is None
    assert model_used is None


async def test_no_output_file_content_counts_as_no_answer(monkeypatch) -> None:
    async def fake_create_subprocess_exec(*args, **kwargs):
        return _FakeProc(returncode=0)  # never writes the -o file

    monkeypatch.setattr(codex_agent.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    answer, model_used = await codex_agent.run_model_chain("test prompt")

    assert answer is None
    assert model_used is None


async def test_spawn_error_counts_as_rung_failure(monkeypatch) -> None:
    async def fake_create_subprocess_exec(*args, **kwargs):
        raise OSError("codex.exe not found")

    monkeypatch.setattr(codex_agent.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    answer, model_used = await codex_agent.run_model_chain("test prompt")

    assert answer is None
    assert model_used is None


async def test_model_chain_override_is_used_instead_of_settings_default(monkeypatch) -> None:
    async def fake_create_subprocess_exec(*args, **kwargs):
        assert "custom-model" in args
        idx = args.index("-o")
        with open(args[idx + 1], "w", encoding="utf-8") as fh:
            fh.write("管理員模型回覆")
        return _FakeProc(returncode=0)

    monkeypatch.setattr(codex_agent.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    answer, model_used = await codex_agent.run_model_chain(
        "test prompt", model_chain=["custom-model"]
    )

    assert answer == "管理員模型回覆"
    assert model_used == "custom-model"


async def test_admin_rung_uses_danger_full_access_sandbox(monkeypatch) -> None:
    seen_args = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        seen_args.append(args)
        idx = args.index("-o")
        with open(args[idx + 1], "w", encoding="utf-8") as fh:
            fh.write("好的,已處理。")
        return _FakeProc(returncode=0)

    monkeypatch.setattr(codex_agent.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    settings = get_settings()

    await codex_agent.run_model_chain(
        "test prompt",
        sandbox="danger-full-access",
        cwd=settings.groupbot_codex_admin_workdir,
        model_chain=["gpt-5.5"],
    )

    args = seen_args[0]
    assert "danger-full-access" in args
    assert settings.groupbot_codex_admin_workdir in args


async def test_handle_admin_message_uses_admin_chain_and_workdir() -> None:
    settings = get_settings()
    with patch(
        "app.services.codex_agent.run_model_chain",
        new=AsyncMock(return_value=("好的,已處理。", "gpt-5.5")),
    ) as run_chain, patch(
        "app.services.codex_agent.line_client.send_text", new=AsyncMock()
    ):
        await codex_agent.handle_admin_message("U-admin", "reply-token", "幫我看一下狀態")

    _, kwargs = run_chain.call_args
    assert kwargs["model_chain"] == settings.codex_admin_model_chain
    assert kwargs["sandbox"] == "danger-full-access"
    assert kwargs["cwd"] == settings.groupbot_codex_admin_workdir


# --------------------------------------------------------------------------
# _resolve_codex_binary — prefer the vendored native codex.exe over the
# codex.cmd npm shim (found live, 2026-07-22: the .cmd shim's cmd.exe ->
# node.exe -> codex.exe spawn chain re-consoles at the node.exe hop, which
# our own creationflags/startupinfo can't reach since it's a grandchild).
# --------------------------------------------------------------------------


def test_resolve_codex_binary_finds_vendored_exe(tmp_path, monkeypatch) -> None:
    codex_agent._RESOLVED_BIN_CACHE.clear()
    npm_root = tmp_path / "npm"
    vendor_bin = (
        npm_root
        / "node_modules"
        / "@openai"
        / "codex"
        / "node_modules"
        / "@openai"
        / "codex-win32-x64"
        / "vendor"
        / "x86_64-pc-windows-msvc"
        / "bin"
    )
    vendor_bin.mkdir(parents=True)
    exe_path = vendor_bin / "codex.exe"
    exe_path.write_text("fake binary")

    configured = str(npm_root / "codex.cmd")
    resolved = codex_agent._resolve_codex_binary(configured)

    assert resolved == str(exe_path)


def test_resolve_codex_binary_falls_back_when_vendor_missing(tmp_path) -> None:
    codex_agent._RESOLVED_BIN_CACHE.clear()
    configured = str(tmp_path / "npm" / "codex.cmd")

    resolved = codex_agent._resolve_codex_binary(configured)

    assert resolved == configured


def test_resolve_codex_binary_caches_result(tmp_path) -> None:
    codex_agent._RESOLVED_BIN_CACHE.clear()
    configured = str(tmp_path / "npm" / "codex.cmd")

    first = codex_agent._resolve_codex_binary(configured)
    assert configured in codex_agent._RESOLVED_BIN_CACHE
    second = codex_agent._resolve_codex_binary(configured)

    assert first == second
