"""桥梁档案业务规则：审定状态机、运营状态顺序流转与字段校验都收在这里。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "bridge_info"
REQUIRED_FIELDS = ["桥梁编号", "桥梁名称", "桥型结构"]

# 运营状态只允许顺序推进：正常 → 限载 → 加固 → 重建，不能跳跃或回退。
STATUS_ORDER = ["正常", "限载", "加固", "重建"]
ACTION_RULES = {"设置限载": "限载", "安排加固": "加固", "启动重建": "重建"}
NEGATIVE_ACTIONS = []

# 审定状态机：待审定 → 已审定 → 已发布，只能顺序推进。
AUDIT_PENDING = "待审定"
AUDIT_APPROVED = "已审定"
AUDIT_PUBLISHED = "已发布"


class BridgeInfoService:
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
            rows = [row for row in rows if keyword in str(row.get("桥梁编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        # 同一桥梁编号重复登记不生成新档案，直接返回既有档案。
        code = str(values.get("桥梁编号") or "").strip()
        for row in store.rows(MODULE):
            if str(row.get("桥梁编号") or "") == code:
                return row, []
        with store.transaction(MODULE):
            entry = {"id": store.next_id(MODULE)}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            entry["status"] = STATUS_ORDER[0]
            entry["审定状态"] = AUDIT_PENDING
            entry["审定版本"] = 0
            entry["pending"] = True
            entry["abnormal"] = False
            store.rows(MODULE).append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"桥梁 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于桥梁档案可执行范围"
        target = ACTION_RULES[action]
        current = str(entry.get("status") or STATUS_ORDER[0])
        try:
            current_index = STATUS_ORDER.index(current)
            target_index = STATUS_ORDER.index(target)
        except ValueError:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        # 状态机收紧：只能推进到紧邻的下一态，跳跃与回退一律拒绝。
        if target_index != current_index + 1:
            return None, f"桥梁当前为「{current}」，只能推进到「{STATUS_ORDER[current_index + 1]}」，不能{action}"
        with store.transaction(MODULE):
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            # 运营动作产生的变更在发布版本之外，需要重新审定后才能对外。
            if entry.get("审定状态") == AUDIT_PUBLISHED:
                entry["审定状态"] = AUDIT_PENDING
        return entry, f"桥梁已{action}，变更待重新审定"
