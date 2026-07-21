"""Tests for the opencode fallback-chain brain (talks to opencode serve over HTTP)."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.config import get_settings
from app.services import opencode_agent


# --------------------------------------------------------------------------
# should_suppress / (silent) protocol
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
    assert opencode_agent.should_suppress(text) is True


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
    assert opencode_agent.should_suppress(text) is False


# --------------------------------------------------------------------------
# build_prompt
# --------------------------------------------------------------------------


def test_build_prompt_with_no_history() -> None:
    prompt = opencode_agent.build_prompt([], "使用者A", "早安")
    assert "(尚無對話紀錄)" in prompt
    assert "[使用者A]: 早安" in prompt


def test_build_prompt_with_history() -> None:
    history = [("使用者A", "哈囉"), ("機器人", "您好")]
    prompt = opencode_agent.build_prompt(history, "使用者B", "馬桶不通")
    assert "[使用者A]: 哈囉" in prompt
    assert "[機器人]: 您好" in prompt
    assert "[使用者B]: 馬桶不通" in prompt
    assert prompt.index("哈囉") < prompt.index("馬桶不通")


def test_build_prompt_without_mention_has_no_mention_note() -> None:
    prompt = opencode_agent.build_prompt([], "使用者A", "早安")
    assert "@" not in prompt


def test_build_prompt_with_mention_appends_instruction() -> None:
    prompt = opencode_agent.build_prompt([], "使用者A", "在嗎", was_mentioned=True)
    assert "不要輸出 (silent)" in prompt


# --------------------------------------------------------------------------
# handle_message: @-mention overrides the (silent) filter
# --------------------------------------------------------------------------


async def test_mentioned_message_falls_back_to_generic_reply_when_model_says_silent() -> None:
    with patch(
        "app.services.opencode_agent.run_model_chain",
        new=AsyncMock(return_value=("(silent)", "opencode/big-pickle")),
    ), patch(
        "app.services.opencode_agent.line_client.send_text", new=AsyncMock()
    ) as send_text:
        await opencode_agent.handle_message(
            "C-group", "U-1", "reply-token", "@木木昌至秦 哈囉", was_mentioned=True
        )
    send_text.assert_awaited_once_with("reply-token", "C-group", opencode_agent._MENTION_FALLBACK_REPLY)


async def test_unmentioned_silent_answer_is_still_suppressed() -> None:
    with patch(
        "app.services.opencode_agent.run_model_chain",
        new=AsyncMock(return_value=("(silent)", "opencode/big-pickle")),
    ), patch(
        "app.services.opencode_agent.line_client.send_text", new=AsyncMock()
    ) as send_text:
        await opencode_agent.handle_message("C-group", "U-1", "reply-token", "哈哈哈", was_mentioned=False)
    send_text.assert_not_awaited()


async def test_mentioned_message_with_real_answer_is_sent_as_is() -> None:
    with patch(
        "app.services.opencode_agent.run_model_chain",
        new=AsyncMock(return_value=("這是總務處的回覆", "opencode/big-pickle")),
    ), patch(
        "app.services.opencode_agent.line_client.send_text", new=AsyncMock()
    ) as send_text:
        await opencode_agent.handle_message(
            "C-group", "U-1", "reply-token", "@木木昌至秦 馬桶不通", was_mentioned=True
        )
    send_text.assert_awaited_once_with("reply-token", "C-group", "這是總務處的回覆")


# --------------------------------------------------------------------------
# _split_provider_model / _extract_answer
# --------------------------------------------------------------------------


def test_split_provider_model_simple() -> None:
    assert opencode_agent._split_provider_model("opencode/big-pickle") == ("opencode", "big-pickle")


def test_split_provider_model_nested_model_id() -> None:
    assert opencode_agent._split_provider_model("nvidia/deepseek-ai/deepseek-v4-flash") == (
        "nvidia",
        "deepseek-ai/deepseek-v4-flash",
    )


def test_extract_answer_concatenates_text_parts_in_order() -> None:
    parts = [
        {"type": "step-start"},
        {"type": "reasoning", "text": "思考中…"},
        {"type": "text", "text": "收到,"},
        {"type": "text", "text": "總務處會盡快派人檢修。"},
        {"type": "step-finish"},
    ]
    assert opencode_agent._extract_answer(parts) == "收到,總務處會盡快派人檢修。"


def test_extract_answer_returns_none_when_no_text_parts() -> None:
    assert opencode_agent._extract_answer([{"type": "reasoning", "text": "只有思考"}]) is None
    assert opencode_agent._extract_answer([]) is None


# --------------------------------------------------------------------------
# run_model_chain: mocked opencode-serve HTTP transport
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fast_timeout(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "groupbot_model_timeout_seconds", 0.2)
    monkeypatch.setattr(settings, "groupbot_primary_model_timeout_seconds", 0.2)
    monkeypatch.setattr(settings, "groupbot_timeout_recovery_delay_seconds", 0.0)
    monkeypatch.setattr(
        settings,
        "groupbot_model_chain",
        "opencode/big-pickle,opencode/deepseek-v4-flash-free",
    )
    yield


def _ok(text: str):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"info": {}, "parts": [{"type": "text", "text": text}]})

    return handler


def _http_error(status: int, body: str = "boom"):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body)

    return handler


def _rate_limited():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="Rate limit exceeded: free-models-per-day")

    return handler


def _hangs():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    return handler


class _ScriptedTransport(httpx.AsyncBaseTransport):
    """Fake opencode-serve backend: /session and /session/{id}/abort|delete
    always succeed; /session/{id}/message pops one scripted handler per call
    (in call order) and records the requested model.
    """

    def __init__(self, message_handlers: list) -> None:
        self._handlers = list(message_handlers)
        self.requested_models: list[str] = []
        self._next_session = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/session":
            self._next_session += 1
            return httpx.Response(200, json={"id": f"ses_{self._next_session}"})
        if request.method == "DELETE" or path.endswith("/abort"):
            return httpx.Response(200, json={})
        if request.method == "POST" and path.endswith("/message"):
            body = httpx.Request.read(request)
            import json as _json

            model = _json.loads(body)["model"]
            self.requested_models.append(f"{model['providerID']}/{model['modelID']}")
            handler = self._handlers.pop(0)
            return await handler(request)
        raise AssertionError(f"unexpected request {request.method} {path}")


_RealAsyncClient = httpx.AsyncClient


def _patch_transport(monkeypatch, transport: _ScriptedTransport) -> None:
    def factory(*, base_url):
        return _RealAsyncClient(base_url=base_url, transport=transport)

    monkeypatch.setattr(opencode_agent.httpx, "AsyncClient", factory)


async def test_first_rung_timeout_falls_back_to_second_rung_success(monkeypatch) -> None:
    transport = _ScriptedTransport([_hangs(), _ok("備援模型的回覆")])
    _patch_transport(monkeypatch, transport)

    answer, model_used = await opencode_agent.run_model_chain("test prompt")

    assert answer == "備援模型的回覆"
    assert model_used == "opencode/deepseek-v4-flash-free"


async def test_all_rungs_failing_returns_none(monkeypatch) -> None:
    transport = _ScriptedTransport([_http_error(500), _rate_limited()])
    _patch_transport(monkeypatch, transport)

    answer, model_used = await opencode_agent.run_model_chain("test prompt")

    assert answer is None
    assert model_used is None


async def test_timeout_triggers_recovery_delay_before_next_rung(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "groupbot_timeout_recovery_delay_seconds", 1.5)
    transport = _ScriptedTransport([_hangs(), _ok("備援模型的回覆")])
    _patch_transport(monkeypatch, transport)
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(opencode_agent.asyncio, "sleep", fake_sleep)

    await opencode_agent.run_model_chain("test prompt")

    assert sleep_calls == [1.5]


async def test_non_timeout_failure_does_not_trigger_recovery_delay(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "groupbot_timeout_recovery_delay_seconds", 1.5)
    transport = _ScriptedTransport([_http_error(500), _ok("備援模型的回覆")])
    _patch_transport(monkeypatch, transport)
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(opencode_agent.asyncio, "sleep", fake_sleep)

    await opencode_agent.run_model_chain("test prompt")

    assert sleep_calls == []


async def test_first_rung_success_does_not_try_second_rung(monkeypatch) -> None:
    transport = _ScriptedTransport([_ok("主要模型回覆")])
    _patch_transport(monkeypatch, transport)

    answer, model_used = await opencode_agent.run_model_chain("test prompt")

    assert answer == "主要模型回覆"
    assert model_used == "opencode/big-pickle"
    assert transport.requested_models == ["opencode/big-pickle"]


async def test_server_unreachable_counts_as_rung_failure(monkeypatch) -> None:
    class _BrokenTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

    def factory(*, base_url):
        return _RealAsyncClient(base_url=base_url, transport=_BrokenTransport())

    monkeypatch.setattr(opencode_agent.httpx, "AsyncClient", factory)

    answer, model_used = await opencode_agent.run_model_chain("test prompt")

    assert answer is None
    assert model_used is None


# --------------------------------------------------------------------------
# run_model_chain: admin-only model_chain / primary_timeout_seconds overrides
# --------------------------------------------------------------------------


async def test_model_chain_override_is_used_instead_of_settings_default(monkeypatch) -> None:
    transport = _ScriptedTransport([_ok("管理員模型回覆")])
    _patch_transport(monkeypatch, transport)

    answer, model_used = await opencode_agent.run_model_chain(
        "test prompt", model_chain=["opencode/deepseek-v4-flash-free"]
    )

    assert answer == "管理員模型回覆"
    assert model_used == "opencode/deepseek-v4-flash-free"
    assert transport.requested_models == ["opencode/deepseek-v4-flash-free"]


async def test_primary_timeout_override_applies_to_rung_zero_only(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "groupbot_model_timeout_seconds", 0.2)
    transport = _ScriptedTransport([_hangs(), _ok("第二順位回覆")])
    _patch_transport(monkeypatch, transport)
    seen_timeouts: list[float] = []

    real_run_rung = opencode_agent._run_rung

    async def spy(client, settings_, model, prompt, *, agent, timeout_seconds, cwd):
        seen_timeouts.append(timeout_seconds)
        return await real_run_rung(
            client, settings_, model, prompt, agent=agent, timeout_seconds=timeout_seconds, cwd=cwd
        )

    monkeypatch.setattr(opencode_agent, "_run_rung", spy)

    answer, model_used = await opencode_agent.run_model_chain(
        "test prompt", primary_timeout_seconds=0.05
    )

    assert answer == "第二順位回覆"
    assert model_used == "opencode/deepseek-v4-flash-free"
    assert seen_timeouts[0] == 0.05
    assert seen_timeouts[1] == 0.2


async def test_handle_admin_message_uses_admin_chain_and_primary_timeout() -> None:
    settings = get_settings()
    with patch(
        "app.services.opencode_agent.run_model_chain", new=AsyncMock(return_value=("好的,已處理。", "opencode/deepseek-v4-flash-free"))
    ) as run_chain, patch(
        "app.services.opencode_agent.line_client.send_text", new=AsyncMock()
    ):
        await opencode_agent.handle_admin_message("U-admin", "reply-token", "幫我看一下狀態")

    _, kwargs = run_chain.call_args
    assert kwargs["model_chain"] == settings.admin_model_chain
    assert kwargs["primary_timeout_seconds"] == settings.groupbot_admin_primary_model_timeout_seconds
