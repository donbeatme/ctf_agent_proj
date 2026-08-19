"""命令执行:仅沙箱(SSH→远程 Docker 容器)唯一执行面。

执行 agent(②)的核心原语。命令只允许在适配器的沙箱内执行——沙箱未配置
(缺 CTF_SSH_HOST 或凭据)时返回 ok=False 错误结果,绝不回退宿主 subprocess /
wsl / 本机 docker(防 LLM 借显式 target 越权读宿主敏感文件)。

CommandRunner 是薄门面:持有/懒建 SandboxManager(config_sandbox 提供凭据),
run / run_python 委托 sandbox.exec / sandbox.run_python(容器生命周期、工作目录
同步、依赖安装由 SandboxManager 负责)。
"""

from __future__ import annotations

from dataclasses import dataclass

_MAX_OUT = 4000   # stdout 截断(工具消息/上下文体积)
_MAX_ERR = 2000   # stderr 截断

_SANDBOX_MSG = "沙箱未配置(缺 CTF_SSH_HOST 或凭据),命令无法执行——绝不回退宿主执行"


@dataclass
class ProcOutcome:
    """一次底层进程执行的归一结果(SSH 通道/测试替身返回)。"""

    returncode: int | None
    stdout: bytes = b""
    stderr: bytes = b""
    timed_out: bool = False


@dataclass
class RunOutcome:
    """一次沙箱执行的结果。as_dict 喂回 LLM 工具消息。"""

    ok: bool
    returncode: int | None
    stdout: str
    stderr: str
    cmd: str | list
    target: str
    timed_out: bool = False
    elapsed_ms: int = 0

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "cmd": self.cmd,
            "target": self.target,
            "timed_out": self.timed_out,
            "elapsed_ms": self.elapsed_ms,
        }


class CommandRunner:
    """沙箱唯一执行门面:懒建 SandboxManager,run/run_python 全部委托沙箱。

    无沙箱(SandboxManager 构造失败)时 run 返回 ok=False 错误结果
    (LLM 工具循环内非致命),不执行任何宿主命令。
    """

    def __init__(self, sandbox=None, timeout: float = 120.0,
                 max_out: int = _MAX_OUT, max_err: int = _MAX_ERR):
        self.sandbox = sandbox
        self.timeout = timeout
        self.max_out = max_out
        self.max_err = max_err
        self._sandbox_tried = False

    def _ensure_sandbox(self):
        """懒建 SandboxManager(读 config_sandbox);构造失败返回 None。"""
        if self.sandbox is None and not self._sandbox_tried:
            self._sandbox_tried = True
            try:
                from sandbox_env import SandboxManager
                self.sandbox = SandboxManager(max_out=self.max_out, max_err=self.max_err)
            except Exception:
                self.sandbox = None
        return self.sandbox

    def _unavailable(self, cmd) -> RunOutcome:
        return RunOutcome(
            ok=False, returncode=None, stdout="", stderr=_SANDBOX_MSG,
            cmd=cmd, target="none",
        )

    def run(self, cmd, *, cwd=None, category=None, tool_id=None,
            timeout=None) -> RunOutcome:
        """在沙箱内执行命令(容器生命周期 + cwd 同步 + 依赖安装由沙箱负责)。"""
        sb = self._ensure_sandbox()
        if sb is None:
            return self._unavailable(cmd)
        return sb.exec(cmd, cwd=cwd, category=category, tool_id=tool_id,
                       timeout=timeout or self.timeout)

    def run_python(self, code, *, cwd=None, category=None, tool_id=None,
                   timeout=None) -> RunOutcome:
        """在沙箱内执行一段 Python(沙箱写 _ctf_exec.py 后运行)。"""
        sb = self._ensure_sandbox()
        if sb is None:
            return self._unavailable(code)
        return sb.run_python(code, cwd=cwd, category=category, tool_id=tool_id,
                             timeout=timeout or self.timeout)
