"""Tests for the LINE client's reply -> push fallback."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services import line_client


async def test_send_text_uses_reply_when_it_succeeds() -> None:
    with (
        patch("app.services.line_client.reply_text", new=AsyncMock(return_value=["m1"])) as reply,
        patch("app.services.line_client.push_text", new=AsyncMock(return_value=["m2"])) as push,
    ):
        result = await line_client.send_text("reply-token", "G1", "回覆內容")

    reply.assert_awaited_once_with("reply-token", "回覆內容")
    push.assert_not_awaited()
    assert result == ["m1"]


async def test_send_text_falls_back_to_push_when_reply_fails() -> None:
    """The fallback chain can take a while (multiple 45s-timeout rungs), so
    by the time we have an answer the LINE replyToken (~1 minute TTL) may
    have expired. In that case we must push directly to the group."""
    with (
        patch("app.services.line_client.reply_text", new=AsyncMock(return_value=None)) as reply,
        patch("app.services.line_client.push_text", new=AsyncMock(return_value=["m2"])) as push,
    ):
        result = await line_client.send_text("expired-token", "G1", "回覆內容")

    reply.assert_awaited_once_with("expired-token", "回覆內容")
    push.assert_awaited_once_with("G1", "回覆內容")
    assert result == ["m2"]


# --------------------------------------------------------------------------
# reply_text / push_text: a network exception must be swallowed into a
# `None` return, not raised — otherwise it propagates out of send_text and
# skips the push fallback entirely (confirmed 2026-07-21: a real admin DM
# got a correct model answer that was then silently lost because
# reply_text raised httpx.ConnectTimeout during a transient network blip
# instead of returning None).
# --------------------------------------------------------------------------


class _RaisingTransport(httpx.AsyncBaseTransport):
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise self._exc


_RealAsyncClient = httpx.AsyncClient


def _patch_httpx_client(monkeypatch, exc: Exception) -> None:
    def factory(*, timeout):
        return _RealAsyncClient(timeout=timeout, transport=_RaisingTransport(exc))

    monkeypatch.setattr(line_client.httpx, "AsyncClient", factory)


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectTimeout("timed out"),
        httpx.ConnectError("connection refused"),
    ],
)
async def test_reply_text_returns_none_on_network_error_instead_of_raising(monkeypatch, exc) -> None:
    _patch_httpx_client(monkeypatch, exc)
    assert await line_client.reply_text("reply-token", "回覆內容") is None


async def test_push_text_returns_none_on_network_error_instead_of_raising(monkeypatch) -> None:
    _patch_httpx_client(monkeypatch, httpx.ConnectTimeout("timed out"))
    assert await line_client.push_text("G1", "回覆內容") is None


async def test_send_text_falls_back_to_push_when_reply_raises_network_error(monkeypatch) -> None:
    """End-to-end: reply_text's real implementation (not mocked) hits a
    network error, send_text must still try push_text instead of letting
    the exception escape."""
    _patch_httpx_client(monkeypatch, httpx.ConnectTimeout("timed out"))
    with patch("app.services.line_client.push_text", new=AsyncMock(return_value=["m2"])) as push:
        await line_client.send_text("reply-token", "G1", "回覆內容")
    push.assert_awaited_once_with("G1", "回覆內容")


# --------------------------------------------------------------------------
# reply_text / push_text: extracting sent message IDs from a real LINE
# response body, so callers can remember which IDs the bot just sent (see
# app/services/conversation.py's record_bot_message_id).
# --------------------------------------------------------------------------


def _json_transport(json_body: dict) -> httpx.AsyncBaseTransport:
    class _Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=json_body)

    return _Transport()


async def test_reply_text_returns_sent_message_ids(monkeypatch) -> None:
    def factory(*, timeout):
        return _RealAsyncClient(
            timeout=timeout,
            transport=_json_transport({"sentMessages": [{"id": "msg-1", "quoteToken": "q1"}]}),
        )

    monkeypatch.setattr(line_client.httpx, "AsyncClient", factory)
    assert await line_client.reply_text("reply-token", "回覆內容") == ["msg-1"]


async def test_push_text_returns_sent_message_ids(monkeypatch) -> None:
    def factory(*, timeout):
        return _RealAsyncClient(
            timeout=timeout,
            transport=_json_transport({"sentMessages": [{"id": "msg-2"}]}),
        )

    monkeypatch.setattr(line_client.httpx, "AsyncClient", factory)
    assert await line_client.push_text("G1", "回覆內容") == ["msg-2"]


# --------------------------------------------------------------------------
# get_group_member_display_name
# --------------------------------------------------------------------------


async def test_get_group_member_display_name_returns_name_on_success(monkeypatch) -> None:
    def factory(*, timeout):
        return _RealAsyncClient(
            timeout=timeout,
            transport=_json_transport({"displayName": "陳大文", "userId": "U-1"}),
        )

    monkeypatch.setattr(line_client.httpx, "AsyncClient", factory)
    assert await line_client.get_group_member_display_name("G1", "U-1") == "陳大文"


async def test_get_group_member_display_name_returns_none_on_network_error(monkeypatch) -> None:
    _patch_httpx_client(monkeypatch, httpx.ConnectTimeout("timed out"))
    assert await line_client.get_group_member_display_name("G1", "U-1") is None


async def test_get_group_member_display_name_returns_none_on_http_error(monkeypatch) -> None:
    def factory(*, timeout):
        return _RealAsyncClient(timeout=timeout, transport=_http_error_transport(404, "not found"))

    monkeypatch.setattr(line_client.httpx, "AsyncClient", factory)
    assert await line_client.get_group_member_display_name("G1", "U-1") is None


def _http_error_transport(status: int, body: str) -> httpx.AsyncBaseTransport:
    class _Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, text=body)

    return _Transport()
