"""Minimal LINE Messaging API client (reply / push) for the group bot."""

import httpx

from app.config import get_settings
from app.logger import logger

LINE_API_BASE = "https://api.line.me/v2/bot"
# LINE text messages are capped at 5000 characters.
MAX_TEXT_LEN = 5000


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
    network blip to api.line.me meant the model's already-computed answer
    was silently lost instead of falling back to push (confirmed
    2026-07-21: an admin DM's model chain succeeded, but the reply was
    dropped because reply_text raised httpx.ConnectTimeout instead of
    returning False).
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


async def push_text(group_id: str, text: str) -> bool:
    """Push a message directly to a group (fallback when replyToken expired
    or reply_text hit a network error). Same reasoning as reply_text above:
    a network exception here must not propagate — it's already the last
    fallback, so the caller only needs to know push succeeded or not.
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{LINE_API_BASE}/message/push",
                headers=_headers(),
                json={"to": group_id, "messages": _text_messages(text)},
            )
    except httpx.HTTPError as exc:
        logger.error(f"LINE push request failed: {exc!r}")
        return False
    if resp.status_code == 200:
        return True
    logger.error(f"LINE push failed ({resp.status_code}): {resp.text}")
    return False


async def send_text(reply_token: str, group_id: str, text: str) -> None:
    """Reply first via the webhook replyToken; if that fails (e.g. the
    token has expired — likely if the model fallback chain took a while),
    fall back to pushing directly to the group.
    """
    if not await reply_text(reply_token, text):
        logger.info(f"Reply token invalid/expired, falling back to push for group={group_id}")
        await push_text(group_id, text)
