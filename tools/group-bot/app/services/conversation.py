"""In-memory rolling conversation context, per LINE group.

Keeps the last N messages (default 10, configurable via
GROUPBOT_CONTEXT_WINDOW) per group, including the bot's own replies.
Speakers are anonymised as 使用者A/B/C… based on a stable per-group mapping
of LINE userId -> letter. Everything lives in process memory only; a
restart clears all history (documented limitation, see README).
"""

from __future__ import annotations

import string
from dataclasses import dataclass, field

from app.config import get_settings

BOT_LABEL = "機器人"


@dataclass
class _GroupState:
    history: list[tuple[str, str]] = field(default_factory=list)  # (label, text)
    user_labels: dict[str, str] = field(default_factory=dict)  # userId -> 使用者A/B/...
    _next_label_index: int = 0

    def label_for_user(self, user_id: str) -> str:
        if user_id not in self.user_labels:
            letter = string.ascii_uppercase[self._next_label_index % 26]
            self.user_labels[user_id] = f"使用者{letter}"
            self._next_label_index += 1
        return self.user_labels[user_id]


_groups: dict[str, _GroupState] = {}


def _state(group_id: str) -> _GroupState:
    if group_id not in _groups:
        _groups[group_id] = _GroupState()
    return _groups[group_id]


def add_user_message(group_id: str, user_id: str, text: str) -> str:
    """Record an incoming user message; returns the anonymised label used."""
    state = _state(group_id)
    label = state.label_for_user(user_id)
    _append(group_id, label, text)
    return label


def add_bot_message(group_id: str, text: str) -> None:
    _append(group_id, BOT_LABEL, text)


def _append(group_id: str, label: str, text: str) -> None:
    state = _state(group_id)
    state.history.append((label, text))
    window = get_settings().groupbot_context_window
    if len(state.history) > window:
        state.history = state.history[-window:]


def history_excluding_last(group_id: str) -> list[tuple[str, str]]:
    """Everything in the rolling window except the most recent entry
    (used as "context so far" when building the prompt for the latest
    message, which is rendered separately)."""
    state = _state(group_id)
    return state.history[:-1]


def reset(group_id: str) -> None:
    _groups.pop(group_id, None)
