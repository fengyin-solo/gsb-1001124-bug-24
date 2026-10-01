"""桥梁档案接口：审定状态机、版本快照锁、批量审定游标与证据链。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import (
    ActionResult,
    ApprovePayload,
    BatchActionPayload,
    BatchPreparePayload,
    EntryPayload,
    PageResult,
    PublishPayload,
    SubmitApprovalPayload,
)
from app.services.bridge_approval import ApprovalError, approval_service
from app.services.bridge_info import BridgeInfoService

router = APIRouter(prefix="/api/bridge_info", tags=["桥梁档案"])

service = BridgeInfoService()

LIST_FIELDS = ["桥梁编号", "桥梁名称", "桥型结构", "跨径组合", "设计荷载", "建成年份", "上次评定等级", "桥梁状态"]
STATUSES = ["正常", "限载", "加固", "重建"]
AUDIT_STATUSES = ["待审定", "已审定", "已发布"]


def _raise(exc: ApprovalError) -> HTTPException:
    return HTTPException(status_code=exc.http_status, detail=exc.message)


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按桥梁编号检索"),
    status: str | None = Query(default=None, description="运营状态：正常、限载、加固、重建"),
    audit_status: str | None = Query(default=None, description="审定状态：待审定、已审定、已发布"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """桥梁档案列表：审定状态与证据链随列表一并返回，与详情同一口径。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items = approval_service.list_archives(audit_status=audit_status)
    if keyword:
        items = [row for row in items if keyword in str(row.get("桥梁编号", ""))]
    if status:
        items = [row for row in items if row.get("status") == status]
    total = len(items)
    start = max(page - 1, 0) * size
    return PageResult(items=items[start:start + size], total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """桥梁档案详情：版本历史、批准记录与图面/定检/工程待办沿同一条证据链展开。"""
    try:
        return approval_service.archive_detail(entry_id)
    except ApprovalError as exc:
        raise _raise(exc)


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条桥梁，缺字段时说明原因而不是静默丢弃。新档案进入「待审定」。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="桥梁已登记，等待审定", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """限载/加固/重建等运营动作仍只能顺序推进，且已发布之后的变更需重新走审定。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


# --------------------------------------------------------------- 审定状态机
@router.post("/{entry_id}/approvals", response_model=ActionResult)
def submit_for_approval(entry_id: int, payload: SubmitApprovalPayload) -> ActionResult:
    """提交审定：冻结一份待审定版本快照，下游清单此时只能读到上一版已发布数据。"""
    try:
        snapshot = approval_service.submit(
            entry_id,
            payload.values,
            client_token=payload.client_token,
        )
    except ApprovalError as exc:
        raise _raise(exc)
    return ActionResult(ok=True, message="已形成待审定快照，等待审定", entry=snapshot)


@router.post("/approvals/{snapshot_id}/approve", response_model=ActionResult)
def approve_snapshot(snapshot_id: int, payload: ApprovePayload) -> ActionResult:
    """审定通过：并发提交只接受一个版本，冲突以最新批准记录裁决。"""
    try:
        snapshot = approval_service.approve(
            snapshot_id,
            conclusion=payload.conclusion,
            base_version=payload.base_version,
            approver=payload.approver,
            client_token=payload.client_token,
        )
    except ApprovalError as exc:
        raise _raise(exc)
    return ActionResult(ok=True, message="审定通过，结论已回写，等待发布", entry=snapshot)


@router.post("/approvals/{snapshot_id}/publish", response_model=ActionResult)
def publish_snapshot(snapshot_id: int, payload: PublishPayload) -> ActionResult:
    """发布：越过审定直接发布会被拒绝；档案/图面/定检/工程待办同一事务落库。"""
    try:
        snapshot = approval_service.publish(snapshot_id, client_token=payload.client_token)
    except ApprovalError as exc:
        raise _raise(exc)
    return ActionResult(ok=True, message="审定版本已发布，图面、定检清单、工程待办已同步", entry=snapshot)


# ----------------------------------------------------------------- 批量审定
@router.post("/approvals/batch", response_model=ActionResult)
def prepare_batch(payload: BatchPreparePayload) -> ActionResult:
    """批量审定第一步：整批形成待审定快照（版本快照锁）。"""
    if not payload.archive_ids:
        return ActionResult(ok=False, message="批量审定至少选择一份桥梁档案")
    try:
        batch = approval_service.prepare_batch(
            payload.archive_ids,
            payload.payloads,
            batch_token=payload.batch_token,
        )
    except ApprovalError as exc:
        raise _raise(exc)
    return ActionResult(ok=True, message="批量待审定快照已形成", entry=batch)


@router.post("/approvals/batch/{batch_id}/approve", response_model=ActionResult)
def approve_batch(batch_id: int, payload: BatchActionPayload) -> ActionResult:
    """批量审定第二步：按待审定游标逐份审定，断连重连后从游标继续。"""
    try:
        batch = approval_service.approve_batch(batch_id, approver=payload.approver)
    except ApprovalError as exc:
        raise _raise(exc)
    return ActionResult(ok=True, message=f"批量审定完成 {batch['cursor']}/{batch['总数']}", entry=batch)


@router.post("/approvals/batch/{batch_id}/publish", response_model=ActionResult)
def publish_batch(batch_id: int) -> ActionResult:
    """批量审定第三步：逐份发布，每份发布内部仍是四表同事务。"""
    try:
        batch = approval_service.publish_batch(batch_id)
    except ApprovalError as exc:
        raise _raise(exc)
    done = sum(1 for item in batch["结果"] if item["ok"])
    return ActionResult(ok=done == batch["总数"], message=f"批量发布完成 {done}/{batch['总数']}", entry=batch)


@router.get("/approvals/batch/{batch_id}", response_model=dict)
def get_batch(batch_id: int) -> dict[str, Any]:
    """读取批量审定进度：连接断开后凭批次id找回待审定游标。"""
    try:
        return approval_service.get_batch(batch_id)
    except ApprovalError as exc:
        raise _raise(exc)


# ------------------------------------------------------------------- 证据链
@router.get("/by-code/{bridge_code}/evidence", response_model=dict)
def evidence_chain(bridge_code: str) -> dict[str, Any]:
    """档案明细与定检页面共用的证据链：同一根因在两处按同一口径复核。"""
    return approval_service.evidence_chain(bridge_code)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出桥梁档案清单：返回当前过滤条件下的全量数据（仅已发布审定版本视图）。"""
    items = approval_service.list_archives()
    return {"module": "bridge_info", "total": len(items), "items": items}
