"""opencode brain, talked to over HTTP, with a model fallback chain.

Every group-triggering LINE message (or admin DM) hits a long-running
`opencode serve` process (started once by
scripts/start-group-bot-background.ps1, see app/config.py:
groupbot_opencode_serve_host/port) instead of spawning a fresh `opencode
run` subprocess per message. The subprocess-per-call design had a ~25-30s
*fixed* cold-start cost on this machine (Bun runtime boot + config
reload) that regularly blew past the per-rung timeout and made the bot
look completely dead — the persistent server cuts that to ~0 (measured
2026-07-21: ~44s cold via subprocess vs ~19s warm via HTTP, same
model+prompt).

The agent definitions (C:\\Users\\user\\.config\\opencode\\agents\\groupbot.md,
...\\admin.md) carry the full system prompts; this module only supplies
the "conversation context + latest message" user prompt (see
`build_prompt`) and drives the create-session -> post-message -> delete-
session HTTP flow.

Fallback chain: each rung gets its own timeout (see
run_model_chain(timeout_seconds=...)). A rung is considered failed (and
the next one tried) if:
  - the HTTP call doesn't finish within the timeout,
  - the server returns a non-200 response,
  - the response body contains a 429 / rate-limit / quota marker, or
  - no usable answer text could be extracted from the response parts.
If every rung fails, the group handler stays silent and only logs an
error; the admin DM handler still replies with an apology (a DM always
expects a reply).
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid

import httpx

from app.config import Settings, get_settings
from app.logger import logger
from app.services import conversation, line_client

_RATE_LIMIT_MARKERS = ("429", "rate limit", "rate_limit", "quota", "too many requests")

# NOTE: a reactive "restart opencode serve the moment one message's fallback
# chain exhausts every rung" self-heal used to live here (added, then
# removed, 2026-07-21). Cut because it caused a worse cascade than the
# problem it was meant to fix: when several LINE messages (group + admin)
# are in flight at once and one of them exhausts its chain, killing the
# shared opencode serve process out from under the *other* still-in-flight
# chains turns their would-have-succeeded rungs into instant
# "server_unreachable" failures, which independently exhaust *their* chains
# too - self-reinforcing, confirmed 2026-07-21 by log evidence of a single
# trigger fanning out into a burst of "all rungs failed" across concurrent
# messages within the same few seconds. Proactive restarts on a fixed timer
# (see scripts/watchdog-opencode-serve.ps1 and the scheduled restart task)
# don't have this problem since they don't correlate with "multiple
# messages failing at once," but a failure-triggered restart does by
# construction. If this gets revisited, it needs to check for zero
# in-flight chains before restarting, not just a cooldown.

_SILENT_RE = re.compile(
    r"""^[\s"'「」『』(（]*\(silent\)[\s"'「」『』)）.,。，!！]*$""",
    re.IGNORECASE,
)


def should_suppress(text: str | None) -> bool:
    """True if the model's answer means "don't send anything to the group"."""
    if text is None:
        return True
    stripped = text.strip()
    if not stripped:
        return True
    return bool(_SILENT_RE.match(stripped))


def build_prompt(
    history: list[tuple[str, str]],
    latest_label: str,
    latest_text: str,
    *,
    was_mentioned: bool = False,
) -> str:
    """Assemble the "context + latest message" user prompt.

    The system prompt itself lives entirely in the opencode agent
    definition — this is only the per-turn payload. was_mentioned=True
    means the group's LINE @-mention on the latest message targeted the
    bot itself (not @all — see app/api/line_webhook.py, which never
    dispatches @all at all) and tells the model to answer for real
    instead of applying its usual topic-relevance (silent) filter.
    """
    if history:
        context_lines = "\n".join(f"[{label}]: {text}" for label, text in history)
    else:
        context_lines = "(尚無對話紀錄)"
    prompt = (
        "以下是最近的對話紀錄(供判斷上下文用):\n"
        f"{context_lines}\n\n"
        "最新訊息(請針對這一則回覆):\n"
        f"[{latest_label}]: {latest_text}"
    )
    if was_mentioned:
        prompt += (
            "\n\n(提示:這則訊息裡有人直接 @ 你本人,不是廣播 @所有人。"
            "既然被直接提及,請正常回覆、不要輸出 (silent),就算內容不完全"
            "屬於總務處業務範圍,也請盡量給出有幫助的回應,或告知使用者"
            "你能協助的範圍與正確窗口。)"
        )
    return prompt


def _looks_rate_limited(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _RATE_LIMIT_MARKERS)


def _split_provider_model(model: str) -> tuple[str, str]:
    """"opencode/big-pickle" -> ("opencode", "big-pickle");
    "nvidia/deepseek-ai/deepseek-v4-flash" -> ("nvidia", "deepseek-ai/deepseek-v4-flash").
    """
    provider_id, _, model_id = model.partition("/")
    return provider_id, model_id


