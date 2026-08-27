"""沙箱环境管理器:SandboxBackend(ABC) 后端接口 + SandboxManager 门面。

镜像 ctf_platform 适配器模式:执行层(executor/runner)只依赖 SandboxManager;
换沙箱后端 = 写新子类(SshSandboxBackend / FakeBackend),门面与主架构零改动。

容器模型 per_challenge:会话键 = sha1(绝对 cwd)[:12],容器名 ctf-<key>;不同
challenge 目录 → 不同容器,题目间隔离且容器内安装持久(解决旧 docker run --rm 无状态)。

全异步(asyncssh/AsyncOpenAI):exec/ensure/sync/cleanup 均 await,为 Phase 3
actor 每 ex 独立容器 + 连接铺路。I/O 方法调用方需在 async 上下文中。
"""

from __future__ import annotations

import hashlib
import os
import shlex
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from opslog import ErrorLevel, emit, record_error

from .config import SandboxSettings
from .errors import SandboxUnavailableError

_MAX_OUT = 4000   # stdout 截断(与 runner 对齐)
_MAX_ERR = 2000   # stderr 截断


@dataclass
class ExecOutcome:
    """一次沙箱内命令执行的归一结果(与 agent.runner.ProcOutcome 同形)。"""

    returncode: int | None
    stdout: bytes = b""
    stderr: bytes = b""
    timed_out: bool = False


def session_key_for(cwd) -> str:
    """会话键 = sha1(绝对 cwd)[:12];容器名 ctf-<key>(docker name 合法字符)。"""
    p = Path(cwd).resolve()
    return hashlib.sha1(str(p).encode("utf-8")).hexdigest()[:12]


def container_name_for(session_key: str) -> str:
    return f"ctf-{session_key}"


class SandboxBackend(ABC):
    """沙箱后端接口:ensure/exec/sync/cleanup 生命周期 + 资源释放。全 async。"""

    name: str = "sandbox"

    def __init__(self, settings: SandboxSettings):
        self.settings = settings

    async def ensure(self, session_key: str | None = None) -> str:
        """容器/环境就绪,返回容器标识;无容器后端 no-op 返回 ''。"""
        return ""

    @abstractmethod
    async def exec(self, cmd_str: str, *, session_key: str | None = None,
                   timeout: float | None = None) -> ExecOutcome:
        """在沙箱内执行一条命令(远程 shell,支持管道/重定向)。"""

    async def sync(self, local_dir, session_key: str | None = None) -> None:
        """把本地目录上传到该会话沙箱工作区(默认 no-op)。"""

    def is_ready(self) -> bool:
        """后端可用性(SSH 已配置/可连)。"""
        return True

    async def close(self) -> None:
        """释放连接等资源。"""

    async def cleanup(self, session_key: str | None = None) -> None:
        """销毁会话容器/环境(默认 no-op)。"""


