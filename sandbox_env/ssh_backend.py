"""SSH 沙箱后端:SshSandboxBackend——per-challenge 持久容器生命周期经 SSH 管理。

Alpine VM 只做 SSH 入口 + docker 宿主。每个 challenge 会话一个持久容器
(容器名 ctf-<session_key>),容器内安装持久;工作目录挂载 {ssh_workdir}/{key}:/work。

- ensure: docker ps 查容器,无则 docker run -d 常驻(sleep infinity)
- exec:   docker exec <name> /bin/bash -lc <cmd>
- cleanup: docker rm -f <name>
- sync:   委托 agent.ssh.SshBackend.sync_to(local_dir, {workdir}/{key}) 增量上传

全异步(asyncssh)。一条 SshBackend 持一条 asyncssh 连接;同一沙箱后端被并发
ex 复用时,连接复用即现成(每 ex 独立后端实例,见 Phase 3 actor 设计)。
"""

from __future__ import annotations

import shlex

from opslog import ErrorLevel, emit, record_error

from .base import ExecOutcome, SandboxBackend, container_name_for
from .config import SandboxSettings
from .errors import SandboxExecError


class SshSandboxBackend(SandboxBackend):
    name = "ssh"

    def __init__(self, settings: SandboxSettings, ssh=None):
        super().__init__(settings)
        if ssh is None:
            from agent.ssh import SshBackend  # lazy:asyncssh 仅真实运行时才引

            ssh = SshBackend(
                host=settings.ssh_host, user=settings.ssh_user,
                password=settings.ssh_password, workdir=settings.ssh_workdir,
            )
        self.ssh = ssh
        self._created: set[str] = set()  # 本进程内已确认存在的容器,免重复 docker ps

    def is_ready(self) -> bool:
        return bool(self.settings.ssh_configured)

    def _session_dir(self, key: str) -> str:
        return f"{self.settings.ssh_workdir.rstrip('/')}/{key}"

    # ---- 生命周期 ----

    async def ensure(self, session_key: str | None = None) -> str:
        key = session_key or "default"
        name = container_name_for(key)
        if name in self._created:
            return name
        # 精确匹配:容器名可能带 / 前缀(锚定避免误复用到其它 ctf-* 容器)
        out = await self.ssh.exec(f"docker ps -aq --filter name=^/{name}$", timeout=30)
        exists = out.returncode == 0 and bool(out.stdout.strip())
        if not exists:
            rdir = self._session_dir(key)
            cmd = (
                f"mkdir -p {shlex.quote(rdir)} && "
                f"docker run -d --name {name} "
                f"-v {shlex.quote(rdir)}:/work -w /work "
                f"{self.settings.image} sleep infinity"
            )
            r = await self.ssh.exec(cmd, timeout=120)
            if r.returncode != 0:
                raise SandboxExecError(
                    f"容器创建失败(ssh exec rc={r.returncode}): "
                    f"{r.stdout.decode('utf-8', 'replace')[:200]} "
                    f"{r.stderr.decode('utf-8', 'replace')[:200]}"
                )
            emit("sandbox", "container_created", session_key=key, container=name)
        else:
            emit("sandbox", "container_reused", session_key=key, container=name)
        self._created.add(name)
        return name

    async def exec(self, cmd_str: str, *, session_key: str | None = None,
                   timeout: float | None = None) -> ExecOutcome:
        key = session_key or "default"
        name = await self.ensure(key)
        full = f"docker exec {name} /bin/bash -lc {shlex.quote(cmd_str)}"
        try:
            raw = await self.ssh.exec(full, timeout if timeout is not None else 120)
        except Exception as exc:  # 通道异常 → 失败 observation,不崩引擎
            return ExecOutcome(None, b"", str(exc).encode("utf-8", "replace"))
        return ExecOutcome(raw.returncode, raw.stdout, raw.stderr, raw.timed_out)

    async def sync(self, local_dir, session_key: str | None = None) -> None:
        if not session_key:
            return
        rdir = self._session_dir(session_key)
        if hasattr(self.ssh, "sync_to"):
            await self.ssh.sync_to(local_dir, rdir)
        else:
            raise SandboxExecError("ssh 后端缺少 sync_to(需 agent.ssh.SshBackend.sync_to)")

    async def cleanup(self, session_key: str | None = None) -> None:
        key = session_key or "default"
        name = container_name_for(key)
        try:
            out = await self.ssh.exec(f"docker rm -f {name}", timeout=60)
            if out.returncode != 0:
                record_error("sandbox", "container_removed", level=ErrorLevel.CLEANUP,
                             session_key=key, container=name,
                             reason=f"docker rm 失败 rc={out.returncode}: "
                                    f"{out.stderr.decode('utf-8', 'replace')[:200]}")
                return
        except Exception as exc:  # noqa: BLE001 — 连接异常也记 CLEANUP,不静默
            record_error("sandbox", "container_removed", exc=exc, level=ErrorLevel.CLEANUP,
                         session_key=key, container=name)
            return
        emit("sandbox", "container_removed", session_key=key, container=name)
        self._created.discard(name)

    async def close(self) -> None:
        if hasattr(self.ssh, "close"):
            await self.ssh.close()
