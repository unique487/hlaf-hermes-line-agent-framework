"""Tests for the desktop-agent path-scope/danger-classification logic.

Actual tool execution now happens inside the Claude Code CLI itself; this
module only decides whether a tool call needs LINE confirmation, so these
tests cover `resolve_path` and `is_dangerous` against Claude Code's built-in
tool names (Bash/Write/Edit/MultiEdit/Read/Glob/Grep/LS).
"""

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


def test_is_dangerous_bash_write_edit_multiedit_always_true() -> None:
    assert tools.is_dangerous("Bash", {"command": "dir"})
    assert tools.is_dangerous("Write", {"file_path": "a.txt", "content": "x"})
    assert tools.is_dangerous("Edit", {"file_path": "a.txt"})
    assert tools.is_dangerous("MultiEdit", {"file_path": "a.txt"})


def test_is_dangerous_read_depends_on_scope(_root) -> None:
    assert not tools.is_dangerous("Read", {"file_path": "in-scope.txt"})
    assert tools.is_dangerous("Read", {"file_path": str(_root.parent / "outside.txt")})


def test_is_dangerous_ls_glob_grep_depend_on_scope(_root) -> None:
    assert not tools.is_dangerous("LS", {"path": str(_root)})
    assert tools.is_dangerous("LS", {"path": str(_root.parent)})
    assert not tools.is_dangerous("Glob", {"path": str(_root), "pattern": "*.py"})
    assert tools.is_dangerous("Grep", {"path": str(_root.parent), "pattern": "TODO"})


def test_is_dangerous_scope_checked_tool_without_path_is_safe() -> None:
    # No extractable path at all (e.g. a pattern-only Glob with no explicit
    # dir) resolves relative to the desktop root, so it's treated as in-scope.
    assert not tools.is_dangerous("Glob", {})


def test_is_dangerous_unknown_tool_defaults_true() -> None:
    assert tools.is_dangerous("delete_everything", {})
