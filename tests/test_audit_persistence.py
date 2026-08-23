"""audit.json 落盘:只持久化派生字段,不含原始轨迹(events.jsonl 才是轨迹真源)。"""

import json

from agent.blueprint import Blueprint
from agent.evaluator import Verdict
from audit import AgentAuditEvaluator, AgentRuntimeBindings
from audit.flag_verifier import FlagVerifier
from audit.integrations.experience import LocalExperienceStore
from audit.schemas import PlanEvaluation
from audit.settings import Settings


def _make(tmp_path, submission, submitted_flag, audit_output):
    state = {"submission": submission, "flag": submitted_flag}
    bindings = AgentRuntimeBindings(
        blueprint=lambda: Blueprint(meta={}),
        task=lambda: {"task_id": "t1", "title": "persist test"},
        current_step=lambda: None,
        observation=lambda: "",
        submitted_flag=lambda: state["flag"],
        completed=lambda: False,
        submission_result=lambda: state["submission"],
    )
    evaluator = AgentAuditEvaluator(
        settings=Settings(
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
        ),
        verifier=FlagVerifier({}),
        experience_store=LocalExperienceStore(tmp_path / "data" / "exp.jsonl"),
        run_id="persist-test",
        agent_id="ctf-agent",
        bindings=bindings,
        audit_output=audit_output,
    )
    evaluator.plan_evaluation = PlanEvaluation(
        decision="pass", score=1.0, issues=[], suggestions=[], evaluator="test"
    )
    return evaluator


def test_audit_json_persists_derived_fields(tmp_path):
    out = tmp_path / "audit.json"
    sub = {"flag": "flag{x}", "ok": True, "correct": True, "message": "ok"}
    ev = _make(tmp_path, submission=sub, submitted_flag="flag{x}", audit_output=out)
    res = ev.reflect("ctx")
    assert res.verdict == Verdict.DONE
    ev.close()

    assert out.is_file()
    data = json.loads(out.read_text(encoding="utf-8"))
    for key in ("plan_evaluation", "step_evaluation", "flag", "metrics", "task_evaluation"):
        assert key in data, key
    # 不落原始 attempt(含 steps 轨迹):轨迹真源是 events.jsonl
    assert "attempt" not in data
    assert "steps" not in data
    assert data["run_id"] == "persist-test"
    assert data["submitted_flag"] == "flag{x}"
    assert data["flag"]["valid"] is True
    assert data["flag"]["mode"] == "platform"
    assert data["metrics"]["flag_success"] == 1.0
    # 原子写:临时文件已清理
    assert not (tmp_path / "audit.json.tmp").exists()


def test_audit_json_not_written_without_record(tmp_path):
    out = tmp_path / "audit.json"
    ev = _make(tmp_path, submission=None, submitted_flag=None, audit_output=out)
    ev.close()  # 从未 reflect → last_record None → 不落盘
    assert not out.exists()
