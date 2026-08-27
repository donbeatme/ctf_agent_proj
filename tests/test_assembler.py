"""CtxAssembler + planner 组件测试:只读投影、生命周期事件分发、溢出压缩。

设计见 design/workspace.md §5/§7。核心不变量:**组件是 workspace 的只读投影**,
数据唯一真值在 workspace(ws.blueprint / ws.events / ws.docs),组件不持独立副本、
不做双重写入——改 workspace,重组装即自动反映。
"""

import pytest

from agent.blueprint import Blueprint, Step, StepStatus
from agent.ctx import (
    AgentCommComponent,
    CtxAssembler,
    DagComponent,
    DocsComponent,
    HistoryComponent,
    SystemPromptComponent,
    TaskComponent,
)
from agent.schema import EvalSource, Goal
from agent.workspace import Workspace


@pytest.fixture
def ws(tmp_path):
    return Workspace.create("run-asm", {"q": "x"}, root=tmp_path)


def make_assembler(ws):
    a = CtxAssembler(ws)
    a.register(
        "planner",
        SystemPromptComponent(),
        TaskComponent(),
        AgentCommComponent(),
        DagComponent(),
        HistoryComponent(),
        DocsComponent(),
    )
    return a


def comp(a, key):
    return next(c for c in a.components("planner") if c.key == key)


def two_step_bp():
    bp = Blueprint(meta={"task": "t"})
    bp.add_step(Step(id="s1", instruction="做", criterion="可验收"))
    bp.add_step(Step(id="s2", instruction="做二", criterion="可验收", depends_on=["s1"]))
    return bp


async def test_assemble_splits_ctx_and_system(ws):
    a = make_assembler(ws)
    ws.set_blueprint(two_step_bp())
    ws.record_opinion(EvalSource.STEP_EVAL, "retry", "s1 要更具体")
    ctx, system, over = await a.assemble(
        "planner",
        system="SYS_BASE\n重规划背景",
        raw_content={"q": "x"},
        goal_list=[Goal(id="目标一")],
    )
    assert over == 0
    assert "# 任务目标" in ctx and "目标一" in ctx
    assert "# 任务" in ctx and '"q"' in ctx
    assert "# 本轮评估意见" in ctx and "s1 要更具体" in ctx
    assert "# 当前计划" in ctx and '"s1"' in ctx
    assert system == "SYS_BASE\n重规划背景"
    assert "SYS_BASE" not in ctx


async def test_dag_projects_blueprint_without_manual_update(ws):
    """投影不变量:改 workspace 的 blueprint,重组装即反映,组件无需 update 喂数据。"""
    a = make_assembler(ws)
    bp = two_step_bp()
    ws.set_blueprint(bp)
    ctx1, _, _ = await a.assemble("planner")
    assert '"s3"' not in ctx1

    bp.add_step(Step(id="s3", instruction="加一步", criterion="可验收", depends_on=["s1"]))
    ctx2, _, _ = await a.assemble("planner")
    assert '"s3"' in ctx2


async def test_history_projects_events_and_filters_system(ws):
    a = make_assembler(ws)
    ws.add_event("planner", "replan")
    ws.record_step("s1", "pass", "完成")
    ws.add_event("system", "state_change")
    ctx, _, _ = await a.assemble("planner")
    assert "kind=replan" in ctx
    assert "kind=step_record" in ctx
    assert "verdict=pass" in ctx
    assert "SCHEDULING" not in ctx       # 系统行为照记本地,ctx 渲染过滤


async def test_docs_projects_registry_and_plan_review_pass_clears(ws):
    a = make_assembler(ws)
    ws.set_doc("doc1", "扫描开放端口")
    ctx, _, _ = await a.assemble("planner")
    assert "doc1" in ctx and "扫描开放端口" in ctx

    a.dispatch("plan_review_pass")
    assert ws.get_doc("doc1") is None
    ctx2, _, _ = await a.assemble("planner")
    assert "doc1" not in ctx2


