"""桥梁档案接口。

除原有“设置限载/安排加固/启动重建”管养动作外，提供档案审定状态机接口：
单件送审/审定/发布、批量版本快照锁（可凭批次号断点续传）、图面与工程待办
读视图，以及档案-图面-定检-待办的证据链复核。状态流转全部由服务端强制。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, BatchReviewPayload, EntryPayload, PageResult
from app.services.bridge_info import BridgeInfoService
from app.services.bridge_review import REVIEW_STATES, ReviewError, review_service

router = APIRouter(prefix="/api/bridge_info", tags=["桥梁档案"])

service = BridgeInfoService()

LIST_FIELDS = ["桥梁编号", "桥梁名称", "桥型结构", "跨径组合", "设计荷载", "建成年份", "上次评定等级", "桥梁状态"]


def _raise(exc: ReviewError) -> None:
    raise HTTPException(status_code=exc.status, detail={"message": exc.message, **exc.extra})


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按桥梁编号检索"),
    review_status: str | None = Query(default=None, description="待审定、已审定、已发布"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按桥梁编号与审定状态过滤桥梁档案列表；列表只呈现当前版本快照视图。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    if review_status and review_status not in REVIEW_STATES:
        raise HTTPException(status_code=400, detail=f"审定状态仅支持：{'、'.join(REVIEW_STATES)}")
    items, total = service.list_entries(keyword=keyword, review_status=review_status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


# 以下静态路径必须注册在 /{entry_id} 之前，否则会被路径参数吞掉。

@router.get("/review/batches/{batch_no}")
def get_batch(batch_no: str) -> dict[str, Any]:
    """读取批量审定批次进度（待审定游标），供断线后继续。"""
    try:
        return review_service.get_batch(batch_no)
    except ReviewError as exc:
        _raise(exc)


@router.post("/review/batches", response_model=ActionResult)
def submit_batch(payload: BatchReviewPayload) -> ActionResult:
    """批量送审：结果先形成待审定快照并锁定版本；重复提交幂等，从游标继续。"""
    try:
        view = review_service.submit_batch({"batch_no": payload.batch_no, "items": payload.items})
    except ReviewError as exc:
        _raise(exc)
    return ActionResult(
        ok=True,
        message=f"批次 {payload.batch_no} 已冻结 {view['条目数']} 条待审定快照，可继续或整体审定",
        entry=view,
    )


@router.post("/review/batches/{batch_no}/approve", response_model=ActionResult)
def approve_batch(batch_no: str, payload: EntryPayload | None = None) -> ActionResult:
    """整批审定通过：任一档案失败整批回滚，只接受一个完整版本。"""
    try:
        view = review_service.approve_batch(batch_no, (payload.values if payload else None))
    except ReviewError as exc:
        _raise(exc)
    return ActionResult(ok=True, message=f"批次 {batch_no} 已整体审定通过，等待发布", entry=view)


@router.post("/review/batches/{batch_no}/publish", response_model=ActionResult)
def publish_batch(batch_no: str) -> ActionResult:
    """整批发布：越过审定的发布请求由服务端拒绝。"""
    try:
        view = review_service.publish_batch(batch_no)
    except ReviewError as exc:
        _raise(exc)
    return ActionResult(ok=True, message=f"批次 {batch_no} 已发布，图面/定检/待办同事务落库", entry=view)


@router.get("/drawings")
def list_drawings(keyword: str | None = None) -> dict[str, Any]:
    """图面清单：只能读到各档案当前版本快照对应的图面。"""
    items = review_service.list_drawings(keyword=keyword)
    return {"module": "bridge_drawing", "total": len(items), "items": items}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出桥梁档案清单：返回当前版本快照视图下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "bridge_info", "total": total, "items": items}


@router.get("/todos")
def list_todos(
    keyword: str | None = None,
    status: str | None = Query(default=None, description="待办、已闭环"),
) -> dict[str, Any]:
    """工程待办清单：只读当前快照版本那条，历史版本待办保留留痕。"""
    items = review_service.list_todos(keyword=keyword, status=status)
    return {"module": "project_todo", "total": len(items), "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条桥梁明细（版本快照视图）；不存在时给出可读的错误说明。"""
    try:
        return service.get_entry(entry_id)
    except ReviewError as exc:
        _raise(exc)


@router.get("/{entry_id}/evidence")
def get_evidence(entry_id: int) -> dict[str, Any]:
    """证据链复核：档案明细页与定检页沿同一条链验收，口径完全一致。"""
    try:
        return review_service.evidence_chain(entry_id)
    except ReviewError as exc:
        _raise(exc)


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记桥梁即冻结 v1 待审定快照；同一桥梁编号重复提交不生成新档案。"""
    idem_key = str(payload.values.get("idem_key") or payload.remark or "") or None
    try:
        entry, message = service.create_entry(payload.values, idem_key=idem_key)
    except ReviewError as exc:
        if exc.status >= 500:
            _raise(exc)
        return ActionResult(ok=False, message=exc.message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/review", response_model=ActionResult)
def submit_review(entry_id: int, payload: EntryPayload) -> ActionResult:
    """修订送审：已发布版本开新待审定快照；基线版本不符按并发冲突拒绝。"""
    try:
        view = review_service.submit_review(entry_id, payload.values)
    except ReviewError as exc:
        _raise(exc)
    return ActionResult(ok=True, message="待审定快照已冻结", entry=view)


@router.post("/{entry_id}/approve", response_model=ActionResult)
def approve_review(entry_id: int, payload: EntryPayload) -> ActionResult:
    """审定通过（待审定→已审定）：结论同事务回写图面、定检清单、工程待办。"""
    try:
        view = review_service.approve_review(entry_id, payload.values)
    except ReviewError as exc:
        _raise(exc)
    return ActionResult(ok=True, message="审定结论已回写图面、定检清单与工程待办", entry=view)


@router.post("/{entry_id}/publish", response_model=ActionResult)
def publish_review(entry_id: int, payload: EntryPayload | None = None) -> ActionResult:
    """发布（已审定→已发布）：越过审定直接发布由服务端拒绝。"""
    try:
        view = review_service.publish_review(entry_id, payload.values if payload else None)
    except ReviewError as exc:
        _raise(exc)
    return ActionResult(ok=True, message="档案已发布，图面、定检清单与工程待办同事务翻牌", entry=view)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条桥梁执行设置限载、安排加固、启动重建；未发布档案会被拦下。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
