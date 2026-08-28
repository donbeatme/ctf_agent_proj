"""执行环境 Provider 层:SshProvider(连接池)+ SandboxProvider(容器会话)。

- SshProvider(SHARED):有界 ssh 连接池,管连接生命周期——建连互斥(消除 SshBackend._connect
  懒加载竞态)/ 借还 / 失效替换 / 健康上报。连接 = SshBackend-like(exec/sync_to/close)。
  capability {remote_exec, file_transfer}。
- SandboxProvider(PER_ACTOR):每 actor 一个容器会话。acquire 时向 SshProvider 借一条连接,
  包成 SshSandboxBackend;容器 key = sha1(cwd)[:12]-{actor}(actor 折叠进
  SandboxManager.session_key),同 cwd 并行步骤各用各的容器;release 先删容器再还连接。
  capability {isolated_exec, linux, docker}。

生命周期边界:连接(可替换/池化)与容器(有状态/会话)各管各的——SandboxProvider 只编排,
不碰连接生命周期;SshProvider 不知道容器存在。handle 面向执行器受限暴露,绝不裸露底层 client。
"""

from __future__ import annotations

import asyncio
import base64
from collections import deque

from agent.providers import Capability, Handle, Lease, Provider, Requirement
from agent.runner import ProcOutcome
from sandbox_env import SandboxManager, SandboxSettings
from sandbox_env.ssh_backend import SshSandboxBackend


class SshHandle(Handle):
    """受限 ssh 句柄:exec 透传,transport 错误标记失效。绝不裸露 asyncssh client。"""

    name = "ssh"

    def __init__(self, backend):
        self._backend = backend      # SshBackend-like
        self._invalid = False

    @property
    def backend(self):
        """底层连接对象。仅内部消费者(池回收 / SandboxProvider 包后端)可取。"""
        return self._backend

    async def exec(self, cmd: str, timeout: float | None = None) -> ProcOutcome:
        out = await self._backend.exec(cmd, timeout=timeout)
        # transport 级错误(returncode=None 且非超时)→ 连接大概率失效,标记待替换
        if out.returncode is None and not out.timed_out:
            self._invalid = True
        return out


class SshProvider(Provider):
    """有界 ssh 连接池(SHARED):管连接生命周期。

    池内 _live 条连接(空闲 + 借出);建连在互斥锁内完成(消除 SshBackend._connect 的
    懒加载竞态);池满时 acquire 等待归还。失效连接(transport 错误)release 时 close,
    live 减一,下一次 acquire 补建新连接(lazy 替换)。
    """

    name = "ssh"
    capability = Capability(keys=frozenset({"remote_exec", "file_transfer"}))

    def __init__(self, settings=None, factory=None, max_connections=4,
                 connect_timeout: float = 8.0):
        self._settings = settings or SandboxSettings.from_env()
        if factory is None:
            from agent.ssh import SshBackend  # lazy:asyncssh 可选依赖

            s = self._settings

            def _factory():
                return SshBackend(
                    host=s.ssh_host, user=s.ssh_user, password=s.ssh_password,
                    workdir=s.ssh_workdir, connect_timeout=connect_timeout,
                    ssh_key=s.ssh_key, host_key=s.ssh_host_key)

            factory = _factory
        self._factory = factory
        self._max = max_connections
        self._idle: list = []
        self._live = 0
        self._lock = asyncio.Lock()
        self._waiters: deque = deque()

    async def acquire(self, req: Requirement) -> Lease:
        while True:
            waiter = None
            async with self._lock:
                if self._idle:
                    conn = self._idle.pop()
                elif self._live < self._max:
                    conn = self._factory()
                    self._live += 1
                else:
                    waiter = asyncio.get_running_loop().create_future()
                    self._waiters.append(waiter)
            if waiter is None:
                return Lease(provider=self, requirement=req,
                             holder=req.actor_id or "?", handle=SshHandle(conn))
            await waiter  # 池满等待;release 唤醒后重试

    async def release(self, lease: Lease) -> None:
        handle = lease.handle
        conn = handle.backend
        invalid = handle._invalid
        async with self._lock:
            if invalid:
                self._live -= 1          # 失效连接不回池
            else:
                self._idle.append(conn)
            if self._waiters:
                self._waiters.popleft().set_result(None)
        if invalid:
            try:
                await conn.close()
            except Exception:
                pass

    async def health(self) -> dict:
        return {"ok": True, "name": self.name, "live": self._live,
                "idle": len(self._idle), "max": self._max}

    async def close(self) -> None:
        while self._idle:
            conn = self._idle.pop()
            try:
                await conn.close()
            except Exception:
                pass
        self._live = 0