async def test_agent_comm_replan_clears_round(ws):
    """agent_comm 作用域从事件流推导:replan 事件推进轮次边界,上一轮意见落回 history。"""
    a = make_assembler(ws)
    ws.record_opinion(EvalSource.REFLECT, "replan", "整体重构")
    ctx, _, _ = await a.assemble("planner")
    assert "整体重构" in ctx

    ws.add_event("planner", "replan")  # 推进轮次边界
    ctx2, _, _ = await a.assemble("planner")
    assert "整体重构" not in ctx2


async def test_agent_comm_render_includes_step_id(ws):
    """§5.5: 评估意见渲染带 step_id,planner 可定位意见针对哪一步。"""
    a = make_assembler(ws)
    ws.record_opinion(EvalSource.STEP_EVAL, "retry", "s1 要更具体", step_id="s1")
    ctx, _, _ = await a.assemble("planner")
    assert "step=s1" in ctx
    assert "s1 要更具体" in ctx


async def test_compression_respects_priority_history_before_dag(ws):
    from agent.llm_api import count_tokens
    a = make_assembler(ws)
    ws.set_blueprint(two_step_bp())
    ws.add_event("planner", "replan")
    ws.record_step("s1", "retry")
    ws.record_step("s1", "pass")

    ctx, _, _ = await a.assemble("planner", raw_content={"q": "x"})
    base_tok = count_tokens(ctx)
    # 预算只少一点点 → 只需推进一档,按优先级 history(2) 先于 dag(4);
    # 索引档只压 PASS(换 uuid 引用),retry/replan 保留原文
    ctx2, _, over = await a.assemble("planner", raw_content={"q": "x"}, budget=base_tok - 1)
    assert over == 0
    assert count_tokens(ctx2) <= base_tok - 1
    assert comp(a, "history").level == 1
    assert comp(a, "dag").level == 0
    assert comp(a, "task").level == 0


async def test_protect_skips_history_compresses_dag(ws):
    from agent.llm_api import count_tokens
    a = make_assembler(ws)
    ws.set_blueprint(two_step_bp())
    ws.add_event("planner", "replan")
    ws.record_step("s1", "retry")

    ctx, _, _ = await a.assemble("planner", raw_content={"q": "x"})
    base_tok = count_tokens(ctx)
    ctx2, _, over = await a.assemble("planner", raw_content={"q": "x"},
                                     budget=base_tok - 2, protect=["history"])
    assert over == 0
    assert count_tokens(ctx2) <= base_tok - 2
    assert comp(a, "history").level == 0   # 被保护,不压
    assert comp(a, "dag").level == 1       # 改压 dag


async def test_compression_never_touches_task_or_system(ws):
    a = make_assembler(ws)
    ws.set_blueprint(two_step_bp())
    ws.record_step("s1", "retry")

    ctx, system, over = await a.assemble("planner", system="SYS_HEADER",
                                         raw_content={"q": "x"}, budget=10)
    assert over > 0                        # 压无可压仍超预算 → 返回信号,组装器不硬压
    assert comp(a, "task").level == 0
    assert "# 任务" in ctx and '"q"' in ctx  # task 原文仍在
    assert system == "SYS_HEADER"


async def test_no_budget_means_no_compression(ws):
    a = make_assembler(ws)
    ws.set_blueprint(two_step_bp())
    ws.record_step("s1", "pass")
    await a.assemble("planner", raw_content={"q": "x"})
    assert all(c.level == 0 for c in a.components("planner"))


