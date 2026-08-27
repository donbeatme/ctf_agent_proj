"""执行环境 Provider(SshProvider 连接池 + SandboxProvider 容器会话)测试。

FakeSsh 工厂注入,不碰真实 SSH/docker。覆盖:借还有界 / 建连互斥 / 失效替换 /
actor 容器 key / 工具安装 / release 顺序 / handle 受限性。
"""

import asyncio

import pytest

from agent.env_providers import (
    SandboxHandle, SandboxProvider, SshHandle, SshProvider,
)
from agent.providers import Requirement
from agent.runner import ProcOutcome
from sandbox_env import SandboxSettings


class FakeSsh:
    """SshBackend-like:记录 exec/sync_to/close,固定成功返回。"""

    def __init__(self):
        self.execs = []      # (cmd, timeout)
        self.syncs = []      # (local, remote)
        self.closed = False

    async def exec(self, cmd_str, timeout=None):
        self.execs.append((cmd_str, timeout))
        if cmd_str.startswith("docker ps -aq"):
            return ProcOutcome(0, b"", b"")          # 无该容器 → 需创建
        if cmd_str.startswith("docker run -d"):
            return ProcOutcome(0, b"", b"")
        if cmd_str.startswith("docker rm -f"):
            return ProcOutcome(0, b"", b"")
        if cmd_str.startswith("docker exec"):
            return ProcOutcome(0, b"OUT", b"")
        return ProcOutcome(0, b"", b"")

    async def sync_to(self, local, remote):
        self.syncs.append((local, remote))

    async def close(self):
        self.closed = True


def _req(actor, cwd=None, tools=()):
    return Requirement(capabilities=frozenset({"isolated_exec"}), actor_id=actor,
                       cwd=cwd, tools=tools)


async def _sandbox_provider(pool=None):
    pool = pool or SshProvider(factory=lambda: FakeSsh(), max_connections=4)
    sp = SandboxProvider(pool, settings=SandboxSettings(ssh_host="vm"))
    return pool, sp


# ===== SshProvider 连接池 =====


async def test_pool_borrow_return_reuses_connection():
    pool = SshProvider(factory=lambda: FakeSsh(), max_connections=1)
    l1 = await pool.acquire(_req("a1"))
    conn1 = l1.handle.backend
    assert isinstance(l1.handle, SshHandle)
    await l1.release()
    assert len(pool._idle) == 1
    l2 = await pool.acquire(_req("a2"))
    assert l2.handle.backend is conn1       # max=1 → 同一连接复用
    assert l1.handle is not l2.handle


async def test_pool_bounded_waits_until_release():
    pool = SshProvider(factory=lambda: FakeSsh(), max_connections=1)
    l1 = await pool.acquire(_req("a1"))
    task = asyncio.create_task(pool.acquire(_req("a2")))
    await asyncio.sleep(0.05)
    assert not task.done()                  # 池满 → 等待
    await l1.release()
    l2 = await asyncio.wait_for(task, timeout=1)
    assert l2.holder == "a2"


async def test_pool_concurrent_acquire_no_duplicate_creation():
    created = []

    def factory():
        c = FakeSsh()
        created.append(c)
        return c

    pool = SshProvider(factory=factory, max_connections=4)
    leases = await asyncio.gather(*[pool.acquire(_req(f"a{i}")) for i in range(4)])
    assert len(created) == 4                # 建连互斥 → 恰好 4 条,无竞态重复
    assert pool._live == 4
    for l in leases:
        await l.release()
    assert len(pool._idle) == 4


async def test_pool_invalid_connection_replaced_on_release():
    created = []

    def factory():
        c = FakeSsh()
        created.append(c)
        return c

    pool = SshProvider(factory=factory, max_connections=2)
    l1 = await pool.acquire(_req("a1"))
    l1.handle._invalid = True               # 模拟 transport 错误标记
    await l1.release()
    assert pool._idle == []                 # 失效连接未回池
    assert pool._live == 0                  # 失效连接已 close,live 减一
    l2 = await pool.acquire(_req("a2"))
    assert len(created) == 2                # 补建新连接
    assert l2.handle.backend is not l1.handle.backend


async def test_handle_marks_invalid_on_transport_error():
    class _Drop(FakeSsh):
        async def exec(self, cmd_str, timeout=None):
            if cmd_str == "boom":
                return ProcOutcome(None, b"", b"connection reset")
            return await super().exec(cmd_str, timeout)

    pool = SshProvider(factory=lambda: _Drop(), max_connections=2)
    l1 = await pool.acquire(_req("a1"))
    out = await l1.handle.exec("boom")
    assert out.returncode is None and not out.timed_out
    assert l1.handle._invalid               # transport 错误 → 标记
    l2 = await pool.acquire(_req("a2"))
    await l2.handle.exec("docker ps -aq")
    assert not l2.handle._invalid           # 命令正常执行不标记


