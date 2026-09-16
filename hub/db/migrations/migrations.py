"""Concrete migration steps.

Every step is a plain function over a raw ``sqlite3.Connection``. A step owns
its own transaction (``BEGIN`` / ``COMMIT``) so that a failure rolls back only
that step, leaving earlier — already recorded — versions durable.

The DDL below is a **frozen snapshot** of the schema as of the version that
introduced the step. It deliberately does not read :mod:`hub.db.models`:
migrations must keep reproducing the same result even after the ORM models
move on (a later change gets its own step).
"""

from __future__ import annotations

import sqlite3

# SQLite cannot ADD a CHECK constraint in place, so the table has to be
# rebuilt (the documented 12-step procedure). The rebuilt table is created
# under a temporary name, filled, then swapped in.
_NEW_STANCES_DDL = """
CREATE TABLE stances_new (
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
    CONSTRAINT ck_stances_stance
        CHECK (stance IN ('support', 'oppose', 'conditional', 'abstain', 'need_info')),
    CONSTRAINT ck_stances_acting_as
        CHECK (acting_as IN ('human', 'agent_on_behalf')),
    CONSTRAINT ck_stances_urgency
        CHECK (urgency IN ('low', 'normal', 'high')),
    CONSTRAINT ck_stances_visibility
        CHECK (visibility IN ('participants', 'all')),
    UNIQUE (matter_id, round_number, user_id),
    FOREIGN KEY(matter_id) REFERENCES matters (id),
    FOREIGN KEY(user_id) REFERENCES users (id),
    FOREIGN KEY(supersedes) REFERENCES stances (stance_id)
)
"""

_STANCES_COLUMNS = (
    "stance_id", "matter_id", "round_number", "user_id", "stance", "confidence",
    "position_summary", "rationale_summary", "non_negotiables", "conditions",
    "open_questions", "depends_on", "questions_for", "disagreement_kind",
    "supersedes", "acting_as", "authority", "ttl_seconds", "urgency",
    "visibility", "content_hash", "created_at",
)

_STANCES_INDEXES = (
    "CREATE INDEX ix_stances_matter_id ON stances (matter_id)",
    "CREATE INDEX ix_stances_user_id ON stances (user_id)",
)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def upgrade_baseline(conn: sqlite3.Connection) -> None:
    """Baseline marker for the schema produced by ``Base.metadata.create_all``.

    Intentionally a no-op. It exists so that pre-versioning databases can be
    assigned a version number (see the baseline policy in the package
    docstring of :mod:`hub.db.migrations`).
    """


def downgrade_baseline(conn: sqlite3.Connection) -> None:
    """Baseline 是标记性步骤（no-op），无结构变更可回滚。"""


def upgrade_add_stance_check_constraints(conn: sqlite3.Connection) -> None:
    """Rebuild ``stances`` so the four enum CHECK constraints actually exist.

    Presence-based guard, not version-based: a brand new database has no
    ``stances`` table yet (``create_all`` will build it correctly right after)
    and a database that already carries the constraints is left untouched —
    which is what makes this step safely re-entrant.
    """
    if not _table_exists(conn, "stances"):
        return
    if "ck_stances_" in _stances_ddl(conn):
        return

    # ``PRAGMA foreign_keys`` only takes effect outside a transaction; the
    # connection handed to us is in autocommit mode, so this is safe here.
    previous_foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN")
        try:
            conn.execute(_NEW_STANCES_DDL)
            columns = ", ".join(_STANCES_COLUMNS)
            conn.execute(
                f"INSERT INTO stances_new ({columns}) SELECT {columns} FROM stances"
            )
            conn.execute("DROP TABLE stances")
            conn.execute("ALTER TABLE stances_new RENAME TO stances")
            for statement in _STANCES_INDEXES:
                conn.execute(statement)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
    finally:
        conn.execute(f"PRAGMA foreign_keys={'ON' if previous_foreign_keys else 'OFF'}")


def _stances_ddl(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='stances'"
    ).fetchone()
    return row[0] if row else ""


