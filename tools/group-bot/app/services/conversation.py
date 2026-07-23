"""In-memory rolling conversation context, per LINE group.

Keeps the last N messages (default 10, configurable via
GROUPBOT_CONTEXT_WINDOW) per group, including the bot's own replies.
Speakers are anonymised as 使用者A/B/C… based on a stable per-group mapping
of LINE userId -> letter. Everything lives in process memory only; a
restart clears all history (documented limitation, see README).
"""

from __future__ import annotations

import string
from collections import deque
from dataclasses import dataclass, field

from app.config import get_settings

BOT_LABEL = "機器人"

# How many of the bot's own sent message IDs to remember per group, so a
# later "swipe-to-reply" quote can be recognised as targeting the bot (see
# record_bot_message_id / is_reply_to_bot). A LINE quote reply can only
# target a fairly recent message in practice, so this doesn't need to track
# more than the rolling context window does.
_BOT_MESSAGE_ID_HISTORY = 50


@dataclass
class _GroupState:
    history: list[tuple[str, str]] = field(default_factory=list)  # (label, text)
    user_labels: dict[str, str] = field(default_factory=dict)  # userId -> 使用者A/B/...
    display_names: dict[str, str] = field(default_factory=dict)  # userId -> LINE displayName
    bot_message_ids: deque[str] = field(
        default_factory=lambda: deque(maxlen=_BOT_MESSAGE_ID_HISTORY)
    )
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


def get_display_name(group_id: str, user_id: str) -> str | None:
    """Cached LINE displayName for a group member, or None if never fetched
    (or the fetch failed) — see app/services/line_client.py's group member
    profile lookup, which populates this via cache_display_name."""
    return _state(group_id).display_names.get(user_id)


def cache_display_name(group_id: str, user_id: str, display_name: str) -> None:
    _state(group_id).display_names[user_id] = display_name


def record_bot_message_id(group_id: str, message_id: str) -> None:
    """Remember a message ID the bot just sent into this group, so a later
    swipe-to-reply quote of it can be recognised (see is_reply_to_bot)."""
    _state(group_id).bot_message_ids.append(message_id)


def is_reply_to_bot(group_id: str, quoted_message_id: str | None) -> bool:
    """True if quoted_message_id refers to a message the bot itself sent
    into this group (a LINE swipe-to-reply targeting the bot)."""
    if not quoted_message_id:
        return False
    return quoted_message_id in _state(group_id).bot_message_ids
