from agent.engine import Engine, EngineState
from agent.evaluator import SmokeEvaluator
from agent.executor import MockExecutor
from agent.workspace import MockWorkspace
from main import _local_planner


class RecordingExecutor(MockExecutor):
    def __init__(self):
        super().__init__(observation="offline step completed")
        self.calls = 0

    def run(self, step, ctx: str, tool_exec=None):
        self.calls += 1
        return super().run(step, ctx, tool_exec=tool_exec)


def test_native_engine_workflow_offline_with_real_planner_and_mock_llm():
    ws = MockWorkspace()
    planner = _local_planner("mock", ws)
    executor = RecordingExecutor()
    evaluator = SmokeEvaluator(ws)
    engine = Engine(planner, executor, evaluator, workspace=ws)

    engine.run(
        {
            "title": "offline workflow reproduction",
            "description": "Validate Engine -> Planner -> Blueprint -> mocks.",
            "goals": [{"id": "obtain_flag"}],
        }
    )

    assert engine.scheduler.state == EngineState.DONE
    assert engine.bp is not None
    assert len(engine.bp.steps) == 1
    assert executor.calls == 1
    assert ws.blueprint is engine.bp
    assert ws.steps
    assert ws.events
    assert engine.run_result is not None
    # 语义完成标志 = task_completed(仅 ee 软鉴定置位);SmokeEvaluator 不判 is_completed
    assert engine.run_result.completed is False


def test_smoke_evaluator_marks_goals_complete_after_step_pass():
    ws = MockWorkspace()
    planner = _local_planner("mock", ws)
    engine = Engine(
        planner,
        MockExecutor(observation="offline step completed"),
        SmokeEvaluator(ws),
        workspace=ws,
    )

    engine.run({"goals": [{"id": "obtain_flag"}]})

    assert engine.scheduler.state == EngineState.DONE
    # task_completed 只由 ee 的 is_completed=true 置位,goal 全部完成不置位
    assert engine.task_completed is False
    assert engine._goal_complete["obtain_flag"] == ["s1"]
