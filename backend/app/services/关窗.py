"""关窗：缓冲按运单物化为窗切片（双写两轨）。

- 现行轨：缓冲载荷指纹 == 现行契约指纹 → 按契约模板出片，写窗切片账。
- 归档轨：缺键 / 过期契约 / 契约混票 → 只读归档（归档键 + 拒因），现行不涨。
- 契约快照、校验与写片都在同一写事务内，同成败；改契约走同一把写锁。
"""
from __future__ import annotations
import json
import uuid
from collections import defaultdict

from app.db import 写事务
from app.seed import 此刻
from app.modules import 切片键契约 as 契约


def _窗规快照(conn) -> dict:
    return {r["键"]: r["值"] for r in conn.execute("SELECT 键,值 FROM 窗规").fetchall()}


def _唯一片键(conn, 现契: dict, 窗号: str, 运单键: str, 起始序号: int) -> tuple[str, int]:
    """按契约模板渲染片键；模板带 {序号} 时递增去撞，否则撞键追加尾号。"""
    序号 = max(1, int(起始序号))
    键 = 契约.出片键(现契, 窗号, 运单键, 序号)
    if not conn.execute("SELECT 1 FROM 窗切片账 WHERE 切片编号=?", (键,)).fetchone():
        return 键, 序号
    if "{序号}" not in (现契.get("模板") or ""):
        # 模板不含序号：片键天然按窗号+运单键稳定，撞键补尾号即可
        return f"{键}-{uuid.uuid4().hex[:6]}", 序号
    while 序号 < 起始序号 + 1000:
        键 = 契约.出片键(现契, 窗号, 运单键, 序号)
        if not conn.execute("SELECT 1 FROM 窗切片账 WHERE 切片编号=?", (键,)).fetchone():
            return 键, 序号
        序号 += 1
    return f"{契约.出片键(现契, 窗号, 运单键, 序号)}-{uuid.uuid4().hex[:6]}", 序号


def 关当前窗() -> dict:
    批 = f"关窗-{uuid.uuid4().hex[:8]}"
    时刻 = 此刻()
    现行片 = 0
    归档行 = 0
    缓冲消耗 = 0
    with 写事务() as conn:
        规 = _窗规快照(conn)
        窗号 = 规.get("当前窗号", "W-demo")
        窗起 = 规.get("窗起", "")
        窗止 = 规.get("窗止", "")
        现契 = 契约.现行契约(conn)
        票 = 契约.窗规票(窗号, 窗起, 窗止, 现契)

        rows = conn.execute(
            "SELECT * FROM 入窗缓冲 WHERE 窗号=? AND 命运='在缓冲' ORDER BY 事件时, 事件编号",
            (窗号,),
        ).fetchall()
        缓冲消耗 = len(rows)

        by_waybill: dict[str, list] = defaultdict(list)
        for r in rows:
            by_waybill[(r["运单键"] or "")].append(r)

        序号 = 0
        归档序 = 0
        for 运单, evs in by_waybill.items():
            可现行, 拒因 = 契约.判定组(evs, 现契, 窗号, 窗起, 窗止)
            if 可现行:
                序号 += 1
                切片, 序号 = _唯一片键(conn, 现契, 窗号, 运单, 序号)
                conn.execute(
                    """INSERT INTO 窗切片账
                       (切片编号,窗号,运单键,事件数,窗起,窗止,物化时,命运,
                        契约版,契约指纹,票指纹)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (切片, 窗号, 运单, len(evs), 窗起, 窗止, 时刻, "已成片",
                     现契["契约版"], 现契["指纹"], 票["票指纹"]),
                )
                for e in evs:
                    conn.execute(
                        "UPDATE 入窗缓冲 SET 命运=? WHERE 事件编号=?",
                        ("已物化", e["事件编号"]),
                    )
                    conn.execute(
                        "UPDATE 观测点 SET 命运=? WHERE 事件编号=?",
                        ("已成片", e["事件编号"]),
                    )
                现行片 += 1
            else:
                归档序 += 1
                归档键 = 契约.出归档键(窗号, 运单, 归档序)
                版, 印 = _组指纹(evs)
                conn.execute(
                    """INSERT INTO 切片归档
                       (归档键,窗号,运单键,事件数,窗起,窗止,
                        契约版,契约指纹,票指纹,拒因,归档时,事件编号)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (归档键, 窗号, 运单, len(evs), 窗起, 窗止,
                     版, 印, 票["票指纹"], 拒因, 时刻,
                     ",".join(str(e["事件编号"]) for e in evs)),
                )
                for e in evs:
                    conn.execute(
                        "UPDATE 入窗缓冲 SET 命运=? WHERE 事件编号=?",
                        ("已归档", e["事件编号"]),
                    )
                    conn.execute(
                        "UPDATE 观测点 SET 命运=? WHERE 事件编号=?",
                        ("已归档", e["事件编号"]),
                    )
                归档行 += 1

        报告 = {
            "批次编号": 批,
            "窗号": 窗号,
            "成片数": 现行片,
            "现行片": 现行片,
            "归档行": 归档行,
            "缓冲消耗": 缓冲消耗,
            "票": 票,
        }
        conn.execute(
            """INSERT INTO 关窗批次(编号,窗号,时刻,成片数,报告) VALUES (?,?,?,?,?)""",
            (批, 窗号, 时刻, 现行片, json.dumps(报告, ensure_ascii=False)),
        )
    return 报告


def _组指纹(组: list) -> tuple[int, str]:
    指纹 = sorted({(int(r["契约版"]), r["契约指纹"] or "") for r in 组})
    return 指纹[0] if 指纹 else (0, "")
