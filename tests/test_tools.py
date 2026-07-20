"""Tests for the desktop agent's file/shell tools and scope checks."""

import sys

import pytest

from app.config import get_settings
from app.services import tools


@pytest.fixture(autouse=True)
def _root(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "hermes_desktop_root", str(tmp_path))
    return tmp_path


def test_resolve_path_relative_stays_in_scope(_root) -> None:
    resolved, in_scope = tools.resolve_path("notes.txt")
    assert in_scope
    assert resolved == (_root / "notes.txt").resolve()


def test_resolve_path_outside_root_is_out_of_scope(_root) -> None:
    outside = _root.parent / "elsewhere.txt"
    resolved, in_scope = tools.resolve_path(str(outside))
    assert not in_scope
    assert resolved == outside.resolve()


def test_is_dangerous_write_and_shell_always_true() -> None:
    assert tools.is_dangerous("write_file", {"path": "a.txt"})
    assert tools.is_dangerous("run_shell", {"command": "dir"})


def test_is_dangerous_read_depends_on_scope(_root) -> None:
    assert not tools.is_dangerous("read_file", {"path": "in-scope.txt"})
    assert tools.is_dangerous("read_file", {"path": str(_root.parent / "outside.txt")})


async def test_write_then_read_file_roundtrip(_root) -> None:
    result = await tools.write_file("sub/note.txt", "hello world")
    assert "已寫入" in result
    content = await tools.read_file("sub/note.txt")
    assert content == "hello world"


async def test_read_file_missing_raises_tool_error(_root) -> None:
    with pytest.raises(tools.ToolError):
        await tools.read_file("missing.txt")


async def test_list_dir_shows_entries(_root) -> None:
    (_root / "a.txt").write_text("x", encoding="utf-8")
    (_root / "sub").mkdir()
    listing = await tools.list_dir(".")
    assert "a.txt" in listing
    assert "sub" in listing


async def test_list_dir_missing_raises_tool_error(_root) -> None:
    with pytest.raises(tools.ToolError):
        await tools.list_dir("no-such-dir")


async def test_run_shell_returns_output_and_exit_code(_root) -> None:
    python = sys.executable
    result = await tools.run_shell(f'"{python}" -c "print(1+1)"')
    assert "(exit code 0)" in result
    assert "2" in result
