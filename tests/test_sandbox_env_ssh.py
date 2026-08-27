"""SshSandboxBackend:per-challenge 持久容器生命周期经 FakeSsh 记录驱动。"""

from pathlib import Path

from agent.runner import ProcOutcome
from sandbox_env import SandboxSettings, session_key_for
from sandbox_env.base import container_name_for
from sandbox_env.ssh_backend import SshSandboxBackend


class FakeSsh:
    """记录 exec/sync_to 调用,固定返回 ProcOutcome(SSH 通道替身)。"""

    def __init__(self):
        self.execs = []  # (cmd, timeout)
        self.syncs = []  # (local, remote)

    async def exec(self, cmd_str, timeout=None):
        self.execs.append((cmd_str, timeout))
        if cmd_str.startswith("docker ps -aq"):
            return ProcOutcome(0, b"", b"")  # 无该容器 → 需要创建
        if cmd_str.startswith("docker run -d"):
            return ProcOutcome(0, b"", b"")
        if cmd_str.startswith("docker rm -f"):
            return ProcOutcome(0, b"", b"")
        if cmd_str.startswith("docker exec"):
            return ProcOutcome(0, b"OUT", b"")
        return ProcOutcome(0, b"", b"")

    async def sync_to(self, local, remote):
        self.syncs.append((Path(local), remote))

    async def close(self):
        pass


def _backend(ssh=None):
    return SshSandboxBackend(SandboxSettings(ssh_host="vm"), ssh=ssh or FakeSsh())


async def test_ensure_creates_container_once():
    ssh = FakeSsh()
    bk = _backend(ssh)
    assert await bk.ensure("abc123") == "ctf-abc123"
    assert await bk.ensure("abc123") == "ctf-abc123"  # 缓存命中,不再 docker ps
    assert ssh.execs[0][0].startswith("docker ps -aq --filter name=^/ctf-abc123$")
    runs = [c for c, _ in ssh.execs if "docker run -d" in c]
    assert len(runs) == 1
    assert "ctf-abc123" in runs[0] and "sleep infinity" in runs[0]


async def test_exec_runs_docker_exec_in_container():
    ssh = FakeSsh()
    bk = _backend(ssh)
    out = await bk.exec("echo hi", session_key="k1")
    assert out.returncode == 0 and out.stdout == b"OUT"
    last = ssh.execs[-1][0]
    assert last.startswith("docker exec ctf-k1 /bin/bash -lc")
    assert "'echo hi'" in last


def test_per_challenge_container_isolation(tmp_path):
    key_a = session_key_for(str(tmp_path / "a"))
    key_b = session_key_for(str(tmp_path / "b"))
    assert key_a != key_b
    assert container_name_for(key_a) != container_name_for(key_b)


async def test_sync_uploads_to_session_subdir(tmp_path):
    ssh = FakeSsh()
    bk = _backend(ssh)
    await bk.sync(tmp_path, "k1")
    assert ssh.syncs == [(tmp_path.resolve(), "/root/ctf/k1")]


async def test_cleanup_removes_container():
    ssh = FakeSsh()
    bk = _backend(ssh)
    await bk.cleanup("k1")
    assert any("docker rm -f ctf-k1" in c for c, _ in ssh.execs)


async def test_cleanup_failure_records_cleanup_event():
    """docker rm 失败:记 container_removed_failed(CLEANUP),不再无条件报 removed。"""
    from opslog import attach, detach

    class _FailRm(FakeSsh):
        async def exec(self, cmd_str, timeout=None):
            self.execs.append((cmd_str, timeout))
            if cmd_str.startswith("docker rm -f"):
                return ProcOutcome(1, b"", b"Error: No such container")
            return await super().exec(cmd_str, timeout)

    bk = _backend(ssh=_FailRm())
    seen = []
    sink = lambda kind, detail: seen.append((kind, detail))
    attach(sink)
    try:
        await bk.cleanup("k1")
    finally:
        detach(sink)
    fail_ev = [d for k, d in seen if k == "sandbox.container_removed_failed"]
    assert len(fail_ev) == 1
    assert fail_ev[0]["level"] == "cleanup"
    assert "docker rm 失败" in fail_ev[0]["reason"]


def test_is_ready_requires_host():
    assert SshSandboxBackend(SandboxSettings(), ssh=FakeSsh()).is_ready() is False
    assert SshSandboxBackend(SandboxSettings(ssh_host="vm"), ssh=FakeSsh()).is_ready() is True
