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


# v10：matters +1 列（irreversible_reason）
# 背景：873ff8d（2026-09-15）把这列加进 models.py 但没写迁移——v9 及以前
# 的存量库启动即炸 no such column（2026-09-18 测试服务器部署实录）。

_MATTER_IRREVERSIBLE_REASON = {
    "irreversible_reason": "TEXT",
}


def upgrade_add_matter_irreversible_reason(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "matters"):
        return
    _add_columns(conn, "matters", _MATTER_IRREVERSIBLE_REASON)


def downgrade_add_matter_irreversible_reason(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "matters"):
        return
    _drop_columns(conn, "matters", tuple(_MATTER_IRREVERSIBLE_REASON))


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


# v11：llm_config（模型配置页的落点）
#
# 为什么配置要进 DB 而不是继续待在 .env：Settings 是 frozen dataclass、
# DeepSeekClient 在 create_app() 里只构造一次 —— 改 .env 必然要重启进程，
# 而这个应用没有任何重启入口。落在 DB 后，页面保存即可对后续调用生效
# （读层见 hub/llm/runtime.py）。
#
# 不播种任何行：**空表 = 逐字段回落 Settings（env / .env）**，即与改造前
# 行为完全一致。这样存量库升级后不需要任何数据迁移，也不需要「猜一个默认
# 值写进去」——写进去反而会把 env 的配置盖掉。

_NEW_LLM_CONFIG_DDL = """
CREATE TABLE llm_config (
    id INTEGER NOT NULL,
    model VARCHAR(64) NOT NULL,
    base_url VARCHAR(255) NOT NULL,
    api_key TEXT,
    updated_at DATETIME NOT NULL,
    updated_by INTEGER,
    PRIMARY KEY (id),
    CONSTRAINT ck_llm_config_singleton CHECK (id = 1),
    FOREIGN KEY(updated_by) REFERENCES users (id)
)
"""


def upgrade_add_llm_config(conn: sqlite3.Connection) -> None:
    """建 llm_config。幂等：表已存在直接返回。"""
    if _table_exists(conn, "llm_config"):
        return
    conn.execute(_NEW_LLM_CONFIG_DDL)


def downgrade_add_llm_config(conn: sqlite3.Connection) -> None:
    """回滚即删表，连配置一起 —— 语义代价：页面设置过的 key 无法找回。

    这是回滚到旧世界的必然代价（旧世界没有这张表可存它）。env 里的
    DEEPSEEK_API_KEY 不受影响，回滚后服务照常可用。
    """
    conn.execute("DROP TABLE IF EXISTS llm_config")


# ============================================================================
# Phase 1: 邀请链接与批处理基础（v12, v13, v14）
# ============================================================================

_NEW_MATTER_COLLABORATION_DDL = """
ALTER TABLE matters ADD COLUMN mode VARCHAR(32) DEFAULT 'project';
ALTER TABLE matters ADD COLUMN auto_start BOOLEAN NOT NULL DEFAULT 0;
"""

_NEW_INVITATION_LINKS_DDL = """
CREATE TABLE invitation_links (
    id VARCHAR(48) NOT NULL PRIMARY KEY,
    matter_id VARCHAR(48) NOT NULL,
    short_code VARCHAR(8) NOT NULL UNIQUE,
    invited_name VARCHAR(128),
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    max_uses INTEGER NOT NULL DEFAULT 1,
    used_count INTEGER NOT NULL DEFAULT 0,
    expires_at DATETIME NOT NULL,
    created_by INTEGER NOT NULL,
    created_at DATETIME NOT NULL,
    revoked_at DATETIME,
    revoked_by INTEGER,
    FOREIGN KEY(matter_id) REFERENCES matters (id),
    FOREIGN KEY(created_by) REFERENCES users (id),
    FOREIGN KEY(revoked_by) REFERENCES users (id),
    UNIQUE(matter_id, short_code)
)
"""

_INVITATION_LINKS_INDEXES = (
    "CREATE INDEX ix_invitation_links_matter_id ON invitation_links (matter_id)",
    "CREATE INDEX ix_invitation_links_short_code ON invitation_links (short_code)",
)

