"""codex brain: one `codex exec` subprocess per message, with a model
fallback chain — the Codex CLI counterpart to app/services/opencode_agent.py.

opencode_agent.py talks to a long-running `opencode serve` process because
opencode is a Bun-runtime app with a ~25-30s fixed cold-start cost per
subprocess spawn (see that module's docstring). Codex CLI is a native
compiled binary with no such cost (measured 2026-07-22: repeated `codex
exec` calls land in the same ~15-20s range every time, dominated by model
latency, not process startup — see the Phase 0 spike in the migration plan
for the raw numbers). That means the entire reason opencode_agent.py needs
a persistent server + worker pool + watchdog/restart scripts simply doesn't
apply here: spawning one `codex exec` per message, and killing that one
subprocess on timeout, can't leak a shared worker or wedge other in-flight
messages, because there is no shared process to begin with.

The agent definitions (C:\\Users\\user\\group-bot-scripts\\codex-groupbot-
workdir\\AGENTS.md, C:\\Users\\user\\codex-admin-workdir\\AGENTS.md) carry
the full system prompts — codex reads the nearest AGENTS.md walking from
its working directory (`-C`) up to the project root, confirmed against
developers.openai.com/codex/guides/agents-md and a live spike run. This
module only supplies the "conversation context + latest message" user
prompt (see `build_prompt`, copied from opencode_agent.py unchanged) and
drives the `codex exec` subprocess per fallback-chain rung.

Tool/file/command access is controlled by CLI flags instead of an opencode
agent's frontmatter `tools:` block: the group agent runs with
`-s read-only` (no file/command access at all, matching groupbot.md's
`tools: *: false`); the admin agent runs with `-s danger-full-access`
(matching admin.md's "no per-tool confirmation gate — the allowlist is the
only access control").

Deliberately NOT ported from opencode_agent.py in this pass: the on-demand
NotebookLM MCP toggle (ensure_mcp_serve_running / shutdown_mcp_serve_if_idle
/ etc). That's the single most complex, highest-risk piece of the opencode
version and isn't related to the instability this migration exists to fix
— tracked as a fast-follow once this base migration is proven stable.
"""

from __future__ import annotations

import asyncio
import glob
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid

# codex.cmd is a Windows batch-file shim (npm global install), so every
# spawn goes through cmd.exe underneath. Without both of the flags below,
# spawning it from a console-less parent (uvicorn started hidden/as a
# background service) pops a new visible console window per rung, per
# message — annoying enough during normal desktop use to look like a bug on
# its own (found live, 2026-07-22: this is what the user was seeing, not the
# opencode watchdog, which was already disabled by then).
#
# creationflags=CREATE_NO_WINDOW alone was NOT enough — verified live with a
# process monitor that a conhost.exe child still spawned under just that
# flag. Only creationflags + an explicit STARTUPINFO with
# STARTF_USESHOWWINDOW/SW_HIDE together produced a child with no window
# handle at all (confirmed via user32 IsWindowVisible on every process in
# the cmd->node->codex chain). Keep both; dropping either one regresses.
_WINDOWS_NO_WINDOW_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


_RESOLVED_BIN_CACHE: dict[str, str] = {}


def _resolve_codex_binary(configured_bin: str) -> str:
    """Prefer the vendored native codex.exe over the codex.cmd npm shim.

    Found live, 2026-07-22: even with both window-hiding flags below, a
    console window still appeared for every message. Root cause: codex.cmd
    is a 3-hop spawn chain — cmd.exe -> node.exe (codex.js) -> codex.exe
    (the real vendored binary) — and node.exe re-spawns codex.exe with
    plain child_process.spawn (no windowsHide flag of its own). Our
    creationflags/startupinfo only suppress a console for the *direct*
    child of our own subprocess call (cmd.exe), so the grandchild
    (codex.exe) still gets Windows auto-allocating it a fresh, visible
    console. Spawning the vendored codex.exe directly makes it the direct
    child instead, so our suppression flags actually reach it.

    Falls back to the configured (.cmd) path if the vendored binary can't
    be found (e.g. a different codex install layout after an upgrade) —
    degrades to "window sometimes pops up again" rather than a hard error.
    """
    if configured_bin in _RESOLVED_BIN_CACHE:
        return _RESOLVED_BIN_CACHE[configured_bin]

    resolved = configured_bin
    if configured_bin.lower().endswith(".cmd"):
        npm_root = os.path.dirname(configured_bin)
        pattern = os.path.join(
            npm_root,
            "node_modules",
            "@openai",
            "codex",
            "node_modules",
            "@openai",
            "codex-win32-x64",
            "vendor",
            "*",
            "bin",
            "codex.exe",
        )
        matches = glob.glob(pattern)
        if matches:
            resolved = matches[0]
        else:
            logger.warning(
                f"could not find vendored codex.exe near {configured_bin}, "
                "falling back to the .cmd shim (console windows may reappear)"
            )

    _RESOLVED_BIN_CACHE[configured_bin] = resolved
    return resolved


