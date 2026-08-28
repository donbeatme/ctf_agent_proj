"""工具依赖管理:ToolManager(探测 / 安装(OS 适配,持久) / 冲突与不兼容)。

依赖声明复用 agent.ctf_skill_tools.CtfSkillToolCatalog(TOOL_MANIFEST ~70 工具);
目录外的工具(wine 等)走按需动态解析安装,agent 只申请、沙箱适配器更新容器。
- probe_tool:在沙箱内跑 verify_check(import X → python3 -c;CLI 名 → command -v);
  非目录工具按名字探测 command -v(无后端/名称不安全 → unknown)
- install_commands:按 Debian 容器做 OS 适配(pip → --break-system-packages;apt → 非交互;
  gem/go → 先装运行时;download → 先装 curl/unzip 再跑清单命令;git → 先装构建依赖再跑命令)
- install_tools:探测缺失 → 适配命令 → 沙箱内安装(持久进会话容器)→ 重校验;
  非目录工具动态解析(apt-cache/pip index)生成命令,解析不到/装不上 → failed 收口不阻塞
- tool_conflicts:纯元数据分析(同 verify_check / brew-only / 已知约束 / 功能冗余)

全 async:所有触达后端 exec 的方法均为 async(为 Phase 3 actor 每 ex 独立容器铺路)。
"""

from __future__ import annotations

import re
import shlex

from opslog import emit

from agent.ctf_skill_tools import CtfSkillToolCatalog

_PIP_RE = re.compile(r"^(?:python3\s+-m\s+)?pip\s+install\s+(.+)$", re.IGNORECASE)
_APT_RE = re.compile(r"^apt(?:-get)?\s+install\s+(?:-y\s+)?(.+)$", re.IGNORECASE)
_GEM_RE = re.compile(r"^gem\s+install\s+(.+)$", re.IGNORECASE)
_GO_RE = re.compile(r"^go\s+install\s+(.+)$", re.IGNORECASE)

# 非目录工具按需安装:名称必须先过安全校验(apt/pip 包名合法字符),防止目录外名称
# 拼进 shell 命令造成注入(command -v / apt-cache / pip install 都是 shell 拼接)。
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}$")

# 已知约束(Debian 沙箱容器)
_KNOWN_CONSTRAINTS = {
    "uncompyle6": "需 Python ≤ 3.8,Debian 容器 3.11+ 不兼容",
    "hashcat": "需 GPU/OpenCL,容器内无 GPU",
}
# 功能冗余(仅一项即够)
_REDUNDANT_PAIRS = [("ropgadget", "ropper")]


