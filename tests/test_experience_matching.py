"""engine._init_run 装载已验证经验(经 executor.match_experience)。"""

from agent.engine import Engine
from agent.evaluator import MockEvaluator
from agent.executor import MockExecutor
from agent.planner import Planner
from agent.workspace import MockWorkspace
from tests.mock_data import MOCK_TASK

RECORDS = [{"procedure_id": "p1", "challenge_id": "c1", "friendly_id": "PCHAL-1",
            "method": "procedure", "platform_verified": 1, "verifier_path": "solve.py"}]


class _ExpExecutor(MockExecutor):
    def __init__(self, records):
        super().__init__()
        self._records = list(records)

    def match_experience(self):
        return list(self._records)


def _engine(executor):
    ws = MockWorkspace()
    return Engine(planner=Planner(), executor=executor, evaluator=MockEvaluator(),
                  workspace=ws, understander=None)


def test_init_run_loads_matched_experience():
    ex = _ExpExecutor(RECORDS)
    eng = _engine(ex)
    eng._init_run(MOCK_TASK)
    assert eng.workspace.get_experience() == RECORDS


def test_init_run_no_match_empty():
    ex = _ExpExecutor([])
    eng = _engine(ex)
    eng._init_run(MOCK_TASK)
    assert eng.workspace.get_experience() == []


def test_init_run_base_executor_empty_when_no_adapter():
    # 基类 Executor.match_experience:无 adapter → 空(而非抛错)
    eng = _engine(MockExecutor())
    eng._init_run(MOCK_TASK)
    assert eng.workspace.get_experience() == []


def test_init_run_reset_clears_stale_experience():
    ex = _ExpExecutor(RECORDS)
    eng = _engine(ex)
    eng._init_run(MOCK_TASK)
    assert eng.workspace.get_experience() == RECORDS
    ex._records = []                       # 下一 run 匹配不到
    eng._init_run(MOCK_TASK)
    assert eng.workspace.get_experience() == []
