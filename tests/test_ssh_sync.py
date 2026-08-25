"""SshBackend.sync 的增量逻辑(用假 SFTP 客户端,不连真实 SSH)。

覆盖:
- 首次同步上传全部文件,并逐级建 workdir
- 二次同步:大小+mtime 未变 → 跳过(不重复 put)
- 文件改动 → 重新上传;忽略 .git/__pycache__/_ctf_exec.py
- _needs_upload 判定:不存在/大小不同/mtime 不同 → 需上传
"""

from pathlib import Path

from agent.ssh import SshBackend


def test_transport_error_records_event(monkeypatch):
    """SSH 传输层异常 → ssh.exec_failed 进审计线(区别于命令失败)。"""
    from opslog import attach, detach

    class _Chan:
        def open_session(self):
            raise ConnectionResetError("connection reset by peer")

    class _Client:
        def get_transport(self):
            return _Chan()

    monkeypatch.setattr(SshBackend, "_connect", lambda self: _Client())
    sb = SshBackend(host="vm", user="u", password="p")
    seen = []
    sink = lambda kind, detail: seen.append((kind, detail))
    attach(sink)
    try:
        out = sb.exec("ls", timeout=5)
    finally:
        detach(sink)
    assert out.returncode is None
    fail_ev = [d for k, d in seen if k == "ssh.exec_failed"]
    assert len(fail_ev) == 1
    assert "ConnectionResetError" in fail_ev[0]["error"]
    assert fail_ev[0]["host"] == "vm"


class _Stat:
    def __init__(self, size, mtime):
        self.st_size = size
        self.st_mtime = mtime


class FakeSftp:
    """最小 SFTP 客户端替身:记录 put/mkdir,维护远程 stat 表。"""

    def __init__(self):
        self.files = {}  # remote path -> (size, mtime)
        self.put_calls = []
        self.mkdirs = []

    def stat(self, path):
        if path in self.files:
            return _Stat(*self.files[path])
        raise FileNotFoundError(path)

    def mkdir(self, path):
        self.mkdirs.append(path)

    def put(self, local, remote):
        self.put_calls.append((local, remote))
        st = Path(local).stat()
        self.files[remote] = (st.st_size, int(st.st_mtime))


def _backend(sftp):
    b = SshBackend(host="h", user="root", password="p", workdir="/root/ctf")
    b._sftp = sftp  # 绕过 paramiko 连接,注入替身
    return b


def _make_tree(root: Path):
    (root / "a.txt").write_text("hello", encoding="utf-8")
    sub = root / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("world", encoding="utf-8")
    # 忽略项
    (root / ".git").mkdir()
    (root / "_ctf_exec.py").write_text("print(1)", encoding="utf-8")
    (root / "__pycache__").mkdir()


def test_sync_uploads_new_files_and_mkdirs(tmp_path):
    _make_tree(tmp_path)
    sftp = FakeSftp()
    _backend(sftp).sync(tmp_path)

    uploaded = {remote for _, remote in sftp.put_calls}
    assert uploaded == {"/root/ctf/a.txt", "/root/ctf/sub/b.txt"}
    assert "/root/ctf" in sftp.mkdirs
    assert "/root/ctf/sub" in sftp.mkdirs


def test_sync_skips_unchanged_on_second_run(tmp_path):
    _make_tree(tmp_path)
    sftp = FakeSftp()
    b = _backend(sftp)
    b.sync(tmp_path)
    first = list(sftp.put_calls)
    b.sync(tmp_path)
    assert sftp.put_calls == first  # 无变化 → 不重复上传


def test_sync_repaints_modified_file(tmp_path):
    _make_tree(tmp_path)
    sftp = FakeSftp()
    b = _backend(sftp)
    b.sync(tmp_path)
    # 改动一个文件(size 变化)再同步 → 只有它被重新上传
    (tmp_path / "a.txt").write_text("hello modified", encoding="utf-8")
    b.sync(tmp_path)
    assert sftp.put_calls[-1] == (str(tmp_path / "a.txt"), "/root/ctf/a.txt")


def test_needs_upload():
    import tempfile

    sftp = FakeSftp()
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "f.txt"
        p.write_text("data", encoding="utf-8")
        sftp.files["/r/f.txt"] = (p.stat().st_size, int(p.stat().st_mtime))
        assert SshBackend._needs_upload(sftp, p, "/r/f.txt") is False
        assert SshBackend._needs_upload(sftp, p, "/r/none.txt") is True
        sftp.files["/r/f2.txt"] = (p.stat().st_size, int(p.stat().st_mtime) + 1)
        assert SshBackend._needs_upload(sftp, p, "/r/f2.txt") is True
        sftp.files["/r/f3.txt"] = (p.stat().st_size + 1, int(p.stat().st_mtime))
        assert SshBackend._needs_upload(sftp, p, "/r/f3.txt") is True
