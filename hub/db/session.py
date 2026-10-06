"""Engine/session factory. SQLite runs in WAL with busy_timeout; single writer process.

写入速度（2026-10-05 甲方要求「写入速度能不能提高一下」）
--------------------------------------------------------
SQLite 的写入慢，几乎总是慢在 **每次 commit 都 fsync** 上，而不是慢在 SQL 上。
云服务器磁盘慢的时候，一次 fsync 几十毫秒，一条留言要提交两三次就是上百毫秒。
所以这里的 PRAGMA 是按「留言板是流水式留言、不是银行账本」来定的：

* ``journal_mode=WAL`` —— 读写不互斥（读侧只查库，写侧一个进程）。
* ``synchronous=NORMAL`` —— **写入提速的关键**。WAL 下 NORMAL 只在 checkpoint
  时 fsync，不再每个 commit 都 fsync。代价写清楚：突然断电/宿主机掉电，
  可能丢最近几条**已提交**的留言；进程崩溃、正常重启不丢数据。
  要换回「一条都不丢」就把这里改回 ``FULL``，写入会慢好几倍。
* ``busy_timeout=10000`` —— 后台总结线程和写请求抢锁时，等 10 秒再报
  database is locked，而不是立刻失败。
* ``temp_store=MEMORY`` / ``cache_size`` / ``mmap_size`` —— 排序和读页留在内存，
  1000 条的大板子读回时不落临时文件。
* ``wal_autocheckpoint`` —— 攒够页数再一次性 checkpoint，避免频繁写主库。
"""

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from hub.db.base import Base
from hub.db.migrations import run_migrations

# 内存页缓存（负数 = KB）：16MB，够放下 1000 条留言的板子索引与热数据。
_CACHE_SIZE_KB = -16000
# 内存映射 I/O 上限：128MB（不支持 mmap 的平台会静默忽略）。
_MMAP_BYTES = 134217728
# WAL 累积多少页后自动 checkpoint。
_WAL_AUTOCHECKPOINT_PAGES = 1000


def make_engine(database_url: str) -> Engine:
    engine = create_engine(database_url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA cache_size={_CACHE_SIZE_KB}")
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.execute(f"PRAGMA mmap_size={_MMAP_BYTES}")
        cursor.execute(f"PRAGMA wal_autocheckpoint={_WAL_AUTOCHECKPOINT_PAGES}")
        cursor.close()

    return engine


def init_db(engine: Engine) -> None:
    # Import models so they register on Base.metadata before create_all.
    import hub.db.models  # noqa: F401

    # Upgrades the *existing* schema first: create_all only ever adds missing
    # tables, it never alters a table that is already there.
    run_migrations(engine)
    Base.metadata.create_all(engine)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
