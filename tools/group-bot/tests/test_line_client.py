"""Tests for the LINE client's reply -> push fallback."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services import line_client


async def test_send_text_uses_reply_when_it_succeeds() -> None:
    with (
        patch("app.services.line_client.reply_text", new=AsyncMock(return_value=True)) as reply,
        patch("app.services.line_client.push_text", new=AsyncMock(return_value=True)) as push,
    ):
        await line_client.send_text("reply-token", "G1", "回覆內容")

    reply.assert_awaited_once_with("reply-token", "回覆內容")
    push.assert_not_awaited()


async def test_send_text_falls_back_to_push_when_reply_fails() -> None:
    """The fallback chain can take a while (multiple 45s-timeout rungs), so
    by the time we have an answer the LINE replyToken (~1 minute TTL) may
    have expired. In that case we must push directly to the group."""
    with (
        patch("app.services.line_client.reply_text", new=AsyncMock(return_value=False)) as reply,
        patch("app.services.line_client.push_text", new=AsyncMock(return_value=True)) as push,
    ):
        await line_client.send_text("expired-token", "G1", "回覆內容")

    reply.assert_awaited_once_with("expired-token", "回覆內容")
    push.assert_awaited_once_with("G1", "回覆內容")


# --------------------------------------------------------------------------
# reply_text / push_text: a network exception must be swallowed into a
# `False` return, not raised — otherwise it propagates out of send_text and
# skips the push fallback entirely (confirmed 2026-07-21: a real admin DM
# got a correct model answer that was then silently lost because
# reply_text raised httpx.ConnectTimeout during a transient network blip
# instead of returning False).
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
async def test_reply_text_returns_false_on_network_error_instead_of_raising(monkeypatch, exc) -> None:
    _patch_httpx_client(monkeypatch, exc)
    assert await line_client.reply_text("reply-token", "回覆內容") is False


async def test_push_text_returns_false_on_network_error_instead_of_raising(monkeypatch) -> None:
    _patch_httpx_client(monkeypatch, httpx.ConnectTimeout("timed out"))
    assert await line_client.push_text("G1", "回覆內容") is False


async def test_send_text_falls_back_to_push_when_reply_raises_network_error(monkeypatch) -> None:
    """End-to-end: reply_text's real implementation (not mocked) hits a
    network error, send_text must still try push_text instead of letting
    the exception escape."""
    _patch_httpx_client(monkeypatch, httpx.ConnectTimeout("timed out"))
    with patch("app.services.line_client.push_text", new=AsyncMock(return_value=True)) as push:
        await line_client.send_text("reply-token", "G1", "回覆內容")
    push.assert_awaited_once_with("G1", "回覆內容")
