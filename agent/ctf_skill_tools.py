"""ctf-skills 工具与依赖声明 → 现有 ws.tools 工具目录(纯声明,不接执行)。

来源与许可:skill 库 vendored 自 https://github.com/ljagiello/ctf-skills(MIT © 2026
Lukasz Jagiello,LICENSE 见 skills/ctf-skills/LICENSE)。TOOL_MANIFEST 手抄自其
scripts/install_ctf_tools.sh(权威依赖清单),本模块只做声明式打包:把工具/依赖信息
归一化成 ws.tools 可消费的条目 + 分类级查询 API。**不做任何执行**——不注册可调用
函数、不触发安装;executor 怎么调工具不属于本模块职责。

三层工具/依赖信息对应:
- TOOL_MANIFEST:install_ctf_tools.sh 的逐工具清单(含 install_command 下载方式
  + verify_check 校验项),跨安装方式去重。
- 每分类 SKILL.md frontmatter 的 allowed-tools(agent 原生工具白名单)/compatibility
  (运行时要求),按分类暴露。
- 每分类 SKILL.md 的 ## Prerequisites 安装命令块,install_commands() 原样提取。
"""

import re
from pathlib import Path

from agent.schema import ID_PATTERN
from agent.skills import SKILLS_DIR, SkillLibrary, _parse_frontmatter, _strip_frontmatter

INSTALL_METHODS = ("pip", "apt", "brew", "gem", "go", "manual", "download", "git")

# install_commands() 提取命令时匹配的安装动词
_INSTALL_VERB_RE = re.compile(
    r"(pip install|python3 -m pip install|apt install|apt-get install|brew install|"
    r"gem install|go install|r2pm|git clone)"
)


def _pip(name, version, import_name, description):
    """pip 条目的静态构造:tool_id 取包名小写化(dot → hyphen 保 spec name 合法),
    verify_check 用脚本的 import 别名(Pillow→import PIL、dnspython→import dns)。"""
    return {
        "tool_id": name.lower().replace(".", "-"),
        "name": name,
        "install_method": "pip",
        "install_command": f"pip install {name}=={version}",
        "verify_check": f"import {import_name}",
        "description": description,
    }


def _sys(tool_id, name, install_method, install_command, verify_check, description,
         alt_methods=None):
    return {
        "tool_id": tool_id,
        "name": name,
        "install_method": install_method,
        "install_command": install_command,
        "verify_check": verify_check,
        "description": description,
        "alt_methods": alt_methods or [],
    }


