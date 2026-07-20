"""Allowlist of LINE userIds permitted to talk to this bot.

Sources, in priority order:
1. `ALLOWED_LINE_USER_IDS` env var (comma-separated) — if set, it is the whole list.
2. Allowlist file (`config/allowlist.json`) — auto-populated: when both sources are
   empty, the first user to DM the official account is captured as admin.
"""

import json
from pathlib import Path

from app.config import get_settings
from app.logger import logger


def _file_path() -> Path:
    return Path(get_settings().allowlist_file)


def _load_file_ids() -> list[str]:
    path = _file_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [str(u) for u in data.get("allowed_user_ids", [])]
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(f"Failed to read allowlist file {path}: {exc}")
        return []


def get_allowed_ids() -> list[str]:
    env_ids = get_settings().env_allowed_user_ids
    if env_ids:
        return env_ids
    return _load_file_ids()


def is_allowed(user_id: str) -> bool:
    return user_id in get_allowed_ids()


def capture_first_admin(user_id: str) -> bool:
    """If no one is allowlisted yet, register `user_id` as the admin.

    Returns True if the user was captured as the first admin.
    """
    if get_allowed_ids():
        return False
    path = _file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"allowed_user_ids": [user_id]}, indent=2),
        encoding="utf-8",
    )
    logger.info(f"Captured first admin userId into {path}")
    return True
