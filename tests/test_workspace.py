"""Workspace 本地存储 + 索引化测试(design/workspace.md §3/§4)。"""

import json

import pytest

from agent.blueprint import Blueprint, Step
from agent.workspace import Workspace

RUN_ID = "run-test"


@pytest.fixture
def ws(tmp_path):
    return Workspace.create(RUN_ID, {"name": "t"}, root=tmp_path)


def test_create_initializes_meta_and_state_file(tmp_path):
    ws = Workspace.create("r1", {"q": "x"}, root=tmp_path)
    assert ws.meta["run_id"] == "r1"
    assert ws.meta["task"] == {"q": "x"}
    assert ws.meta["run_status"] == "PLANNING"
    assert (tmp_path / "r1" / "state.json").exists()


def test_add_event_generates_uuid_and_persists(tmp_path, ws):
    ev = ws.add_event("evaluator_step", "verdict", step_id="s1", verdict="pass", attempts=1)
    assert ev.uuid
    assert ev.agent == "evaluator_step"
    assert ev.detail == {"attempts": 1}
    lines = (tmp_path / RUN_ID / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["uuid"] == ev.uuid
    assert data["verdict"] == "pass"


def test_get_record_by_uuid(ws):
    ev = ws.add_event("planner", "replan")
    assert ws.get_record(ev.uuid).kind == "replan"
    assert ws.get_record("not-exist") is None


def test_query_filters(ws):
    ws.add_event("evaluator_step", "verdict", step_id="s1", verdict="pass")
    ws.add_event("evaluator_step", "verdict", step_id="s1", verdict="retry")
    ws.add_event("planner", "replan")
    ws.add_event("system", "state_change")
    assert len(ws.query(agent="evaluator_step")) == 2
    assert len(ws.query(agent="system")) == 1
    assert len(ws.query(step_id="s1")) == 2
    assert len(ws.query(step_id="s1", verdict="retry")) == 1
    assert len(ws.query(kind="replan")) == 1
    assert ws.query(agent="nobody") == []


def test_query_time_range_inclusive(ws):
    e1 = ws.add_event("a", "x")
    e2 = ws.add_event("a", "y")
    got = ws.query(time_range=(e1.ts, e2.ts))
    assert len(got) == 2
    # 同一秒内 ts 相同:上下界闭区间,两事件都在窗口内
    assert ws.query(time_range=(e1.ts, None)) == [e1, e2]
    assert ws.query(time_range=(None, e1.ts)) == [e1, e2]


def test_record_step_writes_steps_and_event(ws):
    sr = ws.record_step("s1", "pass", "完成", result={"flag": "x"}, attempts=1)
    assert ws.steps["s1"] is sr
    assert ws.steps["s1"].verdict == "pass"
    assert ws.steps["s1"].result == {"flag": "x"}
    evs = ws.query(kind="step_record", step_id="s1")
    assert len(evs) == 1
    assert evs[0].verdict == "pass"


def test_env_and_doc_accessors(ws):
    ws.set_env("target_url", "http://x")
    ws.set_doc("doc1", "参考文档内容")
    assert ws.get_env("target_url") == "http://x"
    assert ws.get_env("missing", "dft") == "dft"
    assert ws.get_doc("doc1") == "参考文档内容"
    assert ws.get_doc("nope") is None


def test_tool_accessors(ws):
    ws.set_tools([
        {"type": "function", "function": {"name": "nmap", "description": "端口扫描"}},
        {"type": "function", "function": {"name": "nc", "description": "原始连接"}},
        {"name": "curl", "description": "HTTP 请求"},
    ])
    assert ws.get_tool("nmap") == {"description": "端口扫描", "parameters": {}}
    assert ws.get_tool("nc") == {"description": "原始连接", "parameters": {}}
    assert ws.get_tool("curl") == {"description": "HTTP 请求", "parameters": {}}
    assert ws.get_tool("nope") is None
    assert ws.get_tool_description("nmap") == "端口扫描"
    assert ws.get_tool_description("nope") == ""
    assert ws.tools == {
        "nmap": {"description": "端口扫描", "parameters": {}},
        "nc": {"description": "原始连接", "parameters": {}},
        "curl": {"description": "HTTP 请求", "parameters": {}},
    }


def test_set_tools_rejects_local_mapping(ws):
    with pytest.raises(TypeError):
        ws.set_tools({"nc": "原始连接"})    # 本地映射不在统一接口,抛错防静默混入


def test_set_tools_accepts_openai_function_calling(ws):
    specs = [{"type": "function", "function": {
        "name": "nmap", "description": "端口扫描",
        "parameters": {"type": "object", "properties": {"host": {"type": "string"}}}}},
        {"type": "function", "function": {"name": "curl", "description": "HTTP 请求"}}]
    ws.set_tools(specs)
    assert ws.get_tool("nmap") == {
        "description": "端口扫描",
        "parameters": {"type": "object", "properties": {"host": {"type": "string"}}},
    }
    assert ws.get_tool_description("curl") == "HTTP 请求"


def test_set_tools_accepts_mcp_shape(ws):
    ws.set_tools([{"name": "nc", "description": "原始连接", "inputSchema": {"type": "object"}}])
    assert ws.get_tool("nc") == {"description": "原始连接", "parameters": {"type": "object"}}


def test_set_tools_drops_unidentifiable(ws):
    ws.set_tools([{"type": "function", "function": {"description": "无 name"}}, 42, "str"])
    assert ws.tools == {}


def test_record_tool_result_appends_event(ws):
    ev = ws.record_tool_result("s1", "nmap", "port 22 open", args={"host": "x"})
    assert ev.kind == "tool_result"
    assert ev.step_id == "s1"
    assert ev.detail.tool == "nmap"
    assert ev.detail.args == {"host": "x"}
    assert ev.detail.output == "port 22 open"
    assert len(ws.query(kind="tool_result")) == 1


def test_record_tool_result_default_args(ws):
    ev = ws.record_tool_result("s1", "nc", "banner")
    assert ev.kind == "tool_result"
    assert ev.detail.tool == "nc"
    assert ev.detail.args == {}
    assert ev.detail.output == "banner"


def test_sync_load_roundtrip_all_fields(tmp_path, ws):
    bp = Blueprint(meta={"task": "t"})
    bp.add_step(Step(id="s1", instruction="做", criterion="可验收"))
    bp.add_step(Step(id="s2", instruction="做二", criterion="可验收", depends_on=["s1"]))
    ws.set_blueprint(bp)
    ws.set_env("target_url", "http://x")
    ws.set_doc("doc1", "doc")
    ws.record_step("s1", "pass", "done", attempts=1)
    ws.sync()

    ws2 = Workspace.load(RUN_ID, root=tmp_path)
    assert ws2.meta == ws.meta
    assert ws2.blueprint.to_dict() == bp.to_dict()
    assert ws2.env_state == {"target_url": "http://x"}
    assert ws2.get_doc("doc1") == "doc"
    assert ws2.steps["s1"].observation == "done"
    assert ws2.steps["s1"].verdict == "pass"
    assert ws2.steps["s1"].attempts == 1
    # set_blueprint 是单一写路径:内部发 REPLAN 事件(带 DAG 快照),故 2 条事件
    assert [e.kind for e in ws2.events] == ["replan", "step_record"]
    assert ws2.events[0].detail.dag is not None      # REPLAN 事件携带 DAG 快照


def test_tools_persist_across_sync_load(tmp_path, ws):
    ws.set_tools([{"type": "function", "function": {"name": "nmap", "description": "端口扫描"}}])
    ws.sync()
    ws2 = Workspace.load(RUN_ID, root=tmp_path)
    assert ws2.tools == {"nmap": {"description": "端口扫描", "parameters": {}}}
    assert ws2.get_tool_description("nmap") == "端口扫描"


def test_tool_result_event_persists_across_load(tmp_path, ws):
    ws.record_tool_result("s1", "nmap", "port 22 open", args={"host": "x"})
    ws.sync()
    ws2 = Workspace.load(RUN_ID, root=tmp_path)
    ev = ws2.query(kind="tool_result")[0]
    assert ev.step_id == "s1"
    assert ev.detail.tool == "nmap"
    assert ev.detail.args == {"host": "x"}


def test_load_reconstructs_events_from_jsonl(tmp_path, ws):
    ws.add_event("evaluator_step", "verdict", step_id="s1", verdict="pass")
    ws.add_event("planner", "replan")
    ws.sync()
    ws2 = Workspace.load(RUN_ID, root=tmp_path)
    assert [e.kind for e in ws2.events] == ["verdict", "replan"]
    assert ws2.get_record(ws2.events[0].uuid).step_id == "s1"


def test_summaries_persist_across_sync_load(tmp_path, ws):
    ws.summaries["planner:history"] = {"text": "旧摘要", "upto": 2}
    ws.sync()
    ws2 = Workspace.load(RUN_ID, root=tmp_path)
    assert ws2.summaries == {"planner:history": {"text": "旧摘要", "upto": 2}}


def test_load_missing_run_raises(tmp_path):
    with pytest.raises(KeyError):
        Workspace.load("missing", root=tmp_path)


def test_create_isolates_runs(tmp_path):
    a = Workspace.create("ra", {"t": 1}, root=tmp_path)
    b = Workspace.create("rb", {"t": 2}, root=tmp_path)
    a.add_event("evaluator_step", "verdict")
    assert len(b.events) == 0
    assert b.query() == []
