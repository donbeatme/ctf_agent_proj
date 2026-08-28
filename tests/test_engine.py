"""engine 调度器分发逻辑验证:任务完成判定 / 死锁处理 / REVISE 收尾 / 序列矩阵。

覆盖:
- 正常全过 → DONE(兜底全部节点终态进反思)
- ee 打 is_completed → 残留未完成节点不再触发死锁,直接进反思收尾
- 真死锁(ESCALATED 阻断)→ 注入重构提示重规划,限次解不开 → FAILED(记录 fail_reason)
- 死锁重排产出新方案 → 恢复执行到 DONE
- 评审不过置 REVISE → 修订后评审通过清回 PENDING,不误入死锁
- test_loop_sequences:mock 模型按预置序列改变计划/判定,驱动主循环跑不同轨迹,
  逐个断言终态与各步骤状态(重试、超限升级、评审修订、反思重排、振荡、超时等)。
- 图拓扑形态(菱形/多入口/删节点/分支升级)与 SKIPPED 前置死锁
- 外部 Agent 异常保护(executor/ep/ee/et 抛错 → 转失败信号,不崩引擎)
- run() 复用(同一 Engine 连续跑任务,运行态重置)与 REVISE 残留清回
- 空初始计划由 ep 判定(评审不过 → 重规划)
"""

import asyncio

import pytest

from agent.blueprint import Blueprint, Step, StepStatus
from agent.env_providers import SandboxHandle, SandboxProvider, SshProvider
from agent.evaluator import Diagnosis, EvalResult, MockEvaluator, Verdict
from agent.engine import Engine, EngineState
from agent.executor import ExecResult, MockExecutor, RealExecutor
from agent.llm_api import ToolResult
from agent.scheduler import ExecutionScheduler
from sandbox_env import SandboxSettings
from tests.mock_data import MOCK_TASK
from tests.test_env_providers import FakeSsh
from agent.planner import Planner
from agent.schema import (
    EvalEvent,
    EvalSource,
    EventKind,
    Feedback,
    GoalEvalDetail,
    PlannerInput,
    PlannerMode,
    StateContext,
    TaskInput,
    parse_plan,
)
from agent.workspace import MockWorkspace


def _apply(bp, response):
    patch = parse_plan(response).to_patch()
    bp.apply_patch(patch)
    return bp


