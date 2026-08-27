"""命令执行层:CommandRunner 沙箱唯一执行面(无宿主 windows/wsl/docker 路径)。

覆盖:
- 委托沙箱:run / run_python 全部走 sandbox.exec / sandbox.run_python(cwd/category/tool_id/timeout 透传)
- 无沙箱:返回 ok=False 错误结果,绝不回退宿主执行
- RunOutcome.as_dict / 超时透传 / ProcOutcome 导出
"""

import sys
import time
import types
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

    async def exec(self, cmd, *, cwd=None, category=None, tool_id=None, target=None, timeout=None):
        self.exec_calls.append({"cmd": cmd, "cwd": cwd, "category": category,
                                "tool_id": tool_id, "timeout": timeout})
        return self.outcome

    async def run_python(self, code, *, cwd=None, category=None, tool_id=None,
                         target=None, timeout=None):
        self.run_python_calls.append({"code": code, "cwd": cwd, "category": category,
                                      "tool_id": tool_id, "timeout": timeout})
        return self.outcome


# ===== 委托沙箱 =====


async def test_run_delegates_to_sandbox(tmp_path):
    sbx = StubSandbox()
    r = CommandRunner(sandbox=sbx, timeout=7.0)
    out = await r.run("ROPgadget --binary a", cwd=str(tmp_path), category="ctf-pwn",
                      tool_id="ROPgadget", timeout=10)
    assert out.target == "ssh" and out.stdout == "sbx"
    assert sbx.exec_calls == [{"cmd": "ROPgadget --binary a", "cwd": str(tmp_path),
                               "category": "ctf-pwn", "tool_id": "ROPgadget", "timeout": 10}]


async def test_run_default_timeout(tmp_path):
    sbx = StubSandbox()
    r = CommandRunner(sandbox=sbx, timeout=5.0)
    await r.run("ls", cwd=tmp_path)
    assert sbx.exec_calls[0]["timeout"] == 5.0


async def test_run_python_delegates_to_sandbox(tmp_path):
    sbx = StubSandbox()
    r = CommandRunner(sandbox=sbx)
    out = await r.run_python("import pwn", cwd=tmp_path, category="ctf-pwn", tool_id="pwntools")
    assert out.stdout == "sbx" and out.target == "ssh"
    assert sbx.run_python_calls[0]["code"] == "import pwn"


async def test_run_python_does_not_write_script_locally(tmp_path):
    sbx = StubSandbox()
    r = CommandRunner(sandbox=sbx)
    await r.run_python("print(1)", cwd=tmp_path)
    assert not any(p.name.startswith("_ctf_exec") for p in tmp_path.iterdir())  # 脚本由沙箱自管,runner 不写


async def test_docker_backend_target_name(tmp_path):
    sbx = StubSandbox(target_name="docker")
    r = CommandRunner(sandbox=sbx)
    out = await r.run("ls", cwd=tmp_path)
    assert out.target == "docker"


# ===== 无沙箱:不回退宿主 =====


async def test_no_sandbox_returns_error_not_host_exec(monkeypatch, tmp_path):
    # 无沙箱凭据:不建任何宿主 subprocess,直接返回 ok=False 错误结果
    import config_sandbox
    monkeypatch.setattr(config_sandbox, "_CONFIG_FILE", Path(tmp_path) / "nope.json")
    r = CommandRunner()
    out = await r.run("whoami", cwd=tmp_path)
    assert out.ok is False
    assert out.returncode is None
    assert out.target == "none"
    assert "沙箱未配置" in out.stderr


async def test_no_sandbox_run_python_error(monkeypatch, tmp_path):
    import config_sandbox
    monkeypatch.setattr(config_sandbox, "_CONFIG_FILE", Path(tmp_path) / "nope.json")
    r = CommandRunner()
    out = await r.run_python("print(1)", cwd=tmp_path)
    assert out.ok is False and out.target == "none"


def test_sandbox_init_failure_records_and_backs_off(monkeypatch):
    """沙箱构造失败:记错误事件 + 持续失败亮 probe,退避期内不再反复重试构造。"""

    class _Boom:
        def __init__(self, *a, **k):
            self.calls = getattr(self.__class__, "calls", 0) + 1
            self.__class__.calls = self.calls
            raise RuntimeError("no ssh creds")

    fake = types.ModuleType("sandbox_env")
    fake.SandboxManager = _Boom
    monkeypatch.setitem(sys.modules, "sandbox_env", fake)

    r = CommandRunner()
    assert r._ensure_sandbox() is None
    assert r.sandbox is None
    assert r.sandbox_blocked() is True  # 持续失败 → 能力探测亮起
    # 退避期内不重试构造
    r._ensure_sandbox()
    assert _Boom.calls == 1
    # 退避期满后允许再试(仍失败则重新记时)
    r._sandbox_failed_at = time.monotonic() - 61
    assert r._ensure_sandbox() is None
    assert _Boom.calls == 2
    assert r.sandbox_blocked() is True


# ===== 超时与结果形状 =====


async def test_timeout_passthrough(tmp_path):
    sbx = StubSandbox(outcome=RunOutcome(
        ok=False, returncode=None, stdout="", stderr="", cmd=None,
        target="ssh", timed_out=True))
    r = CommandRunner(sandbox=sbx)
    out = await r.run("sleep 10", timeout=0.01)
    assert out.timed_out is True
    assert out.ok is False
    assert out.returncode is None


async def test_outcome_as_dict_shape(tmp_path):
    sbx = StubSandbox()
    r = CommandRunner(sandbox=sbx)
    d = (await r.run("python x.py", cwd=tmp_path)).as_dict()
    for k in ("ok", "returncode", "stdout", "stderr", "cmd", "target",
              "timed_out", "elapsed_ms"):
        assert k in d


def test_proc_outcome_still_exported():
    assert ProcOutcome(0, b"o", b"e").stdout == b"o"