# 逐工具清单(手抄自 scripts/install_ctf_tools.sh;alt_methods 为跨安装方式去重后的
# 次要安装方式,不进 ws.tools)。主方式优先级 pip > apt > brew > gem > go > manual。
TOOL_MANIFEST: list[dict] = [
    # ---- pip(install_ctf_tools.sh PIP_PACKAGES,格式 name==version:import) ----
    _pip("pwntools", "4.15.0", "pwn", "pwntools exploit/交互框架"),
    _pip("pycryptodome", "3.23.0", "Crypto", "密码学原语(PyCrypto 继任)"),
    _pip("z3-solver", "4.13.0.0", "z3", "Z3 约束求解器"),
    _pip("sympy", "1.14.0", "sympy", "符号数学计算"),
    _pip("gmpy2", "2.3.0", "gmpy2", "多精度整数运算(GMP)"),
    _pip("hashpumpy", "1.2", "hashpumpy", "哈希长度扩展攻击"),
    _pip("fpylll", "0.6.4", "fpylll", "LLL 格基归约"),
    _pip("py_ecc", "8.0.0", "py_ecc", "椭圆曲线密码原语"),
    _pip("angr", "9.2.193", "angr", "符号执行/二进制分析框架"),
    _pip("frida-tools", "14.8.0", "frida", "动态插桩(hook/内存扫描)"),
    _pip("qiling", "1.4.6", "qiling", "跨平台 OS 级模拟框架"),
    _pip("requests", "2.32.5", "requests", "HTTP 客户端"),
    _pip("flask-unsign", "1.2.1", "flask_unsign", "Flask session 伪造/解密"),
    _pip("sqlmap", "1.10.3", "sqlmap", "自动化 SQL 注入"),
    _pip("ropper", "1.13.13", "ropper", "ROP gadget 搜索"),
    _pip("ROPgadget", "7.7", "ropgadget", "ROP gadget 搜索(binary 分析)"),
    _pip("volatility3", "2.27.0", "volatility3", "内存取证框架"),
    _pip("yara-python", "4.5.4", "yara", "YARA 规则匹配"),
    _pip("pefile", "2024.8.26", "pefile", "PE 文件解析"),
    _pip("capstone", "5.0.3", "capstone", "反汇编框架"),
    _pip("oletools", "0.60.2", "oletools", "OLE/宏分析(olevba/oleid)"),
    _pip("unicorn", "2.1.2", "unicorn", "CPU 模拟引擎"),
    _pip("scapy", "2.7.0", "scapy", "网络报文构造/解析"),
    _pip("Pillow", "11.3.0", "PIL", "图像处理/LSB 隐写"),
    _pip("numpy", "2.2.6", "numpy", "数值计算"),
    _pip("matplotlib", "3.10.8", "matplotlib", "绘图(频谱/波形可视化)"),
    _pip("shodan", "1.31.0", "shodan", "Shodan 资产搜索 API"),
    _pip("uncompyle6", "3.9.3", "uncompyle6", "Python 字节码反编译(≤3.8)"),
    _pip("lief", "0.17.6", "lief", "二进制解析/修改库"),
    _pip("dnspython", "2.8.0", "dns", "DNS 查询库"),
    _pip("dnslib", "0.9.26", "dnslib", "DNS 协议库"),
    _pip("dissect.cobaltstrike", "1.2.1", "dissect.cobaltstrike",
         "Cobalt Strike 配置/内存取证解析"),
    # ---- apt(install_ctf_tools.sh install_apt) ----
    _sys("gdb", "gdb", "apt", "apt-get install -y gdb", "gdb", "GNU 调试器(动态分析)",
         ["brew"]),
    _sys("radare2", "radare2", "apt", "apt-get install -y radare2", "r2",
         "radare2 逆向/反汇编框架", ["brew"]),
    _sys("binutils", "binutils", "apt", "apt-get install -y binutils", "objdump",
         "GNU 二进制工具(objdump/nm 等)", ["brew"]),
    _sys("binwalk", "binwalk", "apt", "apt-get install -y binwalk", "binwalk",
         "固件/文件签名提取", ["brew"]),
    _sys("foremost", "foremost", "apt", "apt-get install -y foremost", "foremost",
         "文件雕刻(file carving)"),
    _sys("exiftool", "exiftool", "apt", "apt-get install -y libimage-exiftool-perl",
         "exiftool", "元数据查看/编辑(EXIF)", ["brew"]),
    _sys("tshark", "tshark", "apt", "apt-get install -y tshark", "tshark",
         "抓包分析(Wireshark CLI)"),
    _sys("sleuthkit", "sleuthkit", "apt", "apt-get install -y sleuthkit", "fls",
         "磁盘/文件系统取证", ["brew"]),
    _sys("ffmpeg", "ffmpeg", "apt", "apt-get install -y ffmpeg", "ffmpeg",
         "音视频处理(频谱隐写)", ["brew"]),
    _sys("steghide", "steghide", "apt", "apt-get install -y steghide", "steghide",
         "隐写隐藏/提取", ["manual"]),
    _sys("testdisk", "testdisk", "apt", "apt-get install -y testdisk", "testdisk",
         "分区/文件恢复", ["brew"]),
    _sys("john", "john", "apt", "apt-get install -y john", "john",
         "密码哈希破解(John the Ripper)", ["brew"]),
    _sys("pcapfix", "pcapfix", "apt", "apt-get install -y pcapfix", "pcapfix",
         "修复损坏的 pcap"),
    _sys("nmap", "nmap", "apt", "apt-get install -y nmap", "nmap",
         "端口/服务扫描", ["brew"]),
    _sys("whois", "whois", "apt", "apt-get install -y whois", "whois",
         "域名注册信息查询", ["brew"]),
    _sys("dnsutils", "dnsutils", "apt", "apt-get install -y dnsutils", "dig",
         "DNS 查询工具(dig/nslookup)"),
    _sys("hashcat", "hashcat", "apt", "apt-get install -y hashcat", "hashcat",
         "GPU 密码哈希破解", ["brew"]),
    _sys("strace", "strace", "apt", "apt-get install -y strace", "strace",
         "系统调用跟踪"),
    _sys("ltrace", "ltrace", "apt", "apt-get install -y ltrace", "ltrace",
         "库调用跟踪"),
    _sys("imagemagick", "imagemagick", "apt", "apt-get install -y imagemagick",
         "convert", "图像处理 CLI(convert)", ["brew"]),
    _sys("curl", "curl", "apt", "apt-get install -y curl", "curl",
         "HTTP 请求", ["brew"]),
    _sys("jq", "jq", "apt", "apt-get install -y jq", "jq", "JSON 解析", ["brew"]),
    _sys("apktool", "apktool", "apt", "apt-get install -y apktool", "apktool",
         "APK 解包/重打包", ["brew"]),
    _sys("upx", "upx", "apt", "apt-get install -y upx", "upx",
         "可执行文件加壳/脱壳", ["brew"]),
    _sys("qemu", "qemu-system-x86", "apt", "apt-get install -y qemu-system-x86",
         "qemu-system-x86_64", "系统模拟/跨架构执行", ["brew"]),
    _sys("sagemath", "sagemath", "apt", "apt-get install -y sagemath", "sage",
         "数学系统(数论/格)", ["manual"]),
    _sys("qrencode", "qrencode", "apt", "apt-get install -y qrencode", "qrencode",
         "QR 码生成", ["brew"]),
    # ---- brew-only(install_ctf_tools.sh install_brew 中 apt 没有的) ----
    # ghidra:官方 zip 不带 JRE(镜像实装 JDK21 + launch.properties JAVA_HOME_OVERRIDE,
    # 见 scripts/Dockerfile.ctf-sandbox),Debian 无 brew 改 download 直装;_adapt 会前置装 curl/unzip
    _sys("ghidra", "ghidra", "download",
         "curl -fsSL -o /opt/ghidra.zip https://github.com/NationalSecurityAgency/ghidra/releases/download/Ghidra_12.1.3_build/ghidra_12.1.3_PUBLIC_20260817.zip "
         "&& unzip -q /opt/ghidra.zip -d /opt && rm -f /opt/ghidra.zip "
         "&& ln -sf /opt/ghidra_12.1.3_PUBLIC/support/analyzeHeadless /usr/local/bin/analyzeHeadless",
         "analyzeHeadless",
         "NSA 逆向框架(headless 反编译)。analyzeHeadless 已装 /usr/local/bin,容器已装 "
         "JDK 21 且 launch.properties 已钉 JAVA_HOME_OVERRIDE——直接跑 analyzeHeadless,勿装 Java/JDK"),
    _sys("wireshark", "wireshark", "brew", "brew install wireshark", "wireshark",
         "抓包图形分析"),
    _sys("bind", "bind", "brew", "brew install bind", "dig",
         "DNS 工具(dig/nslookup)"),
    # ---- gem(install_ctf_tools.sh install_gems) ----
    _sys("one_gadget", "one_gadget", "gem", "gem install one_gadget", "one_gadget",
         "one-gadget 偏移搜索"),
    _sys("seccomp-tools", "seccomp-tools", "gem", "gem install seccomp-tools",
         "seccomp-tools", "seccomp 规则解析"),
    _sys("zsteg", "zsteg", "gem", "gem install zsteg", "zsteg", "PNG/BMP 隐写检测"),
    # ---- go(install_ctf_tools.sh install_go) ----
    _sys("ffuf", "ffuf", "go", "go install github.com/ffuf/ffuf/v2@latest", "ffuf",
         "Web 目录/参数 fuzz"),
    # ---- git(源码构建,可自动化;原 print_manual 类,命令补齐后走 git 适配) ----
    _sys("pwndbg", "pwndbg", "git",
         "git clone --depth 1 https://github.com/pwndbg/pwndbg /opt/pwndbg && "
         "cd /opt/pwndbg && ./setup.sh && "
         "echo '#!/bin/sh' > /usr/local/bin/pwndbg && "
         "echo 'exec gdb -q -ex \"source /opt/pwndbg/gdbinit.py\" \"$@\"' >> /usr/local/bin/pwndbg && "
         "chmod +x /usr/local/bin/pwndbg",
         "pwndbg", "GDB 插件(漏洞利用辅助)"),
    _sys("RsaCtfTool", "RsaCtfTool", "git",
         "git clone --depth 1 https://github.com/RsaCtfTool/RsaCtfTool /opt/RsaCtfTool && "
         "cd /opt/RsaCtfTool && "
         "python3 -m pip install --break-system-packages .",
         "RsaCtfTool", "RSA 攻击自动化"),
    _sys("pycdc", "pycdc", "git",
         "git clone --depth 1 https://github.com/zrax/pycdc /opt/pycdc && "
         "cd /opt/pycdc && cmake . && make && "
         "cp pycdc /usr/local/bin/pycdc",
         "pycdc", "Python 字节码反编译(≥3.9)"),
    # ---- manual(install_ctf_tools.sh print_manual,无法可靠自动化) ----
    _sys("dnSpy", "dnSpy", "manual",
         "https://github.com/dnSpy/dnSpy (Windows/.NET only)", "", ".NET 反编译(Windows)"),
]