_NEW_INVITATION_CONSUMPTIONS_DDL = """
CREATE TABLE invitation_consumptions (
    id VARCHAR(48) NOT NULL PRIMARY KEY,
    invitation_id VARCHAR(48) NOT NULL,
    user_id INTEGER NOT NULL,
    ip_address VARCHAR(64),
    user_agent VARCHAR(255),
    consumed_at DATETIME NOT NULL,
    FOREIGN KEY(invitation_id) REFERENCES invitation_links (id),
    FOREIGN KEY(user_id) REFERENCES users (id)
)
"""

_INVITATION_CONSUMPTIONS_INDEXES = (
    "CREATE INDEX ix_invitation_consumptions_invitation_id ON invitation_consumptions"
    " (invitation_id)",
    "CREATE INDEX ix_invitation_consumptions_user_id ON invitation_consumptions (user_id)",
)

_NEW_BATCH_PROCESSING_QUEUE_DDL = """
CREATE TABLE batch_processing_queue (
    id VARCHAR(48) NOT NULL PRIMARY KEY,
    matter_id VARCHAR(48) NOT NULL,
    round_number INTEGER NOT NULL,
    stance_id VARCHAR(48) NOT NULL UNIQUE,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    batch_id VARCHAR(48),
    scheduled_at DATETIME NOT NULL,
    processed_at DATETIME,
    error_message TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL,
    FOREIGN KEY(matter_id) REFERENCES matters (id),
    FOREIGN KEY(stance_id) REFERENCES stances (stance_id)
)
"""

_BATCH_PROCESSING_QUEUE_INDEXES = (
    "CREATE INDEX ix_batch_processing_queue_matter_id ON batch_processing_queue (matter_id)",
    "CREATE INDEX ix_batch_processing_queue_batch_id ON batch_processing_queue (batch_id)",
)


def upgrade_add_matter_collaboration_mode(conn: sqlite3.Connection) -> None:
    """为 matters 表添加协作模式字段（mode, auto_start）。
    
    mode: 'meeting'（实时）或 'project'（异步批处理），默认 'project'
    auto_start: 所有参与者加入后是否自动启动，默认 FALSE
    """
    # 检查 matters 表是否存在
    if not _table_exists(conn, "matters"):
        return
    
    conn.execute("BEGIN")
    try:
        # 检查列是否已存在
        cursor = conn.execute("PRAGMA table_info(matters)")
        columns = {row[1] for row in cursor.fetchall()}
        
        if "mode" not in columns:
            conn.execute("ALTER TABLE matters ADD COLUMN mode VARCHAR(32) DEFAULT 'project'")
        
        if "auto_start" not in columns:
            conn.execute("ALTER TABLE matters ADD COLUMN auto_start BOOLEAN NOT NULL DEFAULT 0")
        
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def downgrade_add_matter_collaboration_mode(conn: sqlite3.Connection) -> None:
    """SQLite 不支持 DROP COLUMN，需要重建表。
    
    实践中这个回滚很少使用，因为 mode/auto_start 是可选字段，
    不影响现有功能。如果需要回滚，建议手动处理。
    """
    # SQLite 不支持 ALTER TABLE DROP COLUMN
    # 如果真需要回滚，需要重建整个 matters 表
    pass


def upgrade_add_invitation_links(conn: sqlite3.Connection) -> None:
    """创建邀请链接表和消费记录表。幂等：表已存在直接返回。"""
    conn.execute("BEGIN")
    try:
        if not _table_exists(conn, "invitation_links"):
            conn.execute(_NEW_INVITATION_LINKS_DDL)
            for index_sql in _INVITATION_LINKS_INDEXES:
                conn.execute(index_sql)
        
        if not _table_exists(conn, "invitation_consumptions"):
            conn.execute(_NEW_INVITATION_CONSUMPTIONS_DDL)
            for index_sql in _INVITATION_CONSUMPTIONS_INDEXES:
                conn.execute(index_sql)
        
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def downgrade_add_invitation_links(conn: sqlite3.Connection) -> None:
    """删除邀请链接相关表。"""
    conn.execute("BEGIN")
    try:
        conn.execute("DROP TABLE IF EXISTS invitation_consumptions")
        conn.execute("DROP TABLE IF EXISTS invitation_links")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def upgrade_add_batch_processing_queue(conn: sqlite3.Connection) -> None:
    """创建批处理队列表。幂等：表已存在直接返回。"""
    if _table_exists(conn, "batch_processing_queue"):
        return
    
    conn.execute("BEGIN")
    try:
        conn.execute(_NEW_BATCH_PROCESSING_QUEUE_DDL)
        for index_sql in _BATCH_PROCESSING_QUEUE_INDEXES:
            conn.execute(index_sql)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def downgrade_add_batch_processing_queue(conn: sqlite3.Connection) -> None:
    """删除批处理队列表。"""
    conn.execute("DROP TABLE IF EXISTS batch_processing_queue")


