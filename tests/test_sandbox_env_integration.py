"""整链集成:FakeSsh + SshSandboxBackend + SandboxManager + CommandRunner + RealExecutor。

pwn 分类命令 → resolve 到 ssh → manager ensure 容器 → 自动装缺失 gdb → 沙箱内执行。
"""

from agent.executor import RealExecutor
from agent.runner import CommandRunner, ProcOutcome
from sandbox_env import SandboxManager, SandboxSettings, session_key_for
from sandbox_env.ssh_backend import SshSandboxBackend


class ScriptedFakeSsh:
    """脚本化 SSH 通道:容器生命周期命令固定成功;探针随安装状态变化。"""

    def __init__(self):
        self.execs = []  # cmd 列表
        self.syncs = []  # (local, remote)
        self.available = set()
        self._installed = False

    def exec(self, cmd_str, timeout=None):
        self.execs.append(cmd_str)
        if cmd_str.startswith("docker ps -aq"):
            return ProcOutcome(0, b"", b"")
        if cmd_str.startswith("docker run -d"):
            return ProcOutcome(0, b"", b"")
        if cmd_str.startswith("docker rm -f"):
            return ProcOutcome(0, b"", b"")
        if cmd_str.startswith("docker exec"):
            if "command -v " in cmd_str:
                name = cmd_str.split("command -v ", 1)[1].split("'", 1)[0]
                ok = name in self.available or self._installed
                return ProcOutcome(0 if ok else 1, b"", b"")
            if "python3 -c " in cmd_str:
                return ProcOutcome(0 if self._installed else 1, b"", b"")
            return ProcOutcome(0, b"OUT", b"")
        if cmd_str == "apt-get update":
            return ProcOutcome(0, b"", b"")
        if "apt-get install" in cmd_str or "pip install" in cmd_str:
            self._installed = True
            return ProcOutcome(0, b"", b"")
        return ProcOutcome(0, b"", b"")

    def sync_to(self, local, remote):
        self.syncs.append((str(local), remote))

    def close(self):
        pass


def test_pwn_command_full_chain(tmp_path):
    ssh = ScriptedFakeSsh()
    settings = SandboxSettings(ssh_host="vm")
    backend = SshSandboxBackend(settings, ssh=ssh)
    mgr = SandboxManager(settings=settings, backend=backend)
    runner = CommandRunner(sandbox=mgr)
    ex = RealExecutor(runner=runner, workdir=str(tmp_path))

    result = ex._run_command({"command": "gdb -q ./pwn1", "tool_id": "gdb"},
                             category="ctf-pwn")

    assert result["ok"] is True
    assert result["target"] == "ssh"
    assert result["stdout"] == "OUT"

    key = session_key_for(tmp_path)
    # 容器创建 + 工作目录上传到会话子目录
    assert any("docker run -d --name ctf-" in c for c in ssh.execs)
    assert (str(tmp_path.resolve()), f"/root/ctf/{key}") in ssh.syncs
    # 依赖自动装进容器(gdb),且先于实际命令执行
    i_install = next(i for i, c in enumerate(ssh.execs) if "apt-get install -y gdb" in c)
    i_cmd = next(i for i, c in enumerate(ssh.execs) if "gdb -q ./pwn1" in c)
    assert i_install < i_cmd
