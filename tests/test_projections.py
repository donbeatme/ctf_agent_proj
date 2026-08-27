"""事件折叠投影(projections.py)测试:replay 确定性 / 各 kind fold / 提交权威规则。"""

from agent.blueprint import Blueprint, Step, StepStatus
from agent.projections import Projection, apply, replay
from agent.schema import (
    EventKind, GoalEvalDetail, LLMUsageDetail, OpinionDetail, ReplanDetail,
    StepRecordDetail, SubmissionDetail,
)
from agent.workspace import Event, Workspace


def _event(kind, detail=None, *, step_id=None, verdict=None, agent="x"):
    return Event(uuid=f"u-{kind}", agent=agent, kind=kind, step_id=step_id,
                 verdict=verdict, detail=detail or {}, ts="2026-08-27 00:00:00")


def _bp():
    bp = Blueprint(meta={"task": "t"})
    bp.add_step(Step(id="s1", instruction="做", criterion="可验收"))
    return bp


def _dag(*ids, statuses=None):
    """构造 Blueprint.to_dict() 形状的 DAG 快照(可指定步骤状态,默认 PENDING)。"""
    statuses = statuses or {}
    bp = Blueprint(meta={"task": "t"})
    for sid in ids:
        s = Step(id=sid, instruction=f"做{sid}", criterion="可验收")
        if sid in statuses:
            s.status = StepStatus(statuses[sid])
        bp.add_step(s)
    return bp.to_dict()


def test_replay_is_deterministic_and_folds_each_kind():
    events = [
        _event(EventKind.REPLAN, ReplanDetail(changes="add s1", dag=_bp().to_dict()),
               agent="planner"),
        _event(EventKind.STEP_RECORD, StepRecordDetail(observation="o1", attempts=1),
               step_id="s1", verdict="retry", agent="evaluator_step"),
        _event(EventKind.STEP_RECORD, StepRecordDetail(observation="o2", is_completed=True,
                                                       status="PASSED"),
               step_id="s1", verdict="pass", agent="evaluator_step"),
        _event(EventKind.LLM_USAGE, LLMUsageDetail(role="planner", total_tokens=100),
               agent="system"),
        _event(EventKind.GOAL_EVAL, GoalEvalDetail(goal_id="g1", complete=True,
                                                   evidence=["s1"], reasoning="目标一已完成"),
               step_id="s1", agent="evaluator_step"),
        _event(EventKind.SUBMISSION, SubmissionDetail(flag="CTF{x}", ok=True, correct=True),
               agent="executor"),
    ]
    p1 = replay(events, goal_ids=["g1"])
    p2 = replay(events, goal_ids=["g1"])
    # 确定性:两次重放结果一致
    assert p1.steps.keys() == p2.steps.keys()
    assert p1.replans == p2.replans and p1.stalls == p2.stalls
    assert p1.turn == p2.turn and p1.goal_complete == p2.goal_complete
    assert p1.run_tokens == p2.run_tokens
    assert p1.submission == p2.submission and p1.submitted_flag == p2.submitted_flag
    # 各 kind 折叠语义
    assert p1.replans == 1 and p1.last_replan_idx == 0
    assert p1.steps["s1"].verdict == "pass"     # 最后一次 step_record 胜出
    assert p1.steps["s1"].attempts == 0         # 后一条覆盖前一条
    assert p1.task_completed is True
    assert p1.goal_complete == {"g1": ["s1"]}
    assert p1.run_tokens == 100
    assert p1.submitted_flag == "CTF{x}" and p1.submission["correct"] is True
    # DAG 从最后 REPLAN 快照重建 + step_record.status 叠加终态
    assert p1.blueprint is not None
    assert p1.blueprint.steps["s1"].status.value == "PASSED"


def test_replay_no_replan_has_no_blueprint():
    events = [
        _event(EventKind.STEP_RECORD, StepRecordDetail(observation="o"),
               step_id="s1", verdict="pass", agent="evaluator_step"),
    ]
    p = replay(events)
    assert p.blueprint is None
    assert p.steps["s1"].verdict == "pass"


def test_submission_fold_authoritative_correct():
    ev1 = _event(EventKind.SUBMISSION, SubmissionDetail(flag="CTF{x}", ok=True, correct=True))
    ev2 = _event(EventKind.SUBMISSION, SubmissionDetail(flag="CTF{x}", ok=True, correct=None))
    ev3 = _event(EventKind.SUBMISSION, SubmissionDetail(flag="CTF{w}", correct=False))
    p = Projection()
    apply(p, ev1, 0)
    assert p.submission["correct"] is True and p.submitted_flag == "CTF{x}"
    apply(p, ev2, 1)                       # correct=None 不覆盖已确认 True
    assert p.submission["correct"] is True
    assert p.submission["flag"] == "CTF{x}"
    apply(p, ev3, 2)                       # 明确 False 覆盖
    assert p.submission["correct"] is False and p.submitted_flag == "CTF{w}"


def test_turn_accumulates_and_stalls_fold_on_no_change():
    dag = _bp().to_dict()
    events = [
        _event(EventKind.REPLAN, ReplanDetail(changes="add s1", dag=dag), agent="planner"),
        _event(EventKind.STEP_EVAL, OpinionDetail(opinion="o1", observation="obs1"),
               step_id="s1", verdict="retry", agent="evaluator_step"),
        _event(EventKind.REPLAN, ReplanDetail(changes="无改动", dag=dag), agent="planner"),
        _event(EventKind.STEP_EVAL, OpinionDetail(opinion="o2"),
               step_id="s1", verdict="retry", agent="evaluator_step"),
    ]
    p = replay(events)
    assert p.replans == 2
    assert p.stalls == 1                    # 第二次 replan 无改动 → stall
    assert [t.opinion for t in p.turn] == ["o1", "o2"]      # turn 跨 replan 全量累积
    assert p.turn_consumed == 1             # 第一次 replan 时 turn 有 1 条
    assert [t.opinion for t in p.turn[p.turn_consumed:]] == ["o2"]  # 下次喂 planner 仅新窗口
    assert p.last_replan_idx == 2


