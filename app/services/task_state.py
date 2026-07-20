"""In-memory per-user desktop-agent task state.

A single uvicorn worker is assumed: state lives in process memory so an
`asyncio.Event` set by one webhook request can wake a coroutine started by
an earlier request (e.g. resuming a task that's waiting on LINE confirmation).
"""

import asyncio
import time
from dataclasses import dataclass, field

# Sent as the literal text of the LINE "confirm" template's two buttons.
CONFIRM_YES = "/confirm yes"
CONFIRM_NO = "/confirm no"


@dataclass
class TaskState:
    user_id: str
    status: str = "running"  # running | awaiting_confirmation
    steps: list[str] = field(default_factory=list)
    confirm_event: asyncio.Event = field(default_factory=asyncio.Event)
    confirm_result: bool | None = None
    started_at: float = field(default_factory=time.monotonic)
    step_count: int = 0


_tasks: dict[str, TaskState] = {}


def get(user_id: str) -> TaskState | None:
    return _tasks.get(user_id)


def start(user_id: str) -> TaskState:
    task = TaskState(user_id=user_id)
    _tasks[user_id] = task
    return task


def clear(user_id: str) -> None:
    _tasks.pop(user_id, None)
