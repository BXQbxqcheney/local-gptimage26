"""多 API Key 轮询池：Least-Used 轮询 + 429/5xx 指数冷却 + 连续失败故障标记。

复用自本机已验证工程的密钥池模式（全新 Python 实现）：
- acquire()：挑“可用且当前在用数最少”的 Key（并列时轮转游标打散）；
- report_rate_limited()：429 → 指数冷却（60s 起，×2 递增，上限 15min）；
- report_failure()：连续失败 3 次 → 故障标记，冷却 15min；
- report_success()：清零失败计数、解除冷却；
- 全部 Key 不可用时抛 NoAvailableKeyError（附最快恢复时间）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

COOLDOWN_BASE_SECONDS = 60.0
COOLDOWN_MAX_SECONDS = 15 * 60.0
FAILURE_MARK_THRESHOLD = 3


class NoAvailableKeyError(Exception):
    """Key 池耗尽：所有 Key 均处于冷却/故障状态。"""

    def __init__(self, retry_after: float) -> None:
        self.retry_after = max(0.0, retry_after)
        super().__init__(
            f"所有 API Key 均在冷却中，最快 {self.retry_after:.0f} 秒后恢复"
        )


@dataclass
class ApiKeyState:
    """单个 Key 的运行时状态。"""

    key_id: str
    value: str
    in_use: int = 0
    total_used: int = 0
    cooldown_until: float = 0.0
    consecutive_failures: int = 0
    marked_broken: bool = False

    @property
    def available(self) -> bool:
        """是否可用：未故障且不在冷却期。"""
        return not self.marked_broken and time.monotonic() >= self.cooldown_until

    def masked(self) -> str:
        """脱敏展示。"""
        v = self.value
        return f"{v[:4]}...{v[-4:]}" if len(v) > 8 else "*" * len(v)

    def snapshot(self) -> dict:
        """对外状态快照（永不泄露明文 Key）。"""
        return {
            "key_id": self.key_id,
            "masked": self.masked(),
            "in_use": self.in_use,
            "total_used": self.total_used,
            "available": self.available,
            "cooldown_remaining": max(0.0, self.cooldown_until - time.monotonic()),
            "consecutive_failures": self.consecutive_failures,
            "marked_broken": self.marked_broken,
        }


class KeyPool:
    """某个 provider 的 Key 轮询池（线程安全由 FastAPI 单事件循环 + 显式锁保证）。

    注：本项目 asyncio 单线程模型下 acquire/release 之间无 await，
    故无需真实锁；如未来引入多线程调用需补 threading.Lock。
    """

    def __init__(self, provider_name: str, keys: list[str]) -> None:
        if not keys:
            raise ValueError(f"provider {provider_name} 的 Key 列表为空")
        self.provider_name = provider_name
        self._keys: list[ApiKeyState] = [
            ApiKeyState(key_id=f"{provider_name}#key{i + 1}", value=k)
            for i, k in enumerate(keys)
        ]
        self._cursor = 0

    def __len__(self) -> int:
        return len(self._keys)

    def acquire(self) -> ApiKeyState:
        """选取可用 Key：在用数最少优先，并列时游标轮转。"""
        candidates = [k for k in self._keys if k.available]
        if not candidates:
            soonest = min(k.cooldown_until for k in self._keys)
            raise NoAvailableKeyError(soonest - time.monotonic())
        min_use = min(k.in_use for k in candidates)
        tied = [k for k in candidates if k.in_use == min_use]
        # 并列时按游标轮转，保证同优先级 Key 均匀分担
        self._cursor = (self._cursor + 1) % len(tied)
        chosen = tied[self._cursor % len(tied)]
        chosen.in_use += 1
        chosen.total_used += 1
        return chosen

    def report_success(self, key: ApiKeyState) -> None:
        """调用成功：解除冷却、清零失败计数。"""
        key.consecutive_failures = 0
        key.marked_broken = False
        key.cooldown_until = 0.0

    def report_rate_limited(self, key: ApiKeyState) -> None:
        """429：指数冷却（60s 起 ×2，封顶 15min）。"""
        wait = min(
            COOLDOWN_MAX_SECONDS,
            COOLDOWN_BASE_SECONDS * (2 ** min(key.consecutive_failures, 4)),
        )
        key.cooldown_until = time.monotonic() + wait
        key.consecutive_failures += 1
        self._maybe_mark_broken(key)

    def report_failure(self, key: ApiKeyState) -> None:
        """普通失败（网络/5xx 等）：计失败数，达阈值标记故障。"""
        key.consecutive_failures += 1
        self._maybe_mark_broken(key)
        if not key.marked_broken:
            # 未达故障阈值也给短冷却，避免立刻撞同一个 Key
            key.cooldown_until = max(
                key.cooldown_until, time.monotonic() + COOLDOWN_BASE_SECONDS / 2
            )

    def release(self, key: ApiKeyState) -> None:
        """释放占用（无论成败都调用）。"""
        key.in_use = max(0, key.in_use - 1)

    def _maybe_mark_broken(self, key: ApiKeyState) -> None:
        """连续失败达阈值 → 标记故障并长冷却。"""
        if key.consecutive_failures >= FAILURE_MARK_THRESHOLD:
            key.marked_broken = True
            key.cooldown_until = time.monotonic() + COOLDOWN_MAX_SECONDS

    def snapshot(self) -> list[dict]:
        """整池状态快照。"""
        return [k.snapshot() for k in self._keys]

    def reset_all(self) -> int:
        """强制终止冷却：清零全部 Key 的冷却/故障/失败计数，返回重置的 Key 数。

        用户显式操作（网页按钮/API），用于确认限流已解除后立即恢复全部 Key。
        """
        n = 0
        for k in self._keys:
            k.cooldown_until = 0.0
            k.consecutive_failures = 0
            k.marked_broken = False
            n += 1
        return n