def test_workspace_events_replay_into_projection(tmp_path):
    """workspace 事件持久化:set_blueprint/record_step/record_submission 各落事件,load 重放一致。"""
    ws = Workspace.create("run-p", {"q": "x"}, root=tmp_path)
    ws.set_blueprint(_bp())
    ws.record_step("s1", "pass", "done", status="PASSED")
    ws.record_submission({"flag": "CTF{x}", "correct": True})
    ws.sync()

    ws2 = Workspace.load("run-p", root=tmp_path)
    assert [e.kind for e in ws2.events] == ["replan", "step_record", "submission"]
    assert ws2.proj.blueprint is not None
    assert ws2.proj.blueprint.steps["s1"].instruction == "做"
    assert ws2.proj.blueprint.steps["s1"].status.value == "PASSED"
    assert ws2.proj.steps["s1"].verdict == "pass"
    assert ws2.proj.submitted_flag == "CTF{x}"
    assert ws2.proj.submission["correct"] is True


# ===== step 实例版本:重建侧双守卫(出生下标派生 + 快照终态守卫)=====


def test_replay_skip_stale_record_after_readd():
    """v1 s2 PASSED → v2 删 s2 → v3 重加 s2(fresh):旧实例的 PASSED 不叠到新实例。"""
    events = [
        _event(EventKind.REPLAN, ReplanDetail(changes="init", dag=_dag("s1", "s2")), agent="planner"),
        _event(EventKind.STEP_RECORD, StepRecordDetail(observation="o", status="PASSED"),
               step_id="s2", verdict="pass", agent="evaluator_step"),
        _event(EventKind.REPLAN, ReplanDetail(changes="rm s2", dag=_dag("s1")), agent="planner"),
        _event(EventKind.REPLAN, ReplanDetail(changes="readd s2", dag=_dag("s1", "s2")), agent="planner"),
    ]
    p = replay(events)
    assert p.blueprint.steps["s2"].status == StepStatus.PENDING   # 新实例从未执行


def test_replay_preserve_same_instance_record():
    """v1 s1 PASSED → v2 加 s3(s1 同实例未重加):s1 的 PASSED 保留。"""
    events = [
        _event(EventKind.REPLAN, ReplanDetail(changes="init", dag=_dag("s1")), agent="planner"),
        _event(EventKind.STEP_RECORD, StepRecordDetail(observation="o", status="PASSED"),
               step_id="s1", verdict="pass", agent="evaluator_step"),
        _event(EventKind.REPLAN, ReplanDetail(changes="add s3", dag=_dag("s1", "s3")), agent="planner"),
    ]
    p = replay(events)
    assert p.blueprint.steps["s1"].status == StepStatus.PASSED
    assert p.blueprint.steps["s3"].status == StepStatus.PENDING


def test_replay_skipped_terminal_not_crashed_or_overlaid():
    """v1 s2 RETRY 记录 → v2 快照 s2=SKIPPED(取消):不抛 DAGError,SKIPPED 保留。"""
    events = [
        _event(EventKind.REPLAN, ReplanDetail(changes="init", dag=_dag("s1", "s2")), agent="planner"),
        _event(EventKind.STEP_RECORD, StepRecordDetail(observation="o", status="RETRY"),
               step_id="s2", verdict="retry", agent="evaluator_step"),
        _event(EventKind.REPLAN, ReplanDetail(changes="cancel s2",
               dag=_dag("s1", "s2", statuses={"s2": "SKIPPED"})), agent="planner"),
    ]
    p = replay(events)
    assert p.blueprint.steps["s2"].status == StepStatus.SKIPPED


def test_replay_retry_then_pass_same_instance():
    """v1 s1 PENDING → RETRY 记录 → v2 快照 s1=RETRY(非终态)→ PASSED 记录 → 终态 PASSED。"""
    events = [
        _event(EventKind.REPLAN, ReplanDetail(changes="init", dag=_dag("s1")), agent="planner"),
        _event(EventKind.STEP_RECORD, StepRecordDetail(observation="o1", status="RETRY"),
               step_id="s1", verdict="retry", agent="evaluator_step"),
        _event(EventKind.REPLAN, ReplanDetail(changes="replan",
               dag=_dag("s1", statuses={"s1": "RETRY"})), agent="planner"),
        _event(EventKind.STEP_RECORD, StepRecordDetail(observation="o2", status="PASSED"),
               step_id="s1", verdict="pass", agent="evaluator_step"),
    ]
    p = replay(events)
    assert p.blueprint.steps["s1"].status == StepStatus.PASSED


def test_task_completed_sticky_across_readd():
    """task_completed 全局粘性:旧实例 is_completed=True 后重加,不回退(flag 已找到=任务完成)。"""
    events = [
        _event(EventKind.REPLAN, ReplanDetail(changes="init", dag=_dag("s1")), agent="planner"),
        _event(EventKind.STEP_RECORD, StepRecordDetail(observation="o", is_completed=True),
               step_id="s1", verdict="pass", agent="evaluator_step"),
        _event(EventKind.REPLAN, ReplanDetail(changes="readd", dag=_dag("s1")), agent="planner"),
    ]
    p = replay(events, goal_ids=["g1"])
    assert p.task_completed is True
