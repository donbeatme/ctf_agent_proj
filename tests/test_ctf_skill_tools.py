"""ctf-skills 工具/依赖声明包装:CtfSkillToolCatalog / TOOL_MANIFEST / 注入路径。"""

import re

import pytest

from agent.ctf_skill_tools import INSTALL_METHODS, TOOL_MANIFEST, CtfSkillToolCatalog
from agent.schema import ID_PATTERN
from agent.skills import SKILLS_DIR
from agent.tools import ToolRegistry
from agent.workspace import MockWorkspace

TOOL_SKILL_FMT = """---
name: ctf-misc
description: misc desc
license: MIT
compatibility: Requires bash and Python 3.
allowed-tools: Bash Read Write Edit Skill
---

# CTF Misc

## Prerequisites

**Python:**
```bash
pip install z3-solver pwntools
```

**Linux (apt):**
```bash
apt install ffmpeg qrencode
```

**Manual:**
- SageMath — Linux: `apt install sagemath`

## Additional Resources

- [sub.md](sub.md) - sub doc
"""


def _make_catalog(root):
    """构造带 frontmatter + Prerequisites 的分类目录树。"""
    misc = root / "ctf-misc"
    misc.mkdir(parents=True)
    (misc / "SKILL.md").write_text(TOOL_SKILL_FMT, encoding="utf-8")
    (misc / "sub.md").write_text("# Sub Doc\n\n内容\n", encoding="utf-8")
    return CtfSkillToolCatalog(root)


# ===== manifest schema / 与脚本一致性 =====


def test_manifest_schema():
    ids = []
    for e in TOOL_MANIFEST:
        for k in ("tool_id", "name", "install_method", "install_command",
                  "verify_check", "description"):
            assert k in e, f"缺键 {k}: {e.get('tool_id')}"
        assert e["install_method"] in INSTALL_METHODS, e["tool_id"]
        assert re.fullmatch(ID_PATTERN, e["tool_id"]), \
            f"tool_id 不合 ID_PATTERN: {e['tool_id']}"
        assert len(e["tool_id"]) <= 32, f"tool_id 过长: {e['tool_id']}"
        assert e["tool_id"] not in ids, f"tool_id 重复: {e['tool_id']}"
        ids.append(e["tool_id"])


def test_manifest_drift_script():
    script = (SKILLS_DIR / "scripts" / "install_ctf_tools.sh").read_text(encoding="utf-8")
    # 每安装方式取哨兵串:清单与脚本保持同步(漂移即失败)
    assert "pwntools==4.15.0:pwn" in script
    assert "Pillow==11.3.0:PIL" in script
    assert "steghide" in script
    assert "qemu-system-x86" in script
    assert "ghidra" in script
    assert "one_gadget" in script
    assert "ffuf" in script
    assert "RsaCtfTool" in script


def test_pip_count_matches_script():
    script = (SKILLS_DIR / "scripts" / "install_ctf_tools.sh").read_text(encoding="utf-8")
    block = re.search(r"PIP_PACKAGES=\((.*?)\n\)", script, re.S)
    assert block, "PIP_PACKAGES 块未找到"
    script_pip = [s for s in re.findall(r'"([^"]+)"', block.group(1)) if "==" in s]
    manifest_pip = [e for e in TOOL_MANIFEST if e["install_method"] == "pip"]
    assert len(script_pip) == len(manifest_pip)
    assert len(script_pip) == 32


# ===== 归一化 / 注入路径 =====


def test_as_tools_list_normalizes():
    cat = CtfSkillToolCatalog()
    ws = MockWorkspace()
    ws.set_tools(cat.as_tools_list())
    assert set(ws.tools) == {e["tool_id"] for e in TOOL_MANIFEST}
    assert len(ws.tools) == len(TOOL_MANIFEST)


