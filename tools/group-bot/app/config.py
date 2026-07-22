"""Application configuration, loaded from environment variables / .env file."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central application settings.

    Variable names for the LINE credentials intentionally match the
    original Hermes gateway's `.env` (`LINE_CHANNEL_SECRET`,
    `LINE_CHANNEL_ACCESS_TOKEN`, `LINE_ALLOWED_GROUPS`, ...) so the backed-up
    `.env` file can be copied in as-is. New settings specific to this
    service are prefixed `GROUPBOT_`.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "GroupBot"
    app_env: str = "development"
    debug: bool = False
    host: str = "0.0.0.0"
    log_level: str = "INFO"

    # --- LINE Messaging API (group bot official account) ---
    line_channel_secret: str = ""
    line_channel_access_token: str = ""

    # --- Group allowlist ---
    # Comma-separated LINE groupIds allowed to trigger a reply. Any user
    # inside an allowed group can trigger the bot (equivalent to the
    # original Hermes `GATEWAY_ALLOW_ALL_USERS=true` behaviour); DMs and
    # non-listed groups are silently ignored.
    line_allowed_groups: str = ""

    # --- Admin DM allowlist ---
    # Comma-separated LINE userIds allowed to DM this account directly and
    # get the unrestricted "admin" opencode agent (full tool access — read/
    # write/edit/bash — unlike the locked-down group agent). Matches the
    # original Hermes gateway's `LINE_ALLOWED_USERS`. DMs from anyone else
    # are silently ignored, same as the original gateway's default-deny.
    line_allowed_users: str = ""

    # --- This service's own settings ---
    groupbot_port: int = 8001

    # Path/command used to launch `opencode serve` (see
    # scripts/start-group-bot-background.ps1). The Python app itself never
    # spawns opencode per-message anymore — see groupbot_opencode_serve_*
    # below. Kept here only as the canonical path for the startup script.
    groupbot_opencode_bin: str = (
        r"C:\Users\user\AppData\Roaming\npm\node_modules\opencode-ai\bin\opencode.exe"
    )

    # --- Persistent opencode server (opencode serve) ---
    # Every LINE message used to spawn a fresh `opencode run` subprocess,
    # which on this machine has a ~25-30s *fixed* cold-start cost (Bun
    # runtime boot + config reload) before the model even starts
    # answering — on top of the model's own latency, this regularly blew
    # past the 45s per-rung timeout and made both the group bot and the
    # admin DM look completely dead (measured 2026-07-21). Switched to a
    # long-running `opencode serve` process (started once by
    # scripts/start-group-bot-background.ps1) that this app talks to over
    # HTTP instead — cuts a warm call to ~19s (measured, same big-pickle
    # model+prompt shape).
    groupbot_opencode_serve_host: str = "127.0.0.1"
    groupbot_opencode_serve_port: int = 4097

    # Name of the opencode agent definition (see
    # C:\Users\user\.config\opencode\agents\groupbot.md) that carries the
    # full 總務處 system prompt.
    groupbot_opencode_agent: str = "groupbot"

    # Model fallback chain, "provider/model,provider/model,...". Verified
    # against `opencode models` on 2026-07-21. Primary + secondary rungs
    # (opencode/big-pickle, opencode/deepseek-v4-flash-free) are the
    # opencode-zen free-tier models — same "opportunistic pool, no fixed
    # daily quota" caveat as the original Hermes chain (see 工作筆記),
    # which is why the openrouter/nvidia rungs stay as paid backstop.
    # opencode-zen auth lives in ~/.local/share/opencode/auth.json
    # (provider id "opencode"), not this .env — set via
    # `opencode auth login -p opencode`.
    # 2026-07-22: this default is now dead code in practice — see
    # GROUPBOT_MODEL_CHAIN in .env for the live value and the full history
    # of why it looks like this (NVIDIA direct first, then OpenRouter's
    # nemotron rung promoted to rung 2 after finding NVIDIA's direct API
    # enforces a shared per-account concurrency cap that every nvidia/*
    # rung competes for — including the now-removed nvidia/z-ai/glm-5.2).
    # Kept in sync with that override as the fallback default in case the
    # .env line is ever removed.
    groupbot_model_chain: str = (
        "nvidia/deepseek-ai/deepseek-v4-flash,"
        "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free,"
        "opencode/deepseek-v4-flash-free,"
        "opencode/big-pickle"
    )

    # Per-rung timeout in seconds, for every rung *except* the first (see
    # groupbot_primary_model_timeout_seconds). With opencode serve warm,
    # measured deepseek-v4-flash-free/openrouter calls finish in 8-20s;
    # this stays generous mainly for the slower paid nvidia backstops.
    groupbot_model_timeout_seconds: int = 45

    # Timeout for just the *first* rung (opencode/big-pickle). Measured
    # 2026-07-21: opencode-zen's free "big-pickle" is an opportunistic
    # pool (see 工作筆記) and was consistently hanging to the full 45s
    # before falling back, adding ~45s of dead wait to every single
    # message — the actual root cause of "still feels slow, Hermes was
    # faster" (Hermes' own stale_timeout_seconds was 30s, not 45s). This
    # does NOT change which model is tried first — big-pickle is still
    # rung 1 — it just gives up on it faster when it's degraded.
    groupbot_primary_model_timeout_seconds: int = 20

    # Pause after a timed-out rung before trying the next one. Measured
    # 2026-07-21: immediately creating a fresh session right after a
    # timed-out rung reliably made the *next several* rungs (different
    # models/providers) all fail instantly with an empty 0-token response
    # — cascading the whole fallback chain into "all rungs failed" for no
    # real reason. A short pause avoided the cascade in testing. Only
    # applied after "timeout" failures, not other failure reasons.
    groupbot_timeout_recovery_delay_seconds: float = 2.0

    # Rolling conversation context window per group (message count,
    # including the bot's own replies). Lost on restart — no persistence.
    groupbot_context_window: int = 10

    # --- Admin DM agent (private chat with full opencode tool access) ---
    # Name of the opencode agent definition (see
    # C:\Users\user\.config\opencode\agents\admin.md) — unlike "groupbot"
    # this one has read/write/edit/bash enabled, so the admin can actually
    # direct opencode to operate the computer via LINE DM.
    groupbot_admin_opencode_agent: str = "admin"

    # Model chain used only for admin DMs. Same pool as groupbot_model_chain,
    # but with opencode/big-pickle moved out of the first slot: measured
    # 2026-07-21, two consecutive admin DMs both timed out on big-pickle at
    # the full primary timeout before falling back to
    # deepseek-v4-flash-free, which succeeded both times in under 20s. That
    # 20s is pure dead wait on a rung that hasn't succeeded once in an admin
    # DM, plus it's the likely cause of "測試時說正常,實際卻無法寫入": if
    # the admin agent is mid tool-call (writing a file, running a command)
    # when the primary rung's timeout fires, the session gets deleted (see
    # _run_rung's timeout branch) before the write finishes, so it silently
    # never completes even though the model "was working on it". Starting
    # admin DMs on a rung that actually finishes quickly avoids both the
    # extra dead wait and that truncation risk. big-pickle stays in the
    # chain as a later fallback, just not first, for admin only — the group
    # bot's own chain/order is untouched.
    # 2026-07-22 08:xx: free-first (the overnight outage turned out to be an
    # opencode-serve worker-pool leak, not the free models themselves —
    # fixed by aborting on timeout).
    # 2026-07-22 (later same day, three times): reordered again — this
    # default is now dead code in practice, see GROUPBOT_ADMIN_MODEL_CHAIN
    # in .env for the live value and full history. Latest change: admin
    # starts on openrouter/nemotron (group starts on nvidia-direct) so the
    # two channels don't collide on the same provider's shared concurrency
    # cap when both get used around the same time. Kept in sync with that
    # override as the fallback default in case the .env line is ever
    # removed.
    groupbot_admin_model_chain: str = (
        "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free,"
        "nvidia/deepseek-ai/deepseek-v4-flash,"
        "opencode/deepseek-v4-flash-free,"
        "opencode/big-pickle"
    )
    # Timeout for the admin chain's first rung. Unlike the group bot's
    # rung-0 (which only ever needs to produce a short chat reply and can
    # safely be cut short at groupbot_primary_model_timeout_seconds), an
    # admin DM's first rung may be mid file-write/command-execution — give
    # it the same headroom as a normal rung instead of the group bot's
    # aggressive 20s so a real write/edit/bash call has time to finish.
    groupbot_admin_primary_model_timeout_seconds: int = 45

    # Working directory the admin agent's opencode calls run in.
    #
    # NOT G:\我的雲端硬碟\opencode\LINE (an earlier choice, reusing the
    # separate Claude LINE 桌面代理's own workspace) — confirmed 2026-07-21
    # that pointing an opencode session at that directory reliably breaks
    # it: opencode chokes trying to index that folder's few hundred files
    # (someone else's full project, on a Google-Drive mount), and every
    # subsequent call in that session returns an instant 0-token empty
    # response (looks identical to "all rungs failed" from the outside —
    # this was the actual cause of repeated "抱歉,所有模型都暫時失敗了"
    # replies, not a real provider outage). Verified by reproducing with a
    # raw HTTP call: same agent, same everything, only the directory
    # differed, and only the heavy directory failed.
    #
    # Fix: a small dedicated folder with nothing in it but a few small
    # context .md files, so the admin agent still has context on this whole
    # system without opencode trying to index someone else's unrelated
    # codebase.
    #
    # 2026-07-22: moved to a local C: path. The overnight outages were
    # NOT caused by the directory location (proven by A/B: symptoms
    # persisted identically whether this pointed at C: or G:; a fresh
    # opencode serve answers fine from the C: path). The real cause was an
    # opencode-serve worker leak (timed-out generations never aborted ->
    # workers exhausted -> everything hangs), now fixed by aborting on
    # timeout in _run_rung. With that understood, keeping this on C: is
    # strictly better: the folder is a handful of small .md files with no
    # git/.venv, so there's no reason to keep it on the Google-Drive mount
    # where a transient G: stall could add latency. Content was
    # re-synced from G:\我的雲端硬碟\opencode\LINE-admin and checksum-verified
    # before switching; that G: copy is kept as a backup, not deleted.
    groupbot_admin_cwd: str = r"C:\Users\user\opencode-admin-workdir"
    # Admin tasks (actually running tools) can run far longer than a plain
    # Q&A reply — give much more headroom than the group agent's 45s.
    # 2026-07-22: cut from 180 to 90 — live incident showed the admin
    # chain's 3 non-primary rungs (opencode-free, big-pickle, plus
    # whichever of openrouter/nvidia isn't rung 0) hitting their FULL 180s
    # ceiling one after another when providers are degraded, so a total
    # failure took ~6-7 minutes end-to-end before the user even got the
    # "抱歉,所有模型都暫時失敗了" apology. 90s is still 2x the group
    # agent's 45s (keeps meaningful headroom for a rung that's genuinely
    # mid tool-call), but halves the worst-case total wait.
    groupbot_admin_model_timeout_seconds: int = 90

    # --- On-demand MCP-enabled serve for admin DMs (2026-07-22) ---
    # A SEPARATE opencode serve process (its own port), only ever started
    # by an admin DM containing "notebooklm" and stopped by one containing
    # "關閉notebooklm" (see opencode_agent._wants_mcp_serve /
    # _wants_mcp_shutdown), or auto-killed after idling past
    # groupbot_admin_mcp_idle_timeout_seconds (checked opportunistically on
    # every admin DM — see opencode_agent.shutdown_mcp_serve_if_idle).
    #
    # Deliberately a separate process from groupbot_opencode_serve_port,
    # not a toggle on it: MCP servers are only read at serve *startup* from
    # the project opencode.json in that process's launch cwd, so enabling
    # MCP on the shared serve would require restarting it — aborting every
    # in-flight group/admin conversation using it. This one lives on its
    # own port that only an active admin MCP session ever talks to, so
    # starting/stopping/leaving-it-idle never touches the group bot.
    #
    # Its workdir's opencode.json enables only notebooklm-mcp (not the
    # other 4 global MCPs — firebase/playwright/open-computer-use/
    # obsidian are heavier and not what this is for). This is also why the
    # main group-bot serve had to have ALL 5 disabled in the first place:
    # each one gets re-spawned per opencode session and never reaped,
    # degrading a serve to send-timeouts within ~15 minutes (2026-07-22
    # incident — see 工作筆記/opencode-serve-degradation-rootcause memory).
    groupbot_admin_mcp_serve_port: int = 4099
    groupbot_admin_mcp_workdir: str = r"C:\Users\user\group-bot-scripts\opencode-serve-mcp-workdir"
    groupbot_admin_mcp_idle_timeout_seconds: int = 1800

    @property
    def opencode_serve_base_url(self) -> str:
        return f"http://{self.groupbot_opencode_serve_host}:{self.groupbot_opencode_serve_port}"

    @property
    def allowed_group_ids(self) -> list[str]:
        return [g.strip() for g in self.line_allowed_groups.split(",") if g.strip()]

    @property
    def allowed_admin_user_ids(self) -> list[str]:
        return [u.strip() for u in self.line_allowed_users.split(",") if u.strip()]

    @property
    def model_chain(self) -> list[str]:
        return [m.strip() for m in self.groupbot_model_chain.split(",") if m.strip()]

    @property
    def admin_model_chain(self) -> list[str]:
        return [m.strip() for m in self.groupbot_admin_model_chain.split(",") if m.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
