"""SSH 远程执行后端:SshBackend(asyncssh 通道,懒加载,全异步)。

执行 agent(②)的远程目标通道。Alpine VM 只做 SSH 入口 + docker 宿主,命令经
`docker run` 在 Debian 沙箱容器内执行(CTF skill 的命令是 Debian 系)。本模块负责:

- `exec(cmd_str, timeout)`:在远程 shell 跑一条命令,归一为 ProcOutcome。
- `sync(local_dir)`:SFTP 增量上传本地目录到远程工作目录(大小+mtime 相同跳过,
  忽略 .git/__pycache__;不做远程删除)。注意 `_ctf_exec.py` 不能忽略:run_python
  工具先写该脚本再 sync,忽略会导致远端缺文件无法执行。

asyncssh 为可选依赖:方法内 lazy import,未安装时 SshBackend 构造不失败,由
runner 的 ssh_available() 判定不可用并回落其它目标。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from opslog import ErrorLevel, record_error

_IGNORED = {".git", "__pycache__"}


class SshBackend:
    """一个 SSH 连接(连接惰性建立,首次 exec/sync 时连)。所有 I/O 方法为 async。"""

    def __init__(self, host: str, user: str, password: str,
                 workdir: str = "/root/ctf", port: int = 22,
                 connect_timeout: float = 8.0):
        self.host = host
        self.user = user
        self.password = password
        self.workdir = workdir
        self.port = port
        self.connect_timeout = connect_timeout
        self._client = None  # asyncssh.SSHClientConnection(懒)

    # ===== 连接 =====

    async def _connect(self):
        if self._client is None:
            import asyncssh

            self._client = await asyncssh.connect(
                self.host, port=self.port, username=self.user,
                password=self.password, known_hosts=None,
                connect_timeout=self.connect_timeout)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()
            try:
                await asyncio.wait_for(self._client.wait_closed(), 5)
            except Exception:
                pass
        self._client = None

    # ===== 执行 =====

    async def exec(self, cmd_str: str, timeout: float):
        """远程执行一条命令(无 shell 拆分,cmd_str 即完整远程命令)。超时打标。"""
        from agent.runner import ProcOutcome

        conn = await self._connect()
        try:
            proc = await asyncio.wait_for(
                conn.run(cmd_str, check=False, encoding=None), timeout)
            return ProcOutcome(proc.returncode, proc.stdout, proc.stderr)
        except asyncio.TimeoutError:
            return ProcOutcome(None, b"", b"", timed_out=True)
        except Exception as exc:  # 连接/协议异常 → 失败 observation + 进审计线,不崩引擎
            record_error("ssh", "exec", exc=exc, level=ErrorLevel.RECOVERABLE,
                         host=self.host, reason="SSH 传输层异常(区别于命令失败)")
            return ProcOutcome(None, b"", str(exc).encode("utf-8", "replace"))

    # ===== 文件同步 =====

    async def sync(self, local_dir) -> None:
        """把本地目录增量上传到远程 workdir(文件级别:mtime+size 相同跳过)。"""
        await self.sync_to(local_dir, self.workdir)

    async def sync_to(self, local_dir, remote_dir: str) -> None:
        """增量上传本地目录到远程指定子目录(沙箱每会话工作区用)。"""
        conn = await self._connect()
        async with await conn.start_sftp_client() as sftp:
            await self._mkdir_p(sftp, remote_dir)
            await self._sync_dir(sftp, Path(local_dir), remote_dir)

    async def _sync_dir(self, sftp, local: Path, remote: str) -> None:
        for p in sorted(local.iterdir()):
            if p.name in _IGNORED:
                continue
            rp = f"{remote}/{p.name}"
            if p.is_dir():
                await self._mkdir_p(sftp, rp)
                await self._sync_dir(sftp, p, rp)
            elif await self._needs_upload(sftp, p, rp):
                await sftp.put(str(p), rp)

    @staticmethod
    async def _needs_upload(sftp, local: Path, remote: str) -> bool:
        import asyncssh  # lazy:asyncssh 为可选依赖

        try:
            st = await sftp.stat(remote)
        except asyncssh.SFTPNoSuchFile:
            return True
        ls = local.stat()
        return st.size != ls.st_size or int(st.mtime) != int(ls.st_mtime)

    @staticmethod
    async def _mkdir_p(sftp, path: str) -> None:
        import asyncssh  # lazy:asyncssh 为可选依赖

        cur = ""
        for seg in path.strip("/").split("/"):
            if not seg:
                continue
            cur += "/" + seg
            try:
                await sftp.stat(cur)
            except asyncssh.SFTPNoSuchFile:
                try:
                    await sftp.mkdir(cur)
                except asyncssh.SFTPError:
                    pass


__all__ = ["SshBackend", "_IGNORED"]
