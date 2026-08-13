"""Backup script tests (PRD 12.2, M4 任务 9)."""

import sqlite3

import pytest

from hub.db.session import init_db, make_engine
from scripts.backup_db import backup_database


@pytest.fixture()
def src_db(tmp_path):
    """Create a small SQLite database with business tables."""
    db_path = tmp_path / "source.db"
    engine = make_engine(f"sqlite:///{db_path}")
    init_db(engine)
    # Insert a row to verify data is copied
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO users (username, email, password_hash, is_admin, is_active, "
        "must_change_password, created_at) VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
        ("test", "test@example.com", "hash", 0, 1, 0),
    )
    conn.commit()
    conn.close()
    return f"sqlite:///{db_path}"


def test_backup_creates_file_with_integrity(src_db, tmp_path):
    """备份文件存在、完整性检查通过、数据一致。"""
    out_dir = tmp_path / "backups"
    backup_path = backup_database(src_db, str(out_dir))
    assert backup_path.exists()
    # Integrity check
    conn = sqlite3.connect(str(backup_path))
    result = conn.execute("PRAGMA integrity_check").fetchone()
    assert result[0] == "ok"
    # Data check
    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert count == 1
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "users" in tables
    assert "matters" in tables
    conn.close()


def test_backup_with_wal_mode(src_db, tmp_path):
    """WAL 模式下备份仍成功。"""
    # Enable WAL mode on source
    conn = sqlite3.connect(src_db.replace("sqlite:///", ""))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "INSERT INTO users (username, email, password_hash, is_admin, is_active, "
        "must_change_password, created_at) VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
        ("wal_user", "wal@example.com", "hash", 0, 1, 0),
    )
    conn.commit()
    conn.close()

    backup_path = backup_database(src_db, str(tmp_path / "wal_backup"))
    assert backup_path.exists()
    c = sqlite3.connect(str(backup_path))
    assert c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2
    c.close()


def test_backup_nonexistent_raises(src_db, tmp_path):
    """源文件不存在时抛出 FileNotFoundError。"""
    with pytest.raises(FileNotFoundError):
        backup_database("sqlite:///nonexistent.db", str(tmp_path))
