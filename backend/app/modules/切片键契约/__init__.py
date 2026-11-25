"""切片键契约（形态二 · 双写两轨）。

关窗入库管道上的键契约编排：

- 契约 = 契约版 + 切片编号模板 + 形态。声明契约时模板必须渲染出窗号与运单键。
- 装载时把“当时契约”的版本与指纹钉进缓冲行；关窗在**同一写事务**里读同一契约快照，
  校验与写片同成败。
- 现行轨：载荷指纹 == 现行契约指纹 → 按模板出片，写窗切片账。
- 归档轨：缺键 / 过期契约 / 契约混票 → 只读归档（归档键 + 拒因），现行不涨。
  两轨读引用不同：现行读「切片编号 + 契约指纹」，归档读「归档键 + 拒因」。
- 声明契约与关窗共用 db.写事务（进程内写锁 + BEGIN IMMEDIATE），过期键不得进现行。

只读接口（GET /api/切片键契约）给出：契约版、指纹、拒因或归档键、形态标记、valid。
"""
from __future__ import annotations

import hashlib
import json
import uuid

from app.db import 连接, 写事务
from app.seed import 此刻, 契约指纹

形态标记 = "双写"
策略名 = "双写两轨 · 契约指纹拒收"
缺键占位 = "缺键"


# ---------- 指纹与票 ----------

