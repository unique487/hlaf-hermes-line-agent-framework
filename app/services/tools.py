"""Path-scope and danger-classification logic shared by the Claude Code hook.

Actual tool execution (Read/Write/Edit/Bash/...) is done by the Claude Code
CLI itself now — this module no longer runs any tools. It only decides
whether a given tool call is "dangerous" (must be confirmed via LINE before
the hook approves it) and resolves paths relative to `desktop_root`.

Imported directly by `scripts/claude_confirm_hook.py` (the Claude Code
PreToolUse hook), which runs as a separate subprocess in the same repo/venv.
"""

from pathlib import Path

from app.config import get_settings

# Claude Code built-in tools that always require confirmation, regardless of
# the path/command involved.
_ALWAYS_DANGEROUS_TOOLS = {"Bash", "Write", "Edit", "MultiEdit"}

# Read-only tools: only dangerous if the path they target falls outside
# `desktop_root`.
_SCOPE_CHECKED_TOOLS = {"Read", "Glob", "Grep", "LS"}


def _root() -> Path:
    return Path(get_settings().desktop_root).resolve()


def resolve_path(raw: str) -> tuple[Path, bool]:
    """Resolve `raw` against the desktop root; return (path, is_in_scope)."""
    root = _root()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    in_scope = resolved == root or root in resolved.parents
    return resolved, in_scope


def _extract_path(tool_input: dict) -> str:
    """Best-effort extraction of the path a scope-checked tool targets.

    Different Claude Code built-in tools use different argument names:
    Read/Glob/Grep/LS use `file_path` or `path` (the exact key can vary by
    CLI version); Glob/Grep may also carry a `pattern` with no directory,
    in which case we fall back to the (unresolved) pattern text — this
    means a directory-less pattern like `**/*.py` resolves relative to
    `desktop_root` and is therefore treated as in-scope.
    """
    return tool_input.get("file_path") or tool_input.get("path") or tool_input.get("pattern") or ""


def is_dangerous(tool_name: str, tool_input: dict) -> bool:
    """Whether this tool call must be confirmed by the user before running."""
    if tool_name in _ALWAYS_DANGEROUS_TOOLS:
        return True
    if tool_name in _SCOPE_CHECKED_TOOLS:
        path = _extract_path(tool_input)
        if not path:
            return False
        _, in_scope = resolve_path(path)
        return not in_scope
    return True  # unknown/unrecognized tools default to requiring confirmation
