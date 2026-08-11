"""环境探测器:只读检查 ctf-skills 工具/沙箱/分类就绪度(不装依赖、不执行任务)。

③ 边界:只做只读环境探测(CLI 用 shutil.which,模块用 importlib.util.find_spec——
不真正 import,快且安全),把"缺工具/缺沙箱/分类配不了"写进 run.log 供审计;
真正安装依赖、建沙箱、执行任务仍是② 职责。

触发时机(engine 接线):apply_tool 逐工具探测 + 每步执行前按 skill 分类查沙箱/
兼容/安装命令 + run 起始全量清单快照。结果 shape 见 design/contracts.md §1.7。
"""

import importlib.util
import shutil

from agent.ctf_skill_tools import CtfSkillToolCatalog

# 需隔离的分类(运行沙箱/容器才安全);可按需覆盖
SANDBOX_CATEGORIES = {"ctf-pwn", "ctf-reverse", "ctf-malware"}


def default_sandbox_probe(category) -> bool:
    """容器运行时在不在:docker 或 podman CLI 任一可用即视为有(与具体分类无关)。"""
    return shutil.which("docker") is not None or shutil.which("podman") is not None


class SkillEnvProbe:
    """只读环境探测器:逐工具 / 逐分类 / 全量清单的就绪度判定。

    复用 CtfSkillToolCatalog 的 get_tool/compatibility/allowed_tools/install_commands/
    manifest;不 import tools.py(无环)。探测异常一律归 unknown,不崩调用方。
    """

    def __init__(self, catalog=None, sandbox_probe=None, sandbox_categories=None):
        self.catalog = catalog or CtfSkillToolCatalog()
        self._sandbox_probe = sandbox_probe or default_sandbox_probe
        self._sandbox_categories = set(sandbox_categories or SANDBOX_CATEGORIES)

    # ===== 工具级 =====

    def probe_tool(self, tool_id: str) -> dict:
        """单个工具可用性:status ∈ available|missing|manual|unknown + 校验项。

        verify_check 空(manual)→ manual;`import X` → find_spec;否则当 CLI 名 which。
        """
        if self.catalog is None:
            return {"tool_id": tool_id, "status": "unknown", "check": ""}
        meta = self.catalog.get_tool(tool_id)
        if meta is None:
            return {"tool_id": tool_id, "status": "unknown", "check": ""}
        check = meta.get("verify_check") or ""
        if not check:
            return {"tool_id": tool_id, "status": "manual", "check": ""}
        try:
            ok = self._check(check)
        except Exception:
            return {"tool_id": tool_id, "status": "unknown", "check": check}
        return {"tool_id": tool_id, "status": "available" if ok else "missing",
                "check": check}

    def probe_tools(self, tool_ids) -> list[dict]:
        return [self.probe_tool(tid) for tid in tool_ids]

    def _check(self, check: str) -> bool:
        """解析 verify_check 并探测:import 前缀 → find_spec;否则 CLI 名 → which。"""
        if check.startswith("import "):
            mod = check[len("import "):].strip()
            return importlib.util.find_spec(mod) is not None
        return shutil.which(check) is not None

    # ===== 沙箱级 =====

    def probe_sandbox(self, category: str) -> dict:
        """该分类是否*需要*沙箱 + 容器运行时在不在;不需要时 available=None。"""
        needed = category in self._sandbox_categories
        return {
            "category": category,
            "needed": needed,
            "available": self._sandbox_probe(category) if needed else None,
        }

    # ===== 分类级 =====

    def probe_category(self, category: str) -> dict:
        """分类就绪度:exists + compatibility + allowed_tools + install_cmds(前3) + sandbox。"""
        compat = self.catalog.compatibility(category)
        allowed = self.catalog.allowed_tools(category)
        install = self.catalog.install_commands(category)
        return {
            "category": category,
            "exists": bool(compat or allowed or install),
            "compatibility": compat,
            "allowed_tools": allowed,
            "install_cmds": install[:3],
            "sandbox": self.probe_sandbox(category),
        }

    # ===== 全量清单快照 =====

    def probe_manifest(self) -> dict:
        """遍历整清单:总数/可用/缺失/manual + 缺失明细 + 沙箱运行时。

        sandbox 取任一需隔离分类作代表(从 sandbox_categories 挑,保证 needed=True
        才会真正探测运行时;空集合回退 ctf-pwn 探测容器运行时在不在)。
        """
        counts = {"total": 0, "available": 0, "missing": 0, "manual": 0, "unknown": 0}
        missing_list: list[str] = []
        for e in self.catalog.manifest:
            counts["total"] += 1
            p = self.probe_tool(e["tool_id"])
            status = p["status"]
            if status == "available":
                counts["available"] += 1
            elif status == "missing":
                counts["missing"] += 1
                missing_list.append(f"{p['tool_id']}({p['check']})")
            elif status == "manual":
                counts["manual"] += 1
            else:
                counts["unknown"] += 1
        rep_cat = next(iter(sorted(self._sandbox_categories)), "ctf-pwn")
        return {
            **counts,
            "missing_list": missing_list,
            "sandbox": self.probe_sandbox(rep_cat),
        }
