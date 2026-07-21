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
    # `opencode auth login -p opencode`. Order is a deliberate user choice
    # (big-pickle primary, deepseek-v4-flash-free second) — don't reorder
    # to "fix" slowness; use groupbot_primary_model_timeout_seconds below
    # instead, which cuts a stuck primary loose fast without abandoning it
    # as the preferred model.
    groupbot_model_chain: str = (
        "opencode/big-pickle,"
        "opencode/deepseek-v4-flash-free,"
        "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free,"
        "nvidia/deepseek-ai/deepseek-v4-flash,"
        "nvidia/z-ai/glm-5.2"
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
    # 2026-07-22: reliable-model-first order (same rationale as the group
    # chain's GROUPBOT_MODEL_CHAIN in .env). The opencode-zen free rungs
    # (deepseek-v4-flash-free, big-pickle) went down for hours overnight and
    # produced zero successes — every call timed out for 45-180s while
    # holding an opencode-serve worker, which saturated the worker pool and
    # took the whole bot down. Putting the models that DID succeed tonight
    # (nvidia deepseek-v4-flash, glm-5.2) first makes admin DMs actually
    # complete and stops the worker starvation; the free rungs stay as
    # fallback. Revert to free-first once opencode-zen recovers if cost
    # matters more than latency.
    groupbot_admin_model_chain: str = (
        "nvidia/deepseek-ai/deepseek-v4-flash,"
        "nvidia/z-ai/glm-5.2,"
        "opencode/deepseek-v4-flash-free,"
        "opencode/big-pickle,"
        "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"
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
    groupbot_admin_model_timeout_seconds: int = 180

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
