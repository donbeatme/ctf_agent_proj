import hashlib
import json
import re
from pathlib import Path

import pytest

from agent.engine import Engine, EngineState
from agent.evaluator import SmokeEvaluator
from agent.executor import MockExecutor
from agent.workspace import MockWorkspace
from main import _local_planner
from task_understanding.artifact_adapter import contains_binary
from task_understanding.real_understander import RealTaskUnderstander


CASE = Path(__file__).resolve().parent / "fixtures" / "challenges" / "csaw_2021_maze"
MAZE_PUBLIC = CASE / "distfiles" / "maze_public"

_REAL_ELF = MAZE_PUBLIC.is_file() and MAZE_PUBLIC.read_bytes()[:4] == b"\x7fELF"


@pytest.mark.skipif(not _REAL_ELF, reason="real ELF maze fixture is not present")
def test_csaw_maze_real_challenge_ingestion_is_json_safe():
    task = RealTaskUnderstander().understand({"challenge_dir": str(CASE)})

    assert task.raw_content["name"] == "maze"
    assert task.raw_content["category"] == "rev"
    assert task.goal_list[0].id == "obtain_flag"
    assert len(task.raw_content["artifacts"]) == 1

    artifact = task.raw_content["artifacts"][0]
    assert artifact["meta"]["source"] == "maze_public"
    assert artifact["meta"]["kind"] == "binary"
    assert artifact["meta"].get("error") is None
    extra = artifact["meta"]["extra"]
    assert extra["binary_format"] == "ELF"
    assert extra["size_bytes"] == MAZE_PUBLIC.stat().st_size
    assert extra["sha256"] == _sha256_file(MAZE_PUBLIC)
    assert re.fullmatch(r"[0-9a-f]{64}", extra["sha256"])
    assert extra["architecture"] == "x86-64"
    assert not contains_binary(task.model_dump())
    json.dumps(task.model_dump(), ensure_ascii=False)


@pytest.mark.skipif(not _REAL_ELF, reason="real ELF maze fixture is not present")
def test_csaw_maze_real_challenge_offline_workflow_reaches_done():
    ws = MockWorkspace()
    engine = Engine(
        _local_planner("mock", ws),
        MockExecutor(observation="real challenge smoke step completed"),
        SmokeEvaluator(ws),
        workspace=ws,
        understander=RealTaskUnderstander(),
    )

    engine.run({"challenge_dir": str(CASE)})

    assert engine.scheduler.state == EngineState.DONE
    assert engine.task_input.raw_content["name"] == "maze"
    meta = engine.task_input.raw_content["artifacts"][0]["meta"]
    assert meta["source"] == "maze_public"
    assert meta["kind"] == "binary"
    assert meta["extra"]["binary_format"] == "ELF"
    assert engine.bp is not None
    assert len(engine.bp.steps) == 1


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
