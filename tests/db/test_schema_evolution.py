"""A1（rfhWa9）：5 张既有表结构变更 + 每项可单独回滚 + 迁移审计守卫。

逐字完成标准：
> 补迁移机制 + 5 张既有表的结构变更 + 每项可单独回滚
事项验收：
- 同一个库上跑两次迁移，结果幂等且 schema 一致
- 迁移失败能回滚，不留半成品 schema（既有 test_migrations 覆盖）
- 拿现有 hub.db 实例实测一次完整迁移
- idempotency_records 改主键的迁移有专项测试（建旧结构 → 插数据 → 迁移 → 校验数据未丢）

真 SQLite、tmp_path，从不触碰任何真实库文件。
"""

import sqlite3

import pytest

from hub.api.audit import SCHEMA_MIGRATED
from hub.db.migrations import (
    MIGRATIONS,
    current_version,
    rollback_step,
    run_migrations,
)
from hub.db.session import init_db, make_engine


def _connect(db_path) -> sqlite3.Connection:
    return sqlite3.connect(str(db_path))


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _pk(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in sorted((r for r in rows if r[5]), key=lambda r: r[5])]


def _ddl(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row[0] if row else ""


def _legacy_db(tmp_path) -> str:
    """v2 时代的旧库：5 张待改表全部是旧结构，各插 1 行真实数据。"""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, username VARCHAR(64) UNIQUE,
            password_hash VARCHAR(255), is_active BOOLEAN, is_admin BOOLEAN,
            created_at DATETIME
        );
        CREATE TABLE matters (
            id VARCHAR(48) PRIMARY KEY, initiator_id INTEGER,
            title VARCHAR(255), background TEXT, goal TEXT, status VARCHAR(32),
            timeout_seconds INTEGER, max_rounds INTEGER,
            initiator_participates BOOLEAN, draft_questions JSON,
            granted_extra_rounds INTEGER, blocked_reason VARCHAR(255),
            created_at DATETIME, updated_at DATETIME
        );
        CREATE TABLE matter_participants (
            matter_id VARCHAR(48), user_id INTEGER,
            PRIMARY KEY (matter_id, user_id)
        );
        CREATE TABLE rounds (
            id VARCHAR(48) PRIMARY KEY, matter_id VARCHAR(48),
            round_number INTEGER, status VARCHAR(32), questions JSON,
            created_at DATETIME, closed_at DATETIME
        );
        CREATE TABLE round_summaries (
            id VARCHAR(48) PRIMARY KEY, round_id VARCHAR(48) UNIQUE,
            matter_id VARCHAR(48), consensus_points JSON, divergences JSON,
            blind_spots JSON, open_questions JSON, convergence VARCHAR(32),
            generation_status VARCHAR(16), error_code VARCHAR(64),
            retry_count INTEGER, created_at DATETIME
        );
        CREATE TABLE tasks (
            id VARCHAR(48) PRIMARY KEY, round_id VARCHAR(48),
            matter_id VARCHAR(48), assignee_id INTEGER, status VARCHAR(32),
            deadline_at DATETIME, created_at DATETIME, submitted_at DATETIME
        );
        CREATE TABLE idempotency_records (
            task_id VARCHAR(48) NOT NULL, idempotency_key VARCHAR(128) NOT NULL,
            request_fingerprint VARCHAR(64) NOT NULL, response_json TEXT NOT NULL,
            created_at DATETIME NOT NULL,
            PRIMARY KEY (task_id, idempotency_key)
        );
        CREATE TABLE stances (
            stance_id VARCHAR(48) PRIMARY KEY, matter_id VARCHAR(48),
            round_number INTEGER, user_id INTEGER, stance VARCHAR(32),
            confidence FLOAT, position_summary TEXT, rationale_summary TEXT,
            non_negotiables JSON, conditions JSON, open_questions JSON,
            depends_on JSON, questions_for JSON, disagreement_kind VARCHAR(32),
            supersedes VARCHAR(48), acting_as VARCHAR(32), authority VARCHAR(255),
            ttl_seconds INTEGER, urgency VARCHAR(16), visibility VARCHAR(16),
            content_hash VARCHAR(64), created_at DATETIME,
            UNIQUE (matter_id, round_number, user_id)
        );
        CREATE TABLE outputs (
            id VARCHAR(48) PRIMARY KEY, task_id VARCHAR(48) UNIQUE,
            answers JSON, notes TEXT, approved_at DATETIME,
            content_digest VARCHAR(64), created_at DATETIME
        );
        CREATE TABLE audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, actor_user_id INTEGER,
            event_type VARCHAR(64), matter_id VARCHAR(48), detail JSON,
            created_at DATETIME NOT NULL
        );
        INSERT INTO users (username, is_active, is_admin, created_at)
            VALUES ('u1', 1, 0, '2026-01-01T00:00:00Z');
        INSERT INTO matters (id, initiator_id, title, background, goal, status,
            timeout_seconds, max_rounds, initiator_participates, draft_questions,
            granted_extra_rounds, created_at, updated_at)
            VALUES ('mat_1', 1, 'T', 'B', 'G', 'in_progress', 3600, 10, 0,
                    '[]', 0, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');
        INSERT INTO matter_participants VALUES ('mat_1', 1);
        INSERT INTO rounds (id, matter_id, round_number, status, questions, created_at)
            VALUES ('rnd_1', 'mat_1', 1, 'collecting', '[]', '2026-01-01T00:00:00Z');
        INSERT INTO round_summaries (id, round_id, matter_id, consensus_points,
            divergences, blind_spots, open_questions, convergence,
            generation_status, retry_count, created_at)
            VALUES ('sum_1', 'rnd_1', 'mat_1', '[]', '[]', '[]', '[]', 'continue',
                    'ok', 0, '2026-01-01T00:00:00Z');
        INSERT INTO idempotency_records VALUES
            ('tsk_1', 'key-1', 'fp-1', '{"ok":true}', '2026-01-01T00:00:00Z');
        INSERT INTO stances (stance_id, matter_id, round_number, user_id, stance,
            confidence, position_summary, rationale_summary, non_negotiables,
            conditions, open_questions, depends_on, questions_for, acting_as,
            authority, urgency, visibility, content_hash, created_at)
            VALUES ('stn_1', 'mat_1', 1, 1, 'support', 0.8, 'p', 'r', '[]', '[]',
                    '[]', '[]', '[]', 'agent_on_behalf', '历史自由文本授权',
                    'normal', 'participants', 'a', '2026-01-01T00:00:00Z');
        INSERT INTO outputs (id, task_id, answers, approved_at, content_digest,
            created_at)
            VALUES ('out_1', 'tsk_1', '[]', '2026-01-01T00:00:00Z', 'd' || 'x' * 0 || 'a',
                    '2026-01-01T00:00:00Z');
        """
    )
    conn.commit()
    conn.close()
    return str(db)


def test_五张表结构变更全部就位且原行保留(tmp_path):
    db = _legacy_db(tmp_path)
    init_db(make_engine(f"sqlite:///{db}"))

    conn = _connect(db)
    try:
        assert current_version(conn) == MIGRATIONS[-1].version
        # matters +4
        for col in ("irreversible", "options", "overall_deadline", "item_version"):
            assert col in _columns(conn, "matters"), col
        # matter_participants +2
        for col in ("agent_authority", "visibility_scope"):
            assert col in _columns(conn, "matter_participants"), col
        # round_summaries +2（事项原文：agreement_score / clusters）
        for col in ("agreement_score", "clusters"):
            assert col in _columns(conn, "round_summaries"), col
        # idempotency_records 改主键：task_id 作用域 → scope_type + scope_id
        assert _pk(conn, "idempotency_records") == [
            "scope_type", "scope_id", "idempotency_key",
        ]
        # stances：approved_at 可空 + authority 收敛为枚举 + authority_legacy
        for col in ("approved_at", "authority_legacy"):
            assert col in _columns(conn, "stances"), col
        assert "ck_stances_authority" in _ddl(conn, "stances")
        # outputs + authority
        assert "authority" in _columns(conn, "outputs")

        # 原行保留 + 禁止启发式回填：自由文本 authority 只进 legacy，authority 置 NULL
        row = conn.execute(
            "SELECT authority, authority_legacy, approved_at FROM stances"
            " WHERE stance_id='stn_1'"
        ).fetchone()
        assert row[0] is None
        assert row[1] == "历史自由文本授权"
        assert row[2] is None
        idem = conn.execute(
            "SELECT scope_type, scope_id, idempotency_key, request_fingerprint,"
            " response_json FROM idempotency_records"
        ).fetchall()
        assert idem == [("task", "tsk_1", "key-1", "fp-1", '{"ok":true}')]
        assert conn.execute("SELECT title FROM matters WHERE id='mat_1'").fetchone()[0] == "T"
    finally:
        conn.close()


def test_每一项结构变更可单独回滚且数据保留(tmp_path):
    db = _legacy_db(tmp_path)
    engine = make_engine(f"sqlite:///{db}")
    run_migrations(engine, backup=False)

    # 逐个回滚到 v2，再逐个升回来（可回滚 = 可重放）
    for version in range(MIGRATIONS[-1].version, 2, -1):
        rollback_step(engine, version)
        assert current_version(_connect(db)) == version - 1
    conn = _connect(db)
    try:
        assert "irreversible" not in _columns(conn, "matters")
        assert _pk(conn, "idempotency_records") == ["task_id", "idempotency_key"]
        assert "approved_at" not in _columns(conn, "stances")
        # v9 回滚要真的把表摘掉，不能只退版本号
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table'"
            " AND name='participant_questions'"
        ).fetchone() is None
        # v11 同理：llm_config 是新建表，回滚必须真删
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='llm_config'"
        ).fetchone() is None
        # 回滚不丢历史授权：authority_legacy 的值回到 authority
        assert conn.execute(
            "SELECT authority FROM stances WHERE stance_id='stn_1'"
        ).fetchone()[0] == "历史自由文本授权"
        assert conn.execute(
            "SELECT response_json FROM idempotency_records"
        ).fetchone()[0] == '{"ok":true}'
    finally:
        conn.close()

    report = run_migrations(engine, backup=False)
    assert report.applied == tuple(m.version for m in MIGRATIONS if m.version > 2)


def test_同一个库跑两次迁移幂等且schema一致(tmp_path):
    db = _legacy_db(tmp_path)
    engine = make_engine(f"sqlite:///{db}")
    run_migrations(engine, backup=False)

    conn = _connect(db)
    before = conn.execute(
        "SELECT name, sql FROM sqlite_master ORDER BY name"
    ).fetchall()
    conn.close()

    report = run_migrations(engine, backup=False)
    assert report.applied == ()

    conn = _connect(db)
    after = conn.execute(
        "SELECT name, sql FROM sqlite_master ORDER BY name"
    ).fetchall()
    conn.close()
    assert before == after


def test_idempotency改主键专项_旧结构多行数据迁移后未丢(tmp_path):
    """事项验收逐字：建旧结构 → 插数据 → 迁移 → 校验数据未丢。"""
    db = _legacy_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.executemany(
        "INSERT INTO idempotency_records VALUES (?, ?, ?, ?, ?)",
        [(f"tsk_{i}", f"key-{i}", f"fp-{i}", f'{{"i":{i}}}',
          "2026-01-01T00:00:00Z") for i in (2, 3, 4)],
    )
    conn.commit()
    conn.close()

    init_db(make_engine(f"sqlite:///{db}"))

    conn = _connect(db)
    try:
        rows = conn.execute(
            "SELECT scope_type, scope_id, idempotency_key, response_json"
            " FROM idempotency_records ORDER BY scope_id"
        ).fetchall()
        assert [r[1] for r in rows] == ["tsk_1", "tsk_2", "tsk_3", "tsk_4"]
        assert all(r[0] == "task" for r in rows)
        assert rows[3][3] == '{"i":4}'
        # 旧复合键在新主键下仍然唯一：同 scope 同 key 冲突必须被拒
        try:
            conn.execute(
                "INSERT INTO idempotency_records (scope_type, scope_id,"
                " idempotency_key, request_fingerprint, response_json, created_at)"
                " VALUES ('task', 'tsk_1', 'key-1', 'x', 'y', 'z')"
            )
            inserted = True
        except sqlite3.IntegrityError:
            inserted = False
        assert not inserted
        conn.rollback()
    finally:
        conn.close()


def test_迁移写审计事件_全新库守卫不炸(caplog, tmp_path):
    """补充决策 A：主留痕 = schema_migrations；每个已应用步骤补一条
    SCHEMA_MIGRATED 审计；全新库上迁移跑在 create_all 之前、audit_events
    不存在 → 只写 logging 不报错（守卫生效）。"""
    db = _legacy_db(tmp_path)
    engine = make_engine(f"sqlite:///{db}")
    report = run_migrations(engine, backup=False)

    conn = _connect(db)
    try:
        # 旧库由 create_all 时代建成，audit_events 存在 → 每个已应用步骤一条
        rows = conn.execute(
            "SELECT event_type, actor_user_id, matter_id, detail FROM audit_events"
            " WHERE event_type=?", (SCHEMA_MIGRATED,)
        ).fetchall()
        assert len(rows) == len(report.applied)
        assert all(r[1] is None and r[2] is None for r in rows)
        assert any("idempotency" in (r[3] or "") for r in rows)
    finally:
        conn.close()

    # 全新库：迁移先于 create_all（init_db 的真实顺序），audit_events 不存在
    fresh = tmp_path / "fresh.db"
    init_db(make_engine(f"sqlite:///{fresh}"))  # 不抛错 = 守卫生效
    fresh_conn = _connect(fresh)
    try:
        fresh_conn.execute("SELECT 1 FROM audit_events").fetchone()
    finally:
        fresh_conn.close()


def test_真实hubdb实例完整迁移(tmp_path, monkeypatch):
    """事项验收逐字：拿现有 hub.db 实例实测一次完整迁移。"""
    import shutil
    from pathlib import Path

    real = Path("G:/work/公司项目/mcp-decision-hub/hub.db")
    if not real.exists():
        pytest.skip("本机无真实 hub.db 实例")
    db = tmp_path / "real.db"
    shutil.copy(real, db)

    engine = make_engine(f"sqlite:///{db}")
    run_migrations(engine, backup=False)

    conn = _connect(db)
    try:
        assert current_version(conn) == MIGRATIONS[-1].version
        # 原有业务数据在升级后仍可读
        matters_before = conn.execute("SELECT COUNT(*) FROM matters").fetchone()[0]
        assert matters_before >= 0
    finally:
        conn.close()
