"""Tests for LINE webhook HMAC signature verification."""

import base64
import hashlib
import hmac

from app.utils.line_signature import verify_line_signature

SECRET = "test-channel-secret"


def _sign(body: bytes, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def test_valid_signature_accepted() -> None:
    body = b'{"events":[]}'
    assert verify_line_signature(SECRET, body, _sign(body)) is True


def test_wrong_signature_rejected() -> None:
    body = b'{"events":[]}'
    assert verify_line_signature(SECRET, body, "totally-forged") is False


def test_signature_for_different_body_rejected() -> None:
    body = b'{"events":[]}'
    other_signature = _sign(b'{"events":[{"tampered":true}]}')
    assert verify_line_signature(SECRET, body, other_signature) is False


def test_empty_secret_or_signature_rejected() -> None:
    body = b'{"events":[]}'
    assert verify_line_signature("", body, _sign(body)) is False
    assert verify_line_signature(SECRET, body, "") is False
