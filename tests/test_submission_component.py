"""提交状态公用组件(SubmissionComponent):executor 提交 flag 后 → workspace.meta["submission"]
→ ee/et 上下文可见(提交判定是 ee 判任务完成的核心证据)。

覆盖:
- 正确/错误/仅记录/异常四态渲染
- 注册给 EVALUATOR_STEP / EVALUATOR_TASK(ee/et 看得到)
- 非提交角色(planner)不投影提交状态
"""

from agent.schema import Role
from agent.workspace import MockWorkspace


def _ctx_has_submission(ws, role):
    ctx, _, _ = ws.assembler.assemble(role)
    return "# 已提交 flag" in ctx


def test_correct_submission_renders():
    ws = MockWorkspace()
    ws.record_submission({"flag": "CTF{x}", "ok": True, "correct": True,
                          "message": "success"})
    ctx, _, _ = ws.assembler.assemble(Role.EVALUATOR_STEP)
    assert "# 已提交 flag" in ctx
    assert "CTF{x}" in ctx
    assert "正确(平台确认)" in ctx
    assert "success" in ctx


def test_wrong_submission_renders():
    ws = MockWorkspace()
    ws.record_submission({"flag": "CTF{wrong}", "ok": True, "correct": False,
                          "message": "INCORRECT_FLAG"})
    ctx, _, _ = ws.assembler.assemble(Role.EVALUATOR_STEP)
    assert "错误(平台拒绝)" in ctx
    assert "INCORRECT_FLAG" in ctx


def test_record_only_submission():
    ws = MockWorkspace()
    ws.record_submission({"flag": "CTF{x}"})   # 无 adapter:仅记录
    ctx, _, _ = ws.assembler.assemble(Role.EVALUATOR_STEP)
    assert "未判定(仅记录,无平台确认)" in ctx


def test_submission_exception_renders():
    ws = MockWorkspace()
    ws.record_submission({"flag": "CTF{x}", "ok": False, "correct": None,
                          "message": "提交异常: Boom"})
    ctx, _, _ = ws.assembler.assemble(Role.EVALUATOR_STEP)
    assert "未判定(提交异常)" in ctx
    assert "Boom" in ctx


def test_registered_for_ee_and_et():
    ws = MockWorkspace()
    ws.record_submission({"flag": "CTF{x}", "correct": True})
    assert _ctx_has_submission(ws, Role.EVALUATOR_STEP)
    assert _ctx_has_submission(ws, Role.EVALUATOR_TASK)


def test_not_projected_without_submission():
    ws = MockWorkspace()
    assert not _ctx_has_submission(ws, Role.EVALUATOR_STEP)


def test_planner_does_not_see_submission():
    ws = MockWorkspace()
    ws.record_submission({"flag": "CTF{x}", "correct": True})
    ctx, _, _ = ws.assembler.assemble(Role.PLANNER)
    assert "# 已提交 flag" not in ctx


def test_reset_clears_submission():
    ws = MockWorkspace()
    ws.record_submission({"flag": "CTF{x}", "correct": True})
    ws.reset()
    assert "submission" not in ws.meta
    assert not _ctx_has_submission(ws, Role.EVALUATOR_STEP)


def test_anchor_never_compressed():
    ws = MockWorkspace()
    ws.record_submission({"flag": "CTF{x}", "correct": True})
    ws.assembler.assemble(Role.EVALUATOR_STEP)
    comps = ws.assembler.components(Role.EVALUATOR_STEP)
    sub = next(c for c in comps if c.key == "submission")
    assert sub.anchor is True
    assert sub.can_advance() is False