def upgrade_allow_unlimited_invitation_uses(conn: sqlite3.Connection) -> None:
    """允许邀请链接的 max_uses 为 NULL（表示无限制使用）。
    
    SQLite 的 ALTER TABLE 不支持修改列的 NULL 约束，
    因此需要重建 invitation_links 表。
    """
    if not _table_exists(conn, "invitation_links"):
        return
    
    # 必须在事务外禁用外键检查
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN")
    try:
        # 1. 创建新表（max_uses 可为 NULL）
        conn.execute("""
            CREATE TABLE invitation_links_new (
                id VARCHAR(48) PRIMARY KEY,
                matter_id VARCHAR(48) NOT NULL,
                short_code VARCHAR(8) UNIQUE NOT NULL,
                invited_name VARCHAR(128),
                status VARCHAR(16) DEFAULT 'active' NOT NULL,
                max_uses INTEGER,
                used_count INTEGER DEFAULT 0 NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                created_by INTEGER NOT NULL,
                created_at TIMESTAMP NOT NULL,
                revoked_at TIMESTAMP,
                revoked_by INTEGER,
                FOREIGN KEY(matter_id) REFERENCES matters (id),
                FOREIGN KEY(created_by) REFERENCES users (id),
                FOREIGN KEY(revoked_by) REFERENCES users (id),
                CONSTRAINT uq_matter_short_code UNIQUE (matter_id, short_code)
            )
        """)
        
        # 2. 复制数据
        conn.execute("""
            INSERT INTO invitation_links_new
            SELECT * FROM invitation_links
        """)
        
        # 3. 删除旧表
        conn.execute("DROP TABLE invitation_links")
        
        # 4. 重命名新表
        conn.execute("ALTER TABLE invitation_links_new RENAME TO invitation_links")
        
        # 5. 重建索引
        conn.execute(
            "CREATE UNIQUE INDEX ix_invitation_links_short_code"
            " ON invitation_links (short_code)"
        )
        conn.execute("CREATE INDEX ix_invitation_links_matter_id ON invitation_links (matter_id)")
        conn.execute("CREATE INDEX ix_invitation_links_status ON invitation_links (status)")
        
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        # 重新启用外键检查
        conn.execute("PRAGMA foreign_keys=ON")


def downgrade_allow_unlimited_invitation_uses(conn: sqlite3.Connection) -> None:
    """回滚：将 max_uses 改回 NOT NULL（所有 NULL 值设为 1）。"""
    if not _table_exists(conn, "invitation_links"):
        return
    
    # 必须在事务外禁用外键检查
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN")
    try:
        # 1. 创建旧表（max_uses 不可为 NULL）
        conn.execute("""
            CREATE TABLE invitation_links_old (
                id VARCHAR(48) PRIMARY KEY,
                matter_id VARCHAR(48) NOT NULL,
                short_code VARCHAR(8) UNIQUE NOT NULL,
                invited_name VARCHAR(128),
                status VARCHAR(16) DEFAULT 'active' NOT NULL,
                max_uses INTEGER DEFAULT 1 NOT NULL,
                used_count INTEGER DEFAULT 0 NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                created_by INTEGER NOT NULL,
                created_at TIMESTAMP NOT NULL,
                revoked_at TIMESTAMP,
                revoked_by INTEGER,
                FOREIGN KEY(matter_id) REFERENCES matters (id),
                FOREIGN KEY(created_by) REFERENCES users (id),
                FOREIGN KEY(revoked_by) REFERENCES users (id),
                CONSTRAINT uq_matter_short_code UNIQUE (matter_id, short_code)
            )
        """)
        
        # 2. 复制数据（将 NULL 转为 1）
        conn.execute("""
            INSERT INTO invitation_links_old
            SELECT 
                id, matter_id, short_code, invited_name, status,
                COALESCE(max_uses, 1) as max_uses,
                used_count, expires_at, created_by, created_at,
                revoked_at, revoked_by
            FROM invitation_links
        """)
        
        # 3. 删除新表
        conn.execute("DROP TABLE invitation_links")
        
        # 4. 重命名旧表
        conn.execute("ALTER TABLE invitation_links_old RENAME TO invitation_links")
        
        # 5. 重建索引
        conn.execute(
            "CREATE UNIQUE INDEX ix_invitation_links_short_code"
            " ON invitation_links (short_code)"
        )
        conn.execute("CREATE INDEX ix_invitation_links_matter_id ON invitation_links (matter_id)")
        conn.execute("CREATE INDEX ix_invitation_links_status ON invitation_links (status)")
        
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        # 重新启用外键检查
        conn.execute("PRAGMA foreign_keys=ON")