def _票指纹(窗号: str, 窗起: str, 窗止: str, 契约版: int, 指纹: str) -> str:
    体 = {
        "窗号": 窗号,
        "窗起": 窗起,
        "窗止": 窗止,
        "契约版": int(契约版),
        "契约指纹": 指纹,
    }
    raw = json.dumps(体, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _行转契约(r) -> dict:
    if r is None:
        return {"契约版": 0, "模板": "", "指纹": "", "形态": 形态标记, "valid": False, "拒因": ""}
    return {
        "契约版": int(r["契约版"]),
        "模板": r["模板"],
        "指纹": r["指纹"],
        "形态": r["形态"],
        "valid": bool(r["valid"]),
        "拒因": r["拒因"] if "拒因" in r.keys() else "",
    }


# ---------- 只读 ----------

def 现行契约(conn=None) -> dict:
    own = conn is None
    conn = conn or 连接()
    try:
        r = conn.execute(
            "SELECT * FROM 切片键契约 WHERE valid=1 ORDER BY 契约版 DESC LIMIT 1"
        ).fetchone()
        return _行转契约(r)
    finally:
        if own:
            conn.close()


def 概览() -> dict:
    契 = 现行契约()
    conn = 连接()
    try:
        最新 = conn.execute(
            "SELECT * FROM 切片键契约 ORDER BY 契约版 DESC LIMIT 1"
        ).fetchone()
        拒因 = ""
        if 最新 is not None and int(最新["valid"]) == 0:
            拒因 = 最新["拒因"] or ""
        最新拒印 = (最新["指纹"] or "") if 最新 is not None and int(最新["valid"]) == 0 else ""
        归 = conn.execute(
            "SELECT 归档键 FROM 切片归档 ORDER BY 归档时 DESC, rowid DESC LIMIT 1"
        ).fetchone()
        现行片数 = conn.execute("SELECT COUNT(*) AS c FROM 窗切片账").fetchone()["c"]
        归档行数 = conn.execute("SELECT COUNT(*) AS c FROM 切片归档").fetchone()["c"]
    finally:
        conn.close()
    return {
        "策略名": 策略名,
        "契约版": 契["契约版"],
        "指纹": 契["指纹"],
        "模板": 契["模板"],
        "形态标记": 形态标记,
        "valid": 契["valid"] and 契["契约版"] > 0,
        "拒因": 拒因,
        "拒指纹": 最新拒印,
        "归档键": 归["归档键"] if 归 else "",
        "现行片数": 现行片数,
        "归档行数": 归档行数,
    }


def 归档列表() -> list[dict]:
    conn = 连接()
    try:
        rows = conn.execute(
            "SELECT * FROM 切片归档 ORDER BY 归档时 DESC, rowid DESC LIMIT 200"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------- 声明 ----------

def _渲染(模板: str, 窗号: str, 运单键: str, 契约版: int, 序号: int) -> str:
    return 模板.format(窗号=窗号, 运单键=运单键, 契约版=int(契约版), 序号=int(序号))


def 声明契约(体: dict) -> dict:
    模板 = str((体 or {}).get("模板") or "").strip()
    拒因 = ""
    样例 = ""
    if not 模板:
        拒因 = "缺键:模板为空"
    else:
        try:
            样例 = _渲染(模板, "W-探", "运单-探", 1, 1)
        except Exception as e:  # noqa: BLE001 - 模板占位非法统一拒
            拒因 = f"模板占位非法:{e}"
        else:
            if "W-探" not in 样例:
                拒因 = "缺键:模板不含窗号"
            elif "运单-探" not in 样例:
                拒因 = "缺键:模板不含运单键"
    with 写事务() as conn:
        现 = 现行契约(conn)
        下一版 = int(
            conn.execute("SELECT COALESCE(MAX(契约版),0) AS m FROM 切片键契约").fetchone()["m"]
        ) + 1
        if 拒因:
            拒印 = 契约指纹(下一版, 模板, 形态标记) if 模板 else ""
            conn.execute(
                """INSERT INTO 切片键契约(契约版,模板,指纹,形态,valid,声明时,拒因)
                   VALUES (?,?,?,?,?,?,?)""",
                (下一版, 模板, 拒印, 形态标记, 0, 此刻(), 拒因),
            )
            return {
                "受理": False,
                "valid": False,
                "契约版": 现["契约版"],
                "指纹": 现["指纹"],
                "拒指纹": 拒印,
                "形态标记": 形态标记,
                "拒因": 拒因,
                "模板": 模板,
            }
        印 = 契约指纹(下一版, 模板, 形态标记)
        conn.execute(
            """INSERT INTO 切片键契约(契约版,模板,指纹,形态,valid,声明时,拒因)
               VALUES (?,?,?,?,?,?,?)""",
            (下一版, 模板, 印, 形态标记, 1, 此刻(), ""),
        )
        return {
            "受理": True,
            "valid": True,
            "契约版": 下一版,
            "指纹": 印,
            "形态标记": 形态标记,
            "拒因": "",
            "模板": 模板,
        }


# ---------- 关窗判定（现契快照由调用方在同一事务内取得） ----------

def 判定组(组: list, 现契: dict, 窗号: str, 窗起: str = "", 窗止: str = "") -> tuple[bool, str]:
    """判定一组同运单缓冲能否进现行轨；返回 (可现行, 拒因)。"""
    if not 组:
        return False, "缺键:空组"
    运单键 = (组[0]["运单键"] or "").strip()
    if not 运单键:
        return False, "缺键:运单键"
    for r in 组:
        if (r["运单键"] or "").strip() != 运单键:
            return False, "缺键:运单键不一致"
        if (r["窗号"] or "") != 窗号:
            return False, "缺键:跨窗混票"
        # 装载时钉住的窗起止若与本次关窗的窗规不同票，说明窗规改版，按过期处理
        row_keys = r.keys()
        r起 = (r["窗起"] or "") if "窗起" in row_keys else ""
        r止 = (r["窗止"] or "") if "窗止" in row_keys else ""
        if 窗起 and 窗止 and r起 and r止 and (r起 != 窗起 or r止 != 窗止):
            return False, "窗规不同票"
    指纹 = {(int(r["契约版"]), r["契约指纹"] or "") for r in 组}
    if len(指纹) > 1:
        return False, "契约混票"
    版, 印 = next(iter(指纹))
    if not 印:
        return False, "缺键:契约指纹"
    if 版 != 现契["契约版"] or 印 != 现契["指纹"]:
        return False, "过期契约"
    try:
        键 = _渲染(现契["模板"], 窗号, 运单键, 版, 1)
    except Exception as e:  # noqa: BLE001
        return False, f"缺键:模板占位非法:{e}"
    if 窗号 not in 键:
        return False, "缺键:切片编号不含窗号"
    if 运单键 not in 键:
        return False, "缺键:切片编号不含运单键"
    return True, ""


def 出片键(现契: dict, 窗号: str, 运单键: str, 序号: int) -> str:
    return _渲染(现契["模板"], 窗号, 运单键, 现契["契约版"], 序号)


def 出归档键(窗号: str, 运单键: str, 序号: int) -> str:
    键部 = (运单键 or "").strip() or 缺键占位
    return f"归-{窗号}-{键部}-{序号}-{uuid.uuid4().hex[:6]}"


def 窗规票(窗号: str, 窗起: str, 窗止: str, 契: dict) -> dict:
    return {
        "窗号": 窗号,
        "窗起": 窗起,
        "窗止": 窗止,
        "契约版": int(契["契约版"]),
        "契约指纹": 契["指纹"],
        "票指纹": _票指纹(窗号, 窗起, 窗止, 契["契约版"], 契["指纹"]),
    }


def 票指纹口径(窗号: str, 窗起: str, 窗止: str, 契约版: int, 指纹: str) -> str:
    return _票指纹(窗号, 窗起, 窗止, 契约版, 指纹)


# ---------- 贯验协同：切片键/票/窗规 ----------

def 校验切片行(行: dict, 当前窗号: str = "", 当前窗起: str = "", 当前窗止: str = "") -> list[str]:
    """切片自证同票：编号含窗号+运单键，票指纹可由自身字段回算。

    不复核“当前窗规”：历史切片是既成事实，窗规改版不应把旧片翻成撕裂；
    窗规不同票在关窗当时由 判定组 拦截并整票归档。
    """
    错: list[str] = []
    键 = str(行.get("切片编号") or "")
    窗号 = str(行.get("窗号") or "")
    运单键 = str(行.get("运单键") or "")
    if not 窗号 or not 运单键:
        错.append(f"切片缺键:{键 or '?'}")
    if 窗号 and 窗号 not in 键:
        错.append(f"切片编号不含窗号:{键}")
    if 运单键 and 运单键 not in 键:
        错.append(f"切片编号不含运单键:{键}")
    if not (行.get("契约指纹") or ""):
        错.append(f"切片缺契约指纹:{键}")
    票 = 行.get("票指纹") or ""
    if not 票:
        错.append(f"切片缺票指纹:{键}")
    else:
        算 = _票指纹(窗号, str(行.get("窗起") or ""), str(行.get("窗止") or ""),
                      int(行.get("契约版") or 0), str(行.get("契约指纹") or ""))
        if 算 != 票:
            错.append(f"切片票指纹不符:{键}")
    return 错


def 校验归档行(行: dict) -> list[str]:
    错: list[str] = []
    键 = str(行.get("归档键") or "")
    窗号 = str(行.get("窗号") or "")
    运单键 = str(行.get("运单键") or "")
    if not 键:
        错.append("归档缺归档键")
    if 窗号 and 窗号 not in 键:
        错.append(f"归档键不含窗号:{键}")
    if 运单键 and 运单键 not in 键:
        错.append(f"归档键不含运单键:{键}")
    if not (行.get("拒因") or ""):
        错.append(f"归档缺拒因:{键}")
    if not (行.get("契约指纹") or ""):
        错.append(f"归档缺契约指纹:{键}")
    return 错
