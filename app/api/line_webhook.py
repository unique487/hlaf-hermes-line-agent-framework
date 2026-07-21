"""LINE Messaging API webhook endpoint."""

import json
from pathlib import Path

import httpx
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from app.config import get_settings
from app.logger import logger
from app.services import allowlist, claude_code_agent, line_client
from app.utils.line_signature import verify_line_signature

router = APIRouter(tags=["line"])

# LINE doesn't expose an original filename for binary content, only a
# Content-Type on download — map the common image types it actually sends.
_IMAGE_EXT_BY_CONTENT_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


async def _handle_text_message(user_id: str, reply_token: str, text: str) -> None:
    """Background task: run the headless Claude Code agent and send the answer back."""
    if text.strip().lower() in {"/reset", "重來"}:
        claude_code_agent.reset_history(user_id)
        await line_client.send_text(reply_token, user_id, "已清除對話記憶與任務狀態,我們重新開始。")
        return
    await claude_code_agent.handle_message(user_id, reply_token, text)


async def _handle_image_message(user_id: str, reply_token: str, message_id: str) -> None:
    """Background task: download a LINE image, then let Claude Code `Read` it.

    Saved under desktop_root (not a system temp dir) so it falls inside the
    directory `claude -p` already runs in / has `--add-dir` access to.
    """
    settings = get_settings()
    try:
        content, content_type = await line_client.get_message_content(message_id)
    except httpx.HTTPError as exc:
        logger.error(f"Failed to download LINE image {message_id}: {exc}")
        await line_client.send_text(reply_token, user_id, "圖片下載失敗,請稍後再試一次。")
        return

    ext = _IMAGE_EXT_BY_CONTENT_TYPE.get(content_type.split(";")[0].strip(), ".jpg")
    upload_dir = Path(settings.desktop_root) / "line_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    image_path = upload_dir / f"{message_id}{ext}"
    image_path.write_bytes(content)

    prompt = (
        f"使用者透過 LINE 傳送了一張圖片,已下載存放於本機路徑:{image_path}\n"
        "請用 Read 工具查看圖片內容,並回覆使用者你看到了什麼、或依圖片內容協助處理。"
    )
    await claude_code_agent.handle_message(user_id, reply_token, prompt)


async def _handle_first_contact(user_id: str, reply_token: str) -> None:
    await line_client.send_text(
        reply_token,
        user_id,
        "已將你註冊為管理員(白名單第一人)。\n之後私訊我就可以直接對話囉!",
    )


async def _handle_follow_declined(user_id: str, reply_token: str) -> None:
    await line_client.send_text(
        reply_token,
        user_id,
        "您好,這是專屬帳號,目前僅開放特定使用者使用,暫不提供公開服務,敬請見諒。",
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
        message_type = message.get("type")
        if message_type not in {"text", "image"}:
            logger.debug(f"Skipping unsupported message type={message_type}")
            continue

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

        if message_type == "text":
            background_tasks.add_task(
                _handle_text_message, user_id, reply_token, message.get("text", "")
            )
        else:
            background_tasks.add_task(
                _handle_image_message, user_id, reply_token, message.get("id", "")
            )

    return {"status": "ok"}
