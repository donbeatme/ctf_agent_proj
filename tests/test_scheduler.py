"""执行环境调度器(ExecutionScheduler)测试:需求派生 / Provider 匹配 / 会话租约生命周期。

FakeSsh 工厂注入,不碰真实 SSH/docker。覆盖:requirement 组装 / 无匹配 provider 抛错 /
acquire 返回会话租约(handle 受限)/ release 删容器还连接 / close 关 provider。
"""

import pytest

from agent.env_providers import SandboxHandle, SandboxProvider, SshProvider
from agent.scheduler import ExecutionScheduler
from sandbox_env import SandboxSettings
from tests.test_env_providers import FakeSsh


class _Step:
    def __init__(self, id):
        self.id = id


def _sandbox_provider(pool=None, conns=None):
    conns = conns if conns is not None else []

    def factory():
        c = FakeSsh()
        conns.append(c)
        return c

    pool = pool or SshProvider(factory=factory, max_connections=4)
    return SandboxProvider(pool, settings=SandboxSettings(ssh_host="vm")), pool, conns


def test_requirement_for_assembles_context():
    s = ExecutionScheduler(providers=[])
    req = s.requirement_for(actor_id="ex1", cwd="/challenge/x")
    assert req.capabilities == frozenset({"isolated_exec", "linux", "docker"})
    assert req.actor_id == "ex1"
    assert req.cwd == "/challenge/x"
    assert req.tools == ()
    assert req.labels == {}
    req2 = s.requirement_for(actor_id="a", cwd="/c", step=_Step("s1"))
    assert req2.labels == {"step_id": "s1"}


async def test_acquire_selects_sandbox_provider(monkeypatch):
    def fake_install(self, tool_ids, *, session_key=None, force=False):
        return {"installed": list(tool_ids)}

    monkeypatch.setattr("sandbox_env.base.SandboxManager.install_tools", fake_install)
    sp, pool, conns = _sandbox_provider()
    s = ExecutionScheduler(providers=[sp])
    lease = await s.acquire(s.requirement_for(actor_id="ex1", cwd="/challenge/x"))
    assert isinstance(lease.handle, SandboxHandle)
    assert lease.holder == "ex1"
    assert pool._idle == []                                  # 连接被借出
    await s.release(lease)
    cmds = [c for c, _ in conns[0].execs]
    assert any("docker run -d --name" in c for c in cmds)    # acquire 建容器
    assert any("docker rm -f" in c for c in cmds)            # release 删容器
    assert len(pool._idle) == 1                              # 连接还池


async def test_acquire_fails_without_matching_provider():
    s = ExecutionScheduler(providers=[])
    with pytest.raises(RuntimeError, match="无 Provider"):
        await s.acquire(s.requirement_for(actor_id="ex1", cwd="/c"))


async def test_handle_is_restricted(monkeypatch):
    def fake_install(self, tool_ids, *, session_key=None, force=False):
        return {"installed": list(tool_ids)}

    monkeypatch.setattr("sandbox_env.base.SandboxManager.install_tools", fake_install)
    sp, _, _ = _sandbox_provider()
    s = ExecutionScheduler(providers=[sp])
    lease = await s.acquire(s.requirement_for(actor_id="ex1", cwd="/c"))
    assert not hasattr(lease.handle, "backend")
    assert not hasattr(lease.handle, "ssh")
    await s.release(lease)


async def test_close_closes_providers():
    closed = []

    class _P:
        name = "stub"

        async def close(self):
            closed.append(1)

    s = ExecutionScheduler(providers=[_P()])
    await s.close()
    assert closed == [1]
