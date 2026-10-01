"""桥梁档案审定领域服务。

把“审定状态机 + 版本快照锁 + 结论回写 + 并发裁决 + 证据链”全部收在这里，
路由层只做参数拆装。约定：

审定状态机（服务端强制，只能顺序推进，越步一律拒绝）::

    待审定 ──审定通过──▶ 已审定 ──发布──▶ 已发布
      ▲                    │                  │
      └──── 修订送审(仅已发布可再送审) ◀───────┘

版本快照锁：批量/单件送审先冻结一份不可变的“待审定快照”，图面、定检清单、
工程待办在审定发布前只能读到快照版本；审定通过时结论在同一事务回写三处，
发布后三处与档案一起翻成“已发布”。旧版本快照永久保留。

并发：全局锁串行化 + 乐观基线版本；冲突时以“最新批准记录”裁决桥型/荷载，
历史评定等级仍按各版本原快照保存。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "bridge_info"
INSPECTION_MODULE = "bridge"
VERSION_TABLE = "bridge_info_versions"
APPROVAL_TABLE = "bridge_info_approvals"
DRAWING_TABLE = "bridge_drawing"
TODO_TABLE = "project_todo"
BATCH_TABLE = "bridge_review_batch"

ARCHIVE_FIELDS = ["桥梁编号", "桥梁名称", "桥型结构", "跨径组合", "设计荷载", "建成年份", "上次评定等级", "桥梁状态"]
# 发生争议时由“最新批准记录”裁决的字段；评定等级不在其列，历史等级随原快照留存。
ARBITRATE_FIELDS = ["桥型结构", "设计荷载"]

STATE_PENDING = "待审定"
STATE_APPROVED = "已审定"
STATE_PUBLISHED = "已发布"
REVIEW_STATES = [STATE_PENDING, STATE_APPROVED, STATE_PUBLISHED]

PUBLISH_WAIT = "待发布"
PUBLISH_DONE = "已发布"


class ReviewError(Exception):
    """服务端业务拒绝：路由层据此映射 HTTP 状态码。"""

    def __init__(self, message: str, *, status: int = 409, extra: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.extra = extra or {}


class BridgeReviewService:
    def __init__(self) -> None:
        self._seq = 1000
        self.ensure_baselines()

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    def _tick(self) -> int:
        """单调递增序号，充当审定时间戳：越晚批准的记录序号越大。"""
        self._seq += 1
        return self._seq

    @staticmethod
    def _versions(archive_id: int) -> list[dict[str, Any]]:
        return [v for v in store.rows(VERSION_TABLE) if v["档案id"] == archive_id]

    @staticmethod
    def _approvals(archive_id: int) -> list[dict[str, Any]]:
        return [a for a in store.rows(APPROVAL_TABLE) if a["档案id"] == archive_id]

    def _archive(self, archive_id: int) -> dict[str, Any]:
        entry = store.find(MODULE, archive_id)
        if entry is None:
            raise ReviewError(f"桥梁档案 {archive_id} 不存在或已归档", status=404)
        return entry

    @staticmethod
    def _chain_id(code: str, version_no: int) -> str:
        return f"EC-{code}-v{version_no}"

    def _latest_version(self, archive_id: int) -> dict[str, Any] | None:
        versions = self._versions(archive_id)
        return max(versions, key=lambda v: v["版本号"], default=None)

    def _latest_approval(self, archive_id: int) -> dict[str, Any] | None:
        return max(self._approvals(archive_id), key=lambda a: a["审定时间"], default=None)

    def _find_batch(self, batch_no: str) -> dict[str, Any] | None:
        for batch in store.rows(BATCH_TABLE):
            if batch["批次号"] == batch_no:
                return batch
        return None

    # ------------------------------------------------------------------
    # 启动引导：给存量档案补一条 v1 已发布基线（图面/待办同批补齐）
    # ------------------------------------------------------------------
    def ensure_baselines(self) -> None:
        with store.lock:
            for row in store.rows(MODULE):
                if self._latest_version(int(row["id"])) is not None:
                    continue
                code = str(row.get("桥梁编号") or f"BRID-{int(row['id']):04d}")
                snapshot = {field: row.get(field) for field in ARCHIVE_FIELDS}
                seq = self._tick()
                chain_id = self._chain_id(code, 1)
                version = {
                    "id": store.next_id(VERSION_TABLE),
                    "档案id": int(row["id"]),
                    "版本号": 1,
                    "状态": STATE_PUBLISHED,
                    "快照": snapshot,
                    "审定结论": {"结论": "通过", "意见": "存量档案基线，初始化审定"},
                    "批次号": None,
                    "创建时间": seq,
                    "审定时间": seq,
                    "发布时间": seq,
                    "证据链id": chain_id,
                    "裁决": None,
                }
                approval = {
                    "id": store.next_id(APPROVAL_TABLE),
                    "档案id": int(row["id"]),
                    "版本号": 1,
                    "审定时间": seq,
                    "审定人": "系统基线",
                    "结论": "通过",
                    "桥型结构": snapshot.get("桥型结构"),
                    "设计荷载": snapshot.get("设计荷载"),
                    "上次评定等级": snapshot.get("上次评定等级"),
                    "冲突字段": [],
                    "证据链id": chain_id,
                }
                store.rows(VERSION_TABLE).append(version)
                store.rows(APPROVAL_TABLE).append(approval)
                store.rows(DRAWING_TABLE).append({
                    "id": store.next_id(DRAWING_TABLE),
                    "档案id": int(row["id"]),
                    "图号": f"DRAW-{code}",
                    "版本号": 1,
                    "桥型结构": snapshot.get("桥型结构"),
                    "设计荷载": snapshot.get("设计荷载"),
                    "上次评定等级": snapshot.get("上次评定等级"),
                    "审定结论": "通过",
                    "异常": False,
                    "发布状态": PUBLISH_DONE,
                    "证据链id": chain_id,
                    "来源批准记录": approval["id"],
                })
                # 存量定检记录按桥梁编号/名称挂到基线证据链，保证明细与定检页同链
                linked = 0
                for insp in store.rows(INSPECTION_MODULE):
                    if str(insp.get("桥梁编号") or "") == code or \
                       str(insp.get("桥梁名称") or "") == str(row.get("桥梁名称") or ""):
                        insp["桥梁编号"] = code
                        insp.update({
                            "档案版本号": 1,
                            "桥型结构": snapshot.get("桥型结构"),
                            "设计荷载": snapshot.get("设计荷载"),
                            "上次评定等级": snapshot.get("上次评定等级"),
                            "审定结论": "通过",
                            "异常": False,
                            "abnormal": False,
                            "发布状态": PUBLISH_DONE,
                            "证据链id": chain_id,
                            "来源批准记录": approval["id"],
                        })
                        linked += 1
                version["定检关联数"] = linked
                store.rows(TODO_TABLE).append({
                    "id": store.next_id(TODO_TABLE),
                    "工程编号": f"PROJ-TODO-{int(row['id']):04d}",
                    "档案id": int(row["id"]),
                    "工程名称": f"{snapshot.get('桥梁名称') or code} 养护工程待办",
                    "事项": "存量档案基线，无需处置",
                    "版本号": 1,
                    "状态": "已闭环",
                    "异常": False,
                    "发布状态": PUBLISH_DONE,
                    "证据链id": chain_id,
                    "来源批准记录": approval["id"],
                })
                row["审定状态"] = STATE_PUBLISHED
                row["当前版本号"] = 1
                row["证据链id"] = chain_id

    # ------------------------------------------------------------------
    # 版本视图：档案列表/详情、下游三处全部只读这一份视图
    # ------------------------------------------------------------------
    def _arbitration(self, archive_id: int, version: dict[str, Any]) -> dict[str, Any]:
        """返回该版本当前应采用的桥型/荷载裁决结果。

        - 已发布版本：自身批准记录就是最新权威；若之后又出现更新的批准记录
          （并发复审场景），桥型/荷载以最新记录为准并标注冲突字段；
        - 已审定待发布版本：与全局最新批准记录（按审定时间）比对，冲突即以
          最新记录裁决——这就是“冲突以最新审定记录裁决”，发布时按裁决落库；
        - 待审定版本：尚无批准记录，直接呈现快照申报值。
        评定等级不参与裁决，历史等级永远随各版本原快照保存。
        """
        latest = self._latest_approval(archive_id)
        snapshot = version["快照"]
        result: dict[str, Any] = {
            "裁决记录": latest["id"] if latest else None,
            "冲突字段": [],
            "被裁决": False,
        }
        for field in ARBITRATE_FIELDS:
            result[field] = snapshot.get(field)

        if version["状态"] == STATE_PENDING or latest is None:
            return result

        own = next((a for a in self._approvals(archive_id) if a["版本号"] == version["版本号"]), None)
        if own is not None and latest["id"] != own["id"]:
            conflicts = [f for f in ARBITRATE_FIELDS if latest.get(f) != own.get(f)]
            result["冲突字段"] = conflicts
            result["被裁决"] = bool(conflicts)
            for field in ARBITRATE_FIELDS:
                result[field] = latest.get(field)
        return result

    def version_view(self, archive_id: int) -> dict[str, Any]:
        """组装一条档案的当前有效版本视图（下游只能读快照版本，不读裸档案）。"""
        entry = self._archive(archive_id)
        version = self._latest_version(archive_id)
        if version is None:  # 理论上不会发生，基线引导已覆盖
            raise ReviewError(f"桥梁档案 {archive_id} 缺少版本快照", status=409)
        snapshot = version["快照"]
        view: dict[str, Any] = {
            "id": archive_id,
            **{field: snapshot.get(field) for field in ARCHIVE_FIELDS},
            "版本号": version["版本号"],
            "审定状态": version["状态"],
            "发布状态": PUBLISH_DONE if version["状态"] == STATE_PUBLISHED else PUBLISH_WAIT,
            "证据链id": version["证据链id"],
            "批次号": version.get("批次号"),
        }
        conclusion = version.get("审定结论")
        view["审定结论"] = conclusion.get("结论") if conclusion else None
        view["审定意见"] = conclusion.get("意见") if conclusion else None
        view["异常"] = bool(conclusion and conclusion.get("异常"))
        arbitration = self._arbitration(archive_id, version)
        view["裁决信息"] = arbitration
        for field in ARBITRATE_FIELDS:  # 桥型/荷载以裁决结果对外呈现
            view[field] = arbitration[field]
        latest_approval = self._latest_approval(archive_id)
        own_approval = next((a for a in self._approvals(archive_id) if a["版本号"] == version["版本号"]), None)
        view["最新批准记录"] = latest_approval["id"] if latest_approval else None
        view["来源批准记录"] = own_approval["id"] if own_approval else (latest_approval["id"] if latest_approval else None)
        view["pending"] = version["状态"] != STATE_PUBLISHED
        view["abnormal"] = view["异常"] or bool(entry.get("abnormal"))
        view["status"] = entry.get("status")
        return view

    def list_archives(
        self,
        *,
        keyword: str | None = None,
        review_status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        views = [self.version_view(int(row["id"])) for row in store.rows(MODULE)]
        if keyword:
            views = [v for v in views if keyword in str(v.get("桥梁编号") or "")]
        if review_status:
            views = [v for v in views if v["审定状态"] == review_status]
        total = len(views)
        start = max(page - 1, 0) * size
        page_rows = []
        for view in views[start:start + size]:
            view.pop("_entry", None)
            page_rows.append(view)
        return page_rows, total

    # ------------------------------------------------------------------
    # 登记：新档案即冻结 v1 待审定快照（重复提交不生成新档案）
    # ------------------------------------------------------------------
    def create_archive(
        self,
        values: dict[str, Any],
        *,
        idem_key: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        required = ["桥梁编号", "桥梁名称", "桥型结构"]
        missing = [field for field in required if not str(values.get(field) or "").strip()]
        if missing:
            raise ReviewError(f"缺少必填字段：{'、'.join(missing)}", status=400)
        with store.transaction():
            code = str(values["桥梁编号"]).strip()
            existing = next((r for r in store.rows(MODULE) if str(r.get("桥梁编号")) == code), None)
            if existing is not None:
                # 同一桥梁编号重复提交：直接回到既有版本，不再造新档案。
                if idem_key:
                    existing["_last幂等键"] = idem_key
                return self.version_view(int(existing["id"])), "桥梁编号已存在，重复提交未生成新档案"
            entry = {"id": store.next_id(MODULE)}
            entry.update({field: (str(values[field]).strip() if field in required else values.get(field)) for field in ARCHIVE_FIELDS})
            entry["status"] = "正常"
            entry["pending"] = True
            entry["abnormal"] = False
            entry["审定状态"] = STATE_PENDING
            entry["当前版本号"] = 1
            entry["证据链id"] = self._chain_id(code, 1)
            store.rows(MODULE).append(entry)
            seq = self._tick()
            version = {
                "id": store.next_id(VERSION_TABLE),
                "档案id": entry["id"],
                "版本号": 1,
                "状态": STATE_PENDING,
                "快照": {field: entry.get(field) for field in ARCHIVE_FIELDS},
                "审定结论": None,
                "批次号": None,
                "创建时间": seq,
                "审定时间": None,
                "发布时间": None,
                "证据链id": entry["证据链id"],
                "裁决": None,
            }
            store.rows(VERSION_TABLE).append(version)
            return self.version_view(entry["id"]), "桥梁已登记并冻结待审定快照"

    # ------------------------------------------------------------------
    # 送审：已发布才能修订送审；待审定可覆盖送审（同一版本，不新增）
    # ------------------------------------------------------------------
    def submit_review(self, archive_id: int, values: dict[str, Any]) -> dict[str, Any]:
        expected = values.get("expected_version")
        with store.transaction():
            entry = self._archive(archive_id)
            current = self._latest_version(archive_id)
            if expected is not None and int(expected) != int(entry.get("当前版本号", 1)):
                latest = self._latest_approval(archive_id)
                raise ReviewError(
                    f"基线版本不一致：页面基于 v{expected}，档案当前为 v{entry.get('当前版本号')}",
                    extra={"最新批准记录": latest and latest["id"]},
                )
            code = str(entry.get("桥梁编号"))
            if current["状态"] == STATE_APPROVED:
                raise ReviewError("档案已审定，等待发布；不能越过发布重复送审")
            patch = {field: values.get(field) for field in ARCHIVE_FIELDS if values.get(field) is not None}
            if current["状态"] == STATE_PENDING:
                current["快照"].update(patch)
                current["创建时间"] = self._tick()
            else:  # 已发布 → 开出新版本
                new_no = current["版本号"] + 1
                snapshot = {field: current["快照"].get(field) for field in ARCHIVE_FIELDS}
                snapshot.update(patch)
                seq = self._tick()
                chain_id = self._chain_id(code, new_no)
                current = {
                    "id": store.next_id(VERSION_TABLE),
                    "档案id": archive_id,
                    "版本号": new_no,
                    "状态": STATE_PENDING,
                    "快照": snapshot,
                    "审定结论": None,
                    "批次号": None,
                    "创建时间": seq,
                    "审定时间": None,
                    "发布时间": None,
                    "证据链id": chain_id,
                    "裁决": None,
                }
                store.rows(VERSION_TABLE).append(current)
                entry["审定状态"] = STATE_PENDING
                entry["当前版本号"] = new_no
                entry["证据链id"] = chain_id
            return self.version_view(archive_id)

    # ------------------------------------------------------------------
    # 审定通过：待审定 → 已审定，结论同事务回写图面/定检/工程待办
    # ------------------------------------------------------------------
    def _conclusion_payload(self, values: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
        passed = str(values.get("结论") or "通过").strip()
        return {
            "结论": "通过" if passed in ("通过", "合格", "同意") else "不通过",
            "意见": str(values.get("意见") or "").strip(),
            "审定人": str(values.get("审定人") or "值班管理员").strip(),
            "异常": bool(values.get("异常")),
            "桥型结构": values.get("桥型结构") or snapshot.get("桥型结构"),
            "设计荷载": values.get("设计荷载") or snapshot.get("设计荷载"),
            "上次评定等级": values.get("上次评定等级") or snapshot.get("上次评定等级"),
        }

    def _approve_version(
        self,
        entry: dict[str, Any],
        version: dict[str, Any],
        conclusion: dict[str, Any],
        *,
        batch_no: str | None,
    ) -> dict[str, Any]:
        """把一个待审定快照推进为已审定，并三处回写；调用方负责事务与状态校验。"""
        archive_id = int(entry["id"])
        prior = self._latest_approval(archive_id)
        if next((a for a in self._approvals(archive_id) if a["版本号"] == version["版本号"]), None) is not None:
            raise ReviewError(
                f"v{version['版本号']} 已被并发审定抢先批准，本次提交以最新批准记录为准",
                extra={"最新批准记录": prior and prior["id"]},
            )
        seq = self._tick()
        chain_id = version["证据链id"]
        conflicts = [f for f in ARBITRATE_FIELDS if prior and conclusion.get(f) != prior.get(f)]
        approval = {
            "id": store.next_id(APPROVAL_TABLE),
            "档案id": archive_id,
            "版本号": version["版本号"],
            "审定时间": seq,
            "审定人": conclusion["审定人"],
            "结论": conclusion["结论"],
            "桥型结构": conclusion["桥型结构"],
            "设计荷载": conclusion["设计荷载"],
            "上次评定等级": conclusion["上次评定等级"],
            "冲突字段": conflicts,
            "证据链id": chain_id,
            "批次号": batch_no,
        }
        store.rows(APPROVAL_TABLE).append(approval)
        version["状态"] = STATE_APPROVED
        version["审定时间"] = seq
        version["批次号"] = batch_no
        version["审定结论"] = {k: v for k, v in conclusion.items()}
        version["裁决"] = {"冲突字段": conflicts, "先前批准记录": prior and prior["id"]}
        entry["审定状态"] = STATE_APPROVED
        self._writeback(entry, version, approval, PUBLISH_WAIT)
        return approval

    @staticmethod
    def _approved_values(version: dict[str, Any]) -> dict[str, Any]:
        """本版本批准记录自身的桥型/荷载（回写忠实于该次批准，不做裁决）。"""
        conclusion = version.get("审定结论") or {}
        return {
            "桥型结构": conclusion.get("桥型结构", version["快照"].get("桥型结构")),
            "设计荷载": conclusion.get("设计荷载", version["快照"].get("设计荷载")),
        }

    def _writeback(
        self,
        entry: dict[str, Any],
        version: dict[str, Any],
        approval: dict[str, Any],
        publish_state: str,
    ) -> None:
        """结论回写图面、定检清单、工程待办；与档案版本同一事务落库。

        回写内容忠实于本版本批准记录；对外读视图的桥型/荷载裁决由
        ``_arbitration`` 负责，历史等级始终取版本原快照。
        """
        archive_id = int(entry["id"])
        conclusion = version["审定结论"] or {}
        chain_id = version["证据链id"]
        approved = self._approved_values(version)
        bridge_type = approved["桥型结构"]
        load = approved["设计荷载"]
        grade = version["快照"].get("上次评定等级")  # 历史等级按原快照保存
        abnormal = bool(conclusion.get("异常"))
        common = {
            "版本号": version["版本号"],
            "桥型结构": bridge_type,
            "设计荷载": load,
            "上次评定等级": grade,
            "审定结论": conclusion.get("结论"),
            "异常": abnormal,
            "发布状态": publish_state,
            "证据链id": chain_id,
            "来源批准记录": approval["id"],
        }

        # 图面：一桥一图，更新当前图面行
        drawing = next((d for d in store.rows(DRAWING_TABLE) if d["档案id"] == archive_id), None)
        if drawing is None:
            drawing = {"id": store.next_id(DRAWING_TABLE), "档案id": archive_id,
                       "图号": f"DRAW-{entry.get('桥梁编号')}"}
            store.rows(DRAWING_TABLE).append(drawing)
        drawing.update(common)

        # 定检清单：按桥梁编号（其次桥梁名称）关联，全部沿同一证据链回写
        code = str(entry.get("桥梁编号") or "")
        name = str(version["快照"].get("桥梁名称") or "")
        linked = 0
        for row in store.rows(INSPECTION_MODULE):
            marker = str(row.get("桥梁编号") or "")
            if (code and marker == code) or (name and str(row.get("桥梁名称") or "") == name):
                row["桥梁编号"] = code or marker
                row.update({
                    "档案版本号": common["版本号"],
                    "桥型结构": bridge_type,
                    "设计荷载": load,
                    "上次评定等级": grade,
                    "审定结论": conclusion.get("结论"),
                    "异常": abnormal,
                    "abnormal": abnormal,
                    "发布状态": publish_state,
                    "证据链id": chain_id,
                    "来源批准记录": approval["id"],
                })
                linked += 1
        version["定检关联数"] = linked

        # 工程待办：异常结论开待办，正常结论闭环；每版本留痕，视图只读当前版本那条
        todo = next((t for t in store.rows(TODO_TABLE)
                     if t["档案id"] == archive_id and t["版本号"] == version["版本号"]), None)
        if todo is None:
            todo = {
                "id": store.next_id(TODO_TABLE),
                "工程编号": f"PROJ-TODO-{archive_id:04d}-v{version['版本号']}",
                "档案id": archive_id,
                "工程名称": f"{name or code} 养护工程待办",
            }
            store.rows(TODO_TABLE).append(todo)
        todo.update({
            "事项": conclusion.get("意见") or ("结论异常，需安排处置" if abnormal else "审定通过，常规跟踪"),
            "版本号": common["版本号"],
            "状态": "待办" if abnormal else "已闭环",
            "异常": abnormal,
            "发布状态": publish_state,
            "证据链id": chain_id,
            "来源批准记录": approval["id"],
        })

    def approve_review(self, archive_id: int, values: dict[str, Any]) -> dict[str, Any]:
        expected = values.get("expected_version")
        with store.transaction():
            entry = self._archive(archive_id)
            version = self._latest_version(archive_id)
            if version["状态"] != STATE_PENDING:
                raise ReviewError(
                    f"档案当前为「{version['状态']}」，只有待审定快照可以审定通过，不能跳步"
                )
            if expected is not None and int(expected) != version["版本号"]:
                latest = self._latest_approval(archive_id)
                raise ReviewError(
                    f"并发冲突：送审基线为 v{expected}，当前待审定版本为 v{version['版本号']}",
                    extra={"最新批准记录": latest and latest["id"]},
                )
            conclusion = self._conclusion_payload(values, version["快照"])
            self._approve_version(entry, version, conclusion, batch_no=None)
            return self.version_view(archive_id)

    # ------------------------------------------------------------------
    # 发布：已审定 → 已发布；档案与三处回写同时翻牌
    # ------------------------------------------------------------------
    def publish_review(self, archive_id: int, values: dict[str, Any] | None = None) -> dict[str, Any]:
        values = values or {}
        expected = values.get("expected_version")
        with store.transaction():
            entry = self._archive(archive_id)
            version = self._latest_version(archive_id)
            if version["状态"] == STATE_PENDING:
                raise ReviewError("档案尚未审定通过，服务端拒绝越过审定直接发布")
            if version["状态"] == STATE_PUBLISHED:
                raise ReviewError("该版本已经发布，请勿重复发布")
            if expected is not None and int(expected) != version["版本号"]:
                latest = self._latest_approval(archive_id)
                raise ReviewError(
                    f"并发冲突：请求发布 v{expected}，当前待发布版本为 v{version['版本号']}",
                    extra={"最新批准记录": latest and latest["id"]},
                )
            approval = next((a for a in self._approvals(archive_id) if a["版本号"] == version["版本号"]), None)
            if approval is None:
                raise ReviewError("缺少批准记录，发布被拒绝")
            seq = self._tick()
            version["状态"] = STATE_PUBLISHED
            version["发布时间"] = seq
            arbitration = self._arbitration(archive_id, version)
            snapshot = version["快照"]
            # 档案落库：非争议字段取本版本原快照；桥型/荷载取最新批准记录裁决，
            # 历史评定等级始终保留快照原值，不被新裁决覆盖。
            entry["当前版本号"] = version["版本号"]
            entry["审定状态"] = STATE_PUBLISHED
            entry["证据链id"] = version["证据链id"]
            for field in ARCHIVE_FIELDS:
                entry[field] = snapshot.get(field)
            for field in ARBITRATE_FIELDS:
                entry[field] = arbitration[field]
            entry["pending"] = False
            self._publish_writeback(archive_id, version)
            return self.version_view(archive_id)

    def _publish_writeback(self, archive_id: int, version: dict[str, Any]) -> None:
        chain_id = version["证据链id"]
        arbitration = self._arbitration(archive_id, version)
        # 桥型/荷载冲突已在发布时按最新批准记录裁决：三处回写行同步裁决值
        ruled = {field: arbitration[field] for field in ARBITRATE_FIELDS}
        for table in (DRAWING_TABLE, TODO_TABLE):
            for row in store.rows(table):
                if row.get("档案id") == archive_id and row.get("版本号") == version["版本号"]:
                    row["发布状态"] = PUBLISH_DONE
                    row["证据链id"] = chain_id
                    row.update(ruled)
        code = version["快照"].get("桥梁编号")
        for row in store.rows(INSPECTION_MODULE):
            if row.get("档案版本号") == version["版本号"] and (
                row.get("证据链id") == chain_id or (code and str(row.get("桥梁编号") or "") == str(code))
            ):
                row["发布状态"] = PUBLISH_DONE
                row.update(ruled)

    # ------------------------------------------------------------------
    # 批量审定：版本快照锁 + 待审定游标断点续传 + 整版原子
    # ------------------------------------------------------------------
    def submit_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        batch_no = str(payload.get("batch_no") or "").strip()
        if not batch_no:
            raise ReviewError("缺少批次号 batch_no，断点续传需要稳定批次号", status=400)
        raw_items = payload.get("items") or []
        if not isinstance(raw_items, list) or not raw_items:
            raise ReviewError("批量审定条目不能为空", status=400)
        with store.transaction():
            existing = self._find_batch(batch_no)
            if existing is not None:
                return self._resume_batch(existing, raw_items)
            batch = {
                "id": store.next_id(BATCH_TABLE),
                "批次号": batch_no,
                "状态": STATE_PENDING,
                "创建时间": self._tick(),
                "游标": 0,
                "条目": [],
            }
            store.rows(BATCH_TABLE).append(batch)
            self._ingest_batch_items(batch, raw_items)
            return self._batch_view(batch)

    def _resume_batch(self, batch: dict[str, Any], raw_items: list[dict[str, Any]]) -> dict[str, Any]:
        """连接断开后凭批次号从待审定游标继续；已收录条目幂等跳过，不生成新版本。"""
        if batch["状态"] != STATE_PENDING:
            return self._batch_view(batch)
        done_ids = {int(item["档案id"]) for item in batch["条目"]}
        remaining = [item for item in raw_items if int(item.get("档案id") or 0) not in done_ids]
        # 游标只前进：重复提交已经收录的条目时直接确认，不再快照第二遍。
        batch["游标"] = len(batch["条目"])
        self._ingest_batch_items(batch, remaining)
        return self._batch_view(batch)

    def _ingest_batch_items(self, batch: dict[str, Any], raw_items: list[dict[str, Any]]) -> None:
        for raw in raw_items:
            archive_id = int(raw.get("档案id") or 0)
            entry = self._archive(archive_id)
            version = self._latest_version(archive_id)
            if version["状态"] == STATE_APPROVED:
                raise ReviewError(
                    f"桥梁 {entry.get('桥梁编号')} 已审定待发布，请先发布再纳入下一批",
                )
            locked = next((b for b in store.rows(BATCH_TABLE)
                           if b["状态"] == STATE_PENDING and b["批次号"] != batch["批次号"]
                           and any(int(i["档案id"]) == archive_id for i in b["条目"])), None)
            if locked is not None:
                raise ReviewError(f"桥梁 {entry.get('桥梁编号')} 已被批次 {locked['批次号']} 锁定")
            expected = raw.get("expected_version")
            if expected is not None and int(expected) != version["版本号"]:
                raise ReviewError(
                    f"桥梁 {entry.get('桥梁编号')} 基线版本不一致（页面 v{expected} / 当前 v{version['版本号']}）"
                )
            if version["状态"] == STATE_PENDING and version.get("批次号") == batch["批次号"]:
                target = version  # 同批重传，沿用原快照
            elif version["状态"] == STATE_PUBLISHED:
                target = self._open_batch_version(entry, version, raw, batch["批次号"])
            elif version["状态"] == STATE_PENDING and version.get("批次号") is None:
                # 单件已送审的待审定快照允许并入本批
                version["批次号"] = batch["批次号"]
                snapshot_patch = {f: raw.get(f) for f in ARCHIVE_FIELDS if raw.get(f) is not None}
                version["快照"].update(snapshot_patch)
                target = version
            else:
                raise ReviewError(f"桥梁 {entry.get('桥梁编号')} 存在未完成审定，无法纳入本批")
            target["_批量申报"] = {
                "结论": str(raw.get("结论") or "通过"),
                "意见": str(raw.get("意见") or ""),
                "审定人": str(raw.get("审定人") or "批量审定"),
                "异常": bool(raw.get("异常")),
                "桥型结构": raw.get("桥型结构") or target["快照"].get("桥型结构"),
                "设计荷载": raw.get("设计荷载") or target["快照"].get("设计荷载"),
                "上次评定等级": raw.get("上次评定等级") or target["快照"].get("上次评定等级"),
            }
            batch["条目"].append({"档案id": archive_id, "版本号": target["版本号"], "状态": "已锁定"})
            batch["游标"] = len(batch["条目"])

    def _open_batch_version(self, entry: dict[str, Any], base: dict[str, Any],
                            raw: dict[str, Any], batch_no: str) -> dict[str, Any]:
        new_no = base["版本号"] + 1
        snapshot = {field: base["快照"].get(field) for field in ARCHIVE_FIELDS}
        snapshot.update({field: raw.get(field) for field in ARCHIVE_FIELDS if raw.get(field) is not None})
        seq = self._tick()
        chain_id = self._chain_id(str(entry.get("桥梁编号")), new_no)
        version = {
            "id": store.next_id(VERSION_TABLE),
            "档案id": int(entry["id"]),
            "版本号": new_no,
            "状态": STATE_PENDING,
            "快照": snapshot,
            "审定结论": None,
            "批次号": batch_no,
            "创建时间": seq,
            "审定时间": None,
            "发布时间": None,
            "证据链id": chain_id,
            "裁决": None,
        }
        store.rows(VERSION_TABLE).append(version)
        entry["审定状态"] = STATE_PENDING
        entry["当前版本号"] = new_no
        entry["证据链id"] = chain_id
        return version

    def get_batch(self, batch_no: str) -> dict[str, Any]:
        batch = self._find_batch(batch_no)
        if batch is None:
            raise ReviewError(f"批次 {batch_no} 不存在", status=404)
        return self._batch_view(batch)

    def _batch_view(self, batch: dict[str, Any]) -> dict[str, Any]:
        items = []
        for item in batch["条目"]:
            archive_id = int(item["档案id"])
            version = next((v for v in self._versions(archive_id) if v["版本号"] == item["版本号"]), None)
            items.append({
                "档案id": archive_id,
                "版本号": item["版本号"],
                "状态": (version or {}).get("状态", item["状态"]),
                "桥梁编号": (version or {}).get("快照", {}).get("桥梁编号"),
                "桥梁名称": (version or {}).get("快照", {}).get("桥梁名称"),
                "证据链id": (version or {}).get("证据链id"),
            })
        return {
            "批次号": batch["批次号"],
            "状态": batch["状态"],
            "游标": batch["游标"],
            "条目数": len(batch["条目"]),
            "items": items,
        }

    def approve_batch(self, batch_no: str, values: dict[str, Any] | None = None) -> dict[str, Any]:
        """整批审定通过：任一条目失败整批回滚，只接受一个完整版本。"""
        values = values or {}
        reviewer = str(values.get("审定人") or "批量审定").strip()
        with store.transaction():
            batch = self._find_batch(batch_no)
            if batch is None:
                raise ReviewError(f"批次 {batch_no} 不存在", status=404)
            if batch["状态"] == STATE_APPROVED:
                raise ReviewError("批次已审定，等待发布，请勿重复审定")
            if batch["状态"] == STATE_PUBLISHED:
                raise ReviewError("批次已发布，无需再次审定")
            if batch["游标"] != len(batch["条目"]) or not batch["条目"]:
                raise ReviewError("批次快照未收录完整，请从待审定游标续传后再审定")
            for item in batch["条目"]:
                archive_id = int(item["档案id"])
                entry = self._archive(archive_id)
                version = next(v for v in self._versions(archive_id) if v["版本号"] == item["版本号"])
                if version["状态"] != STATE_PENDING:
                    raise ReviewError("批次内存在非待审定版本，整批已回滚")
                payload = version.pop("_批量申报", None) or {}
                payload.setdefault("审定人", reviewer)
                conclusion = self._conclusion_payload(payload, version["快照"])
                # 同一待审定版本若已被并发单件审定抢先批准，则整批拒绝（只接受一个版本）；
                # 桥型/荷载冲突由 _approve_version 对照最新批准记录登记，读视图按最新记录裁决。
                self._approve_version(entry, version, conclusion, batch_no=batch_no)
                item["状态"] = STATE_APPROVED
            batch["状态"] = STATE_APPROVED
            batch["审定时间"] = self._tick()
            return self._batch_view(batch)

    def publish_batch(self, batch_no: str) -> dict[str, Any]:
        with store.transaction():
            batch = self._find_batch(batch_no)
            if batch is None:
                raise ReviewError(f"批次 {batch_no} 不存在", status=404)
            if batch["状态"] == STATE_PENDING:
                raise ReviewError("批次尚未审定通过，服务端拒绝越过审定发布")
            if batch["状态"] == STATE_PUBLISHED:
                raise ReviewError("批次已发布，请勿重复发布")
            for item in batch["条目"]:
                view = self.publish_review(int(item["档案id"]), {"expected_version": item["版本号"]})
                item["状态"] = view["审定状态"]
            batch["状态"] = STATE_PUBLISHED
            batch["发布时间"] = self._tick()
            return self._batch_view(batch)

    # ------------------------------------------------------------------
    # 图面 / 工程待办清单（读视图，同样只呈现当前快照版本对应行）
    # ------------------------------------------------------------------
    def list_drawings(self, *, keyword: str | None = None) -> list[dict[str, Any]]:
        result = []
        for entry in store.rows(MODULE):
            view = self.version_view(int(entry["id"]))
            drawing = next((d for d in store.rows(DRAWING_TABLE)
                            if d["档案id"] == int(entry["id"]) and d["版本号"] == view["版本号"]), None)
            if drawing is None:
                # 待审定快照尚未审定回写：投影一份“只读快照”占位，锁定期不得读到旧版回写
                if view["审定状态"] != STATE_PENDING:
                    continue
                drawing = {
                    "id": None,
                    "档案id": int(entry["id"]),
                    "图号": f"DRAW-{view.get('桥梁编号')}",
                    "版本号": view["版本号"],
                    "桥型结构": view.get("桥型结构"),
                    "设计荷载": view.get("设计荷载"),
                    "上次评定等级": view.get("上次评定等级"),
                    "审定结论": None,
                    "异常": False,
                    "发布状态": PUBLISH_WAIT,
                    "证据链id": view["证据链id"],
                    "来源批准记录": None,
                }
            row = dict(drawing)
            row["桥梁编号"] = view.get("桥梁编号")
            row["桥梁名称"] = view.get("桥梁名称")
            row["审定状态"] = view["审定状态"]
            if keyword and keyword not in str(row.get("图号") or "") and keyword not in str(row.get("桥梁编号") or ""):
                continue
            result.append(row)
        return result

    def list_todos(self, *, keyword: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        result = []
        for entry in store.rows(MODULE):
            view = self.version_view(int(entry["id"]))
            todo = next((t for t in store.rows(TODO_TABLE)
                         if t["档案id"] == int(entry["id"]) and t["版本号"] == view["版本号"]), None)
            if todo is None:
                if view["审定状态"] != STATE_PENDING:
                    continue
                todo = {
                    "id": None,
                    "工程编号": f"PROJ-TODO-{int(entry['id']):04d}-v{view['版本号']}",
                    "档案id": int(entry["id"]),
                    "工程名称": f"{view.get('桥梁名称') or view.get('桥梁编号')} 养护工程待办",
                    "事项": "待审定快照锁定中，结论待回写",
                    "版本号": view["版本号"],
                    "状态": "待办",
                    "异常": False,
                    "发布状态": PUBLISH_WAIT,
                    "证据链id": view["证据链id"],
                    "来源批准记录": None,
                }
            row = dict(todo)
            row["桥梁编号"] = view.get("桥梁编号")
            row["桥梁名称"] = view.get("桥梁名称")
            row["审定状态"] = view["审定状态"]
            if keyword and keyword not in str(row.get("工程编号") or "") and keyword not in str(row.get("桥梁编号") or ""):
                continue
            if status and row.get("状态") != status:
                continue
            result.append(row)
        return result

    # ------------------------------------------------------------------
    # 证据链：档案明细与定检页面沿同一条链复核，两处验收口径一致
    # ------------------------------------------------------------------
    def match_inspection_rows(self, archive_id: int) -> list[dict[str, Any]]:
        entry = self._archive(archive_id)
        code = str(entry.get("桥梁编号") or "")
        version = self._latest_version(archive_id)
        name = str(version["快照"].get("桥梁名称") or "")
        rows = []
        for row in store.rows(INSPECTION_MODULE):
            if (code and str(row.get("桥梁编号") or "") == code) or \
               (name and str(row.get("桥梁名称") or "") == name):
                rows.append(row)
        return rows

    def evidence_chain(self, archive_id: int) -> dict[str, Any]:
        view = self.version_view(archive_id)
        version = self._latest_version(archive_id)
        chain_id = version["证据链id"]
        approval = next((a for a in self._approvals(archive_id) if a["版本号"] == version["版本号"]), None)
        drawing = next((d for d in store.rows(DRAWING_TABLE)
                        if d["档案id"] == archive_id and d["版本号"] == version["版本号"]), None)
        todo = next((t for t in store.rows(TODO_TABLE)
                     if t["档案id"] == archive_id and t["版本号"] == version["版本号"]), None)
        inspections = [dict(r) for r in self.match_inspection_rows(archive_id)]

        issues: list[str] = []
        expect_publish = PUBLISH_DONE if version["状态"] == STATE_PUBLISHED else PUBLISH_WAIT

        def _check_link(label: str, linked: dict[str, Any] | None) -> None:
            if linked is None:
                if version["状态"] == STATE_PENDING:
                    return  # 待审定快照尚未回写，不算断链
                issues.append(f"{label}缺少版本 v{version['版本号']} 的回写记录")
                return
            if linked.get("证据链id") != chain_id:
                issues.append(f"{label}证据链不一致：档案为 {chain_id}，{label}为 {linked.get('证据链id')}")
            if linked.get("发布状态") != expect_publish:
                issues.append(f"{label}发布状态不一致：应为{expect_publish}，实际为 {linked.get('发布状态')}")
            if linked.get("异常") != view["异常"]:
                issues.append(f"{label}异常标记与档案审定结论不一致")

        _check_link("图面", drawing)
        for idx, row in enumerate(inspections, 1):
            _check_link(f"定检清单第{idx}条", row)
        _check_link("工程待办", todo)

        return {
            "档案id": archive_id,
            "桥梁编号": view.get("桥梁编号"),
            "桥梁名称": view.get("桥梁名称"),
            "版本号": version["版本号"],
            "审定状态": version["状态"],
            "发布状态": view["发布状态"],
            "证据链id": chain_id,
            "异常": view["异常"],
            "裁决信息": view["裁决信息"],
            "版本快照": {
                "创建时间": version["创建时间"],
                "审定时间": version.get("审定时间"),
                "发布时间": version.get("发布时间"),
                "历史评定等级": version["快照"].get("上次评定等级"),
                "审定结论": version.get("审定结论"),
            },
            "批准记录": approval,
            "图面": drawing,
            "定检记录": inspections,
            "工程待办": todo,
            "一致": not issues,
            "异常项": issues,
        }

    def evidence_for_inspection(self, inspection_id: int) -> dict[str, Any]:
        """定检页面入口：由定检记录反查桥梁档案，沿同一条证据链复核。"""
        row = store.find(INSPECTION_MODULE, inspection_id)
        if row is None:
            raise ReviewError(f"定检记录 {inspection_id} 不存在或已归档", status=404)
        code = str(row.get("桥梁编号") or "")
        name = str(row.get("桥梁名称") or "")
        archive = next((r for r in store.rows(MODULE)
                        if (code and str(r.get("桥梁编号") or "") == code)
                        or (name and str(self._latest_version(int(r["id"]))["快照"].get("桥梁名称") or "") == name)), None)
        if archive is None:
            raise ReviewError(f"定检记录 {inspection_id} 尚未关联桥梁档案", status=404)
        result = self.evidence_chain(int(archive["id"]))
        result["定检记录id"] = inspection_id
        return result

    def inspection_view(self, row: dict[str, Any]) -> dict[str, Any]:
        """定检清单列表行：叠加档案快照版本字段（只能读快照版本）。"""
        result = dict(row)
        code = str(row.get("桥梁编号") or "")
        name = str(row.get("桥梁名称") or "")
        archive = None
        for cand in store.rows(MODULE):
            version = self._latest_version(int(cand["id"]))
            if (code and str(cand.get("桥梁编号") or "") == code) or \
               (name and str(version["快照"].get("桥梁名称") or "") == name):
                archive = cand
                break
        if archive is not None:
            view = self.version_view(int(archive["id"]))
            result["档案版本号"] = row.get("档案版本号", view["版本号"])
            result["档案审定状态"] = view["审定状态"]
            result["发布状态"] = row.get("发布状态", view["发布状态"])
            result["证据链id"] = row.get("证据链id", view["证据链id"])
            result["来源批准记录"] = row.get("来源批准记录", view["来源批准记录"])
            result["桥型结构"] = row.get("桥型结构", view.get("桥型结构"))
            result["设计荷载"] = row.get("设计荷载", view.get("设计荷载"))
            result["上次评定等级"] = row.get("上次评定等级", view.get("上次评定等级"))
            result["异常"] = row.get("异常", view["异常"])
        return result


review_service = BridgeReviewService()
