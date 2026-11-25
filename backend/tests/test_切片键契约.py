"""切片键契约（形态二 · 双写两轨）红测。

约定：
- 现行切片只读 切片编号 + 契约指纹；归档旁路只读 归档键 + 拒因，两轨读引用不同。
- 切片编号必须由当前契约模板渲染，含窗号 + 运单键。
- 装载时记录契约指纹；关窗时与现行指纹不一致即过期，整票进只读归档，现行不涨。
- 声明契约与关窗同一把写锁 + BEGIN IMMEDIATE 串行，改契约并发关窗不得写出两套键。

夹具（本文件 + fixtures/）：
- 新契约成片
- 缺键或过期契约：归档有行、现行不涨、指纹可回读
- 改契约与关窗并发：过期键不得进现行
"""
from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

import pytest

_临时 = tempfile.mkdtemp()
os.environ.setdefault("数据目录", _临时)
os.environ.setdefault("夹具目录", str(Path(__file__).resolve().parents[2] / "fixtures"))

from app import seed  # noqa: E402
from app.db import 连接  # noqa: E402
from app.modules import 切片键契约 as 契约  # noqa: E402
from app.services import 目录, 装载, 关窗, 贯验, 夹具  # noqa: E402

清空表 = (
    "观测点",
    "入窗缓冲",
    "窗切片账",
    "切片归档",
    "迟到匣",
    "窗贯验",
    "装载批次",
    "关窗批次",
    "切片键契约",
    "窗规",
)


def _清库() -> None:
    conn = 连接()
    try:
        for 表 in 清空表:
            conn.execute(f"DELETE FROM {表}")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _净库():
    """每个用例前后都回到只有基线默认契约的干净库，避免串污其他测试模块。"""
    seed.初始化库()  # 建表先于清表：同库共享时别的模块可能先导入
    _清库()
    seed.初始化库()
    yield
    _清库()
    seed.初始化库()


def _灌(前缀: str, *运单: str, 时: str = "2026-11-24T00:10:00Z") -> dict:
    运单 = 运单 or ("运单-A", "运单-B")
    事件 = [
        {
            "事件编号": f"{前缀}-{i}",
            "运单键": w,
            "网点键": "网点-东站",
            "事件时": 时,
        }
        for i, w in enumerate(运单, start=1)
    ]
    return 装载.装一批({"批次编号": f"批-{前缀}", "事件": 事件})


