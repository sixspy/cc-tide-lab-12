import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

数据目录 = Path(os.environ.get("数据目录", Path(__file__).resolve().parent.parent / "data"))
夹具目录 = Path(os.environ.get("夹具目录", Path(__file__).resolve().parent.parent.parent / "fixtures"))
库路径 = 数据目录 / "潮切.db"

# 进程内写锁：改契约与关窗必须串行，避免并发写出两套键。
# 跨进程再由 BEGIN IMMEDIATE 兜底（SQLite 预留写锁）。
写锁 = threading.RLock()


def 连接() -> sqlite3.Connection:
    数据目录.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(库路径, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


@contextmanager
def 写事务():
    """串行写事务：进程内写锁 + BEGIN IMMEDIATE。

    校验与写片必须在同一事务、同一契约快照下完成，成功同提交、失败同回滚。
    """
    with 写锁:
        conn = 连接()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
