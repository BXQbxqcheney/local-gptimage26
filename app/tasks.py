"""内存任务注册表：状态机、事件流、TTL 清理、并发上限。

- 状态机：pending → running → done | failed | cancelled
- 事件流：任务内 asyncio.Queue 列表（每 SSE 订阅者一队列）+ 环形历史（断线补发）
- TTL：终态任务超时自动清理（v1 不做持久化，重启即失，README 写明）
- 并发上限：运行中 GIF 任务数超限拒绝新建（429）
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


class TaskLimitError(Exception):
    """并发任务超上限。"""


@dataclass
class Event:
    """任务事件。"""

    ts: float
    level: str  # info | warn | frame | done | error
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "level": self.level,
            "message": self.message,
            "data": self.data,
        }


@dataclass
class Task:
    """一次 GIF 生成任务的全量运行时状态。"""

    id: str
    prompt: str
    params: dict[str, Any]
    status: str = "pending"  # pending | running | done | failed | cancelled
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    frames: list[dict[str, Any]] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)  # 环形历史（上限保护）
    result: dict[str, Any] | None = None
    error: str | None = None
    cancel_flag: Any = None  # gif.CancelFlag（避免循环导入用 Any）
    _queues: list[asyncio.Queue] = field(default_factory=list)

    def public_view(self) -> dict[str, Any]:
        """对外任务视图（含事件历史，供 SSE 断线后的轮询兜底）。"""
        return {
            "id": self.id,
            "status": self.status,
            "prompt": self.prompt,
            "params": self.params,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "frames": self.frames,
            "events": [e.to_dict() for e in self.events],
            "result": self.result,
            "error": self.error,
        }


MAX_EVENT_HISTORY = 500


class TaskRegistry:
    """任务注册表（单例，挂在 app.state）。"""

    def __init__(self, max_running: int, ttl_seconds: int) -> None:
        self._tasks: dict[str, Task] = {}
        self._max_running = max_running
        self._ttl = ttl_seconds

    def create(self, prompt: str, params: dict[str, Any]) -> Task:
        """新建任务；运行中任务超上限抛 TaskLimitError。"""
        running = sum(1 for t in self._tasks.values() if t.status in ("pending", "running"))
        if running >= self._max_running:
            raise TaskLimitError(
                f"运行中任务已达上限 {self._max_running}，请稍后再试"
            )
        task = Task(id=uuid.uuid4().hex[:12], prompt=prompt, params=params)
        self._tasks[task.id] = task
        return task

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def mark_running(self, task: Task) -> None:
        task.status = "running"

    def finish(self, task: Task, status: str, result: dict | None = None, error: str | None = None) -> None:
        """置终态并广播结束事件。"""
        task.status = status
        task.finished_at = time.time()
        task.result = result
        task.error = error

    def emit(self, task: Task, level: str, message: str, data: dict[str, Any] | None = None) -> None:
        """记录事件并推送给所有 SSE 订阅队列。"""
        evt = Event(ts=time.time(), level=level, message=message, data=data or {})
        task.events.append(evt)
        if len(task.events) > MAX_EVENT_HISTORY:
            del task.events[: len(task.events) - MAX_EVENT_HISTORY]
        for q in list(task._queues):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                # 订阅者消费不动：丢弃其队列中最老事件再塞（保最新）
                try:
                    q.get_nowait()
                    q.put_nowait(evt)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def subscribe(self, task: Task) -> asyncio.Queue:
        """订阅事件流（新队列先回放历史，再收增量）。"""
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        for evt in task.events:
            q.put_nowait(evt)
        task._queues.append(q)
        return q

    def unsubscribe(self, task: Task, q: asyncio.Queue) -> None:
        """退订。"""
        if q in task._queues:
            task._queues.remove(q)

    def cleanup_expired(self) -> int:
        """清理 TTL 到期的终态任务，返回清理数。"""
        now = time.time()
        expired = [
            tid
            for tid, t in self._tasks.items()
            if t.status in ("done", "failed", "cancelled")
            and t.finished_at is not None
            and now - t.finished_at > self._ttl
        ]
        for tid in expired:
            del self._tasks[tid]
        return len(expired)

    def stats(self) -> dict[str, Any]:
        """任务统计（健康视图）。"""
        by_status: dict[str, int] = {}
        for t in self._tasks.values():
            by_status[t.status] = by_status.get(t.status, 0) + 1
        return {"total": len(self._tasks), "by_status": by_status}
