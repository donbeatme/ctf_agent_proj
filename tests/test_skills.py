"""ctf-skills 技能库加载器:SkillLibrary / CtfSkillsDocStore / planner 接线。"""

import re

import pytest

from agent.planner import MockPlannerLLM, Planner
from agent.schema import ID_PATTERN, Goal, PlannerInput, PlannerMode, TaskInput
from agent.skills import SKILLS_DIR, CtfSkillsDocStore, SkillLibrary, _parse_frontmatter, _rewrite_links
from agent.workspace import MockWorkspace

SKILL_FMT = """---
name: {cat}
description: {desc}
license: MIT
---

# {Title}

## Prerequisites

## Additional Resources

- [sub.md](sub.md) - sub doc one-liner

## Technique

use the skill
"""


def _make_lib(root):
    """构造 2 分类的 ctf-skills 目录树:ctf-misc(base64)+ ctf-crypto(rsa)。"""
    misc = root / "ctf-misc"
    misc.mkdir(parents=True)
    (misc / "SKILL.md").write_text(
        SKILL_FMT.format(cat="ctf-misc", desc="base64 编码/解码、杂项技巧",
                         Title="CTF Misc"), encoding="utf-8")
    (misc / "sub.md").write_text("# Sub Doc\n\n内容\n", encoding="utf-8")
    (misc / "other.md").write_text("# Other Doc\n\n内容\n", encoding="utf-8")

    crypto = root / "ctf-crypto"
    crypto.mkdir(parents=True)
    (crypto / "SKILL.md").write_text(
        SKILL_FMT.format(cat="ctf-crypto", desc="RSA/AES 密码学攻击",
                         Title="CTF Crypto"), encoding="utf-8")
    (crypto / "rsa.md").write_text("# RSA Attacks\n\n内容\n", encoding="utf-8")
    return CtfSkillsDocStore(root)


# ===== frontmatter / 链接改写 =====


def test_parse_frontmatter():
    fm = _parse_frontmatter(
        "---\nname: ctf-misc\ndescription: 一句话描述\nlicense: MIT\n"
        "compatibility: Requires bash\n---\n\n正文")
    assert fm["name"] == "ctf-misc"
    assert fm["description"] == "一句话描述"
    assert fm["license"] == "MIT"
    assert _parse_frontmatter("无 frontmatter") == {}


def test_rewrite_links_drops_anchor():
    out = _rewrite_links(
        "看 [pyjails](pyjails.md#锚点) 和 [encodings.md](encodings.md)",
        "ctf-misc")
    assert "[pyjails](ctf-misc.pyjails)" in out
    assert "[encodings](ctf-misc.encodings)" in out
    assert "ctf-misc.encodings" in out


# ===== 目录扫描 =====


def test_catalog_scan(tmp_path):
    lib = _make_lib(tmp_path)
    cats = lib.categories()
    assert "ctf-misc" in cats and "ctf-crypto" in cats
    # SKILL.md → 分类 id;子文档 → 分类.stem
    assert set(lib.catalog) == {
        "ctf-misc", "ctf-crypto", "ctf-misc.sub", "ctf-misc.other", "ctf-crypto.rsa"}
    for doc_id in lib.catalog:
        assert re.fullmatch(ID_PATTERN, doc_id), f"doc_id 不合 ID_PATTERN: {doc_id}"


def test_load_skill_description_first_line(tmp_path):
    lib = _make_lib(tmp_path)
    doc = lib.load_doc("ctf-misc")
    assert doc.splitlines()[0] == "base64 编码/解码、杂项技巧"   # 首行 = 描述(供 Docs 组件渲染)
    assert "[sub](ctf-misc.sub)" in doc                          # 相对链接已改写
    assert "---" not in doc.splitlines()[0]                      # frontmatter 已剥


def test_load_tech_heading_and_links(tmp_path):
    lib = _make_lib(tmp_path)
    doc = lib.load_doc("ctf-crypto.rsa")
    assert "RSA Attacks" in doc.splitlines()[0]      # 原文保留,首行是 # 标题
    assert lib.catalog["ctf-crypto.rsa"].description == "RSA Attacks"
    assert lib.load_doc("no.such") is None


# ===== 检索路由 =====


def test_search_routing(tmp_path):
    lib = _make_lib(tmp_path)
    assert [i for i, _ in lib.search(
        {"title": "base64", "description": "base64 编码后作为 flag 提交"})] == ["ctf-misc"]
    assert [i for i, _ in lib.search({"description": "RSA 解密 n e c 拿 flag"})] == ["ctf-crypto"]
    assert lib.search({"description": "毫无特征的一句话"}) == []


# ===== planner 接线 =====


def _planner_with(lib):
    planner = Planner(workspace=MockWorkspace(), docs=lib)
    planner.llm_call = MockPlannerLLM(
        '{"add":[{"id":"s1","instruction":"做","criterion":"过",'
        '"skill_id":"ctf-misc","depends_on":[]}],"reason":"r"}')
    return planner


def test_plan_seeds_real_doc_ids(tmp_path):
    planner = _planner_with(_make_lib(tmp_path))
    planner.plan(PlannerInput(
        mode=PlannerMode.INITIAL,
        task_input=TaskInput(
            raw_content={"title": "base64", "description": "base64 编码题"},
            goal_list=[Goal(id="g1")])))
    assert "ctf-misc" in planner.workspace.docs          # 真实分类 id
    assert "doc0" not in planner.workspace.docs          # 不再丢 id


def test_get_doc_fallback_to_sub_doc(tmp_path):
    planner = _planner_with(_make_lib(tmp_path))
    planner.plan(PlannerInput(
        mode=PlannerMode.INITIAL,
        task_input=TaskInput(
            raw_content={"title": "base64", "description": "base64 编码题"},
            goal_list=[Goal(id="g1")])))
    assert "ctf-misc.sub" not in planner.workspace.docs      # 子文档正文未预灌
    res = planner._lookup("get_doc", {"doc_id": "ctf-misc.sub"})
    assert res.get("doc_id") == "ctf-misc.sub"
    assert "Sub Doc" in res.get("content", "")               # 兜底取到全文
    assert "ctf-misc.sub" in planner.workspace.docs          # 取到即入注册表
    err = planner._lookup("get_doc", {"doc_id": "no.such"})
    assert "error" in err


# ===== 真实库冒烟(vendored skills/ctf-skills) =====


def test_real_library():
    if not SKILLS_DIR.exists():
        pytest.skip("vendored skills/ctf-skills 不存在")
    lib = SkillLibrary()
    assert len(lib.categories()) >= 9
    for doc_id in lib.catalog:
        assert re.fullmatch(ID_PATTERN, doc_id)
    hits = [i for i, _ in CtfSkillsDocStore().search(
        {"title": "base64 编码", "description": "base64 编码后作为 flag 提交"})]
    assert "ctf-misc" in hits
