"""FastAPI application entrypoint — 總務處 LINE 群組小幫手 (group-bot)."""

import os
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.line_webhook import router as line_webhook_router
from app.config import get_settings
from app.logger import logger

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    logger.info(f"{settings.app_name} starting up in '{settings.app_env}' mode (port={settings.groupbot_port})")
    if not settings.line_channel_secret or not settings.line_channel_access_token:
        logger.warning("LINE credentials not set — /line/webhook will reject all calls")
    if not settings.allowed_group_ids:
        logger.warning("LINE_ALLOWED_GROUPS is empty — every group message will be ignored")
    if not shutil.which(settings.groupbot_opencode_bin) and not os.path.exists(settings.groupbot_opencode_bin):
        logger.warning(
            f"opencode binary not found at '{settings.groupbot_opencode_bin}' — "
            "cannot answer until it's installed, or GROUPBOT_OPENCODE_BIN is set correctly"
        )
    logger.info(f"model chain: {' -> '.join(settings.model_chain)}")
    yield
    logger.info(f"{settings.app_name} shutting down")


app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.include_router(health_router)
app.include_router(line_webhook_router)


@app.get("/")
async def root() -> dict[str, str]:
    return {"message": "總務處 LINE 群組小幫手 (group-bot)"}
