"""Blueprint 语义方法(update 内容 vs 结构)失效范围验证。

apply_patch 按字段分发到语义方法,失效粒度(design/dag.md §5):
- 内容变更(instruction/criterion):非终态步骤回 PENDING 重跑,不级联后代
- 结构变更(depends_on / remove):非终态步骤回 PENDING,并级联非终态后代
- 终态步骤(PASSED/SKIPPED)任何更新不改状态、不级联
"""

from agent.blueprint import Blueprint, Step, StepStatus
from agent.schema import parse_plan


def _set(bp, sid, status, force=True):
    bp.set_status(sid, status, force=force)


def _chain():
    """s1 → s2 → s3 → s4(单链),全部 PENDING。"""
    bp = Blueprint(meta={"task": "示例"})
    for sid in ("s1", "s2", "s3", "s4"):
        deps = [f"s{int(sid[1:]) - 1}"] if sid != "s1" else []
        bp.add_step(Step(sid, f"指令{sid}", f"标准{sid}", deps))
    return bp


def test_add_step_with_skill_id():
    """planner 检索后把 skill_id 绑进 Step(add 路径),to_patch 透传。"""
    bp = Blueprint(meta={"task": "t"})
    bp.apply_patch(parse_plan(
        '{"add":[{"id":"s1","instruction":"注入","criterion":"拿到flag","skill_id":"doc0"}],"reason":"r"}'
    ).to_patch())
    assert bp.steps["s1"].skill_id == "doc0"


def test_update_skill_id_rearms_non_done_step():
    """改 skill 绑定是内容变更:非终态步骤回 PENDING 重跑,不级联后代。"""
    bp = _chain()
    _set(bp, "s1", StepStatus.PASSED)
    _set(bp, "s2", StepStatus.ESCALATED)
    _set(bp, "s3", StepStatus.RETRY)
    bp.apply_patch(parse_plan('{"update":[{"id":"s2","skill_id":"doc1"}]}').to_patch())
    assert bp.steps["s2"].skill_id == "doc1"
    assert bp.steps["s2"].status == StepStatus.PENDING
    assert bp.steps["s3"].status == StepStatus.RETRY


def test_update_skill_id_on_done_no_op():
    """终态步骤改 skill 绑定:字段可改,状态不重置。"""
    bp = _chain()
    _set(bp, "s1", StepStatus.PASSED)
    bp.apply_patch(parse_plan('{"update":[{"id":"s1","skill_id":"doc2"}]}').to_patch())
    assert bp.steps["s1"].skill_id == "doc2"
    assert bp.steps["s1"].status == StepStatus.PASSED


def test_update_criterion_on_done_no_op():
    bp = _chain()
    _set(bp, "s1", StepStatus.PASSED)
    _set(bp, "s2", StepStatus.PASSED)
    _set(bp, "s3", StepStatus.RETRY)
    bp.apply_patch(parse_plan('{"update":[{"id":"s2","criterion":"可逆且正确"}]}').to_patch())
    assert bp.steps["s2"].status == StepStatus.PASSED        # 终态不重置
    assert bp.steps["s2"].criterion == "可逆且正确"          # 字段已改
    assert bp.steps["s3"].status == StepStatus.RETRY         # 不级联后代


def test_update_instruction_on_terminal_no_op():
    bp = _chain()
    _set(bp, "s1", StepStatus.PASSED)
    bp.apply_patch(parse_plan('{"update":[{"id":"s1","instruction":"先读题再找 flag"}]}').to_patch())
    assert bp.steps["s1"].status == StepStatus.PASSED
    assert bp.steps["s1"].instruction == "先读题再找 flag"


def test_update_criterion_rearms_non_done_step_not_descendants():
    bp = _chain()
    _set(bp, "s1", StepStatus.PASSED)
    _set(bp, "s2", StepStatus.ESCALATED)
    _set(bp, "s3", StepStatus.RETRY)
    bp.apply_patch(parse_plan('{"update":[{"id":"s2","criterion":"可逆(收紧)"}]}').to_patch())
    assert bp.steps["s2"].status == StepStatus.PENDING       # 自身复活,可重跑
    assert bp.steps["s3"].status == StepStatus.RETRY         # 内容变更不级联后代


def test_set_depends_on_cascades_non_done_descendants():
    bp = _chain()
    _set(bp, "s1", StepStatus.PASSED)
    _set(bp, "s2", StepStatus.ESCALATED)
    _set(bp, "s3", StepStatus.PASSED)                        # 终态后代
    _set(bp, "s4", StepStatus.RETRY)                         # 非终态后代
    bp.add_step(Step("s0", "准备", "就绪"))                   # 新入口,供 s2 换依赖
    bp.apply_patch(parse_plan('{"update":[{"id":"s2","depends_on":["s0"]}]}').to_patch())
    assert bp.steps["s2"].status == StepStatus.PENDING       # 自身重置
    assert bp.steps["s3"].status == StepStatus.PASSED        # 终态后代不动
    assert bp.steps["s4"].status == StepStatus.PENDING       # 非终态后代被级联


def test_set_depends_on_on_done_no_cascade():
    bp = _chain()
    _set(bp, "s1", StepStatus.PASSED)
    _set(bp, "s2", StepStatus.PASSED)
    _set(bp, "s3", StepStatus.RETRY)
    bp.add_step(Step("s0", "准备", "就绪"))
    bp.apply_patch(parse_plan('{"update":[{"id":"s2","depends_on":["s0"]}]}').to_patch())
    assert bp.steps["s2"].status == StepStatus.PASSED        # 终态不改
    assert bp.steps["s3"].status == StepStatus.RETRY         # 不级联


def test_remove_step_cascades_non_done_descendants():
    bp = _chain()
    _set(bp, "s1", StepStatus.PASSED)
    _set(bp, "s2", StepStatus.ESCALATED)
    _set(bp, "s3", StepStatus.RETRY)
    bp.apply_patch(parse_plan('{"remove":["s2"]}').to_patch())
    assert "s2" not in bp.steps
    assert bp.steps["s3"].depends_on == []                   # 引用已清
    assert bp.steps["s3"].status == StepStatus.PENDING       # 后代级联重置