class SandboxManager:
    """沙箱门面:runner/executor 消费的唯一入口。委托后端生命周期 + 工具依赖管理。

    exec() 流程:派生会话键 → ensure 容器 → 上传工作目录 → 依赖钩子(缺失自动装)→ 后端执行。
    返回与 agent.runner.RunOutcome 同形的对象,runner 可直接透传。
    """

    def __init__(self, settings: SandboxSettings | None = None, backend=None,
                 catalog=None, max_out: int = _MAX_OUT, max_err: int = _MAX_ERR):
        self.settings = settings or SandboxSettings.from_env()
        if backend is None:
            from .ssh_backend import SshSandboxBackend  # lazy:避免构造时引 asyncssh

            if not self.settings.ssh_configured:
                raise SandboxUnavailableError(
                    "CTF_SSH_HOST 未配置,无法构造 SshSandboxBackend——沙箱管理器不可用"
                )
            backend = SshSandboxBackend(self.settings)
        self.backend = backend
        from .tools import ToolManager  # lazy:目录扫描在首次使用时才发生

        self.tools = ToolManager(self.backend, catalog=catalog)
        self.max_out = max_out
        self.max_err = max_err

    @property
    def target_name(self) -> str:
        """沙箱目标名(ssh|docker):runner 沙箱接管路由时据此决策(ssh 后端→ssh,其余→docker)。"""
        return "ssh" if getattr(self.backend, "name", "") == "ssh" else "docker"

    # ===== 生命周期 =====

    def session_key(self, cwd=None) -> str:
        return session_key_for(cwd or os.getcwd())

    async def ensure(self, session_key: str | None = None) -> str:
        name = await self.backend.ensure(session_key)
        emit("sandbox", "ensure", session_key=session_key or "default", container=name)
        return name

    async def cleanup(self, session_key: str | None = None) -> None:
        await self.backend.cleanup(session_key)
        emit("sandbox", "cleanup", session_key=session_key or "default")

    async def close(self) -> None:
        await self.backend.close()

    # ===== 执行 =====

    async def exec(self, cmd, *, cwd=None, category=None, tool_id=None,
                   target=None, timeout=None):
        """沙箱内执行命令:ensure → sync → 依赖钩子 → 后端 exec。"""
        work = Path(cwd or os.getcwd()).resolve()
        key = self.session_key(work)
        await self.backend.ensure(key)
        try:
            await self.backend.sync(work, session_key=key)
        except Exception as exc:
            # 同步失败不阻断本次命令(远端持久容器可能已有文件),但必须报错进 log,不能静默
            record_error("sandbox", "sync", exc=exc, level=ErrorLevel.RECOVERABLE,
                         cwd=str(work), reason="工作目录同步失败,远端 /work 可能缺失附件")
        else:
            emit("sandbox", "sync", cwd=str(work), session_key=key)
        if self.settings.install_auto and tool_id:
            await self._ensure_deps(tool_id, key)
        cmd_str = shlex.join(cmd) if isinstance(cmd, list) else str(cmd)
        t0 = time.perf_counter()
        raw = await self.backend.exec(cmd_str, session_key=key, timeout=timeout)
        out = self._to_outcome(raw, cmd, target or self.backend.name, t0)
        emit("sandbox", "exec", cwd=str(work), session_key=key, tool_id=tool_id,
             target=out.target, ok=out.ok, returncode=out.returncode,
             timed_out=out.timed_out, elapsed_ms=out.elapsed_ms, cmd=cmd_str)
        return out

    async def run_python(self, code, *, cwd=None, category=None, tool_id=None,
                         target=None, timeout=None):
        """沙箱内执行 Python:写 _ctf_exec.py → sync → python3 运行。"""
        work = Path(cwd or os.getcwd()).resolve()
        work.mkdir(parents=True, exist_ok=True)
        script = work / "_ctf_exec.py"
        script.write_text(code, encoding="utf-8")
        key = self.session_key(work)
        await self.backend.ensure(key)
        try:
            await self.backend.sync(work, session_key=key)
        except Exception as exc:
            record_error("sandbox", "sync", exc=exc, level=ErrorLevel.RECOVERABLE,
                         cwd=str(work), reason="工作目录同步失败,远端 /work 可能缺失附件")
        else:
            emit("sandbox", "sync", cwd=str(work), session_key=key)
        if self.settings.install_auto and tool_id:
            await self._ensure_deps(tool_id, key)
        argv = ["python3", "/work/_ctf_exec.py"]
        t0 = time.perf_counter()
        raw = await self.backend.exec(shlex.join(argv), session_key=key, timeout=timeout)
        out = self._to_outcome(raw, argv, target or self.backend.name, t0)
        emit("sandbox", "run_python", cwd=str(work), session_key=key, tool_id=tool_id,
             target=out.target, ok=out.ok, returncode=out.returncode,
             timed_out=out.timed_out, elapsed_ms=out.elapsed_ms, cmd=shlex.join(argv))
        return out

    async def _ensure_deps(self, tool_id: str, key: str) -> None:
        """依赖钩子:工具缺失时安装进该会话容器(持久)。

        目录外工具(如 wine)状态为 unknown/missing → 交给 install_tools 动态解析安装;
        解析不到或装不上在 install_tools 内部收口,这里不抛、不阻塞命令执行。
        """
        try:
            st = (await self.tools.probe_tool(tool_id, session_key=key)).get("status")
        except Exception:
            return  # 探测失败不阻塞执行,命令如实报错
        if st == "missing":
            await self.tools.install_tools([tool_id], session_key=key)
        elif st == "unknown" and self.tools.catalog.get_tool(tool_id) is None:
            await self.tools.install_tools([tool_id], session_key=key)

    # ===== 工具委托 =====

    async def probe_tool(self, tool_id: str, session_key: str | None = None) -> dict:
        return await self.tools.probe_tool(tool_id, session_key=session_key)

    async def install_tools(self, tool_ids, *, session_key=None, force=False) -> dict:
        return await self.tools.install_tools(tool_ids, session_key=session_key, force=force)

    def tool_conflicts(self) -> list[dict]:
        return self.tools.tool_conflicts()

    # ===== 结果归一 =====

    def _to_outcome(self, raw: ExecOutcome, cmd, target: str, t0: float):
        from agent.runner import RunOutcome  # lazy:避免模块级循环导入

        ms = int((time.perf_counter() - t0) * 1000)
        return RunOutcome(
            ok=not raw.timed_out and raw.returncode == 0,
            returncode=raw.returncode,
            stdout=self._cap(self._decode(raw.stdout), self.max_out),
            stderr=self._cap(self._decode(raw.stderr), self.max_err),
            cmd=list(cmd) if isinstance(cmd, list) else str(cmd),
            target=target,
            timed_out=raw.timed_out,
            elapsed_ms=ms,
        )

    @staticmethod
    def _decode(data: bytes) -> str:
        if not data:
            return ""
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("utf-8", errors="replace")

    @staticmethod
    def _cap(text: str, n: int) -> str:
        if len(text) <= n:
            return text
        note = f"…(截断,共 {len(text)} 字符)"
        return text[:max(0, n - len(note))] + note
