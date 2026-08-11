import pytest

from hub.config import Settings
from hub.db.session import init_db, make_engine, make_session_factory


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        database_url=f"sqlite:///{tmp_path}/test.db",
        session_secret="test-secret",
        admin_username=None,
        admin_initial_password=None,
        llm_provider_name="测试服务商（M1 骨架）",
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
