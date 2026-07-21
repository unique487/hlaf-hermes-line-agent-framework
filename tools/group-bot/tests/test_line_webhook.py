"""Integration tests for the LINE webhook endpoint."""

import base64
import hashlib
import hmac
import json
from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app

CHANNEL_SECRET = "test-channel-secret"
ALLOWED_GROUP = "C-allowed-group"
ADMIN_USER = "U-admin"

client = TestClient(app)


@pytest.fixture(autouse=True)
def _configure(monkeypatch) -> Iterator[None]:
    settings = get_settings()
    monkeypatch.setattr(settings, "line_channel_secret", CHANNEL_SECRET)
    monkeypatch.setattr(settings, "line_allowed_groups", ALLOWED_GROUP)
    monkeypatch.setattr(settings, "line_allowed_users", ADMIN_USER)
    yield


def _sign(body: bytes) -> str:
    digest = hmac.new(CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def _group_text_event(group_id: str, user_id: str, text: str, mention: dict | None = None) -> bytes:
    message: dict = {"type": "text", "text": text}
    if mention is not None:
        message["mention"] = mention
    return json.dumps(
        {
            "events": [
                {
                    "type": "message",
                    "replyToken": "reply-token-1",
                    "source": {"type": "group", "groupId": group_id, "userId": user_id},
                    "message": message,
                }
            ]
        }
    ).encode()


def _dm_text_event(user_id: str, text: str) -> bytes:
    return json.dumps(
        {
            "events": [
                {
                    "type": "message",
                    "replyToken": "reply-token-2",
                    "source": {"type": "user", "userId": user_id},
                    "message": {"type": "text", "text": text},
                }
            ]
        }
    ).encode()


def test_webhook_rejects_invalid_signature() -> None:
    resp = client.post(
        "/line/webhook", content=b'{"events":[]}', headers={"X-Line-Signature": "forged"}
    )
    assert resp.status_code == 400


def test_webhook_rejects_missing_signature() -> None:
    resp = client.post("/line/webhook", content=b'{"events":[]}')
    assert resp.status_code == 400


def test_webhook_accepts_empty_events() -> None:
    body = b'{"events":[]}'
    resp = client.post("/line/webhook", content=body, headers={"X-Line-Signature": _sign(body)})
    assert resp.status_code == 200


def test_allowed_group_message_dispatches_to_opencode_agent() -> None:
    body = _group_text_event(ALLOWED_GROUP, "U-1", "三樓廁所馬桶不通")
    with patch(
        "app.api.line_webhook.opencode_agent.handle_message", new=AsyncMock()
    ) as handle:
        resp = client.post("/line/webhook", content=body, headers={"X-Line-Signature": _sign(body)})
    assert resp.status_code == 200
    handle.assert_awaited_once_with(ALLOWED_GROUP, "U-1", "reply-token-1", "三樓廁所馬桶不通", False)


def test_self_mentioned_group_message_dispatches_with_was_mentioned_true() -> None:
    mention = {"mentionees": [{"index": 0, "length": 6, "userId": "Ubot", "type": "user", "isSelf": True}]}
    body = _group_text_event(ALLOWED_GROUP, "U-1", "@木木昌至秦 在嗎", mention=mention)
    with patch(
        "app.api.line_webhook.opencode_agent.handle_message", new=AsyncMock()
    ) as handle:
        resp = client.post("/line/webhook", content=body, headers={"X-Line-Signature": _sign(body)})
    assert resp.status_code == 200
    handle.assert_awaited_once_with(ALLOWED_GROUP, "U-1", "reply-token-1", "@木木昌至秦 在嗎", True)


def test_mention_of_other_user_does_not_set_was_mentioned() -> None:
    mention = {"mentionees": [{"index": 0, "length": 3, "userId": "U-someone-else", "type": "user", "isSelf": False}]}
    body = _group_text_event(ALLOWED_GROUP, "U-1", "@小明 在嗎", mention=mention)
    with patch(
        "app.api.line_webhook.opencode_agent.handle_message", new=AsyncMock()
    ) as handle:
        resp = client.post("/line/webhook", content=body, headers={"X-Line-Signature": _sign(body)})
    assert resp.status_code == 200
    handle.assert_awaited_once_with(ALLOWED_GROUP, "U-1", "reply-token-1", "@小明 在嗎", False)


def test_at_all_broadcast_is_never_dispatched() -> None:
    mention = {"mentionees": [{"index": 0, "length": 4, "type": "all"}]}
    body = _group_text_event(ALLOWED_GROUP, "U-1", "@所有人 開會囉", mention=mention)
    with patch(
        "app.api.line_webhook.opencode_agent.handle_message", new=AsyncMock()
    ) as handle:
        resp = client.post("/line/webhook", content=body, headers={"X-Line-Signature": _sign(body)})
    assert resp.status_code == 200
    handle.assert_not_awaited()


def test_unlisted_group_message_is_silently_ignored() -> None:
    body = _group_text_event("C-stranger-group", "U-1", "在嗎?")
    with patch(
        "app.api.line_webhook.opencode_agent.handle_message", new=AsyncMock()
    ) as handle:
        resp = client.post("/line/webhook", content=body, headers={"X-Line-Signature": _sign(body)})
    assert resp.status_code == 200
    handle.assert_not_awaited()


def test_non_admin_direct_message_is_silently_ignored() -> None:
    body = _dm_text_event("U-stranger", "你好")
    with patch(
        "app.api.line_webhook.opencode_agent.handle_message", new=AsyncMock()
    ) as handle, patch(
        "app.api.line_webhook.opencode_agent.handle_admin_message", new=AsyncMock()
    ) as handle_admin:
        resp = client.post("/line/webhook", content=body, headers={"X-Line-Signature": _sign(body)})
    assert resp.status_code == 200
    handle.assert_not_awaited()
    handle_admin.assert_not_awaited()


def test_admin_direct_message_dispatches_to_admin_agent() -> None:
    body = _dm_text_event(ADMIN_USER, "幫我查一下今天天氣")
    with patch(
        "app.api.line_webhook.opencode_agent.handle_admin_message", new=AsyncMock()
    ) as handle_admin:
        resp = client.post("/line/webhook", content=body, headers={"X-Line-Signature": _sign(body)})
    assert resp.status_code == 200
    handle_admin.assert_awaited_once_with(ADMIN_USER, "reply-token-2", "幫我查一下今天天氣")


def test_non_text_message_in_allowed_group_is_ignored() -> None:
    body = json.dumps(
        {
            "events": [
                {
                    "type": "message",
                    "replyToken": "reply-token-3",
                    "source": {"type": "group", "groupId": ALLOWED_GROUP, "userId": "U-1"},
                    "message": {"type": "sticker", "stickerId": "1"},
                }
            ]
        }
    ).encode()
    with patch(
        "app.api.line_webhook.opencode_agent.handle_message", new=AsyncMock()
    ) as handle:
        resp = client.post("/line/webhook", content=body, headers={"X-Line-Signature": _sign(body)})
    assert resp.status_code == 200
    handle.assert_not_awaited()