async def test_anchor_never_compresses_even_with_levels_and_methods(ws):
    """anchor 显式保护:即使声明了多档 + compress_methods,锚点组件也不进压缩候选。"""
    from agent.ctx import CtxComponent

    class Anchored(CtxComponent):
        key = "anchored"
        anchor = True
        LEVELS = ("raw", "tier")          # 本应可压,anchor 拦住
        compress_methods = "压我"
        def render(self):
            return "A" * 100

    a = make_assembler(ws)
    a.register("planner", Anchored())
    await a.assemble("planner", raw_content={"q": "x"})
    ctx, _, over = await a.assemble("planner", raw_content={"q": "x"}, budget=10)
    comp = next(c for c in a.components("planner") if c.key == "anchored")
    assert comp.level == 0                # anchor 拦住,不进机械候选
    assert over > 0                       # 压无可压 → 诚实信号
    assert "A" * 100 in ctx               # 原文保留


async def test_clear_scope_resets_levels(ws):
    a = make_assembler(ws)
    ws.set_blueprint(two_step_bp())
    ws.record_step("s1", "retry")
    await a.assemble("planner", raw_content={"q": "x"}, budget=10)
    assert comp(a, "dag").level == 1
    a.clear("planner")
    assert all(c.level == 0 for c in a.components("planner"))


async def test_assemble_start_levels_presets_trace_level(ws):
    """start_levels 指定组件起始压缩档位:drift 重试把 trace 预压到 summary 档。
    无 compress 注入时摘要档 render 回落到索引;未知 key/档位保持 raw。"""
    from agent.ctx import TraceComponent

    a = CtxAssembler(ws)
    a.register_class("executor", (TraceComponent, (), {"agent": "executor"}))
    ws.record_tool_call("s1", "cmd", {"cmd": "id"})
    ctx_raw, _, _ = await a.assemble("executor")
    assert "# 本轮工具轨迹" in ctx_raw
    assert "(索引)" not in ctx_raw
    ctx_comp, _, _ = await a.assemble("executor", start_levels={"trace": "summary"})
    assert "本轮工具轨迹(索引)" in ctx_comp
    ctx_other, _, _ = await a.assemble("executor", start_levels={"trace": "bogus", "nope": "summary"})
    assert "(索引)" not in ctx_other


async def test_run_end_deletes_all_components(ws):
    a = make_assembler(ws)
    await a.assemble("planner", raw_content={"q": "x"})
    assert all(c.created for c in a.components("planner"))
    a.dispatch("run_end")
    assert all(not c.created for c in a.components("planner"))


async def test_join_dedups_lines_keeping_first(ws):
    """判重兜底:同数据两通道进 ctx 时,重复行只保留第一个出现,顺序语义不破坏。"""
    from agent.ctx import CtxComponent

    class A(CtxComponent):
        key = "dup_a"
        def render(self):
            return "# 区块A\n共享行一\nA 独有"

    class B(CtxComponent):
        key = "dup_b"
        def render(self):
            return "# 区块B\n共享行一\nB 独有"

    a = make_assembler(ws)
    a.register("planner", A(), B())
    ctx, _, _ = await a.assemble("planner")
    assert ctx.count("共享行一") == 1
    assert ctx.index("共享行一") < ctx.index("A 独有")   # 第一个出现保留在原位
    assert "A 独有" in ctx and "B 独有" in ctx


async def test_docs_skeleton_level_drops_doc_keeps_id(ws):
    a = make_assembler(ws)
    ws.set_doc("doc1", "扫描开放端口")
    ws.record_step("s1", "pass")
    await a.assemble("planner", raw_content={"q": "x"}, budget=10)
    assert comp(a, "docs").level == 1
    ctx, _, _ = await a.assemble("planner", raw_content={"q": "x"}, budget=10)
    assert "doc1" in ctx
    assert "扫描开放端口" not in ctx


# ===== 四通道正交性:agent_comm 只投影本轮非 pass 意见 =====

