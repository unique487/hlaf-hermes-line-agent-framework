"""Tests for group allowlist logic."""

from app.config import get_settings
from app.services import allowlist


def test_allowed_group_passes(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "line_allowed_groups", "G1,G2")
    assert allowlist.is_group_allowed("G1") is True
    assert allowlist.is_group_allowed("G2") is True


def test_unknown_group_rejected(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "line_allowed_groups", "G1,G2")
    assert allowlist.is_group_allowed("G-stranger") is False


def test_empty_group_id_rejected(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "line_allowed_groups", "G1")
    assert allowlist.is_group_allowed("") is False


def test_whitespace_in_env_list_is_trimmed(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "line_allowed_groups", " G1 , G2 ,, ")
    assert allowlist.is_group_allowed("G1") is True
    assert allowlist.is_group_allowed("G2") is True
