"""LINE-driven desktop agent: drives the local headless Claude Code CLI (`claude -p`).

Single entry point is `handle_message`. Every LINE message spawns (or resumes)
a `claude -p --output-format stream-json` subprocess as the "brain" — no more
hand-rolled JSON tool-calling loop. Claude Code uses its own built-in tools
(Read/Write/Edit/Bash/...) directly; dangerous calls are gated by the
PreToolUse hook (`scripts/claude_confirm_hook.py`), which blocks until this
module's `/internal/claude-confirm` endpoint (see `app/api/internal.py`)
resolves a LINE Yes/No round-trip.

Each LINE user maps to one Claude Code session id so multi-turn conversations
resume via `--resume`; `/reset`/`重來` drops the mapping so the next message
starts a fresh session.
"""

import asyncio
import json
import os
import uuid
from pathlib import Path

from app.config import get_settings
from app.logger import logger
from app.prompts.persona import desktop_system_prompt
from app.services import line_client, task_state
from app.services.task_state import CONFIRM_NO, CONFIRM_YES, TaskState

STATUS_KEYWORDS = {"進度", "狀態", "/status"}

_HOOK_SETTINGS_FILE = (
    Path(__file__).resolve().parent.parent.parent / "config" / "claude_hooks.json"
)

# LINE user_id -> Claude Code session id, for multi-turn `--resume`.
_sessions: dict[str, str] = {}
# Claude Code session id -> LINE user_id, so the confirm hook (which only
# knows the session id) can find the right TaskState/LINE user to push to.
_session_to_user: dict[str, str] = {}


def reset_history(user_id: str) -> None:
    """Drop the user's Claude Code session (next message starts fresh) and task state."""
    session_id = _sessions.pop(user_id, None)
    if session_id:
        _session_to_user.pop(session_id, None)
    task_state.clear(user_id)


def get_user_for_session(session_id: str) -> str | None:
    """Look up which LINE user a Claude Code session id belongs to."""
    return _session_to_user.get(session_id)


async def handle_message(user_id: str, reply_token: str, text: str) -> None:
    stripped = text.strip()
    lowered = stripped.lower()
    task = task_state.get(user_id)

    if lowered in {CONFIRM_YES, CONFIRM_NO}:
        if task and task.status == "awaiting_confirmation":
            task.confirm_result = lowered == CONFIRM_YES
            task.confirm_event.set()
        else:
            await line_client.send_text(reply_token, user_id, "目前沒有待確認的動作。")
        return

    if stripped in STATUS_KEYWORDS:
        await _report_status(reply_token, user_id, task)
        return

    if task is not None:
        await line_client.send_text(
            reply_token,
            user_id,
            f"目前有任務在執行中(第 {task.step_count} 步),請稍候,或輸入「進度」查看狀態。",
        )
        return

    await _run_task(user_id, reply_token, stripped)


async def _report_status(reply_token: str, user_id: str, task: TaskState | None) -> None:
    if task is None:
        await line_client.send_text(
            reply_token, user_id, "目前沒有任務在執行,你可以直接跟我聊天或下指令。"
        )
        return
    log = "\n".join(task.steps[-10:]) or "(尚未完成任何步驟)"
    await line_client.send_text(
        reply_token, user_id, f"狀態:{task.status}\n第 {task.step_count} 步\n{log}"
    )


async def _run_task(user_id: str, reply_token: str, text: str) -> None:
    task = task_state.start(user_id)
    pinger = asyncio.create_task(_progress_pinger(user_id, task))
    try:
        final_answer = await _run_claude(user_id, task, text)
    finally:
        pinger.cancel()
        task_state.clear(user_id)

    await line_client.send_text(reply_token, user_id, final_answer)


def _build_command(
    claude_cli_path: str, desktop_root: str, session_id: str, resume: bool
) -> list[str]:
    """Build the `claude -p` argv.

    Verified against a real, logged-in `claude` CLI (v2.1.215): `-p`,
    `--output-format stream-json`, `--resume`, `--session-id`, `--add-dir`,
    `--settings`, `--append-system-prompt`, and passing the prompt text as
    the final positional argv element all behave as expected.
    """
    cmd = [
        claude_cli_path,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--add-dir",
        desktop_root,
        "--settings",
        str(_HOOK_SETTINGS_FILE),
        "--append-system-prompt",
        desktop_system_prompt(),
    ]
    cmd += ["--resume", session_id] if resume else ["--session-id", session_id]
    return cmd


def _subprocess_env(settings) -> dict[str, str]:
    env = dict(os.environ)
    env["CLAUDE_INTERNAL_BASE_URL"] = settings.internal_base_url
    env["CLAUDE_DESKTOP_ROOT"] = settings.desktop_root
    # PreToolUse hook (config/claude_hooks.json) needs this repo's own root to
    # find scripts/claude_confirm_hook.py. It can't use `$CLAUDE_PROJECT_DIR`
    # for that: since `claude -p` runs with cwd=desktop_root (see
    # `_run_claude`), that variable resolves to the user's workspace, not
    # this repo — and on Windows, embedding the (Chinese-containing) GDrive
    # path as literal text in the hook's shell command line also gets
    # mangled by the system's non-UTF-8 codepage. Passed as a real env var
    # instead, so the hook reads it via `os.environ` (Unicode-safe) rather
    # than via shell text substitution.
    env["CLAUDE_HOOKS_REPO_ROOT"] = str(
        Path(__file__).resolve().parent.parent.parent
    )
    return env


