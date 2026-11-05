"""密钥池测试：轮询、429 冷却、故障标记、快照脱敏。"""

import time

from app.keypool import ApiKeyState, KeyPool, NoAvailableKeyError


def test_acquire_round_robin():
    pool = KeyPool("p", ["k1", "k2"])
    seen = [pool.acquire().value for _ in range(4)]
    # 两个 Key 均匀分担
    assert set(seen) == {"k1", "k2"}


def test_release_then_reuse():
    pool = KeyPool("p", ["k1"])
    k = pool.acquire()
    assert k.in_use == 1
    pool.release(k)
    assert k.in_use == 0
    k2 = pool.acquire()
    assert k2 is k


def test_429_cooldown_and_skip():
    pool = KeyPool("p", ["k1", "k2"])
    a = pool.acquire()
    pool.release(a)
    pool.report_rate_limited(a)
    assert not a.available
    # a 冷却中：下一个必须拿到另一个 Key
    b = pool.acquire()
    assert b is not a


def test_consecutive_failures_mark_broken():
    pool = KeyPool("p", ["k1"])
    k = pool.acquire()
    pool.release(k)
    for _ in range(3):
        pool.report_failure(k)
    assert k.marked_broken
    assert not k.available


def test_all_keys_down_raises_with_retry_after():
    pool = KeyPool("p", ["k1"])
    k = pool.acquire()
    pool.release(k)
    pool.report_rate_limited(k)
    try:
        pool.acquire()
        raise AssertionError("应当抛出 NoAvailableKeyError")
    except NoAvailableKeyError as exc:
        assert exc.retry_after > 0


def test_success_resets_state():
    pool = KeyPool("p", ["k1"])
    k = pool.acquire()
    pool.release(k)
    pool.report_failure(k)
    pool.report_failure(k)
    pool.report_success(k)
    assert k.available and k.consecutive_failures == 0


def test_snapshot_masks_key():
    state = ApiKeyState(key_id="p#key1", value="sk-abcdefghijklmnop")
    snap = state.snapshot()
    assert "abcdefghijklmnop" not in str(snap)
    assert snap["masked"].startswith("sk-")


def test_reset_all_clears_cooldown_and_marks():
    """强制终止冷却：冷却中/故障标记的 Key 立即恢复可用。"""
    pool = KeyPool("p", ["k1", "k2"])
    a = pool.acquire()
    b = pool.acquire()
    pool.release(a)
    pool.release(b)
    pool.report_rate_limited(a)
    for _ in range(3):
        pool.report_failure(b)
    assert not a.available and not b.available
    n = pool.reset_all()
    assert n == 2
    assert a.available and b.available
    assert not a.marked_broken and a.consecutive_failures == 0
