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
