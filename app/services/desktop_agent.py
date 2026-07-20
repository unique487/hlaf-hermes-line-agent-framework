"""Hermes desktop agent: a tool-using loop on top of OpenCode Zen chat completions.

Single entry point is `handle_message`. Casual chat and "operate my computer"
requests go through the same loop — the model either replies in plain text
(final answer) or emits a single JSON tool call, which we execute (after a
LINE confirmation round-trip for anything dangerous) and feed back in.
"""

import asyncio
import json
from collections import defaultdict, deque

from app.config import get_settings
from app.logger import logger
from app.prompts.hermes import desktop_system_prompt
from app.services import line_client, task_state, tools
from app.services.hermes_agent import chat_completion
from app.services.task_state import CONFIRM_NO, CONFIRM_YES, TaskState

_HISTORY_MAX_MESSAGES = 20
_history: dict[str, deque[dict[str, str]]] = defaultdict(
    lambda: deque(maxlen=_HISTORY_MAX_MESSAGES)
)

STATUS_KEYWORDS = {"進度", "狀態", "/status"}


def reset_history(user_id: str) -> None:
    _history.pop(user_id, None)
    task_state.clear(user_id)


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
    history = _history[user_id]
    messages = [
        {"role": "system", "content": desktop_system_prompt()},
        *history,
        {"role": "user", "content": text},
    ]

    pinger = asyncio.create_task(_progress_pinger(user_id, task))
    try:
        final_answer = await _loop(task, messages)
    finally:
        pinger.cancel()
        task_state.clear(user_id)

    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": final_answer})
    await line_client.send_text(reply_token, user_id, final_answer)


async def _loop(task: TaskState, messages: list[dict[str, str]]) -> str:
    settings = get_settings()
    for _ in range(settings.hermes_max_tool_steps):
        task.step_count += 1
        reply = await chat_completion(messages)
        call = _parse_tool_call(reply)
        if call is None:
            return reply

        tool_name = call.get("tool", "")
        args = call.get("args") or {}
        messages.append({"role": "assistant", "content": reply})

        if tool_name not in tools.TOOL_HANDLERS:
            messages.append(
                {
                    "role": "user",
                    "content": f"[工具執行結果] 未知工具 '{tool_name}',請改用可用工具或直接回答。",
                }
            )
            continue

        description = _describe_action(tool_name, args)
        task.steps.append(f"第 {task.step_count} 步:{description}")

        dangerous = tools.is_dangerous(tool_name, args)
        if dangerous and not await _ask_confirmation(task, description):
            messages.append(
                {
                    "role": "user",
                    "content": "[使用者取消] 使用者拒絕了這個動作,請提出替代方案或結束任務。",
                }
            )
            continue

        try:
            result = await tools.TOOL_HANDLERS[tool_name](**args)
        except tools.ToolError as exc:
            result = f"執行失敗:{exc}"
        except TypeError as exc:
            result = f"參數錯誤:{exc}"
        except Exception as exc:  # noqa: BLE001 - surface any tool failure back to the model
            logger.exception(f"Tool '{tool_name}' raised an unexpected error")
            result = f"執行時發生未預期錯誤:{exc}"

        messages.append({"role": "user", "content": f"[工具執行結果]\n{result}"})

    return "任務已達到最大步驟限制,已自動停止避免無限迴圈。目前進度:\n" + "\n".join(task.steps[-5:])


def _parse_tool_call(reply: str) -> dict | None:
    stripped = reply.strip()
    if not stripped.startswith("{"):
        return None
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "tool" not in data:
        return None
    return data


def _describe_action(tool_name: str, args: dict) -> str:
    if tool_name == "run_shell":
        return f"執行指令:{args.get('command', '')}"
    if tool_name == "write_file":
        return f"寫入檔案:{args.get('path', '')}"
    if tool_name in {"read_file", "list_dir"}:
        return f"{tool_name}:{args.get('path', '')}"
    return f"{tool_name}({args})"


async def _ask_confirmation(task: TaskState, description: str) -> bool:
    settings = get_settings()
    task.status = "awaiting_confirmation"
    task.confirm_event = asyncio.Event()
    task.confirm_result = None
    await line_client.push_confirm(task.user_id, description)
    try:
        await asyncio.wait_for(
            task.confirm_event.wait(), timeout=settings.hermes_confirm_timeout_seconds
        )
    except TimeoutError:
        await line_client.push_text(task.user_id, "確認逾時,已自動取消這個動作。")
        task.status = "running"
        return False
    task.status = "running"
    return bool(task.confirm_result)


async def _progress_pinger(user_id: str, task: TaskState) -> None:
    settings = get_settings()
    try:
        while True:
            await asyncio.sleep(settings.hermes_progress_interval_seconds)
            if task.status == "running":
                log = "\n".join(task.steps[-3:]) or "(執行中)"
                await line_client.push_text(user_id, f"[進度回報] 第 {task.step_count} 步\n{log}")
    except asyncio.CancelledError:
        pass
