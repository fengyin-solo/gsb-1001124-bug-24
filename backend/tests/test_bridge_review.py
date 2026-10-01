"""桥梁档案审定状态机与版本快照锁的验收测试。

覆盖：顺序推进与越步拒绝、结论三处同事务回写、乐观并发只接受一个版本、
批量快照锁与断点续传、桥型/荷载冲突以最新批准记录裁决且历史等级留快照、
证据链在档案明细与定检两处口径一致。
"""
from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.services.bridge_review import (  # noqa: E402
    DRAWING_TABLE,
    TODO_TABLE,
    VERSION_TABLE,
    review_service,
)
from app.store import store  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)


def setup_function() -> None:
    """每个用例回到干净种子 + v1 已发布基线。"""
    store.reset()
    review_service.ensure_baselines()


def get_archive(archive_id: int) -> dict:
    res = client.get(f"/api/bridge_info/{archive_id}")
    assert res.status_code == 200, res.text
    return res.json()


# ---------------------------------------------------------------------------
# 1. 状态机：待审定 → 已审定 → 已发布，只能顺序推进
# ---------------------------------------------------------------------------
def test_state_machine_sequential_and_skip_rejected():
    # 种子档案基线为“已发布”，修订送审开出 v2 待审定
    res = client.post("/api/bridge_info/1/review", json={"values": {"桥型结构": "预应力混凝土连续梁"}})
    assert res.status_code == 200, res.text
    assert res.json()["entry"]["审定状态"] == "待审定"

    # 越过审定直接发布：服务端拒绝
    res = client.post("/api/bridge_info/1/publish", json={"values": {}})
    assert res.status_code == 409
    assert "越过审定" in res.json()["detail"]["message"]

    # 已审定后重复送审：拒绝
    res = client.post("/api/bridge_info/1/approve", json={"values": {"结论": "通过", "审定人": "张工", "上次评定等级": "2类"}})
    assert res.status_code == 200
    res = client.post("/api/bridge_info/1/review", json={"values": {}})
    assert res.status_code == 409

    # 顺序发布成功
    res = client.post("/api/bridge_info/1/publish", json={"values": {}})
    assert res.status_code == 200
    assert res.json()["entry"]["审定状态"] == "已发布"

    # 重复发布拒绝
    res = client.post("/api/bridge_info/1/publish", json={"values": {}})
    assert res.status_code == 409


# ---------------------------------------------------------------------------
# 2. 审定结论同事务回写图面、定检清单、工程待办
# ---------------------------------------------------------------------------
def test_approval_writeback_same_transaction():
    client.post("/api/bridge_info/1/review", json={"values": {"桥型结构": "钢箱梁", "设计荷载": "公路-I级", "上次评定等级": "3类"}})
    res = client.post("/api/bridge_info/1/approve", json={"values": {
        "结论": "不通过", "异常": True, "意见": "承载能力不足，需限载", "审定人": "张工",
    }})
    assert res.status_code == 200
    entry = res.json()["entry"]
    chain_id = entry["证据链id"]
    assert chain_id == "EC-BRID-0001-v2"

    # 已审定但未发布：三处为“待发布”，证据链一致
    ev = client.get("/api/bridge_info/1/evidence").json()
    assert ev["审定状态"] == "已审定"
    assert ev["图面"]["发布状态"] == "待发布"
    assert ev["图面"]["证据链id"] == chain_id
    assert ev["工程待办"]["状态"] == "待办"
    assert ev["工程待办"]["发布状态"] == "待发布"
    assert ev["图面"]["桥型结构"] == "钢箱梁"
    # 定检记录（种子 BRID-0001 同桥梁编号）沿同一链回写
    assert ev["定检记录"], "种子定检记录应被关联"
    for row in ev["定检记录"]:
        assert row["证据链id"] == chain_id
        assert row["发布状态"] == "待发布"
        assert row["异常"] is True
    assert ev["一致"] is True

    # 发布后三处同步翻牌，档案等级取本版本快照
    client.post("/api/bridge_info/1/publish", json={"values": {}})
    ev = client.get("/api/bridge_info/1/evidence").json()
    assert ev["一致"] is True
    assert ev["发布状态"] == "已发布"
    assert ev["图面"]["发布状态"] == "已发布"
    assert ev["工程待办"]["发布状态"] == "已发布"
    archive = get_archive(1)
    assert archive["上次评定等级"] == "3类"
    assert archive["桥型结构"] == "钢箱梁"