def test_openai_tool_specs_includes_catalog():
    ws = MockWorkspace()
    ws.set_tools(CtfSkillToolCatalog().as_tools_list())
    reg = ToolRegistry()
    reg.set_workspace(ws)
    specs = reg.openai_tool_specs()
    names = [s["function"]["name"] for s in specs]
    assert "get_doc" in names                       # 注册表内置
    assert "pwntools" in names and "ghidra" in names  # 目录工具合成
    assert all(s["type"] == "function" for s in specs)


# ===== frontmatter / Prerequisites 解析 =====


def test_frontmatter_allowed_tools(tmp_path):
    cat = _make_catalog(tmp_path)
    assert cat.allowed_tools("ctf-misc") == ["Bash", "Read", "Write", "Edit", "Skill"]
    assert cat.compatibility("ctf-misc") == "Requires bash and Python 3."


def test_install_commands_parses_prerequisites(tmp_path):
    cat = _make_catalog(tmp_path)
    cmds = cat.install_commands("ctf-misc")
    assert "pip install z3-solver pwntools" in cmds
    assert "apt install ffmpeg qrencode" in cmds
    assert "apt install sagemath" in cmds           # 列表行内代码也提取


def test_unknown_category_returns_empty():
    cat = CtfSkillToolCatalog()
    assert cat.allowed_tools("no.such") == []
    assert cat.compatibility("no.such") == ""
    assert cat.install_commands("no.such") == []


# ===== 真实库冒烟(vendored skills/ctf-skills) =====


def test_installer_path_exists():
    cat = CtfSkillToolCatalog()
    assert cat.installer_path == SKILLS_DIR / "scripts" / "install_ctf_tools.sh"
    assert cat.installer_path.exists()


def test_query_apis_real_library():
    cat = CtfSkillToolCatalog()
    assert "Skill" in cat.allowed_tools("ctf-misc")        # 该分类额外允许 Skill 工具
    assert cat.compatibility("ctf-reverse").startswith(
        "Requires filesystem-based agent")
    checks = cat.verify_checks()
    assert "import pwn" in checks                          # pip import 别名
    assert "r2" in checks                                  # CLI 名(radare2)
    assert "gdb" in " ".join(cat.install_commands("ctf-reverse"))


def test_real_library_smoke():
    if not SKILLS_DIR.exists():
        pytest.skip("vendored skills/ctf-skills 不存在")
    cat = CtfSkillToolCatalog()
    assert len(cat.categories()) >= 11
    for c in cat.categories():
        assert cat.allowed_tools(c), f"{c} 缺 allowed-tools"
        assert cat.compatibility(c), f"{c} 缺 compatibility"
    ids = [s["name"] for s in cat.as_tools_list()]
    assert len(ids) == len(TOOL_MANIFEST)
    for tid in ids:
        assert re.fullmatch(ID_PATTERN, tid)


# ===== 动态申请:get_tool / apply_tool / remove_tool =====


def test_get_tool():
    cat = CtfSkillToolCatalog()
    meta = cat.get_tool("sqlmap")
    assert meta is not None and meta["tool_id"] == "sqlmap"
    assert "description" in meta
    assert cat.get_tool("no.such") is None


def _apply_registry(ws):
    """带 workspace 的 ToolRegistry(apply_tool/remove_tool 读 ws.tool_catalog)。"""
    reg = ToolRegistry()
    reg.set_workspace(ws)
    return reg


def test_apply_tool_adds_to_ws():
    ws = MockWorkspace()
    ws.tool_catalog = CtfSkillToolCatalog()
    res = _apply_registry(ws).call_tool("apply_tool", {"tool_ids": ["sqlmap", "ghidra", "no.such"]})
    assert "sqlmap" in ws.tools and "ghidra" in ws.tools
    assert res["added"] == ["sqlmap", "ghidra"]
    assert res["unknown"] == ["no.such"]
    assert ws.tools["sqlmap"]["description"]  # 从目录取到 description


