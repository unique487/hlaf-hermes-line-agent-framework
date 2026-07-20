"""FastAPI application entrypoint."""

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
    logger.info(f"{settings.app_name} starting up in '{settings.app_env}' mode")
    if not settings.line_channel_secret or not settings.line_channel_access_token:
        logger.warning("LINE credentials not set — /line/webhook will reject all calls")
    if not settings.opencode_zen_api_key:
        logger.warning("OPENCODE_ZEN_API_KEY not set — Hermes cannot answer")
    yield
    logger.info(f"{settings.app_name} shutting down")


app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.include_router(health_router)
app.include_router(line_webhook_router)


@app.get("/")
async def root() -> dict[str, str]:
    """Hello world root endpoint."""
    return {"message": "Hello, HLAF (Hermes Line Agent Framework)!"}