def test_writeback_atomic_rollback():
    """模拟回写阶段抛错：事务整体回滚，档案版本与批准记录都不前进。"""
    from app.services import bridge_review

    client.post("/api/bridge_info/1/review", json={"values": {}})
    original = bridge_review.BridgeReviewService._writeback

    def boom(self, entry, version, approval, publish_state):
        raise RuntimeError("模拟图面回写失败")

    bridge_review.BridgeReviewService._writeback = boom
    try:
        res = client.post("/api/bridge_info/1/approve", json={"values": {"审定人": "张工"}})
        assert res.status_code == 500
    finally:
        bridge_review.BridgeReviewService._writeback = original

    # 整事务回滚：仍是待审定 v2，且没有 v2 的批准记录
    assert get_archive(1)["审定状态"] == "待审定"
    approvals = [a for a in store.rows("bridge_info_approvals") if a["档案id"] == 1]
    assert all(a["版本号"] == 1 for a in approvals)


# ---------------------------------------------------------------------------
# 3. 并发审定：乐观版本只接受一个，重复批准以最新记录裁决，旧快照保留
# ---------------------------------------------------------------------------
def test_concurrent_optimistic_version_and_latest_approval_wins():
    # 页面基于陈旧 v1 送审 v2 时传 expected_version 不匹配 → 冲突拒绝
    client.post("/api/bridge_info/1/review", json={"values": {}})
    res = client.post("/api/bridge_info/1/approve", json={"values": {"expected_version": 99, "审定人": "甲"}})
    assert res.status_code == 409
    assert "并发冲突" in res.json()["detail"]["message"]

    # 甲先批准 v2
    client.post("/api/bridge_info/1/approve", json={"values": {
        "桥型结构": "甲方案-钢箱梁", "设计荷载": "公路-I级", "审定人": "甲",
    }})
    # 乙对同一版本重复批准：只接受一个版本，第二个被拒，以最新批准记录（甲）为准
    res = client.post("/api/bridge_info/1/approve", json={"values": {
        "桥型结构": "乙方案-混凝土梁", "设计荷载": "公路-II级", "审定人": "乙",
    }})
    assert res.status_code == 409

    # 发布后再送 v3：此时历史 v2 已发布（最新批准记录是甲）。
    # v3 若由乙给出不同桥型/荷载，裁决以最新批准记录为准，v2 旧快照的等级仍保留。
    client.post("/api/bridge_info/1/publish", json={"values": {}})
    client.post("/api/bridge_info/1/review", json={"values": {
        "桥型结构": "乙方案-混凝土梁", "设计荷载": "公路-II级", "上次评定等级": "4类",
    }})
    # 乙审定 v3：该批准即最新批准记录，没有更新的裁决者
    res = client.post("/api/bridge_info/1/approve", json={"values": {
        "桥型结构": "乙方案-混凝土梁", "设计荷载": "公路-II级", "上次评定等级": "4类", "审定人": "乙",
    }})
    assert res.status_code == 200
    archive = get_archive(1)
    assert archive["裁决信息"]["被裁决"] is False  # 自身就是最新批准记录

    # 旧快照仍在，历史等级按原快照保存
    versions = [v for v in store.rows(VERSION_TABLE) if v["档案id"] == 1]
    assert [v["版本号"] for v in versions] == [1, 2, 3]
    v2 = next(v for v in versions if v["版本号"] == 2)
    assert v2["快照"]["上次评定等级"] == "桥梁档案样例1"


def test_duplicate_create_does_not_make_new_archive():
    before = client.get("/api/bridge_info").json()["total"]
    payload = {"values": {"桥梁编号": "BRID-0001", "桥梁名称": "桥梁档案样例1", "桥型结构": "重复提交"}}
    r1 = client.post("/api/bridge_info", json=payload)
    r2 = client.post("/api/bridge_info", json=payload)
    assert r1.status_code == 200 and r2.status_code == 200
    assert "重复提交未生成新档案" in r2.json()["message"]
    after = client.get("/api/bridge_info").json()["total"]
    assert before == after


