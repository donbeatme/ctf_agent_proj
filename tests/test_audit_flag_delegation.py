"""audit 正确性权威让位 _local_verify/平台:reflect 三层判定 + 动态 flag 不回环。"""

import pytest

from agent.blueprint import Blueprint
from agent.evaluator import Verdict
from audit import AgentAuditEvaluator, AgentRuntimeBindings
from audit.flag_verifier import FlagVerifier
from audit.integrations.experience import LocalExperienceStore
from audit.schemas import PlanEvaluation
from audit.settings import Settings


def _offline_settings(tmp_path):
    return Settings(
        mode="offline",
        data_dir=tmp_path / "data",
        langsmith_enabled=False,
        deepseek_api_key=None,
        deepseek_base_url="",
        deepseek_model="",
        ragflow_enabled=False,
        ragflow_api_key=None,
        ragflow_base_url="",
        ragflow_dataset_name="",
        experience_search_limit=3,
    )


def _make_evaluator(tmp_path, verifier=None, submission=None, submitted_flag=None):
    state = {"submission": submission, "flag": submitted_flag}
    bindings = AgentRuntimeBindings(
        blueprint=lambda: Blueprint(meta={}),
        task=lambda: {"task_id": "t1", "title": "delegation test"},
        current_step=lambda: None,
        observation=lambda: "",
        submitted_flag=lambda: state["flag"],
        completed=lambda: False,
        submission_result=lambda: state["submission"],
    )
    evaluator = AgentAuditEvaluator(
        settings=_offline_settings(tmp_path),
        verifier=verifier or FlagVerifier({}),
        experience_store=LocalExperienceStore(tmp_path / "data" / "exp.jsonl"),
        run_id="delegation-test",
        agent_id="ctf-agent",
        bindings=bindings,
    )
    # 跳过结构评审,只测 reflect 的 flag 合并判定
    evaluator.plan_evaluation = PlanEvaluation(
        decision="pass", score=1.0, issues=[], suggestions=[], evaluator="test"
    )
    return evaluator


def test_correct_true_platform_verdict_done(tmp_path):
    sub = {"flag": "flag{x}", "ok": True, "correct": True, "message": "ok"}
    ev = _make_evaluator(tmp_path, submission=sub, submitted_flag="flag{x}")
    res = ev.reflect("ctx")
    assert res.verdict == Verdict.DONE
    flag = ev.last_record.flag
    assert flag.valid is True and flag.mode == "platform" and flag.submitted is True


def test_correct_false_replan(tmp_path):
    sub = {"flag": "flag{wrong}", "ok": True, "correct": False, "message": "提交错误"}
    ev = _make_evaluator(tmp_path, submission=sub, submitted_flag="flag{wrong}")
    res = ev.reflect("ctx")
    assert res.verdict == Verdict.REPLAN
    assert ev.last_record.flag.valid is False


def test_dynamic_flag_missing_rule_submitted_no_loop(tmp_path):
    """Hack World 场景:correct=None + 无规则 + 已提交(ok=True)→ DONE,不回环。"""
    sub = {"flag": "flag{uuid}", "ok": True, "correct": None, "message": "无法本地判定"}
    ev = _make_evaluator(tmp_path, submission=sub, submitted_flag="flag{uuid}")
    res = ev.reflect("ctx")
    assert res.verdict == Verdict.DONE
    flag = ev.last_record.flag
    assert flag.valid is None and flag.mode == "missing" and flag.submitted is True


def test_never_submitted_missing_rule_replan(tmp_path):
    ev = _make_evaluator(tmp_path, submission=None, submitted_flag=None)
    res = ev.reflect("ctx")
    assert res.verdict == Verdict.REPLAN
    flag = ev.last_record.flag
    assert flag.valid is None and flag.submitted is False


def test_static_rule_fallback_when_correct_none(tmp_path):
    verifier = FlagVerifier({"t1": {"mode": "exact", "value": "flag{good}"}})
    ev = _make_evaluator(
        tmp_path, verifier=verifier,
        submission={"flag": "flag{good}", "ok": True, "correct": None},
        submitted_flag="flag{good}",
    )
    res = ev.reflect("ctx")
    assert res.verdict == Verdict.DONE
    flag = ev.last_record.flag
    assert flag.valid is True and flag.mode == "exact"


def test_static_rule_fallback_replans_on_mismatch(tmp_path):
    verifier = FlagVerifier({"t1": {"mode": "exact", "value": "flag{good}"}})
    ev = _make_evaluator(
        tmp_path, verifier=verifier,
        submission={"flag": "flag{bad}", "ok": True, "correct": None},
        submitted_flag="flag{bad}",
    )
    res = ev.reflect("ctx")
    assert res.verdict == Verdict.REPLAN
    assert ev.last_record.flag.valid is False


def test_settings_mode_invalid_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("CTF_AUDIT_MODE", "bogus")
    with pytest.raises(ValueError):
        Settings.from_env()
