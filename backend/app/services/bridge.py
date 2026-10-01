"""桥梁定检业务规则。

检测记录自身状态流转（开始检测/完成评定/归档报告）仍收在这里；列表与明细
叠加桥梁档案的“当前版本快照”视图——档案批量审定期间，定检清单只能读到
快照版本，并沿同一条证据链接受复核。
"""
from __future__ import annotations

from typing import Any

from app.services.bridge_review import review_service
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
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("检测编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        page_rows = [review_service.inspection_view(row) for row in rows[start:start + size]]
        return page_rows, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return review_service.inspection_view(row) if row is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": store.next_id(MODULE)}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        # 可按桥梁编号直接挂到既有档案上，列表与证据链即可闭环
        bridge_code = str(values.get("桥梁编号") or "").strip()
        if bridge_code:
            entry["桥梁编号"] = bridge_code
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return review_service.inspection_view(entry), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"检测记录 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于桥梁定检可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return review_service.inspection_view(entry), f"检测记录已{action}"