def upgrade_add_meeting_tables(conn: sqlite3.Connection) -> None:
    """添加会议模式表（voice-copilot 实时协作）。
    
    新增三个表：
    - meetings: 会议实例
    - meeting_stances: 会议立场（本地 LLM 整理后的文本）
    - meeting_convergences: 收敛结果（云端 LLM 生成的摘要）
    """
    if _table_exists(conn, "meetings"):
        return
    
    conn.execute("BEGIN")
    try:
        # 1. 会议表
        conn.execute("""
            CREATE TABLE meetings (
                id VARCHAR(48) PRIMARY KEY,
                matter_id VARCHAR(48) NOT NULL,
                round_number INTEGER DEFAULT 1 NOT NULL,
                status VARCHAR(16) DEFAULT 'active' NOT NULL,
                timeout_minutes INTEGER DEFAULT 3 NOT NULL,
                created_at TIMESTAMP NOT NULL,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                FOREIGN KEY(matter_id) REFERENCES matters (id)
            )
        """)
        conn.execute("CREATE INDEX ix_meetings_matter_id ON meetings (matter_id)")
        
        # 2. 会议立场表
        conn.execute("""
            CREATE TABLE meeting_stances (
                id VARCHAR(48) PRIMARY KEY,
                meeting_id VARCHAR(48) NOT NULL,
                user_id INTEGER NOT NULL,
                round_number INTEGER NOT NULL,
                text TEXT NOT NULL,
                submitted_at TIMESTAMP NOT NULL,
                FOREIGN KEY(meeting_id) REFERENCES meetings (id),
                FOREIGN KEY(user_id) REFERENCES users (id),
                CONSTRAINT uq_meeting_stance_per_round 
                    UNIQUE (meeting_id, round_number, user_id)
            )
        """)
        conn.execute("CREATE INDEX ix_meeting_stances_meeting_id ON meeting_stances (meeting_id)")
        conn.execute("CREATE INDEX ix_meeting_stances_user_id ON meeting_stances (user_id)")
        
        # 3. 会议收敛结果表
        conn.execute("""
            CREATE TABLE meeting_convergences (
                id VARCHAR(48) PRIMARY KEY,
                meeting_id VARCHAR(48) NOT NULL,
                round_number INTEGER NOT NULL,
                consensus JSON NOT NULL,
                divergences JSON NOT NULL,
                follow_ups JSON NOT NULL,
                generated_at TIMESTAMP NOT NULL,
                FOREIGN KEY(meeting_id) REFERENCES meetings (id),
                CONSTRAINT uq_meeting_convergence_per_round 
                    UNIQUE (meeting_id, round_number)
            )
        """)
        conn.execute(
            "CREATE INDEX ix_meeting_convergences_meeting_id"
            " ON meeting_convergences (meeting_id)"
        )
        
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def downgrade_add_meeting_tables(conn: sqlite3.Connection) -> None:
    """回滚：删除会议模式表。"""
    if not _table_exists(conn, "meetings"):
        return
    
    conn.execute("BEGIN")
    try:
        conn.execute("DROP TABLE IF EXISTS meeting_convergences")
        conn.execute("DROP TABLE IF EXISTS meeting_stances")
        conn.execute("DROP TABLE IF EXISTS meetings")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# --------------------------------------------------------------------------
# v17：留言板协作 —— 新增 matter_messages，matter_participants 增两列
# 背景：协作形态从「一轮一轮下发任务」改为「留言板」。参与人不再等平台派题，
# 随时留言、互相可见，因此需要一张纯留言表；邀请注册要采集「姓名 / 负责什么」，
# 这两个是事项内的角色信息，挂在 matter_participants 而不是 users。
# 存量轮次数据不动（Round/Task 等表保留），新形态不读取它们。
# --------------------------------------------------------------------------

