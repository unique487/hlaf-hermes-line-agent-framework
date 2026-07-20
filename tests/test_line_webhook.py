"""Tests for the LINE webhook endpoint."""

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
from app.utils.line_signature import verify_line_signature

CHANNEL_SECRET = "test-channel-secret"

client = TestClient(app)


@pytest.fixture(autouse=True)
def _configure(tmp_path, monkeypatch) -> Iterator[None]:
    """Point settings at a test secret and an empty temp allowlist."""
    settings = get_settings()
    monkeypatch.setattr(settings, "line_channel_secret", CHANNEL_SECRET)
    monkeypatch.setattr(settings, "allowed_line_user_ids", "")
    monkeypatch.setattr(settings, "allowlist_file", str(tmp_path / "allowlist.json"))
    yield


def _sign(body: bytes) -> str:
    digest = hmac.new(CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def _text_event_payload(user_id: str, text: str) -> bytes:
    return json.dumps(
        {
            "events": [
                {
                    "type": "message",
                    "replyToken": "reply-token-1",
                    "source": {"type": "user", "userId": user_id},
                    "message": {"type": "text", "text": text},
                }
            ]
        }
    ).encode()


def test_signature_helper_roundtrip() -> None:
    body = b'{"events":[]}'
    assert verify_line_signature(CHANNEL_SECRET, body, _sign(body))
    assert not verify_line_signature(CHANNEL_SECRET, body, "bad-signature")
    assert not verify_line_signature("", body, _sign(body))


def test_webhook_rejects_invalid_signature() -> None:
    resp = client.post(
        "/line/webhook",
        content=b'{"events":[]}',
        headers={"X-Line-Signature": "forged"},
    )
    assert resp.status_code == 400


def test_webhook_accepts_empty_events() -> None:
    body = b'{"events":[]}'
    resp = client.post("/line/webhook", content=body, headers={"X-Line-Signature": _sign(body)})
    assert resp.status_code == 200


def test_first_contact_captures_admin_and_greets() -> None:
    body = _text_event_payload("U-admin", "哈囉")
    with patch("app.api.line_webhook.line_client.send_text", new=AsyncMock()) as send:
        resp = client.post("/line/webhook", content=body, headers={"X-Line-Signature": _sign(body)})
    assert resp.status_code == 200
    send.assert_awaited_once()
    assert "管理員" in send.await_args.args[2]

    from app.services import allowlist

    assert allowlist.is_allowed("U-admin")
    assert not allowlist.is_allowed("U-stranger")


def test_allowed_user_message_reaches_hermes() -> None:
    from app.services import allowlist

    allowlist.capture_first_admin("U-admin")
    body = _text_event_payload("U-admin", "你好 Hermes")
    with (
        patch(
            "app.api.line_webhook.hermes_agent.ask_hermes",
            new=AsyncMock(return_value="你好!"),
        ) as ask,
        patch("app.api.line_webhook.line_client.send_text", new=AsyncMock()) as send,
    ):
        resp = client.post("/line/webhook", content=body, headers={"X-Line-Signature": _sign(body)})
    assert resp.status_code == 200
    ask.assert_awaited_once_with("U-admin", "你好 Hermes")
    send.assert_awaited_once_with("reply-token-1", "U-admin", "你好!")


def test_non_allowlisted_user_is_ignored() -> None:
    from app.services import allowlist

    allowlist.capture_first_admin("U-admin")
    body = _text_event_payload("U-stranger", "在嗎?")
    with (
        patch("app.api.line_webhook.hermes_agent.ask_hermes", new=AsyncMock()) as ask,
        patch("app.api.line_webhook.line_client.send_text", new=AsyncMock()) as send,
    ):
        resp = client.post("/line/webhook", content=body, headers={"X-Line-Signature": _sign(body)})
    assert resp.status_code == 200
    ask.assert_not_awaited()
    send.assert_not_awaited()


def test_follow_before_any_admin_is_silent() -> None:
    body = json.dumps(
        {
            "events": [
                {
                    "type": "follow",
                    "replyToken": "reply-token-3",
                    "source": {"type": "user", "userId": "U-first-ever"},
                }
            ]
        }
    ).encode()
    with patch("app.api.line_webhook.line_client.send_text", new=AsyncMock()) as send:
        resp = client.post("/line/webhook", content=body, headers={"X-Line-Signature": _sign(body)})
    assert resp.status_code == 200
    send.assert_not_awaited()


def test_follow_after_admin_exists_sends_decline() -> None:
    from app.services import allowlist

    allowlist.capture_first_admin("U-admin")
    body = json.dumps(
        {
            "events": [
                {
                    "type": "follow",
                    "replyToken": "reply-token-4",
                    "source": {"type": "user", "userId": "U-new-follower"},
                }
            ]
        }
    ).encode()
    with patch("app.api.line_webhook.line_client.send_text", new=AsyncMock()) as send:
        resp = client.post("/line/webhook", content=body, headers={"X-Line-Signature": _sign(body)})
    assert resp.status_code == 200
    send.assert_awaited_once()
    assert send.await_args.args[0] == "reply-token-4"
    assert send.await_args.args[1] == "U-new-follower"
    assert "僅開放特定使用者" in send.await_args.args[2]


def test_group_messages_are_skipped() -> None:
    body = json.dumps(
        {
            "events": [
                {
                    "type": "message",
                    "replyToken": "reply-token-2",
                    "source": {"type": "group", "groupId": "G1", "userId": "U-admin"},
                    "message": {"type": "text", "text": "群組訊息"},
                }
            ]
        }
    ).encode()
    with patch("app.api.line_webhook.line_client.send_text", new=AsyncMock()) as send:
        resp = client.post("/line/webhook", content=body, headers={"X-Line-Signature": _sign(body)})
    assert resp.status_code == 200
    send.assert_not_awaited()
