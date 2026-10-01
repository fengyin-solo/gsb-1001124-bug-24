"""桥梁档案业务规则。

管养处置（设置限载/安排加固/启动重建）仍在本服务内；登记、审定、发布与版本
快照锁统一走 :class:`app.services.bridge_review.BridgeReviewService`，
路由层不做业务判断。
"""
from __future__ import annotations

from typing import Any

from app.services.bridge_review import ReviewError, review_service
from app.store import store

MODULE = "bridge_info"
REQUIRED_FIELDS = ["桥梁编号", "桥梁名称", "桥型结构"]
STATUS_ORDER = ["正常", "限载", "加固", "重建"]
ACTION_RULES = {"设置限载": "限载", "安排加固": "加固", "启动重建": "重建"}
NEGATIVE_ACTIONS = []


class BridgeInfoService:
    # -- 审定侧只读/写入全部委托给 review_service --------------------------
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        review_status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        return review_service.list_archives(
            keyword=keyword, review_status=review_status, page=page, size=size
        )

    def get_entry(self, entry_id: int) -> dict[str, Any]:
        return review_service.version_view(entry_id)

    def create_entry(self, values: dict[str, Any], *, idem_key: str | None = None) -> tuple[dict[str, Any] | None, str]:
        view, message = review_service.create_archive(values, idem_key=idem_key)
        return view, message

    # -- 管养处置动作：不允许越过审定状态机，只能作用在已发布版本上 --------
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        with store.transaction():
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"桥梁 {entry_id} 不存在或已归档"
            try:
                view = review_service.version_view(entry_id)
            except ReviewError as exc:
                return None, exc.message
            if view["审定状态"] != "已发布":
                return None, f"档案当前为「{view['审定状态']}」，需审定发布后才能{action}"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于桥梁档案可执行范围"
            target = ACTION_RULES[action]
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            return review_service.version_view(entry_id), f"桥梁已{action}"
