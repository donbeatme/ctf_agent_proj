"""SSH 远程执行后端:SshBackend(paramiko 通道,懒加载)。

执行 agent(②)的远程目标通道。Alpine VM 只做 SSH 入口 + docker 宿主,命令经
`docker run` 在 Debian 沙箱容器内执行(CTF skill 的命令是 Debian 系)。本模块负责:

- `exec(cmd_str, timeout)`:在远程 shell 跑一条命令,归一为 ProcOutcome。
- `sync(local_dir)`:SFTP 增量上传本地目录到远程工作目录(大小+mtime 相同跳过,
  忽略 .git/__pycache__;不做远程删除)。注意 `_ctf_exec.py` 不能忽略:run_python
  工具先写该脚本再 sync,忽略会导致远端缺文件无法执行。

paramiko 为可选依赖:方法内 lazy import,未安装时 SshBackend 构造不失败,由
runner 的 ssh_available() 判定不可用并回落其它目标。
"""

from __future__ import annotations

from pathlib import Path

from opslog import ErrorLevel, record_error

_IGNORED = {".git", "__pycache__"}


class SshBackend:
    """一个 SSH 连接(连接惰性建立,首次 exec/sync 时连)。"""

    def __init__(self, host: str, user: str, password: str,
                 workdir: str = "/root/ctf", port: int = 22,
                 connect_timeout: float = 8.0):
        self.host = host
        self.user = user
        self.password = password
        self.workdir = workdir
        self.port = port
        self.connect_timeout = connect_timeout
        self._client = None  # paramiko.SSHClient(懒)
        self._sftp = None    # paramiko.SFTPClient(懒)

    # ===== 连接 =====

    def _connect(self):
        if self._client is None:
            import paramiko

            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            c.connect(self.host, port=self.port, username=self.user,
                      password=self.password, timeout=self.connect_timeout)
            self._client = c
        return self._client

    def close(self) -> None:
        for conn in (self._sftp, self._client):
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        self._client = None
        self._sftp = None

    # ===== 执行 =====

    def exec(self, cmd_str: str, timeout: float):
        """远程执行一条命令(无 shell 拆分,cmd_str 即完整远程命令)。超时打标。"""
        from agent.runner import ProcOutcome

        client = self._connect()
        try:
            chan = client.get_transport().open_session()
            chan.settimeout(timeout)
            chan.exec_command(cmd_str)
            out = _read_all(chan.makefile("rb"))
            err = _read_all(chan.makefile_stderr("rb"))
            rc = chan.recv_exit_status()
            return ProcOutcome(rc, out, err)
        except TimeoutError:
            return ProcOutcome(None, b"", b"", timed_out=True)
        except Exception as exc:  # 连接/协议异常 → 失败 observation + 进审计线,不崩引擎
            record_error("ssh", "exec", exc=exc, level=ErrorLevel.RECOVERABLE,
                         host=self.host, reason="SSH 传输层异常(区别于命令失败)")
            return ProcOutcome(None, b"", str(exc).encode("utf-8", "replace"))

    # ===== 文件同步 =====

    def sync(self, local_dir) -> None:
        """把本地目录增量上传到远程 workdir(文件级别:mtime+size 相同跳过)。"""
        self.sync_to(local_dir, self.workdir)

    def sync_to(self, local_dir, remote_dir: str) -> None:
        """增量上传本地目录到远程指定子目录(沙箱每会话工作区用)。"""
        sftp = self._sftp or self._connect().open_sftp()
        self._sftp = sftp
        self._mkdir_p(sftp, remote_dir)
        self._sync_dir(sftp, Path(local_dir), remote_dir)

    def _sync_dir(self, sftp, local: Path, remote: str) -> None:
        for p in sorted(local.iterdir()):
            if p.name in _IGNORED:
                continue
            rp = f"{remote}/{p.name}"
            if p.is_dir():
                self._mkdir_p(sftp, rp)
                self._sync_dir(sftp, p, rp)
            elif self._needs_upload(sftp, p, rp):
                sftp.put(str(p), rp)

    @staticmethod
    def _needs_upload(sftp, local: Path, remote: str) -> bool:
        try:
            st = sftp.stat(remote)
        except FileNotFoundError:
            return True
        ls = local.stat()
        return st.st_size != ls.st_size or int(st.st_mtime) != int(ls.st_mtime)

    @staticmethod
    def _mkdir_p(sftp, path: str) -> None:
        cur = ""
        for seg in path.strip("/").split("/"):
            if not seg:
                continue
            cur += "/" + seg
            try:
                sftp.stat(cur)
            except FileNotFoundError:
                try:
                    sftp.mkdir(cur)
                except OSError:
                    pass


def _read_all(f) -> bytes:
    return f.read()


__all__ = ["SshBackend", "_IGNORED"]