async def test_pool_health():
    pool = SshProvider(factory=lambda: FakeSsh(), max_connections=4)
    l1 = await pool.acquire(_req("a1"))
    h = await pool.health()
    assert h["ok"] is True and h["live"] == 1 and h["idle"] == 0 and h["max"] == 4
    await l1.release()
    assert (await pool.health())["idle"] == 1


# ===== SandboxProvider 容器会话 =====


async def test_sandbox_acquire_actor_container(monkeypatch):
    installed = []

    async def fake_install(self, tool_ids, *, session_key=None, force=False):
        installed.append((list(tool_ids), session_key))
        return {"installed": list(tool_ids)}

    monkeypatch.setattr("sandbox_env.base.SandboxManager.install_tools", fake_install)
    pool, sp = await _sandbox_provider()
    lease = await sp.acquire(_req("a1", cwd="/challenge/x", tools=("gdb",)))
    conn = lease.ssh_lease.handle.backend
    assert isinstance(lease.handle, SandboxHandle)
    assert lease.holder == "a1"
    assert lease.handle._key.endswith("-a1")
    assert installed == [(["gdb"], lease.handle._key)]   # 工具需求 → install_tools 带 actor key
    runs = [c for c, _ in conn.execs if "docker run -d --name" in c]
    assert runs and "-a1" in runs[0]                     # 容器按 actor 命名
    assert pool._live == 1 and pool._idle == []          # 连接被借出


async def test_sandbox_same_cwd_different_actors_isolated(monkeypatch):
    async def fake_install(self, tool_ids, *, session_key=None, force=False):
        return {"installed": list(tool_ids)}

    monkeypatch.setattr("sandbox_env.base.SandboxManager.install_tools", fake_install)
    _, sp = await _sandbox_provider()
    l1 = await sp.acquire(_req("a1", cwd="/c"))
    l2 = await sp.acquire(_req("a2", cwd="/c"))
    assert l1.handle._key != l2.handle._key              # 同 cwd 不同 actor → 不同容器
    assert l1.handle._key.split("-")[-1] == "a1"
    assert l2.handle._key.split("-")[-1] == "a2"


async def test_sandbox_release_cleans_container_and_returns_connection(monkeypatch):
    async def fake_install(self, tool_ids, *, session_key=None, force=False):
        return {"installed": list(tool_ids)}

    monkeypatch.setattr("sandbox_env.base.SandboxManager.install_tools", fake_install)
    pool, sp = await _sandbox_provider()
    lease = await sp.acquire(_req("a1", cwd="/challenge/x"))
    conn = lease.ssh_lease.handle.backend
    await sp.release(lease)
    assert any("docker rm -f" in c and "-a1" in c for c, _ in conn.execs)  # 容器删除
    assert len(pool._idle) == 1                                             # 连接还池
    assert (await sp.health())["active"] == 0


async def test_sandbox_reset_recreates_container(monkeypatch):
    async def fake_install(self, tool_ids, *, session_key=None, force=False):
        return {"installed": list(tool_ids)}

    monkeypatch.setattr("sandbox_env.base.SandboxManager.install_tools", fake_install)
    _, sp = await _sandbox_provider()
    lease = await sp.acquire(_req("a1", cwd="/challenge/x"))
    conn = lease.ssh_lease.handle.backend
    await lease.handle.reset()
    cmds = [c for c, _ in conn.execs]
    assert any("docker rm -f" in c for c in cmds)
    assert any("docker run -d" in c for c in cmds)       # 毁后重建


async def test_sandbox_handle_restricted_and_exec(monkeypatch):
    async def fake_install(self, tool_ids, *, session_key=None, force=False):
        return {"installed": list(tool_ids)}

    monkeypatch.setattr("sandbox_env.base.SandboxManager.install_tools", fake_install)
    _, sp = await _sandbox_provider()
    lease = await sp.acquire(_req("a1", cwd="/c"))
    assert not hasattr(lease.handle, "backend")          # 不裸露底层
    assert not hasattr(lease.handle, "ssh")
    out = await lease.handle.exec("ls", cwd="/c")
    assert out.ok and out.stdout == "OUT"                # 经沙箱执行


def test_sandbox_provider_requires_ssh():
    with pytest.raises(ValueError):
        SandboxProvider(SshProvider(factory=lambda: FakeSsh()),
                        settings=SandboxSettings())      # ssh_host=None
