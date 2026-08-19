"""ExperienceComponent 渲染 + ws.experience 持久化 + 组件装配范围(env 门控)。"""

import json

from agent.ctx import ExperienceComponent
from agent.schema import Role
from agent.workspace import MockWorkspace, Workspace

RECORDS = [
    {
        "procedure_id": "p1", "challenge_id": "c1", "friendly_id": "PCHAL-1",
        "method": "procedure", "platform_verified": 1, "last_ok_at": "2026-08-18T21:42:01+0800",
        "verifier_path": "solve_extract.py", "flag": "CTF2{old}",
    },
    {
        "procedure_id": "p2", "challenge_id": "c2", "friendly_id": "PCHAL-2",
        "method": "procedure", "platform_verified": 0, "last_ok_at": None,
        "verifier_path": None,
    },
]


def _rendered(ws, level):
    c = ExperienceComponent().create(ws)
    for _ in range(level):
        c.advance_level()
    return c.render()


def test_raw_render_lines():
    ws = MockWorkspace()
    ws.set_experience(RECORDS)
    text = _rendered(ws, 0)
    assert text.startswith("# 已验证解题经验")
    assert "- PCHAL-1 [procedure] 已验证=是 上次成功=2026-08-18T21:42:01+0800 脚本=solve_extract.py" in text
    assert "- PCHAL-2 [procedure] 已验证=否 上次成功=- 脚本=-" in text
    # 不渲染过期 hint flag(实例相关,不入 ctx)
    assert "CTF2{old}" not in text


TRACE_RECORD = {
    "procedure_id": "p3", "challenge_id": "c3", "friendly_id": "PCHAL-3",
    "method": "procedure", "platform_verified": 1, "last_ok_at": None,
    "verifier_path": "solve_extract.py",
    "trace_json": {
        "oracle": "注入点: id 参数;响应带标记",
        "true_mark": "VALID",
        "false_mark": "INVALID",
        "waf_mark": "WAF blocked",
        "extraction": "union 注入按列数逐步提取",
        "flag_format": "CTF2{...}",
        "verified_flag": "CTF2{instance-specific}",  # 实例 flag,不得入 ctx
    },
}


def test_raw_render_includes_trace_json():
    ws = MockWorkspace()
    ws.set_experience([TRACE_RECORD])
    text = _rendered(ws, 0)
    assert "oracle: 注入点: id 参数;响应带标记" in text
    assert "true_mark: VALID" in text and "false_mark: INVALID" in text
    assert "waf_mark: WAF blocked" in text
    assert "extraction: union 注入按列数逐步提取" in text
    assert "flag_format: CTF2{...}" in text
    # trace_json 里的 verified_flag 是实例相关 flag,不渲染
    assert "CTF2{instance-specific}" not in text


def test_raw_render_trace_json_string():
    ws = MockWorkspace()
    rec = {k: v for k, v in TRACE_RECORD.items() if k != "trace_json"}
    rec["trace_json"] = json.dumps(TRACE_RECORD["trace_json"])
    ws.set_experience([rec])
    text = _rendered(ws, 0)
    assert "oracle: 注入点: id 参数;响应带标记" in text
    assert "extraction: union 注入按列数逐步提取" in text


def test_raw_render_trace_json_missing():
    ws = MockWorkspace()
    ws.set_experience([{k: v for k, v in TRACE_RECORD.items() if k != "trace_json"}])
    text = _rendered(ws, 0)
    assert "- PCHAL-3 [procedure] 已验证=是 上次成功=- 脚本=solve_extract.py" in text


def test_ref_render_index():
    ws = MockWorkspace()
    ws.set_experience(RECORDS)
    text = _rendered(ws, 1)
    assert text.startswith("# 已验证解题经验(索引)")
    assert "`p1`" in text and "`p2`" in text


def test_render_empty():
    assert _rendered(MockWorkspace(), 0) == ""
    assert _rendered(MockWorkspace(), 1) == ""


def test_no_render_without_workspace():
    c = ExperienceComponent().create(None)
    assert c.render() == ""


def test_priority_and_compress_methods():
    c = ExperienceComponent()
    assert c.key == "experience"
    assert c.priority == 4
    assert c.LEVELS == ("raw", "ref")
    assert c.compress_methods  # 可压缩


def test_experience_sync_load_reset(tmp_path):
    ws = Workspace.create("r1", {"challenge_id": "c1"}, root=tmp_path)
    ws.set_experience(RECORDS)
    ws.sync()
    loaded = Workspace.load("r1", root=tmp_path)
    assert loaded.get_experience() == RECORDS
    loaded.reset()
    assert loaded.get_experience() == []
    reloaded = Workspace.load("r1", root=tmp_path)
    assert reloaded.get_experience() == []


def test_mock_experience_roundtrip():
    ws = MockWorkspace()
    ws.set_experience(RECORDS)
    assert ws.get_experience() == RECORDS
    ws.set_experience([])
    assert ws.get_experience() == []
    ws.set_experience(None)
    assert ws.get_experience() == []


def test_experience_scope_default_ee():
    ws = MockWorkspace()
    assert any(type(c) is ExperienceComponent for c in ws.assembler.components(Role.EVALUATOR_STEP))
    assert any(type(c) is ExperienceComponent for c in ws.assembler.components(Role.EVALUATOR_TASK))
    assert not any(type(c) is ExperienceComponent for c in ws.assembler.components(Role.EXECUTOR))


def test_experience_scope_agent(monkeypatch):
    monkeypatch.setenv("CTF_EXPERIENCE_SCOPE", "agent")
    ws = MockWorkspace()
    assert any(type(c) is ExperienceComponent for c in ws.assembler.components(Role.EXECUTOR))
    assert not any(type(c) is ExperienceComponent for c in ws.assembler.components(Role.EVALUATOR_STEP))


def test_experience_scope_all(monkeypatch):
    monkeypatch.setenv("CTF_EXPERIENCE_SCOPE", "all")
    ws = MockWorkspace()
    assert any(type(c) is ExperienceComponent for c in ws.assembler.components(Role.EXECUTOR))
    assert any(type(c) is ExperienceComponent for c in ws.assembler.components(Role.EVALUATOR_STEP))


def test_experience_scope_none(monkeypatch):
    monkeypatch.setenv("CTF_EXPERIENCE_SCOPE", "none")
    ws = MockWorkspace()
    assert not any(type(c) is ExperienceComponent for c in ws.assembler.components(Role.EXECUTOR))
    assert not any(type(c) is ExperienceComponent for c in ws.assembler.components(Role.EVALUATOR_STEP))
    assert not any(type(c) is ExperienceComponent for c in ws.assembler.components(Role.EVALUATOR_TASK))
