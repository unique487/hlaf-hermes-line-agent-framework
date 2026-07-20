"""Hermes agent brain — OpenCode Zen (OpenAI-compatible) with model fallback.

Keeps a short in-memory conversation history per user so follow-up
messages have context. History is lost on server restart (by design for now).
"""

from collections import defaultdict, deque

import httpx

from app.config import get_settings
from app.logger import logger
from app.prompts.hermes import HERMES_SYSTEM_PROMPT

# Per-user rolling history of {"role": ..., "content": ...} entries.
_HISTORY_MAX_MESSAGES = 20
_history: dict[str, deque[dict[str, str]]] = defaultdict(
    lambda: deque(maxlen=_HISTORY_MAX_MESSAGES)
)

FALLBACK_REPLY = "Hermes 暫時連不上大腦(所有模型都失敗了),請稍後再試。"


def reset_history(user_id: str) -> None:
    _history.pop(user_id, None)


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


async def ask_hermes(user_id: str, text: str) -> str:
    """Answer a user message, trying each configured model in order."""
    history = _history[user_id]
    messages = [
        {"role": "system", "content": HERMES_SYSTEM_PROMPT},
        *history,
        {"role": "user", "content": text},
    ]

    for model in get_settings().zen_model_list:
        try:
            answer = await _chat_completion(model, messages)
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
            logger.warning(f"Zen model '{model}' failed: {exc}")
            continue
        logger.info(f"Zen model '{model}' answered ({len(answer)} chars)")
        history.append({"role": "user", "content": text})
        history.append({"role": "assistant", "content": answer})
        return answer

    logger.error("All Zen models failed")
    return FALLBACK_REPLY
