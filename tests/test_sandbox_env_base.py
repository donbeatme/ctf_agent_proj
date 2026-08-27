"""SandboxManager 门面 + SandboxBackend 基类:FakeBackend 证明管理器与后端解耦。"""

import re
from pathlib import Path

import pytest

from agent.runner import RunOutcome
from sandbox_env import SandboxBackend, SandboxManager, SandboxSettings
from sandbox_env.base import ExecOutcome, container_name_for, session_key_for
from sandbox_env.errors import SandboxUnavailableError


class FakeBackend(SandboxBackend):
    """记录 ensure/exec/sync/cleanup 调用,exec 结果可脚本化。"""

    name = "fake"

    def __init__(self, settings=None, exec_fn=None):
        super().__init__(settings or SandboxSettings())
        self.ensured = []
        self.synced = []
        self.cleaned = []
        self.calls = []
        self._exec_fn = exec_fn or (lambda cmd, **kw: ExecOutcome(0, b"ok", b""))

    async def ensure(self, session_key=None):
        self.ensured.append(session_key)
        return "fake-ctr"

    async def exec(self, cmd_str, *, session_key=None, timeout=None):
        self.calls.append(cmd_str)
        return self._exec_fn(cmd_str, session_key=session_key, timeout=timeout)

    async def sync(self, local_dir, session_key=None):
        self.synced.append((Path(local_dir), session_key))

    async def cleanup(self, session_key=None):
        self.cleaned.append(session_key)


# ===== session_key / 容器名 =====


def test_session_key_stable_and_distinct(tmp_path):
    a = str(tmp_path / "challenge-a")
    b = str(tmp_path / "challenge-b")
    assert session_key_for(a) == session_key_for(a)
    assert session_key_for(a) != session_key_for(b)
    assert len(session_key_for(a)) == 12


def test_container_name_docker_valid():
    name = container_name_for("abc123def456")
    assert name == "ctf-abc123def456"
    assert re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name)


# ===== 门面 exec 流程 =====


async def test_manager_exec_ensure_sync_then_run(tmp_path):
    bk = FakeBackend()
    m = SandboxManager(backend=bk)
    out = await m.exec("echo hi", cwd=tmp_path, target="ssh")
    assert out.ok and out.target == "ssh" and out.stdout == "ok"
    assert isinstance(out, RunOutcome)
    key = session_key_for(tmp_path)
    assert bk.ensured == [key]
    assert bk.synced == [(tmp_path.resolve(), key)]
    assert bk.calls == ["echo hi"]


async def test_exec_dep_hook_installs_missing_tool(tmp_path):
    installed = {"ok": False}

    def exec_fn(cmd, **kw):
        if cmd == "python3 -c 'import pwn'":
            return ExecOutcome(0 if installed["ok"] else 1, b"", b"")
        if cmd == "apt-get update":
            return ExecOutcome(0, b"", b"")
        if "pip install" in cmd or "apt-get install" in cmd:
            installed["ok"] = True
            return ExecOutcome(0, b"", b"")
        return ExecOutcome(0, b"out", b"")

    bk = FakeBackend(exec_fn=exec_fn)
    m = SandboxManager(backend=bk)
    out = await m.exec("python3 -c 'print(1)'", cwd=tmp_path, tool_id="pwntools")
    assert out.ok
    # 依赖钩子:缺失 → 自动装(pip --break-system-packages),先于实际命令
    install_cmd = "python3 -m pip install --break-system-packages pwntools==4.15.0"
    assert install_cmd in bk.calls
    assert bk.calls.index("python3 -c 'print(1)'") > bk.calls.index(install_cmd)


async def test_exec_dep_hook_skipped_when_install_auto_off(tmp_path):
    bk = FakeBackend()
    m = SandboxManager(backend=bk, settings=SandboxSettings(install_auto=False))
    await m.exec("echo hi", cwd=tmp_path, tool_id="pwntools")
    assert bk.calls == ["echo hi"]  # 未探测、未安装


async def test_run_python_writes_syncs_and_runs(tmp_path):
    bk = FakeBackend()
    m = SandboxManager(backend=bk)
    out = await m.run_python("print('x')", cwd=tmp_path)
    assert out.ok
    key = session_key_for(tmp_path)
    assert (tmp_path / f"_ctf_exec_{key}.py").read_text(encoding="utf-8") == "print('x')"
    assert bk.synced == [(tmp_path.resolve(), key)]
    assert bk.calls == [f"python3 /work/_ctf_exec_{key}.py"]


async def test_cleanup_delegates(tmp_path):
    bk = FakeBackend()
    m = SandboxManager(backend=bk)
    key = session_key_for(tmp_path)
    await m.cleanup(key)
    assert bk.cleaned == [key]


async def test_exec_event_carries_cmd_and_sync_success(tmp_path):
    """exec 事件带 cmd 内容;sync 成功也进事件(补审计线缺口)。"""
    from opslog import attach, detach

    bk = FakeBackend()
    m = SandboxManager(backend=bk)
    seen = []
    sink = lambda kind, detail: seen.append((kind, detail))
    attach(sink)
    try:
        out = await m.exec("echo hi", cwd=tmp_path)
    finally:
        detach(sink)
    assert out.ok
    exec_ev = [d for k, d in seen if k == "sandbox.exec"]
    assert exec_ev and exec_ev[0]["cmd"] == "echo hi"
    assert [d for k, d in seen if k == "sandbox.sync"]  # 同步成功也要进事件


async def test_sync_failure_recorded_not_silent(tmp_path):
    """附件目录同步失败:不阻断本次命令,但必须报错进 log(不能静默吞掉)。"""
    from opslog import attach, detach

    class _BoomSync(FakeBackend):
        async def sync(self, local_dir, session_key=None):
            raise FileNotFoundError("distfiles 缺失(本地挑战目录未就绪)")

    m = SandboxManager(backend=_BoomSync())
    seen = []
    sink = lambda kind, detail: seen.append((kind, detail))
    attach(sink)
    try:
        out = await m.exec("ls", cwd=tmp_path)
    finally:
        detach(sink)
    assert out.ok  # RECOVERABLE: 记录后继续,不阻断命令
    sync_events = [d for k, d in seen if k == "sandbox.sync_failed"]
    assert len(sync_events) == 1
    assert sync_events[0]["level"] == "recoverable"
    assert "distfiles 缺失" in sync_events[0]["error"]


def test_manager_without_ssh_raises():
    with pytest.raises(SandboxUnavailableError, match="CTF_SSH_HOST"):
        SandboxManager(settings=SandboxSettings())  # 无 host → 拒绝构造 SshSandboxBackend
