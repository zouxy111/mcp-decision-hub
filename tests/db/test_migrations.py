"""迁移机制测试（垂直切片，逐片红绿）。

真实现是 SQLite，属于系统边界，因此不 mock —— 全部用 tmp_path 建真库。
从不触碰仓库根的 hub.db。
"""

import os
import sqlite3

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

import hub.db.migrations as migrations_module
import scripts.backup_db as backup_script
from hub.db.migrations import (
    MIGRATIONS,
    Migration,
    MigrationError,
    current_version,
    run_migrations,
)
from hub.db.models import Matter, Stance
from hub.db.session import init_db, make_engine, make_session_factory
from tests.conftest import make_user


def _connect(db_path) -> sqlite3.Connection:
    return sqlite3.connect(str(db_path))


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _stance_kwargs(matter, user, *, stance="conditional") -> dict:
    return {
        "matter_id": matter.id,
        "round_number": 1,
        "user_id": user.id,
        "stance": stance,
        "confidence": 0.5,
        "position_summary": "p",
        "rationale_summary": "r",
        "acting_as": "human",
        "urgency": "normal",
        "visibility": "participants",
        "content_hash": "a" * 64,
    }


def test_空库跑迁移会建版本表并记录最新版本(tmp_path):
    db = tmp_path / "fresh.db"
    engine = make_engine(f"sqlite:///{db}")

    report = run_migrations(engine, backup=False)

    assert report.applied == tuple(m.version for m in MIGRATIONS)
    assert report.to_version == MIGRATIONS[-1].version

    conn = _connect(db)
    try:
        assert "schema_migrations" in _table_names(conn)
        assert current_version(conn) == MIGRATIONS[-1].version
    finally:
        conn.close()


def test_空库经init_db迁到最新且stances的CHECK约束生效(tmp_path):
    db = tmp_path / "wired.db"
    engine = make_engine(f"sqlite:///{db}")

    init_db(engine)

    conn = _connect(db)
    try:
        assert current_version(conn) == MIGRATIONS[-1].version
    finally:
        conn.close()

    factory = make_session_factory(engine)
    with factory() as session:
        alice = make_user(session, "alice")
        matter = Matter(initiator_id=alice.id, title="T", goal="G", background="B")
        session.add(matter)
        session.flush()
        session.add(Stance(**_stance_kwargs(matter, alice, stance="banana")))
        with pytest.raises(IntegrityError):
            session.flush()


def test_重复跑迁移是幂等的且不重复执行(tmp_path, monkeypatch):
    db = tmp_path / "idem.db"
    engine = make_engine(f"sqlite:///{db}")

    calls: list[int] = []
    spy = Migration(99, "spy", lambda conn: calls.append(99))
    monkeypatch.setattr(
        migrations_module, "MIGRATIONS", (*migrations_module.MIGRATIONS, spy)
    )

    first = run_migrations(engine, backup=False)
    assert first.applied == (1, 2, 99)
    assert calls == [99]

    second = run_migrations(engine, backup=False)
    assert second.applied == ()
    assert second.from_version == second.to_version == 99
    assert calls == [99]


def test_迁移失败后无半成品且版本号不前进(tmp_path, monkeypatch):
    """守的是**失败后的结果**，不是某条具体实现路径。

    这个结果由两个机制共同保证：
      1. runner 里「异常 → 若 conn.in_transaction 则显式 ROLLBACK」；
      2. 连接池还池时 SQLAlchemy 默认 reset_on_return='rollback'。

    实测：把 1 去掉后本条**仍然绿**——因为 2 会兜底。也就是说显式 ROLLBACK 是
    **纵深防御**，在本架构下无法被这条测试单独观测到（连接归还池之前，外部看不到
    那个未提交事务）。因此这里只承诺「结果正确」。
    """
    db = tmp_path / "boom.db"
    engine = make_engine(f"sqlite:///{db}")

    def explodes(conn):
        conn.execute("BEGIN")
        conn.execute("CREATE TABLE half_done (x INTEGER)")  # 半成品
        raise RuntimeError("步骤内部炸了")

    broken = Migration(5, "broken", explodes)
    monkeypatch.setattr(
        migrations_module, "MIGRATIONS", (*migrations_module.MIGRATIONS, broken)
    )

    with pytest.raises(MigrationError) as excinfo:
        run_migrations(engine, backup=False)

    assert excinfo.value.version == 5
    assert excinfo.value.name == "broken"
    assert isinstance(excinfo.value.__cause__, RuntimeError)

    conn = _connect(db)
    try:
        assert current_version(conn) == 2  # 只前进到最后一次成功的版本
        assert "half_done" not in _table_names(conn)
    finally:
        conn.close()


