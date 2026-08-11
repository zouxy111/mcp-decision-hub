"""Runtime configuration loaded from environment variables (.env supported)."""

import os
from dataclasses import dataclass

from dotenv import find_dotenv, load_dotenv


@dataclass(frozen=True)
class Settings:
    database_url: str
    session_secret: str
    admin_username: str | None
    admin_initial_password: str | None
    invite_ttl_seconds: int = 7 * 24 * 3600
    task_timeout_seconds: int = 72 * 3600
    max_rounds: int = 10
    llm_provider_name: str = "DeepSeek"
    content_item_limit: int = 16 * 1024
    content_total_limit: int = 64 * 1024
    notes_limit: int = 8 * 1024
    request_body_limit: int = 96 * 1024
    deepseek_api_key: str | None = None
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"
    llm_request_timeout_seconds: int = 120


def load_settings() -> Settings:
    load_dotenv(find_dotenv(usecwd=True))  # .env 作为补充来源；不覆盖已存在的环境变量
    return Settings(
        database_url=os.environ.get("DATABASE_URL", "sqlite:///./hub.db"),
        session_secret=os.environ.get("SESSION_SECRET", "dev-secret-change-me"),
        admin_username=os.environ.get("ADMIN_USERNAME") or None,
        admin_initial_password=os.environ.get("ADMIN_INITIAL_PASSWORD") or None,
        llm_provider_name=os.environ.get("LLM_PROVIDER_NAME", "DeepSeek"),
        deepseek_api_key=os.environ.get("DEEPSEEK_API_KEY") or None,
        llm_base_url=os.environ.get("LLM_BASE_URL", "https://api.deepseek.com"),
        llm_model=os.environ.get("LLM_MODEL", "deepseek-chat"),
        llm_request_timeout_seconds=int(
            os.environ.get("LLM_REQUEST_TIMEOUT_SECONDS", "120")
        ),
    )
