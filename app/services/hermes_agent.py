"""Stateless chat completion against OpenCode Zen (OpenAI-compatible), with model fallback."""

import httpx

from app.config import get_settings
from app.logger import logger

FALLBACK_REPLY = "Hermes 暫時連不上大腦(所有模型都失敗了),請稍後再試。"


async def _chat_completion(model: str, messages: list[dict[str, str]]) -> str:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            f"{settings.opencode_zen_base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.opencode_zen_api_key}",
                "Content-Type": "application/json",
            },
            json={"model": model, "messages": messages},
        )
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    if not isinstance(content, str) or not content.strip():
        raise ValueError(f"Model {model} returned empty content")
    return content.strip()


async def chat_completion(messages: list[dict[str, str]]) -> str:
    """Send a message list to Zen, trying each configured model in order."""
    for model in get_settings().zen_model_list:
        try:
            answer = await _chat_completion(model, messages)
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
            logger.warning(f"Zen model '{model}' failed: {exc}")
            continue
        logger.info(f"Zen model '{model}' answered ({len(answer)} chars)")
        return answer

    logger.error("All Zen models failed")
    return FALLBACK_REPLY