# ---------------------------------------------------------------------------
# 4. 批量审定：版本快照锁、游标断点续传、整版原子、审定后发布
# ---------------------------------------------------------------------------
def test_batch_snapshot_lock_resume_and_publish():
    # 第一批冻结档案 1、2
    res = client.post("/api/bridge_info/review/batches", json={
        "batch_no": "B20261001-01",
        "items": [
            {"档案id": 1, "桥型结构": "批量-钢箱梁", "设计荷载": "公路-I级", "上次评定等级": "2类", "异常": True},
            {"档案id": 2, "桥型结构": "批量-T梁", "设计荷载": "公路-II级", "上次评定等级": "3类"},
        ],
    })
    assert res.status_code == 200
    batch = res.json()["entry"]
    assert batch["游标"] == 2 and batch["状态"] == "待审定"

    # 锁定期：下游（图面/定检/待办）只能读到快照版本（新送审值），发布状态待发布
    drawings = client.get("/api/bridge_info/drawings").json()["items"]
    d1 = next(d for d in drawings if d["桥梁编号"] == "BRID-0001")
    assert d1["版本号"] == 2 and d1["审定状态"] == "待审定"
    assert d1["桥型结构"] == "批量-钢箱梁"  # 只读快照版本
    assert d1["发布状态"] == "待发布"
    # 档案主数据在发布前仍保留已发布基线
    assert store.find("bridge_info", 1)["桥型结构"] == "桥梁档案样例1"

    # 断线重连：只传第二条 + 第三条，已收录的不重复快照，游标继续前进
    res = client.post("/api/bridge_info/review/batches", json={
        "batch_no": "B20261001-01",
        "items": [
            {"档案id": 2},                       # 已收录：幂等确认
            {"档案id": 3, "桥型结构": "批量-板梁"},
        ],
    })
    batch = res.json()["entry"]
    assert batch["游标"] == 3
    assert {i["档案id"] for i in batch["items"]} == {1, 2, 3}
    # 重复提交没有给档案2造新版本
    v2_counts = [v for v in store.rows(VERSION_TABLE) if v["档案id"] == 2]
    assert len(v2_counts) == 2  # v1 基线 + v2 批次快照

    # 越过审定发布整批：拒绝
    res = client.post("/api/bridge_info/review/batches/B20261001-99/publish")
    assert res.status_code == 404

    # 整批审定 + 发布
    res = client.post("/api/bridge_info/review/batches/B20261001-01/approve", json={"values": {"审定人": "审定组"}})
    assert res.status_code == 200, res.text
    assert all(i["状态"] == "已审定" for i in res.json()["entry"]["items"])
    res = client.post("/api/bridge_info/review/batches/B20261001-01/publish")
    assert res.status_code == 200
    assert all(i["状态"] == "已发布" for i in res.json()["entry"]["items"])

    # 发布后图面读到新快照；档案1异常结论开了待办
    drawings = client.get("/api/bridge_info/drawings").json()["items"]
    d1 = next(d for d in drawings if d["桥梁编号"] == "BRID-0001")
    assert d1["桥型结构"] == "批量-钢箱梁" and d1["发布状态"] == "已发布"
    todos = client.get("/api/bridge_info/todos?status=待办").json()["items"]
    assert any(t["桥梁编号"] == "BRID-0001" for t in todos)

    # 证据链一致
    for archive_id in (1, 2, 3):
        ev = client.get(f"/api/bridge_info/{archive_id}/evidence").json()
        assert ev["一致"] is True, ev["异常项"]


def test_batch_lock_conflicts():
    client.post("/api/bridge_info/review/batches", json={
        "batch_no": "B-LOCK-1", "items": [{"档案id": 1}],
    })
    # 同一档案被另一批次锁定：拒绝
    res = client.post("/api/bridge_info/review/batches", json={
        "batch_no": "B-LOCK-2", "items": [{"档案id": 1}],
    })
    assert res.status_code == 409
    assert "锁定" in res.json()["detail"]["message"]


def test_batch_atomic_all_or_nothing():
    # 档案1 先被单件送审到 v2，整批里混入基线版本错误的条目 → 整批回滚
    client.post("/api/bridge_info/review/batches", json={
        "batch_no": "B-ATOMIC",
        "items": [
            {"档案id": 1},
            {"档案id": 2, "expected_version": 99},
        ],
    })
    assert client.get("/api/bridge_info/review/batches/B-ATOMIC").status_code == 404
    # 档案1没有留下半批次快照
    assert get_archive(1)["版本号"] == 1