async def test_agent_comm_excludes_pass_verdict(ws):
    """pass 是闸门(不产出内容);非 pass(FAIL/RETRY/ESCALATE/REPLAN)才进 ctx。"""
    a = make_assembler(ws)
    ws.record_opinion(EvalSource.PLAN_REVIEW, "pass", "计划可执行")
    ws.record_opinion(EvalSource.STEP_EVAL, "retry", "s1 需重试")
    ctx, _, _ = await a.assemble("planner")
    assert "s1 需重试" in ctx
    assert "计划可执行" not in ctx


async def test_agent_comm_boundary_scoped_to_latest_replan(ws):
    """replan 事件推进边界:只渲染最近一次 replan 之后的意见。"""
    a = make_assembler(ws)
    ws.add_event("planner", "replan")
    ws.record_opinion(EvalSource.SCHEDULING, "fail", "死锁重排")
    ctx, _, _ = await a.assemble("planner")
    assert "死锁重排" in ctx

    ws.add_event("planner", "replan")  # 新边界
    ws.record_opinion(EvalSource.STEP_EVAL, "retry", "s1 需重试")
    ctx2, _, _ = await a.assemble("planner")
    assert "s1 需重试" in ctx2
    assert "死锁重排" not in ctx2          # 上一轮意见落回 history,不进本轮


def test_history_only_step_record_and_replan(ws):
    """history = 全局 step_record 轨迹 + replan 边界;trace/ac 走各自通道,不混入。"""
    h = comp(make_assembler(ws), "history")
    ws.add_event("planner", "replan")
    ws.record_step("s1", "pass", "完成")
    ws.record_tool_result("s1", "nmap", "port 22 open")
    ws.record_opinion(EvalSource.STEP_EVAL, "retry", "s1 需重试")
    text = h.create(ws).render()
    assert "kind=step_record" in text
    assert "kind=replan" in text
    assert "nmap" not in text          # trace 不进 history
    assert "s1 需重试" not in text      # ac 不进 history


# ===== DagComponent:step_id 作用域(executor 的 dag.step 视角) =====

async def test_dag_step_scope_renders_only_step(ws):
    a = make_assembler(ws)
    ws.set_blueprint(two_step_bp())
    ctx, _, _ = await a.assemble("planner", step_id="s2")
    assert "# 当前步骤 s2" in ctx
    assert "instruction: 做二" in ctx
    assert "criterion: 可验收" in ctx
    assert '"s1"' not in ctx          # 只投影该步,不渲染全图


# ===== CtxAssembler.ingest:模型返回装填回 workspace(反向通道) =====

def test_ingest_planner_writes_blueprint_and_replan(ws):
    a = make_assembler(ws)
    a.ingest("planner", blueprint=two_step_bp(), reason="initial")
    assert ws.blueprint is not None
    assert "s1" in ws.blueprint.steps
    assert ws.events[-1].kind == "replan"


def test_ingest_executor_records_trace_and_step_result(ws):
    a = make_assembler(ws)
    ws.set_blueprint(two_step_bp())
    a.ingest("executor", step_id="s1", tool_calls=[
        {"tool": "nmap", "args": {"host": "x"}, "result": "port 22 open"}], result={"flag": "ok"})
    assert len(ws.query(kind="use_tool")) == 1
    assert ws.query(kind="use_tool")[0].detail.tool == "nmap"
    assert len(ws.query(kind="tool_result")) == 1
    assert ws.query(kind="tool_result")[0].detail.output == "port 22 open"
    assert ws.blueprint.steps["s1"].result == {"flag": "ok"}


def test_ingest_executor_normalizes_tool_call_aliases(ws):
    """契约:tool_calls 条目 tool/args/result;name/output 别名归一,result 缺省只记 use_tool。"""
    a = make_assembler(ws)
    ws.set_blueprint(two_step_bp())
    # OpenAI 风格别名:name + output
    a.ingest("executor", step_id="s1", tool_calls=[
        {"name": "nmap", "args": {"host": "x"}, "output": "port 22 open"}])
    ut = ws.query(kind="use_tool")
    assert len(ut) == 1 and ut[0].detail.tool == "nmap"
    tr = ws.query(kind="tool_result")
    assert len(tr) == 1 and tr[0].detail.output == "port 22 open"
    # result 缺省(工具无输出)→ 只记 use_tool,不记 tool_result
    a.ingest("executor", step_id="s2", tool_calls=[{"tool": "nc", "args": {"host": "y"}}])
    assert len(ws.query(kind="use_tool")) == 2
    assert len(ws.query(kind="tool_result")) == 1