def _hidden_startupinfo() -> subprocess.STARTUPINFO | None:
    if sys.platform != "win32":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si

from app.config import Settings, get_settings
from app.logger import logger
from app.services import conversation, line_client

# Same marker list as opencode_agent.py — codex_exec surfaces upstream
# rate-limit errors on stderr/exit code instead of in a JSON response body,
# but the wording of the underlying provider errors is the same family.
_RATE_LIMIT_MARKERS = (
    "429",
    "rate limit",
    "rate_limit",
    "quota",
    "too many requests",
    "resourceexhausted",
    "resource_exhausted",
)

_SILENT_RE = re.compile(
    r"""^[\s"'「」『』(（]*\(silent\)[\s"'「」『』)）.,。，!！]*$""",
    re.IGNORECASE,
)


def should_suppress(text: str | None) -> bool:
    """True if the model's answer means "don't send anything to the group"."""
    if text is None:
        return True
    stripped = text.strip()
    if not stripped:
        return True
    return bool(_SILENT_RE.match(stripped))


def _load_reference_doc(workdir: str, filename: str) -> str:
    try:
        with open(os.path.join(workdir, filename), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def group_reference_context(workdir: str) -> str:
    """Inline the group persona's small reference docs (extension-table
    detail, receivable-buildings list) directly into the prompt, instead of
    telling the model it can read them itself.

    Found live, 2026-07-22: giving the group bot (sandbox -s read-only)
    permission and instructions to read these files on demand made it
    invoke codex's shell/exec tool (spawning powershell.exe) to satisfy the
    read. That nested spawn turned out to be one of the vectors behind a
    recurring visible console window (see codex_agent.py's binary-
    resolution and --ignore-user-config comments for the others). The
    group bot's actual need is always "the same ~2.5KB of static facts,
    every time" — there's no reason to let the model invoke a live tool
    for that at all. Inlining removes the group bot's only reason to ever
    call the shell/exec tool, closing this vector at the source rather
    than trying to suppress whatever window codex's shell tool spawns.
    OPENCODE_GROUPBOT_STATUS.md (architecture background, not business
    facts) is deliberately NOT inlined here — the group persona's own
    anti-disclosure rule already means it shouldn't be handing out system
    internals over the group chat anyway; that doc stays admin-only.
    """
    docs = [
        _load_reference_doc(workdir, "GA_EXTENSIONS.md"),
        _load_reference_doc(workdir, "SCHOOL_BUILDINGS.md"),
    ]
    return "\n\n".join(d for d in docs if d)


def build_prompt(
    history: list[tuple[str, str]],
    latest_label: str,
    latest_text: str,
    *,
    was_mentioned: bool = False,
    is_reply_to_bot: bool = False,
    reference_context: str = "",
) -> str:
    """Assemble the "context + latest message" user prompt. Identical to
    opencode_agent.build_prompt — the system prompt lives entirely in the
    codex AGENTS.md, this is only the per-turn payload — plus an optional
    inlined reference_context block (see group_reference_context)."""
    if history:
        context_lines = "\n".join(f"[{label}]: {text}" for label, text in history)
    else:
        context_lines = "(尚無對話紀錄)"
    prompt = ""
    if reference_context:
        prompt += f"以下是可查閱的參考資料(不是對話內容,只是背景資訊):\n{reference_context}\n\n"
    prompt += (
        "以下是最近的對話紀錄(供判斷上下文用):\n"
        f"{context_lines}\n\n"
        "最新訊息(請針對這一則回覆):\n"
        f"[{latest_label}]: {latest_text}"
    )
    if was_mentioned or is_reply_to_bot:
        reason = (
            "有人直接 @ 你本人,不是廣播 @所有人"
            if was_mentioned
            else "有人直接回覆(swipe-to-reply)你之前傳的訊息"
        )
        prompt += (
            f"\n\n(提示:這則訊息{reason}。"
            "既然是直接對你說話,請正常回覆、不要輸出 (silent),就算內容不完全"
            "屬於總務處業務範圍,也請盡量給出有幫助的回應,或告知使用者"
            "你能協助的範圍與正確窗口。)"
        )
    return prompt


def build_admin_prompt(history: list[tuple[str, str]], text: str) -> str:
    """Prompt for the admin DM channel — deliberately NOT build_prompt.

    Found live, 2026-07-22: admin DM "你現在使用什麼模型?" got back a
    request for conversation history instead of an answer. Root cause:
    handle_admin_message used to call build_prompt, whose "以下是最近的
    對話紀錄(供判斷上下文用)" / "(尚無對話紀錄)" framing exists to help
    the GROUP bot decide whether a message is on-topic (the (silent)
    protocol) — the admin channel has no such relevance judgment to make
    (every DM always gets a real reply), so that framing is inert
    scaffolding there. A smaller model (gpt-5.4-mini) read the empty-
    history line and "supplied for context" wording as a literal request
    for more input rather than internal framing to ignore. Admin gets its
    own minimal prompt instead: just the message, prefixed with prior
    turns only when there are any.
    """
    if not history:
        return text
    context_lines = "\n".join(f"[{label}]: {msg}" for label, msg in history)
    return f"先前對話:\n{context_lines}\n\n目前訊息:\n{text}"


def _looks_rate_limited(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _RATE_LIMIT_MARKERS)


async def _run_rung(
    settings: Settings,
    model: str,
    prompt: str,
    *,
    codex_bin: str,
    sandbox: str,
    timeout_seconds: int,
    cwd: str,
) -> tuple[str | None, str]:
    """Run one fallback-chain rung as a `codex exec` subprocess.

    Returns (answer_or_None, failure_reason). failure_reason is "" on
    success, otherwise one of: "spawn_error" / "timeout" / "exec_error" /
    "rate_limited" / "no_answer".
    """
    with tempfile.TemporaryDirectory(prefix="codexbot-") as tmpdir:
        output_path = os.path.join(tmpdir, "last-message.txt")
        args = [
            _resolve_codex_binary(codex_bin),
            "exec",
            "--skip-git-repo-check",
            # Without this, every call also loads the desktop user's own
            # $CODEX_HOME/config.toml — found live, 2026-07-22: that config
            # spawns MCP servers (node_repl, notebooklm-mcp), GUI-capable
            # plugins (browser/computer-use), and a notify hook
            # (codex-computer-use.exe) on every turn. None of that is ours
            # to run per LINE message, it's pure overhead, and the
            # computer-use/notify pieces are a second console/window vector
            # entirely separate from the codex.exe spawn chain itself.
            # Auth (CODEX_HOME/auth.json) is unaffected by this flag.
            "--ignore-user-config",
            "-C",
            cwd,
            "-s",
            sandbox,
            "-m",
            model,
            "-o",
            output_path,
            prompt,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=_WINDOWS_NO_WINDOW_FLAGS,
                startupinfo=_hidden_startupinfo(),
            )
        except OSError as exc:
            logger.error(f"failed to spawn codex exec: {exc}")
            return None, "spawn_error"

        try:
            _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            # Kill just this one subprocess — unlike opencode serve's
            # shared worker pool, there is nothing else in flight that a
            # kill here could affect, so this needs no abort-then-delete
            # dance, no recovery-delay pause before the next rung, and no
            # watchdog to catch a leak. See module docstring.
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass  # already exited on its own between the timeout and the kill
            logger.warning(f"model={model} timed out after {timeout_seconds}s")
            return None, "timeout"

        stderr_text = stderr.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            if _looks_rate_limited(stderr_text):
                logger.warning(f"model={model} looks rate-limited: {stderr_text[:300]}")
                return None, "rate_limited"
            logger.warning(f"model={model} exec failed (code={proc.returncode}): {stderr_text[:300]}")
            return None, "exec_error"

        try:
            with open(output_path, encoding="utf-8") as fh:
                answer = fh.read().strip()
        except OSError:
            answer = ""

        if not answer:
            logger.warning(f"model={model} produced no output: {stderr_text[:300]}")
            return None, "no_answer"

        return answer, ""


async def run_model_chain(
    prompt: str,
    *,
    cwd: str | None = None,
    sandbox: str | None = None,
    timeout_seconds: int | None = None,
    primary_timeout_seconds: int | None = None,
    model_chain: list[str] | None = None,
) -> tuple[str | None, str | None]:
    """Try each model in settings.codex_model_chain (or the given
    model_chain override, e.g. settings.codex_admin_model_chain) in order
    until one succeeds. Mirrors opencode_agent.run_model_chain's shape.

    Returns (answer, model_used); (None, None) if every rung failed.
    """
    settings = get_settings()
    cwd = cwd or settings.groupbot_codex_workdir
    sandbox = sandbox or "read-only"
    timeout_seconds = timeout_seconds or settings.groupbot_codex_model_timeout_seconds
    primary_timeout_seconds = primary_timeout_seconds or settings.groupbot_codex_primary_model_timeout_seconds
    chain = model_chain if model_chain is not None else settings.codex_model_chain
    _ = f"codexbot-{uuid.uuid4().hex[:8]}"  # kept for log correlation only

    for index, model in enumerate(chain):
        rung_timeout = primary_timeout_seconds if index == 0 else timeout_seconds
        started = time.monotonic()
        answer, reason = await _run_rung(
            settings,
            model,
            prompt,
            codex_bin=settings.groupbot_codex_bin,
            sandbox=sandbox,
            timeout_seconds=rung_timeout,
            cwd=cwd,
        )
        elapsed = time.monotonic() - started
        if answer is not None:
            logger.info(f"codex model_chain: model={model} succeeded in {elapsed:.1f}s")
            return answer, model
        logger.info(f"codex model_chain: model={model} failed ({reason}) after {elapsed:.1f}s")

    logger.error("codex model_chain: all rungs failed, staying silent")
    return None, None


_MENTION_FALLBACK_REPLY = (
    "你好,這則訊息看起來跟總務處業務比較沒關係,如果有總務相關的問題"
    "(採購、修繕、場地、動植物事件等)歡迎告訴我更多細節。"
)


async def _with_greeting(group_id: str, user_id: str, answer: str) -> str:
    """Prefix a group reply with "{顯示名稱}老師好," — identical to
    opencode_agent._with_greeting."""
    display_name = conversation.get_display_name(group_id, user_id)
    if display_name is None:
        display_name = await line_client.get_group_member_display_name(group_id, user_id)
        if display_name:
            conversation.cache_display_name(group_id, user_id, display_name)
    if not display_name:
        return answer
    return f"{display_name}老師好,{answer}"


async def handle_message(
    group_id: str,
    user_id: str,
    reply_token: str,
    text: str,
    was_mentioned: bool = False,
    is_reply_to_bot: bool = False,
) -> None:
    """Single entry point wired up by the LINE webhook when
    GROUPBOT_BRAIN=codex. Same contract/behaviour as
    opencode_agent.handle_message — see that function's docstring for the
    full (silent)-suppression / force-reply / greeting-prefix rationale,
    which is unchanged here.
    """
    settings = get_settings()
    label = conversation.add_user_message(group_id, user_id, text)
    history = conversation.history_excluding_last(group_id)
    force_reply = was_mentioned or is_reply_to_bot
    reference_context = group_reference_context(settings.groupbot_codex_workdir)
    prompt = build_prompt(
        history,
        label,
        text,
        was_mentioned=was_mentioned,
        is_reply_to_bot=is_reply_to_bot,
        reference_context=reference_context,
    )

    answer, model_used = await run_model_chain(prompt)

    if answer is None:
        if force_reply:
            logger.error(f"group={group_id} directly addressed but all model rungs failed")
        else:
            logger.info(f"group={group_id} model_used={model_used} all rungs failed, staying silent")
        return

    if should_suppress(answer):
        if not force_reply:
            logger.info(f"group={group_id} model={model_used} suppressed reply (silent/empty)")
            return
        logger.info(f"group={group_id} model={model_used} said (silent) despite direct address; using fallback")
        answer = _MENTION_FALLBACK_REPLY

    assert answer is not None  # should_suppress already filtered None out
    answer = await _with_greeting(group_id, user_id, answer)
    conversation.add_bot_message(group_id, answer)
    sent_ids = await line_client.send_text(reply_token, group_id, answer)
    for message_id in sent_ids:
        conversation.record_bot_message_id(group_id, message_id)


async def handle_admin_message(user_id: str, reply_token: str, text: str) -> None:
    """Entry point for admin 1:1 DMs when GROUPBOT_BRAIN=codex. Unlike the
    group agent, this always replies and runs with `-s danger-full-access`
    (full read/write/edit/bash access), scoped to
    settings.groupbot_codex_admin_workdir. No per-tool confirmation gate —
    the admin allowlist check in app/services/allowlist.py is the only
    access control, same as the opencode admin agent.

    The NotebookLM MCP on-demand toggle from opencode_agent.py is not
    ported here — see module docstring.
    """
    settings = get_settings()
    conversation.add_user_message(user_id, user_id, text)
    history = conversation.history_excluding_last(user_id)
    prompt = build_admin_prompt(history, text)

    answer, model_used = await run_model_chain(
        prompt,
        cwd=settings.groupbot_codex_admin_workdir,
        sandbox="danger-full-access",
        timeout_seconds=settings.groupbot_codex_admin_model_timeout_seconds,
        primary_timeout_seconds=settings.groupbot_codex_admin_primary_model_timeout_seconds,
        model_chain=settings.codex_admin_model_chain,
    )

    if answer is None:
        logger.error(f"admin DM user={user_id[:8]}… all model rungs failed")
        await line_client.send_text(reply_token, user_id, "抱歉,所有模型都暫時失敗了,請稍後再試一次。")
        return

    logger.info(f"admin DM user={user_id[:8]}… model={model_used} replied")
    conversation.add_bot_message(user_id, answer)
    await line_client.send_text(reply_token, user_id, answer)
