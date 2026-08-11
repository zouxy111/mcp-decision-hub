import pytest
from fastapi.testclient import TestClient

from hub.config import Settings
from hub.db.session import init_db, make_engine, make_session_factory


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        database_url=f"sqlite:///{tmp_path}/test.db",
        session_secret="test-secret",
        admin_username=None,
        admin_initial_password=None,
        llm_provider_name="DeepSeek（测试）",
    )


@pytest.fixture()
def session_factory(settings):
    engine = make_engine(settings.database_url)
    init_db(engine)
    return make_session_factory(engine)


@pytest.fixture()
def db_session(session_factory):
    with session_factory() as session:
        yield session


@pytest.fixture()
def client(settings):
    from hub.main import create_app

    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def make_user(db_session, username, password="pw-12345", *, is_admin=False,
              is_active=True, must_change_password=False, email=None):
    """Test user factory. Uses argon2 directly to stay independent of service modules."""
    from argon2 import PasswordHasher

    from hub.db.models import User

    user = User(
        username=username,
        email=email or f"{username}@example.com",
        password_hash=PasswordHasher().hash(password),
        is_admin=is_admin,
        is_active=is_active,
        must_change_password=must_change_password,
    )
    db_session.add(user)
    db_session.flush()
    return user


class FakeLLM:
    """Scripted LLM double. Each script item is either a dict returned
    verbatim or an exception instance to raise. Records every call."""

    def __init__(self, script=()):
        self._script = list(script)
        self.calls: list[dict] = []

    def complete_json(self, system_prompt, user_prompt, *, schema_name):
        from hub.llm.client import LLMError

        self.calls.append(
            {"system_prompt": system_prompt, "user_prompt": user_prompt,
             "schema_name": schema_name}
        )
        if not self._script:
            raise LLMError("LLM_FAKE_EXHAUSTED", "FakeLLM 脚本已耗尽", retry_count=3)
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture()
def make_fake_llm():
    def _make(script=()):
        return FakeLLM(script)

    return _make
