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


def _sent_message_ids(resp: httpx.Response) -> list[str]:
    """Extract the LINE-assigned message IDs from a successful reply/push
    response body (`{"sentMessages": [{"id": ..., ...}, ...]}`), so callers
    can remember which IDs the bot just sent (see
    app/services/conversation.py's record_bot_message_id, used to detect
    a later swipe-to-reply quote of the bot's own message).
    """
    try:
        data = resp.json()
    except ValueError:
        return []
    return [m["id"] for m in data.get("sentMessages", []) if isinstance(m, dict) and "id" in m]


async def reply_text(reply_token: str, text: str) -> list[str] | None:
    """Reply using a webhook replyToken. Returns the sent message IDs on
    success, None on any failure — including network errors (timeout/
    connect/etc), not just a non-200 status. A raised httpx exception here
    used to propagate out of send_text and skip the push_text fallback
    entirely: a transient network blip to api.line.me meant the model's
    already-computed answer was silently lost instead of falling back to
    push (confirmed 2026-07-21: an admin DM's model chain succeeded, but
    the reply was dropped because reply_text raised httpx.ConnectTimeout
    instead of returning False).
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
        return None
    if resp.status_code == 200:
        return _sent_message_ids(resp)
    logger.warning(f"LINE reply failed ({resp.status_code}): {resp.text}")
    return None


async def push_text(group_id: str, text: str) -> list[str] | None:
    """Push a message directly to a group (fallback when replyToken expired
    or reply_text hit a network error). Same reasoning as reply_text above:
    a network exception here must not propagate — it's already the last
    fallback, so the caller only needs to know whether push succeeded (and,
    if so, what message IDs it sent).
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
        return None
    if resp.status_code == 200:
        return _sent_message_ids(resp)
    logger.error(f"LINE push failed ({resp.status_code}): {resp.text}")
    return None


async def send_text(reply_token: str, group_id: str, text: str) -> list[str]:
    """Reply first via the webhook replyToken; if that fails (e.g. the
    token has expired — likely if the model fallback chain took a while),
    fall back to pushing directly to the group. Returns whichever send
    path succeeded's sent message IDs (empty list if both failed).
    """
    ids = await reply_text(reply_token, text)
    if ids is not None:
        return ids
    logger.info(f"Reply token invalid/expired, falling back to push for group={group_id}")
    ids = await push_text(group_id, text)
    return ids or []


async def get_group_member_display_name(group_id: str, user_id: str) -> str | None:
    """Best-effort lookup of a group member's LINE display name (Messaging
    API group-member profile endpoint), used to prefix group replies with
    "{name}老師好," for politeness (see app/services/opencode_agent.py).
    Returns None on any failure — an unknown name means the caller just
    skips the greeting, never raises.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{LINE_API_BASE}/group/{group_id}/member/{user_id}",
                headers=_headers(),
            )
    except httpx.HTTPError as exc:
        logger.warning(f"LINE group member profile lookup failed: {exc!r}")
        return None
    if resp.status_code != 200:
        logger.warning(f"LINE group member profile lookup failed ({resp.status_code}): {resp.text}")
        return None
    return resp.json().get("displayName")
