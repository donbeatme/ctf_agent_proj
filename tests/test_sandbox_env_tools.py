"""ToolManager:探测 / 安装 OS 适配 / 冲突与不兼容分析。FakeBackend 记录命令 + 脚本化返回。"""

from sandbox_env import SandboxBackend, SandboxSettings
from sandbox_env.base import ExecOutcome
from sandbox_env.tools import ToolManager


class FakeBackend(SandboxBackend):
    """记录 exec 命令;安装命令执行后所有探针命中(容器内安装持久)。"""

    name = "fake"

    def __init__(self):
        super().__init__(SandboxSettings())
        self.calls = []
        self.available = set()   # 探针可命中的 verify 名(import 模块 或 CLI 名)
        self._installed = False

    def exec(self, cmd_str, *, session_key=None, timeout=None):
        self.calls.append(cmd_str)
        if cmd_str.startswith("python3 -c "):
            mod = cmd_str.split("python3 -c ", 1)[1].strip("'\"")
            return ExecOutcome(0 if (mod in self.available or self._installed) else 1, b"", b"")
        if cmd_str.startswith("command -v "):
            name = cmd_str.split("command -v ", 1)[1].strip("'\"")
            return ExecOutcome(0 if (name in self.available or self._installed) else 1, b"", b"")
        if cmd_str == "apt-get update":
            return ExecOutcome(0, b"", b"")
        if any(k in cmd_str for k in ("pip install", "apt-get install", "gem install", "go install")):
            self._installed = True
            return ExecOutcome(0, b"", b"")
        return ExecOutcome(0, b"", b"")


# ===== 探测 =====


def test_probe_pip_uses_import_check():
    bk = FakeBackend()
    tm = ToolManager(bk)
    tm.probe_tool("pwntools")
    assert bk.calls[0] == "python3 -c 'import pwn'"


def test_probe_cli_uses_command_v():
    bk = FakeBackend()
    tm = ToolManager(bk)
    assert tm.probe_tool("gdb")["status"] == "missing"
    assert bk.calls[0] == "command -v gdb"


def test_probe_unknown_manual_brew():
    tm = ToolManager()
    assert tm.probe_tool("no-such-tool")["status"] == "unknown"
    assert tm.probe_tool("pwndbg")["status"] == "manual"
    assert tm.probe_tool("ghidra")["status"] == "incompatible"


# ===== 安装 =====


def test_install_missing_then_available():
    bk = FakeBackend()
    tm = ToolManager(bk)
    assert tm.probe_tool("gdb")["status"] == "missing"
    r = tm.install_tools(["gdb"])
    assert r["installed"] == ["gdb"] and r["failed"] == []
    assert tm.probe_tool("gdb")["status"] == "available"
    # apt 先 update 一次,再装
    assert bk.calls.index("apt-get update") < bk.calls.index(
        "DEBIAN_FRONTEND=noninteractive apt-get install -y gdb"
    )


def test_install_commands_os_adaptation():
    tm = ToolManager()
    cmds = tm.install_commands(["pwntools", "gdb", "one_gadget", "ffuf"])
    assert cmds["pwntools"] == "python3 -m pip install --break-system-packages pwntools==4.15.0"
    assert cmds["gdb"] == "DEBIAN_FRONTEND=noninteractive apt-get install -y gdb"
    assert cmds["one_gadget"] == (
        "DEBIAN_FRONTEND=noninteractive apt-get install -y ruby && gem install one_gadget"
    )
    assert "apt-get install -y golang" in cmds["ffuf"]
    assert cmds["ffuf"].endswith("go install github.com/ffuf/ffuf/v2@latest")


def test_brew_manual_have_no_install_command():
    tm = ToolManager()
    assert tm.install_commands(["ghidra", "pwndbg"]) == {}


def test_install_tools_buckets():
    bk = FakeBackend()
    tm = ToolManager(bk)
    r = tm.install_tools(["gdb", "ghidra", "pwndbg", "no-such"])
    assert r["installed"] == ["gdb"]
    assert r["incompatible"] == ["ghidra"]
    assert r["skipped_manual"] == ["pwndbg"]
    assert r["failed"] == ["no-such"]


def test_install_already_available_skips():
    bk = FakeBackend()
    bk.available.add("gdb")
    tm = ToolManager(bk)
    r = tm.install_tools(["gdb"])
    assert r["installed"] == []
    assert not any("apt-get install" in c for c in bk.calls)


def test_install_force_reinstalls_even_if_available():
    bk = FakeBackend()
    bk.available.add("gdb")
    tm = ToolManager(bk)
    r = tm.install_tools(["gdb"], force=True)
    assert r["installed"] == ["gdb"]


# ===== 冲突与不兼容 =====


def test_conflicts_finds_bind_dnsutils():
    tm = ToolManager()
    items = tm.tool_conflicts()
    dig = [i for i in items if i["severity"] == "conflict"]
    assert any(i["a"] == "dnsutils" and i["b"] == "bind" and "dig" in i["reason"] for i in dig)


def test_conflicts_brew_only_incompatible():
    tm = ToolManager()
    items = tm.tool_conflicts()
    inc = {i["a"] for i in items if i["severity"] == "incompatible"}
    assert {"ghidra", "wireshark", "bind"} <= inc


def test_conflicts_warnings():
    tm = ToolManager()
    items = tm.tool_conflicts()
    warns = {(i["a"], i["b"]) for i in items if i["severity"] == "warning"}
    assert ("uncompyle6", None) in warns
    assert ("ropgadget", "ropper") in warns
