import json
from pathlib import Path

from agent.blueprint import Blueprint
from agent.engine import Engine
from agent.evaluator import EvalResult, MockEvaluator, Verdict
from agent.executor import MockExecutor
from agent.schema import Role, TaskInput
from agent.understander import MockTaskUnderstander, TaskUnderstander
from agent.workspace import MockWorkspace
from task_understanding.artifact_adapter import contains_binary
from task_understanding.real_understander import RealTaskUnderstander


SAMPLE = Path(__file__).resolve().parent / "fixtures" / "challenges" / "sample_challenge"


class RecordingPlanner:
    def __init__(self):
        self.received = None
        self.workspace = None

    def plan(self, planner_input):
        self.received = planner_input
        return Blueprint(meta={"reason": "recorded"})


class RaisingUnderstander(TaskUnderstander):
    def understand(self, raw: dict) -> TaskInput:
        raise RuntimeError("understander failed")


def _engine(planner, *, understander=None, max_cycles=2):
    evaluator = MockEvaluator(
        {
            Role.EVALUATOR_PLAN: EvalResult(Verdict.PASS, "plan accepted"),
            Role.EVALUATOR_STEP: EvalResult(Verdict.PASS, "step accepted"),
            Role.EVALUATOR_TASK: EvalResult(Verdict.DONE, "task done"),
        }
    )
    return Engine(
        planner,
        MockExecutor(observation="not used"),
        evaluator,
        workspace=MockWorkspace(),
        understander=understander,
        max_cycles=max_cycles,
    )


def _sources(task_input: TaskInput) -> set[str]:
    return {
        (artifact.get("meta") or {}).get("source")
        for artifact in task_input.raw_content.get("artifacts", [])
    }


def test_engine_passes_real_understander_task_input_to_planner():
    planner = RecordingPlanner()
    engine = _engine(planner, understander=RealTaskUnderstander())

    engine.run({"challenge_dir": str(SAMPLE)})

    assert isinstance(engine.task_input, TaskInput)
    assert planner.received is not None
    assert planner.received.task_input is engine.task_input

    task_input = planner.received.task_input
    assert task_input.raw_content["name"] == "Sample Web Challenge"
    assert task_input.raw_content["category"] == "web"
    assert task_input.raw_content["target"] == "http://127.0.0.1:8080"
    assert task_input.raw_content["flag_format"] == "flag{...}"
    assert task_input.raw_content["hints"] == ["Inspect the provided challenge materials."]

    assert len(engine.goals) == 1
    assert engine.goals[0].id == "obtain_flag"
    assert len(task_input.goal_list) == 1
    assert task_input.goal_list[0].id == "obtain_flag"

    assert "README.txt" in _sources(task_input)

    dumped = task_input.model_dump()
    assert not contains_binary(dumped)
    json.dumps(dumped, ensure_ascii=False)


def test_engine_understander_exception_fallback_is_preserved():
    planner = RecordingPlanner()
    raw = {"challenge_dir": "/definitely/not/exist"}
    engine = _engine(planner, understander=RaisingUnderstander(), max_cycles=1)

    engine.run(raw)

    assert isinstance(engine.task_input, TaskInput)
    assert engine.task_input.raw_content == raw
    assert engine.task_input.goal_list == []
    assert planner.received.task_input is engine.task_input


def test_engine_default_understander_remains_mock():
    planner = RecordingPlanner()
    engine = _engine(planner, max_cycles=1)

    assert isinstance(engine.understander, MockTaskUnderstander)