class ScriptedPlanner:
    """按顺序消费预置 PlanPatch JSON 的 planner;记录每次输入。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def plan(self, pin):
        self.calls.append(pin)
        bp = Blueprint.from_dict(pin.feedback.dag) if pin.mode == PlannerMode.REVISE \
            else Blueprint(meta={"task": MOCK_TASK})
        return _apply(bp, self._responses.pop(0))


def seq(results):
    """按顺序返回 EvalResult;耗尽后保持最后一个。"""
    state = {"i": 0}

    def fn(ctx):
        r = results[min(state["i"], len(results) - 1)]
        state["i"] += 1
        return r

    return fn


def make_engine(planner_responses, ep, ee, et, executor, goal_responses=None, **kw):
    planner = ScriptedPlanner(planner_responses)
    evaluator = MockEvaluator(
        {"evaluator_plan": seq(ep), "evaluator_step": seq(ee), "evaluator_task": seq(et)},
        goal_responses=goal_responses,
    )
    engine = Engine(planner, executor, evaluator, workspace=MockWorkspace(), **kw)
    return engine, planner


def _plan_responses(*bodies):
    return ['{"add":' + bodies[0] + ',"reason":"initial"}'] + list(bodies[1:])


def make_engine_raw(planner_responses, ep, ee, et, executor=None, **kw):
    """与 make_engine 相同,但 ep/ee/et 直接传 callable(seq/raise_then/自定义),不做 seq 包装。"""
    planner = ScriptedPlanner(planner_responses)
    evaluator = MockEvaluator({"evaluator_plan": ep, "evaluator_step": ee, "evaluator_task": et})
    engine = Engine(planner, executor or MockExecutor(observation="执行完成"), evaluator,
                    workspace=MockWorkspace(), **kw)
    return engine, planner


def raise_then(results):
    """第一次调用抛 RuntimeError,之后按序返回 EvalResult(模拟外部评估 Agent 异常)。"""
    state = {"i": -1}

    def fn(ctx):
        state["i"] += 1
        if state["i"] == 0:
            raise RuntimeError("boom")
        return results[min(state["i"] - 1, len(results) - 1)]

    return fn


# ===== 1. 正常全过 → DONE =====

def test_happy_path_all_pass_done():
    engine, _ = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]},'
            '{"id":"s2","instruction":"编码","criterion":"可逆","depends_on":["s1"]}]',
            '{}',  # 反思终局修订:空补丁合法收尾
        ),
        ep=[EvalResult(Verdict.PASS, "计划可执行")],
        ee=[EvalResult(Verdict.PASS, "s1: 完成"), EvalResult(Verdict.PASS, "s2: 完成")],
        et=[EvalResult(Verdict.DONE, "反思: 无问题")],
        executor=MockExecutor(observation="执行完成"),
    )
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    assert engine.fail_reason is None
    assert all(s.status.value == "PASSED" for s in engine.bp.steps.values())
    assert engine.task_completed is False  # 未打 is_completed,靠全部终态收尾


def test_engine_wires_sandbox_session_lifecycle(monkeypatch):
    """scheduler 接线:run 开会话(acquire 容器)→ handle 注入执行器 → 结束 release(删容器还连接)。"""

    def fake_install(self, tool_ids, *, session_key=None, force=False):
        return {"installed": list(tool_ids)}

    monkeypatch.setattr("sandbox_env.base.SandboxManager.install_tools", fake_install)

    conns = []

    def factory():
        c = FakeSsh()
        conns.append(c)
        return c

    pool = SshProvider(factory=factory, max_connections=2)
    provider = SandboxProvider(pool, settings=SandboxSettings(ssh_host="vm"))
    sched = ExecutionScheduler(providers=[provider])
    received = []

    class _Exec(MockExecutor):
        allowed_cwd = "/challenge/x"

        def set_sandbox(self, handle):
            received.append(handle)

    engine, _ = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]}]',
            '{}',
        ),
        ep=[EvalResult(Verdict.PASS, "计划可执行")],
        ee=[EvalResult(Verdict.PASS, "s1: 完成")],
        et=[EvalResult(Verdict.DONE, "反思: 无问题")],
        executor=_Exec(observation="执行完成"),
        scheduler=sched,
    )
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    assert received and isinstance(received[0], SandboxHandle)   # acquire 的 handle 已注入
    cmds = [c for c, _ in conns[0].execs]
    assert any("docker run -d --name" in c and "-ex1" in c for c in cmds)  # acquire 建 actor 容器
    assert any("docker rm -f" in c for c in cmds)                          # release 删容器
    assert len(provider._active) == 0                                      # 活跃租约清空
    assert len(pool._idle) == 1                                            # 连接还池


def test_wave_runs_steps_concurrently(monkeypatch):
    """并行 wave(max_concurrency=2):两个独立入口步骤并发执行,每步独立容器租约。

    用执行区间互相重叠证明真并发(串行会退化为接续);docker run 容器名带各自
    actor(step.id)后缀;结束后连接全还池、租约清空。
    """
    import time as _time

    def fake_install(self, tool_ids, *, session_key=None, force=False):
        return {"installed": list(tool_ids)}

    monkeypatch.setattr("sandbox_env.base.SandboxManager.install_tools", fake_install)

    conns = []

    def factory():
        c = FakeSsh()
        conns.append(c)
        return c

    pool = SshProvider(factory=factory, max_connections=2)
    provider = SandboxProvider(pool, settings=SandboxSettings(ssh_host="vm"))
    sched = ExecutionScheduler(providers=[provider])

    intervals = {}

    async def run_fn(step, ctx, tool_exec=None):
        t0 = _time.monotonic()
        await asyncio.sleep(0.05)
        intervals[step.id] = (t0, _time.monotonic())
        return ExecResult(observation=f"{step.id}: 完成")

    class _Exec(MockExecutor):
        allowed_cwd = "/challenge/x"

    engine, _ = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]},'
            '{"id":"s2","instruction":"编码","criterion":"可逆","depends_on":[]}]',
            '{}',
        ),
        ep=[EvalResult(Verdict.PASS, "计划可执行")],
        ee=[EvalResult(Verdict.PASS, "s1: 完成"), EvalResult(Verdict.PASS, "s2: 完成")],
        et=[EvalResult(Verdict.DONE, "反思: 无问题")],
        executor=_Exec(observation="", fn=run_fn),
        scheduler=sched,
        max_concurrency=2,
    )
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    assert set(intervals) == {"s1", "s2"}
    # 两步骤执行区间互相重叠 → 真并发
    assert intervals["s1"][0] < intervals["s2"][1]
    assert intervals["s2"][0] < intervals["s1"][1]
    # 每步独立容器:docker run 容器名带各自 actor(step.id)后缀
    names = [c for conn in conns for c, _ in conn.execs if "docker run -d --name" in c]
    assert any("-s1" in n for n in names)
    assert any("-s2" in n for n in names)
    # 各自 release:连接全还池 + 租约清空
    assert len(pool._idle) == 2
    assert len(provider._active) == 0


def test_wave_retry_degrades_to_single_step(monkeypatch):
    """并行 wave 中某步 retry:退化为该步单步重放,其余未评估步骤回 PENDING 待重调度,
    不误判死锁;s1 重试成功后 s2 正常调度执行到 DONE。"""

    def fake_install(self, tool_ids, *, session_key=None, force=False):
        return {"installed": list(tool_ids)}

    monkeypatch.setattr("sandbox_env.base.SandboxManager.install_tools", fake_install)

    conns = []

    def factory():
        c = FakeSsh()
        conns.append(c)
        return c

    pool = SshProvider(factory=factory, max_connections=2)
    provider = SandboxProvider(pool, settings=SandboxSettings(ssh_host="vm"))
    sched = ExecutionScheduler(providers=[provider])
    ran = []

    async def run_fn(step, ctx, tool_exec=None):
        ran.append(step.id)
        return ExecResult(observation=f"{step.id}: 完成")

    class _Exec(MockExecutor):
        allowed_cwd = "/challenge/x"

    engine, planner = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]},'
            '{"id":"s2","instruction":"编码","criterion":"可逆","depends_on":[]}]',
            '{}',
        ),
        ep=[EvalResult(Verdict.PASS, "计划可执行")],
        # s1 第一拍 retry(占 ee 第 1 次),重试后 pass(第 2 次);s2 pass
        ee=[EvalResult(Verdict.RETRY, "s1: 不完整"),
            EvalResult(Verdict.PASS, "s1: 完成"),
            EvalResult(Verdict.PASS, "s2: 完成")],
        et=[EvalResult(Verdict.DONE, "反思: 无问题")],
        executor=_Exec(observation="", fn=run_fn),
        scheduler=sched,
        max_concurrency=2,
    )
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    assert ran.count("s1") == 2          # s1 第一拍 + retry 重放
    # s2 第一拍随 wave 被 abandon(结果丢弃、回 PENDING),之后重调度再执行一次
    assert ran.count("s2") == 2
    assert len(planner.calls) == 2       # 初始规划 + 终局反思(无多余 replan)
    assert len(pool._idle) == 2
    assert len(provider._active) == 0


def test_wave_step_eval_escalate_replans_with_eval_result(monkeypatch):
    """并行 wave 中某步 step_eval 判 ESCALATE → _replan 拿到的是 EvalResult。

    回归:此前 wave 分支把 ExecResult 传给 _replan(取 res.opinion 崩
    AttributeError);串行路径传 EvalResult。修后 wave 也传 EvalResult,
    升级意见进 turn、重排删 s1、s2 重调度后通过到 DONE。
    """

    def fake_install(self, tool_ids, *, session_key=None, force=False):
        return {"installed": list(tool_ids)}

    monkeypatch.setattr("sandbox_env.base.SandboxManager.install_tools", fake_install)

    conns = []

    def factory():
        c = FakeSsh()
        conns.append(c)
        return c

    pool = SshProvider(factory=factory, max_connections=2)
    provider = SandboxProvider(pool, settings=SandboxSettings(ssh_host="vm"))
    sched = ExecutionScheduler(providers=[provider])
    ran = []

    async def run_fn(step, ctx, tool_exec=None):
        ran.append(step.id)
        return ExecResult(observation=f"{step.id}: 完成")

    class _Exec(MockExecutor):
        allowed_cwd = "/challenge/x"

    engine, planner = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]},'
            '{"id":"s2","instruction":"提交","criterion":"平台判定","depends_on":[]}]',
            '{"remove":["s1"],"update":[{"id":"s2","depends_on":[]}],"reason":"escalate recovery"}',
            '{}',
        ),
        ep=[EvalResult(Verdict.PASS, "计划可执行"), EvalResult(Verdict.PASS, "计划可执行")],
        # wave 中 s1 首评 ESCALATE → replan 删 s1;s2 随 wave 回 PENDING,重调度后 pass
        ee=[EvalResult(Verdict.ESCALATE, "s1: 需重新设计"),
            EvalResult(Verdict.PASS, "s2: 完成")],
        et=[EvalResult(Verdict.DONE, "反思: 无问题")],
        executor=_Exec(observation="", fn=run_fn),
        scheduler=sched,
        max_concurrency=2,
    )
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    # 升级意见进 turn:证明 _replan 拿到的是 EvalResult(带 opinion),非 ExecResult
    assert engine.turn[0].source == EvalSource.STEP_EVAL
    assert engine.turn[0].opinion == "s1: 需重新设计"
    assert "s1" not in engine.bp.steps          # 升级后被重排删除
    assert engine.bp.steps["s2"].status.value == "PASSED"
    assert ran.count("s1") == 1                 # 只在首拍 wave 执行一次
    assert ran.count("s2") == 2                 # 首拍 wave + 重调度再执行
    assert len(planner.calls) == 3              # 初始 + escalate replan + reflect
    assert len(pool._idle) == 2
    assert len(provider._active) == 0


def test_executor_ctx_includes_history_index_and_plan_note():
    """executor ctx:history(index 档全局台账)+ agent_comm 含 planner 计划级 plan-note。

    进度知识走 history(已过兄弟的判定/产物),计划期预期走 agent_comm 的
    plan_note;task 组件保持稳定,不承载共享知识。ScriptedPlanner 不写 meta.reason,
    这里注入 ReasonPlanner 补 reason,验证 _record_plan 的 plan-note 落账链路。
    """
    seen = {}

    class ReasonPlanner(ScriptedPlanner):
        def plan(self, pin):
            bp = super().plan(pin)
            bp.meta.setdefault("reason", "initial")
            return bp

    class CaptureExecutor(MockExecutor):
        def run(self, step, ctx, tool_exec=None):
            seen["ctx"] = ctx
            return super().run(step, ctx, tool_exec)

    engine = Engine(
        ReasonPlanner(
            ['{"add":[{"id":"s1","instruction":"扫描端口","criterion":"flag","depends_on":[]},'
             '{"id":"s2","instruction":"扫服务","criterion":"flag","depends_on":["s1"]}],'
             '"reason":"initial"}',
             '{}']
        ),
        CaptureExecutor(observation="完成"),
        MockEvaluator({
            "evaluator_plan": seq([EvalResult(Verdict.PASS, "计划可执行")]),
            "evaluator_step": seq([EvalResult(Verdict.PASS, "s1: 完成"),
                                   EvalResult(Verdict.PASS, "s2: 完成")]),
            "evaluator_task": seq([EvalResult(Verdict.DONE, "反思: 无问题")]),
        }),
        workspace=MockWorkspace(),
    )
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    ctx = seen["ctx"]
    assert "执行历史(索引)" in ctx        # history 组件(index 档)进入 executor
    assert "plan_note: initial" in ctx    # 计划级 plan-note 进 agent_comm


def test_llm_usage_events_carry_step_node_id_and_round():
    """executor 的 llm_usage 事件携带 node_id/round(执行环境 ContextVar 落事件)。

    _run_step 经 set_run_context 置 node_id=step.id / round=attempts,exec_task 复制
    上下文 → _emit_llm_usage 读到归属;planner/evaluator(非步骤作用域)usage 不带。
    """
    engine, _ = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"可验收","depends_on":[]}]',
            '{}',
        ),
        ep=[EvalResult(Verdict.PASS, "计划可执行")],
        ee=[EvalResult(Verdict.PASS, "s1: 完成")],
        et=[EvalResult(Verdict.DONE, "反思: 无问题")],
        executor=MockExecutor(observation="执行完成"),
    )
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    usage = [e for e in engine.workspace.events if e.kind == EventKind.LLM_USAGE]
    exec_usage = [e for e in usage if e.detail.role == "executor"]
    assert exec_usage, "应有 executor llm_usage 事件"
    assert exec_usage[0].node_id == "s1"
    assert exec_usage[0].round == 1

def test_request_stop_fails_run():
    """前端停跑接口:request_stop 在主循环下一拍转 FAILED。"""

    class StopOnStart:
        def __init__(self):
            self.engine = None

        def on_run_started(self, **kw):
            self.engine.request_stop("用户停止")

    stopper = StopOnStart()
    engine, _ = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]}]',
            "{}",
        ),
        ep=[EvalResult(Verdict.PASS, "计划可执行")],
        ee=[EvalResult(Verdict.PASS, "s1: 完成")],
        et=[EvalResult(Verdict.DONE, "反思: 无问题")],
        executor=MockExecutor(observation="执行完成"),
        subscribers=[stopper],
    )
    stopper.engine = engine
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.FAILED
    assert engine.fail_reason == "用户停止"


# ===== 2. ee 打 is_completed:残留未完成节点不触发死锁 =====

def test_is_completed_skips_deadlock():
    engine, _ = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]},'
            '{"id":"s2","instruction":"编码","criterion":"可逆","depends_on":["s1"]},'
            '{"id":"s3","instruction":"提交","criterion":"平台判定","depends_on":["s2"]}]',
            '{}',  # 反思终局修订:空补丁合法收尾
        ),
        ep=[EvalResult(Verdict.PASS, "计划可执行")],
        ee=[EvalResult(Verdict.PASS, "s1: 完成", is_completed=True)],  # 任务目标已达成
        et=[EvalResult(Verdict.DONE, "反思: 无问题")],
        executor=MockExecutor(observation="执行完成"),
    )
    engine.run(MOCK_TASK)
    # ee 判 is_completed → 早停收口:s1 通过后不再调度 s2/s3,直接反思收尾
    assert engine.scheduler.state == EngineState.DONE
    assert engine.task_completed is True
    assert engine.fail_reason is None
    assert engine.bp.steps["s2"].status.value != "PASSED"  # 早停,未执行
    assert engine.bp.steps["s3"].status.value != "PASSED"  # 早停,未执行


# ===== 3. 真死锁:ESCALATED 阻断,限次解不开 → FAILED =====

def test_real_deadlock_fails_after_bounded_replans():
    engine, planner = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"访问目标","criterion":"拿到入口","depends_on":[]},'
            '{"id":"s2","instruction":"提交","criterion":"平台判定","depends_on":["s1"]}]',
            # s1 升级后第一次重规划:仍不解除 s1,只改 s2
            '{"update":[{"id":"s2","criterion":"平台判定通过(第一版)"}],"reason":"revise"}',
            # 三次死锁重排:每次只改 s2,不新增可执行节点、不解 s1
            '{"update":[{"id":"s2","criterion":"平台判定通过(第二版)"}],"reason":"deadlock1"}',
            '{"update":[{"id":"s2","criterion":"平台判定通过(第三版)"}],"reason":"deadlock2"}',
            '{"update":[{"id":"s2","criterion":"平台判定通过(第四版)"}],"reason":"deadlock3"}',
        ),
        ep=[EvalResult(Verdict.PASS, "计划可执行")],
        ee=[EvalResult(Verdict.ESCALATE, "s1: 目标不可达", observation="timeout")],
        et=[],
        executor=MockExecutor(observation="执行完成"),
        max_deadlock_attempts=3, max_stalls=100, max_replans=100,
    )
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.FAILED
    assert "调度死锁" in engine.fail_reason
    assert engine.replans == 4  # s1 升级 1 次 + 死锁重排 3 次
    # 三次死锁重排都携带状态上下文(trigger=deadlock + 死锁报告 detail)
    deadlock_calls = [p for p in planner.calls
                      if p.feedback and p.feedback.state_context
                      and p.feedback.state_context.trigger == "deadlock"]
    assert len(deadlock_calls) == 3
    assert all("死锁" in p.feedback.state_context.detail for p in deadlock_calls)
    # 状态上下文不进 ctx 段落,由 planner 渲染进系统提示词(见 test_planner_renders_state_context)


# ===== 4. 死锁重排产出新方案 → 恢复执行到 DONE =====

def test_deadlock_recovery_new_plan():
    engine, _ = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"访问目标","criterion":"拿到入口","depends_on":[]},'
            '{"id":"s2","instruction":"提交","criterion":"平台判定","depends_on":["s1"]}]',
            '{"update":[{"id":"s2","criterion":"平台判定通过(第一版)"}],"reason":"revise"}',
            # 死锁重排:绕开被升级的 s1——删掉它并把 s2 改为入口直接执行
            '{"remove":["s1"],"update":[{"id":"s2","depends_on":[]}],"reason":"deadlock recovery"}',
            '{}',  # 反思
        ),
        ep=[EvalResult(Verdict.PASS, "计划可执行")],
        ee=[
            EvalResult(Verdict.ESCALATE, "s1: 目标不可达", observation="timeout"),
            EvalResult(Verdict.PASS, "s2: 提交成功"),
        ],
        et=[EvalResult(Verdict.DONE, "反思: 无问题")],
        executor=MockExecutor(observation="执行完成"),
        max_deadlock_attempts=3, max_stalls=100, max_replans=100,
    )
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    assert engine.fail_reason is None
    assert engine.bp.steps["s2"].status.value == "PASSED"
    assert "s1" not in engine.bp.steps  # 被死锁重排删除


# ===== 5. 评审不过置 REVISE → 修订后评审通过清回 PENDING =====

def test_unexpected_ee_verdict_escalates():
    """ee 返回契约外 verdict(FAIL)→ 按升级处理,不误判步骤通过。"""
    engine, _ = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"访问目标","criterion":"拿到入口","depends_on":[]},'
            '{"id":"s2","instruction":"提交","criterion":"平台判定","depends_on":["s1"]}]',
            '{"update":[{"id":"s2","criterion":"平台判定通过(第一版)"}],"reason":"revise"}',
            '{"remove":["s1"],"update":[{"id":"s2","depends_on":[]}],"reason":"deadlock recovery"}',
            '{}',  # 反思
        ),
        ep=[EvalResult(Verdict.PASS, "计划可执行")],
        ee=[
            EvalResult(Verdict.FAIL, "s1: FAIL"),  # ee 不该返 FAIL:契约外
            EvalResult(Verdict.PASS, "s2: 提交成功"),
        ],
        et=[EvalResult(Verdict.DONE, "反思: 无问题")],
        executor=MockExecutor(observation="执行完成"),
    )
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    assert engine.fail_reason is None
    # FAIL 走了升级→重规划(产生 turn 事件),而非 pass 分支(s1 未被标 PASSED)
    assert engine.turn and engine.turn[0].source == EvalSource.STEP_EVAL
    assert "FAIL" in engine.turn[0].opinion
    assert engine.bp.steps["s2"].status.value == "PASSED"


def test_deadlock_budget_resets_per_episode():
    """死锁重排预算按'连续解不开'计量:两段独立死锁各自在预算内解开 → DONE,不累计误 FAIL。"""
    engine, _ = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"访问目标","criterion":"拿到入口","depends_on":[]},'
            '{"id":"s2","instruction":"编码","criterion":"可逆","depends_on":["s1"]},'
            '{"id":"s3","instruction":"提交","criterion":"平台判定","depends_on":["s2"]}]',
            '{"update":[{"id":"s3","criterion":"平台判定通过(第一版)"}],"reason":"s1 升级后收紧 s3"}',
            '{"remove":["s1"],"update":[{"id":"s2","depends_on":[]}],"reason":"deadlock ep1"}',
            '{}',  # s3 升级后重规划:不改结构(no-op)
            '{"remove":["s3"]}',  # deadlock ep2
            '{}',  # 反思
        ),
        ep=[EvalResult(Verdict.PASS, "计划可执行")],
        ee=[
            EvalResult(Verdict.ESCALATE, "s1: 目标不可达", observation="timeout"),
            EvalResult(Verdict.PASS, "s2: 编码完成"),
            EvalResult(Verdict.ESCALATE, "s3: 平台判定失败", observation="wrong flag"),
        ],
        et=[EvalResult(Verdict.DONE, "反思: 无问题")],
        executor=MockExecutor(observation="执行完成"),
        max_deadlock_attempts=1,  # 每段死锁只给 1 次重排预算
        max_stalls=100, max_replans=100,
    )
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    assert engine.fail_reason is None
    assert engine.bp.steps["s2"].status.value == "PASSED"
    assert "s1" not in engine.bp.steps  # 第一段死锁重排删除
    assert "s3" not in engine.bp.steps  # 第二段死锁重排删除


def test_revise_cleared_on_review_pass():
    engine, _ = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]},'
            '{"id":"s2","instruction":"编码","criterion":"可逆","depends_on":["s1"]}]',
            '{"update":[{"id":"s2","criterion":"编码结果可逆(收紧)"}],"reason":"review fix"}',
            '{}',  # 反思
        ),
        ep=[
            EvalResult(Verdict.FAIL, "s2 验收标准不清晰"),
            EvalResult(Verdict.PASS, "计划可执行"),
        ],
        ee=[EvalResult(Verdict.PASS, "s1: 完成"), EvalResult(Verdict.PASS, "s2: 完成")],
        et=[EvalResult(Verdict.DONE, "反思: 无问题")],
        executor=MockExecutor(observation="执行完成"),
    )
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    assert engine.fail_reason is None
    assert all(s.status.value == "PASSED" for s in engine.bp.steps.values())


# ===== 序列矩阵:mock 模型按预置序列改状态,驱动主循环跑不同轨迹 =====

def _chk_not_completed(e, p):
    assert e.task_completed is False


def _chk_completed(e, p):
    assert e.task_completed is True


def _chk_attempts3(e, p):
    assert e.bp.steps["s1"].attempts == 3


def _chk_deadlock_prompt(e, p):
    dead = [c for c in p.calls if c.feedback and c.feedback.state_context
            and c.feedback.state_context.trigger == "deadlock"]
    assert len(dead) == 2, f"死锁重排应带死锁状态上下文 2 次,实际 {len(dead)}"
    assert all("死锁" in c.feedback.state_context.detail for c in dead)


def _chk_oscillation(e, p):
    assert "振荡" in e.fail_reason


# 每个场景 = 规划响应序列 + 各评估角色 verdict 序列;steps 断言最终步骤状态(None=被重排移除)
SCENARIOS = [
    {  # 1 纯通过链
        "id": "happy_all_pass",
        "planner": _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]},'
            '{"id":"s2","instruction":"编码","criterion":"可逆","depends_on":["s1"]}]',
            '{}',  # 反思终局修订
        ),
        "evaluator_plan": [EvalResult(Verdict.PASS, "计划可执行")],
        "evaluator_step": [EvalResult(Verdict.PASS, "s1: 完成"), EvalResult(Verdict.PASS, "s2: 完成")],
        "evaluator_task": [EvalResult(Verdict.DONE, "反思: 无问题")],
        "state": EngineState.DONE,
        "steps": {"s1": "PASSED", "s2": "PASSED"},
        "check": _chk_not_completed,
    },
    {  # 2 重试后通过
        "id": "retry_then_pass",
        "planner": _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]}]',
            '{}',
        ),
        "evaluator_plan": [EvalResult(Verdict.PASS, "计划可执行")],
        "evaluator_step": [EvalResult(Verdict.RETRY, "s1: 重试1"), EvalResult(Verdict.RETRY, "s1: 重试2"),
               EvalResult(Verdict.PASS, "s1: 完成")],
        "evaluator_task": [EvalResult(Verdict.DONE, "反思: 无问题")],
        "state": EngineState.DONE,
        "steps": {"s1": "PASSED"},
        "check": _chk_attempts3,
    },
    {  # 3 重试超限转升级,重排绕过
        "id": "retry_exhaust_escalate",
        "planner": _plan_responses(
            '[{"id":"s1","instruction":"访问目标","criterion":"拿到入口","depends_on":[]},'
            '{"id":"s2","instruction":"提交","criterion":"平台判定","depends_on":["s1"]}]',
            '{"remove":["s1"],"update":[{"id":"s2","depends_on":[]}]}',
            '{}',
        ),
        "evaluator_plan": [EvalResult(Verdict.PASS, "计划可执行"), EvalResult(Verdict.PASS, "计划可执行")],
        "evaluator_step": [EvalResult(Verdict.RETRY, "s1: 重试1"), EvalResult(Verdict.RETRY, "s1: 重试2"),
               EvalResult(Verdict.RETRY, "s1: 重试3"), EvalResult(Verdict.PASS, "s2: 完成")],
        "evaluator_task": [EvalResult(Verdict.DONE, "反思: 无问题")],
        "state": EngineState.DONE,
        "steps": {"s1": None, "s2": "PASSED"},
    },
    {  # 4 升级 → 死锁 → 重排解开
        "id": "escalate_deadlock_resolve",
        "planner": _plan_responses(
            '[{"id":"s1","instruction":"访问目标","criterion":"拿到入口","depends_on":[]},'
            '{"id":"s2","instruction":"提交","criterion":"平台判定","depends_on":["s1"]}]',
            '{"update":[{"id":"s2","criterion":"平台判定通过(收紧)"}],"reason":"revise"}',
            '{"remove":["s1"],"update":[{"id":"s2","depends_on":[]}],"reason":"deadlock resolve"}',
            '{}',
        ),
        "evaluator_plan": [EvalResult(Verdict.PASS, "计划可执行")],
        "evaluator_step": [EvalResult(Verdict.ESCALATE, "s1: 目标不可达", observation="timeout"),
               EvalResult(Verdict.PASS, "s2: 完成")],
        "evaluator_task": [EvalResult(Verdict.DONE, "反思: 无问题")],
        "state": EngineState.DONE,
        "steps": {"s1": None, "s2": "PASSED"},
    },
    {  # 5 评审不过 → 修订 → 通过 → 执行
        "id": "review_fail_revise",
        "planner": _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]},'
            '{"id":"s2","instruction":"编码","criterion":"可逆","depends_on":["s1"]}]',
            '{"update":[{"id":"s2","criterion":"编码结果可逆(收紧)"}],"reason":"review fix"}',
            '{}',
        ),
        "evaluator_plan": [EvalResult(Verdict.FAIL, "s2 验收标准不清晰"), EvalResult(Verdict.PASS, "计划可执行")],
        "evaluator_step": [EvalResult(Verdict.PASS, "s1: 完成"), EvalResult(Verdict.PASS, "s2: 完成")],
        "evaluator_task": [EvalResult(Verdict.DONE, "反思: 无问题")],
        "state": EngineState.DONE,
        "steps": {"s1": "PASSED", "s2": "PASSED"},
    },
    {  # 6 ee 判完成 → 早停收口,跳过剩余 DAG 步骤
        "id": "is_completed_skip_deadlock",
        "planner": _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]},'
            '{"id":"s2","instruction":"编码","criterion":"可逆","depends_on":["s1"]}]',
            '{}',  # 反思终局修订:空补丁合法收尾
            '{}',
        ),
        "evaluator_plan": [EvalResult(Verdict.PASS, "计划可执行")],
        "evaluator_step": [EvalResult(Verdict.PASS, "s1: 完成", is_completed=True),
               EvalResult(Verdict.ESCALATE, "s2: 编码失败")],
        "evaluator_task": [EvalResult(Verdict.DONE, "反思: 无问题")],
        "state": EngineState.DONE,
        "steps": {"s1": "PASSED", "s2": "PENDING"},  # 早停:s2 未执行,保持 PENDING
        "check": _chk_completed,
    },
    {  # 7 死锁解不开 → FAILED
        "id": "deadlock_fail",
        "planner": _plan_responses(
            '[{"id":"s1","instruction":"访问目标","criterion":"拿到入口","depends_on":[]},'
            '{"id":"s2","instruction":"提交","criterion":"平台判定","depends_on":["s1"]}]',
            '{"update":[{"id":"s2","criterion":"平台判定通过(第一版)"}]}',
            '{"update":[{"id":"s2","criterion":"平台判定通过(第二版)"}]}',
            '{"update":[{"id":"s2","criterion":"平台判定通过(第三版)"}]}',
        ),
        "evaluator_plan": [EvalResult(Verdict.PASS, "计划可执行")],
        "evaluator_step": [EvalResult(Verdict.ESCALATE, "s1: 目标不可达")],
        "evaluator_task": [],
        "kw": {"max_deadlock_attempts": 2},
        "state": EngineState.FAILED,
        "check": _chk_deadlock_prompt,
    },
    {  # 8 振荡(连续无改动)→ FAILED
        "id": "oscillation_fail",
        "planner": _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]}]',
            '{}', '{}',
        ),
        "evaluator_plan": [EvalResult(Verdict.PASS, "计划可执行"), EvalResult(Verdict.PASS, "计划可执行")],
        "evaluator_step": [EvalResult(Verdict.ESCALATE, "s1: 无法完成")],
        "evaluator_task": [],
        "kw": {"max_stalls": 2},
        "state": EngineState.FAILED,
        "check": _chk_oscillation,
    },
    {  # 9 反思重排(et REPLAN)→ 修订后重新评审 → DONE
        "id": "reflect_replan_done",
        "planner": _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]}]',
            '{"update":[{"id":"s1","criterion":"拿到 flag 文本(收紧)"}],"reason":"et 意见"}',
            '{}',  # 第二次反思(DONE)的终局修订
        ),
        "evaluator_plan": [EvalResult(Verdict.PASS, "计划可执行"),
                           EvalResult(Verdict.PASS, "修订后可执行")],
        "evaluator_step": [EvalResult(Verdict.PASS, "s1: 完成")],
        "evaluator_task": [EvalResult(Verdict.REPLAN, "收紧 s1 验收标准"),
                           EvalResult(Verdict.DONE, "无问题")],
        "state": EngineState.DONE,
        "steps": {"s1": "PASSED"},
    },
    {  # 10 总调度次数超限 → FAILED + fail_reason
        "id": "max_cycles_exceeded",
        "planner": _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]},'
            '{"id":"s2","instruction":"编码","criterion":"可逆","depends_on":["s1"]}]',
            '{}',
        ),
        "evaluator_plan": [EvalResult(Verdict.PASS, "计划可执行")],
        "evaluator_step": [EvalResult(Verdict.PASS, "s1: 完成"), EvalResult(Verdict.PASS, "s2: 完成")],
        "evaluator_task": [EvalResult(Verdict.DONE, "反思: 无问题")],
        "kw": {"max_cycles": 3},
        "state": EngineState.FAILED,
        "steps": {},
        "fail_reason": True,
    },
    {  # 11 goal-driven: step PASS 后 goal_eval 逐条置 complete → task_completed → DONE
        "id": "goal_driven_completion",
        "task": {"description": "test", "goals": [
            {"id": "g1", "description": "获取源码"},
            {"id": "g2", "description": "找到flag"},
        ]},
        "planner": _plan_responses(
            '[{"id":"s1","instruction":"下载附件","criterion":"拿到文件","depends_on":[]},'
            '{"id":"s2","instruction":"分析源码找flag","criterion":"提取flag","depends_on":["s1"]}]',
            '{}',
        ),
        "evaluator_plan": [EvalResult(Verdict.PASS, "计划可执行")],
        "evaluator_step": [EvalResult(Verdict.PASS, "s1: 完成"), EvalResult(Verdict.PASS, "s2: 完成")],
        "evaluator_task": [EvalResult(Verdict.DONE, "反思: 目标全部达成")],
        "goal_responses": [
            # s1 PASS 后: g1 完成(引用 s1)
            [GoalEvalDetail(goal_id="g1", complete=True, evidence=["s1"],
                           reasoning="s1 已下载源码文件,满足'获取源码'目标")],
            # s2 PASS 后: g2 完成(引用 s2), g1 已完成不再评估
            [GoalEvalDetail(goal_id="g2", complete=True, evidence=["s2"],
                           reasoning="s2 提取到 flag,满足'找到flag'目标")],
        ],
        "state": EngineState.DONE,
        "steps": {"s1": "PASSED", "s2": "PASSED"},
    },
    {  # 12 goal-driven resume: 断点续跑时从 events 重建 goal 状态
        "id": "goal_driven_resume",
        "task": {"description": "test", "goals": [
            {"id": "g1", "description": "获取源码"},
        ]},
        "planner": _plan_responses(
            '[{"id":"s1","instruction":"下载附件","criterion":"拿到文件","depends_on":[]}]',
            '{}',
        ),
        "evaluator_plan": [EvalResult(Verdict.PASS, "计划可执行")],
        "evaluator_step": [EvalResult(Verdict.PASS, "s1: 完成")],
        "evaluator_task": [EvalResult(Verdict.DONE, "反思: 目标达成")],
        "goal_responses": [
            [GoalEvalDetail(goal_id="g1", complete=True, evidence=["s1"],
                           reasoning="s1 已下载源码,满足目标")],
        ],
        "state": EngineState.DONE,
        "steps": {"s1": "PASSED"},
    },
]


def _run_scenario(sc):
    goal_responses = sc.get("goal_responses")
    if goal_responses is not None:
        it = iter(goal_responses)
        goal_responses = lambda ctx, goals, dag: next(it)

    engine, planner = make_engine(
        sc["planner"], sc["evaluator_plan"], sc["evaluator_step"], sc["evaluator_task"],
        sc.get("exec") or MockExecutor(observation="执行完成"),
        goal_responses=goal_responses,
        **sc.get("kw", {}),
    )
    engine.run(sc.get("task", MOCK_TASK))
    assert engine.scheduler.state == sc["state"], (
        f"终态 {engine.scheduler.state.value},期望 {sc['state'].value}"
    )
    if sc.get("fail_reason"):
        assert engine.fail_reason, "期望 fail_reason 非空"
    for sid, status in sc.get("steps", {}).items():
        if status is None:
            assert sid not in engine.bp.steps, f"{sid} 应被重排移除,实际仍存在"
        else:
            assert engine.bp.steps[sid].status.value == status, (
                f"{sid} 终态 {engine.bp.steps[sid].status.value},期望 {status}"
            )
    if sc.get("check"):
        sc["check"](engine, planner)


@pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: s["id"])
def test_loop_sequences(sc):
    _run_scenario(sc)


def test_escalated_step_rearmed_by_criterion_tighten():
    """ee 升级后 planner 收紧 criterion → 该步回 PENDING 重跑 → DONE(内容变更重置自身,保留恢复路径)。"""
    engine, _ = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]}]',
            '{"update":[{"id":"s1","criterion":"拿到 flag 文本(收紧)"}],"reason":"ee 意见"}',
            '{}',
        ),
        ep=[EvalResult(Verdict.PASS, "计划可执行")],
        ee=[EvalResult(Verdict.ESCALATE, "s1: 标准太泛"),
            EvalResult(Verdict.PASS, "s1: 完成")],
        et=[EvalResult(Verdict.DONE, "反思: 无问题")],
        executor=MockExecutor(observation="执行完成"),
    )
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    assert engine.bp.steps["s1"].status.value == "PASSED"
    assert engine.bp.steps["s1"].attempts == 2


# ===== 4. 图拓扑形态 =====

GRAPH_SCENARIOS = [
    {  # 1 菱形 fan-out/fan-in:全过 → DONE,全部 PASSED
        "id": "diamond_fanout_fanin",
        "planner": _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]},'
            '{"id":"s2","instruction":"A 路","criterion":"A 完成","depends_on":["s1"]},'
            '{"id":"s3","instruction":"B 路","criterion":"B 完成","depends_on":["s1"]},'
            '{"id":"s4","instruction":"合并","criterion":"A+B 齐备","depends_on":["s2","s3"]}]',
            '{}',
        ),
        "evaluator_plan": [EvalResult(Verdict.PASS, "计划可执行")],
        "evaluator_step": [EvalResult(Verdict.PASS, "s1: 完成"), EvalResult(Verdict.PASS, "s2: 完成"),
               EvalResult(Verdict.PASS, "s3: 完成"), EvalResult(Verdict.PASS, "s4: 完成")],
        "evaluator_task": [EvalResult(Verdict.DONE, "反思: 无问题")],
        "state": EngineState.DONE,
        "steps": {"s1": "PASSED", "s2": "PASSED", "s3": "PASSED", "s4": "PASSED"},
    },
    {  # 2 多入口独立分支:一支升级 → 重排移除阻塞点,另一支照常 → DONE
        "id": "multi_entry_branch_escalate",
        "planner": _plan_responses(
            '[{"id":"s1","instruction":"主路","criterion":"主路完成","depends_on":[]},'
            '{"id":"s2","instruction":"依赖主路","criterion":"收尾","depends_on":["s1"]},'
            '{"id":"s3","instruction":"旁路","criterion":"旁路完成","depends_on":[]},'
            '{"id":"s4","instruction":"依赖旁路","criterion":"旁路收尾","depends_on":["s3"]}]',
            '{"update":[{"id":"s4","criterion":"旁路利用完成(收紧)"}],"reason":"ee 意见"}',
            '{"remove":["s1"]}',
            '{}',
        ),
        "evaluator_plan": [EvalResult(Verdict.PASS, "计划可执行")],
        "evaluator_step": [EvalResult(Verdict.ESCALATE, "s1: 主路不可行"),
               EvalResult(Verdict.PASS, "s3: 完成"),
               EvalResult(Verdict.PASS, "s4: 完成"),
               EvalResult(Verdict.PASS, "s2: 完成")],
        "evaluator_task": [EvalResult(Verdict.DONE, "反思: 无问题")],
        "state": EngineState.DONE,
        "steps": {"s1": None, "s2": "PASSED", "s3": "PASSED", "s4": "PASSED"},
    },
    {  # 3 删中间节点:依赖重接线后继续执行 → DONE
        "id": "remove_middle_node",
        "planner": _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]},'
            '{"id":"s2","instruction":"中转","criterion":"中转完成","depends_on":["s1"]},'
            '{"id":"s3","instruction":"提交","criterion":"平台判定","depends_on":["s2"]}]',
            '{"remove":["s2"]}',
            '{}',
        ),
        "evaluator_plan": [EvalResult(Verdict.PASS, "计划可执行")],
        "evaluator_step": [EvalResult(Verdict.PASS, "s1: 完成"),
               EvalResult(Verdict.ESCALATE, "s2: 中转不可行"),
               EvalResult(Verdict.PASS, "s3: 完成")],
        "evaluator_task": [EvalResult(Verdict.DONE, "反思: 无问题")],
        "state": EngineState.DONE,
        "steps": {"s1": "PASSED", "s2": None, "s3": "PASSED"},
    },
    {  # 4 菱形分支升级:一支阻塞合并点 → 死锁重排删支 → 合并节点照常 → DONE
        "id": "diamond_branch_escalate_merge",
        "planner": _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]},'
            '{"id":"s2","instruction":"A 路","criterion":"A 完成","depends_on":["s1"]},'
            '{"id":"s3","instruction":"B 路","criterion":"B 完成","depends_on":["s1"]},'
            '{"id":"s4","instruction":"合并","criterion":"A+B 齐备","depends_on":["s2","s3"]}]',
            '{"update":[{"id":"s4","criterion":"A+B 合并完成(收紧)"}],"reason":"ee 意见"}',
            '{"remove":["s2"]}',
            '{}',
        ),
        "evaluator_plan": [EvalResult(Verdict.PASS, "计划可执行")],
        "evaluator_step": [EvalResult(Verdict.PASS, "s1: 完成"),
               EvalResult(Verdict.ESCALATE, "s2: A 路不可行"),
               EvalResult(Verdict.PASS, "s3: 完成"),
               EvalResult(Verdict.PASS, "s4: 完成")],
        "evaluator_task": [EvalResult(Verdict.DONE, "反思: 无问题")],
        "state": EngineState.DONE,
        "steps": {"s1": "PASSED", "s2": None, "s3": "PASSED", "s4": "PASSED"},
    },
]


@pytest.mark.parametrize("sc", GRAPH_SCENARIOS, ids=lambda s: s["id"])
def test_graph_shapes(sc):
    _run_scenario(sc)


class _SkipInitialPlanner:
    """初次规划即产出带 SKIPPED 前置步骤的图(补丁无法表达 SKIPPED 状态,故手写)。"""

    def __init__(self, revise_responses):
        self._responses = list(revise_responses)
        self.calls = []

    def plan(self, pin):
        self.calls.append(pin)
        if pin.mode == PlannerMode.INITIAL:
            bp = Blueprint(meta={"task": MOCK_TASK})
            bp.add_step(Step("s1", "读题", "拿到文本"))
            bp.add_step(Step("s2", "提交", "平台判定", ["s1"]))
            bp.set_status("s1", StepStatus.SKIPPED)
            return bp
        return _apply(Blueprint.from_dict(pin.feedback.dag), self._responses.pop(0))


def test_skipped_predecessor_deadlock_resolves():
    """SKIPPED 前置不满足 PASSED,依赖方永远不 ready → 调度死锁 → 重排删掉 SKIPPED 节点后恢复。"""
    planner = _SkipInitialPlanner(['{"remove":["s1"]}', '{}'])
    evaluator = MockEvaluator({
        "evaluator_plan": seq([EvalResult(Verdict.PASS, "计划可执行")]),
        "evaluator_step": seq([EvalResult(Verdict.PASS, "s2: 完成")]),
        "evaluator_task": seq([EvalResult(Verdict.DONE, "反思: 无问题")]),
    })
    engine = Engine(planner, MockExecutor(observation="执行完成"), evaluator, workspace=MockWorkspace())
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    assert engine.fail_reason is None
    assert engine.bp.steps["s2"].status.value == "PASSED"
    assert "s1" not in engine.bp.steps
    assert engine.turn[0].source == EvalSource.SCHEDULING


# ===== 6. 外部 Agent 异常保护(design/scheduler.md §4 健壮性) =====

def _raise_executor(step, ctx):
    raise RuntimeError("boom executor")


def test_executor_exception_becomes_ee_observation():
    """executor 抛异常 → _safe_call 转成 ExecResult(observation=异常文本) 喂给 ee,不崩引擎。"""
    observed = {}

    def ee(ctx):
        observed["ctx"] = ctx
        return EvalResult(Verdict.PASS, "s1: 完成")

    evaluator = MockEvaluator({
        "evaluator_plan": seq([EvalResult(Verdict.PASS, "计划可执行")]),
        "evaluator_step": ee,
        "evaluator_task": seq([EvalResult(Verdict.DONE, "反思: 无问题")]),
    })
    planner = ScriptedPlanner(_plan_responses(
        '[{"id":"s1","instruction":"访问目标","criterion":"拿到入口","depends_on":[]}]', '{}'))
    engine = Engine(planner, MockExecutor(fn=_raise_executor), evaluator, workspace=MockWorkspace())
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    assert engine.bp.steps["s1"].status.value == "PASSED"
    assert "执行异常" in observed["ctx"] and "boom executor" in observed["ctx"]


def test_review_exception_triggers_replan():
    """ep(review) 抛异常 → 转 Verdict.FAIL 意见入 turn → 回 PLANNING 重规划,不崩引擎。"""
    engine, _ = make_engine_raw(
        _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]}]',
            '{"update":[{"id":"s1","criterion":"拿到 flag 文本(收紧)"}],"reason":"review exception"}',
            '{}',
        ),
        ep=raise_then([EvalResult(Verdict.PASS, "计划可执行")]),
        ee=seq([EvalResult(Verdict.PASS, "s1: 完成")]),
        et=seq([EvalResult(Verdict.DONE, "反思: 无问题")]),
    )
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    assert engine.turn[0].source == EvalSource.PLAN_REVIEW
    assert "评审异常" in engine.turn[0].opinion


def test_step_eval_exception_escalates():
    """ee(step_eval) 抛异常 → 转 Verdict.ESCALATE → 该步升级 → 重规划绕过,不崩引擎。"""
    engine, _ = make_engine_raw(
        _plan_responses(
            '[{"id":"s1","instruction":"访问目标","criterion":"拿到入口","depends_on":[]},'
            '{"id":"s2","instruction":"提交","criterion":"平台判定","depends_on":["s1"]}]',
            '{"remove":["s1"],"update":[{"id":"s2","depends_on":[]}],"reason":"escalate recovery"}',
            '{}',
        ),
        ep=seq([EvalResult(Verdict.PASS, "计划可执行"), EvalResult(Verdict.PASS, "计划可执行")]),
        ee=raise_then([EvalResult(Verdict.PASS, "s2: 完成")]),
        et=seq([EvalResult(Verdict.DONE, "反思: 无问题")]),
    )
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    assert engine.turn[0].source == EvalSource.STEP_EVAL
    assert "步骤校验异常" in engine.turn[0].opinion
    assert "s1" not in engine.bp.steps          # 升级后被死锁重排删除
    assert engine.bp.steps["s2"].status.value == "PASSED"


def test_reflect_exception_still_terminates_done():
    """et(reflect) 抛异常 → 转 Verdict.REPLAN → 修订后重新评审 → 二次反思 DONE,不崩引擎。"""
    engine, _ = make_engine_raw(
        _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]}]',
            '{}', '{}'),
        ep=seq([EvalResult(Verdict.PASS, "计划可执行")]),
        ee=seq([EvalResult(Verdict.PASS, "s1: 完成")]),
        et=raise_then([EvalResult(Verdict.DONE, "反思: 无问题")]),
    )
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    assert engine.fail_reason is None
    assert engine.turn[0].source == EvalSource.REFLECT
    assert "反思异常" in engine.turn[0].opinion
    assert engine.turn[-1].source == EvalSource.REFLECT
    assert "反思: 无问题" in engine.turn[-1].opinion


# ===== 7. run() 复用:同一 Engine 连续跑多个任务 =====

def test_engine_reusable_across_tasks():
    engine, _ = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]}]', '{}'),
        ep=[EvalResult(Verdict.PASS, "计划可执行")],
        ee=[EvalResult(Verdict.PASS, "s1: 完成")],
        et=[EvalResult(Verdict.DONE, "反思: 无问题")],
        executor=MockExecutor(observation="执行完成"),
    )
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    assert engine.replans == 1 and len(engine.turn) == 1  # 反思终局修订一次

    # 复用同一 Engine 跑第二个任务:若运行态未重置,replans 会累计、turn 会残留
    engine.planner = ScriptedPlanner(_plan_responses(
        '[{"id":"sA","instruction":"访问目标","criterion":"拿到入口","depends_on":[]}]', '{}'))
    engine.evaluator = MockEvaluator({
        "evaluator_plan": seq([EvalResult(Verdict.PASS, "计划可执行")]),
        "evaluator_step": seq([EvalResult(Verdict.PASS, "sA: 完成")]),
        "evaluator_task": seq([EvalResult(Verdict.DONE, "反思: 无问题")]),
    })
    engine.run({"task": "second"})
    assert engine.scheduler.state == EngineState.DONE
    assert engine.replans == 1                      # 从第一个任务的 1 重置,未累计
    assert len(engine.turn) == 1                    # 不残留第一个任务的 turn
    assert engine.turn[0].source == EvalSource.REFLECT
    assert engine.current is None
    assert engine.fail_reason is None
    assert engine.task_completed is False
    assert "s1" not in engine.bp.steps              # 蓝图已换
    assert engine.bp.steps["sA"].status.value == "PASSED"


def test_workspace_reset_on_engine_reuse():
    """§5.4: 复用同一 Engine 跑第二个 run 时,workspace 的 run 级状态(blueprint/events/steps)被清理。"""
    engine, _ = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]}]', '{}'),
        ep=[EvalResult(Verdict.PASS, "计划可执行")],
        ee=[EvalResult(Verdict.PASS, "s1: 完成")],
        et=[EvalResult(Verdict.DONE, "反思: 无问题")],
        executor=MockExecutor(observation="执行完成", result={"flag": "x"}),
    )
    engine.run(MOCK_TASK)
    ws = engine.workspace
    assert "s1" in ws.blueprint.steps
    assert any(e.kind == EventKind.STEP_RECORD for e in ws.events)

    # 复用跑第二个任务:workspace 应只留第二个 run 的投影数据
    engine.planner = ScriptedPlanner(_plan_responses(
        '[{"id":"sA","instruction":"访问目标","criterion":"拿到入口","depends_on":[]}]', '{}'))
    engine.evaluator = MockEvaluator({
        "evaluator_plan": seq([EvalResult(Verdict.PASS, "计划可执行")]),
        "evaluator_step": seq([EvalResult(Verdict.PASS, "sA: 完成")]),
        "evaluator_task": seq([EvalResult(Verdict.DONE, "反思: 无问题")]),
    })
    engine.run({"task": "second"})
    assert "sA" in ws.blueprint.steps and "s1" not in ws.blueprint.steps
    assert "s1" not in ws.steps
    assert all(e.step_id != "s1" for e in ws.events)
    assert any(e.step_id == "sA" for e in ws.events)   # 第二个 run 的事件已写入


def test_run_result_exposes_completion_and_product():
    """§5.3: run() 后 run_result 暴露终态/达成标志/通过步骤的最终产物。"""
    engine, _ = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]},'
            '{"id":"s2","instruction":"编码","criterion":"可逆","depends_on":["s1"]}]',
            '{}',
        ),
        ep=[EvalResult(Verdict.PASS, "计划可执行")],
        ee=[EvalResult(Verdict.PASS, "s1: 完成"), EvalResult(Verdict.PASS, "s2: 完成")],
        et=[EvalResult(Verdict.DONE, "反思: 无问题")],
        executor=MockExecutor(observation="执行完成", result={"flag": "CTF{x}"}),
    )
    engine.run(MOCK_TASK)
    rr = engine.run_result
    assert rr is not None
    assert rr.state == "DONE"
    assert rr.completed is False          # 未打 is_completed,靠全部终态收尾
    assert rr.fail_reason is None
    assert set(rr.product) == {"s1", "s2"}
    assert rr.product["s1"] == {"flag": "CTF{x}"}
    assert rr.replans == 1 and rr.cycles > 0
    assert rr.tokens == 0                 # mock 未上报用量 → 0


class UsagePlanner:
    """planner 返回时在 bp.meta 写入 token 用量(模拟真实 _usage 上报)。"""

    def __init__(self, responses, usage):
        self._responses = list(responses)
        self._usage = usage

    def plan(self, pin):
        bp = Blueprint.from_dict(pin.feedback.dag) if pin.mode == PlannerMode.REVISE \
            else Blueprint(meta={"task": MOCK_TASK})
        bp.meta["_usage"] = self._usage
        return _apply(bp, self._responses.pop(0))


def test_run_token_budget_exceeded_fails():
    """§5.1: run 级累计 token 预算超限 → FAILED,run_result.tokens 记录实际用量。"""
    planner = UsagePlanner(
        _plan_responses('[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]}]'),
        {"prompt_tokens": 100, "completion_tokens": 50},
    )
    evaluator = MockEvaluator({
        "evaluator_plan": seq([EvalResult(Verdict.PASS, "计划可执行")]),
        "evaluator_step": seq([EvalResult(Verdict.PASS, "s1: 完成")]),
        "evaluator_task": seq([EvalResult(Verdict.DONE, "反思: 无问题")]),
    })
    engine = Engine(planner, MockExecutor(observation="执行完成"), evaluator,
                    workspace=MockWorkspace(), run_token_budget_tokens=100)
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.FAILED
    assert "token 预算超限" in engine.fail_reason
    assert engine.run_result.tokens == 150
    assert engine.run_result.state == "FAILED"


def test_planner_receives_task_and_goals_initial_and_revise():
    """外部契约:TaskInput 在初始规划与重规划都携带 task + goal_list(单一来源,不丢)。"""
    task = {**MOCK_TASK, "goals": [{"id": "g1"}, {"id": "g2"}]}
    engine, planner = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]}]',
            '{}',  # 反思终局修订(第二次 REVISE 调用)
        ),
        ep=[EvalResult(Verdict.PASS, "计划可执行")],
        ee=[EvalResult(Verdict.PASS, "s1: 完成")],
        et=[EvalResult(Verdict.DONE, "反思: 无问题")],
        executor=MockExecutor(observation="执行完成"),
    )
    engine.run(task)
    init_pin = planner.calls[0]
    assert init_pin.mode == PlannerMode.INITIAL
    assert init_pin.task_input.raw_content == MOCK_TASK   # 理解层消费 raw["goals"] 后 raw_content 干净
    assert [g.id for g in init_pin.task_input.goal_list] == ["g1", "g2"]
    # 反思终局修订触发第二次 REVISE 规划:raw_content 不丢,goal_list 仍在
    assert len(planner.calls) == 2
    revise_pin = planner.calls[1]
    assert revise_pin.mode == PlannerMode.REVISE
    assert revise_pin.task_input.raw_content == MOCK_TASK
    assert [g.id for g in revise_pin.task_input.goal_list] == ["g1", "g2"]


def test_goal_eval_empty_reasoning_degrades_not_crash():
    """外部评估返回空 reasoning(契约违规)→ 降级(CTX_OVERFLOW)而非崩 run。"""
    task = {**MOCK_TASK, "goals": [{"id": "g1"}]}
    engine, _ = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]}]',
            '{}',
        ),
        ep=[EvalResult(Verdict.PASS, "计划可执行")],
        ee=[EvalResult(Verdict.PASS, "s1: 完成")],
        et=[EvalResult(Verdict.DONE, "反思: 无问题")],
        executor=MockExecutor(observation="执行完成"),
        goal_responses=lambda ctx, goals, dag: [
            GoalEvalDetail(goal_id="g1", complete=True, evidence=["s1"])],  # reasoning 为空
    )
    engine.run(task)   # 不抛异常(旧实现会 ValidationError 崩 run)
    assert engine.scheduler.state == EngineState.DONE
    assert "g1" in engine._goal_complete   # 完成标记在 append 前已写
    # turn 未收录该 goal 意见(append 失败被降级捕获)
    assert not any(e.source == EvalSource.GOAL_EVAL for e in engine.turn)


# ===== 8. REVISE 残留未被补丁触碰 → 评审通过 _clear_revise 清回 PENDING =====

def test_revise_residual_cleared_when_patch_ignores_it():
    """评审不过置 REVISE 后,planner 补丁只改 s1 未触碰 s2;评审通过时残留 REVISE 由 _clear_revise 清回 PENDING。"""
    engine, _ = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]},'
            '{"id":"s2","instruction":"编码","criterion":"可逆","depends_on":["s1"]}]',
            '{"update":[{"id":"s1","criterion":"拿到 flag 文本(收紧)"}],"reason":"review fix 只改 s1"}',
            '{}',
        ),
        ep=[EvalResult(Verdict.FAIL, "s2 验收标准不清晰"), EvalResult(Verdict.PASS, "计划可执行")],
        ee=[EvalResult(Verdict.PASS, "s1: 完成"), EvalResult(Verdict.PASS, "s2: 完成")],
        et=[EvalResult(Verdict.DONE, "反思: 无问题")],
        executor=MockExecutor(observation="执行完成"),
    )
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    assert all(s.status.value == "PASSED" for s in engine.bp.steps.values())


# ===== 9. 空初始计划:由 ep 判断(模拟真 ep 直接 FAIL → 回 PLANNING 重规划) =====

def test_empty_initial_plan_rejected_by_review():
    """空计划评审不过 → 回 PLANNING 重规划产出真步骤 → DONE。
    空计划不放行是 ep 的职责,引擎调度层不做空图特判;真 ep 上线后替换此 mock。"""
    planner = ScriptedPlanner([
        '{"add":[],"reason":"empty initial"}',
        '{"add":[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]}],"reason":"regenerate"}',
        '{}',
    ])
    evaluator = MockEvaluator({
        "evaluator_plan": seq([EvalResult(Verdict.FAIL, "计划为空,不可执行"), EvalResult(Verdict.PASS, "计划可执行")]),
        "evaluator_step": seq([EvalResult(Verdict.PASS, "s1: 完成")]),
        "evaluator_task": seq([EvalResult(Verdict.DONE, "反思: 无问题")]),
    })
    engine = Engine(planner, MockExecutor(observation="执行完成"), evaluator, workspace=MockWorkspace())
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    assert engine.bp.steps["s1"].status.value == "PASSED"
    assert engine.turn[0].source == EvalSource.PLAN_REVIEW
    assert "计划为空" in engine.turn[0].opinion


# ===== 10. PlannerInput 两条路:task_input(任务理解层 mock)+ feedback(③ 自产) =====

def test_planner_input_two_paths_validation():
    with pytest.raises(ValueError):
        PlannerInput(mode=PlannerMode.INITIAL, feedback=Feedback(dag={}, turn=[]))
    with pytest.raises(ValueError):
        PlannerInput(mode=PlannerMode.REVISE, feedback=Feedback(dag=None, turn=[
            EvalEvent(source=EvalSource.PLAN_REVIEW, opinion="x")]))
    with pytest.raises(ValueError):
        PlannerInput(mode=PlannerMode.REVISE, feedback=Feedback(dag={}, turn=[]))
    pin = PlannerInput(
        mode=PlannerMode.REVISE,
        feedback=Feedback(dag={}, turn=[EvalEvent(source=EvalSource.PLAN_REVIEW, opinion="x")]),
    )
    assert pin.feedback.dag == {}


async def test_planner_renders_state_context_into_system():
    """状态注入提示词:解释触发原因/状态语义,渲染进系统提示词,不进 ctx。"""
    captured = {}

    def llm_call(*, system=None, prompt=None, **kw):
        captured["system"] = system
        captured["prompt"] = prompt
        return "{}"
    planner = Planner(llm_call=llm_call)
    pin = PlannerInput(
        mode=PlannerMode.REVISE,
        task_input=TaskInput(raw_content=MOCK_TASK),
        feedback=Feedback(
            dag={"meta": {}, "steps": {}},
            turn=[EvalEvent(source=EvalSource.SCHEDULING, opinion="死锁")],
            state_context=StateContext(trigger="deadlock", detail="被阻塞: s1"),
        ),
    )
    await planner.plan(pin)
    assert "# 重规划背景" in captured["system"]
    assert "调度死锁" in captured["system"]        # 触发原因说明
    assert "ESCALATED" in captured["system"]       # 状态语义
    assert "被阻塞: s1" in captured["system"]      # 具体原因
    assert "重规划背景" not in captured["prompt"]   # 不进 ctx 段落


def test_plan_review_note_invites_independent_opinion_eval():
    """plan_review_fail 提示词不指示按意见改,而是邀请评估意见合理性并给理由。"""
    from agent.planner import TRIGGER_NOTES
    note = TRIGGER_NOTES["plan_review_fail"]
    assert "按评审意见修订" not in note
    assert "评估其合理性" in note
    assert "理由" in note



def test_engine_records_workspace_and_dispatches_hooks(tmp_path):
    """引擎接线:规划/步骤落账 ws(blueprint/events),生命周期 hook 分发,planner ctx 投影。"""
    from agent.workspace import Workspace
    ws = Workspace.create("run-hooks", MOCK_TASK, root=tmp_path)
    responses = [
        '{"add":[{"id":"s1","instruction":"扫描","criterion":"flag","depends_on":[]}],"reason":"i"}',
        '{}',
    ]
    state = {"i": 0}

    def llm(**kw):
        r = responses[min(state["i"], len(responses) - 1)]
        state["i"] += 1
        return r

    planner = Planner(llm_call=llm, workspace=ws)
    evaluator = MockEvaluator({
        "evaluator_plan": lambda ctx: EvalResult(Verdict.PASS, "ok"),
        "evaluator_step": lambda ctx: EvalResult(Verdict.PASS, "验收通过"),
        "evaluator_task": lambda ctx: EvalResult(Verdict.DONE, "收尾"),
    })
    engine = Engine(planner, MockExecutor(observation="完成"), evaluator, workspace=ws)
    engine.run(MOCK_TASK)

    assert ws.blueprint is not None and "s1" in ws.blueprint.steps
    kinds = [e.kind for e in ws.events]
    assert "replan" in kinds                        # 规划落账打点(_record_plan)
    assert "step_record" in kinds                   # 步骤验收落账打点(_record_step)
    # run_end 已分发:组件释放;assemble 重建后投影真实 workspace(dag + history)
    assert all(not c.created for c in ws.assembler.components("planner"))
    ctx, _, _ = asyncio.run(ws.assembler.assemble("planner", raw_content=MOCK_TASK))
    assert '"s1"' in ctx
    assert "step_record" in ctx


def test_executor_ingest_records_trace_and_step_result(tmp_path):
    """executor 返回走 ingest 反向装填:工具轨迹落 use_tool 事件、总结果写 dag.step.result
    (进 step_record 的 detail),供 trace 通道与 ee 验收。"""
    from agent.workspace import Workspace
    ws = Workspace.create("run-trace", MOCK_TASK, root=tmp_path)
    responses = [
        '{"add":[{"id":"s1","instruction":"扫描","criterion":"flag","depends_on":[]}],"reason":"i"}',
        '{}',
    ]
    state = {"i": 0}

    def llm(**kw):
        r = responses[min(state["i"], len(responses) - 1)]
        state["i"] += 1
        return r

    planner = Planner(llm_call=llm, workspace=ws)
    evaluator = MockEvaluator({
        "evaluator_plan": lambda ctx: EvalResult(Verdict.PASS, "ok"),
        "evaluator_step": lambda ctx: EvalResult(Verdict.PASS, "验收通过"),
        "evaluator_task": lambda ctx: EvalResult(Verdict.DONE, "收尾"),
    })
    executor = MockExecutor(observation="扫到开放端口", result={"port": 22},
                            tool_calls=[{"tool": "nmap", "args": {"host": "x"}, "result": "port 22 open"}])
    engine = Engine(planner, executor, evaluator, workspace=ws)
    engine.run(MOCK_TASK)

    calls = ws.query(kind="use_tool")
    assert len(calls) == 1
    assert calls[0].step_id == "s1"
    assert calls[0].detail.tool == "nmap"
    results = ws.query(kind="tool_result")
    assert len(results) == 1
    assert results[0].detail.tool == "nmap"
    assert results[0].detail.output == "port 22 open"
    rec = ws.query(kind="step_record")[0]
    assert rec.detail.result == {"port": 22}   # 总结果进 step_record(全局查询/审计)


def test_replan_opinion_recorded_to_events():
    """评估意见落事件流(agent_comm 通道):escalate 重排与反思终局各一条,pass 不落。"""
    engine, _ = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]}]',
            '{"update":[{"id":"s1","criterion":"拿到 flag(收紧)"}],"reason":"escalate 后收紧"}',
            '{}',
        ),
        ep=[EvalResult(Verdict.PASS, "计划可执行")],
        ee=[
            EvalResult(Verdict.ESCALATE, "s1: 失败", observation="x"),
            EvalResult(Verdict.PASS, "s1: 完成"),
        ],
        et=[EvalResult(Verdict.DONE, "反思: 无问题")],
        executor=MockExecutor(observation="执行完成"),
    )
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    evs = engine.workspace.query(kind="step_eval")
    assert len(evs) == 1                          # 只记非 pass:escalate
    assert evs[0].verdict == "escalate"
    assert evs[0].detail.observation == "x"
    reflects = engine.workspace.query(kind="reflect")
    assert len(reflects) == 1
    assert reflects[0].verdict == "done"


def test_retry_opinion_recorded_to_events():
    """未超限的 retry 也落意见事件(不推进 replan 边界),ex 重放时能从 ac 看到为何 retry。"""
    engine, _ = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]}]',
            '{}',
        ),
        ep=[EvalResult(Verdict.PASS, "计划可执行")],
        ee=[EvalResult(Verdict.RETRY, "s1: 需重试"), EvalResult(Verdict.PASS, "s1: 完成")],
        et=[EvalResult(Verdict.DONE, "反思: 无问题")],
        executor=MockExecutor(observation="执行完成"),
    )
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    evs = engine.workspace.query(kind="step_eval")
    assert len(evs) == 1
    assert evs[0].verdict == "retry"
    assert evs[0].step_id == "s1"


def test_engine_wires_external_agent_ctx():
    """engine 接线:ep/ex/ee/et 上下文经 assembler.assemble 组装(dag/task/step/观察投影),
    不再走 slice_for mock。ep 见全 DAG+任务、ex 见步骤作用域、ee 见步骤+观察、et 见全 DAG。"""
    seen = {}

    def cap(key, fn):
        def wrap(*a):
            seen[key] = a
            return fn(*a)
        return wrap

    class CaptureExecutor(MockExecutor):
        def run(self, step, ctx, tool_exec=None):
            seen["executor"] = (step.id, ctx)
            return super().run(step, ctx, tool_exec)

    engine, _ = make_engine_raw(
        _plan_responses(
            '[{"id":"s1","instruction":"扫描端口","criterion":"flag","depends_on":[]}]',
            '{}',  # 反思终局修订
        ),
        ep=cap("evaluator_plan", lambda ctx: EvalResult(Verdict.PASS, "计划可执行")),
        ee=cap("evaluator_step", lambda ctx: EvalResult(Verdict.PASS, "s1: 完成")),
        et=cap("evaluator_task", lambda ctx: EvalResult(Verdict.DONE, "反思: 无问题")),
        executor=CaptureExecutor(observation="扫到开放端口"),
    )
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE

    ep_ctx = seen["evaluator_plan"][0]
    assert "当前计划" in ep_ctx and '"s1"' in ep_ctx   # dag 投影全 DAG
    assert '"title"' in ep_ctx and "base64" in ep_ctx  # task 投影

    ex_step, ex_ctx = seen["executor"]
    assert ex_step == "s1"                              # dag 步骤作用域
    assert "instruction: 扫描端口" in ex_ctx
    assert "observation" not in ex_ctx                  # 执行观察只在 step_eval,不进 executor

    ee_ctx = seen["evaluator_step"][0]
    assert "instruction: 扫描端口" in ee_ctx             # 步骤作用域
    assert "observation: 扫到开放端口" in ee_ctx          # 观察经 engine 附到 step_eval ctx

    et_ctx = seen["evaluator_task"][0]
    assert '"s1"' in et_ctx                             # 反思见全 DAG


# ===== 11. 端到端冒烟:真实 planner(mock llm 注入)+ mock 其他 agent =====

def test_real_planner_mock_agents_smoke(tmp_path):
    """冒烟路径:真实 Planner + mock 执行/评估 → 全环走到 DONE,产出真 DAG、全部 PASSED。"""
    from agent.evaluator import SmokeEvaluator
    from agent.workspace import Workspace

    ws = Workspace.create("run-smoke", MOCK_TASK, root=tmp_path)
    responses = [
        '{"add":[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]},'
        '{"id":"s2","instruction":"编码","criterion":"可逆","depends_on":["s1"]}],'
        '"reason":"initial"}',
        '{}',  # 反思终局修订
    ]
    state = {"i": 0}

    def llm(**kw):
        r = responses[min(state["i"], len(responses) - 1)]
        state["i"] += 1
        return r

    engine = Engine(Planner(llm_call=llm, workspace=ws),
                    MockExecutor(observation="执行完成"),
                    SmokeEvaluator(ws), workspace=ws)
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    assert engine.fail_reason is None
    assert {"s1", "s2"} <= set(engine.bp.steps)
    assert all(s.status.value == "PASSED" for s in engine.bp.steps.values())


def test_real_planner_ep_fail_replans_then_done(tmp_path):
    """空初始计划 → SmokeEvaluator.ep FAIL → 真实 planner 重规划产出步骤 → DONE。
    验证 ep 按真实 blueprint 判空驱动重规划,而非无脑放行。"""
    from agent.evaluator import SmokeEvaluator
    from agent.workspace import Workspace

    ws = Workspace.create("run-smoke2", MOCK_TASK, root=tmp_path)
    responses = [
        '{"add":[],"reason":"empty initial"}',
        '{"add":[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]}],'
        '"reason":"regenerate"}',
        '{}',
    ]
    state = {"i": 0}

    def llm(**kw):
        r = responses[min(state["i"], len(responses) - 1)]
        state["i"] += 1
        return r

    engine = Engine(Planner(llm_call=llm, workspace=ws),
                    MockExecutor(observation="执行完成"),
                    SmokeEvaluator(ws), workspace=ws)
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    assert engine.replans == 2        # 空计划重规划 + 反思终局修订
    assert engine.bp.steps["s1"].status.value == "PASSED"


# ===== 12. Engine.resume 端到端测试 (P2-8) =====

class _NoopPlanner:
    """占位 planner:不会被调用,仅满足 Engine.__init__ 接口。"""
    def plan(self, pin):
        raise AssertionError("planner should not be called")


def test_resume_after_completed_run_skips_to_done(tmp_path):
    """完整 run 到 DONE 后 resume:直返 DONE,不进入 dispatch 循环。"""
    from agent.workspace import Workspace

    ws = Workspace.create("run-r1", MOCK_TASK, root=tmp_path)
    responses = [
        '{"add":[{"id":"s1","instruction":"扫描","criterion":"flag","depends_on":[]}],"reason":"i"}',
        '{}',
    ]
    state = {"i": 0}

    def llm(**kw):
        r = responses[min(state["i"], len(responses) - 1)]
        state["i"] += 1
        return r

    planner = Planner(llm_call=llm, workspace=ws)
    evaluator = MockEvaluator({
        "evaluator_plan": lambda ctx: EvalResult(Verdict.PASS, "ok"),
        "evaluator_step": lambda ctx: EvalResult(Verdict.PASS, "通过"),
        "evaluator_task": lambda ctx: EvalResult(Verdict.DONE, "收尾"),
    })
    engine = Engine(planner, MockExecutor(observation="完成"), evaluator, workspace=ws)
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE

    engine2 = Engine.resume("run-r1", _NoopPlanner(),
                            MockExecutor(observation="x"),
                            MockEvaluator({}), root=tmp_path)
    assert engine2.scheduler.state == EngineState.DONE
    assert engine2.bp is not None and "s1" in engine2.bp.steps
    assert engine2.bp.steps["s1"].status.value == "PASSED"


def test_persisted_raw_content_excludes_goals_and_resume_rebuilds(tmp_path):
    """goals 任务:meta["task"] 落理解层剥离后的 raw_content(不带 goals),resume 重建一致。"""
    from agent.workspace import Workspace

    ws = Workspace.create("run-goals", MOCK_TASK, root=tmp_path)
    # 用户原始任务带 goals(理解层应剥离)
    task = {**MOCK_TASK, "goals": [{"id": "g1"}, {"id": "g2"}]}

    responses = [
        '{"add":[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]}],"reason":"i"}',
        '{}',
    ]
    state = {"i": 0}

    def llm(**kw):
        r = responses[min(state["i"], len(responses) - 1)]
        state["i"] += 1
        return r

    planner = Planner(llm_call=llm, workspace=ws)
    evaluator = MockEvaluator({
        "evaluator_plan": lambda ctx: EvalResult(Verdict.PASS, "ok"),
        "evaluator_step": lambda ctx: EvalResult(Verdict.PASS, "通过"),
        "evaluator_task": lambda ctx: EvalResult(Verdict.DONE, "收尾"),
    })
    engine = Engine(planner, MockExecutor(observation="完成"), evaluator, workspace=ws)
    engine.run(task)
    assert engine.scheduler.state == EngineState.DONE
    # meta["task"] 已替换为理解层输出:goals 剥离
    assert "goals" not in ws.meta["task"]
    assert ws.meta["goal_list"] == [{"id": "g1"}, {"id": "g2"}]

    # resume:raw_content 不带 goals,goal_list 重建
    engine2 = Engine.resume("run-goals", _NoopPlanner(),
                            MockExecutor(observation="x"),
                            MockEvaluator({}), root=tmp_path)
    assert engine2.scheduler.state == EngineState.DONE
    assert "goals" not in engine2.raw_content
    assert [g.id for g in engine2.task_input.goal_list] == ["g1", "g2"]


def test_resume_mid_run_continues_to_done(tmp_path):
    """手动构造中间态 workspace(SCHEDULING + 残留步骤)后 resume 跑完全程 → DONE。"""
    from agent.workspace import Workspace

    ws = Workspace.create("run-r2", MOCK_TASK, root=tmp_path)
    bp = Blueprint(meta={"task": MOCK_TASK})
    bp.add_step(Step("s1", "读题", "拿到文本"))
    bp.add_step(Step("s2", "提交", "平台判定", ["s1"]))
    bp.set_status("s1", StepStatus.PASSED, force=True)
    ws.set_blueprint(bp)
    ws.meta["run_status"] = "SCHEDULING"
    ws.sync()

    evaluator = MockEvaluator({
        "evaluator_step": lambda ctx: EvalResult(Verdict.PASS, "s2: 完成"),
        "evaluator_task": lambda ctx: EvalResult(Verdict.DONE, "收尾"),
    })
    # 反思终局修订会调用 planner:返回空补丁收尾
    reflect_planner = ScriptedPlanner(['{}'])
    engine = Engine.resume("run-r2", reflect_planner,
                            MockExecutor(observation="完成"), evaluator,
                            root=tmp_path)
    assert engine.scheduler.state == EngineState.DONE
    assert engine.bp.steps["s2"].status.value == "PASSED"


def test_resume_from_planning_with_bp_skips_to_review(tmp_path):
    """run_status=PLANNING + 已有 bp → 跳过初始规划,直接进入 PLAN_REVIEW。"""
    from agent.workspace import Workspace

    ws = Workspace.create("run-r3", MOCK_TASK, root=tmp_path)
    bp = Blueprint(meta={"task": MOCK_TASK})
    bp.add_step(Step("s1", "扫描", "flag"))
    ws.set_blueprint(bp)
    ws.sync()  # run_status 仍为 PLANNING

    calls = []
    evaluator = MockEvaluator({
        "evaluator_plan": lambda ctx: (
            calls.append("ep"), EvalResult(Verdict.PASS, "ok"))[1],
        "evaluator_step": lambda ctx: (
            calls.append("ee"), EvalResult(Verdict.PASS, "s1: 完成"))[1],
        "evaluator_task": lambda ctx: (
            calls.append("et"), EvalResult(Verdict.DONE, "收尾"))[1],
    })
    # 反思终局修订会调用 planner:返回空补丁
    reflect_planner = ScriptedPlanner(['{}'])
    engine = Engine.resume("run-r3", reflect_planner,
                            MockExecutor(observation="完成"), evaluator,
                            root=tmp_path)
    assert engine.scheduler.state == EngineState.DONE
    # ep 被调用(评审),planner 未被用于初始规划(只用于反思收尾)
    assert "ep" in calls


def test_resume_from_planning_without_bp_does_initial_plan(tmp_path):
    """run_status=PLANNING + 无 bp → 执行初始规划后继续。"""
    from agent.workspace import Workspace

    ws = Workspace.create("run-r4", MOCK_TASK, root=tmp_path)
    # 不设 bp,保持 PLANNING
    ws.sync()

    calls = []
    evaluator = MockEvaluator({
        "evaluator_plan": lambda ctx: EvalResult(Verdict.PASS, "ok"),
        "evaluator_step": lambda ctx: EvalResult(Verdict.PASS, "s1: 完成"),
        "evaluator_task": lambda ctx: EvalResult(Verdict.DONE, "收尾"),
    })

    class CapturePlanner:
        def plan(self, pin):
            calls.append(pin.mode)
            if pin.mode == PlannerMode.INITIAL:
                bp = Blueprint(meta={"task": MOCK_TASK})
                bp.add_step(Step("s1", "扫描", "flag"))
                return bp
            # REVISE: return existing DAG unchanged (reflect→done)
            return Blueprint.from_dict(pin.feedback.dag)

    engine = Engine.resume("run-r4", CapturePlanner(),
                            MockExecutor(observation="完成"), evaluator,
                            root=tmp_path)
    assert engine.scheduler.state == EngineState.DONE
    assert calls[0] == PlannerMode.INITIAL  # 首个调用是初始规划


def test_resume_with_is_completed_rebuilds_task_completed(tmp_path):
    """step_record 带 is_completed=True → task_completed 正确重建。"""
    from agent.workspace import Workspace

    ws = Workspace.create("run-r5", MOCK_TASK, root=tmp_path)
    bp = Blueprint(meta={"task": MOCK_TASK})
    bp.add_step(Step("s1", "扫描", "flag"))
    bp.set_status("s1", StepStatus.PASSED, force=True)
    ws.set_blueprint(bp)
    ws.record_step("s1", "pass", "完成", is_completed=True)
    ws.meta["run_status"] = "DONE"
    ws.sync()

    engine = Engine.resume("run-r5", _NoopPlanner(),
                            MockExecutor(observation="x"),
                            MockEvaluator({}), root=tmp_path)
    assert engine.task_completed is True


def test_resume_rebuilds_turn_from_events(tmp_path):
    """最后 REPLAN 之后的非 PASS 意见事件正确重建 self.turn。"""
    from agent.workspace import Workspace

    ws = Workspace.create("run-r6", MOCK_TASK, root=tmp_path)
    bp = Blueprint(meta={"task": MOCK_TASK})
    bp.add_step(Step("s1", "扫描", "flag"))
    bp.set_status("s1", StepStatus.PASSED, force=True)
    ws.set_blueprint(bp)
    # 模拟:replan → step_eval(escalate) → plan_review(fail)
    ws.add_event("planner", "replan", reason="r1", source="", changes="")
    ws.add_event("evaluator_step", "step_eval", step_id="s1",
                 verdict="escalate", opinion="s1 升级", observation="timeout")
    ws.add_event("evaluator_plan", "plan_review",
                 verdict="fail", opinion="计划仍需修订")
    ws.meta["run_status"] = "DONE"
    ws.sync()

    engine = Engine.resume("run-r6", _NoopPlanner(),
                            MockExecutor(observation="x"),
                            MockEvaluator({}), root=tmp_path)
    assert len(engine.turn) == 2
    assert engine.turn[0].source == EvalSource.STEP_EVAL
    assert engine.turn[0].opinion == "s1 升级"
    assert engine.turn[0].observation == "timeout"
    assert engine.turn[1].source == EvalSource.PLAN_REVIEW
    assert engine.turn[1].opinion == "计划仍需修订"


def test_resume_after_kill_rebuilds_terminal_state_from_events(tmp_path):
    """事件溯源确定性:清空 state.json 的 blueprint/steps(kill 崩溃窗口)后 resume,
    终态从事件流重建,与快照完整的对照 run 完全一致(DAG/步骤状态/计数器)。"""
    import json

    from agent.workspace import Workspace

    ws = Workspace.create("run-rk", MOCK_TASK, root=tmp_path)
    responses = [
        '{"add":[{"id":"s1","instruction":"扫描","criterion":"flag","depends_on":[]}],"reason":"i"}',
        '{}',   # 反思终局修订:空补丁收尾
    ]
    state = {"i": 0}

    def llm(**kw):
        r = responses[min(state["i"], len(responses) - 1)]
        state["i"] += 1
        return r

    planner = Planner(llm_call=llm, workspace=ws)
    evaluator = MockEvaluator({
        "evaluator_plan": lambda ctx: EvalResult(Verdict.PASS, "ok"),
        "evaluator_step": lambda ctx: EvalResult(Verdict.PASS, "通过"),
        "evaluator_task": lambda ctx: EvalResult(Verdict.DONE, "收尾"),
    })
    engine = Engine(planner, MockExecutor(observation="完成"), evaluator, workspace=ws)
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE

    ref = Engine.resume("run-rk", _NoopPlanner(),
                        MockExecutor(observation="x"),
                        MockEvaluator({}), root=tmp_path)

    # 模拟 kill:state.json 的 blueprint/steps 丢失,事件流保留
    st_path = ws.root / "state.json"
    st = json.loads(st_path.read_text(encoding="utf-8"))
    st["blueprint"] = None
    st["steps"] = {}
    st_path.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")

    engine2 = Engine.resume("run-rk", _NoopPlanner(),
                            MockExecutor(observation="x"),
                            MockEvaluator({}), root=tmp_path)
    assert engine2.scheduler.state == EngineState.DONE
    assert engine2.bp is not None
    assert engine2.bp.to_dict() == ref.bp.to_dict()          # DAG 从 REPLAN 快照重建
    assert engine2.bp.steps["s1"].status.value == "PASSED"   # 状态从 step_record.status 叠加
    assert engine2._goal_complete == ref._goal_complete
    assert engine2._run_tokens == ref._run_tokens            # 用量从 llm_usage 事件重建
    assert engine2.submitted_flag == ref.submitted_flag
    assert engine2.run_result is not None


# ===== 13. Planner LLM 调用异常保护 (P2-9) =====

class _RaisePlanner:
    """planner.plan() 直接抛异常,模拟 LLM 调用失败。"""
    def plan(self, pin):
        raise RuntimeError("LLM timeout")


def test_planner_initial_failure_causes_failed():
    """初始规划 LLM 异常 → FAILED + fail_reason,不崩进程。"""
    engine = Engine(_RaisePlanner(),
                    MockExecutor(observation="完成"),
                    MockEvaluator({}), workspace=MockWorkspace())
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.FAILED
    assert "LLM timeout" in engine.fail_reason
    assert "Planner LLM" in engine.fail_reason


def test_planner_replan_failure_causes_failed():
    """重规划 LLM 异常 → FAILED + fail_reason,不崩进程。"""
    engine, _ = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]}]',
            '{}',
        ),
        ep=[EvalResult(Verdict.PASS, "计划可执行")],
        ee=[EvalResult(Verdict.ESCALATE, "s1: 失败")],
        et=[],
        executor=MockExecutor(observation="执行完成"),
    )
    # 替换 planner:第一次调用返回正常(初始规划),之后抛异常(重规划)
    call_count = [0]

    class _RaiseOnReplan:
        def plan(self, pin):
            call_count[0] += 1
            if call_count[0] == 1:
                bp = Blueprint(meta={"task": MOCK_TASK})
                bp.add_step(Step("s1", "读题", "拿到文本"))
                return bp
            raise RuntimeError("LLM replan timeout")

    engine.planner = _RaiseOnReplan()
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.FAILED
    assert "LLM replan timeout" in engine.fail_reason
    assert "Planner LLM" in engine.fail_reason


# ===== 环境阻塞收口:容器题靶机不可达 → FAILED,不再空转后续步骤 =====

def test_env_blocked_container_fails_run():
    """开靶失败(如平台 429)→ s1 过后 SCHEDULING 即收口 FAILED,不再跑依赖目标的 s2。"""

    class _FailTargetAdapter:
        def start_target(self, challenge_id):
            raise RuntimeError("开靶机失败 HTTP 429: RATE_LIMIT_EXCEEDED")

    def _outcome():
        from agent.runner import RunOutcome
        return RunOutcome(ok=True, returncode=0, stdout="ok", stderr="",
                          cmd=[], target="ssh")

    class _FakeRunner:
        def run(self, *a, **k):
            return _outcome()

        def run_python(self, *a, **k):
            return _outcome()

    def llm(*, system, prompt, tools, tool_exec, **kw):
        return ToolResult(
            content="完成",
            trace=[{"name": "answer", "arguments": '{"text": "无目标"}',
                    "result": {"answer": "无目标"}}],
            rounds=1, total_usage={"prompt_tokens": 1, "completion_tokens": 1,
                                   "total_tokens": 2})

    task = dict(MOCK_TASK)
    task.update({"challenge_id": "c-env", "has_container": 1})
    task.pop("target", None)
    ws = MockWorkspace()
    executor = RealExecutor(llm_fn=llm, runner=_FakeRunner(), workspace=ws,
                            adapter=_FailTargetAdapter())
    planner = ScriptedPlanner(_plan_responses(
        '[{"id":"s1","instruction":"启动靶机","criterion":"拿到URL","depends_on":[]},'
        '{"id":"s2","instruction":"侦察","criterion":"清单","depends_on":["s1"]}]',
        "{}",
    ))
    evaluator = MockEvaluator(
        {"evaluator_plan": seq([EvalResult(Verdict.PASS, "计划可执行")]),
         "evaluator_step": seq([EvalResult(Verdict.PASS, "s1: 完成")]),
         "evaluator_task": seq([EvalResult(Verdict.DONE, "反思: 无问题")])},
    )
    engine = Engine(planner, executor, evaluator, workspace=ws)
    engine.run(task)
    assert engine.scheduler.state == EngineState.FAILED
    assert "环境阻塞" in (engine.fail_reason or "")


def test_capability_blocked_sandbox_fails_run():
    """沙箱不可用(构造失败,probe 亮起)→ 首步派发前即收口 FAILED,不再空转后续步骤。"""

    def _outcome():
        from agent.runner import RunOutcome
        return RunOutcome(ok=True, returncode=0, stdout="ok", stderr="",
                          cmd=[], target="ssh")

    class _BlockedSandboxRunner:
        def __init__(self):
            self.sandbox = None
            self._sandbox_failed_at = 1.0  # 模拟曾尝试构造且失败

        def run(self, *a, **k):
            return _outcome()

        def run_python(self, *a, **k):
            return _outcome()

        def sandbox_blocked(self):
            return self.sandbox is None and self._sandbox_failed_at is not None

        def _ensure_sandbox(self):
            return None

    def llm(*, system, prompt, tools, tool_exec, **kw):
        return ToolResult(
            content="完成",
            trace=[{"name": "answer", "arguments": '{"text": "无沙箱"}',
                    "result": {"answer": "无沙箱"}}],
            rounds=1, total_usage={"prompt_tokens": 1, "completion_tokens": 1,
                                   "total_tokens": 2})

    task = dict(MOCK_TASK)
    task.pop("has_container", None)
    task.pop("target", None)
    ws = MockWorkspace()
    executor = RealExecutor(llm_fn=llm, runner=_BlockedSandboxRunner(), workspace=ws)
    planner = ScriptedPlanner(_plan_responses(
        '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]},'
        '{"id":"s2","instruction":"编码","criterion":"可逆","depends_on":["s1"]}]',
        "{}",
    ))
    evaluator = MockEvaluator(
        {"evaluator_plan": seq([EvalResult(Verdict.PASS, "计划可执行")]),
         "evaluator_step": seq([EvalResult(Verdict.PASS, "s1: 完成")]),
         "evaluator_task": seq([EvalResult(Verdict.DONE, "反思: 无问题")])},
    )
    engine = Engine(planner, executor, evaluator, workspace=ws)
    engine.run(task)
    assert engine.scheduler.state == EngineState.FAILED
    assert "环境阻塞" in (engine.fail_reason or "")


# ===== ee 三分类诊断路由 =====

def _capture_executor(ctxs):
    """记录每次 executor ctx,并带一条工具调用(喂 trace 通道,供重试档位断言)。"""
    def run(step, ctx, tool_exec=None):
        ctxs.append(ctx)
        return ExecResult(observation="执行完成",
                          tool_calls=[{"tool": "cmd", "args": {"cmd": "echo probe"}}])
    return run


def test_retry_drift_uses_compressed_ctx():
    """drift 重试:继承压缩 ctx(trace 摘要档,无 compress → 索引)。"""
    ctxs = []
    engine, _ = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"访问目标","criterion":"拿到入口","depends_on":[]}]',
            "{}",
        ),
        ep=[EvalResult(Verdict.PASS, "计划可执行")],
        ee=[EvalResult(Verdict.RETRY, "s1: 方向偏了", diagnosis=Diagnosis.DRIFT),
            EvalResult(Verdict.PASS, "s1: 完成")],
        et=[EvalResult(Verdict.DONE, "反思: 无问题")],
        executor=MockExecutor(fn=_capture_executor(ctxs)),
    )
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    assert len(ctxs) == 2
    assert "本轮工具轨迹" in ctxs[1]
    assert "本轮工具轨迹(索引)" in ctxs[1]   # drift 重试 trace 被压缩(无 compress → 索引档)


def test_retry_incomplete_keeps_raw_ctx():
    """incomplete 重试:继承前 8 轮原始 ctx,不压缩。"""
    ctxs = []
    engine, _ = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"访问目标","criterion":"拿到入口","depends_on":[]}]',
            "{}",
        ),
        ep=[EvalResult(Verdict.PASS, "计划可执行")],
        ee=[EvalResult(Verdict.RETRY, "s1: 8 轮未达成", diagnosis=Diagnosis.INCOMPLETE),
            EvalResult(Verdict.PASS, "s1: 完成")],
        et=[EvalResult(Verdict.DONE, "反思: 无问题")],
        executor=MockExecutor(fn=_capture_executor(ctxs)),
    )
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    assert len(ctxs) == 2
    assert "本轮工具轨迹" in ctxs[1]
    # incomplete 重试保留原始轨迹:trace 不压缩(history 固定 index 档是共享进度预算,与 retry_mode 无关)
    assert "本轮工具轨迹(索引)" not in ctxs[1]


def test_retry_planner_target_routes_to_single_node_replan():
    """RETRY + planner_target:不等重试耗尽,直接升级并触发单节点重设计(scope_step_id)。"""
    engine, planner = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"访问目标","criterion":"拿到入口","depends_on":[]}]',
            '{"update":[{"id":"s1","instruction":"改目标","criterion":"新标准"}],"reason":"redesign"}',
            "{}",
        ),
        ep=[EvalResult(Verdict.PASS, "计划可执行")],
        ee=[EvalResult(Verdict.RETRY, "s1: 目标本身有误", diagnosis=Diagnosis.PLANNER_TARGET),
            EvalResult(Verdict.PASS, "s1: 完成")],
        et=[EvalResult(Verdict.DONE, "反思: 无问题")],
        executor=MockExecutor(observation="执行完成"),
    )
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    replan = planner.calls[1]
    assert replan.feedback.scope_step_id == "s1"
    assert replan.feedback.state_context.trigger == "step_target_redesign"
    assert replan.feedback.turn[0].diagnosis == "planner_target"


def test_step_eval_escalate_planner_target_scope():
    """ESCALATE + planner_target:同样走单节点重设计。"""
    engine, planner = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"访问目标","criterion":"拿到入口","depends_on":[]}]',
            '{"update":[{"id":"s1","instruction":"改目标","criterion":"新标准"}],"reason":"redesign"}',
            "{}",
        ),
        ep=[EvalResult(Verdict.PASS, "计划可执行")],
        ee=[EvalResult(Verdict.ESCALATE, "s1: 目标有误", diagnosis=Diagnosis.PLANNER_TARGET),
            EvalResult(Verdict.PASS, "s1: 完成")],
        et=[EvalResult(Verdict.DONE, "反思: 无问题")],
        executor=MockExecutor(observation="执行完成"),
    )
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    replan = planner.calls[1]
    assert replan.feedback.scope_step_id == "s1"
    assert replan.feedback.state_context.trigger == "step_target_redesign"


def test_step_eval_escalate_other_no_scope():
    """ESCALATE + 非 planner_target:维持全 DAG 重规划,不设 scope。"""
    engine, planner = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"访问目标","criterion":"拿到入口","depends_on":[]},'
            '{"id":"s2","instruction":"提交","criterion":"平台判定","depends_on":["s1"]}]',
            '{"remove":["s1"],"update":[{"id":"s2","depends_on":[]}],"reason":"revise"}',
            "{}",
        ),
        ep=[EvalResult(Verdict.PASS, "计划可执行")],
        ee=[EvalResult(Verdict.ESCALATE, "s1: 目标不可达", observation="timeout"),
            EvalResult(Verdict.PASS, "s2: 完成")],
        et=[EvalResult(Verdict.DONE, "反思: 无问题")],
        executor=MockExecutor(observation="执行完成"),
    )
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    replan = planner.calls[1]
    assert replan.feedback.scope_step_id is None
    assert replan.feedback.state_context.trigger == "step_escalated"


# ===== goal 评估:逐 goal LLM 软鉴定 + _llm_wrap 记账 =====

def test_goal_eval_llm_soft_adjudication_accounted(monkeypatch):
    """步骤 PASS 后 goal 评估走真实 StepLLMEvaluator(逐 goal LLM 并行)+ _llm_wrap 记账:
    _goal_complete 填充、GOAL_EVAL 事件落账、goal 评估 token 用量计入 _run_tokens。"""
    import json

    from agent import llm_api as ev_llm
    from agent.evaluator import GOAL_EVAL_SYSTEM, ConfigurableEvaluator, StepLLMEvaluator
    from agent.schema import Goal, Role

    class _Understander:
        def understand(self, raw_content):
            return TaskInput(raw_content=raw_content, goal_list=[Goal(id="g1")])

    goal_calls = {"n": 0}

    def _chat(system=None, prompt=None, model=None, **kw):
        if system == GOAL_EVAL_SYSTEM:
            goal_calls["n"] += 1
            return json.dumps({"complete": True, "evidence": ["s1"], "reasoning": "入口达成"})
        return json.dumps({"verdict": "pass", "opinion": "ok", "is_completed": False})

    monkeypatch.setattr(ev_llm, "chat", _chat)
    monkeypatch.setattr(ev_llm, "role_model", lambda role=None: "stub-model")
    monkeypatch.setattr(ev_llm, "pop_token_log",
                        lambda: [{"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}])

    evaluator = ConfigurableEvaluator({
        Role.EVALUATOR_PLAN: MockEvaluator(
            {Role.EVALUATOR_PLAN: EvalResult(Verdict.PASS, "计划可执行")}),
        Role.EVALUATOR_STEP: StepLLMEvaluator(),
        Role.EVALUATOR_TASK: MockEvaluator(
            {Role.EVALUATOR_TASK: EvalResult(Verdict.DONE, "反思完成")}),
    })
    engine = Engine(
        ScriptedPlanner(_plan_responses(
            '[{"id":"s1","instruction":"访问","criterion":"入口","depends_on":[]}]',
            "{}")),
        MockExecutor(observation="执行完成"), evaluator,
        workspace=MockWorkspace(), understander=_Understander(),
    )
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    # 逐 goal LLM 软鉴定恰好 1 次(单 goal)
    assert goal_calls["n"] == 1
    # 软鉴定结果落账:_goal_complete + GOAL_EVAL 事件
    assert engine._goal_complete.get("g1") == ["s1"]
    goal_events = [e for e in engine.workspace.events if e.kind == EventKind.GOAL_EVAL]
    assert any(getattr(e.detail, "goal_id", None) == "g1"
               and getattr(e.detail, "complete", False) for e in goal_events)
    # goal 评估经 _llm_wrap:token 用量计入 _run_tokens(仅 step_eval 记账不足以到该下界)
    assert engine._run_tokens >= 45


# ===== 早停收口:goal 全完成 + 已提交 flag → task_completed =====

def test_goal_complete_with_submitted_flag_early_stops():
    """CTF 核心目标=拿到 flag:goal 全部判完成且已提交 flag → 调度器早停,
    跳过剩余冗余 DAG 步骤直接反思终局(治 web4_0826 赢后还烧 75% token 的浪费)。"""
    from agent.schema import Goal

    class _Understander:
        def understand(self, raw_content):
            return TaskInput(raw_content=raw_content,
                             goal_list=[Goal(id="g1"), Goal(id="g2")])

    engine, _ = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"下载附件","criterion":"拿到文件","depends_on":[]},'
            '{"id":"s2","instruction":"分析找flag","criterion":"提取flag","depends_on":["s1"]}]',
            '{}',
        ),
        ep=[EvalResult(Verdict.PASS, "计划可执行")],
        ee=[EvalResult(Verdict.PASS, "s1: 完成"), EvalResult(Verdict.PASS, "s2: 完成")],
        et=[EvalResult(Verdict.DONE, "反思: 目标达成")],
        executor=MockExecutor(result={"flag": "CTF2{abc}"}),
        # s1 PASS 后一次 goal 评估即判 g1+g2 全完成
        goal_responses=lambda ctx, goals, dag: [
            GoalEvalDetail(goal_id="g1", complete=True, evidence=["s1"], reasoning="拿到附件"),
            GoalEvalDetail(goal_id="g2", complete=True, evidence=["s1"], reasoning="提交 flag"),
        ],
        understander=_Understander(),
    )
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    assert engine.task_completed is True
    # s2 未执行:s2 PASS 从未发生(早停在 SCHEDULING 前截住)
    assert engine.bp.steps["s2"].status.value != "PASSED"
    assert engine.submitted_flag == "CTF2{abc}"


def test_goal_complete_without_submitted_flag_does_not_early_stop():
    """未提交 flag(如纯 web 探测类任务):goal 全完成不置 task_completed,
    不误停,步骤照常全部走完。"""
    from agent.schema import Goal

    class _Understander:
        def understand(self, raw_content):
            return TaskInput(raw_content=raw_content,
                             goal_list=[Goal(id="g1"), Goal(id="g2")])

    it = iter([
        [GoalEvalDetail(goal_id="g1", complete=True, evidence=["s1"], reasoning="下载完成")],
        [GoalEvalDetail(goal_id="g2", complete=True, evidence=["s2"], reasoning="分析完成")],
    ])
    engine, _ = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"下载附件","criterion":"拿到文件","depends_on":[]},'
            '{"id":"s2","instruction":"分析","criterion":"完成","depends_on":["s1"]}]',
            '{}',
        ),
        ep=[EvalResult(Verdict.PASS, "计划可执行")],
        ee=[EvalResult(Verdict.PASS, "s1: 完成"), EvalResult(Verdict.PASS, "s2: 完成")],
        et=[EvalResult(Verdict.DONE, "反思: 目标达成")],
        executor=MockExecutor(observation="执行完成"),
        goal_responses=lambda ctx, goals, dag: next(it),
        understander=_Understander(),
    )
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    assert engine.task_completed is False
    assert engine.submitted_flag is None
    assert engine.bp.steps["s1"].status.value == "PASSED"
    assert engine.bp.steps["s2"].status.value == "PASSED"
