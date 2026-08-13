"""SQLite online backup script (PRD 12.2 — backup and deployment docs).

Uses the sqlite3.Connection.backup() API (Python 3.12+) which is safe for
WAL-mode databases while the application is running — no stop required.

Usage:
    uv run python scripts/backup_db.py [--src sqlite:///./hub.db] [--out ./backups]

The script creates a timestamped copy: ``hub-backup-YYYYmmdd-HHMMSS.db``.
After backup, runs ``PRAGMA integrity_check`` on the copy.
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Resolve project root so imports work when run from scripts/
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def _sqlite_path_from_url(url: str) -> str:
    """Extract filesystem path from a sqlite:/// URL."""
    if not url.startswith("sqlite:///"):
        raise ValueError(f"不是 sqlite:/// URL: {url}")
    path = url[len("sqlite:///"):]
    # Handle relative paths
    if not os.path.isabs(path):
        path = str(PROJECT_ROOT / path)
    return path


def backup_database(src_url: str, out_dir: str) -> Path:
    """Backup a SQLite database using the online backup API. Returns the path
    to the backup file. Safe for WAL databases while the app is running."""
    src_path = _sqlite_path_from_url(src_url)
    if not os.path.exists(src_path):
        raise FileNotFoundError(f"源数据库不存在: {src_path}")

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = out / f"hub-backup-{ts}.db"

    # sqlite3.backup() opens a new connection to the target file
    src_conn = sqlite3.connect(src_path)
    try:
        dst_conn = sqlite3.connect(str(backup_path))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()

    # Integrity check on the backup copy
    check_conn = sqlite3.connect(str(backup_path))
    try:
        result = check_conn.execute("PRAGMA integrity_check").fetchone()
        if result[0] != "ok":
            raise RuntimeError(f"备份完整性检查失败: {result[0]}")
    finally:
        check_conn.close()

    return backup_path


def main():
    parser = argparse.ArgumentParser(description="SQLite online backup (WAL-safe)")
    parser.add_argument(
        "--src", default=os.environ.get("DATABASE_URL", "sqlite:///./hub.db"),
        help="源数据库 URL（默认读 DATABASE_URL 环境变量）",
    )
    parser.add_argument(
        "--out", default="./backups",
        help="备份输出目录（默认 ./backups）",
    )
    args = parser.parse_args()
    try:
        path = backup_database(args.src, args.out)
        print(f"备份成功: {path}")
        size_mb = path.stat().st_size / (1024 * 1024)
        print(f"大小: {size_mb:.2f} MB")
        print("完整性检查: OK")
    except Exception as e:
        print(f"备份失败: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
