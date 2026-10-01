"""桥梁定检业务规则：状态流转、字段校验、快照可见性与证据链。

定检清单是桥梁档案审定的下游：待审定快照中的定检记录不会提前出现，
档案发布后审定结论回写过来，列表与档案明细共用同一条证据链复核。
"""
from __future__ import annotations

from typing import Any

from app.services.bridge_approval import approval_service
from app.store import store

MODULE = "bridge"
REQUIRED_FIELDS = ["检测编号", "桥梁名称", "检测类型"]
STATUS_ORDER = ["待检测", "检测中", "已评定", "已归档"]
ACTION_RULES = {"开始检测": "检测中", "完成评定": "已评定", "归档报告": "已归档"}
NEGATIVE_ACTIONS = []


class BridgeService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        bridge_code: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [dict(row) for row in store.rows(MODULE)]
        if bridge_code:
            rows = [row for row in rows if row.get("桥梁编号") == bridge_code]
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("检测编号", "")) or keyword in str(row.get("桥梁名称", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        # 版本快照锁：只能读取已发布快照版本，待审定快照里的数据对清单不可见。
        visible: list[dict[str, Any]] = []
        for row in rows:
            code = str(row.get("桥梁编号") or "")
            visible_rows = approval_service.visible_inspections(code) if code else []
            if any(int(item.get("id", 0)) == int(row.get("id", 0)) for item in visible_rows):
                row["证据链"] = approval_service.evidence_summary(code) if code else None
                visible.append(row)
        total = len(visible)
        start = max(page - 1, 0) * size
        return visible[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        if row is None:
            return None
        view = dict(row)
        code = str(row.get("桥梁编号") or "")
        if code:
            view["证据链"] = approval_service.evidence_chain(code)
        return view

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.transaction(MODULE):
            entry = {"id": store.next_id(MODULE)}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            store.rows(MODULE).append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"检测记录 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于桥梁定检可执行范围"
        target = ACTION_RULES[action]
        current = str(entry.get("status") or STATUS_ORDER[0])
        try:
            current_index = STATUS_ORDER.index(current)
            target_index = STATUS_ORDER.index(target)
        except ValueError:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if target_index != current_index + 1:
            return None, f"检测记录当前为「{current}」，只能推进到「{STATUS_ORDER[current_index + 1]}」，不能{action}"
        with store.transaction(MODULE):
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"检测记录已{action}"
