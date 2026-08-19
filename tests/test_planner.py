"""Planner 的文档合并检索 + DAG 失败重试。"""

from agent.planner import CombinedDocStore, Planner
from agent.schema import Goal, PlannerInput, PlannerMode, TaskInput
from agent.workspace import MockWorkspace


class _FakeStore:
    def __init__(self, results, docs=None):
        self._results = results
        self._docs = docs or {}

    def search(self, task):
        return list(self._results)

    def load_doc(self, doc_id):
        return self._docs.get(doc_id)


def _pin():
    return PlannerInput(
        mode=PlannerMode.INITIAL,
        task_input=TaskInput(
            raw_content={"title": "t", "description": "d"},
            goal_list=[Goal(id="obtain_flag")],
        ),
    )


def test_combined_docstore_dedups_and_falls_through():
    skill = _FakeStore(
        [("ctf-pwn", "SKILL pwn")],
        {"ctf-pwn": "SKILL pwn", "ctf-pwn/overflow": "sub doc"},
    )
    exp = _FakeStore(
        [("exp-1", "EXP 1"), ("ctf-pwn", "SKILL pwn dup")],
        {"exp-1": "EXP 1"},
    )
    combined = CombinedDocStore((skill, exp))

    assert combined.search({}) == [("ctf-pwn", "SKILL pwn"), ("exp-1", "EXP 1")]  # ctf-pwn 去重,保留首个
    assert combined.load_doc("exp-1") == "EXP 1"        # 落到经验库
    assert combined.load_doc("ctf-pwn/overflow") == "sub doc"  # 落到技能库子文档
    assert combined.load_doc("missing") is None


def test_combined_docstore_skill_only_when_no_experience():
    skill = _FakeStore([("ctf-crypto", "SKILL crypto")])
    combined = CombinedDocStore((skill, _FakeStore([])))
    assert combined.search({}) == [("ctf-crypto", "SKILL crypto")]


def test_plan_retries_on_dagerror_once():
    calls = []

    def mock_llm(*, system=None, prompt=None, messages=None, **kw):
        calls.append(prompt)
        if len(calls) == 1:
            # 空 DAG 上 update 不存在的 step → DAGError
            return '{"update":[{"id":"s1","instruction":"x","criterion":"c"}]}'
        return '{"add":[{"id":"s1","instruction":"读题","criterion":"拿到文本","depends_on":[]}]}'

    planner = Planner(llm_call=mock_llm, workspace=MockWorkspace())
    bp = planner.plan(_pin())

    assert len(calls) == 2
    assert "补丁无法应用" in calls[1]
    assert bp.steps["s1"].instruction == "读题"


def test_plan_propagates_dagerror_after_retry():
    calls = []

    def mock_llm(*, system=None, prompt=None, messages=None, **kw):
        calls.append(prompt)
        return '{"update":[{"id":"s1","instruction":"x","criterion":"c"}]}'

    from agent.blueprint import DAGError

    planner = Planner(llm_call=mock_llm, workspace=MockWorkspace())
    try:
        planner.plan(_pin())
    except DAGError:
        assert len(calls) == 2  # 重试一次后仍失败,异常照常上抛
        return
    raise AssertionError("DAGError 应上抛")
