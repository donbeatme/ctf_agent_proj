"""SshBackend.sync 的增量逻辑(用假 SFTP 客户端,不连真实 SSH)。

覆盖:
- 首次同步上传全部文件,并逐级建 workdir
- 二次同步:大小+mtime 未变 → 跳过(不重复 put)
- 文件改动 → 重新上传;忽略 .git/__pycache__(但 _ctf_exec.py 必须上传——run_python 依赖它)
- _needs_upload 判定:不存在/大小不同/mtime 不同 → 需上传
"""

import asyncssh
from pathlib import Path

from agent.ssh import SshBackend


async def test_transport_error_records_event(monkeypatch):
    """SSH 传输层异常 → ssh.exec_failed 进审计线(区别于命令失败)。"""
    from opslog import attach, detach

    class _Client:
        async def run(self, cmd_str, *, check=False, encoding=None):
            raise ConnectionResetError("connection reset by peer")

    async def _fake_connect(self):
        return _Client()

    monkeypatch.setattr(SshBackend, "_connect", _fake_connect)
    sb = SshBackend(host="vm", user="u", password="p")
    seen = []
    sink = lambda kind, detail: seen.append((kind, detail))
    attach(sink)
    try:
        out = await sb.exec("ls", timeout=5)
    finally:
        detach(sink)
    assert out.returncode is None
    fail_ev = [d for k, d in seen if k == "ssh.exec_failed"]
    assert len(fail_ev) == 1
    assert "ConnectionResetError" in fail_ev[0]["error"]
    assert fail_ev[0]["host"] == "vm"


class _Stat:
    def __init__(self, size, mtime):
        self.size = size
        self.mtime = mtime


class FakeSftp:
    """最小 SFTP 客户端替身:记录 put/mkdir,维护远程 stat 表。"""

    def __init__(self):
        self.files = {}  # remote path -> (size, mtime)
        self.put_calls = []
        self.mkdirs = []

    async def stat(self, path):
        if path in self.files:
            return _Stat(*self.files[path])
        raise asyncssh.SFTPNoSuchFile(path)

    async def mkdir(self, path):
        self.mkdirs.append(path)

    async def put(self, local, remote):
        self.put_calls.append((local, remote))
        st = Path(local).stat()
        self.files[remote] = (st.st_size, int(st.st_mtime))


class _FakeConn:
    """替身连接:start_sftp_client 返回托管 FakeSftp 的异步上下文管理器。"""

    def __init__(self, sftp):
        self._sftp = sftp

    async def start_sftp_client(self):
        return _SftpCtx(self._sftp)


class _SftpCtx:
    def __init__(self, sftp):
        self._sftp = sftp

    async def __aenter__(self):
        return self._sftp

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _backend(sftp):
    b = SshBackend(host="h", user="root", password="p", workdir="/root/ctf")
    b._client = _FakeConn(sftp)  # 绕过 asyncssh 连接,注入替身
    return b


def _make_tree(root: Path):
    (root / "a.txt").write_text("hello", encoding="utf-8")
    sub = root / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("world", encoding="utf-8")
    # 忽略项
    (root / ".git").mkdir()
    (root / "_ctf_exec_abc123.py").write_text("print(1)", encoding="utf-8")
    (root / "__pycache__").mkdir()


async def test_sync_uploads_new_files_and_mkdirs(tmp_path):
    _make_tree(tmp_path)
    sftp = FakeSftp()
    await _backend(sftp).sync(tmp_path)

    uploaded = {remote for _, remote in sftp.put_calls}
    # _ctf_exec_*.py 必须上传:run_python 先本地写脚本再 sync,忽略会导致远端缺文件
    assert uploaded == {"/root/ctf/a.txt", "/root/ctf/sub/b.txt", "/root/ctf/_ctf_exec_abc123.py"}
    assert "/root/ctf" in sftp.mkdirs
    assert "/root/ctf/sub" in sftp.mkdirs


async def test_sync_skips_unchanged_on_second_run(tmp_path):
    _make_tree(tmp_path)
    sftp = FakeSftp()
    b = _backend(sftp)
    await b.sync(tmp_path)
    first = list(sftp.put_calls)
    await b.sync(tmp_path)
    assert sftp.put_calls == first  # 无变化 → 不重复上传


async def test_sync_repaints_modified_file(tmp_path):
    _make_tree(tmp_path)
    sftp = FakeSftp()
    b = _backend(sftp)
    await b.sync(tmp_path)
    # 改动一个文件(size 变化)再同步 → 只有它被重新上传
    (tmp_path / "a.txt").write_text("hello modified", encoding="utf-8")
    await b.sync(tmp_path)
    assert sftp.put_calls[-1] == (str(tmp_path / "a.txt"), "/root/ctf/a.txt")


async def test_needs_upload():
    import tempfile

    sftp = FakeSftp()
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "f.txt"
        p.write_text("data", encoding="utf-8")
        sftp.files["/r/f.txt"] = (p.stat().st_size, int(p.stat().st_mtime))
        assert await SshBackend._needs_upload(sftp, p, "/r/f.txt") is False
        assert await SshBackend._needs_upload(sftp, p, "/r/none.txt") is True
        sftp.files["/r/f2.txt"] = (p.stat().st_size, int(p.stat().st_mtime) + 1)
        assert await SshBackend._needs_upload(sftp, p, "/r/f2.txt") is True
        sftp.files["/r/f3.txt"] = (p.stat().st_size + 1, int(p.stat().st_mtime))
        assert await SshBackend._needs_upload(sftp, p, "/r/f3.txt") is True
