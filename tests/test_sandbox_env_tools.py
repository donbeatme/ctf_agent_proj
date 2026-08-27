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
    assert tm.probe_tool("wireshark")["status"] == "incompatible"


def test_probe_download_uses_command_v():
    bk = FakeBackend()
    tm = ToolManager(bk)
    assert tm.probe_tool("ghidra")["status"] == "missing"
    assert bk.calls[0] == "command -v analyzeHeadless"
    bk.available.add("analyzeHeadless")
    assert tm.probe_tool("ghidra")["status"] == "available"


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
    assert tm.install_commands(["wireshark", "pwndbg"]) == {}


def test_install_commands_download():
    tm = ToolManager()
    cmds = tm.install_commands(["ghidra"])
    assert "apt-get install -y --no-install-recommends curl unzip" in cmds["ghidra"]
    assert "analyzeHeadless" in cmds["ghidra"]
    assert cmds["ghidra"].startswith("DEBIAN_FRONTEND=noninteractive")


def test_install_tools_buckets():
    bk = FakeBackend()
    tm = ToolManager(bk)
    r = tm.install_tools(["gdb", "ghidra", "wireshark", "pwndbg", "no-such"])
    assert r["installed"] == ["gdb", "ghidra"]
    assert r["incompatible"] == ["wireshark"]
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


# ===== 非目录工具:按需动态安装 + 装不上的回退 =====


class AptFakeBackend(SandboxBackend):
    """模拟 apt 源:apt_pkgs 里的非目录包可装(apt-cache show 命中),其余解析失败。"""

    name = "apt-fake"

    def __init__(self, apt_pkgs=()):
        super().__init__(SandboxSettings())
        self.calls = []
        self.apt_pkgs = set(apt_pkgs)
        self.installed = set()

    def exec(self, cmd_str, *, session_key=None, timeout=None):
        self.calls.append(cmd_str)
        if cmd_str.startswith("command -v "):
            name = cmd_str.split("command -v ", 1)[1].strip("'\"")
            return ExecOutcome(0 if name in self.installed else 1, b"", b"")
        if cmd_str.startswith("apt-cache show "):
            name = cmd_str.split("apt-cache show ", 1)[1].strip("'\"")
            if name in self.apt_pkgs:
                return ExecOutcome(0, f"Package: {name}\nVersion: 1.0\n".encode(), b"")
            return ExecOutcome(100, b"", b"")
        if cmd_str.startswith("python3 -m pip index versions "):
            return ExecOutcome(1, b"", b"")
        if cmd_str == "apt-get update":
            return ExecOutcome(0, b"", b"")
        if "apt-get install -y " in cmd_str:
            self.installed.add(cmd_str.split("apt-get install -y ", 1)[1].strip())
            return ExecOutcome(0, b"", b"")
        return ExecOutcome(0, b"", b"")


def test_probe_noncatalog_by_name():
    bk = AptFakeBackend()
    tm = ToolManager(bk)
    assert tm.probe_tool("wine")["status"] == "missing"
    assert bk.calls[0] == "command -v wine"
    bk.installed.add("wine")
    assert tm.probe_tool("wine")["status"] == "available"


def test_dynamic_install_apt_package():
    bk = AptFakeBackend(apt_pkgs={"wine"})
    tm = ToolManager(bk)
    r = tm.install_tools(["wine"])
    assert r["installed"] == ["wine"] and r["failed"] == []
    assert tm.probe_tool("wine")["status"] == "available"
    assert bk.calls.index("apt-get update") < bk.calls.index(
        "DEBIAN_FRONTEND=noninteractive apt-get install -y wine"
    )
    # 已装过:再次 install 不再重装,也不报 failed(非目录 probe 恒 miss,靠缓存跳过)
    r2 = tm.install_tools(["wine"])
    assert r2["installed"] == [] and r2["failed"] == []
    assert bk.calls.count("DEBIAN_FRONTEND=noninteractive apt-get install -y wine") == 1


def test_dynamic_install_unavailable_fallback():
    bk = AptFakeBackend(apt_pkgs=set())
    tm = ToolManager(bk)
    r = tm.install_tools(["definitely-not-a-pkg"])
    assert r["installed"] == [] and r["failed"] == ["definitely-not-a-pkg"]
    assert not any("apt-get install" in c for c in bk.calls)
    # 失败缓存:第二次不再重复解析(apt-cache 不再出现),不抛异常
    n_first = bk.calls.count("apt-cache show definitely-not-a-pkg")
    tm.install_tools(["definitely-not-a-pkg"])
    assert bk.calls.count("apt-cache show definitely-not-a-pkg") == n_first
    assert not any("apt-get install" in c for c in bk.calls)


def test_dynamic_install_force_retries():
    bk = AptFakeBackend(apt_pkgs={"wine"})
    tm = ToolManager(bk)
    tm.install_tools(["wine"])
    r = tm.install_tools(["wine"], force=True)
    assert r["installed"] == ["wine"]
    assert bk.calls.count("DEBIAN_FRONTEND=noninteractive apt-get install -y wine") == 2


def test_dynamic_install_unsafe_name_no_exec():
    bk = AptFakeBackend(apt_pkgs={"wine"})
    tm = ToolManager(bk)
    r = tm.install_tools(["wine; rm -rf /"])
    assert r["failed"] == ["wine; rm -rf /"]
    # 不安全名称:不产生任何解析/安装命令(防注入)
    assert not any("apt-cache" in c or "pip index" in c or "apt-get install" in c
                   for c in bk.calls)


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
    assert {"wireshark", "bind"} <= inc
    assert "ghidra" not in inc


def test_conflicts_warnings():
    tm = ToolManager()
    items = tm.tool_conflicts()
    warns = {(i["a"], i["b"]) for i in items if i["severity"] == "warning"}
    assert ("uncompyle6", None) in warns
    assert ("ropgadget", "ropper") in warns
