import json
import os
import subprocess
import sys
from pathlib import Path

from agent.engine import Engine, EngineState
from agent.evaluator import SmokeEvaluator
from agent.executor import MockExecutor
from agent.workspace import MockWorkspace
from main import _local_planner
from task_understanding.artifact_adapter import contains_binary
from task_understanding.real_understander import RealTaskUnderstander


REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE = REPO_ROOT / "tests" / "fixtures" / "challenges" / "sample_challenge"
PYTHON = sys.executable


class RecordingExecutor(MockExecutor):
    def __init__(self):
        super().__init__(observation="local challenge step completed")
        self.calls = 0

    def run(self, step, ctx: str, tool_exec=None):
        self.calls += 1
        return super().run(step, ctx, tool_exec=tool_exec)


def test_local_challenge_full_offline_workflow_reaches_done():
    ws = MockWorkspace()
    planner = _local_planner("mock", ws)
    executor = RecordingExecutor()
    engine = Engine(
        planner,
        executor,
        SmokeEvaluator(ws),
        workspace=ws,
        understander=RealTaskUnderstander(),
    )

    engine.run({"challenge_dir": str(SAMPLE)})

    assert engine.scheduler.state == EngineState.DONE
    assert executor.calls == 1
    assert engine.bp is not None
    assert len(engine.bp.steps) == 1
    assert ws.blueprint is engine.bp
    assert ws.steps
    assert ws.events

    task_input = engine.task_input
    assert task_input.raw_content["name"] == "Sample Web Challenge"
    assert task_input.raw_content["category"] == "web"
    assert task_input.goal_list[0].id == "obtain_flag"
    assert not contains_binary(task_input.model_dump())
    json.dumps(task_input.model_dump(), ensure_ascii=False)


def test_run_local_challenge_cli_help_lists_command():
    # 子进程也要 UTF-8 模式:否则中文帮助文本按 locale(GBK) 写出,
    # 而父进程在 -X utf8 下按 utf-8 解码会炸(Windows 编码错位)。
    result = subprocess.run(
        [PYTHON, "-X", "utf8", "main.py", "--help"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert "run-task" in result.stdout
    assert "run-local-challenge" in result.stdout


def test_run_local_challenge_cli_mock_mode(tmp_path):
    result = subprocess.run(
        [
            PYTHON,
            "-X",
            "utf8",
            "main.py",
            "run-local-challenge",
            "--challenge-dir",
            str(SAMPLE),
            "--run-id",
            f"pytest-local-{tmp_path.name}",
            "--planner-mode",
            "mock",
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        # 纯离线冒烟:全 mock 确定性收敛;ee 默认 real 常开,这里显式关掉
        env={**os.environ, "EVALUATOR_STEP": "mock"},
    )

    assert "challenge name: Sample Web Challenge" in result.stdout
    assert "goal count: 1" in result.stdout
    assert "Engine final state: DONE" in result.stdout
    assert "Blueprint step count: 1" in result.stdout