def test_ingest_eval_records_opinion(ws):
    a = make_assembler(ws)
    a.ingest("evaluator_step", verdict="retry", opinion="s1 需重试", step_id="s1")
    evs = ws.query(kind="step_eval")
    assert len(evs) == 1
    assert evs[0].verdict == "retry"
    assert evs[0].detail.opinion == "s1 需重试"


def test_ingest_unknown_role_raises(ws):
    a = make_assembler(ws)
    with pytest.raises(ValueError):
        a.ingest("nobody", x=1)


async def test_executor_ctx_includes_docs_and_bound_skill_survives_pass(ws):
    """workspace 注册的 executor 角色含 Docs 组件;plan_review_pass 保留绑定
    skill_id 的文档(executor 可查),清掉未绑定的规划用文档。"""
    ws.set_doc("doc0", "SQL注入绕过认证完整步骤")
    ws.set_doc("doc1", "端口扫描流程")
    bp = Blueprint(meta={"task": "t"})
    bp.add_step(Step(id="s1", instruction="注入", criterion="拿到flag", skill_id="doc0"))
    ws.set_blueprint(bp)

    await ws.assembler.assemble("executor", step_id="s1")  # 先 assemble 实例化组件(create 注入 ws)
    ws.assembler.dispatch("plan_review_pass")
    assert ws.get_doc("doc0") is not None      # 绑定保留
    assert ws.get_doc("doc1") is None          # 未绑定清掉

    ctx, _, _ = await ws.assembler.assemble("executor", step_id="s1")
    assert "技能库文档" in ctx and "doc0" in ctx
    assert "skill: doc0" in ctx                # 当前步骤渲染带技能绑定


async def test_dag_render_includes_skill_binding(ws):
    """DAG 渲染带 skill 绑定:planner 原始 JSON 含 skill_id,executor 当前步骤视图含 skill。"""
    bp = Blueprint(meta={"task": "t"})
    bp.add_step(Step(id="s1", instruction="注入", criterion="拿到flag", skill_id="doc0"))
    ws.set_blueprint(bp)
    ctx, _, _ = await ws.assembler.assemble("planner")
    assert "skill_id" in ctx and "doc0" in ctx
    ctx2, _, _ = await ws.assembler.assemble("executor", step_id="s1")
    assert "skill: doc0" in ctx2


async def test_render_unchanged_after_event_replay(ws):
    """事件溯源:同一事件流重放(load)后,History/AgentComm/DAG 渲染逐字不变。"""
    bp = Blueprint(meta={"task": "t"})
    bp.add_step(Step(id="s1", instruction="做", criterion="可验收"))
    ws.set_blueprint(bp)
    ws.record_opinion(EvalSource.STEP_EVAL, "retry", "s1 要更具体", step_id="s1")
    bp.set_status("s1", StepStatus.PASSED, force=True)   # 与 live path 一致:先 set_status 再 record_step
    ws.record_step("s1", "pass", "完成", status="PASSED")

    a = make_assembler(ws)
    ctx1, _, _ = await a.assemble("planner", raw_content={"q": "x"}, goal_list=[Goal(id="目标一")])
    ws.sync()
    ws2 = Workspace.load("run-asm", root=ws.root.parent)
    a2 = make_assembler(ws2)
    ctx2, _, _ = await a2.assemble("planner", raw_content={"q": "x"}, goal_list=[Goal(id="目标一")])
    assert ctx1 == ctx2
