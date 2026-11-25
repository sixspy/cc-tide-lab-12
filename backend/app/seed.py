from datetime import datetime, timezone
import hashlib
from app.db import 连接

建表 = """
CREATE TABLE IF NOT EXISTS 窗规 (
  键 TEXT PRIMARY KEY,
  值 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS 网点源 (
  源键 TEXT PRIMARY KEY,
  名称 TEXT NOT NULL,
  区域 TEXT NOT NULL DEFAULT '',
  说明 TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS 观测点 (
  事件编号 TEXT PRIMARY KEY,
  运单键 TEXT NOT NULL,
  网点键 TEXT NOT NULL,
  事件时 TEXT NOT NULL,
  处理时 TEXT NOT NULL,
  命运 TEXT NOT NULL DEFAULT '已装载',
  批次编号 TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS 入窗缓冲 (
  事件编号 TEXT PRIMARY KEY,
  运单键 TEXT NOT NULL,
  网点键 TEXT NOT NULL,
  事件时 TEXT NOT NULL,
  窗号 TEXT NOT NULL,
  命运 TEXT NOT NULL DEFAULT '在缓冲',
  入缓冲时 TEXT NOT NULL,
  契约版 INTEGER NOT NULL DEFAULT 0,
  契约指纹 TEXT NOT NULL DEFAULT '',
  窗起 TEXT NOT NULL DEFAULT '',
  窗止 TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS 窗切片账 (
  切片编号 TEXT PRIMARY KEY,
  窗号 TEXT NOT NULL,
  运单键 TEXT NOT NULL,
  事件数 INTEGER NOT NULL,
  窗起 TEXT NOT NULL,
  窗止 TEXT NOT NULL,
  物化时 TEXT NOT NULL,
  命运 TEXT NOT NULL DEFAULT '已成片',
  契约版 INTEGER NOT NULL DEFAULT 0,
  契约指纹 TEXT NOT NULL DEFAULT '',
  票指纹 TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS 切片键契约 (
  契约版 INTEGER PRIMARY KEY,
  模板 TEXT NOT NULL,
  指纹 TEXT NOT NULL,
  形态 TEXT NOT NULL DEFAULT '双写',
  valid INTEGER NOT NULL DEFAULT 1,
  声明时 TEXT NOT NULL,
  拒因 TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS 切片归档 (
  归档键 TEXT PRIMARY KEY,
  窗号 TEXT NOT NULL,
  运单键 TEXT NOT NULL,
  事件数 INTEGER NOT NULL,
  窗起 TEXT NOT NULL,
  窗止 TEXT NOT NULL,
  契约版 INTEGER NOT NULL,
  契约指纹 TEXT NOT NULL,
  票指纹 TEXT NOT NULL,
  拒因 TEXT NOT NULL,
  归档时 TEXT NOT NULL,
  事件编号 TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS 迟到匣 (
  编号 INTEGER PRIMARY KEY AUTOINCREMENT,
  事件编号 TEXT NOT NULL,
  运单键 TEXT NOT NULL,
  原因 TEXT NOT NULL,
  事件时 TEXT NOT NULL,
  入匣时 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS 窗贯验 (
  编号 INTEGER PRIMARY KEY AUTOINCREMENT,
  时刻 TEXT NOT NULL,
  缓冲笔数 INTEGER NOT NULL,
  切片笔数 INTEGER NOT NULL,
  迟到笔数 INTEGER NOT NULL,
  是否对齐 INTEGER NOT NULL,
  说明 TEXT NOT NULL,
  归档笔数 INTEGER NOT NULL DEFAULT 0,
  键检查 TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS 装载批次 (
  编号 TEXT PRIMARY KEY,
  时刻 TEXT NOT NULL,
  入流 INTEGER NOT NULL DEFAULT 0,
  入缓冲 INTEGER NOT NULL DEFAULT 0,
  迟到 INTEGER NOT NULL DEFAULT 0,
  报告 TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS 关窗批次 (
  编号 TEXT PRIMARY KEY,
  窗号 TEXT NOT NULL,
  时刻 TEXT NOT NULL,
  成片数 INTEGER NOT NULL DEFAULT 0,
  报告 TEXT NOT NULL DEFAULT '{}'
);
"""


