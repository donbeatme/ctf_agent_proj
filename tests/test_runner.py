"""命令执行层:CommandRunner 沙箱唯一执行面(无宿主 windows/wsl/docker 路径)。

覆盖:
- 委托沙箱:run / run_python 全部走 sandbox.exec / sandbox.run_python(cwd/category/tool_id/timeout 透传)
- 无沙箱:返回 ok=False 错误结果,绝不回退宿主执行
- RunOutcome.as_dict / 超时透传 / ProcOutcome 导出
"""

from pathlib import Path

from agent.runner import CommandRunner, ProcOutcome, RunOutcome


class StubSandbox:
    """记录 exec/run_python 调用,固定返回 RunOutcome(沙箱管理器替身)。"""

    def __init__(self, target_name="ssh", outcome=None):
        self.target_name = target_name
        self.exec_calls = []
        self.run_python_calls = []
        self.outcome = outcome or RunOutcome(
            ok=True, returncode=0, stdout="sbx", stderr="",
            cmd=None, target=target_name,
        )

    def exec(self, cmd, *, cwd=None, category=None, tool_id=None, target=None, timeout=None):
        self.exec_calls.append({"cmd": cmd, "cwd": cwd, "category": category,
                                "tool_id": tool_id, "timeout": timeout})
        return self.outcome

    def run_python(self, code, *, cwd=None, category=None, tool_id=None,
                   target=None, timeout=None):
        self.run_python_calls.append({"code": code, "cwd": cwd, "category": category,
                                      "tool_id": tool_id, "timeout": timeout})
        return self.outcome


# ===== 委托沙箱 =====


def test_run_delegates_to_sandbox(tmp_path):
    sbx = StubSandbox()
    r = CommandRunner(sandbox=sbx, timeout=7.0)
    out = r.run("ROPgadget --binary a", cwd=str(tmp_path), category="ctf-pwn",
                tool_id="ROPgadget", timeout=10)
    assert out.target == "ssh" and out.stdout == "sbx"
    assert sbx.exec_calls == [{"cmd": "ROPgadget --binary a", "cwd": str(tmp_path),
                               "category": "ctf-pwn", "tool_id": "ROPgadget", "timeout": 10}]


def test_run_default_timeout(tmp_path):
    sbx = StubSandbox()
    r = CommandRunner(sandbox=sbx, timeout=5.0)
    r.run("ls", cwd=tmp_path)
    assert sbx.exec_calls[0]["timeout"] == 5.0


def test_run_python_delegates_to_sandbox(tmp_path):
    sbx = StubSandbox()
    r = CommandRunner(sandbox=sbx)
    out = r.run_python("import pwn", cwd=tmp_path, category="ctf-pwn", tool_id="pwntools")
    assert out.stdout == "sbx" and out.target == "ssh"
    assert sbx.run_python_calls[0]["code"] == "import pwn"


def test_run_python_does_not_write_script_locally(tmp_path):
    sbx = StubSandbox()
    r = CommandRunner(sandbox=sbx)
    r.run_python("print(1)", cwd=tmp_path)
    assert not (tmp_path / "_ctf_exec.py").exists()  # 脚本由沙箱自管,runner 不写


def test_docker_backend_target_name(tmp_path):
    sbx = StubSandbox(target_name="docker")
    r = CommandRunner(sandbox=sbx)
    out = r.run("ls", cwd=tmp_path)
    assert out.target == "docker"


# ===== 无沙箱:不回退宿主 =====


def test_no_sandbox_returns_error_not_host_exec(monkeypatch, tmp_path):
    # 无沙箱凭据:不建任何宿主 subprocess,直接返回 ok=False 错误结果
    import config_sandbox
    monkeypatch.setattr(config_sandbox, "_CONFIG_FILE", Path(tmp_path) / "nope.json")
    r = CommandRunner()
    out = r.run("whoami", cwd=tmp_path)
    assert out.ok is False
    assert out.returncode is None
    assert out.target == "none"
    assert "沙箱未配置" in out.stderr


def test_no_sandbox_run_python_error(monkeypatch, tmp_path):
    import config_sandbox
    monkeypatch.setattr(config_sandbox, "_CONFIG_FILE", Path(tmp_path) / "nope.json")
    r = CommandRunner()
    out = r.run_python("print(1)", cwd=tmp_path)
    assert out.ok is False and out.target == "none"


# ===== 超时与结果形状 =====


def test_timeout_passthrough(tmp_path):
    sbx = StubSandbox(outcome=RunOutcome(
        ok=False, returncode=None, stdout="", stderr="", cmd=None,
        target="ssh", timed_out=True))
    r = CommandRunner(sandbox=sbx)
    out = r.run("sleep 10", timeout=0.01)
    assert out.timed_out is True
    assert out.ok is False
    assert out.returncode is None


def test_outcome_as_dict_shape(tmp_path):
    sbx = StubSandbox()
    r = CommandRunner(sandbox=sbx)
    d = r.run("python x.py", cwd=tmp_path).as_dict()
    for k in ("ok", "returncode", "stdout", "stderr", "cmd", "target",
              "timed_out", "elapsed_ms"):
        assert k in d


def test_proc_outcome_still_exported():
    assert ProcOutcome(0, b"o", b"e").stdout == b"o"
