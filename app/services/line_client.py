"""Minimal LINE Messaging API client (reply / push)."""

import httpx

from app.config import get_settings
from app.logger import logger
from app.services.task_state import CONFIRM_NO, CONFIRM_YES

LINE_API_BASE = "https://api.line.me/v2/bot"
# Binary message content (images/video/audio/files) is served from a
# separate host than the rest of the Messaging API.
LINE_DATA_API_BASE = "https://api-data.line.me/v2/bot"
# LINE text messages are capped at 5000 characters.
MAX_TEXT_LEN = 5000
# LINE confirm template: text max 240 chars, button labels max 20 chars.
MAX_CONFIRM_TEXT_LEN = 240


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {get_settings().line_channel_access_token}",
        "Content-Type": "application/json",
    }


def _text_messages(text: str) -> list[dict[str, str]]:
    text = text.strip() or "（空白回覆）"
    return [{"type": "text", "text": text[:MAX_TEXT_LEN]}]


async def reply_text(reply_token: str, text: str) -> bool:
    """Reply using a webhook replyToken. Returns True on success, False on
    any failure — including network errors (timeout/connect/etc), not just
    a non-200 status. A raised httpx exception here used to propagate out
    of send_text and skip the push_text fallback entirely: a transient
    network blip to api.line.me meant an already-computed answer was
    silently lost instead of falling back to push (confirmed 2026-07-21:
    this exact traceback — httpx.ConnectTimeout escaping reply_text — was
    the reason this account stopped responding; same bug fixed in the
    sibling groupbot project's line_client.py).
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{LINE_API_BASE}/message/reply",
                headers=_headers(),
                json={"replyToken": reply_token, "messages": _text_messages(text)},
            )
    except httpx.HTTPError as exc:
        logger.warning(f"LINE reply request failed: {exc!r}")
        return False
    if resp.status_code == 200:
        return True
    logger.warning(f"LINE reply failed ({resp.status_code}): {resp.text}")
    return False


async def push_text(user_id: str, text: str) -> bool:
    """Push a message directly to a user (fallback when replyToken expired
    or reply_text hit a network error). Same reasoning as reply_text above:
    a network exception here must not propagate — it's already the last
    fallback, so the caller only needs to know push succeeded or not.
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{LINE_API_BASE}/message/push",
                headers=_headers(),
                json={"to": user_id, "messages": _text_messages(text)},
            )
    except httpx.HTTPError as exc:
        logger.error(f"LINE push request failed: {exc!r}")
        return False
    if resp.status_code == 200:
        return True
    logger.error(f"LINE push failed ({resp.status_code}): {resp.text}")
    return False


async def send_text(reply_token: str, user_id: str, text: str) -> None:
    """Reply first; if the token is no longer valid, fall back to push."""
    if not await reply_text(reply_token, text):
        await push_text(user_id, text)


async def get_message_content(message_id: str) -> tuple[bytes, str]:
    """Download a message's binary content (e.g. an image). Raises on failure.

    Returns (content_bytes, content_type) — content_type drives the saved
    file's extension since LINE doesn't expose an original filename.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{LINE_DATA_API_BASE}/message/{message_id}/content",
            headers={"Authorization": f"Bearer {get_settings().line_channel_access_token}"},
        )
    resp.raise_for_status()
    return resp.content, resp.headers.get("Content-Type", "application/octet-stream")


async def push_confirm(user_id: str, description: str) -> bool:
    """Push a Yes/No confirm-template message; buttons send fixed reply text."""
    text = (description.strip() or "需要確認")[:MAX_CONFIRM_TEXT_LEN]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{LINE_API_BASE}/message/push",
                headers=_headers(),
                json={
                    "to": user_id,
                    "messages": [
                        {
                            "type": "template",
                            "altText": text,
                            "template": {
                                "type": "confirm",
                                "text": text,
                                "actions": [
                                    {"type": "message", "label": "✅ 執行", "text": CONFIRM_YES},
                                    {"type": "message", "label": "❌ 取消", "text": CONFIRM_NO},
                                ],
                            },
                        }
                    ],
                },
            )
    except httpx.HTTPError as exc:
        logger.error(f"LINE confirm push request failed: {exc!r}")
        return False
    if resp.status_code == 200:
        return True
    logger.error(f"LINE confirm push failed ({resp.status_code}): {resp.text}")
    return False
