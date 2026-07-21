"""Application configuration, loaded from environment variables / .env file."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Claude"
    app_env: str = "development"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    # --- LINE Messaging API ---
    line_channel_secret: str = ""
    line_channel_access_token: str = ""

    # --- Access control ---
    # Comma-separated LINE userIds allowed to talk to this bot.
    # Empty = first user to DM the OA is captured as admin (stored in allowlist file).
    allowed_line_user_ids: str = ""
    allowlist_file: str = "config/allowlist.json"

    # --- Desktop agent (LINE controls this computer, driven by the local
    # headless Claude Code CLI: `claude -p`) ---
    # Tool calls (Read/Glob/Grep/LS) targeting paths outside this root pause for
    # LINE confirmation; Bash/Write/Edit/MultiEdit always pause regardless of path.
    #
    # 2026-07-22: moved from G:\我的雲端硬碟\claude\LINE (a Google-Drive mount)
    # to a local C: path, same rationale as the sibling opencode group-bot's
    # groupbot_admin_cwd move: the `claude -p` subprocess runs with cwd here
    # and reads/writes files here, so keeping it on the cloud-drive mount
    # means a transient G: stall (observed causing multi-minute hangs) can
    # block the agent's file operations. The old root held only a
    # line_uploads/ folder of past LINE image uploads (copied over,
    # checksum-verified); nothing else lived there. The G: copy is kept as
    # a backup, not deleted.
    desktop_root: str = r"C:\Users\user\claude-line-desktop-root"
    progress_interval_seconds: int = 300
    confirm_timeout_seconds: int = 600
    # Path/command used to invoke the Claude Code CLI. Override with a full
    # path if `claude` isn't on PATH for the process running this service.
    claude_cli_path: str = "claude"
    # Overall wall-clock timeout for one `claude -p` invocation (one LINE turn).
    claude_timeout_seconds: int = 1800
    # Base URL the PreToolUse hook script (scripts/claude_confirm_hook.py) uses
    # to call back into this process for dangerous-action confirmation. Empty
    # = derived from `port` (see `internal_base_url`).
    internal_base_url_override: str = ""

    @property
    def internal_base_url(self) -> str:
        return self.internal_base_url_override or f"http://127.0.0.1:{self.port}"

    @property
    def env_allowed_user_ids(self) -> list[str]:
        return [u.strip() for u in self.allowed_line_user_ids.split(",") if u.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
