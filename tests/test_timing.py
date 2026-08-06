"""PhaseTimer 计时与超时检测测试。"""

import time

import pytest

from agent.timing import PhaseTimer


def test_timer_no_deadline():
    t = PhaseTimer("test")
    with t:
        pass
    assert t.elapsed_ms >= 0
    assert t.timed_out is False


def test_timer_with_deadline_not_timed_out():
    t = PhaseTimer("test", deadline_ms=60_000)  # 60s, 不会超时
    with t:
        pass
    assert t.timed_out is False


def test_timer_with_deadline_timed_out():
    t = PhaseTimer("test", deadline_ms=1)       # 1ms, 必然超时
    with t:
        time.sleep(0.01)
    assert t.timed_out is True
    assert t.elapsed_ms >= 1


def test_timer_check_returns_true_within_deadline():
    t = PhaseTimer("test", deadline_ms=60_000)
    t.__enter__()
    try:
        assert t.check() is True
    finally:
        t.__exit__()


def test_timer_check_returns_false_after_deadline():
    t = PhaseTimer("test", deadline_ms=1)
    t.__enter__()
    try:
        time.sleep(0.01)
        assert t.check() is False
        assert t.timed_out is True
    finally:
        t.__exit__()


def test_timer_remaining_ms():
    t = PhaseTimer("test", deadline_ms=100_000)
    t.__enter__()
    try:
        assert t.remaining_ms is not None
        assert 0 < t.remaining_ms <= 100_000
    finally:
        t.__exit__()

    t2 = PhaseTimer("test")
    assert t2.remaining_ms is None


def test_timer_elapsed_s():
    t = PhaseTimer("test")
    with t:
        time.sleep(0.05)
    assert 0.04 <= t.elapsed_s <= 0.2


def test_timer_double_exit_idempotent():
    t = PhaseTimer("test", deadline_ms=100_000)
    t.__enter__()
    t.__exit__()
    first_elapsed = t.elapsed_ms
    t.__exit__()
    assert t.elapsed_ms == first_elapsed          # 两次退出不改 elapsed