_STANCES_V1_SHAPE_DDL = """
CREATE TABLE stances_new (
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


def downgrade_add_stance_check_constraints(conn: sqlite3.Connection) -> None:
    """回滚 v2：重建 stances 为无 CHECK 的 pre-versioning 形态（数据保留）。

    Re-entrant：库里的 stances 本来就没有 CHECK（或表不存在）→ 跳过。
    """
    if not _table_exists(conn, "stances"):
        return
    if "ck_stances_" not in _stances_ddl(conn):
        return
    _rebuild_table(
        conn,
        create_new=_STANCES_V1_SHAPE_DDL,
        insert_select=(
            f"INSERT INTO stances_new ({', '.join(_STANCES_V2_SHAPE_COLUMNS)}) "
            f"SELECT {', '.join(_STANCES_V2_SHAPE_COLUMNS)} FROM stances"
        ),
        old_table="stances",
        new_table="stances_new",
        indexes=_STANCES_INDEXES,
    )


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_columns(conn: sqlite3.Connection, table: str,
                 definitions: dict[str, str]) -> None:
    """ADD COLUMN per definition; re-entrant via column presence."""
    existing = _columns(conn, table)
    for name, ddl in definitions.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _drop_columns(conn: sqlite3.Connection, table: str,
                  names: tuple[str, ...]) -> None:
    existing = _columns(conn, table)
    for name in names:
        if name in existing:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {name}")


# --------------------------------------------------------------------------
# v3（PRD A1 · 源自决策 Q1/Q2/Q5 与补充决策 S1/S3）：stances 的「加列」与
# 「收紧枚举」在 SQLite 下走同一次表重建，物理上无法拆开 → 合并为一条迁移。
# 禁止启发式回填（PRD §3.4-1）：存量自由文本 authority 原值搬入
# authority_legacy，authority 一律置 NULL。
# --------------------------------------------------------------------------

_STANCES_V3_DDL = """
CREATE TABLE stances_new (
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
    authority VARCHAR(32),
    authority_legacy TEXT,
    approved_at DATETIME,
    ttl_seconds INTEGER,
    urgency VARCHAR(16) NOT NULL,
    visibility VARCHAR(16) NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (stance_id),
    CONSTRAINT ck_stances_stance
        CHECK (stance IN ('support', 'oppose', 'conditional', 'abstain', 'need_info')),
    CONSTRAINT ck_stances_acting_as
        CHECK (acting_as IN ('human', 'agent_on_behalf')),
    CONSTRAINT ck_stances_urgency
        CHECK (urgency IN ('low', 'normal', 'high')),
    CONSTRAINT ck_stances_visibility
        CHECK (visibility IN ('participants', 'all')),
    CONSTRAINT ck_stances_authority
        CHECK (authority IS NULL OR authority IN ('propose_only', 'can_commit')),
    UNIQUE (matter_id, round_number, user_id),
    FOREIGN KEY(matter_id) REFERENCES matters (id),
    FOREIGN KEY(user_id) REFERENCES users (id),
    FOREIGN KEY(supersedes) REFERENCES stances (stance_id)
)
"""

_STANCES_V3_COLUMNS = (
    "stance_id", "matter_id", "round_number", "user_id", "stance", "confidence",
    "position_summary", "rationale_summary", "non_negotiables", "conditions",
    "open_questions", "depends_on", "questions_for", "disagreement_kind",
    "supersedes", "acting_as", "authority", "authority_legacy", "approved_at",
    "ttl_seconds", "urgency", "visibility", "content_hash", "created_at",
)

_STANCES_V2_SHAPE_COLUMNS = (
    "stance_id", "matter_id", "round_number", "user_id", "stance", "confidence",
    "position_summary", "rationale_summary", "non_negotiables", "conditions",
    "open_questions", "depends_on", "questions_for", "disagreement_kind",
    "supersedes", "acting_as", "authority", "ttl_seconds", "urgency",
    "visibility", "content_hash", "created_at",
)


def _rebuild_table(conn: sqlite3.Connection, *, create_new: str,
                   insert_select: str, old_table: str, new_table: str,
                   indexes: tuple[str, ...] = ()) -> None:
    """The documented SQLite 12-step table rebuild, in one transaction."""
    previous_foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN")
        try:
            conn.execute(create_new)
            conn.execute(insert_select)
            conn.execute(f"DROP TABLE {old_table}")
            conn.execute(f"ALTER TABLE {new_table} RENAME TO {old_table}")
            for statement in indexes:
                conn.execute(statement)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
    finally:
        conn.execute(f"PRAGMA foreign_keys={'ON' if previous_foreign_keys else 'OFF'}")


def _stances_has_v3(conn: sqlite3.Connection) -> bool:
    return {"approved_at", "authority_legacy"} <= _columns(conn, "stances")


def upgrade_stance_authority_enum_and_approval(conn: sqlite3.Connection) -> None:
    """v3：stances 加 approved_at（可空）+ authority 收敛为两档枚举 +
    authority_legacy 只读兼容列。走一次表重建（加列与加 CHECK 无法拆开）。

    Re-entrant：全新库由 create_all 直接建成 v3 形态（列已存在）→ 跳过。
    """
    if not _table_exists(conn, "stances") or _stances_has_v3(conn):
        return
    _rebuild_table(
        conn,
        create_new=_STANCES_V3_DDL,
        insert_select=(
            f"INSERT INTO stances_new ({', '.join(_STANCES_V3_COLUMNS)}) "
            "SELECT stance_id, matter_id, round_number, user_id, stance,"
            " confidence, position_summary, rationale_summary, non_negotiables,"
            " conditions, open_questions, depends_on, questions_for,"
            " disagreement_kind, supersedes, acting_as, NULL AS authority,"
            " authority AS authority_legacy, NULL AS approved_at, ttl_seconds,"
            " urgency, visibility, content_hash, created_at FROM stances"
        ),
        old_table="stances",
        new_table="stances_new",
        indexes=_STANCES_INDEXES,
    )


def downgrade_stance_authority_enum_and_approval(conn: sqlite3.Connection) -> None:
    """回滚 v3：退回 v2 形态（无 approved_at / authority_legacy / authority
    CHECK）。历史授权从 authority_legacy 还原回 authority（可逆性的关键）；
    尚未经过本迁移的 authority 取值不受影响。"""
    if not _table_exists(conn, "stances") or not _stances_has_v3(conn):
        return
    _rebuild_table(
        conn,
        create_new=_NEW_STANCES_DDL,
        insert_select=(
            f"INSERT INTO stances_new ({', '.join(_STANCES_V2_SHAPE_COLUMNS)}) "
            "SELECT stance_id, matter_id, round_number, user_id, stance,"
            " confidence, position_summary, rationale_summary, non_negotiables,"
            " conditions, open_questions, depends_on, questions_for,"
            " disagreement_kind, supersedes, acting_as,"
            " COALESCE(authority, authority_legacy) AS authority, ttl_seconds,"
            " urgency, visibility, content_hash, created_at FROM stances"
        ),
        old_table="stances",
        new_table="stances_new",
        indexes=_STANCES_INDEXES,
    )


# v4：matters +4 列（irreversible / options / overall_deadline / item_version）

_MATTER_ITEM_COLUMNS = {
    "irreversible": "BOOLEAN",
    "options": "JSON",
    "overall_deadline": "DATETIME",
    "item_version": "INTEGER",
}


def upgrade_add_matter_item_columns(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "matters"):
        return
    _add_columns(conn, "matters", _MATTER_ITEM_COLUMNS)


def downgrade_add_matter_item_columns(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "matters"):
        return
    _drop_columns(conn, "matters", tuple(_MATTER_ITEM_COLUMNS))


# v5：matter_participants +2 列（agent_authority / visibility_scope）

_PARTICIPANT_AUTHORITY_COLUMNS = {
    "agent_authority": "VARCHAR(32)",
    "visibility_scope": "VARCHAR(32)",
}


def upgrade_add_participant_authority_columns(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "matter_participants"):
        return
    _add_columns(conn, "matter_participants", _PARTICIPANT_AUTHORITY_COLUMNS)


def downgrade_add_participant_authority_columns(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "matter_participants"):
        return
    _drop_columns(conn, "matter_participants",
                  tuple(_PARTICIPANT_AUTHORITY_COLUMNS))


# v6：round_summaries +2 列（agreement_score / clusters）

_SUMMARY_EVALUATION_COLUMNS = {
    "agreement_score": "FLOAT",
    "clusters": "JSON",
}


def upgrade_add_summary_evaluation_columns(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "round_summaries"):
        return
    _add_columns(conn, "round_summaries", _SUMMARY_EVALUATION_COLUMNS)


def downgrade_add_summary_evaluation_columns(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "round_summaries"):
        return
    _drop_columns(conn, "round_summaries", tuple(_SUMMARY_EVALUATION_COLUMNS))


# v7：idempotency_records 改主键（task_id 作用域 → scope_type + scope_id）。
# SQLite 改主键必须重建表 + 拷数据（事项点名的最易出错步骤）。

_IDEMPOTENCY_V7_DDL = """
CREATE TABLE idempotency_records_new (
    scope_type VARCHAR(32) NOT NULL,
    scope_id VARCHAR(48) NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    task_id VARCHAR(48),
    request_fingerprint VARCHAR(64) NOT NULL,
    response_json TEXT NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (scope_type, scope_id, idempotency_key),
    FOREIGN KEY(task_id) REFERENCES tasks (id)
)
"""


def _idempotency_pk(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("PRAGMA table_info(idempotency_records)").fetchall()
    return [r[1] for r in sorted((r for r in rows if r[5]), key=lambda r: r[5])]


def upgrade_rescope_idempotency_primary_key(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "idempotency_records"):
        return
    if _idempotency_pk(conn) == ["scope_type", "scope_id", "idempotency_key"]:
        return
    _rebuild_table(
        conn,
        create_new=_IDEMPOTENCY_V7_DDL,
        insert_select=(
            "INSERT INTO idempotency_records_new (scope_type, scope_id,"
            " idempotency_key, task_id, request_fingerprint, response_json,"
            " created_at) SELECT 'task', task_id, idempotency_key, task_id,"
            " request_fingerprint, response_json, created_at"
            " FROM idempotency_records"
        ),
        old_table="idempotency_records",
        new_table="idempotency_records_new",
    )


def downgrade_rescope_idempotency_primary_key(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "idempotency_records"):
        return
    if _idempotency_pk(conn) == ["task_id", "idempotency_key"]:
        return
    # 旧结构只容纳 task 作用域：非 task 作用域的行在回滚后无法表达，
    # 将被丢弃 —— 这是回滚到旧世界的语义代价，见 docstring 之诚实边界。
    _rebuild_table(
        conn,
        create_new=(
            "CREATE TABLE idempotency_records_new ("
            "task_id VARCHAR(48) NOT NULL, idempotency_key VARCHAR(128) NOT NULL,"
            "request_fingerprint VARCHAR(64) NOT NULL, response_json TEXT NOT NULL,"
            "created_at DATETIME NOT NULL,"
            "PRIMARY KEY (task_id, idempotency_key),"
            "FOREIGN KEY(task_id) REFERENCES tasks (id))"
        ),
        insert_select=(
            "INSERT INTO idempotency_records_new (task_id, idempotency_key,"
            " request_fingerprint, response_json, created_at) SELECT task_id,"
            " idempotency_key, request_fingerprint, response_json, created_at"
            " FROM idempotency_records WHERE scope_type='task'"
        ),
        old_table="idempotency_records",
        new_table="idempotency_records_new",
    )


# v8：outputs + authority（补齐两路径留痕对称）

_OUTPUT_AUTHORITY_COLUMNS = {"authority": "VARCHAR(32)"}


def upgrade_add_output_authority(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "outputs"):
        return
    _add_columns(conn, "outputs", _OUTPUT_AUTHORITY_COLUMNS)


def downgrade_add_output_authority(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "outputs"):
        return
    _drop_columns(conn, "outputs", tuple(_OUTPUT_AUTHORITY_COLUMNS))


# v9：participant_questions（r5Am9i · ask_participant 的落点）
#
# 为什么不挂到 stances.questions_for：那一列是「本人向他人提问」，且该表按
# (matter_id, round_number, user_id) 唯一 —— 目标本轮尚未提交立场时连行都
# 不存在，承载不了「待其提交立场时需回答的问题」。详见
# outputs/2026-09-16-r5Am9i-ask_participant-阻塞.md。
#
# 注：开工包 PRD-09（rs9ncY 决策日志）原定 v9 → 顺延 v10。

_NEW_PARTICIPANT_QUESTIONS_DDL = """
CREATE TABLE participant_questions (
    id VARCHAR(48) NOT NULL,
    matter_id VARCHAR(48) NOT NULL,
    round_number INTEGER NOT NULL,
    target_user_id INTEGER NOT NULL,
    asked_by_user_id INTEGER NOT NULL,
    question TEXT NOT NULL,
    question_hash VARCHAR(64) NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (matter_id, round_number, target_user_id, asked_by_user_id,
            question_hash),
    FOREIGN KEY(matter_id) REFERENCES matters (id),
    FOREIGN KEY(target_user_id) REFERENCES users (id),
    FOREIGN KEY(asked_by_user_id) REFERENCES users (id)
)
"""


def upgrade_add_participant_questions(conn: sqlite3.Connection) -> None:
    """建 participant_questions。幂等：表已存在直接返回。"""
    if _table_exists(conn, "participant_questions"):
        return
    conn.execute(_NEW_PARTICIPANT_QUESTIONS_DDL)
    conn.execute(
        "CREATE INDEX ix_participant_questions_matter_target"
        " ON participant_questions (matter_id, target_user_id, round_number)"
    )


def downgrade_add_participant_questions(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS ix_participant_questions_matter_target")
    conn.execute("DROP TABLE IF EXISTS participant_questions")