def _extract_answer(parts: list[dict]) -> str | None:
    """Concatenate every "text"-type part's text, in order. Reasoning/
    step-start/step-finish parts are ignored. None if there's no text
    part at all (treated as a failed rung, not an empty reply).
    """
    texts = [p.get("text", "") for p in parts if p.get("type") == "text"]
    if not texts:
        return None
    return "".join(texts)


async def _abort_session(client: httpx.AsyncClient, session_id: str) -> None:
    """Best-effort POST /session/{id}/abort to stop a still-running
    generation and free its opencode-serve worker. Called on timeout
    before the session is deleted (see _run_rung). Swallows all errors —
    aborting is a cleanup nicety, never allowed to raise into the caller.
    """
    try:
        await client.post(f"/session/{session_id}/abort", timeout=5)
    except httpx.HTTPError:
        pass


async def _run_rung(
    client: httpx.AsyncClient,
    settings: Settings,
    model: str,
    prompt: str,
    *,
    agent: str,
    timeout_seconds: int,
    cwd: str | None,
) -> tuple[str | None, str]:
    """Run one fallback-chain rung against the persistent opencode server.

    Returns (answer_or_None, failure_reason). failure_reason is "" on
    success, otherwise one of: "server_unreachable" / "timeout" /
    "http_error" / "rate_limited" / "no_answer".
    """
    provider_id, model_id = _split_provider_model(model)
    session_id: str | None = None
    try:
        create_resp = await client.post(
            "/session",
            json={},
            params={"directory": cwd} if cwd is not None else {},
            timeout=10,
        )
        create_resp.raise_for_status()
        session_id = create_resp.json()["id"]

        message_resp = await client.post(
            f"/session/{session_id}/message",
            json={
                "parts": [{"type": "text", "text": prompt}],
                "model": {"providerID": provider_id, "modelID": model_id},
                "agent": agent,
            },
            timeout=timeout_seconds,
        )
    except httpx.TimeoutException:
        # Abort the still-in-flight generation so its opencode-serve worker
        # is actually freed, THEN let `finally` delete the session.
        #
        # History (2026-07-21/22): this used to deliberately NOT abort,
        # because on an older opencode version aborting seemed to make the
        # next few rungs return instant empty responses (a "cascade").
        # That trade-off turned out to be the real root cause of the
        # multi-hour outages: a persistent `opencode serve` holds a fixed
        # pool of workers ("Worker local total request limit reached
        # (48/48)"), and a timed-out generation that is never aborted keeps
        # occupying its worker indefinitely. On a slow-provider night the
        # bot times out a lot, leaks a worker each time, and within tens of
        # minutes every worker is stuck — at which point ALL new messages
        # (group + admin) hang, even though /doc still returns 200 (so the
        # liveness watchdog never notices). Confirmed by A/B: a
        # seconds-old serve answers a trivial prompt fine; a ~15-min-old
        # one under load hangs on the same prompt. Aborting frees the
        # worker and is the actual fix; the existing
        # groupbot_timeout_recovery_delay_seconds pause between rungs is
        # kept as the guard against the old cascade symptom.
        await _abort_session(client, session_id)
        logger.warning(f"model={model} timed out after {timeout_seconds}s")
        return None, "timeout"
    except httpx.HTTPError as exc:
        logger.error(f"opencode serve unreachable/errored: {exc}")
        return None, "server_unreachable"
    finally:
        if session_id:
            try:
                await client.delete(f"/session/{session_id}", timeout=5)
            except httpx.HTTPError:
                pass

    if message_resp.status_code != 200:
        body_text = message_resp.text
        if _looks_rate_limited(body_text):
            logger.warning(f"model={model} looks rate-limited: {body_text[:300]}")
            return None, "rate_limited"
        logger.warning(f"model={model} http {message_resp.status_code}: {body_text[:300]}")
        return None, "http_error"

    payload = message_resp.json()
    if _looks_rate_limited(message_resp.text):
        logger.warning(f"model={model} looks rate-limited: {message_resp.text[:300]}")
        return None, "rate_limited"

    answer = _extract_answer(payload.get("parts", []))
    if answer is None:
        logger.warning(f"model={model} produced no parseable answer: {message_resp.text[:300]}")
        return None, "no_answer"

    return answer, ""


