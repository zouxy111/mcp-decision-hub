"""Runtime configuration loaded from environment variables."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    session_secret: str
    admin_username: str | None
    admin_initial_password: str | None
    invite_ttl_seconds: int = 7 * 24 * 3600
    task_timeout_seconds: int = 72 * 3600
    max_rounds: int = 10
    llm_provider_name: str = "未接入 LLM（M1 骨架）"
    content_item_limit: int = 16 * 1024
    content_total_limit: int = 64 * 1024
    notes_limit: int = 8 * 1024
    request_body_limit: int = 96 * 1024


def load_settings() -> Settings:
    return Settings(
        database_url=os.environ.get("DATABASE_URL", "sqlite:///./hub.db"),
        session_secret=os.environ.get("SESSION_SECRET", "dev-secret-change-me"),
        admin_username=os.environ.get("ADMIN_USERNAME") or None,
        admin_initial_password=os.environ.get("ADMIN_INITIAL_PASSWORD") or None,
        llm_provider_name=os.environ.get("LLM_PROVIDER_NAME", "未接入 LLM（M1 骨架）"),
    )