async def _run_claude(user_id: str, task: TaskState, text: str) -> str:
    settings = get_settings()
    resume = user_id in _sessions
    session_id = _sessions.get(user_id) or str(uuid.uuid4())
    _sessions[user_id] = session_id
    _session_to_user[session_id] = user_id

    cmd = _build_command(settings.claude_cli_path, settings.desktop_root, session_id, resume)
    logger.info(f"Spawning claude for user {user_id[:8]}… session={session_id} resume={resume}")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            str(text),
            cwd=settings.desktop_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_subprocess_env(settings),
            # `stream-json` events can embed large content in a single NDJSON
            # line (e.g. a `tool_result` block for a `Read` on an image) —
            # comfortably past asyncio's default 64 KiB StreamReader limit,
            # which raises `ValueError: Separator is not found, and chunk
            # exceed the limit` and crashes the whole task with no reply
            # ever sent to the user. 10 MiB covers any image this bot will
            # realistically be asked to look at.
            limit=10 * 1024 * 1024,
        )
    except FileNotFoundError:
        logger.error(f"claude CLI not found at '{settings.claude_cli_path}'")
        return (
            "找不到 claude 指令,請確認這台電腦已安裝並登入 "
            "Claude Code CLI(或設定 CLAUDE_CLI_PATH)。"
        )

    try:
        final_answer = await asyncio.wait_for(
            _consume_stream(proc, task), timeout=settings.claude_timeout_seconds
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return "任務執行逾時,已自動中止。"
    except Exception:
        # Any other failure reading/parsing the subprocess stream (crashed
        # process, malformed output, etc.) — log it, but still answer the
        # user rather than leaving them with silence forever (this is what
        # actually happened before: an uncaught ValueError here killed the
        # background task with no LINE reply at all).
        logger.exception(f"claude subprocess failed for user {user_id[:8]}…")
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        return "處理過程發生錯誤,請稍後再試一次。"

    return final_answer


async def _consume_stream(proc: asyncio.subprocess.Process, task: TaskState) -> str:
    """Read the `claude -p --output-format stream-json` NDJSON stream.

    NOTE (unverified assumption): event shapes follow the documented
    `stream-json` format — `{"type": "assistant", "message": {"content": [...]}}`
    for model turns (with `tool_use`/`text` content blocks) and
    `{"type": "result", "result": "..."}` for the final answer. Needs
    cross-checking against real `claude -p ... --output-format stream-json
    --verbose` output on the deployment machine.
    """
    final_answer: str | None = None
    assert proc.stdout is not None
    async for raw_line in proc.stdout:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            logger.debug(f"claude stream: non-JSON line ignored: {line[:200]}")
            continue

        event_type = event.get("type")
        if event_type == "assistant":
            description = _describe_assistant_event(event)
            if description:
                task.step_count += 1
                task.steps.append(f"第 {task.step_count} 步:{description}")
        elif event_type == "result":
            final_answer = event.get("result") or "(沒有回覆內容)"

    returncode = await proc.wait()
    if final_answer is not None:
        return final_answer

    stderr = b""
    if proc.stderr is not None:
        stderr = await proc.stderr.read()
    logger.error(
        f"claude exited {returncode} without a result event: "
        f"{stderr.decode('utf-8', errors='replace')[:500]}"
    )
    return "任務執行失敗,沒有拿到結果,請稍後再試一次。"


def _describe_assistant_event(event: dict) -> str | None:
    message = event.get("message") or {}
    content = message.get("content") or []
    parts = [
        _describe_tool_use(block.get("name", "?"), block.get("input") or {})
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_use"
    ]
    return "; ".join(parts) if parts else None


def _describe_tool_use(name: str, tool_input: dict) -> str:
    if name == "Bash":
        return f"執行指令:{tool_input.get('command', '')}"
    if name in {"Write", "Edit", "MultiEdit"}:
        return f"寫入檔案:{tool_input.get('file_path', '')}"
    if name == "Read":
        return f"讀取檔案:{tool_input.get('file_path', '')}"
    if name in {"Glob", "Grep", "LS"}:
        path = tool_input.get("path") or tool_input.get("pattern") or ""
        return f"{name}:{path}"
    return f"{name}({tool_input})"


async def _progress_pinger(user_id: str, task: TaskState) -> None:
    settings = get_settings()
    try:
        while True:
            await asyncio.sleep(settings.progress_interval_seconds)
            if task.status == "running":
                log = "\n".join(task.steps[-3:]) or "(執行中)"
                await line_client.push_text(user_id, f"[進度回報] 第 {task.step_count} 步\n{log}")
    except asyncio.CancelledError:
        pass
