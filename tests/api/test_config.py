from hub.config import Settings, load_settings


def test_settings_llm_defaults():
    settings = Settings(
        database_url="sqlite:///x.db",
        session_secret="s",
        admin_username=None,
        admin_initial_password=None,
    )
    assert settings.deepseek_api_key is None
    assert settings.llm_base_url == "https://api.deepseek.com"
    assert settings.llm_model == "deepseek-flash"
    assert settings.llm_request_timeout_seconds == 120
    assert settings.llm_provider_name == "DeepSeek"


def test_load_settings_reads_llm_env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-123")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.test")
    monkeypatch.setenv("LLM_MODEL", "deepseek-reasoner")
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT_SECONDS", "60")
    settings = load_settings()
    assert settings.deepseek_api_key == "sk-test-123"
    assert settings.llm_base_url == "https://example.test"
    assert settings.llm_model == "deepseek-reasoner"
    assert settings.llm_request_timeout_seconds == 60


def test_load_settings_missing_key_is_none(monkeypatch, tmp_path):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    # chdir 到空目录，避免项目根 .env（任务 18 冒烟会创建）干扰本用例
    monkeypatch.chdir(tmp_path)
    assert load_settings().deepseek_api_key is None


def test_load_dotenv_reads_project_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("DEEPSEEK_API_KEY=sk-from-dotenv\n", encoding="utf-8")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    assert load_settings().deepseek_api_key == "sk-from-dotenv"


def test_load_dotenv_does_not_override_existing_env(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("DEEPSEEK_API_KEY=sk-from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-environ")
    monkeypatch.chdir(tmp_path)
    assert load_settings().deepseek_api_key == "sk-from-environ"


def test_settings_m4_fields_defaults():
    settings = Settings(
        database_url="sqlite:///x.db",
        session_secret="s",
        admin_username=None,
        admin_initial_password=None,
    )
    assert settings.timeout_scan_interval_seconds == 60
    assert settings.rate_limit_token_per_minute == 60
    assert settings.rate_limit_submit_per_minute == 10
    assert settings.rate_limit_account_per_minute == 120
    assert settings.poll_seconds_idle == 300
    assert settings.poll_seconds_active == 30


def test_load_settings_reads_m4_and_legacy_env(monkeypatch, tmp_path):
    """M4 新增字段与 M2/M3 遗留字段的 env 读取（遗留字段此前从未被 env 覆盖）。"""
    monkeypatch.chdir(tmp_path)  # 避免项目根 .env 干扰
    monkeypatch.setenv("TASK_TIMEOUT_SECONDS", "3600")
    monkeypatch.setenv("MAX_ROUNDS", "5")
    monkeypatch.setenv("TIMEOUT_SCAN_INTERVAL_SECONDS", "15")
    monkeypatch.setenv("RATE_LIMIT_TOKEN_PER_MINUTE", "40")
    monkeypatch.setenv("RATE_LIMIT_SUBMIT_PER_MINUTE", "4")
    monkeypatch.setenv("RATE_LIMIT_ACCOUNT_PER_MINUTE", "90")
    monkeypatch.setenv("POLL_SECONDS_IDLE", "600")
    monkeypatch.setenv("POLL_SECONDS_ACTIVE", "10")
    monkeypatch.setenv("CONTENT_ITEM_LIMIT", "1024")
    monkeypatch.setenv("CONTENT_TOTAL_LIMIT", "2048")
    monkeypatch.setenv("NOTES_LIMIT", "512")
    monkeypatch.setenv("REQUEST_BODY_LIMIT", "4096")
    monkeypatch.setenv("INVITE_TTL_SECONDS", "86400")
    settings = load_settings()
    assert settings.task_timeout_seconds == 3600
    assert settings.max_rounds == 5
    assert settings.timeout_scan_interval_seconds == 15
    assert settings.rate_limit_token_per_minute == 40
    assert settings.rate_limit_submit_per_minute == 4
    assert settings.rate_limit_account_per_minute == 90
    assert settings.poll_seconds_idle == 600
    assert settings.poll_seconds_active == 10
    assert settings.content_item_limit == 1024
    assert settings.content_total_limit == 2048
    assert settings.notes_limit == 512
    assert settings.request_body_limit == 4096
    assert settings.invite_ttl_seconds == 86400


def test_load_settings_unset_m4_env_falls_back_to_defaults(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for var in ("TASK_TIMEOUT_SECONDS", "MAX_ROUNDS",
                "TIMEOUT_SCAN_INTERVAL_SECONDS", "RATE_LIMIT_TOKEN_PER_MINUTE",
                "RATE_LIMIT_SUBMIT_PER_MINUTE", "RATE_LIMIT_ACCOUNT_PER_MINUTE",
                "POLL_SECONDS_IDLE", "POLL_SECONDS_ACTIVE",
                "CONTENT_ITEM_LIMIT", "CONTENT_TOTAL_LIMIT", "NOTES_LIMIT",
                "REQUEST_BODY_LIMIT", "INVITE_TTL_SECONDS"):
        monkeypatch.delenv(var, raising=False)
    settings = load_settings()
    assert settings.task_timeout_seconds == 72 * 3600
    assert settings.max_rounds == 10
    assert settings.timeout_scan_interval_seconds == 60
    assert settings.rate_limit_token_per_minute == 60
    assert settings.rate_limit_submit_per_minute == 10
    assert settings.rate_limit_account_per_minute == 120
    assert settings.poll_seconds_idle == 300
    assert settings.poll_seconds_active == 30
