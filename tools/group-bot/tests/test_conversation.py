"""Tests for the in-memory rolling conversation context."""

from app.config import get_settings
from app.services import conversation


def _reset(group_id: str) -> None:
    conversation.reset(group_id)


def test_user_labels_are_stable_and_anonymised() -> None:
    _reset("G1")
    label_a1 = conversation.add_user_message("G1", "U-alice", "hi")
    label_b = conversation.add_user_message("G1", "U-bob", "yo")
    label_a2 = conversation.add_user_message("G1", "U-alice", "again")

    assert label_a1 == "使用者A"
    assert label_b == "使用者B"
    assert label_a2 == "使用者A"


def test_different_groups_have_independent_label_maps() -> None:
    _reset("G1")
    _reset("G2")
    assert conversation.add_user_message("G1", "U-x", "hi") == "使用者A"
    # Same physical user, different group, but first speaker in G2 is still A.
    assert conversation.add_user_message("G2", "U-x", "hi") == "使用者A"


def test_history_excludes_the_message_just_added() -> None:
    _reset("G1")
    conversation.add_user_message("G1", "U-a", "first")
    conversation.add_user_message("G1", "U-b", "second")
    history = conversation.history_excluding_last("G1")
    assert history == [("使用者A", "first")]


def test_bot_messages_are_recorded_with_bot_label() -> None:
    _reset("G1")
    conversation.add_user_message("G1", "U-a", "問題")
    conversation.add_bot_message("G1", "回覆內容")
    history = conversation.history_excluding_last("G1")
    assert history[-1] == ("使用者A", "問題")


def test_rolling_window_caps_at_configured_size(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "groupbot_context_window", 3)
    _reset("G1")
    for i in range(5):
        conversation.add_user_message("G1", "U-a", f"msg{i}")
    # Window is 3; history_excluding_last drops the newest, so at most 2 remain visible.
    history = conversation.history_excluding_last("G1")
    assert len(history) == 2
    assert history == [("使用者A", "msg2"), ("使用者A", "msg3")]


def test_display_name_is_unset_until_cached() -> None:
    _reset("G1")
    assert conversation.get_display_name("G1", "U-a") is None
    conversation.cache_display_name("G1", "U-a", "陳大文")
    assert conversation.get_display_name("G1", "U-a") == "陳大文"


def test_display_names_are_independent_per_group() -> None:
    _reset("G1")
    _reset("G2")
    conversation.cache_display_name("G1", "U-a", "陳大文")
    assert conversation.get_display_name("G2", "U-a") is None


def test_bot_message_id_is_recognised_as_reply_to_bot() -> None:
    _reset("G1")
    assert conversation.is_reply_to_bot("G1", "msg-1") is False
    conversation.record_bot_message_id("G1", "msg-1")
    assert conversation.is_reply_to_bot("G1", "msg-1") is True


def test_is_reply_to_bot_false_for_unknown_or_missing_id() -> None:
    _reset("G1")
    conversation.record_bot_message_id("G1", "msg-1")
    assert conversation.is_reply_to_bot("G1", "msg-999") is False
    assert conversation.is_reply_to_bot("G1", None) is False


def test_bot_message_id_history_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(conversation, "_BOT_MESSAGE_ID_HISTORY", 3)
    _reset("G1")
    for i in range(4):
        conversation.record_bot_message_id("G1", f"msg-{i}")
    # maxlen=3: the oldest ID (msg-0) has been evicted, the rest remain.
    assert conversation.is_reply_to_bot("G1", "msg-0") is False
    assert conversation.is_reply_to_bot("G1", "msg-3") is True
