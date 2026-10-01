"""桥梁定检接口：维护检测记录，覆盖开始检测、完成评定、归档报告等动作。

清单数据受档案版本快照锁约束，只能看到已发布审定版本；
异常复核与档案明细共用 /api/bridge_info/by-code/{桥梁编号}/evidence 证据链。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.bridge import BridgeService
from app.services.bridge_approval import approval_service

router = APIRouter(prefix="/api/bridge", tags=["桥梁定检"])

service = BridgeService()

LIST_FIELDS = ["检测编号", "桥梁名称", "检测类型", "检测日期", "技术状况评分", "主要病害", "检测单位", "检测状态"]
STATUSES = ["待检测", "检测中", "已评定", "已归档"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按检测编号或桥梁名称检索"),
    status: str | None = Query(default=None, description="待检测、检测中、已评定、已归档"),
    bridge_code: str | None = Query(default=None, description="按桥梁编号过滤，只返回该档案已发布版本的定检"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按检测编号与状态过滤桥梁定检列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, bridge_code=bridge_code, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条检测记录明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"检测记录 {entry_id} 不存在或已归档")
    return entry


@router.get("/by-code/{bridge_code}/evidence", response_model=dict)
def evidence_chain(bridge_code: str) -> dict[str, Any]:
    """定检页面的证据链入口，与档案明细接口指向同一份复核数据。"""
    return approval_service.evidence_chain(bridge_code)


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条检测记录，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="检测记录已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条检测记录执行开始检测、完成评定、归档报告；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出桥梁定检清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "bridge", "total": total, "items": items}
