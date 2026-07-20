"""Tools the desktop agent can call: file I/O and shell execution.

All paths are resolved against `hermes_desktop_root` from settings. Calls that
stay inside that root are considered safe for read-only tools; everything
else (or any write/shell call) is flagged dangerous by `is_dangerous` and
must be confirmed by the caller before the tool actually runs.
"""

import asyncio
from pathlib import Path

from app.config import get_settings

MAX_READ_CHARS = 4000
MAX_SHELL_OUTPUT_CHARS = 3000
MAX_LIST_ENTRIES = 200
SHELL_TIMEOUT_SECONDS = 120


class ToolError(Exception):
    """Raised when a tool cannot complete; the message is shown to the model."""


def _root() -> Path:
    return Path(get_settings().hermes_desktop_root).resolve()


def resolve_path(raw: str) -> tuple[Path, bool]:
    """Resolve `raw` against the desktop root; return (path, is_in_scope)."""
    root = _root()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    in_scope = resolved == root or root in resolved.parents
    return resolved, in_scope


def is_dangerous(tool_name: str, args: dict) -> bool:
    """Whether this tool call must be confirmed by the user before running."""
    if tool_name in {"write_file", "run_shell"}:
        return True
    if tool_name in {"read_file", "list_dir"}:
        _, in_scope = resolve_path(args.get("path", ""))
        return not in_scope
    return True  # unknown tools are rejected by the caller anyway


async def read_file(path: str) -> str:
    resolved, _ = resolve_path(path)

    def _read() -> str:
        if not resolved.is_file():
            raise ToolError(f"找不到檔案:{resolved}")
        text = resolved.read_text(encoding="utf-8", errors="replace")
        if len(text) > MAX_READ_CHARS:
            return text[:MAX_READ_CHARS] + f"\n...(已截斷,檔案共 {len(text)} 字元)"
        return text

    return await asyncio.to_thread(_read)


async def list_dir(path: str) -> str:
    resolved, _ = resolve_path(path)

    def _list() -> str:
        if not resolved.is_dir():
            raise ToolError(f"找不到資料夾:{resolved}")
        entries = sorted(resolved.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        lines = [f"{'📄' if e.is_file() else '📁'} {e.name}" for e in entries[:MAX_LIST_ENTRIES]]
        if len(entries) > MAX_LIST_ENTRIES:
            lines.append(f"...(還有 {len(entries) - MAX_LIST_ENTRIES} 項,已省略)")
        return "\n".join(lines) or "(空資料夾)"

    return await asyncio.to_thread(_list)


async def write_file(path: str, content: str) -> str:
    resolved, _ = resolve_path(path)

    def _write() -> str:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return f"已寫入 {resolved}({len(content)} 字元)"

    return await asyncio.to_thread(_write)


async def run_shell(command: str, cwd: str | None = None) -> str:
    resolved_cwd = resolve_path(cwd)[0] if cwd else _root()
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(resolved_cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=SHELL_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise ToolError(f"指令執行逾時({SHELL_TIMEOUT_SECONDS} 秒),已中止") from exc

    output = stdout.decode("utf-8", errors="replace")
    if len(output) > MAX_SHELL_OUTPUT_CHARS:
        output = output[:MAX_SHELL_OUTPUT_CHARS] + f"\n...(輸出已截斷,共 {len(output)} 字元)"
    return f"(exit code {proc.returncode})\n{output or '(無輸出)'}"


TOOL_HANDLERS = {
    "read_file": read_file,
    "list_dir": list_dir,
    "write_file": write_file,
    "run_shell": run_shell,
}
