from sqlalchemy import text

from hub.db.session import init_db, make_engine, make_session_factory


def test_engine_enables_wal_and_busy_timeout(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/t.db")
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 5000
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_session_factory_yields_working_session(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/t.db")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        assert session.execute(text("SELECT 1")).scalar() == 1