def 此刻() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def 契约指纹(契约版: int, 模板: str, 形态: str) -> str:
    """契约指纹只由（版本, 模板, 形态）决定，可回读比对。"""
    raw = f"{int(契约版)}|{形态}|{模板}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


默认契约模板 = "片-{窗号}-{运单键}-{契约版}-{序号}"


def _迁移列(conn) -> None:
    """老库补列：CREATE TABLE IF NOT EXISTS 不会给已有表加列。"""
    补 = {
        "入窗缓冲": [
            ("契约版", "INTEGER NOT NULL DEFAULT 0"),
            ("契约指纹", "TEXT NOT NULL DEFAULT ''"),
            ("窗起", "TEXT NOT NULL DEFAULT ''"),
            ("窗止", "TEXT NOT NULL DEFAULT ''"),
        ],
        "窗切片账": [
            ("契约版", "INTEGER NOT NULL DEFAULT 0"),
            ("契约指纹", "TEXT NOT NULL DEFAULT ''"),
            ("票指纹", "TEXT NOT NULL DEFAULT ''"),
        ],
        "窗贯验": [
            ("归档笔数", "INTEGER NOT NULL DEFAULT 0"),
            ("键检查", "TEXT NOT NULL DEFAULT ''"),
        ],
    }
    for 表, 列们 in 补.items():
        已有 = {r["name"] for r in conn.execute(f"PRAGMA table_info({表})").fetchall()}
        for 列, 声明 in 列们:
            if 列 not in 已有:
                conn.execute(f"ALTER TABLE {表} ADD COLUMN {列} {声明}")


def _回填旧载荷(conn) -> None:
    """老库升级：无契约列的旧切片/旧缓冲按基线 v1 契约回填，避免误判撕裂。"""
    from app.modules import 切片键契约 as 契

    印 = 契约指纹(1, 默认契约模板, "双写")
    for r in conn.execute(
        "SELECT 切片编号,窗号,窗起,窗止 FROM 窗切片账 WHERE 契约指纹=''"
    ).fetchall():
        票 = 契.票指纹口径(r["窗号"], r["窗起"], r["窗止"], 1, 印)
        conn.execute(
            "UPDATE 窗切片账 SET 契约版=1,契约指纹=?,票指纹=? WHERE 切片编号=?",
            (印, 票, r["切片编号"]),
        )
    conn.execute(
        "UPDATE 入窗缓冲 SET 契约版=1,契约指纹=? WHERE 契约指纹=''", (印,)
    )
    # 旧缓冲没有窗起止：留空表示“未声明”，不参与窗规同票判定
    conn.execute(
        "UPDATE 入窗缓冲 SET 窗起='',窗止='' WHERE 窗起 IS NULL OR 窗止 IS NULL"
    )


def 初始化库() -> None:
    conn = 连接()
    conn.executescript(建表)
    _迁移列(conn)
    if conn.execute("SELECT COUNT(*) AS c FROM 窗规").fetchone()["c"] == 0:
        for k, v in {
            "窗长秒": "3600",
            "允许迟到秒": "0",
            "关窗策略": "按事件时整点",
            "当前窗号": "W-demo",
            "窗起": "2026-11-24T00:00:00Z",
            "窗止": "2026-11-24T01:00:00Z",
        }.items():
            conn.execute("INSERT INTO 窗规(键,值) VALUES (?,?)", (k, v))
    if conn.execute("SELECT COUNT(*) AS c FROM 网点源").fetchone()["c"] == 0:
        for row in [
            ("网点-东站", "东站分拨", "华东", "示范网点"),
            ("网点-西港", "西港分拨", "华东", "示范网点"),
            ("网点-北仓", "北仓分拨", "华北", "示范网点"),
        ]:
            conn.execute(
                "INSERT INTO 网点源(源键,名称,区域,说明) VALUES (?,?,?,?)", row
            )
    if conn.execute("SELECT COUNT(*) AS c FROM 切片键契约").fetchone()["c"] == 0:
        印 = 契约指纹(1, 默认契约模板, "双写")
        conn.execute(
            """INSERT INTO 切片键契约(契约版,模板,指纹,形态,valid,声明时,拒因)
               VALUES (?,?,?,?,?,?,?)""",
            (1, 默认契约模板, 印, "双写", 1, 此刻(), ""),
        )
    _回填旧载荷(conn)
    conn.commit()
    conn.close()