class SandboxHandle(Handle):
    """受限沙箱句柄:执行/工具/重置。绝不裸露 backend / ssh / docker client。"""

    name = "sandbox"

    def __init__(self, mgr: SandboxManager, key: str):
        self._mgr = mgr
        self._key = key

    async def exec(self, cmd, *, cwd=None, category=None, tool_id=None,
                   target=None, timeout=None):
        return await self._mgr.exec(cmd, cwd=cwd, category=category,
                                    tool_id=tool_id, target=target, timeout=timeout)

    async def run_python(self, code, *, cwd=None, category=None, tool_id=None,
                         target=None, timeout=None):
        return await self._mgr.run_python(code, cwd=cwd, category=category,
                                          tool_id=tool_id, target=target,
                                          timeout=timeout)

    async def probe_tool(self, tool_id: str) -> dict:
        return await self._mgr.probe_tool(tool_id, session_key=self._key)

    async def install_tools(self, tool_ids) -> dict:
        return await self._mgr.install_tools(tool_ids, session_key=self._key)

    async def reset(self) -> None:
        """毁容器重来:cleanup 删容器 → ensure 重建(状态/工具清零)。"""
        await self._mgr.cleanup(self._key)
        await self._mgr.ensure(self._key)


class ExecutionEnvironmentLease(Lease):
    """容器会话租约:sandbox handle + 借来的 ssh 连接(还池在 release,先容器后连接)。"""

    def __init__(self, *, provider, requirement, holder,
                 sandbox: SandboxHandle, ssh_lease: Lease):
        super().__init__(provider=provider, requirement=requirement,
                         holder=holder, handle=sandbox)
        self.ssh_lease = ssh_lease


class SandboxProvider(Provider):
    """每 actor 容器会话 Provider(PER_ACTOR)。

    容器 key = sha1(cwd)[:12]-{actor},同 cwd 并行步骤各用各的容器。acquire 向
    SshProvider 借一条连接(该 actor 会话持有),包成 SshSandboxBackend 构造
    SandboxManager(actor 折叠进 session_key);release 先删容器再还连接。
    连接生命周期归 SshProvider,本类只编排容器会话。
    """

    name = "sandbox"
    capability = Capability(keys=frozenset({"isolated_exec", "linux", "docker"}))

    def __init__(self, ssh_provider: SshProvider, settings=None):
        self._ssh = ssh_provider
        self._settings = settings or SandboxSettings.from_env()
        if not self._settings.ssh_configured:
            raise ValueError("SandboxProvider 需要 ssh 后端(CTF_SSH_HOST 未配置)")
        self._active: set[str] = set()      # holder → 活跃租约(health/观测)

    async def acquire(self, req: Requirement) -> ExecutionEnvironmentLease:
        ssh_lease = await self._ssh.acquire(req)
        try:
            backend = SshSandboxBackend(self._settings, ssh=ssh_lease.handle.backend)
            mgr = SandboxManager(self._settings, backend=backend, actor=req.actor_id)
            key = mgr.session_key(req.cwd)
            await mgr.ensure(key)
            if req.tools:
                await mgr.install_tools(list(req.tools), session_key=key)
            holder = req.actor_id or "?"
            lease = ExecutionEnvironmentLease(
                provider=self, requirement=req, holder=holder,
                sandbox=SandboxHandle(mgr, key), ssh_lease=ssh_lease)
            self._active.add(holder)
            return lease
        except Exception:
            await ssh_lease.release()   # 建容器/装工具失败也要还连接
            raise

    async def release(self, lease: ExecutionEnvironmentLease) -> None:
        try:
            await lease.handle._mgr.cleanup(lease.handle._key)   # 删容器(总删)
        finally:
            await lease.ssh_lease.release()                       # 还连接
            self._active.discard(lease.holder)

    async def health(self) -> dict:
        return {"ok": True, "name": self.name, "active": len(self._active)}

    async def probe_image(self, script: str, timeout: float = 180.0) -> str:
        """在沙箱镜像的 scratch 容器内执行探测脚本(docker run --rm,只读,不落会话)。

        借一条 ssh 连接,把脚本 base64 内联进 docker run 命令(避免引号/换行转义坑),
        返回容器 stdout。供 run 起始环境快照探测真实镜像工具就绪度——本地 host 探测
        (importlib/which)会误报缺失。探测失败返回空串(调用方回退 host 探测)。
        """
        req = Requirement(capabilities=frozenset({"remote_exec"}), actor_id="probe")
        ssh_lease = await self._ssh.acquire(req)
        try:
            backend = ssh_lease.handle.backend
            b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
            inner = "import base64,sys;exec(base64.b64decode(sys.argv[1]).decode())"
            cmd = f"docker run --rm {self._settings.image} python3 -c \"{inner}\" {b64}"
            out = await backend.exec(cmd, timeout=timeout)
            if out.returncode != 0:
                return ""
            return out.stdout.decode("utf-8", "replace")
        finally:
            await ssh_lease.release()

    async def close(self) -> None:
        """关闭底层 ssh 连接池(scheduler.close 级联;沙箱会话无独立资源要释放)。

        本类只编排容器会话,连接生命周期归 SshProvider——收尾时把借来的连接池一并关掉,
        否则 ExecutionScheduler.close 对 SandboxProvider 是 no-op,池内 idle 连接泄漏。
        """
        await self._ssh.close()


__all__ = [
    "SshHandle", "SshProvider", "SandboxHandle",
    "ExecutionEnvironmentLease", "SandboxProvider",
]
