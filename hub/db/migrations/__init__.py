"""Tiny hand-rolled schema migration runner (SQLite only, no Alembic).

Why hand-rolled: this project is a single-file / single-instance SQLite app
with one writer process, a single SQL dialect and no multi-dialect burden —
only three things are actually needed: a version table, ordered steps and
idempotency. See ``docs/ops/deployment.md`` § "Schema 升级".

Contract
--------
* ``MIGRATIONS`` is an ordered tuple of steps; ``version`` starts at 1 and is
  strictly increasing.
* Migrations are recorded in the ``schema_migrations`` table.
* ``run_migrations`` is idempotent: if the database is already at the latest
  version nothing is executed and ``MigrationReport.applied`` is empty.
* Steps run through a raw :class:`sqlite3.Connection` (obtained from the
  engine's pool), never through the ORM — a SQLite table rebuild needs exact
  control over the emitted SQL.
* A step owns its own transaction and must guard itself against re-running
  (e.g. "constraint already present → return"). A step that raises is rolled
  back and re-raised as :class:`MigrationError`; the version only advances
  past steps that succeeded.

Baseline policy for pre-versioning databases
--------------------------------------------
Before this module existed the schema was created solely by
``Base.metadata.create_all`` and nothing recorded *which* schema a database
had. There is therefore no way to tell what a versionless database contains,
so we make one assumption:

    **No version table, but the business tables already exist (probed via the
    ``users`` table)  →  the database is claimed as baseline, i.e. version 1.**

The claim is materialised by inserting version 1 into ``schema_migrations``
before the remaining steps run, so subsequent starts see it as authoritative.

When this policy judges wrongly
-------------------------------
The claim is only safe because version 1 is a no-op marker: it never rewrites
anything, it merely states "whatever ``create_all`` produced". It is *wrong*
when a database was produced by a *different* schema than the pre-versioning
one, for example:

* a database restored from a partial/old file backup whose schema predates
  the current ``create_all`` output (e.g. ``stances`` missing whole columns,
  not just the CHECK constraints) — later steps assume the baseline shape and
  can fail or copy data incorrectly;
* a database whose version table was dropped or lost while its schema stayed
  at a newer version — it will be re-claimed as baseline and older steps may
  run again against a newer schema;
* a half-initialised database (``create_all`` interrupted mid-way) that
  happens to contain ``users`` but not all tables.

In all of those cases the assumption is unverifiable by construction. The
mitigation is operational, not technical: as long as an upgrade is about to
happen, ``run_migrations`` takes a WAL-safe backup *before* touching anything
(``backup=True``, the default) and aborts the upgrade if that backup fails.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import Engine

from hub.db.migrations import migrations as _steps


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    upgrade: Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class MigrationReport:
    from_version: int
    to_version: int
    applied: tuple[int, ...]


class MigrationError(RuntimeError):
    """Raised when a step fails; carries the offending version and name."""

    def __init__(self, message: str, *, version: int | None = None,
                 name: str | None = None) -> None:
        super().__init__(message)
        self.version = version
        self.name = name


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "baseline", _steps.upgrade_baseline),
    Migration(
        2,
        "add_stance_check_constraints",
        _steps.upgrade_add_stance_check_constraints,
    ),
)

_VERSION_TABLE = "schema_migrations"
_BUSINESS_PROBE_TABLE = "users"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def current_version(conn: sqlite3.Connection) -> int:
    """Version recorded in ``schema_migrations``; 0 when there is no table.

    Pure read: this never creates or repairs the version table.
    """
    if not _table_exists(conn, _VERSION_TABLE):
        return 0
    row = conn.execute(f"SELECT MAX(version) FROM {_VERSION_TABLE}").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _claimed_version(conn: sqlite3.Connection) -> int:
    version = current_version(conn)
    if version > 0:
        return version
    if _table_exists(conn, _BUSINESS_PROBE_TABLE):
        return 1  # pre-versioning database → claim baseline (see module docstring)
    return 0


def _migration(version: int) -> Migration:
    for migration in MIGRATIONS:
        if migration.version == version:
            return migration
    raise MigrationError(f"版本 {version} 不在 MIGRATIONS 中")


def _ensure_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_VERSION_TABLE} ("
        "version INTEGER PRIMARY KEY, "
        "name TEXT NOT NULL, "
        "applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
        ")"
    )


def _record(conn: sqlite3.Connection, migration: Migration) -> None:
    conn.execute(
        f"INSERT OR REPLACE INTO {_VERSION_TABLE} (version, name) VALUES (?, ?)",
        (migration.version, migration.name),
    )


def _backup(engine: Engine) -> Path | None:
    """WAL-safe pre-migration backup, written next to the database file.

    Reuses ``scripts.backup_db.backup_database`` (``sqlite3.Connection.backup``,
    safe while the app is running). Returns the backup path, or ``None`` for
    databases that have no file to copy (in-memory).
    """
    database = engine.url.database
    if not database or database == ":memory:":
        return None
    from scripts.backup_db import backup_database

    db_path = Path(database).resolve()
    return backup_database(f"sqlite:///{db_path}", str(db_path.parent / "backups"))


def run_migrations(engine: Engine, *, backup: bool = True) -> MigrationReport:
    raw = engine.raw_connection()
    try:
        # Work on the real DBAPI connection: only there can we disable the
        # per-connection ``PRAGMA foreign_keys`` (needed by table rebuilds)
        # and drive transactions explicitly.
        conn = raw.driver_connection
        conn.isolation_level = None
        from_version = _claimed_version(conn)
        pending = tuple(m for m in MIGRATIONS if m.version > from_version)
        if not pending:
            # Already at the latest version: return *before* the backup so the
            # idempotent path has zero side effects (no backup noise on every
            # app start / test run).
            return MigrationReport(from_version, from_version, ())
        if backup:
            # Only reached when there really is an upgrade to run.
            # Before anything is touched. A failure here aborts the upgrade.
            _backup(engine)
        _ensure_version_table(conn)
        if from_version > 0:
            # Materialise the baseline claim so later starts read it as fact.
            _record(conn, _migration(from_version))
        for migration in pending:
            try:
                migration.upgrade(conn)
            except BaseException as exc:
                # A step that died mid-transaction must not linger: roll its
                # transaction back so no half-built schema survives.
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise MigrationError(
                    f"迁移 v{migration.version} ({migration.name}) 失败: {exc}",
                    version=migration.version,
                    name=migration.name,
                ) from exc
            _record(conn, migration)
        return MigrationReport(
            from_version=from_version,
            to_version=MIGRATIONS[-1].version,
            applied=tuple(m.version for m in pending),
        )
    finally:
        raw.close()