_BOARD_PARTICIPANT_COLUMNS = {
    "display_name": "VARCHAR(100)",
    "responsibility": "TEXT",
}

_MATTER_MESSAGES_DDL = """
CREATE TABLE IF NOT EXISTS matter_messages (
    id VARCHAR(48) NOT NULL,
    matter_id VARCHAR(48) NOT NULL,
    user_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    kind VARCHAR(16) NOT NULL DEFAULT 'message',
    acting_as VARCHAR(32) NOT NULL DEFAULT 'human',
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(matter_id) REFERENCES matters (id),
    FOREIGN KEY(user_id) REFERENCES users (id)
)
"""


def upgrade_add_board_tables(conn: sqlite3.Connection) -> None:
    """建留言表 + 参与人自我介绍两列；可重入。"""
    conn.execute("BEGIN")
    try:
        if _table_exists(conn, "matter_participants"):
            _add_columns(conn, "matter_participants", _BOARD_PARTICIPANT_COLUMNS)
        if not _table_exists(conn, "matter_messages"):
            conn.execute(_MATTER_MESSAGES_DDL)
            conn.execute(
                "CREATE INDEX ix_matter_messages_matter_id ON matter_messages (matter_id)"
            )
            conn.execute(
                "CREATE INDEX ix_matter_messages_user_id ON matter_messages (user_id)"
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def downgrade_add_board_tables(conn: sqlite3.Connection) -> None:
    """回滚：删留言表与参与人两列（留言内容随表丢弃）。"""
    conn.execute("BEGIN")
    try:
        conn.execute("DROP TABLE IF EXISTS matter_messages")
        if _table_exists(conn, "matter_participants"):
            _drop_columns(conn, "matter_participants",
                          tuple(_BOARD_PARTICIPANT_COLUMNS))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# --------------------------------------------------------------------------
# v18：留言板三件事（2026-10-05 甲方要求）
#   1) 上传前必须本人同意 → human_approved_at 留痕
#   2) 可以传 md 文件 → attachment_name / attachment_md（正文存库，云端可读）
#   3) 云端提问 → 本地处理 → 传回回答 → asked_to_user_id /
#      reply_to_message_id / question_status
# SQLite 的 ADD COLUMN 不能带外键，所以这三列在迁移库里没有约束；
# 新库走 create_all 时由 ORM 定义带上外键。可重入。
# --------------------------------------------------------------------------

_BOARD_V18_COLUMNS = {
    "human_approved_at": "DATETIME",
    "attachment_name": "VARCHAR(255)",
    "attachment_md": "TEXT",
    "asked_to_user_id": "INTEGER",
    "reply_to_message_id": "VARCHAR(48)",
    "question_status": "VARCHAR(16)",
}


def upgrade_add_board_message_extras(conn: sqlite3.Connection) -> None:
    """matter_messages 增 6 列：同意留痕 / md 附件 / 定向提问。"""
    if not _table_exists(conn, "matter_messages"):
        return
    conn.execute("BEGIN")
    try:
        _add_columns(conn, "matter_messages", _BOARD_V18_COLUMNS)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_matter_messages_asked_to"
            " ON matter_messages (asked_to_user_id)"
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def downgrade_add_board_message_extras(conn: sqlite3.Connection) -> None:
    """回滚：删掉这 6 列（附件正文随列丢弃）。"""
    if not _table_exists(conn, "matter_messages"):
        return
    conn.execute("BEGIN")
    try:
        # 先删依赖这些列的索引，否则 DROP COLUMN 会被索引挡住
        conn.execute("DROP INDEX IF EXISTS ix_matter_messages_asked_to")
        _drop_columns(conn, "matter_messages", tuple(_BOARD_V18_COLUMNS))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# --------------------------------------------------------------------------
# v19：留言板增量滚动总结（2026-10-05 甲方要求）
# 「云端 AI 实时总结，每次有新信息进来就结合之前的总结给当前任务一个大概判断，
#   保证速度、不占太多上下文」—— 落成一张每板一行的表：
#   covered_messages 是增量游标，更新时只喂「上次总结 + 新增留言」。
# --------------------------------------------------------------------------

_BOARD_SUMMARIES_DDL = """
CREATE TABLE IF NOT EXISTS board_summaries (
    matter_id VARCHAR(48) NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    judgement TEXT NOT NULL DEFAULT '',
    key_points JSON NOT NULL DEFAULT '[]',
    open_questions JSON NOT NULL DEFAULT '[]',
    covered_messages INTEGER NOT NULL DEFAULT 0,
    generation_status VARCHAR(16) NOT NULL DEFAULT 'idle',
    error TEXT,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (matter_id),
    FOREIGN KEY(matter_id) REFERENCES matters (id)
)
"""


def upgrade_add_board_summaries(conn: sqlite3.Connection) -> None:
    """建 board_summaries（一板一行）；可重入。"""
    conn.execute("BEGIN")
    try:
        if not _table_exists(conn, "board_summaries"):
            conn.execute(_BOARD_SUMMARIES_DDL)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def downgrade_add_board_summaries(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN")
    try:
        conn.execute("DROP TABLE IF EXISTS board_summaries")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# --------------------------------------------------------------------------
# v20：总结文档 + 重读开关 + 留言板读索引（2026-10-05 甲方要求）
#
#   1. 「每轮大模型总结完云端信息，都写一个总结文档」→ board_summary_documents
#      （只追加、不覆盖，version 递增）；
#   2. 「下一次总结只读最新的留言板（除非有人提了需求）」→ 增量游标沿用
#      board_summaries.covered_messages；有人提需求时置 reread_requested，
#      下一轮从第一条重新读，理由与提出人留痕；
#   3. 留言板容量放到 1000 条 → 给 (matter_id, created_at, id) 建复合索引，
#      读回「最近 N 条」不再走临时排序。
# --------------------------------------------------------------------------

_BOARD_DOCUMENTS_DDL = """
CREATE TABLE IF NOT EXISTS board_summary_documents (
    id VARCHAR(48) NOT NULL,
    matter_id VARCHAR(48) NOT NULL,
    version INTEGER NOT NULL,
    covered_from INTEGER NOT NULL DEFAULT 0,
    covered_to INTEGER NOT NULL DEFAULT 0,
    delta_messages INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL DEFAULT '',
    judgement TEXT NOT NULL DEFAULT '',
    key_points JSON NOT NULL DEFAULT '[]',
    open_questions JSON NOT NULL DEFAULT '[]',
    content_md TEXT NOT NULL DEFAULT '',
    trigger VARCHAR(16) NOT NULL DEFAULT 'auto',
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (matter_id, version),
    FOREIGN KEY(matter_id) REFERENCES matters (id)
)
"""

_BOARD_V20_COLUMNS = {
    "document_version": "INTEGER NOT NULL DEFAULT 0",
    "reread_requested": "BOOLEAN NOT NULL DEFAULT 0",
    "reread_requested_at": "DATETIME",
    "reread_requested_by": "INTEGER",
    "reread_reason": "TEXT",
}


def upgrade_add_board_summary_documents(conn: sqlite3.Connection) -> None:
    """建总结文档表 + 总结表加 5 列 + 留言板读索引；可重入。"""
    conn.execute("BEGIN")
    try:
        if not _table_exists(conn, "board_summary_documents"):
            conn.execute(_BOARD_DOCUMENTS_DDL)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_board_summary_documents_matter"
            " ON board_summary_documents (matter_id)"
        )
        if _table_exists(conn, "board_summaries"):
            _add_columns(conn, "board_summaries", _BOARD_V20_COLUMNS)
        if _table_exists(conn, "matter_messages"):
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_matter_messages_board"
                " ON matter_messages (matter_id, created_at, id)"
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def downgrade_add_board_summary_documents(conn: sqlite3.Connection) -> None:
    """回滚：删索引/文档表，并把 board_summaries 的 5 列删掉。"""
    conn.execute("BEGIN")
    try:
        conn.execute("DROP INDEX IF EXISTS ix_matter_messages_board")
        conn.execute("DROP INDEX IF EXISTS ix_board_summary_documents_matter")
        conn.execute("DROP TABLE IF EXISTS board_summary_documents")
        if _table_exists(conn, "board_summaries"):
            _drop_columns(conn, "board_summaries", tuple(_BOARD_V20_COLUMNS))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
