"""main.py actor mode 接线测试:_parallel_engine_kw 的并行参数派生与串行回退。

接线目标:--actors>1 时 main 给 Engine 注入 {scheduler, max_concurrency},每步独立
容器租约并发执行;ssh 未配置/actors<=1 回退串行(空 dict,Engine **{} 无效果)。
构造 provider 不发起真实连接(懒连接),测试注入 settings 即可,不依赖 VM。
"""

from agent.scheduler import ExecutionScheduler
from sandbox_env import SandboxSettings


def _args(actors):
    class A:
        pass

    a = A()
    a.actors = actors
    return a


def test_parallel_engine_kw_serial_when_actors_le_1():
    from main import _parallel_engine_kw

    assert _parallel_engine_kw(_args(1)) == {}
    assert _parallel_engine_kw(_args(0)) == {}
    assert _parallel_engine_kw(_args(-1)) == {}


def test_parallel_engine_kw_returns_scheduler_when_actors_gt_1():
    """actors>1 且 ssh 已配置:返回 {scheduler, max_concurrency};注入 settings 不连真实 ssh。"""
    from main import _parallel_engine_kw

    settings = SandboxSettings(ssh_host="127.0.0.1")
    kw = _parallel_engine_kw(_args(4), settings=settings)
    assert set(kw) == {"scheduler", "max_concurrency"}
    assert kw["max_concurrency"] == 4
    assert isinstance(kw["scheduler"], ExecutionScheduler)


def test_parallel_engine_kw_falls_back_when_ssh_not_configured():
    """actors>1 但 ssh 未配置:回退串行(返回 {}),不抛(打印警告)。"""
    from main import _parallel_engine_kw

    assert _parallel_engine_kw(_args(4), settings=SandboxSettings(ssh_host=None)) == {}


def test_close_scheduler_none_is_noop():
    """串行路径无调度器:收尾 close 为 no-op。"""
    from main import _close_scheduler

    _close_scheduler(None)
