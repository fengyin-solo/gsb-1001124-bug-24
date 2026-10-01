"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
并发审定与“同一事务落库”在内存层用一把可重入锁 + 表快照回滚模拟，
换成数据库时把 ``locked`` / ``transaction`` 替换成行锁与 BEGIN/COMMIT 即可。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from copy import deepcopy
from typing import Any, Iterator

from app.seed import SEED_ROWS

# 审批流内部使用的台账表，不计入运营概览的业务模块数。
INTERNAL_MODULES = {
    "bridge_drawing",            # 图面（按桥梁编号 + 版本保存）
    "bridge_approval_snapshot",  # 档案版本快照（含旧快照，长期保留）
    "bridge_approval_record",    # 批准记录（冲突时以最新一条裁决）
    "bridge_approval_batch",     # 批量审定游标（断连后继续）
}


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._lock = threading.RLock()

    @contextmanager
    def locked(self) -> Iterator[None]:
        """串行化审定/发布等关键区，保证并发审定只有一个版本落库。"""
        with self._lock:
            yield

    @contextmanager
    def transaction(self, *modules: str) -> Iterator[None]:
        """档案、图面、定检清单、工程待办同一事务落库。

        进入时深拷贝涉及的表，任一步骤抛错就整体回滚，绝不允许只写进一半。
        """
        with self._lock:
            backup = {name: deepcopy(self._tables.get(name, [])) for name in modules}
            try:
                yield
            except Exception:
                for name, snapshot in backup.items():
                    self._tables[name] = snapshot
                raise

    def module_names(self) -> list[str]:
        return sorted(name for name in self._tables if name not in INTERNAL_MODULES)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

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