async def run_model_chain(
    prompt: str,
    *,
    agent: str | None = None,
    timeout_seconds: int | None = None,
    cwd: str | None = None,
    title_prefix: str = "groupbot",
    model_chain: list[str] | None = None,
    primary_timeout_seconds: int | None = None,
) -> tuple[str | None, str | None]:
    """Try each model in settings.model_chain (or the given model_chain
    override, e.g. settings.admin_model_chain) in order until one succeeds.

    Returns (answer, model_id_used); (None, None) if every rung failed.
    """
    settings = get_settings()
    agent = agent or settings.groupbot_opencode_agent
    timeout_seconds = timeout_seconds or settings.groupbot_model_timeout_seconds
    primary_timeout_seconds = primary_timeout_seconds or settings.groupbot_primary_model_timeout_seconds
    chain = model_chain if model_chain is not None else settings.model_chain
    _ = f"{title_prefix}-{uuid.uuid4().hex[:8]}"  # kept for log correlation only

    async with httpx.AsyncClient(base_url=settings.opencode_serve_base_url) as client:
        for index, model in enumerate(chain):
            # Rung 0 (the preferred model) gets a shorter leash by default:
            # it's an opportunistic free-tier pool that's often degraded,
            # and waiting the full per-rung timeout on it before falling
            # back was adding tens of seconds of dead time to every message
            # (see groupbot_primary_model_timeout_seconds docstring).
            # Callers whose rung 0 may be mid tool-execution (admin DMs)
            # pass a larger primary_timeout_seconds instead.
            rung_timeout = primary_timeout_seconds if index == 0 else timeout_seconds
            started = time.monotonic()
            answer, reason = await _run_rung(
                client, settings, model, prompt, agent=agent, timeout_seconds=rung_timeout, cwd=cwd
            )
            elapsed = time.monotonic() - started
            if answer is not None:
                logger.info(f"model_chain: model={model} succeeded in {elapsed:.1f}s")
                return answer, model
            logger.info(f"model_chain: model={model} failed ({reason}) after {elapsed:.1f}s")

            if reason == "timeout":
                # Give opencode serve a moment to settle after a timed-out
                # rung before immediately hammering it with a fresh
                # session for the next one — see the comment in _run_rung
                # above. Only for "timeout"; other failure reasons (rate
                # limited, http error, no answer) didn't show this
                # cascade in testing, so don't slow those down.
                await asyncio.sleep(settings.groupbot_timeout_recovery_delay_seconds)

    logger.error("model_chain: all rungs failed, staying silent")
    return None, None


_MENTION_FALLBACK_REPLY = (
    "你好,這則訊息看起來跟總務處業務比較沒關係,如果有總務相關的問題"
    "(採購、修繕、場地、動植物事件等)歡迎告訴我更多細節。"
)


async def handle_message(
    group_id: str, user_id: str, reply_token: str, text: str, was_mentioned: bool = False
) -> None:
    """Single entry point wired up by the LINE webhook.

    Records the incoming message in the rolling context, runs the model
    fallback chain, and — unless the model says `(silent)` / all rungs
    failed — sends the reply (reply-token first, push fallback) and
    records the bot's own reply into the context too. was_mentioned=True
    (the bot itself was @-tagged) overrides the (silent) topic filter: a
    direct mention always gets a real reply, falling back to a generic
    "that's outside 總務處 scope" message if the model still said
    (silent) despite the prompt instruction not to.
    """
    label = conversation.add_user_message(group_id, user_id, text)
    history = conversation.history_excluding_last(group_id)
    prompt = build_prompt(history, label, text, was_mentioned=was_mentioned)

    answer, model_used = await run_model_chain(prompt)

    if answer is None:
        if was_mentioned:
            logger.error(f"group={group_id} @-mentioned but all model rungs failed")
        else:
            logger.info(f"group={group_id} model_used={model_used} all rungs failed, staying silent")
        return

    if should_suppress(answer):
        if not was_mentioned:
            logger.info(f"group={group_id} model={model_used} suppressed reply (silent/empty)")
            return
        logger.info(f"group={group_id} model={model_used} said (silent) despite @-mention; using fallback")
        answer = _MENTION_FALLBACK_REPLY

    assert answer is not None  # should_suppress already filtered None out
    conversation.add_bot_message(group_id, answer)
    await line_client.send_text(reply_token, group_id, answer)


async def handle_admin_message(user_id: str, reply_token: str, text: str) -> None:
    """Entry point for admin 1:1 DMs (see app/api/line_webhook.py).

    Unlike the group agent, this always replies (no `(silent)` protocol —
    a DM is always addressed to the bot) and runs the unrestricted "admin"
    opencode agent (full read/write/edit/bash tool access, see
    C:\\Users\\user\\.config\\opencode\\agents\\admin.md), scoped to
    settings.groupbot_admin_cwd. There is no per-tool confirmation gate —
    the admin allowlist check in app/services/allowlist.py is the only
    access control, since opencode serve has no way to prompt a human for
    interactive approval either.
    """
    settings = get_settings()
    label = conversation.add_user_message(user_id, user_id, text)
    history = conversation.history_excluding_last(user_id)
    prompt = build_prompt(history, label, text)

    answer, model_used = await run_model_chain(
        prompt,
        agent=settings.groupbot_admin_opencode_agent,
        timeout_seconds=settings.groupbot_admin_model_timeout_seconds,
        cwd=settings.groupbot_admin_cwd,
        title_prefix="admin-dm",
        model_chain=settings.admin_model_chain,
        primary_timeout_seconds=settings.groupbot_admin_primary_model_timeout_seconds,
    )

    if answer is None:
        logger.error(f"admin DM user={user_id[:8]}… all model rungs failed")
        await line_client.send_text(reply_token, user_id, "抱歉,所有模型都暫時失敗了,請稍後再試一次。")
        return

    logger.info(f"admin DM user={user_id[:8]}… model={model_used} replied")
    conversation.add_bot_message(user_id, answer)
    await line_client.send_text(reply_token, user_id, answer)