_LANGGRAPH_TABLE_DDL = (
    "CREATE TABLE checkpoints (thread_id TEXT PRIMARY KEY, payload BLOB)",
    "CREATE TABLE writes (thread_id TEXT, idx INTEGER, value TEXT, "
    "PRIMARY KEY (thread_id, idx))",
)


def _langgraph_snapshot(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        name: conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()[0]
        for name in ("checkpoints", "writes")
    }


def test_迁移不触碰LangGraph自管的表(tmp_path):
    db = tmp_path / "langgraph.db"
    engine = _make_legacy_db(db)  # stances 无 CHECK，v2 真的会跑表重建

    conn = _connect(db)
    try:
        for statement in _LANGGRAPH_TABLE_DDL:
            conn.execute(statement)
        conn.execute("INSERT INTO checkpoints VALUES ('t1', x'00')")
        conn.execute("INSERT INTO writes VALUES ('t1', 0, 'w')")
        conn.commit()
        before = _langgraph_snapshot(conn)
    finally:
        conn.close()

    report = run_migrations(engine, backup=False)
    assert report.applied == (2,)  # 确认 v2 真的执行了

    conn = _connect(db)
    try:
        assert _langgraph_snapshot(conn) == before
        assert conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0] == 1
    finally:
        conn.close()


