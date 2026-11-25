"""窗贯验：缓冲/切片/归档/迟到互证，外加切片键契约键检查。"""
from __future__ import annotations

import json

from app.db import 连接
from app.seed import 此刻
from app.modules import 切片键契约 as 契约


def 跑一次() -> dict:
    conn = 连接()
    try:
        缓冲 = conn.execute(
            "SELECT COUNT(*) AS c FROM 入窗缓冲 WHERE 命运='在缓冲'"
        ).fetchone()["c"]
        已物化缓冲 = conn.execute(
            "SELECT COUNT(*) AS c FROM 入窗缓冲 WHERE 命运='已物化'"
        ).fetchone()["c"]
        已归档缓冲 = conn.execute(
            "SELECT COUNT(*) AS c FROM 入窗缓冲 WHERE 命运='已归档'"
        ).fetchone()["c"]
        切片行 = conn.execute("SELECT * FROM 窗切片账").fetchall()
        切片事件 = sum(int(r["事件数"]) for r in 切片行)
        归档行 = conn.execute("SELECT * FROM 切片归档").fetchall()
        归档事件 = sum(int(r["事件数"]) for r in 归档行)
        迟到 = conn.execute("SELECT COUNT(*) AS c FROM 迟到匣").fetchone()["c"]

        键错: list[str] = []
        for r in 切片行:
            键错.extend(契约.校验切片行(dict(r)))
        for r in 归档行:
            键错.extend(契约.校验归档行(dict(r)))

        # 口径：已物化缓冲事件数 = 切片事件；已归档缓冲事件数 = 归档事件。
        去向对 = (int(已物化缓冲) == int(切片事件)) and (int(已归档缓冲) == int(归档事件))
        对齐 = 1 if (去向对 and not 键错) else 0
        if not 去向对:
            说明 = f"撕裂：已物化缓冲={已物化缓冲} 切片事件={切片事件} 已归档缓冲={已归档缓冲} 归档事件={归档事件}"
        elif 键错:
            说明 = f"键契约撕裂：{len(键错)} 条（{键错[0]}）"
        else:
            说明 = "缓冲物化/归档与切片事件数一致，切片键同票"
        时刻 = 此刻()
        conn.execute(
            """INSERT INTO 窗贯验
               (时刻,缓冲笔数,切片笔数,迟到笔数,是否对齐,说明,归档笔数,键检查)
               VALUES (?,?,?,?,?,?,?,?)""",
            (时刻, 缓冲, int(切片事件), 迟到, 对齐, 说明, int(归档事件),
             json.dumps(键错[:20], ensure_ascii=False)),
        )
        conn.commit()
        return {
            "时刻": 时刻,
            "在缓冲": 缓冲,
            "已物化缓冲": int(已物化缓冲),
            "已归档缓冲": int(已归档缓冲),
            "切片事件": int(切片事件),
            "归档笔数": int(归档事件),
            "迟到": 迟到,
            "是否对齐": bool(对齐),
            "说明": 说明,
            "键检查": 键错[:20],
        }
    finally:
        conn.close()
