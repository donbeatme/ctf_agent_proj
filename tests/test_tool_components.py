"""Tool / Trace 组件测试:ws.tools 投影、trace 轨迹轮次边界、agent 作用域、压缩档位、预热缓存。

设计见 design/workspace.md §6.2(priority 阶梯:trace=1 / tools=4)。
- ToolComponent 投影 ws.tools(静态目录):raw 全目录 → ref 仅 id
- TraceComponent 投影**最近一次 replan 之后**的 use_tool + tool_result 事件:
  "轨迹" = 模型知道自己正在干什么的过程记录(ut 调用意图 + tr 世界响应),
  和 output(决策文本)区分;replan 推进轮次边界,上一轮轨迹落回 history 审计。
  agent 参数限定只投影某角色的轨迹(executor/ep 各自的工具调用),None = 全角色
- 压缩:raw → index(uuid 引用,get_record 可展开)→ summary(本轮轨迹摘要);
  摘要按事件集签名缓存,本轮轨迹未变读缓存,跨 replan 变化才重算;
  未注入 compress 停在 index 档(不装假摘要)
"""

import pytest

from agent.ctx import CtxAssembler, ToolComponent, TraceComponent
from agent.tools import ToolRegistry, openai_tool_specs
from agent.workspace import Workspace


@pytest.fixture
def ws(tmp_path):
    return Workspace.create("run-tools", {"q": "x"}, root=tmp_path)


def make_assembler(ws, compress=None):
    a = CtxAssembler(ws, compress=compress)
    a.register("planner", ToolComponent(), TraceComponent())
    return a


def comp(a, key):
    return next(c for c in a.components("planner") if c.key == key)


# ===== ToolComponent:ws.tools 投影 =====

async def test_tool_component_renders_catalog(ws):
    ws.set_tools([
        {"type": "function", "function": {"name": "nmap", "description": "端口扫描"}},
        {"type": "function", "function": {"name": "nc", "description": "原始连接"}},
    ])
    a = make_assembler(ws)
    ctx, _, _ = await a.assemble("planner")
    assert "# 可用工具" in ctx
    assert "- nmap: 端口扫描" in ctx
    assert "- nc: 原始连接" in ctx


def test_tool_component_ref_level_ids_only(ws):
    ws.set_tools([{"type": "function", "function": {"name": "nmap", "description": "端口扫描"}}])
    a = make_assembler(ws)
    t = comp(a, "tools")
    t.create(ws)
    t.advance_level()
    assert t.render() == "# 可用工具(索引)\n`nmap`"


async def test_tool_component_empty_catalog_noop(ws):
    a = make_assembler(ws)
    ctx, _, _ = await a.assemble("planner")
    assert ctx == ""          # 无工具目录不渲染


def test_tool_component_normalize_interface():
    """组件类提供与本地协议解耦的归一入口:标准格式 → 统一内部形式。"""
    out = ToolComponent.normalize([
        {"type": "function", "function": {"name": "nmap", "description": "扫描",
                                          "parameters": {"type": "object"}}},
        {"name": "nc", "description": "连接", "inputSchema": {"type": "object"}},
    ])
    assert set(out) == {"nmap", "nc"}
    assert out["nmap"]["description"] == "扫描"
    assert out["nmap"]["parameters"] == {"type": "object"}
    assert out["nc"]["parameters"] == {"type": "object"}
    # 本地映射不在统一接口:抛 TypeError,而非静默接受
    with pytest.raises(TypeError):
        ToolComponent.normalize({"nc2": "连接(简单映射)"})
    assert ToolComponent.normalize(None) == {}
    assert ToolComponent.normalize([]) == {}


# ===== TraceComponent:ut+tr 轨迹投影 =====

async def test_trace_renders_call_and_result_interleaved(ws):
    ws.record_tool_call("s1", "nmap", {"host": "x"})
    ws.record_tool_result("s1", "nmap", "port 22 open")
    a = make_assembler(ws)
    ctx, _, _ = await a.assemble("planner")
    assert "# 本轮工具轨迹" in ctx
    assert "call nmap" in ctx          # ut:调用意图
    assert "-> port 22 open" in ctx    # tr:世界响应


async def test_trace_scope_only_after_last_replan(ws):
    ws.add_event("planner", "replan")
    ws.record_tool_call("s1", "nmap", {"host": "x"})
    ws.record_tool_result("s1", "nmap", "port 22 open")
    ws.add_event("planner", "replan")   # 推进轮次边界
    ws.record_tool_result("s2", "nc", "banner")
    a = make_assembler(ws)
    ctx, _, _ = await a.assemble("planner")
    assert "nmap" not in ctx       # 上一轮轨迹落回 history 审计,不进本轮 ctx
    assert "nc" in ctx
    assert "step=s2" in ctx


async def test_trace_agent_scope_filters_role(ws):
    ws.record_tool_result("s1", "nmap", "port 22 open", agent="executor")
    ws.record_tool_result("s2", "skill_query", "docs found", agent="evaluator_plan")
    a = CtxAssembler(ws)
    a.register("planner", TraceComponent(agent="executor"))
    ctx, _, _ = await a.assemble("planner")
    assert "nmap" in ctx
    assert "skill_query" not in ctx
    a2 = CtxAssembler(ws)
    a2.register("planner", TraceComponent(agent="evaluator_plan"))
    ctx2, _, _ = await a2.assemble("planner")
    assert "skill_query" in ctx2
    assert "nmap" not in ctx2