class _ForeignKeysTracer:
    """只读包装：每条非 PRAGMA 语句执行后，记下真连接上 PRAGMA foreign_keys 的取值。

    底层是真 sqlite3 连接（不 mock SQLite），只是把「重建期间外键处于什么状态」观测下来。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.trace: list[int] = []

    def execute(self, sql, *args):
        result = self._conn.execute(sql, *args)
        if "foreign_key" not in sql.lower():
            self.trace.append(
                self._conn.execute("PRAGMA foreign_keys").fetchone()[0]
            )
        return result


def test_表重建期间外键确实被关掉且事后恢复为开(tmp_path):
    db = tmp_path / "fk.db"
    engine = _make_legacy_db(db)
    rebuild = next(m for m in MIGRATIONS if m.version == 2)

    raw = engine.raw_connection()
    try:
        conn = raw.driver_connection
        conn.isolation_level = None
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

        tracer = _ForeignKeysTracer(conn)
        rebuild.upgrade(tracer)  # 内部临时关外键做表重建

        # (a) 重建期间真的被关过：删掉 OFF+恢复整段 → trace 全是 1 → 红
        assert 0 in tracer.trace
        # (b) 事后恢复为开：保留 OFF 只删恢复 → 这里读到 0 → 红
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert "ck_stances_" in _stances_ddl(conn)
    finally:
        raw.close()


def test_走完迁移后引擎连接的外键开关仍为开(tmp_path):
    db = tmp_path / "fk_e2e.db"
    engine = _make_legacy_db(db)

    run_migrations(engine, backup=False)

    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_升级存量库前先生成迁移前状态的备份(tmp_path):
    db = tmp_path / "backed.db"
    engine = _make_legacy_db(db)

    report = run_migrations(engine)  # backup 默认 True

    assert report.applied == (2,)
    backups = sorted((tmp_path / "backups").glob("hub-backup-*.db"))
    assert len(backups) == 1

    # 备份必须是「迁移前」的状态 —— 里面的 stances 还没有 CHECK
    conn = _connect(backups[0])
    try:
        assert "ck_stances_" not in _stances_ddl(conn)
    finally:
        conn.close()


def _backup_snapshot(backups_dir) -> set[tuple[str, int, int]]:
    """备份目录的确定性指纹：任何一次真实写入都会改变它。

    只看「文件数」是测不出来的：备份名是秒级时间戳，同一秒内重写会覆盖同名文件，
    文件数不变。所以要比 (名字, 大小, mtime_ns) 三元组。
    """
    if not backups_dir.exists():
        return set()
    return {
        (p.name, p.stat().st_size, p.stat().st_mtime_ns)
        for p in backups_dir.iterdir()
    }


def test_没有待迁版本时不产生备份(tmp_path):
    """幂等路径零副作用：无待迁步骤就绝不写任何东西。

    把已存在备份的 mtime 钉到过去，这样「又写了一次」无论是否落入同一秒都必然可见
    （新文件 → 多一个条目；覆盖同名文件 → mtime_ns 必然变化），不受时钟粒度影响。
    """
    db = tmp_path / "noop.db"
    engine = make_engine(f"sqlite:///{db}")
    backups_dir = tmp_path / "backups"

    run_migrations(engine)  # 首次：有待迁版本 → 备份
    assert len(_backup_snapshot(backups_dir)) == 1

    pinned = 946684800_000000000  # 2000-01-01T00:00:00Z
    for path in backups_dir.iterdir():
        os.utime(path, ns=(pinned, pinned))
    before = _backup_snapshot(backups_dir)
    assert {mtime for _, _, mtime in before} == {pinned}

    report = run_migrations(engine)  # 第二次：无待迁版本

    assert report.applied == ()
    assert _backup_snapshot(backups_dir) == before


def test_备份失败则迁移整体不执行(tmp_path, monkeypatch):
    db = tmp_path / "no_backup.db"
    engine = _make_legacy_db(db)

    def failing_backup(src_url, out_dir):
        raise RuntimeError("备份工具挂了")

    monkeypatch.setattr(backup_script, "backup_database", failing_backup)

    with pytest.raises(RuntimeError, match="备份工具挂了"):
        run_migrations(engine)

    conn = _connect(db)
    try:
        assert "ck_stances_" not in _stances_ddl(conn)  # 迁移没跑
        assert current_version(conn) == 0  # 版本号也没写
    finally:
        conn.close()


# 存量库的 stances：与真实旧 hub.db 一致，**没有那 4 条 CHECK**。
_LEGACY_STANCES_DDL = """
CREATE TABLE stances (
    stance_id VARCHAR(48) NOT NULL,
    matter_id VARCHAR(48) NOT NULL,
    round_number INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    stance VARCHAR(32) NOT NULL,
    confidence FLOAT NOT NULL,
    position_summary TEXT NOT NULL,
    rationale_summary TEXT NOT NULL,
    non_negotiables JSON NOT NULL,
    conditions JSON NOT NULL,
    open_questions JSON NOT NULL,
    depends_on JSON NOT NULL,
    questions_for JSON NOT NULL,
    disagreement_kind VARCHAR(32),
    supersedes VARCHAR(48),
    acting_as VARCHAR(32) NOT NULL,
    authority VARCHAR(255),
    ttl_seconds INTEGER,
    urgency VARCHAR(16) NOT NULL,
    visibility VARCHAR(16) NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (stance_id),
    UNIQUE (matter_id, round_number, user_id),
    FOREIGN KEY(matter_id) REFERENCES matters (id),
    FOREIGN KEY(user_id) REFERENCES users (id),
    FOREIGN KEY(supersedes) REFERENCES stances (stance_id)
)
"""


def _make_legacy_db(db_path):
    """建一个「无版本表 + stances 无 CHECK」的存量库，返回其 engine。"""
    import hub.db.models  # noqa: F401
    from hub.db.base import Base

    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)

    conn = _connect(db_path)
    try:
        # 裸 SQL 把 stances 换成旧版（无 CHECK）
        for index in ("ix_stances_matter_id", "ix_stances_user_id"):
            conn.execute(f"DROP INDEX IF EXISTS {index}")
        conn.execute("DROP TABLE stances")
        conn.execute(_LEGACY_STANCES_DDL)
        conn.execute("CREATE INDEX ix_stances_matter_id ON stances (matter_id)")
        conn.execute("CREATE INDEX ix_stances_user_id ON stances (user_id)")
        conn.commit()

        # 前置条件：确实是「无版本表 + 无 CHECK」的旧库
        assert "schema_migrations" not in _table_names(conn)
        assert "ck_stances_" not in _stances_ddl(conn)
    finally:
        conn.close()
    return engine


def _stances_ddl(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='stances'"
    ).fetchone()
    return row[0] if row else ""


def test_存量库升级后stances的CHECK约束生效且数据保留(tmp_path):
    db = tmp_path / "legacy.db"
    engine = _make_legacy_db(db)

    # 旧库里先放一行合法数据，迁移必须把它带过去
    with make_session_factory(engine)() as session:
        alice = make_user(session, "alice")
        matter = Matter(initiator_id=alice.id, title="T", goal="G", background="B")
        session.add(matter)
        session.flush()
        session.add(Stance(**_stance_kwargs(matter, alice)))
        session.commit()
        matter_id = matter.id

    conn = _connect(db)
    try:
        stance_id = conn.execute("SELECT stance_id FROM stances").fetchone()[0]
    finally:
        conn.close()

    report = run_migrations(engine, backup=False)

    assert report.from_version == 1  # 无版本表 + 已有业务表 → 认领为 baseline v1
    assert report.applied == (2,)

    conn = _connect(db)
    try:
        assert "ck_stances_" in _stances_ddl(conn)
        assert current_version(conn) == 2
        rows = conn.execute("SELECT stance_id, matter_id FROM stances").fetchall()
        assert rows == [(stance_id, matter_id)]
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='stances'"
            )
        }
        assert {"ix_stances_matter_id", "ix_stances_user_id"} <= indexes
    finally:
        conn.close()

    with make_session_factory(engine)() as session:
        bob = make_user(session, "bob")
        other = Matter(initiator_id=bob.id, title="T2", goal="G", background="B")
        session.add(other)
        session.flush()
        session.add(Stance(**_stance_kwargs(other, bob, stance="banana")))
        with pytest.raises(IntegrityError):
            session.flush()

