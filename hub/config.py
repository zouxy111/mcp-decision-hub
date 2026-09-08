"""Runtime configuration loaded from environment variables (.env supported)."""

import os
from dataclasses import dataclass, fields

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
    timeout_scan_interval_seconds: int = 60
    rate_limit_token_per_minute: int = 60
    rate_limit_submit_per_minute: int = 10
    rate_limit_account_per_minute: int = 120
    rate_limit_login_username_per_minute: int = 5
    rate_limit_login_ip_per_minute: int = 20
    poll_seconds_idle: int = 300
    poll_seconds_active: int = 30


# Single source of truth for int defaults: the dataclass field defaults.
_INT_DEFAULTS = {f.name: f.default for f in fields(Settings)}


def _int_env(name: str, field: str) -> int:
    """int 转换读取 env；缺省回退 dataclass 默认值（设计决策 9）。"""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return _INT_DEFAULTS[field]
    return int(raw)


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
        invite_ttl_seconds=_int_env("INVITE_TTL_SECONDS", "invite_ttl_seconds"),
        task_timeout_seconds=_int_env("TASK_TIMEOUT_SECONDS", "task_timeout_seconds"),
        max_rounds=_int_env("MAX_ROUNDS", "max_rounds"),
        content_item_limit=_int_env("CONTENT_ITEM_LIMIT", "content_item_limit"),
        content_total_limit=_int_env("CONTENT_TOTAL_LIMIT", "content_total_limit"),
        notes_limit=_int_env("NOTES_LIMIT", "notes_limit"),
        request_body_limit=_int_env("REQUEST_BODY_LIMIT", "request_body_limit"),
        timeout_scan_interval_seconds=_int_env(
            "TIMEOUT_SCAN_INTERVAL_SECONDS", "timeout_scan_interval_seconds"),
        rate_limit_token_per_minute=_int_env(
            "RATE_LIMIT_TOKEN_PER_MINUTE", "rate_limit_token_per_minute"),
        rate_limit_submit_per_minute=_int_env(
            "RATE_LIMIT_SUBMIT_PER_MINUTE", "rate_limit_submit_per_minute"),
        rate_limit_account_per_minute=_int_env(
            "RATE_LIMIT_ACCOUNT_PER_MINUTE", "rate_limit_account_per_minute"),
        rate_limit_login_username_per_minute=_int_env(
            "RATE_LIMIT_LOGIN_USERNAME_PER_MINUTE",
            "rate_limit_login_username_per_minute"),
        rate_limit_login_ip_per_minute=_int_env(
            "RATE_LIMIT_LOGIN_IP_PER_MINUTE", "rate_limit_login_ip_per_minute"),
        poll_seconds_idle=_int_env("POLL_SECONDS_IDLE", "poll_seconds_idle"),
        poll_seconds_active=_int_env("POLL_SECONDS_ACTIVE", "poll_seconds_active"),
    )
