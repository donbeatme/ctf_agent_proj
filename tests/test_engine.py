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

import pytest

from agent.blueprint import Blueprint, Step, StepStatus
from agent.evaluator import EvalResult, MockEvaluator, Verdict
from agent.engine import Engine, EngineState
from agent.executor import MockExecutor
from tests.mock_data import MOCK_TASK
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


# ===== 2. ee 打 is_completed:残留未完成节点不触发死锁 =====

def test_is_completed_skips_deadlock():
    engine, _ = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]},'
            '{"id":"s2","instruction":"编码","criterion":"可逆","depends_on":["s1"]},'
            '{"id":"s3","instruction":"提交","criterion":"平台判定","depends_on":["s2"]}]',
            '{"update":[{"id":"s3","criterion":"平台判定通过"}],"reason":"s2 升级后收紧 s3 标准"}',
            '{}',  # 反思
        ),
        ep=[EvalResult(Verdict.PASS, "计划可执行")],
        ee=[
            EvalResult(Verdict.PASS, "s1: 完成", is_completed=True),  # 任务目标已达成
            EvalResult(Verdict.ESCALATE, "s2: 编码结果不符", observation="aGVsbG8="),
        ],
        et=[EvalResult(Verdict.DONE, "反思: 无问题")],
        executor=MockExecutor(observation="执行完成"),
    )
    engine.run(MOCK_TASK)
    # 任务已完成(ee 判定),s2 被升级也不进死锁,直接反思收尾
    assert engine.scheduler.state == EngineState.DONE
    assert engine.task_completed is True
    assert engine.fail_reason is None
    assert engine.bp.steps["s2"].status.value == "ESCALATED"
    # 升级 s2 触发的重规划(STEP_EVAL source)带状态上下文,但不是死锁触发
    assert all(p.feedback and p.feedback.state_context is not None for p in engine.planner.calls[1:])
    assert all(p.feedback.state_context.trigger != "deadlock" for p in engine.planner.calls[1:])


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
    {  # 6 ee 判完成 + 残留未完成 → 不触发死锁
        "id": "is_completed_skip_deadlock",
        "planner": _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]},'
            '{"id":"s2","instruction":"编码","criterion":"可逆","depends_on":["s1"]}]',
            '{}',  # s2 升级后重规划:不动 DAG,保持 s2 为残留未完成
            '{}',
        ),
        "evaluator_plan": [EvalResult(Verdict.PASS, "计划可执行")],
        "evaluator_step": [EvalResult(Verdict.PASS, "s1: 完成", is_completed=True),
               EvalResult(Verdict.ESCALATE, "s2: 编码失败")],
        "evaluator_task": [EvalResult(Verdict.DONE, "反思: 无问题")],
        "state": EngineState.DONE,
        "steps": {"s1": "PASSED", "s2": "ESCALATED"},
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


def test_planner_renders_state_context_into_system():
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
    planner.plan(pin)
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
    ctx, _, _ = ws.assembler.assemble("planner", raw_content=MOCK_TASK)
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
        def run(self, step, ctx):
            seen["executor"] = (step.id, ctx)
            return super().run(step, ctx)

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
    from main import SmokeEvaluator
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
    from main import SmokeEvaluator
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
