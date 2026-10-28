"""出站队列：所有"要说出去的话"先入队，再由唯一的发送线程按时间出队。

谁往里放
--------
* :meth:`Brain.chat`：一轮回复拆成的多段（每段带 ``ready_at``，用来实现"打字延迟"）；
* :meth:`Brain.tick`：``proactive`` 模块提名成功的主动消息。

谁往外取
--------
只有 :meth:`Brain.drain`（以及包着它的 :meth:`Brain.flush_outbox`）。因此
``notifier`` 永远只有一个调用者，屏幕上的顺序 = 出队顺序。

排序口径
--------
**严格 FIFO + 队首阻塞**：只要队首还没到 ``ready_at``，后面的条目一律不取。
这样"分三段回复"不会被突然插进来的主动消息打断；代价是主动消息最多等一条
回复的延迟（几秒），这个代价是故意的。

过期
----
``expires_at`` 已过的条目在入队、出队、``purge_expired`` 时都会被丢弃；主动消息
过期必须回抛 ``skipped``，否则模块的 ``pending`` 永远清不掉、规则再也触发不了。
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

__all__ = ["OutboxItem", "Outbox", "KIND_REPLY", "KIND_PROACTIVE"]

#: 普通回复（由 ``chat`` 入队）
KIND_REPLY: str = "reply"
#: 主动消息（由 ``proactive`` 模块提名、``tick`` 入队）
KIND_PROACTIVE: str = "proactive"


@dataclass(frozen=True)
class OutboxItem:
    """一条待发送消息；不可变，修改用 :func:`dataclasses.replace`。"""

    kind: str
    text: str
    ready_at: datetime
    meta: dict[str, Any] = field(default_factory=dict)
    expires_at: datetime | None = None
    item_id: str = ""

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at is not None and self.expires_at <= now


class Outbox:
    """线程安全的出站队列；FIFO，队首未到点则整队等待。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[OutboxItem] = []
        self._seq = itertools.count(1)

    # -- 入队 -------------------------------------------------------------- #

    def put(
        self,
        kind: str,
        text: str,
        *,
        ready_at: datetime,
        meta: dict[str, Any] | None = None,
        expires_at: datetime | None = None,
    ) -> OutboxItem:
        """入队一条消息，返回带上 ``item_id`` 的条目。"""
        item = OutboxItem(
            kind=str(kind),
            text=str(text),
            ready_at=ready_at,
            meta=dict(meta or {}),
            expires_at=expires_at,
            item_id=f"{kind}-{next(self._seq)}",
        )
        with self._lock:
            self._items.append(item)
        return item

    # -- 出队 -------------------------------------------------------------- #

    def take_ready(self, now: datetime) -> list[OutboxItem]:
        """取出队首连续到点的条目；遇到未到点的条目立即停止（保序）。"""
        taken: list[OutboxItem] = []
        with self._lock:
            while self._items and self._items[0].ready_at <= now:
                taken.append(self._items.pop(0))
        return taken

    # -- 取消 / 过期 / 清空 ------------------------------------------------ #

    def cancel(self, *, kind: str | None = None, reason: str = "cancelled") -> list[OutboxItem]:
        """撤掉尚未出队的条目（可按 ``kind`` 过滤），返回被撤掉的条目。"""
        removed: list[OutboxItem] = []
        with self._lock:
            keep: list[OutboxItem] = []
            for item in self._items:
                if kind is None or item.kind == kind:
                    removed.append(replace(item, meta={**item.meta, "cancel_reason": reason}))
                else:
                    keep.append(item)
            self._items = keep
        return removed

    def purge_expired(self, now: datetime) -> list[OutboxItem]:
        """丢弃已过期条目（不论位置），返回被丢弃的条目。"""
        dropped: list[OutboxItem] = []
        with self._lock:
            keep: list[OutboxItem] = []
            for item in self._items:
                if item.is_expired(now):
                    dropped.append(item)
                else:
                    keep.append(item)
            self._items = keep
        return dropped

    def clear(self) -> list[OutboxItem]:
        """清空队列，返回被清掉的条目。"""
        with self._lock:
            items = self._items
            self._items = []
        return items

    # -- 只读视图 ---------------------------------------------------------- #

    def pending(self, kind: str | None = None) -> int:
        with self._lock:
            if kind is None:
                return len(self._items)
            return sum(1 for item in self._items if item.kind == kind)

    def next_ready_at(self) -> datetime | None:
        """队首的发送时间；空队列返回 None（供自适应轮询用）。"""
        with self._lock:
            return self._items[0].ready_at if self._items else None

    def snapshot(self) -> list[OutboxItem]:
        """当前队列副本，供调试与测试。"""
        with self._lock:
            return list(self._items)

    def __len__(self) -> int:
        return self.pending()
