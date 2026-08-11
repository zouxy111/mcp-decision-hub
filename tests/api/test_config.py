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
    assert settings.llm_model == "deepseek-chat"
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