async def test_trace_no_replan_renders_all(ws):
    ws.record_tool_result("s1", "nmap", "port 22 open")
    a = make_assembler(ws)
    ctx, _, _ = await a.assemble("planner")
    assert "nmap" in ctx


async def test_trace_replan_event_not_rendered(ws):
    ws.record_tool_result("s1", "nc", "banner")
    ws.add_event("planner", "replan")
    a = make_assembler(ws)
    ctx, _, _ = await a.assemble("planner")
    assert "replan" not in ctx    # 只有 use_tool/tool_result 被投影,replan 只是边界标记


# ===== TraceComponent:压缩 =====

async def test_trace_compresses_to_index_without_llm(ws):
    # budget 按 token 计:index 档渲染 ~40-47 tok,raw 档 ~370 tok,取 100 保证
    # 触发压缩(raw→index)且 index 稳定收进预算(uuid 切分有 ±tok 抖动)
    ws.record_tool_result("s1", "nmap", "x" * 2000)
    a = make_assembler(ws)                          # 无 compress → 机械降级
    ctx, _, over = await a.assemble("planner", budget=100)
    tr = comp(a, "trace")
    assert tr.level == 1                            # 压到 index 档
    assert over == 0
    assert "# 本轮工具轨迹(索引)" in ctx
    assert "x" * 2000 not in ctx                    # 原文被索引替换
    assert "[ref " in ctx                           # uuid 引用(get_record 可展开)


async def test_trace_without_compress_stops_at_index(ws):
    ws.record_tool_result("s1", "nmap", "x" * 300)
    a = make_assembler(ws)
    ctx, _, _ = await a.assemble("planner", budget=40)
    tr = comp(a, "trace")
    assert tr.level == 1
    assert tr.can_advance() is False                # 无压缩 api → 摘要档不可用
    assert "摘要" not in ctx                        # 不装假摘要


async def test_trace_compressed_before_tools(ws):
    ws.set_tools([
        {"type": "function", "function": {"name": "nmap", "description": "端口扫描"}},
        {"type": "function", "function": {"name": "nc", "description": "原始连接"}},
        {"type": "function", "function": {"name": "curl", "description": "HTTP 请求"}},
        {"type": "function", "function": {"name": "hydra", "description": "口令爆破"}},
    ])
    ws.record_tool_result("s1", "nmap", "x" * 300)
    a = make_assembler(ws)
    await a.assemble("planner", budget=95)
    assert comp(a, "trace").level > comp(a, "tools").level  # 优先级 1 先压,4 后压


# ===== TraceComponent:摘要档 =====

async def test_trace_summary_warm_cache_zero_refold(ws):
    ws.record_tool_result("s1", "nmap", "x" * 300)
    calls = []

    async def fake_compress(prompt, content):
        calls.append(content)
        return "ok" if len(calls) == 1 else "X" * 500

    a = make_assembler(ws, compress=fake_compress)
    await a.precompress("planner")                  # 预热:折一次并落盘
    assert len(calls) == 1
    ctx, _, over = await a.assemble("planner", budget=20)  # 溢出尝试超限 → 机械兜底
    tr = comp(a, "trace")
    assert tr.level == 2                            # 命中预热缓存 → 摘要档
    assert "# 本轮工具轨迹(摘要)" in ctx
    assert "ok" in ctx
    assert over == 0
    assert len(calls) == 2                          # 预热 1 + 溢出尝试 1(超限);摘要档读缓存


async def test_trace_summary_refolds_on_round_change(ws):
    calls = []

    async def fake_compress(prompt, content):
        calls.append(content)
        return "[摘要]"

    ws.record_tool_result("s1", "nmap", "port 22 open")
    a = make_assembler(ws, compress=fake_compress)
    await a.precompress("planner")
    assert len(calls) == 1

    ws.add_event("planner", "replan")   # 新轮次:边界推进
    ws.record_tool_result("s2", "nc", "banner")
    await a.precompress("planner")                  # 本轮事件集变了 → 重折(替换,不累计)
    assert len(calls) == 2


async def test_trace_summary_cache_persists_across_load(tmp_path, ws):
    ws.record_tool_result("s1", "nmap", "port 22 open")
    calls = []

    async def fake_compress(prompt, content):
        calls.append(content)
        return "[摘要]"

    a = make_assembler(ws, compress=fake_compress)
    await a.precompress("planner")
    assert len(calls) == 1
    ws.sync()

    ws2 = Workspace.load("run-tools", root=tmp_path)
    a2 = make_assembler(ws2, compress=fake_compress)
    await a2.precompress("planner")
    assert len(calls) == 1                          # 签名缓存恢复 → 不重折


# ===== openai_tool_specs:白名单导出 + ws.tools 目录合并 =====

def test_openai_tool_specs_module_whitelist():
    """模块级 openai_tool_specs(names=...) 只导出白名单内的 @tool 注册工具。"""
    specs = openai_tool_specs(names={"get_doc"})
    assert [s["function"]["name"] for s in specs] == ["get_doc"]


def test_openai_tool_specs_instance_merges_catalog(ws):
    """实例级 openai_tool_specs = 注册表 + ws.tools 目录(能力声明),合成一张视图。"""
    ws.set_tools([{"type": "function", "function": {"name": "nmap", "description": "端口扫描"}}])
    reg = ToolRegistry()
    reg.set_workspace(ws)
    names = [s["function"]["name"] for s in reg.openai_tool_specs()]
    assert "get_doc" in names and "nmap" in names      # 注册表 + 目录合成
    filtered = [s["function"]["name"] for s in reg.openai_tool_specs(names={"nmap"})]
    assert filtered == ["nmap"]                         # names 白名单过滤目录
