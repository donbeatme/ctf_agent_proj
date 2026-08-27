"""audit 评估器接主架构的集成测试(P0/P1 对齐验收)。

验证三件事:
1. service.planner_docs 真正喂进 Planner 的 DocStore 缝(经验检索不再是死代码)。
2. AgentRuntimeBindings.submitted_flag 读到引擎的 submitted_flag(Mock 未提交 → None,链路不炸)。
3. audit 事件(audit_plan_review/audit_step_eval/audit_reflect)走 EVENT_SCHEMA 类型化通道,
   不再是退化 dict。
"""

import json
from pathlib import Path

from agent.blueprint import Blueprint, Step
from agent.engine import Engine, EngineState
from agent.executor import MockExecutor
from agent.planner import Planner
from agent.schema import EventKind, EvalSource, Role
from agent.workspace import MockWorkspace, Workspace
from audit import AgentAuditService, AgentRuntimeBindings
from audit.agent_adapter import AuditExperienceDocStore
from audit.settings import Settings
from main import _SequencedMockPlannerLLM, _mock_noop_plan


def _passing_plan() -> str:
    """带验收标记的计划:结构评审 1.0 分,能走到 执行→步骤验收→任务反思。"""
    return json.dumps(
        {
            "add": [
                {
                    "id": "s1",
                    "instruction": "分析题面,提取候选 flag,校验后提交",
                    "criterion": "候选 flag 通过独立校验并成功提交",
                    "depends_on": [],
                }
            ],
            "update": [],
            "remove": [],
            "reason": "audit wiring integration test.",
        },
        ensure_ascii=False,
    )


def _make_settings(tmp_path: Path) -> Settings:
    return Settings(
        mode="offline",
        data_dir=tmp_path / "data",
        langsmith_enabled=False,
        llm_api_key=None,
        llm_base_url="",
        llm_model="",
        ragflow_enabled=False,
        ragflow_api_key=None,
        ragflow_base_url="",
        ragflow_dataset_name="",
        experience_search_limit=3,
    )


def test_audit_evaluator_wiring_end_to_end(tmp_path):
    ws = MockWorkspace()
    settings = _make_settings(tmp_path)
    service = AgentAuditService(
        settings=settings,
        flag_rules={},
        run_id="wiring-test",
        agent_id="ctf-agent",
        event_sink=lambda kind, detail: ws.add_event(Role.SYSTEM, kind, **detail),
    )
    planner = Planner(
        llm_call=_SequencedMockPlannerLLM(_passing_plan(), _mock_noop_plan()),
        workspace=ws,
        docs=service.planner_docs,
    )
    holder: dict = {}
    bindings = AgentRuntimeBindings(
        blueprint=lambda: holder["engine"].bp,
        task=lambda: {"task_id": "t1", "title": "wiring test"},
        current_step=lambda: holder["engine"].current,
        observation=lambda: holder["engine"]._obs or "",
        submitted_flag=lambda: holder["engine"].submitted_flag if holder.get("engine") else None,
        completed=lambda: holder["engine"].task_completed,
    )
    audit_out = tmp_path / "audit.json"
    evaluator = service.bind_evaluator(bindings, audit_output=audit_out)
    engine = Engine(
        planner,
        MockExecutor(observation="flag 提交完成"),
        evaluator,
        workspace=ws,
    )
    holder["engine"] = engine
    try:
        engine.run({"task_id": "t1", "title": "wiring test", "goals": [{"id": "obtain_flag"}]})
    finally:
        evaluator.close()

    # P0-2: planner 确实用上了 audit 经验库
    assert planner.docs is service.planner_docs
    # P0-1: binding 读到引擎 submitted_flag(Mock 未提交 → None,链路不炸)
    assert evaluator.bindings.submitted_flag() is None
    # P0-3: 三个 audit 事件全走类型化通道(非退化 dict)
    for kind in (EventKind.AUDIT_PLAN_REVIEW, EventKind.AUDIT_STEP_EVAL, EventKind.AUDIT_REFLECT):
        hits = [e for e in ws.events if e.kind == kind]
        assert hits, f"缺少事件 {kind}"
        assert all(not isinstance(e.detail, dict) for e in hits), f"{kind} 走了 dict 通道"
    assert engine.scheduler.state in (EngineState.DONE, EngineState.FAILED)
    # P0-4: reflect 产生过 AuditRecord → close 时 audit.json 已落盘(服务→评估器 audit_output 管线)
    assert audit_out.is_file(), "audit.json 未由 close() 落盘"
    dumped = json.loads(audit_out.read_text(encoding="utf-8"))
    assert "steps" not in dumped and "attempt" not in dumped  # 轨迹真源是 events.jsonl,不双写
    assert dumped["flag"]["valid"] is None and dumped["flag"]["submitted"] is False  # 未提交 → 无判定来源,REPLAN 收敛 FAILED


