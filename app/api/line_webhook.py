"""LINE Messaging API webhook endpoint."""

import json

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from app.config import get_settings
from app.logger import logger
from app.services import allowlist, claude_code_agent, line_client
from app.utils.line_signature import verify_line_signature

router = APIRouter(tags=["line"])


async def _handle_text_message(user_id: str, reply_token: str, text: str) -> None:
    """Background task: run Hermes (headless Claude Code) and send the answer back."""
    if text.strip().lower() in {"/reset", "重來"}:
        claude_code_agent.reset_history(user_id)
        await line_client.send_text(reply_token, user_id, "已清除對話記憶與任務狀態,我們重新開始。")
        return
    await claude_code_agent.handle_message(user_id, reply_token, text)


async def _handle_first_contact(user_id: str, reply_token: str) -> None:
    await line_client.send_text(
        reply_token,
        user_id,
        "已將你註冊為 Hermes 的管理員(白名單第一人)。\n之後私訊我就可以直接對話囉!",
    )


async def _handle_follow_declined(user_id: str, reply_token: str) -> None:
    await line_client.send_text(
        reply_token,
        user_id,
        "您好,這是 Hermes 專屬帳號,目前僅開放特定使用者使用,暫不提供公開服務,敬請見諒。",
    )


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
        user_id = source.get("userId", "")
        reply_token = event.get("replyToken", "")

        if event_type == "follow":
            # Someone added the OA as a friend. Only greet strangers — if no
            # admin exists yet, stay quiet and let their first message capture them.
            if user_id and reply_token and allowlist.get_allowed_ids():
                background_tasks.add_task(_handle_follow_declined, user_id, reply_token)
            continue

        if event_type != "message" or source.get("type") != "user":
            logger.debug(f"Skipping event type={event_type} source={source.get('type')}")
            continue
        message = event.get("message", {})
        if message.get("type") != "text":
            logger.debug(f"Skipping non-text message type={message.get('type')}")
            continue

        text = message.get("text", "")
        if not user_id or not reply_token:
            continue

        if allowlist.capture_first_admin(user_id):
            background_tasks.add_task(_handle_first_contact, user_id, reply_token)
            continue
        if not allowlist.is_allowed(user_id):
            # Silent ignore: no reply, no read receipt — indistinguishable
            # from the OA being offline to a non-allowlisted user.
            logger.info(f"Ignored message from non-allowlisted user {user_id[:8]}…")
            continue

        background_tasks.add_task(_handle_text_message, user_id, reply_token, text)

    return {"status": "ok"}
