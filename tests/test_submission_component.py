"""提交状态公用组件(SubmissionComponent):executor 提交 flag 后 → workspace.meta["submission"]
→ ee/et 上下文可见(提交判定是 ee 判任务完成的核心证据)。

覆盖:
- 正确/错误/仅记录/异常四态渲染
- 注册给 EVALUATOR_STEP / EVALUATOR_TASK(ee/et 看得到)
- 非提交角色(planner)不投影提交状态
"""

from agent.schema import Role
from agent.workspace import MockWorkspace


async def _ctx_has_submission(ws, role):
    ctx, _, _ = await ws.assembler.assemble(role)
    return "# 已提交 flag" in ctx


async def test_correct_submission_renders():
    ws = MockWorkspace()
    ws.record_submission({"flag": "CTF{x}", "ok": True, "correct": True,
                          "message": "success"})
    ctx, _, _ = await ws.assembler.assemble(Role.EVALUATOR_STEP)
    assert "# 已提交 flag" in ctx
    assert "CTF{x}" in ctx
    assert "正确(平台确认)" in ctx
    assert "success" in ctx


async def test_wrong_submission_renders():
    ws = MockWorkspace()
    ws.record_submission({"flag": "CTF{wrong}", "ok": True, "correct": False,
                          "message": "INCORRECT_FLAG"})
    ctx, _, _ = await ws.assembler.assemble(Role.EVALUATOR_STEP)
    assert "错误(平台拒绝)" in ctx
    assert "INCORRECT_FLAG" in ctx


async def test_record_only_submission():
    ws = MockWorkspace()
    ws.record_submission({"flag": "CTF{x}"})   # 无 adapter:仅记录
    ctx, _, _ = await ws.assembler.assemble(Role.EVALUATOR_STEP)
    assert "未判定(仅记录,无平台确认)" in ctx


async def test_submission_exception_renders():
    ws = MockWorkspace()
    ws.record_submission({"flag": "CTF{x}", "ok": False, "correct": None,
                          "message": "提交异常: Boom"})
    ctx, _, _ = await ws.assembler.assemble(Role.EVALUATOR_STEP)
    assert "未判定(提交异常)" in ctx
    assert "Boom" in ctx


async def test_registered_for_ee_and_et():
    ws = MockWorkspace()
    ws.record_submission({"flag": "CTF{x}", "correct": True})
    assert await _ctx_has_submission(ws, Role.EVALUATOR_STEP)
    assert await _ctx_has_submission(ws, Role.EVALUATOR_TASK)


async def test_not_projected_without_submission():
    ws = MockWorkspace()
    assert not await _ctx_has_submission(ws, Role.EVALUATOR_STEP)


async def test_planner_does_not_see_submission():
    ws = MockWorkspace()
    ws.record_submission({"flag": "CTF{x}", "correct": True})
    ctx, _, _ = await ws.assembler.assemble(Role.PLANNER)
    assert "# 已提交 flag" not in ctx


async def test_reset_clears_submission():
    ws = MockWorkspace()
    ws.record_submission({"flag": "CTF{x}", "correct": True})
    ws.reset()
    assert "submission" not in ws.meta
    assert not await _ctx_has_submission(ws, Role.EVALUATOR_STEP)


async def test_record_submission_keeps_authoritative_correct():
    """平台已确认 correct=True 后,赢后重提(correct=None, ALREADY_SOLVED)不覆盖权威判定。"""
    ws = MockWorkspace()
    ws.record_submission({"flag": "CTF{x}", "ok": True, "correct": True,
                          "message": "提交成功,答案正确"})
    # 冗余步骤再次提交 → ALREADY_SOLVED, correct=None,不得洗掉已确认的判定
    ws.record_submission({"flag": "CTF{x}", "ok": True, "correct": None,
                          "message": "该题已解决且为动态 flag,无法本地判定"})
    assert ws.meta["submission"]["correct"] is True
    ctx, _, _ = await ws.assembler.assemble(Role.EVALUATOR_STEP)
    assert "正确(平台确认)" in ctx
    # 明确的错误判定仍可作为新的权威结论覆盖
    ws.record_submission({"flag": "CTF{wrong}", "ok": True, "correct": False,
                          "message": "提交错误"})
    assert ws.meta["submission"]["correct"] is False


async def test_record_submission_unknown_after_unknown_overwrites():
    """没有已确认判定时(correct=None),普通覆盖语义不变。"""
    ws = MockWorkspace()
    ws.record_submission({"flag": "CTF{a}", "ok": True, "correct": None,
                          "message": "仅记录"})
    ws.record_submission({"flag": "CTF{b}", "ok": True, "correct": None,
                          "message": "仍无判定"})
    assert ws.meta["submission"]["flag"] == "CTF{b}"


async def test_anchor_never_compressed():
    ws = MockWorkspace()
    ws.record_submission({"flag": "CTF{x}", "correct": True})
    await ws.assembler.assemble(Role.EVALUATOR_STEP)
    comps = ws.assembler.components(Role.EVALUATOR_STEP)
    sub = next(c for c in comps if c.key == "submission")
    assert sub.anchor is True
    assert sub.can_advance() is False