# ---------------------------------------------------------------------------
# 5. 冲突裁决：桥型/荷载以最新批准记录为准，历史等级按原快照保存
# ---------------------------------------------------------------------------
def test_arbitration_latest_approval_fields():
    # v2 甲批准钢箱梁/公路-I级，3类
    client.post("/api/bridge_info/1/review", json={"values": {"上次评定等级": "3类"}})
    client.post("/api/bridge_info/1/approve", json={"values": {
        "桥型结构": "钢箱梁", "设计荷载": "公路-I级", "审定人": "甲",
    }})
    client.post("/api/bridge_info/1/publish", json={"values": {}})

    # v3 由乙先审定（桥型/荷载不同于已发布 v2）
    client.post("/api/bridge_info/1/review", json={"values": {
        "桥型结构": "混凝土梁", "设计荷载": "公路-II级", "上次评定等级": "4类",
    }})
    client.post("/api/bridge_info/1/approve", json={"values": {
        "桥型结构": "混凝土梁", "设计荷载": "公路-II级", "审定人": "乙",
    }})

    # 此时甲留下一条时间戳更新的复审批准记录（模拟并发复审抢先）：
    # 待发布 v3 读视图按该最新记录裁决，但尚未翻牌落库。
    v2_approval = next(a for a in store.rows("bridge_info_approvals")
                       if a["档案id"] == 1 and a["版本号"] == 2)
    v2_approval.update({
        "审定时间": review_service._tick(),
        "审定人": "甲-复审",
        "桥型结构": "钢箱梁-复审",
        "设计荷载": "公路-I级",
        "冲突字段": ["桥型结构"],
    })
    approved_view = get_archive(1)
    assert set(approved_view["裁决信息"]["冲突字段"]) == {"桥型结构", "设计荷载"}
    assert approved_view["桥型结构"] == "钢箱梁-复审"
    assert approved_view["设计荷载"] == "公路-I级"
    assert approved_view["上次评定等级"] == "4类"  # 等级不被裁决，按 v3 快照

    client.post("/api/bridge_info/1/publish", json={"values": {}})
    archive = get_archive(1)
    # 发布按裁决值落库
    assert archive["桥型结构"] == "钢箱梁-复审"
    assert archive["设计荷载"] == "公路-I级"
    # 历史等级不参与裁决，按 v3 原快照保存
    assert archive["上次评定等级"] == "4类"
    grades = {v["版本号"]: v["快照"]["上次评定等级"]
              for v in store.rows(VERSION_TABLE) if v["档案id"] == 1}
    assert grades[2] == "3类"
    assert grades[3] == "4类"
    # 图面发布后同样为裁决值，证据链仍一致
    ev = client.get("/api/bridge_info/1/evidence").json()
    assert ev["一致"] is True, ev["异常项"]
    assert ev["图面"]["桥型结构"] == "钢箱梁-复审"


# ---------------------------------------------------------------------------
# 6. 证据链：档案明细与定检页面沿同一条链复核，两处验收一致
# ---------------------------------------------------------------------------
def test_evidence_chain_same_on_both_pages():
    client.post("/api/bridge_info/1/review", json={"values": {"桥型结构": "钢桁架"}})
    client.post("/api/bridge_info/1/approve", json={"values": {"异常": True, "审定人": "张工", "意见": "腹板锈蚀"}})

    # 档案明细入口
    ev_archive = client.get("/api/bridge_info/1/evidence").json()
    # 定检页面入口（种子定检 id=1 的桥梁编号同为 BRID-0001）
    ev_inspection = client.get("/api/bridge/1/evidence").json()
    assert ev_archive["证据链id"] == ev_inspection["证据链id"]
    assert ev_archive["异常项"] == ev_inspection["异常项"]
    assert ev_archive["一致"] == ev_inspection["一致"] is True
    assert ev_inspection["定检记录id"] == 1

    # 定检列表行也带同一证据链与档案版本
    rows = client.get("/api/bridge").json()["items"]
    row1 = next(r for r in rows if r["id"] == 1)
    assert row1["证据链id"] == ev_archive["证据链id"]
    assert row1["档案审定状态"] == "已审定"
    assert row1["异常"] is True

    # 发布后两处仍一致
    client.post("/api/bridge_info/1/publish", json={"values": {}})
    a = client.get("/api/bridge_info/1/evidence").json()
    b = client.get("/api/bridge/1/evidence").json()
    assert a["一致"] is b["一致"] is True
    assert a["发布状态"] == b["发布状态"] == "已发布"


def test_batch_publish_atomic_with_nested_transactions():
    """整批发布内部复用单件发布（嵌套事务）：任一条失败整批回滚，无半成品。"""
    client.post("/api/bridge_info/review/batches", json={
        "batch_no": "B-NEST",
        "items": [{"档案id": 1}, {"档案id": 2}],
    })
    client.post("/api/bridge_info/review/batches/B-NEST/approve", json={"values": {}})
    # 把档案2的状态人为改坏，让整批发布的第二条单件发布失败
    versions = {v["版本号"]: v for v in store.rows(VERSION_TABLE) if v["档案id"] == 2}
    target = max(versions.values(), key=lambda v: v["版本号"])
    target["状态"] = "待审定"
    res = client.post("/api/bridge_info/review/batches/B-NEST/publish")
    assert res.status_code == 409
    # 整批回滚：档案1也不能被发布
    assert get_archive(1)["审定状态"] == "已审定"
    batch = client.get("/api/bridge_info/review/batches/B-NEST").json()
    assert batch["状态"] == "已审定"


def test_evidence_reports_broken_chain():
    """人工破坏图面证据链后，复核应在两处都报异常。"""
    client.post("/api/bridge_info/1/review", json={"values": {}})
    client.post("/api/bridge_info/1/approve", json={"values": {"审定人": "张工"}})
    client.post("/api/bridge_info/1/publish", json={"values": {}})
    for drawing in store.rows(DRAWING_TABLE):
        if drawing["档案id"] == 1:
            drawing["证据链id"] = "EC-TAMPERED"
    ev = client.get("/api/bridge_info/1/evidence").json()
    assert ev["一致"] is False
    assert any("图面证据链不一致" in issue for issue in ev["异常项"])
