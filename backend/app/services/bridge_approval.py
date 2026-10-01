"""桥梁档案审定领域服务。

这里把“审定状态机”收紧成一条单向流水线：

    待审定 ──提交快照──▶ 待审定(快照锁) ──审定通过──▶ 已审定 ──发布──▶ 已发布

约束要点：
* 待审定、已审定、已发布只能顺序推进，越过审定直接发布由服务端拒绝；
* 审定结论在同一事务里回写到图面、定检清单、工程待办，与档案一起落库，失败整体回滚；
* 并发审定只接受一个版本：关键区加锁 + 基准版本乐观校验，先落库的批准记录为准；
* 批量审定先形成“待审定快照”，图面/定检/工程清单只能读到已发布快照版本；
* 桥型或荷载冲突时以最新批准记录裁决，历史等级仍随原快照原样保存；
* 重复提交不生成新档案，断连后凭待审定游标继续。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.store import store

ARCHIVE = "bridge_info"
DRAWING = "bridge_drawing"
SNAPSHOTS = "bridge_approval_snapshot"
RECORDS = "bridge_approval_record"
BATCHES = "bridge_approval_batch"

AUDIT_PENDING = "待审定"
AUDIT_APPROVED = "已审定"
AUDIT_PUBLISHED = "已发布"
AUDIT_FLOW = [AUDIT_PENDING, AUDIT_APPROVED, AUDIT_PUBLISHED]

# 快照负载里允许携带的三类回写对象
DRAWING_KEYS = ["图号", "图名", "版本", "状态"]
INSPECTION_KEYS = ["检测编号", "桥梁名称", "检测类型", "检测日期", "技术状况评分", "主要病害", "检测单位", "检测状态"]
PROJECT_KEYS = ["工程编号", "工程名称", "工程类型", "施工路段", "承建单位", "开工日期", "竣工日期", "工程状态"]


class ApprovalError(Exception):
    """审定流程的业务拒绝；http_status 给出服务端应返回的状态码。"""

    def __init__(self, message: str, *, http_status: int = 409) -> None:
        super().__init__(message)
        self.message = message
        self.http_status = http_status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pick(values: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    return {key: values[key] for key in keys if key in values and values[key] is not None}


def _archive_view(row: dict[str, Any]) -> dict[str, Any]:
    """档案列表/明细的统一视图：补齐审定状态机字段，兼容老种子数据。"""
    view = dict(row)
    view.setdefault("审定状态", AUDIT_PENDING)
    view.setdefault("审定版本", 0)
    view.setdefault("桥梁编号", None)
    return view


def _find_archive(archive_id: int) -> dict[str, Any]:
    row = store.find(ARCHIVE, archive_id)
    if row is None:
        raise ApprovalError(f"桥梁档案 {archive_id} 不存在或已归档", http_status=404)
    return row


def _published_snapshots(bridge_code: str) -> list[dict[str, Any]]:
    return [
        snap for snap in store.rows(SNAPSHOTS)
        if snap["桥梁编号"] == bridge_code and snap["审定状态"] == AUDIT_PUBLISHED
    ]


def _latest_published(bridge_code: str) -> dict[str, Any] | None:
    published = _published_snapshots(bridge_code)
    return max(published, key=lambda snap: int(snap["版本号"]), default=None)


def _pending_snapshot(archive_id: int | None = None, bridge_code: str | None = None) -> dict[str, Any] | None:
    for snap in store.rows(SNAPSHOTS):
        if snap["审定状态"] != AUDIT_PENDING:
            continue
        if archive_id is not None and snap["档案id"] == archive_id:
            return snap
        if bridge_code is not None and snap["桥梁编号"] == bridge_code:
            return snap
    return None


class BridgeApprovalService:
    # ------------------------------------------------------------------ 提交
    def submit(self, archive_id: int, payload: dict[str, Any], *, client_token: str | None = None) -> dict[str, Any]:
        """单档案提交审定：把档案连同图面/定检/工程待办冻结成一份待审定快照。

        重复提交（同一 client_token）直接返回原快照，不生成新档案、新版本。
        """
        with store.locked():
            archive = _find_archive(archive_id)
            archive = self._ensure_submittable(archive)

            existing = _pending_snapshot(archive_id=archive_id)
            if existing is not None:
                if client_token and existing.get("client_token") == client_token:
                    return existing
                raise ApprovalError(
                    f"桥梁档案 {archive_id} 已有待审定快照 v{existing['版本号'] + 1}，"
                    "请先审定或放弃该快照，不能重复提交",
                )

            bridge_code = str(archive["桥梁编号"])
            return self._freeze_snapshot(archive, payload, client_token=client_token, batch_id=None)

    def _ensure_submittable(self, archive: dict[str, Any]) -> dict[str, Any]:
        state = archive.get("审定状态", AUDIT_PENDING)
        if state == AUDIT_APPROVED:
            raise ApprovalError("档案已审定待发布，请先发布或驳回后再提交新版本")
        if state not in AUDIT_FLOW:
            raise ApprovalError(f"审定状态「{state}」不在状态机内")
        return archive

    def _freeze_snapshot(
        self,
        archive: dict[str, Any],
        payload: dict[str, Any],
        *,
        client_token: str | None,
        batch_id: int | None,
    ) -> dict[str, Any]:
        bridge_code = str(archive["桥梁编号"])
        latest = _latest_published(bridge_code)
        version_no = int(latest["版本号"]) if latest else 0
        snapshot = {
            "id": store.next_id(SNAPSHOTS),
            "档案id": int(archive["id"]),
            "桥梁编号": bridge_code,
            "版本号": version_no,  # 基准版本；发布成功后才占用 version_no + 1
            "审定状态": AUDIT_PENDING,
            "client_token": client_token,
            "批量审定id": batch_id,
            "提交时间": _now(),
            "审定时间": None,
            "发布时间": None,
            "审定结论": None,
            "评定等级": payload.get("上次评定等级") or archive.get("上次评定等级"),
            "档案数据": {
                key: payload.get(key, archive.get(key))
                for key in ["桥梁名称", "桥型结构", "跨径组合", "设计荷载", "建成年份", "上次评定等级"]
            },
            "图面": [_pick(item, DRAWING_KEYS) for item in payload.get("图面", [])],
            "定检清单": [_pick(item, INSPECTION_KEYS) for item in payload.get("定检清单", [])],
            "工程待办": [_pick(item, PROJECT_KEYS) for item in payload.get("工程待办", [])],
        }
        store.rows(SNAPSHOTS).append(snapshot)
        # 档案保持“待审定”，但挂上待审定游标；下游此时仍只能读到上一版已发布数据。
        archive["审定状态"] = AUDIT_PENDING
        archive["待审定快照id"] = snapshot["id"]
        return snapshot

    # ------------------------------------------------------------------ 审定
    def approve(
        self,
        snapshot_id: int,
        *,
        conclusion: str = "审定通过",
        base_version: int | None = None,
        approver: str = "审定人",
        client_token: str | None = None,
    ) -> dict[str, Any]:
        """审定通过：冲突以最新批准记录裁决；结论与档案同事务写库（发布时回写三类对象）。

        此处只落“批准记录 + 已审定状态”，不对外发布，保证未审定版本不会泄漏到清单。
        """
        with store.locked():
            snap = self._get_snapshot(snapshot_id)
            # 网络重发：同一 client_token 的审定视为幂等，直接回原结论，不产生第二份批准记录。
            if client_token:
                repeat = self._find_record_by_token(client_token)
                if repeat is not None and int(repeat["快照id"]) == snapshot_id:
                    return snap
            if snap["审定状态"] != AUDIT_PENDING:
                raise ApprovalError(
                    f"快照当前为「{snap['审定状态']}」，只有待审定快照可以审定通过",
                )
            archive = _find_archive(int(snap["档案id"]))

            # 乐观锁：提交方带的基准版本必须仍是该桥梁的最新版本，否则拒绝旧快照。
            if base_version is not None and int(base_version) != int(snap["版本号"]):
                raise ApprovalError(
                    f"快照基准版本 v{base_version} 与待审定版本 v{snap['版本号']} 不一致，"
                    "请基于最新版本重新提交",
                )

            # 桥型/荷载冲突：以最新批准记录（= 最新已发布档案）裁决。
            latest_record = self._latest_record(str(snap["桥梁编号"]))
            if latest_record is not None:
                for field in ("桥型结构", "设计荷载"):
                    incoming = str(snap["档案数据"].get(field) or "")
                    canonical = str(latest_record.get(f"裁决_{field}") or "")
                    if incoming and canonical and incoming != canonical:
                        raise ApprovalError(
                            f"{field}冲突：待审定值「{incoming}」与最新批准记录「{canonical}」"
                            "不一致，冲突以最新审定记录裁决，历史等级仍按原快照保存",
                        )

            record = None
            if client_token:
                record = self._find_record_by_token(client_token)
            with store.transaction(RECORDS, SNAPSHOTS, ARCHIVE):
                if record is None:
                    record = {
                        "id": store.next_id(RECORDS),
                        "快照id": snapshot_id,
                        "桥梁编号": snap["桥梁编号"],
                        "版本号": int(snap["版本号"]) + 1,
                        "审定时间": _now(),
                        "审定人": approver,
                        "审定结论": conclusion,
                        "client_token": client_token,
                        "裁决_桥型结构": snap["档案数据"].get("桥型结构"),
                        "裁决_设计荷载": snap["档案数据"].get("设计荷载"),
                        "评定等级": snap.get("评定等级"),
                    }
                    store.rows(RECORDS).append(record)

                snap["审定状态"] = AUDIT_APPROVED
                snap["审定时间"] = record["审定时间"]
                snap["审定结论"] = conclusion
                archive["审定状态"] = AUDIT_APPROVED
                archive["已审定快照id"] = snapshot_id
            return snap

    def _latest_record(self, bridge_code: str) -> dict[str, Any] | None:
        rows = [row for row in store.rows(RECORDS) if row["桥梁编号"] == bridge_code]
        return max(rows, key=lambda row: int(row["id"]), default=None)

    def _find_record_by_token(self, token: str) -> dict[str, Any] | None:
        for row in store.rows(RECORDS):
            if row.get("client_token") == token:
                return row
        return None

    # ------------------------------------------------------------------ 发布
    def publish(self, snapshot_id: int, *, client_token: str | None = None) -> dict[str, Any]:
        """发布已审定快照：档案 + 图面 + 定检清单 + 工程待办同一事务落库。"""
        with store.locked():
            snap = self._get_snapshot(snapshot_id)
            if snap["审定状态"] == AUDIT_PENDING:
                # 越过审定直接发布：服务端拒绝。
                raise ApprovalError("快照尚未审定，不能越过「已审定」直接发布")
            if snap["审定状态"] == AUDIT_PUBLISHED:
                if client_token:
                    return snap  # 发布重试按幂等处理
                raise ApprovalError("该快照已发布，不能重复发布")

            archive = _find_archive(int(snap["档案id"]))
            tables = (ARCHIVE, DRAWING, "bridge", "project")
            with store.transaction(*tables):
                new_version = int(snap["版本号"]) + 1
                data = snap["档案数据"]
                archive.update({
                    "桥梁名称": data.get("桥梁名称"),
                    "桥型结构": data.get("桥型结构"),
                    "跨径组合": data.get("跨径组合"),
                    "设计荷载": data.get("设计荷载"),
                    "建成年份": data.get("建成年份"),
                    "上次评定等级": snap.get("评定等级") or data.get("上次评定等级"),
                    "审定状态": AUDIT_PUBLISHED,
                    "审定版本": new_version,
                    "已发布快照id": snap["id"],
                    "pending": False,
                })
                archive.pop("待审定快照id", None)
                archive.pop("已审定快照id", None)

                self._write_drawings(snap, new_version)
                self._write_inspections(snap, new_version)
                self._write_projects(snap, new_version)

                snap["审定状态"] = AUDIT_PUBLISHED
                snap["发布时间"] = _now()
                snap["版本号"] = new_version
            return snap

    def _write_drawings(self, snap: dict[str, Any], version: int) -> None:
        rows = store.rows(DRAWING)
        for item in snap.get("图面", []):
            rows.append({
                "id": store.next_id(DRAWING),
                "桥梁编号": snap["桥梁编号"],
                "快照版本": version,
                "可见版本": version,
                "审定状态": AUDIT_PUBLISHED,
                "图号": item.get("图号"),
                "图名": item.get("图名"),
                "版本": item.get("版本"),
                "状态": item.get("状态", "已发布"),
            })

    def _write_inspections(self, snap: dict[str, Any], version: int) -> None:
        rows = store.rows("bridge")
        for item in snap.get("定检清单", []):
            code = item.get("检测编号")
            target = self._match_row(
                rows,
                code=code,
                bridge_name=None if code else item.get("桥梁名称"),
                bridge_code=snap["桥梁编号"],
                name_field="桥梁名称",
                code_field="检测编号",
            )
            payload = dict(item)
            payload["桥梁编号"] = snap["桥梁编号"]
            payload["快照版本"] = version
            payload["审定状态"] = AUDIT_PUBLISHED
            payload["审定结论"] = snap.get("审定结论")
            if target is None:
                payload["id"] = store.next_id("bridge")
                payload.setdefault("status", "已评定")
                payload["pending"] = False
                rows.append(payload)
            else:
                target.update(payload)

    def _write_projects(self, snap: dict[str, Any], version: int) -> None:
        rows = store.rows("project")
        for item in snap.get("工程待办", []):
            target = self._match_row(
                rows,
                code=item.get("工程编号"),
                bridge_name=None,
                bridge_code=snap["桥梁编号"],
                name_field="施工路段",
                code_field="工程编号",
            )
            payload = dict(item)
            payload["桥梁编号"] = snap["桥梁编号"]
            payload["快照版本"] = version
            payload["审定状态"] = AUDIT_PUBLISHED
            if target is None:
                payload["id"] = store.next_id("project")
                payload.setdefault("status", "待开工")
                payload["pending"] = item.get("工程状态") != "已竣工"
                rows.append(payload)
            else:
                target.update(payload)

    @staticmethod
    def _match_row(
        rows: list[dict[str, Any]],
        *,
        code: Any,
        bridge_name: Any,
        bridge_code: str,
        name_field: str,
        code_field: str,
    ) -> dict[str, Any] | None:
        for row in rows:
            if row.get("桥梁编号") == bridge_code and code and row.get(code_field) == code:
                return row
        for row in rows:
            if row.get("桥梁编号") == bridge_code and bridge_name and str(row.get(name_field, "")) == str(bridge_name):
                return row
        return None

    # -------------------------------------------------------------- 批量审定
    def prepare_batch(
        self,
        archive_ids: list[int],
        payloads: dict[str, Any] | None = None,
        *,
        batch_token: str | None = None,
    ) -> dict[str, Any]:
        """批量审定第一步：整批冻结为待审定快照（版本快照锁）。

        全部档案校验通过才落快照；同一 batch_token 重试返回原批次，
        并带待审定游标，连接断开后据此继续。
        """
        payloads = payloads or {}
        with store.locked():
            if batch_token:
                existing = self._find_batch_by_token(batch_token)
                if existing is not None:
                    return self._batch_view(existing)

            batch_id = store.next_id(BATCHES)
            token = batch_token or f"batch-{batch_id}"
            archives = [_find_archive(archive_id) for archive_id in archive_ids]
            for archive in archives:
                self._ensure_submittable(archive)
                pending = _pending_snapshot(archive_id=int(archive["id"]))
                if pending is not None and pending.get("批量审定id") != batch_id:
                    raise ApprovalError(
                        f"桥梁档案 {archive['id']} 已有待审定快照，不能并入本批",
                    )

            with store.transaction(BATCHES, SNAPSHOTS, ARCHIVE):
                snapshot_ids: list[int] = []
                for archive in archives:
                    payload = payloads.get(str(archive["id"]), payloads.get(archive["桥梁编号"], {}))
                    snap = self._freeze_snapshot(
                        archive,
                        payload if isinstance(payload, dict) else {},
                        client_token=f"{token}:{archive['id']}",
                        batch_id=batch_id,
                    )
                    snapshot_ids.append(snap["id"])
                batch = {
                    "id": batch_id,
                    "batch_token": token,
                    "档案ids": list(archive_ids),
                    "快照ids": snapshot_ids,
                    "cursor": 0,
                    "审定状态": AUDIT_PENDING,
                    "创建时间": _now(),
                }
                store.rows(BATCHES).append(batch)
            return self._batch_view(batch)

    def approve_batch(self, batch_id: int, *, approver: str = "审定人") -> dict[str, Any]:
        """批量审定第二步：逐份审定，游标随成功份数推进，失败不影响已审定项。"""
        with store.locked():
            batch = self._get_batch(batch_id)
            results: list[dict[str, Any]] = []
            snapshot_ids = batch["快照ids"]
            for index, snapshot_id in enumerate(snapshot_ids):
                snap = store.find(SNAPSHOTS, snapshot_id)
                if snap is None:
                    ok, message = False, "快照缺失"
                elif snap["审定状态"] == AUDIT_PENDING:
                    try:
                        self.approve(snapshot_id, approver=f"{approver}-批量", client_token=f"{batch['batch_token']}:approve:{snapshot_id}")
                        ok, message = True, "已审定"
                    except ApprovalError as exc:
                        ok, message = False, exc.message
                else:
                    # 断连后重进：已审定/已发布的快照直接计入游标，只继续剩余待审定项。
                    ok, message = True, f"已{snap['审定状态']}"
                results.append({"档案序号": index, "快照id": snapshot_id, "ok": ok, "message": message})
            approved = sum(1 for item in results if item["ok"])
            batch["cursor"] = approved
            if approved == len(snapshot_ids):
                batch["审定状态"] = AUDIT_APPROVED
            return self._batch_view(batch, results=results)

    def publish_batch(self, batch_id: int) -> dict[str, Any]:
        """批量审定第三步：逐份发布；每份发布内部仍是四表同事务。"""
        with store.locked():
            batch = self._get_batch(batch_id)
            results: list[dict[str, Any]] = []
            for index, snapshot_id in enumerate(batch["快照ids"]):
                snap = store.find(SNAPSHOTS, snapshot_id)
                if snap is None:
                    results.append({"档案序号": index, "快照id": snapshot_id, "ok": False, "message": "快照缺失"})
                    continue
                if snap["审定状态"] == AUDIT_PUBLISHED:
                    results.append({"档案序号": index, "快照id": snapshot_id, "ok": True, "message": "已发布"})
                    continue
                try:
                    self.publish(snapshot_id, client_token=f"{batch['batch_token']}:publish:{snapshot_id}")
                    results.append({"档案序号": index, "快照id": snapshot_id, "ok": True, "message": "已发布"})
                except ApprovalError as exc:
                    results.append({"档案序号": index, "快照id": snapshot_id, "ok": False, "message": exc.message})
            if all(item["ok"] for item in results):
                batch["审定状态"] = AUDIT_PUBLISHED
            return self._batch_view(batch, results=results)

    def get_batch(self, batch_id: int) -> dict[str, Any]:
        return self._batch_view(self._get_batch(batch_id))

    def _get_batch(self, batch_id: int) -> dict[str, Any]:
        batch = store.find(BATCHES, batch_id)
        if batch is None:
            raise ApprovalError(f"批量审定批次 {batch_id} 不存在", http_status=404)
        return batch

    def _find_batch_by_token(self, token: str) -> dict[str, Any] | None:
        for row in store.rows(BATCHES):
            if row.get("batch_token") == token:
                return row
        return None

    def _batch_view(self, batch: dict[str, Any], *, results: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        snapshots = [store.find(SNAPSHOTS, snap_id) for snap_id in batch["快照ids"]]
        return {
            "批量审定id": batch["id"],
            "batch_token": batch["batch_token"],
            "审定状态": batch["审定状态"],
            "cursor": batch.get("cursor", 0),
            "总数": len(batch["快照ids"]),
            "待审定游标": [snap["id"] for snap in snapshots if snap and snap["审定状态"] == AUDIT_PENDING],
            "快照": [
                {
                    "快照id": snap["id"],
                    "档案id": snap["档案id"],
                    "桥梁编号": snap["桥梁编号"],
                    "版本号": snap["版本号"],
                    "审定状态": snap["审定状态"],
                }
                for snap in snapshots if snap
            ],
            "结果": results or [],
        }

    # -------------------------------------------------------------- 查询视图
    def list_archives(
        self,
        *,
        audit_status: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = [_archive_view(row) for row in store.rows(ARCHIVE)]
        if audit_status:
            rows = [row for row in rows if row["审定状态"] == audit_status]
        for row in rows:
            code = str(row.get("桥梁编号") or "")
            latest = _latest_published(code)
            row["审定版本"] = int(latest["版本号"]) if latest else int(row.get("审定版本", 0))
            row["证据链"] = self.evidence_summary(code)
        return rows

    def archive_detail(self, archive_id: int) -> dict[str, Any]:
        archive = _archive_view(_find_archive(archive_id))
        code = str(archive["桥梁编号"])
        versions = [
            {
                "快照id": snap["id"],
                "版本号": snap["版本号"],
                "审定状态": snap["审定状态"],
                "评定等级": snap.get("评定等级"),
                "提交时间": snap["提交时间"],
                "审定时间": snap["审定时间"],
                "发布时间": snap["发布时间"],
                "审定结论": snap["审定结论"],
            }
            for snap in sorted(store.rows(SNAPSHOTS), key=lambda item: item["id"])
            if snap["桥梁编号"] == code
        ]
        archive["版本历史"] = versions
        archive["证据链"] = self.evidence_chain(code)
        return archive

    def _get_snapshot(self, snapshot_id: int) -> dict[str, Any]:
        snap = store.find(SNAPSHOTS, snapshot_id)
        if snap is None:
            raise ApprovalError(f"审定快照 {snapshot_id} 不存在", http_status=404)
        return snap

    # ---------------------------------------------------------------- 证据链
    def evidence_summary(self, bridge_code: str) -> dict[str, Any]:
        drawings = [
            row for row in store.rows(DRAWING)
            if row.get("桥梁编号") == bridge_code and row.get("审定状态") == AUDIT_PUBLISHED
        ]
        inspections = self.visible_inspections(bridge_code)
        projects = self.visible_projects(bridge_code)
        records = [row for row in store.rows(RECORDS) if row["桥梁编号"] == bridge_code]
        return {
            "图面份数": len(drawings),
            "定检记录数": len(inspections),
            "工程待办数": len(projects),
            "最新批准记录id": max((int(row["id"]) for row in records), default=None),
        }

    def evidence_chain(self, bridge_code: str) -> dict[str, Any]:
        """同一根因沿同一条证据链复核：档案快照、批准记录、图面、定检、工程待办全挂在桥梁编号下。"""
        snapshots = [
            {
                "快照id": snap["id"],
                "版本号": snap["版本号"],
                "审定状态": snap["审定状态"],
                "评定等级": snap.get("评定等级"),
                "审定结论": snap.get("审定结论"),
                "提交时间": snap.get("提交时间"),
                "审定时间": snap.get("审定时间"),
                "发布时间": snap.get("发布时间"),
                "异常": snap.get("审定状态") == AUDIT_PENDING and self._has_conflict(snap),
            }
            for snap in sorted(store.rows(SNAPSHOTS), key=lambda item: item["id"])
            if snap["桥梁编号"] == bridge_code
        ]
        records = [dict(row) for row in sorted(store.rows(RECORDS), key=lambda item: item["id"]) if row["桥梁编号"] == bridge_code]
        return {
            "桥梁编号": bridge_code,
            "快照链": snapshots,
            "批准记录": records,
            "图面": [dict(row) for row in store.rows(DRAWING) if row.get("桥梁编号") == bridge_code],
            "定检记录": self.visible_inspections(bridge_code, include_hidden_marker=True),
            "工程待办": self.visible_projects(bridge_code, include_hidden_marker=True),
        }

    def _has_conflict(self, snap: dict[str, Any]) -> bool:
        latest = self._latest_record(str(snap["桥梁编号"]))
        if latest is None:
            return False
        for field in ("桥型结构", "设计荷载"):
            incoming = str(snap["档案数据"].get(field) or "")
            canonical = str(latest.get(f"裁决_{field}") or "")
            if incoming and canonical and incoming != canonical:
                return True
        return False

    # ---------------------------------------------------- 下游清单的快照可见性
    def visible_inspections(self, bridge_code: str, *, include_hidden_marker: bool = False) -> list[dict[str, Any]]:
        """定检清单只能读取已发布快照版本；待审定快照中的数据不出现。"""
        latest = _latest_published(bridge_code)
        visible_version = int(latest["版本号"]) if latest else None
        rows: list[dict[str, Any]] = []
        for row in store.rows("bridge"):
            if row.get("桥梁编号") != bridge_code:
                continue
            row_version = row.get("可见版本", row.get("快照版本"))
            if visible_version is None or (row_version is not None and int(row_version) <= int(visible_version)):
                rows.append(dict(row))
            elif include_hidden_marker:
                hidden = dict(row)
                hidden["锁定中"] = True
                rows.append(hidden)
        return rows

    def visible_projects(self, bridge_code: str, *, include_hidden_marker: bool = False) -> list[dict[str, Any]]:
        latest = _latest_published(bridge_code)
        visible_version = int(latest["版本号"]) if latest else None
        rows: list[dict[str, Any]] = []
        for row in store.rows("project"):
            if row.get("桥梁编号") != bridge_code:
                continue
            row_version = row.get("可见版本", row.get("快照版本"))
            if visible_version is None or (row_version is not None and int(row_version) <= int(visible_version)):
                rows.append(dict(row))
            elif include_hidden_marker:
                hidden = dict(row)
                hidden["锁定中"] = True
                rows.append(hidden)
        return rows


approval_service = BridgeApprovalService()
