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

    app_name: str = "HLAF"
    app_env: str = "development"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    # --- LINE Messaging API ---
    line_channel_secret: str = ""
    line_channel_access_token: str = ""

    # --- Hermes agent brain (OpenCode Zen, OpenAI-compatible) ---
    opencode_zen_api_key: str = ""
    opencode_zen_base_url: str = "https://opencode.ai/zen/v1"
    # Tried in order; falls back to the next model when one fails.
    opencode_zen_models: str = "big-pickle,deepseek-v4-flash-free,mimo-v2.5-free"

    # --- Access control ---
    # Comma-separated LINE userIds allowed to talk to Hermes.
    # Empty = first user to DM the OA is captured as admin (stored in allowlist file).
    allowed_line_user_ids: str = ""
    allowlist_file: str = "config/allowlist.json"

    @property
    def zen_model_list(self) -> list[str]:
        return [m.strip() for m in self.opencode_zen_models.split(",") if m.strip()]

    @property
    def env_allowed_user_ids(self) -> list[str]:
        return [u.strip() for u in self.allowed_line_user_ids.split(",") if u.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
