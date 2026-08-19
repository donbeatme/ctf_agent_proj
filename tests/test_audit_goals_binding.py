"""audit eval_goals:goal_evaluator binding 后返回非空(引擎 GOAL_EVAL→EVALUATOR_STEP 缝)。"""

from audit import AgentAuditEvaluator, AgentRuntimeBindings
from audit.flag_verifier import FlagVerifier
from audit.integrations.experience import LocalExperienceStore
from audit.settings import Settings
from main import _deterministic_goal_eval


def _make(tmp_path):
    bindings = AgentRuntimeBindings(
        blueprint=lambda: None,
        task=lambda: {"task_id": "t1", "title": "goal test"},
        current_step=lambda: None,
        observation=lambda: "",
        submitted_flag=lambda: None,
        completed=lambda: False,
    )
    return AgentAuditEvaluator(
        settings=Settings(
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
        ),
        verifier=FlagVerifier({}),
        experience_store=LocalExperienceStore(tmp_path / "data" / "exp.jsonl"),
        run_id="goal-test",
        agent_id="ctf-agent",
        bindings=bindings,
    )


def test_eval_goals_empty_without_binding(tmp_path):
    ev = _make(tmp_path)
    assert ev.eval_goals("ctx", [{"id": "g1"}], "[s1] status=PASSED") == []


def test_eval_goals_nonempty_with_binding(tmp_path):
    ev = _make(tmp_path)
    ev.bindings.goal_evaluator = _deterministic_goal_eval
    res = ev.eval_goals("ctx", [{"id": "g1"}], "[s1] status=PASSED")
    assert len(res) == 1
    g = res[0]
    assert g.goal_id == "g1" and g.complete is True
    assert g.evidence == ["s1"]


def test_eval_goals_missing_evidence(tmp_path):
    ev = _make(tmp_path)
    ev.bindings.goal_evaluator = _deterministic_goal_eval
    res = ev.eval_goals("ctx", [{"id": "g1"}], "[s1] status=FAILED")
    assert len(res) == 1
    assert res[0].complete is False and res[0].evidence == []
