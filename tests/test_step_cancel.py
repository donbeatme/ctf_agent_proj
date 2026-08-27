"""运行侧中断原语(step_cancel)测试:request_stop 硬中断执行中的长步骤 + 事件 typed 落账。

覆盖:
- request_stop 顺带中断当前执行步骤:不再等沙箱跑完,落 SKIPPED + step_cancel 事件,
  被取消步骤无 step_record(运行时纪律抑制),随后转 FAILED。
- cancel_current 原语(不经 request_stop):SKIPPED + 事件落账,取消后收敛终态。
- step_cancel 事件真实持久化 → load 重放:typed 归一化(非退化 dict) + 迟到记录被投影抑制。

取消由慢执行器在 run 内部 0.2s 后经 loop.call_later 触发,消除"timer 早于 EXECUTING/loop
就绪"的外部线程竞态;call_later 与外部线程 call_soon_threadsafe 走同一 loop 线程路径。
"""

import asyncio
import time

from agent.blueprint import Blueprint, Step
from agent.evaluator import EvalResult, Verdict
from agent.engine import EngineState
from agent.executor import ExecResult, MockExecutor
from agent.schema import EventKind, Role
from agent.workspace import Workspace
from tests.mock_data import MOCK_TASK
from tests.test_engine import _plan_responses, make_engine


class _SlowBase(MockExecutor):
    """run 后 delay 秒触发引擎回调(经 loop 线程),再睡 seconds 秒。"""

    def __init__(self, seconds=5.0, delay=0.2):
        super().__init__()
        self.seconds = seconds
        self.delay = delay
        self.engine = None

    def _trigger(self):
        raise NotImplementedError

    async def run(self, step, ctx, tool_exec=None):
        loop = asyncio.get_running_loop()
        loop.call_later(self.delay, self._trigger)
        await asyncio.sleep(self.seconds)
        return ExecResult(observation="慢执行完成")


class StopTriggered(_SlowBase):
    def _trigger(self):
        self.engine.request_stop("用户停止")


class CancelTriggered(_SlowBase):
    def _trigger(self):
        self.engine.cancel_current("replan rebuild")


def test_request_stop_aborts_long_step():
    """request_stop 接线中断:长步骤被打断 → SKIPPED + step_cancel 事件 + 无 step_record + FAILED。"""
    slow = StopTriggered(seconds=5.0)
    engine, _ = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]}]',
            "{}",
        ),
        ep=[EvalResult(Verdict.PASS, "计划可执行")],
        ee=[EvalResult(Verdict.PASS, "s1: 完成")],
        et=[EvalResult(Verdict.DONE, "反思: 无问题")],
        executor=slow,
    )
    slow.engine = engine
    t0 = time.perf_counter()
    engine.run(MOCK_TASK)
    elapsed = time.perf_counter() - t0

    assert elapsed < 4.0                            # 未等 5s 慢执行跑完 → 已中断
    assert engine.scheduler.state == EngineState.FAILED
    assert engine.fail_reason == "用户停止"
    assert engine.bp.steps["s1"].status.value == "SKIPPED"
    cancels = [e for e in engine.workspace.events if e.kind == EventKind.STEP_CANCEL]
    assert cancels and cancels[0].step_id == "s1"
    assert cancels[0].detail.reason == "用户停止"
    # 被取消步骤无 step_record(抑制,而非留一条 RETRY/ESCALATE)
    records = [e for e in engine.workspace.events
               if e.kind == EventKind.STEP_RECORD and e.step_id == "s1"]
    assert not records


def test_cancel_current_primitive_marks_skipped():
    """cancel_current 原语(不经 request_stop):SKIPPED + 事件落账,current 清空,收敛 DONE。"""
    cancel = CancelTriggered(seconds=5.0)
    engine, _ = make_engine(
        _plan_responses(
            '[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]}]',
            "{}",
        ),
        ep=[EvalResult(Verdict.PASS, "计划可执行")],
        ee=[EvalResult(Verdict.PASS, "s1: 完成")],
        et=[EvalResult(Verdict.DONE, "反思: 无问题")],
        executor=cancel,
    )
    cancel.engine = engine
    engine.run(MOCK_TASK)

    assert engine.bp.steps["s1"].status.value == "SKIPPED"
    cancels = [e for e in engine.workspace.events if e.kind == EventKind.STEP_CANCEL]
    assert cancels and cancels[0].detail.reason == "replan rebuild"
    assert engine.current is None
    # 未接 stop → 取消后回 SCHEDULING,SKIPPED 步骤不再调度 → 全终态 → 反思收口 DONE
    assert engine.scheduler.state == EngineState.DONE
    records = [e for e in engine.workspace.events
               if e.kind == EventKind.STEP_RECORD and e.step_id == "s1"]
    assert not records


def test_step_cancel_typed_roundtrip(tmp_path):
    """step_cancel 事件持久化 → load 重放:typed 归一化(非退化 dict) + 迟到记录抑制。"""
    ws = Workspace.create("run-cancel", {"q": "x"}, root=tmp_path)
    bp = Blueprint(meta={"task": "t"})
    bp.add_step(Step(id="s1", instruction="做", criterion="可验收"))
    ws.set_blueprint(bp)
    ws.add_event(Role.SYSTEM, EventKind.STEP_CANCEL, step_id="s1", reason="rebuild")
    ws.record_step("s1", "pass", "迟到", status="PASSED")
    ws.sync()

    ws2 = Workspace.load("run-cancel", root=tmp_path)
    cancels = [e for e in ws2.events if e.kind == EventKind.STEP_CANCEL]
    assert cancels and not isinstance(cancels[0].detail, dict)  # typed 通道
    assert cancels[0].detail.reason == "rebuild"
    assert "s1" not in ws2.proj.steps                     # 迟到记录被抑制
    assert ws2.proj.blueprint.steps["s1"].status.value == "PENDING"  # overlay 同步跳过