def _直插缓冲(事件编号: str, 运单键: str, 窗号: str = "W-demo", 指纹: str = "") -> None:
    """绕过装载校验，模拟缺键/脏载荷进缓冲。"""
    conn = 连接()
    try:
        conn.execute(
            """INSERT INTO 入窗缓冲
               (事件编号,运单键,网点键,事件时,窗号,命运,入缓冲时,契约版,契约指纹)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (事件编号, 运单键, "网点-东站", "2026-11-24T00:11:00Z", 窗号,
             "在缓冲", "2026-11-24T00:12:00Z", 1, 指纹 or 契约.现行契约()["指纹"]),
        )
        conn.commit()
    finally:
        conn.close()


def test_新契约成片():
    签 = 契约.声明契约({"模板": "片-{窗号}-{运单键}-N{契约版}-{序号}"})
    assert 签["受理"] is True and 签["valid"] is True
    现 = 契约.现行契约()
    assert 签["契约版"] == 现["契约版"]
    assert 签["指纹"] == 现["指纹"]
    assert 契约.概览()["形态标记"] == "双写"

    _灌("新", "运单-A", "运单-B")
    报 = 关窗.关当前窗()
    assert 报["现行片"] == 2
    assert 报["归档行"] == 0
    片 = 目录.切片列表()
    assert len(片) == 2
    for r in 片:
        assert r["窗号"] in r["切片编号"]
        assert r["运单键"] in r["切片编号"]
        assert f"N{现['契约版']}" in r["切片编号"]
        assert r["契约指纹"] == 现["指纹"]
        assert r["票指纹"] == 报["票"]["票指纹"]
        # 切片键与窗规声明同票
        assert 报["票"]["窗号"] == r["窗号"]
    assert 契约.归档列表() == []
    验 = 贯验.跑一次()
    assert 验["是否对齐"] is True
    assert 验["键检查"] == []


def test_过期契约整票归档现行不涨():
    旧 = 契约.现行契约()
    _灌("过", "运单-A", "运单-B")
    契约.声明契约({"模板": "片-{窗号}-{运单键}-X{契约版}-{序号}"})
    报 = 关窗.关当前窗()

    assert 报["现行片"] == 0
    assert 报["归档行"] == 2
    assert 目录.切片列表() == []
    归 = 契约.归档列表()
    assert len(归) == 2
    for r in 归:
        assert r["归档键"]
        assert r["拒因"] == "过期契约"
        assert r["契约指纹"] == 旧["指纹"]
    览 = 契约.概览()
    assert 览["现行片数"] == 0
    assert 览["归档行数"] == 2
    assert 览["归档键"]
    验 = 贯验.跑一次()
    assert 验["是否对齐"] is True  # 归档事件也算物化去向
    assert 验["归档笔数"] == 2


def test_缺键模板拒声明():
    旧 = 契约.现行契约()
    签 = 契约.声明契约({"模板": "片-{窗号}-{序号}"})
    assert 签["受理"] is False
    assert 签["valid"] is False
    assert "运单键" in 签["拒因"]
    assert 签["拒指纹"]  # 拒收也要可回读指纹
    现 = 契约.现行契约()
    assert 现["契约版"] == 旧["契约版"]
    assert 现["指纹"] == 旧["指纹"]
    览 = 契约.概览()
    assert 览["valid"] is True and 览["契约版"] == 旧["契约版"]
    assert 览["拒因"] == 签["拒因"] and 览["拒指纹"] == 签["拒指纹"]


def test_窗规改版同票判定():
    """装载后改窗起止：旧缓冲窗规不同票，不得进现行。"""
    _灌("规", "运单-规")
    目录.改窗规({"窗起": "2026-11-24T00:05:00Z", "窗止": "2026-11-24T01:05:00Z"})
    报 = 关窗.关当前窗()
    assert 报["现行片"] == 0 and 报["归档行"] == 1
    assert 契约.归档列表()[0]["拒因"] == "窗规不同票"
    assert 贯验.跑一次()["是否对齐"] is True


def test_窗规改版不翻旧片():
    """已落账切片是既成事实：窗规改版后贯验仍应按自身票指纹判齐。"""
    _灌("旧", "运单-旧")
    assert 关窗.关当前窗()["现行片"] == 1
    目录.改窗规({"窗起": "2026-11-24T00:10:00Z", "窗止": "2026-11-24T01:10:00Z"})
    验 = 贯验.跑一次()
    assert 验["是否对齐"] is True
    assert 验["键检查"] == []


def test_缺键载荷归档():
    _直插缓冲("脏-1", "运单-脏")
    conn = 连接()
    try:
        conn.execute("UPDATE 入窗缓冲 SET 运单键='' WHERE 事件编号='脏-1'")
        conn.commit()
    finally:
        conn.close()
    报 = 关窗.关当前窗()
    assert 报["现行片"] == 0
    assert 报["归档行"] == 1
    assert 目录.切片列表() == []
    归 = 契约.归档列表()
    assert len(归) == 1
    assert "缺键" in 归[0]["拒因"]
    assert 归[0]["契约指纹"] == 契约.现行契约()["指纹"]


def test_夹具_新契约成片():
    体 = 夹具.读取("切片键新契约样例")
    契约.声明契约(体["契约声明"])
    报 = 装载.装一批({"批次编号": "夹具-键-新", "事件": 体["事件"]})
    assert 报["入缓冲"] == 2
    关 = 关窗.关当前窗()
    assert 关["现行片"] == 2
    assert 关["归档行"] == 0
    assert all("运单键" not in r["切片编号"] for r in 目录.切片列表())
    assert 贯验.跑一次()["是否对齐"] is True


def test_夹具_过期契约归档():
    体 = 夹具.读取("切片键过期样例")
    契约.声明契约(体["先声明"])
    装载.装一批({"批次编号": "夹具-键-过期", "事件": 体["事件"]})
    油 = 契约.现行契约()["指纹"]
    契约.声明契约(体["后声明"])
    关 = 关窗.关当前窗()
    assert 关["现行片"] == 0
    assert 关["归档行"] == 2
    归 = 契约.归档列表()
    assert all(r["契约指纹"] == 油 for r in 归)
    assert all(r["拒因"] == "过期契约" for r in 归)
    assert 贯验.跑一次()["是否对齐"] is True


def test_改契约与关窗并发不得双成功():
    _灌("并", "运单-A", "运单-B")
    门 = threading.Barrier(2)
    结果: dict[str, object] = {}

    def 关上():
        门.wait()
        结果["关"] = 关窗.关当前窗()

    def 改上():
        门.wait()
        结果["改"] = 契约.声明契约({"模板": "片-{窗号}-{运单键}-P{契约版}-{序号}"})

    t1 = threading.Thread(target=关上)
    t2 = threading.Thread(target=改上)
    t1.start(); t2.start()
    t1.join(30); t2.join(30)
    assert not t1.is_alive() and not t2.is_alive()
    assert 结果["关"]["现行片"] + 结果["关"]["归档行"] == 2

    片 = 目录.切片列表()
    归 = 契约.归档列表()
    assert not (片 and 归)  # 不得两轨双成功
    assert sum(r["事件数"] for r in 片) + sum(r["事件数"] for r in 归) == 2
    现 = 契约.现行契约()
    if 片:
        # 关窗先赢：现行片落在旧契约上，随后契约才改
        assert all(r["契约指纹"] != 现["指纹"] for r in 片)
        assert all(r["契约版"] == 1 for r in 片)
    else:
        # 改契约先赢：旧载荷整票归档，现行不涨
        assert 归
        assert all(r["契约指纹"] != 现["指纹"] for r in 归)
        assert all(r["契约版"] == 1 for r in 归)
        assert all(r["拒因"] == "过期契约" for r in 归)
    # 现行轨不得出现过期指纹的片
    assert all(r["契约指纹"] != "" for r in 片)


def test_契约只读接口与中文页面():
    from starlette.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        r = c.get("/api/切片键契约")
        assert r.status_code == 200
        d = r.json()
        for k in ("契约版", "指纹", "拒因", "归档键", "形态标记", "valid"):
            assert k in d
        assert d["形态标记"] == "双写"
        assert d["valid"] is True
        页 = c.get("/窗切片账.html")
        assert 页.status_code == 200
        assert "归档" in 页.text and "契约" in 页.text
        验页 = c.get("/窗贯验.html")
        assert "键检查" in 验页.text
