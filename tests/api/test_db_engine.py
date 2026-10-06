"""引擎 PRAGMA：WAL + 写入提速的那几项（2026-10-05 甲方要求「写入提速」）。

这些 PRAGMA 是写路径快慢的主要开关（SQLite 的慢几乎都在 fsync 上），
所以钉在测试里：改回 ``synchronous=FULL`` 或去掉连接参数都会在这里报错。
"""

from sqlalchemy import text

from hub.db.session import init_db, make_engine, make_session_factory


def test_engine_enables_wal_and_busy_timeout(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/t.db")
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 10000
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_engine_uses_write_friendly_pragmas(tmp_path):
    """写入提速三项：synchronous=NORMAL、内存临时表、正数页缓存。"""
    engine = make_engine(f"sqlite:///{tmp_path}/t.db")
    with engine.connect() as conn:
        synchronous = conn.execute(text("PRAGMA synchronous")).scalar()
        temp_store = conn.execute(text("PRAGMA temp_store")).scalar()
        cache_size = conn.execute(text("PRAGMA cache_size")).scalar()
    # 0=OFF 1=NORMAL 2=FULL 3=EXTRA
    assert synchronous == 1, "写入提速要求 synchronous=NORMAL（WAL 下不每条 commit fsync）"
    assert temp_store == 2, "排序/临时表放内存（2=MEMORY）"
    assert cache_size == -16000, "页缓存 16MB（负数=KB）"


def test_session_factory_yields_working_session(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/t.db")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        assert session.execute(text("SELECT 1")).scalar() == 1


def test_app_compresses_large_json(settings):
    """大响应走 gzip：公网 RTT ~180ms 的部署上，几百 KB 的留言 JSON 靠这个省时间。"""
    from fastapi.testclient import TestClient

    from hub.main import create_app

    app = create_app(settings, llm=None)
    big = "x" * 4000

    @app.get("/__big")
    def _big():
        return {"data": big}

    with TestClient(app) as client:
        resp = client.get("/__big", headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200
    assert resp.headers.get("content-encoding") == "gzip"
    assert resp.json()["data"] == big