def test_audit_events_cqrs_roundtrip_and_projection_isolation(tmp_path):
    """audit 事件真实持久化 → load 重放:typed 归一化、不入 turn/history 投影、标准投影不受污染。"""
    ws = Workspace.create("run-audit", {"q": "x"}, root=tmp_path)
    sink = lambda kind, detail: ws.add_event(Role.SYSTEM, kind, **detail)  # 镜像 main._workspace_event_sink
    bp = Blueprint(meta={"task": "t"})
    bp.add_step(Step(id="s1", instruction="做", criterion="可验收"))
    ws.set_blueprint(bp)
    sink("audit_plan_review", {"decision": "pass", "score": 0.95,
                               "issues": ["s3 与 s4 职责重叠"], "suggestions": ["拆分 s3"]})
    ws.record_opinion(EvalSource.STEP_EVAL, "retry", "s1 要更具体", step_id="s1")
    sink("audit_step_eval", {"step_id": "s1", "decision": "retry", "score": 0.4,
                             "reasoning": "缺少 flag 证据", "diagnosis": "incomplete"})
    ws.record_step("s1", "pass", "完成", status="PASSED")
    sink("audit_reflect", {"decision": "pass", "reason": "目标达成", "flag_valid": True, "store_error": None})
    ws.sync()

    ws2 = Workspace.load("run-audit", root=tmp_path)
    # 1) 3 个 audit 事件全走 typed 通道(load 归一化后非退化 dict)
    for kind in (EventKind.AUDIT_PLAN_REVIEW, EventKind.AUDIT_STEP_EVAL, EventKind.AUDIT_REFLECT):
        hits = [e for e in ws2.events if e.kind == kind]
        assert hits, f"缺少 audit 事件 {kind}"
        assert all(not isinstance(e.detail, dict) for e in hits)
    # 2) audit 是 observability 通道,不入 turn/history 投影
    assert [t.source.value for t in ws2.proj.turn] == ["step_eval"]
    assert [e.kind for e in ws2.proj.history_events] == ["replan", "step_record"]
    # 3) 标准投影不受 audit 事件污染
    assert ws2.proj.replans == 1 and ws2.proj.run_tokens == 0
    assert ws2.proj.steps["s1"].verdict == "pass"
    # 4) typed 字段正确(step_id 在 Event 顶层,detail 持有评分/诊断)
    pr = next(e.detail for e in ws2.events if e.kind == EventKind.AUDIT_PLAN_REVIEW)
    assert pr.score == 0.95 and "拆分 s3" in pr.suggestions
    se = [e for e in ws2.events if e.kind == EventKind.AUDIT_STEP_EVAL]
    assert se[0].step_id == "s1" and se[0].detail.diagnosis == "incomplete"


# ===== 接线:audit 优先读 challenge_type(回退 category)=====


def _capture_store():
    """捕获 retrieve_experience query 的桩 store。"""
    queries = []

    class _CapturingStore:
        def retrieve_experience(self, query, limit=5, agent_id=""):
            queries.append(query)
            return []

    return _CapturingStore(), queries


def test_audit_experience_search_prefers_challenge_type():
    """AuditExperienceDocStore.search:task 带 challenge_type → query 类别用它,非 unknown。"""
    store, queries = _capture_store()
    docs = AuditExperienceDocStore(store, agent_id="ctf-agent", limit=3)
    docs.search({
        "title": "sample",
        "challenge_type": "ctf-pwn",
        "description": "buffer overflow",
    })
    assert queries
    assert "ctf-pwn" in queries[0]
    assert "unknown" not in queries[0]


def test_audit_experience_search_falls_back_to_category():
    """task 无 challenge_type → 回退 category。"""
    store, queries = _capture_store()
    docs = AuditExperienceDocStore(store, agent_id="ctf-agent", limit=3)
    docs.search({"title": "sample", "category": "web"})
    assert queries
    assert "web" in queries[0]


def test_audit_attempt_records_challenge_type_as_category():
    """_new_attempt:task 带 challenge_type → CTFAttempt.category 用它(非 unknown)。"""
    attempt = _make_attempt({"title": "t", "challenge_type": "ctf-crypto"})
    assert attempt.category == "ctf-crypto"


def test_audit_attempt_category_falls_back_to_category_field():
    attempt = _make_attempt({"title": "t", "category": "rev"})
    assert attempt.category == "rev"


def test_audit_attempt_category_defaults_unknown():
    attempt = _make_attempt({"title": "t"})
    assert attempt.category == "unknown"


def _make_attempt(task: dict):
    from audit.agent_adapter import AgentAuditEvaluator, AgentRuntimeBindings
    from audit.flag_verifier import FlagVerifier
    from audit.integrations.experience import LocalExperienceStore
    from audit.settings import Settings

    from pathlib import Path
    settings = Settings(
        mode="offline",
        data_dir=Path("data"),
        langsmith_enabled=False,
        llm_api_key=None,
        llm_base_url="",
        llm_model="",
        ragflow_enabled=False,
        ragflow_api_key=None,
        ragflow_base_url="",
        ragflow_dataset_name="",
        experience_search_limit=3,
    )
    from agent.blueprint import Blueprint
    bindings = AgentRuntimeBindings(
        blueprint=lambda: Blueprint(meta={"task": task}),
        task=lambda: task,
        current_step=lambda: None,
        observation=lambda: "",
        submitted_flag=lambda: None,
    )
    evaluator = AgentAuditEvaluator(
        settings=settings,
        verifier=FlagVerifier({}),
        experience_store=LocalExperienceStore(Path("data") / "x.jsonl"),
        run_id="t",
        agent_id="ctf-agent",
        bindings=bindings,
    )
    return evaluator._new_attempt()