def test_apply_tool_no_catalog_error():
    ws = MockWorkspace()  # 未设 tool_catalog
    res = _apply_registry(ws).call_tool("apply_tool", {"tool_ids": ["sqlmap"]})
    assert "error" in res


def test_remove_tool_removes_from_ws():
    ws = MockWorkspace()
    ws.tool_catalog = CtfSkillToolCatalog()
    reg = _apply_registry(ws)
    reg.call_tool("apply_tool", {"tool_ids": ["sqlmap", "ghidra"]})
    res = reg.call_tool("remove_tool", {"tool_ids": ["sqlmap", "no.such"]})
    assert "sqlmap" not in ws.tools and "ghidra" in ws.tools
    assert res["removed"] == ["sqlmap"]
    assert res["missing"] == ["no.such"]
    # 幂等:再删同一 id → missing
    res2 = reg.call_tool("remove_tool", {"tool_ids": ["sqlmap"]})
    assert res2["removed"] == [] and res2["missing"] == ["sqlmap"]


def test_add_tools_merges():
    ws = MockWorkspace()
    ws.set_tools([{"name": "nmap", "description": "port scan",
                   "parameters": {"type": "object", "properties": {}}}])
    ws.add_tools([{"name": "sqlmap", "description": "sqli",
                   "parameters": {"type": "object", "properties": {}}}])
    assert set(ws.tools) == {"nmap", "sqlmap"}
    # 并入不覆盖已有
    ws.add_tools([{"name": "nmap", "description": "new desc",
                   "parameters": {"type": "object", "properties": {}}}])
    assert ws.tools["nmap"]["description"] == "port scan"
    removed = ws.remove_tools(["sqlmap", "no.such"])
    assert removed == ["sqlmap"]
    assert "sqlmap" not in ws.tools and "nmap" in ws.tools


# ===== 工具目录组件(全量菜单,step 绑定 skill 才渲染) =====


def test_tool_directory_render_full():
    from agent.blueprint import Blueprint, Step
    from agent.ctx import ToolDirectoryComponent
    from agent.schema import Role

    def _make_ws(skill_id=None):
        ws = MockWorkspace()
        bp = Blueprint()
        bp.add_step(Step(id="s1", instruction="x", criterion="y", skill_id=skill_id))
        ws.set_blueprint(bp)
        ws.tool_catalog = CtfSkillToolCatalog()
        return ws

    # 绑定 skill_id:全量渲染(含其它分类工具,本轮不过滤)
    ws = _make_ws("ctf-web")
    comp = ToolDirectoryComponent()
    comp.create(ws, step_id="s1")
    text = comp.render()
    assert "# 工具目录" in text
    assert "- sqlmap:" in text          # web 工具
    assert "- ghidra:" in text          # 也含 reverse 工具(全量)
    # ref 档:仅 id
    comp.level = 1
    ref = comp.render()
    assert "`sqlmap`" in ref and "- sqlmap:" not in ref

    # 无 skill_id:照样全量渲染(不按 skill 绑定门槛)
    ws2 = _make_ws(None)
    comp2 = ToolDirectoryComponent()
    comp2.create(ws2, step_id="s1")
    assert "# 工具目录" in comp2.render()

    # 无 catalog → 不渲染
    ws3 = _make_ws("ctf-web")
    ws3.tool_catalog = None
    comp3 = ToolDirectoryComponent()
    comp3.create(ws3, step_id="s1")
    assert comp3.render() == ""

    # planner 上下文也接收目录(全量)
    ctx, _, _ = ws.assembler.assemble(Role.PLANNER)
    assert "# 工具目录" in ctx and "- sqlmap:" in ctx


def test_executor_accepts_tool_exec():
    from agent.blueprint import Step
    from agent.executor import MockExecutor

    step = Step(id="s1", instruction="x", criterion="y")
    ex = MockExecutor(observation="ok")
    res = ex.run(step, "ctx", tool_exec=lambda name, args: {"ok": True})
    assert res.observation == "ok"
