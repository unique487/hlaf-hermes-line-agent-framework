"""LINE Messaging API webhook endpoint for the 總務處 group bot."""

import json

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from app.config import get_settings
from app.logger import logger
from app.services import allowlist, conversation, opencode_agent
from app.utils.line_signature import verify_line_signature

router = APIRouter(tags=["line"])


@router.post("/line/webhook")
async def line_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_line_signature: str = Header(default=""),
) -> dict[str, str]:
    body = await request.body()
    settings = get_settings()

    if not verify_line_signature(settings.line_channel_secret, body, x_line_signature):
        logger.warning("Rejected webhook call with invalid LINE signature")
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

    for event in payload.get("events", []):
        source = event.get("source", {})
        event_type = event.get("type")
        source_type = source.get("type")

        if event_type != "message" or source_type not in ("group", "user"):
            # Rooms and non-message events are silently ignored, per spec.
            logger.debug(f"Skipping event type={event_type} source={source_type}")
            continue

        message = event.get("message", {})
        if message.get("type") != "text":
            logger.debug(f"Skipping non-text message type={message.get('type')}")
            continue

        reply_token = event.get("replyToken", "")
        text = message.get("text", "")
        if not reply_token:
            continue

        if source_type == "group":
            group_id = source.get("groupId", "")
            user_id = source.get("userId", "")
            if not group_id:
                continue
            if not allowlist.is_group_allowed(group_id):
                # Silent ignore: unlisted groups get no reply, no read receipt.
                logger.info(f"Ignored message from non-allowlisted group {group_id[:8]}…")
                continue

            mentionees = (message.get("mention") or {}).get("mentionees", [])
            if any(m.get("type") == "all" for m in mentionees):
                # @所有人 broadcasts are never a reply trigger, even if the
                # text content would otherwise look relevant.
                logger.info(f"Ignored @all broadcast in group {group_id[:8]}…")
                continue
            was_mentioned = any(m.get("isSelf") for m in mentionees)

            # A LINE swipe-to-reply quote of one of the bot's own past
            # messages should always get a real reply too, same as an
            # @-mention — the sender is explicitly addressing the bot even
            # though there's no @-mention token in the text.
            quoted_message_id = message.get("quotedMessageId")
            is_reply_to_bot = conversation.is_reply_to_bot(group_id, quoted_message_id)

            background_tasks.add_task(
                opencode_agent.handle_message,
                group_id,
                user_id,
                reply_token,
                text,
                was_mentioned,
                is_reply_to_bot,
            )
        else:  # source_type == "user" — 1:1 DM
            user_id = source.get("userId", "")
            if not user_id:
                continue
            if not allowlist.is_user_admin(user_id):
                # Silent ignore: only the admin allowlist gets the
                # unrestricted opencode agent over DM.
                logger.info(f"Ignored DM from non-admin user {user_id[:8]}…")
                continue
            background_tasks.add_task(
                opencode_agent.handle_admin_message, user_id, reply_token, text
            )

    return {"status": "ok"}