class ToolManager:
    def __init__(self, backend=None, catalog=None):
        self.backend = backend  # SandboxBackend:probe/install 命令经它跑进沙箱
        self.catalog = catalog or CtfSkillToolCatalog()
        # 非目录工具动态安装结果缓存 {(tool_id, session_key): "installed"|"unavailable"}
        # probe 对非目录工具可能恒 unknown/missing(名字探测),缓存避免每次 exec 都重试解析
        self._dynamic_state: dict[tuple[str, str | None], str] = {}

    # ===== 探测 =====

    async def probe_tool(self, tool_id: str, session_key: str | None = None) -> dict:
        """沙箱内校验工具可用性。状态:available|missing|incompatible|manual|unknown。"""
        entry = self.catalog.get_tool(tool_id)
        if entry is None:
            return await self._probe_noncatalog(tool_id, session_key)
        check = entry.get("verify_check") or ""
        method = entry.get("install_method")
        if method == "manual":
            return self._probe_result(tool_id, "manual", check)
        if method == "brew":
            return self._probe_result(tool_id, "incompatible", check)
        cmd = self._probe_cmd(check)
        try:
            out = await self.backend.exec(cmd, session_key=session_key, timeout=30)
        except Exception:
            return self._probe_result(tool_id, "unknown", check)
        status = "available" if out.returncode == 0 else "missing"
        return self._probe_result(tool_id, status, check)

    def _probe_result(self, tool_id: str, status: str, check: str | None) -> dict:
        emit("sandbox", "probe", tool_id=tool_id, status=status)
        return {"tool_id": tool_id, "status": status, "check": check}

    async def _probe_noncatalog(self, tool_id: str, session_key: str | None = None) -> dict:
        """非目录工具:尽力按名字探测(command -v)。无后端/名称不安全→unknown(不拼 shell)。"""
        if self.backend is None or not _SAFE_NAME_RE.match(tool_id):
            return self._probe_result(tool_id, "unknown", None)
        check = f"command -v {shlex.quote(tool_id)}"
        try:
            out = await self.backend.exec(check, session_key=session_key, timeout=30)
        except Exception:
            return self._probe_result(tool_id, "unknown", check)
        status = "available" if out.returncode == 0 else "missing"
        return self._probe_result(tool_id, status, check)

    @staticmethod
    def _probe_cmd(check: str) -> str:
        if check.startswith("import "):
            return f"python3 -c {shlex.quote(check)}"
        return f"command -v {shlex.quote(check)}"

    # ===== 安装(OS 适配,持久进会话容器) =====

    def install_commands(self, tool_ids, os="debian") -> dict[str, str]:
        """逐工具 OS 适配安装命令(仅可自动安装的 pip/apt/gem/go/download/git;brew/manual 无命令)。"""
        out: dict[str, str] = {}
        for tid in tool_ids:
            entry = self.catalog.get_tool(tid)
            if entry is None:
                continue
            adapted = self._adapt(entry.get("install_method"), entry.get("install_command") or "")
            if adapted:
                out[tid] = adapted
        return out

    @staticmethod
    def _adapt(method: str, cmd: str) -> str | None:
        if method == "pip":
            m = _PIP_RE.match(cmd)
            return f"python3 -m pip install --break-system-packages {m.group(1).strip()}" if m else None
        if method == "apt":
            m = _APT_RE.match(cmd)
            return f"DEBIAN_FRONTEND=noninteractive apt-get install -y {m.group(1).strip()}" if m else None
        if method == "gem":
            m = _GEM_RE.match(cmd)
            return f"DEBIAN_FRONTEND=noninteractive apt-get install -y ruby && {cmd}" if m else None
        if method == "go":
            m = _GO_RE.match(cmd)
            return (
                f"DEBIAN_FRONTEND=noninteractive apt-get install -y golang "
                f"&& export PATH=$PATH:$(go env GOPATH)/bin && {cmd}"
            ) if m else None
        if method == "download":
            # 官方 zip 直装:前置 curl/unzip,再跑清单里 download 命令。
            # probe_tool 对 download 不做 brew/manual 短路,落到 command -v 探测。
            return (
                f"DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends curl unzip "
                f"&& {cmd}"
            ) if cmd else None
        if method == "git":
            # 源码构建(原 manual 类,命令补齐后可自动装):前置 git/cmake 等构建依赖,
            # 再跑清单里完整的 clone+build+落地 PATH 命令。命令含 apt-get → install_tools
            # 会先 update 一次(镜像已清 apt lists)。
            return (
                f"DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "
                f"git cmake build-essential && {cmd}"
            ) if cmd else None
        return None  # brew/manual/未知 → 不可自动安装

    async def _resolve_dynamic(self, tool_id: str, session_key: str | None = None) -> tuple[str, str] | None:
        """非目录工具动态解析安装方式:apt-cache show → pip index versions(存在性检查,不下载)。

        返回 (安装命令, 方法);两类都查不到/名称不安全/无后端 → None(不可自动安装)。
        仅在安装失败后由 install_tools 写缓存,避免每次 exec 重复解析。
        """
        if self.backend is None or not _SAFE_NAME_RE.match(tool_id):
            return None
        if await self._apt_has(tool_id, session_key):
            return (f"DEBIAN_FRONTEND=noninteractive apt-get install -y {tool_id}", "apt")
        try:
            out = await self.backend.exec(
                f"python3 -m pip index versions {shlex.quote(tool_id)}",
                session_key=session_key, timeout=120,
            )
        except Exception:
            out = None
        if out is not None and out.returncode == 0 and out.stdout.strip():
            return (f"python3 -m pip install --break-system-packages {tool_id}", "pip")
        return None

    async def _apt_has(self, pkg: str, session_key: str | None = None) -> bool:
        """apt 源里是否存在该包。镜像构建时清掉了 /var/lib/apt/lists,新容器 apt-cache
        查不到 → 先 apt-get update 一次(容器内持久)再重试,否则直接判不存在。"""
        if await self._apt_show(pkg, session_key):
            return True
        if not await self._apt_lists_present(session_key):
            try:
                await self.backend.exec("apt-get update", session_key=session_key, timeout=240)
            except Exception:
                pass
            return await self._apt_show(pkg, session_key)
        return False

    async def _apt_show(self, pkg: str, session_key: str | None = None) -> bool:
        try:
            out = await self.backend.exec(
                f"apt-cache show {shlex.quote(pkg)}", session_key=session_key, timeout=60
            )
        except Exception:
            return False
        return out.returncode == 0 and bool(out.stdout.strip())

    async def _apt_lists_present(self, session_key: str | None = None) -> bool:
        try:
            out = await self.backend.exec(
                "ls -A /var/lib/apt/lists 2>/dev/null | wc -l", session_key=session_key, timeout=30
            )
        except Exception:
            return False
        n = out.stdout.strip()
        return out.returncode == 0 and bool(n) and n != b"0"

    async def install_tools(self, tool_ids, *, session_key: str | None = None,
                            force: bool = False) -> dict:
        """探测缺失 → 安装 → 重校验。报告 {installed, failed, skipped_manual, incompatible}。

        目录外工具走动态解析(apt-cache/pip index),解析不到或装不上 → failed 收口,
        不抛异常、不阻塞调用方(命令仍如实跑并报自己的错)。
        """
        report = {"installed": [], "failed": [], "skipped_manual": [], "incompatible": []}
        cmds = self.install_commands(tool_ids)
        to_run = {tid: cmd for tid, cmd in cmds.items()}
        dynamic: dict[str, str] = {}  # 非目录工具:{tid: 解析出的安装方法}
        for tid in tool_ids:
            if tid in to_run:
                continue
            entry = self.catalog.get_tool(tid)
            if entry is not None:
                if entry.get("install_method") == "manual":
                    report["skipped_manual"].append(tid)
                else:
                    report["incompatible"].append(tid)  # brew 等无适配命令
                continue
            state = self._dynamic_state.get((tid, session_key))
            if not force and state == "installed":
                continue  # 已动态装好(非目录 probe 恒 miss,靠缓存跳过,不重装)
            if not force:
                st = (await self.probe_tool(tid, session_key=session_key)).get("status")
                if st == "available":
                    continue  # 名字已在容器内,无需安装
                if state == "unavailable":
                    report["failed"].append(tid)  # 缓存:之前解析/安装失败,快速收口
                    continue
            resolved = await self._resolve_dynamic(tid, session_key)
            if resolved is None:
                self._dynamic_state[(tid, session_key)] = "unavailable"
                report["failed"].append(tid)  # 解析不到 → 装不上,失败收口
            else:
                to_run[tid], dynamic[tid] = resolved
        if not force:
            for tid in list(to_run):
                if (await self.probe_tool(tid, session_key=session_key)).get("status") == "available":
                    to_run.pop(tid)  # 已可用,跳过
                    dynamic.pop(tid, None)
        if to_run and any("apt-get" in c for c in to_run.values()):
            await self.backend.exec("apt-get update", session_key=session_key, timeout=180)
        for tid, cmd in to_run.items():
            out = await self.backend.exec(cmd, session_key=session_key, timeout=600)
            if tid in dynamic:
                # 非目录工具:安装命令成功即算装上(二进制名可能与包名不同,不作名字探测)
                ok = out.returncode == 0
            else:
                ok = out.returncode == 0 and (
                    (await self.probe_tool(tid, session_key=session_key)).get("status") == "available"
                )
            if ok:
                report["installed"].append(tid)
                if tid in dynamic:
                    self._dynamic_state[(tid, session_key)] = "installed"
            else:
                report["failed"].append(tid)
                if tid in dynamic:
                    self._dynamic_state[(tid, session_key)] = "unavailable"
        for tid in report["installed"]:
            emit("sandbox", "install", tool_id=tid, result="installed",
                 source=("dynamic_" + dynamic[tid]) if tid in dynamic else "catalog")
        for tid in report["failed"]:
            emit("sandbox", "install", tool_id=tid, result="failed",
                 source=("dynamic_" + dynamic[tid]) if tid in dynamic else "catalog")
        for tid in report["skipped_manual"]:
            emit("sandbox", "install", tool_id=tid, result="skipped_manual", source="catalog")
        for tid in report["incompatible"]:
            emit("sandbox", "install", tool_id=tid, result="incompatible", source="catalog")
        return report

    # ===== 冲突与不兼容(纯元数据分析) =====

    def tool_conflicts(self) -> list[dict]:
        """形状 {a, b, reason, severity: conflict|incompatible|warning}。"""
        out: list[dict] = []
        groups: dict[str, list] = {}
        for e in self.catalog.manifest:
            ck = e.get("verify_check")
            if ck:
                groups.setdefault(ck, []).append(e)
        for ck, es in groups.items():
            if len(es) > 1:
                for i in range(len(es)):
                    for j in range(i + 1, len(es)):
                        out.append({
                            "a": es[i]["tool_id"], "b": es[j]["tool_id"],
                            "reason": f"verify_check 相同: {ck}", "severity": "conflict",
                        })
        for e in self.catalog.manifest:
            if e.get("install_method") == "brew" and not e.get("alt_methods"):
                out.append({
                    "a": e["tool_id"], "b": None,
                    "reason": "主安装方式 brew,Debian 沙箱无 Homebrew,且无备选安装方式",
                    "severity": "incompatible",
                })
        for tid, reason in _KNOWN_CONSTRAINTS.items():
            if self.catalog.get_tool(tid):
                out.append({"a": tid, "b": None, "reason": reason, "severity": "warning"})
        for a, b in _REDUNDANT_PAIRS:
            if self.catalog.get_tool(a) and self.catalog.get_tool(b):
                out.append({"a": a, "b": b, "reason": "功能重叠(ROP gadget 搜索二选一)", "severity": "warning"})
        emit("sandbox", "conflicts", count=len(out),
             severity=",".join(sorted({c["severity"] for c in out})) or "none")
        return out