def _install_lines(body: str) -> list[str]:
    """提取 SKILL.md ## Prerequisites 段里的安装命令行(代码围栏行 + 含安装动词的行内代码)。"""
    lines = body.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.strip().startswith("## Prerequisites"):
            start = i
            break
    if start is None:
        return []
    out: list[str] = []
    in_fence = False
    for ln in lines[start + 1:]:
        s = ln.strip()
        if s.startswith("## "):
            break
        if s.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            if s and not s.startswith("#"):
                out.append(s)
            continue
        if s.startswith(("-", "*")) and _INSTALL_VERB_RE.search(s):
            for part in re.findall(r"`([^`]+)`", s):
                if _INSTALL_VERB_RE.search(part):
                    out.append(part)
    return out


class CtfSkillToolCatalog(SkillLibrary):
    """ctf-skills 工具与依赖的声明式目录:逐工具 ws.tools 条目 + 分类级查询。

    复用 SkillLibrary 的目录树扫描(categories()/SKILL.md 路径),frontmatter 的
    allowed-tools/compatibility 按分类重读解析。as_tools_list() 可直接
    Engine(tools=...) 注入(纯声明,目录工具不可调用)。
    """

    def __init__(self, root=SKILLS_DIR, manifest=TOOL_MANIFEST):
        super().__init__(root)
        self._manifest = list(manifest)
        self._meta: dict[str, dict] = {}
        for cat in self.categories():
            meta = self._catalog.get(cat)
            if meta is None:
                continue
            raw = meta.path.read_text(encoding="utf-8")
            fm = _parse_frontmatter(raw)
            allowed = fm.get("allowed-tools", "")
            self._meta[cat] = {
                "allowed_tools": allowed.split() if allowed else [],
                "compatibility": fm.get("compatibility", ""),
                "body": _strip_frontmatter(raw),
            }

    @property
    def manifest(self) -> list[dict]:
        """只读返回 TOOL_MANIFEST 副本。"""
        return list(self._manifest)

    @property
    def installer_path(self) -> Path:
        """vendored 安装脚本路径(整库依赖的下载/更新入口引用,不执行)。"""
        return Path(self.root) / "scripts" / "install_ctf_tools.sh"

    def as_tools_list(self) -> list[dict]:
        """归一为 Engine(tools=...) 可注入的声明列表(OpenAI function-calling 形状)。"""
        return [
            {
                "name": e["tool_id"],
                "description": e["description"],
                "parameters": {"type": "object", "properties": {}},
            }
            for e in self._manifest
        ]

    def get_tool(self, tool_id: str) -> dict | None:
        """按 tool_id 查清单条目(apply_tool 校验 + 取 description);不存在返回 None。"""
        return next((e for e in self._manifest if e["tool_id"] == tool_id), None)

    def allowed_tools(self, category: str) -> list[str]:
        """该分类 frontmatter 的 allowed-tools(agent 原生工具白名单)。"""
        return self._meta.get(category, {}).get("allowed_tools", [])

    def compatibility(self, category: str) -> str:
        """该分类 frontmatter 的 compatibility(运行时要求)。"""
        return self._meta.get(category, {}).get("compatibility", "")

    def verify_checks(self) -> list[str]:
        """全清单校验项(CLI 名 + import 模块),去空,供环境校验。"""
        return [e["verify_check"] for e in self._manifest if e.get("verify_check")]

    def install_commands(self, category: str) -> list[str]:
        """该分类 SKILL.md ## Prerequisites 段的安装命令行(含不在脚本里的额外依赖,
        如 torch/ysoserial)。"""
        return _install_lines(self._meta.get(category, {}).get("body", ""))
