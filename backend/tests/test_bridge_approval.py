"""桥梁档案审定状态机验收测试。

覆盖：顺序推进/越序拒绝、同事务回写与回滚、并发版本唯一、
版本快照锁、冲突裁决与历史等级保留、批量游标续审、幂等重连、证据链两端复核。
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app import store as store_module  # noqa: E402
from app.services.bridge_approval import approval_service  # noqa: E402


def reset_store() -> None:
    store_module.store.__init__()


class ApprovalFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()
        self.client = TestClient(app)

    def payload(self, **overrides):
        data = {
            "桥型结构": "预应力混凝土连续梁",
            "跨径组合": "3×30m",
            "设计荷载": "公路-I级",
            "建成年份": "2010",
            "上次评定等级": "2类",
            "图面": [{"图号": "T-01", "图名": "桥型布置图", "版本": "A", "状态": "待发布"}],
            "定检清单": [{
                "检测编号": "BRID-AUDIT-1",
                "桥梁名称": "桥梁档案样例1",
                "检测类型": "定期检查",
                "检测日期": "2026-09-20",
                "技术状况评分": "88",
                "主要病害": "无",
                "检测单位": "检测中心",
                "检测状态": "已评定",
            }],
            "工程待办": [{
                "工程编号": "PROJ-AUDIT-1",
                "工程名称": "支座更换",
                "工程类型": "养护工程",
                "施工路段": "桥梁档案样例1",
                "承建单位": "养护公司",
                "工程状态": "待开工",
            }],
        }
        data.update(overrides)
        return data

    def test_states_can_only_advance_in_order(self) -> None:
        # 运营状态机：正常态只能先限载，直接加固/重建被拒绝。
        r = self.client.post("/api/bridge_info/1/actions", json={"values": {"action": "安排加固"}})
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertIn("只能推进到", body["message"])

        r = self.client.post("/api/bridge_info/1/actions", json={"values": {"action": "设置限载"}})
        self.assertTrue(r.json()["ok"])
        r = self.client.post("/api/bridge_info/1/actions", json={"values": {"action": "设置限载"}})
        self.assertFalse(r.json()["ok"], "重复/回退动作必须被拒绝")

    def test_skip_approval_publish_rejected_and_happy_path(self) -> None:
        # 越过审定直接发布：服务端 409。
        submit = self.client.post("/api/bridge_info/1/approvals", json={"values": self.payload()}).json()
        snapshot_id = submit["entry"]["id"]
        r = self.client.post(f"/api/bridge_info/approvals/{snapshot_id}/publish", json={})
        self.assertEqual(r.status_code, 409)
        self.assertIn("尚未审定", r.json()["detail"])

        # 顺序推进：审定 → 发布。
        r = self.client.post(f"/api/bridge_info/approvals/{snapshot_id}/approve", json={"conclusion": "同意发布"})
        self.assertEqual(r.status_code, 200)
        r = self.client.post(f"/api/bridge_info/approvals/{snapshot_id}/publish", json={})
        self.assertEqual(r.status_code, 200, r.text)

        detail = self.client.get("/api/bridge_info/1").json()
        self.assertEqual(detail["审定状态"], "已发布")
        self.assertEqual(detail["审定版本"], 1)
        self.assertEqual(detail["上次评定等级"], "2类")

    def test_writeback_is_atomic_and_visible_after_publish(self) -> None:
        snapshot_id = self.client.post("/api/bridge_info/1/approvals", json={"values": self.payload()}).json()["entry"]["id"]
        self.client.post(f"/api/bridge_info/approvals/{snapshot_id}/approve", json={})

        # 发布前：定检清单里看不到快照里的新记录，图面台账也是空的。
        before = self.client.get("/api/bridge?bridge_code=BRID-0001").json()
        self.assertEqual(before["total"], 0)
        self.assertEqual(len(store_module.store.rows("bridge_drawing")), 0)

        self.client.post(f"/api/bridge_info/approvals/{snapshot_id}/publish", json={})

        # 发布后：图面、定检、工程待办同一事务落库，结论回写到位。
        drawings = store_module.store.rows("bridge_drawing")
        self.assertEqual(len(drawings), 1)
        self.assertEqual(drawings[0]["桥梁编号"], "BRID-0001")
        self.assertEqual(drawings[0]["快照版本"], 1)

        inspections = self.client.get("/api/bridge?bridge_code=BRID-0001").json()
        self.assertEqual(inspections["total"], 1)
        self.assertEqual(inspections["items"][0]["检测编号"], "BRID-AUDIT-1")
        self.assertEqual(inspections["items"][0]["审定结论"], "审定通过")

        projects = [p for p in store_module.store.rows("project") if p.get("桥梁编号") == "BRID-0001"]
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["工程编号"], "PROJ-AUDIT-1")

    def test_transaction_rolls_back_on_failure(self) -> None:
        snapshot_id = self.client.post("/api/bridge_info/1/approvals", json={"values": self.payload()}).json()["entry"]["id"]
        self.client.post(f"/api/bridge_info/approvals/{snapshot_id}/approve", json={})

        original_archive = dict(store_module.store.find("bridge_info", 1))
        original_bridge_rows = len(store_module.store.rows("bridge"))

        # 打桩：工程待办回写阶段失败，验证档案与图面/定检的改动整体回滚。
        def boom(snap, version):
            raise RuntimeError("模拟工程待办落库失败")

        approval_service._write_projects = boom  # type: ignore[method-assign]
        try:
            with self.assertRaises(RuntimeError):
                approval_service.publish(snapshot_id)
        finally:
            del approval_service._write_projects

        archive = store_module.store.find("bridge_info", 1)
        self.assertEqual(archive.get("审定状态"), "已审定")
        self.assertEqual(archive.get("桥梁名称"), original_archive["桥梁名称"])
        self.assertEqual(len(store_module.store.rows("bridge_drawing")), 0)
        self.assertEqual(len(store_module.store.rows("bridge")), original_bridge_rows)

    def test_concurrent_approval_only_one_version_wins(self) -> None:
        first = self.client.post("/api/bridge_info/1/approvals", json={"values": self.payload()}).json()["entry"]
        self.assertEqual(first["id"], 1)
        # 并发只能有一个完整版本：同一档案第二份待审定快照不被接受。
        r = self.client.post(
            "/api/bridge_info/1/approvals",
            json={"values": self.payload(设计荷载="公路-II级"), "client_token": "tok-2"},
        )
        self.assertEqual(r.status_code, 409)

        # 多线程同时审定同一快照，只有一份批准记录落库。
        outcomes: list[str] = []

        def approve(token: str) -> None:
            resp = self.client.post(
                f"/api/bridge_info/approvals/{first['id']}/approve",
                json={"client_token": token},
            )
            outcomes.append("ok" if resp.status_code == 200 else resp.json()["detail"])

        threads = [threading.Thread(target=approve, args=(f"c{i}",)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        records = [r for r in store_module.store.rows("bridge_approval_record") if r["桥梁编号"] == "BRID-0001"]
        self.assertEqual(len(records), 1, outcomes)

    def test_conflict_resolved_by_latest_record_but_history_grade_kept(self) -> None:
        # v1 发布：公路-I级，2类。
        snap1 = self.client.post("/api/bridge_info/1/approvals", json={"values": self.payload()}).json()["entry"]
        self.client.post(f"/api/bridge_info/approvals/{snap1['id']}/approve", json={})
        self.client.post(f"/api/bridge_info/approvals/{snap1['id']}/publish", json={})

        # v2 改了设计荷载且历史等级改成 3类，先审定——荷载冲突被最新批准记录拦下。
        snap2 = self.client.post(
            "/api/bridge_info/1/approvals",
            json={"values": self.payload(设计荷载="公路-II级", 上次评定等级="3类")},
        ).json()["entry"]
        self.assertEqual(snap2["版本号"], 1, "新快照基准版本应为已发布 v1")
        r = self.client.post(f"/api/bridge_info/approvals/{snap2['id']}/approve", json={})
        self.assertEqual(r.status_code, 409)
        self.assertIn("设计荷载冲突", r.json()["detail"])

        # 待审定快照仍保留原 3 类等级（历史等级按原快照保存），档案仍停在 v1 裁决值。
        stored = store_module.store.find("bridge_approval_snapshot", snap2["id"])
        self.assertEqual(stored["评定等级"], "3类")
        archive = self.client.get("/api/bridge_info/1").json()
        self.assertEqual(archive["审定版本"], 1)
        self.assertEqual(archive["设计荷载"], "公路-I级")

        # 证据链把这份待审定快照标记为异常，档案列表与详情共用同一口径。
        evidence = self.client.get("/api/bridge_info/by-code/BRID-0001/evidence").json()
        pending = [s for s in evidence["快照链"] if s["审定状态"] == "待审定"]
        self.assertTrue(pending and pending[-1]["异常"])

        # 按最新裁决值修正同一份待审定快照后再审定发布，升级为 v2（等级 3类）。
        stored["档案数据"]["设计荷载"] = "公路-I级"
        self.assertEqual(
            self.client.post(f"/api/bridge_info/approvals/{snap2['id']}/approve", json={}).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(f"/api/bridge_info/approvals/{snap2['id']}/publish", json={}).status_code,
            200,
        )
        detail = self.client.get("/api/bridge_info/1").json()
        self.assertEqual(detail["审定版本"], 2)
        self.assertEqual(detail["上次评定等级"], "3类")
        # 历史等级仍按原快照保存：v1 是 2类，v2 是 3类。
        grades = {v["版本号"]: v["评定等级"] for v in detail["版本历史"]}
        self.assertEqual(grades, {1: "2类", 2: "3类"})

    def test_duplicate_submit_is_idempotent(self) -> None:
        r1 = self.client.post(
            "/api/bridge_info/1/approvals",
            json={"values": self.payload(), "client_token": "dup"},
        ).json()["entry"]
        r2 = self.client.post(
            "/api/bridge_info/1/approvals",
            json={"values": self.payload(), "client_token": "dup"},
        ).json()["entry"]
        self.assertEqual(r1["id"], r2["id"])
        snapshots = [s for s in store_module.store.rows("bridge_approval_snapshot") if s["桥梁编号"] == "BRID-0001"]
        self.assertEqual(len(snapshots), 1)

    def test_batch_snapshot_lock_cursor_resume_and_publish(self) -> None:
        token = "batch-run-1"
        prep = self.client.post(
            "/api/bridge_info/approvals/batch",
            json={"archive_ids": [1, 2, 3], "batch_token": token},
        )
        self.assertEqual(prep.status_code, 200, prep.text)
        batch_id = prep.json()["entry"]["批量审定id"]
        self.assertEqual(len(prep.json()["entry"]["待审定游标"]), 3)

        # 断连重连：同 token 拿到原批次而非新建。
        again = self.client.post(
            "/api/bridge_info/approvals/batch",
            json={"archive_ids": [1, 2, 3], "batch_token": token},
        ).json()["entry"]
        self.assertEqual(again["批量审定id"], batch_id)

        # 全部待审定期间，清单读不到任何快照版本。
        self.assertEqual(self.client.get("/api/bridge?bridge_code=BRID-0001").json()["total"], 0)

        done = self.client.post(f"/api/bridge_info/approvals/batch/{batch_id}/approve", json={}).json()["entry"]
        self.assertEqual(done["cursor"], 3)
        self.assertEqual(done["审定状态"], "已审定")
        # 再次审定（模拟断连从游标继续）：游标保持 3，不重复批准。
        records_before = len(store_module.store.rows("bridge_approval_record"))
        again_approve = self.client.post(f"/api/bridge_info/approvals/batch/{batch_id}/approve", json={}).json()["entry"]
        self.assertEqual(again_approve["cursor"], 3)
        self.assertEqual(len(store_module.store.rows("bridge_approval_record")), records_before)

        progress = self.client.get(f"/api/bridge_info/approvals/batch/{batch_id}").json()
        self.assertEqual(progress["待审定游标"], [])

        pub = self.client.post(f"/api/bridge_info/approvals/batch/{batch_id}/publish").json()["entry"]
        self.assertEqual(pub["审定状态"], "已发布")
        for archive_id in (1, 2, 3):
            self.assertEqual(self.client.get(f"/api/bridge_info/{archive_id}").json()["审定状态"], "已发布")

    def test_snapshot_lock_downstream_reads_until_published(self) -> None:
        def make_payload(grade: str, ins_code: str):
            payload = self.payload(上次评定等级=grade)
            payload["定检清单"][0]["检测编号"] = ins_code
            return payload

        s1 = self.client.post("/api/bridge_info/1/approvals", json={"values": make_payload("2类", "INS-V1")}).json()["entry"]
        self.client.post(f"/api/bridge_info/approvals/{s1['id']}/approve", json={})
        self.client.post(f"/api/bridge_info/approvals/{s1['id']}/publish", json={})
        self.assertEqual(
            [i["检测编号"] for i in self.client.get("/api/bridge?bridge_code=BRID-0001").json()["items"]],
            ["INS-V1"],
        )

        # v2 待审定、已审定未发布期间：清单仍只能读 v1。
        s2 = self.client.post("/api/bridge_info/1/approvals", json={"values": make_payload("3类", "INS-V2")}).json()["entry"]
        self.assertEqual(s2["版本号"], 1)
        self.assertEqual(
            [i["检测编号"] for i in self.client.get("/api/bridge?bridge_code=BRID-0001").json()["items"]],
            ["INS-V1"],
        )
        self.client.post(f"/api/bridge_info/approvals/{s2['id']}/approve", json={})
        self.assertEqual(
            [i["检测编号"] for i in self.client.get("/api/bridge?bridge_code=BRID-0001").json()["items"]],
            ["INS-V1"],
        )

        # 发布后切到 v2：v1 定检记录与历史等级快照都保留。
        self.client.post(f"/api/bridge_info/approvals/{s2['id']}/publish", json={})
        visible = {
            i["检测编号"]: i.get("快照版本")
            for i in self.client.get("/api/bridge?bridge_code=BRID-0001").json()["items"]
        }
        self.assertEqual(visible, {"INS-V1": 1, "INS-V2": 2})
        grades = {
            s["版本号"]: s["评定等级"]
            for s in self.client.get("/api/bridge_info/1").json()["版本历史"]
        }
        self.assertEqual(grades, {1: "2类", 2: "3类"})

    def test_evidence_chain_shared_between_archive_and_inspection(self) -> None:
        snapshot_id = self.client.post("/api/bridge_info/1/approvals", json={"values": self.payload()}).json()["entry"]["id"]
        self.client.post(f"/api/bridge_info/approvals/{snapshot_id}/approve", json={"conclusion": "证据链验收"})
        self.client.post(f"/api/bridge_info/approvals/{snapshot_id}/publish", json={})

        from_archive = self.client.get("/api/bridge_info/by-code/BRID-0001/evidence").json()
        from_inspection = self.client.get("/api/bridge/by-code/BRID-0001/evidence").json()
        self.assertEqual(from_archive, from_inspection)
        self.assertEqual(from_archive["桥梁编号"], "BRID-0001")
        self.assertEqual(len(from_archive["批准记录"]), 1)
        self.assertEqual(from_archive["批准记录"][0]["审定结论"], "证据链验收")
        self.assertEqual(len(from_archive["图面"]), 1)
        self.assertEqual(len(from_archive["定检记录"]), 1)
        self.assertEqual(len(from_archive["工程待办"]), 1)

        # 档案列表和档案详情都带同一口径的证据链摘要。
        listed = self.client.get("/api/bridge_info?keyword=BRID-0001").json()["items"][0]
        self.assertEqual(listed["证据链"]["最新批准记录id"], from_archive["批准记录"][0]["id"])
        detail = self.client.get("/api/bridge_info/1").json()
        self.assertEqual(detail["证据链"]["批准记录"][0]["id"], listed["证据链"]["最新批准记录id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
