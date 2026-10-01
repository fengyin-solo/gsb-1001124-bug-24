"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

桥梁档案审定相关的版本表、批准记录、图面、工程待办与批量审定批次也都挂在这里，
并通过 ``transaction()`` 提供“同一事务落库”的语义：事务内任意一步抛错，
本次改动的所有表都会整体回滚到进入事务前的状态。
"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS

# 桥梁档案审定流转涉及的扩展表（不参与通用模块列表，仅由审定服务读写）。
REVIEW_TABLES = [
    "bridge_info_versions",   # 档案版本快照（不可变，旧快照长期保留）
    "bridge_info_approvals",  # 批准/裁决记录（冲突以最新一条为准）
    "bridge_drawing",         # 图面（结论回写对象之一）
    "project_todo",           # 工程待办（结论回写对象之一）
    "bridge_review_batch",    # 批量审定批次（版本快照锁 + 待审定游标）
]


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tables: dict[str, list[dict[str, Any]]] = {}
        self._txn_depth = 0
        self._txn_backup: dict[str, list[dict[str, Any]]] | None = None
        self._reset_tables()

    def _reset_tables(self) -> None:
        """按种子数据重建全部表；调用方需自行持锁。"""
        self._tables = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        for name in REVIEW_TABLES:
            self._tables.setdefault(name, [])

    def reset(self) -> None:
        """回到初始种子状态（测试隔离用），审定基线由服务层重新引导。"""
        with self._lock:
            self._txn_depth = 0
            self._txn_backup = None
            self._reset_tables()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def module_names(self) -> list[str]:
        return sorted(name for name in self._tables if name not in REVIEW_TABLES)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """档案审定事务：进入时备份全表，异常时整体回滚，正常时提交。

        - 图面、定检清单、工程待办与档案版本必须落在同一事务里；
        - 批量审定要求“只接受一个完整版本”，任一条目失败即整批回滚；
        - 支持嵌套（如整批发布复用单件发布）：只在最外层做一次备份/提交，
          内层事务的异常仍会冒泡到最外层并触发整体回滚。
        """
        with self._lock:
            if self._txn_depth == 0:
                self._txn_backup = copy.deepcopy(self._tables)
            self._txn_depth += 1
            try:
                yield
            except BaseException:
                if self._txn_depth == 1:
                    # 最外层捕获：恢复到事务前的整体快照
                    self._tables = self._txn_backup  # type: ignore[assignment]
                    self._txn_backup = None
                self._txn_depth -= 1
                raise
            else:
                self._txn_depth -= 1
                if self._txn_depth == 0:
                    self._txn_backup = None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
