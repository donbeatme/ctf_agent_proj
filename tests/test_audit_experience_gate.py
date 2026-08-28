"""经验回流防污染：质量门（有确定性信号才存）+ search_text 紧凑检索（ctx 不再吃整段 JSON）。"""

import json

from audit.integrations.experience import (
    LocalExperienceStore,
    build_experience,
    experience_markdown,
    should_store_experience,
)
from audit.schemas import (
    AttemptMetrics,
    AuditRecord,
    CTFAttempt,
    FlagResult,
    PlanEvaluation,
    Reflection,
    StepEvaluation,
    TaskEvaluation,
)


def _record(flag_valid, reflection=None, decision="fail", category="ctf-pwn"):
    attempt = CTFAttempt(
        attempt_id="a1", task_id="t1", agent_id="ctf-agent", category=category,
        started_at="2026-08-01T00:00:00+00:00", ended_at="2026-08-01T00:01:00+00:00",
        steps=[], plan=[],
    )
    metrics = AttemptMetrics(
        attempt_id="a1", flag_success=1.0 if flag_valid else 0.0,
        step_evaluation_score=0.8, total_steps=2, trajectory_events=2,
        effective_steps=2, tool_success_rate=1.0, tool_error_rate=0.0,
        retry_rate=0.0, duration_seconds=60.0, token_count=0,
        step_efficiency=1.0, time_efficiency=1.0, composite_score=0.8,
    )
    return AuditRecord(
        attempt=attempt,
        plan_evaluation=PlanEvaluation("pass", 1.0, [], [], "test"),
        step_evaluation=StepEvaluation(0.8, 1, 0, 0, [], "test"),
        flag=FlagResult("t1", flag_valid, "platform",
                        "flag 验证通过" if flag_valid else "flag 不匹配", submitted=True),
        metrics=metrics,
        task_evaluation=TaskEvaluation(decision, "reason", reflection=reflection),
    )


def _substantive_reflection():
    return Reflection(
        "a1", "ret2libc 泄露地址时漏了 canary 偏移",
        ["canary 偏移需本地 gdb 复核"], ["先本地预演再上远程"], "TaskReflection/LlmApi")


# ===== should_store_experience 质量门 =====


def test_should_store_flag_valid_true():
    assert should_store_experience(_record(flag_valid=True)) is True


def test_should_store_flag_valid_false():
    assert should_store_experience(_record(flag_valid=False)) is True


def test_should_store_flag_none_no_reflection_skips():
    assert should_store_experience(_record(flag_valid=None, reflection=None)) is False


def test_should_store_flag_none_substantive_reflection_stores():
    assert should_store_experience(
        _record(flag_valid=None, reflection=_substantive_reflection())) is True


def test_should_store_flag_none_no_issue_default_skips():
    r = _record(flag_valid=None, reflection=Reflection(
        "a1", "任务和独立 Flag 验证均通过，未发现必须反思的问题。", [], [], "offline"))
    assert should_store_experience(r) is False


# ===== LocalExperienceStore 质量门落库 =====


def test_local_store_gate_skips_no_signal(tmp_path):
    store = LocalExperienceStore(tmp_path / "exp.jsonl")
    res = store.store_experience(_record(flag_valid=None, reflection=None))
    assert res["status"] == "skipped" and res["reason"] == "no-signal"
    assert not (tmp_path / "exp.jsonl").exists()


def test_local_store_stores_flagged_run_with_search_text(tmp_path):
    store = LocalExperienceStore(tmp_path / "exp.jsonl")
    res = store.store_experience(_record(flag_valid=False))
    assert res["status"] == "stored"
    exp = json.loads(
        (tmp_path / "exp.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )["experience"]
    assert exp["search_text"]


# ===== search_text 紧凑检索 =====


def test_build_experience_search_text_excludes_scores():
    st = build_experience(_record(flag_valid=False))["search_text"]
    assert "category=ctf-pwn" in st and "outcome=fail" in st
    assert "flag 确认错误" in st
    # 不含分数/计数/字段名 → 检索不再被结构 token 刷分
    assert "composite_score" not in st and "0.8" not in st


def test_experience_markdown_compact_no_json_blob():
    exp = build_experience(_record(flag_valid=False))
    md = experience_markdown(exp)
    # 不再渲染整段 JSON(键与分数不进 ctx)
    assert '"score"' not in md and "composite_score" not in md
    assert "outcome=fail" in md


def test_local_retrieve_scores_search_text_not_json_keys(tmp_path):
    store = LocalExperienceStore(tmp_path / "exp.jsonl")
    store.store_experience(_record(flag_valid=False, reflection=_substantive_reflection()))
    # 语义查询命中 search_text 的诊断/教训
    hits = store.retrieve_experience("canary 偏移 ret2libc", agent_id="ctf-agent")
    assert hits
    assert "composite_score" not in hits[0]["memory"]  # 返回体同样紧凑
    # 只匹配 JSON 字段名的查询对 search_text 无命中(旧实现会误命中)
    assert not store.retrieve_experience("composite_score step_evaluation_score")
