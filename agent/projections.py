"""CQRS 读模型:事件流(events.jsonl)的增量折叠投影。

事件流是唯一事实源(写入全走 Workspace.add_event);Projection 是 append 时同步
折叠的读模型缓存,load/resume 时全量重放重建——ctx 渲染 / resume / 计数器恢复
全部 O(1) 读,不再线性扫事件流。

live path 引擎直接持有物化 blueprint(ws.blueprint,可变聚合)与自身运行时计数器
(self.turn / self.replans 等);投影的对应字段主要服务 resume,从事件流一次性
取回全部运行态。增量折叠规则与旧 _rebuild_from_events / _rebuild_turn 等价
(stalls 以 ReplanDetail.changes=="无改动" 判定;turn 在 replan 边界推进
turn_consumed)。

## step 实例版本(重建侧 + 运行侧契约)

step 实例身份不显式打标,由 REPLAN 谱系派生:step 被 remove 后重加 = 新实例
(隐式版本),replay 的状态叠加按出生下标过滤,旧实例的 step_record 不污染新实例。
**运行侧前置契约**:并行下 replan 若重建正在 RUNNING 的 step 实例,必须先取消
旧实例(token + SKIP + 抑制其 step_record),否则同 step_id 双实例并存——这是
中断机制(step_cancel)的硬前提,重建侧(本模块)与运行侧由它衔接。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent.blueprint import Blueprint, DONE_STATUSES, StepStatus
from agent.schema import (
    EvalEvent, EvalSource, EventKind, Role, StepResult,
)

# 意见类事件(进入 turn 投影;与 engine._rebuild_turn 的 opinion_kinds 对齐)
_OPINION_KINDS = {
    EventKind.PLAN_REVIEW, EventKind.STEP_EVAL,
    EventKind.REFLECT, EventKind.SCHEDULING, EventKind.GOAL_EVAL,
}
# history 投影源(与 HistoryComponent._events 对齐)
_HISTORY_KINDS = {EventKind.STEP_RECORD, EventKind.REPLAN}
# 连续无改动即 stall(与 engine._patch_summary 的 "无改动" 哨兵一致)
_NO_CHANGE = "无改动"


@dataclass
class Projection:
    """事件流折叠出的读模型(append 增量维护;replay 全量重建)。"""

    steps: dict[str, StepResult] = field(default_factory=dict)
    blueprint: Blueprint | None = None          # 仅 replay 重建(最后 REPLAN 的 dag 快照)
    submission: dict | None = None              # {flag, ok, correct, message}
    submitted_flag: str | None = None
    goal_complete: dict = field(default_factory=dict)  # goal_id → evidence 列表
    task_completed: bool = False
    replans: int = 0
    stalls: int = 0                              # 连续无改动重规划次数
    turn: list[EvalEvent] = field(default_factory=list)   # 全量意见(跨 replan 累积)
    turn_consumed: int = 0                       # 最后 replan 边界(下次喂 planner 的起点)
    run_tokens: int = 0                          # per-run token 累计(llm_usage 投影)
    last_replan_idx: int = -1                    # 最后 replan 事件在事件流的下标
    history_events: list = field(default_factory=list)     # list[Event]: step_record + replan
    goal_ids: list[str] = field(default_factory=list)      # run 级目标 id(replay 时注入)

    def _mark_task_completed(self):
        """ee 判定任务已完成:全部目标置"有证据的完成"(空证据列表),与 _rebuild_from_events 对齐。"""
        self.task_completed = True
        for gid in self.goal_ids:
            self.goal_complete.setdefault(gid, [])


def _submission_fold(prev: dict | None, ev) -> tuple[dict, str | None]:
    """提交判定折叠:权威 correct 优先——后来的 correct=None(如重提 ALREADY_SOLVED)
    不覆盖已确认的 True/False。与旧 workspace.record_submission 规则共享同一语义。"""
    d = ev.detail
    info = {
        "flag": str(getattr(d, "flag", "") or ""),
        "ok": getattr(d, "ok", None),
        "correct": getattr(d, "correct", None),
        "message": getattr(d, "message", None),
    }
    if info["correct"] is None and prev is not None and prev.get("correct") is not None:
        return prev, (prev.get("flag") or None)
    return info, (info["flag"] or None)


def _ev_to_eval(ev) -> EvalEvent:
    """事件 → EvalEvent(意见投影;GOAL_EVAL 以 reasoning 作意见,与 _rebuild_turn 对齐)。"""
    d = ev.detail
    if ev.kind == EventKind.GOAL_EVAL:
        opinion = getattr(d, "reasoning", "") or ""
        observation = (f"goal_id={getattr(d, 'goal_id', '')} "
                       f"complete={bool(getattr(d, 'complete', False))}")
    else:
        opinion = getattr(d, "opinion", "") or ""
        observation = getattr(d, "observation", None)
    return EvalEvent(source=EvalSource(ev.kind), opinion=opinion,
                     observation=observation, step_id=ev.step_id)


def apply(proj: Projection, ev, idx: int) -> None:
    """单条事件的增量折叠。Workspace.add_event 每 append 一条调一次;replay 全量调。"""
    kind = ev.kind
    detail = ev.detail
    if kind == EventKind.REPLAN:
        proj.replans += 1
        proj.last_replan_idx = idx
        proj.turn_consumed = len(proj.turn)
        if getattr(detail, "changes", None) == _NO_CHANGE:
            proj.stalls += 1
        else:
            proj.stalls = 0
    elif kind == EventKind.STEP_RECORD:
        is_completed = bool(getattr(detail, "is_completed", False))
        proj.steps[ev.step_id] = StepResult(
            step_id=ev.step_id,
            verdict=ev.verdict or "",
            observation=getattr(detail, "observation", "") or "",
            result=getattr(detail, "result", None) or {},
            attempts=int(getattr(detail, "attempts", 0) or 0),
            is_completed=is_completed,
        )
        if is_completed:
            proj._mark_task_completed()
    elif kind == EventKind.GOAL_EVAL:
        if getattr(detail, "complete", False):
            proj.goal_complete[detail.goal_id] = list(getattr(detail, "evidence", None) or [])
    elif kind == EventKind.SUBMISSION:
        proj.submission, proj.submitted_flag = _submission_fold(proj.submission, ev)
    elif kind == EventKind.LLM_USAGE:
        proj.run_tokens += int(getattr(detail, "total_tokens", 0) or 0)

    if kind in _HISTORY_KINDS and ev.agent != Role.SYSTEM:
        proj.history_events.append(ev)
    if kind in _OPINION_KINDS:
        proj.turn.append(_ev_to_eval(ev))


def _derive_birth_index(events) -> dict[str, int]:
    """每个 step_id 在当前 DAG 谱系的出生下标 = 最后一次「重新出现」的 replan 下标。

    step 实例身份不显式打标:remove 后重加 = 新实例(隐式版本),旧实例的
    step_record 不得叠加到新实例。实例只由 DAG 结构变更(replan)诞生,故谱系
    级联差即完整版本判别。运行侧(并行下 replan 重建 RUNNING 实例必须先取消
    旧实例)属中断契约,本函数只负责重建侧。
    """
    birth: dict[str, int] = {}
    present: set[str] = set()
    for idx, e in enumerate(events):
        if e.kind != EventKind.REPLAN:
            continue
        dag = getattr(e.detail, "dag", None)
        if not dag:
            continue
        S = set((dag.get("steps") or {}).keys())
        for sid in S - present:          # 谱系里新出现 → 出生于此
            birth[sid] = idx
        present = S
    return birth


def replay(events, goal_ids=None) -> Projection:
    """全量重放事件流 → 投影。blueprint 从最后一条 REPLAN 事件的 dag 快照重建
    (无 REPLAN 或快照缺失 → None,由调用方回退 state.json)。"""
    proj = Projection(goal_ids=list(goal_ids or []))
    for idx, e in enumerate(events):
        apply(proj, e, idx)
    for e in reversed(events):
        if e.kind == EventKind.REPLAN and getattr(e.detail, "dag", None):
            proj.blueprint = Blueprint.from_dict(e.detail.dag)
            break
    # REPLAN 快照是规划时刻的 DAG,执行态(步骤状态)可能落后;以 step_record 事件的
    # status 叠加各步最终 DAG 状态(最后一次胜出)——事件源合一,resume 拿到真实终态。
    # 双守卫(版本正确性):
    #   ① birth 判别——被 replan remove+重加 = 新实例,旧实例的 step_record 不叠;
    #   ② 快照终态守卫——快照已是 PASSED/SKIPPED(取消/已执行)= 最新意图,旧记录
    #      不叠(同时避免 SKIPPED 撞旧记录触发终态保护抛 DAGError)。
    if proj.blueprint is not None:
        birth = _derive_birth_index(events)
        for idx, e in enumerate(events):
            if e.kind != EventKind.STEP_RECORD or e.step_id not in proj.blueprint.steps:
                continue
            if birth.get(e.step_id, -1) > idx:
                continue
            if proj.blueprint.steps[e.step_id].status in DONE_STATUSES:
                continue
            st = getattr(e.detail, "status", None)
            if st:
                proj.blueprint.set_status(e.step_id, StepStatus(st), force=True)
    return proj
