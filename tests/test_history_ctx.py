"""测试真实 planner + 导出 ctx 中 history 组件渲染内容到文件。"""
import os
import sys

# 切到项目根目录(确保 config.json / runs 路径正确)
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.workspace import MockWorkspace
from agent.planner import Planner
from agent.schema import (
    PlannerInput, PlannerMode, TaskInput, Feedback,
    StateContext, EvalSource, EvalEvent, Goal,
)
from agent.ctx import HistoryComponent, AgentCommComponent, TraceComponent


def build_workspace_with_history():
    """构造一个带多轮历史事件的 workspace,模拟真实 engine 运行过的场景。"""
    ws = MockWorkspace()

    # ---- 第0轮: 初始规划 ----
    ws.add_event("planner", "replan", state="PLANNING",
                 reason="base64编码任务,拆为读题→编码→提交三步",
                 source="initial", changes="add s1,s2,s3")

    # ---- 第1轮: s1执行+验收通过 ----
    ws.record_tool_call("s1", "read_file", {"path": "/challenge/README.md"})
    ws.record_tool_result("s1", "read_file",
                          "题目:请将字符串 'CTF{hello_world}' 进行base64编码后提交",
                          args={"path": "/challenge/README.md"})
    ws.record_step("s1", "pass",
                   observation="读取成功,待编码文本为 CTF{hello_world}",
                   result={"text": "CTF{hello_world}"}, attempts=1)

    # s2执行+验收失败→重试
    ws.record_tool_call("s2", "base64_encode", {"input": "CTF{hello_world}"})
    ws.record_tool_result("s2", "base64_encode",
                          "Q1RGe2hlbGxvX3dvcmxkfQ==",
                          args={"input": "CTF{hello_world}"})
    ws.add_event("evaluator_step", "step_eval", step_id="s2", verdict="retry",
                 opinion="s2: base64结果需验证可逆性,建议用base64 -d反解验证")

    # s2重试: 先验证可逆性
    ws.record_tool_call("s2", "base64_decode",
                        {"input": "Q1RGe2hlbGxvX3dvcmxkfQ=="})
    ws.record_tool_result("s2", "base64_decode",
                          "CTF{hello_world}",
                          args={"input": "Q1RGe2hlbGxvX3dvcmxkfQ=="})
    ws.record_step("s2", "pass",
                   observation="编码结果可逆,与原文本一致",
                   result={"encoded": "Q1RGe2hlbGxvX3dvcmxkfQ=="}, attempts=2)

    # s3执行: 提交flag → 失败
    ws.record_tool_call("s3", "submit_flag",
                        {"flag": "Q1RGe2hlbGxvX3dvcmxkfQ=="})
    ws.record_tool_result("s3", "submit_flag",
                          "错误: 请直接提交原始字符串的base64,不要带CTF{}前缀",
                          args={"flag": "Q1RGe2hlbGxvX3dvcmxkfQ=="})
    ws.add_event("evaluator_step", "step_eval", step_id="s3", verdict="retry",
                 opinion="s3: 平台提示不要带CTF{}前缀,需重新编码hello_world部分")

    # ---- 第2轮: 重规划(因s3失败触发) ----
    ws.add_event("planner", "replan", state="PLANNING",
                 reason="s3提交失败:平台要求去掉CTF{}前缀,需新增s4步骤仅编码hello_world",
                 source="step_eval", changes="add s4; update s3")

    # s4执行: 编码hello_world
    ws.record_tool_call("s4", "base64_encode", {"input": "hello_world"})
    ws.record_tool_result("s4", "base64_encode",
                          "aGVsbG9fd29ybGQ=",
                          args={"input": "hello_world"})

    # 同时评审未通过:ep觉得s3指令不够精确
    ws.add_event("evaluator_plan", "plan_review", verdict="fail",
                 opinion="s3指令'提交编码后的flag'太模糊,建议明确为'提交s4产出的base64结果到平台'")

    # ---- 第3轮: 再次重规划(因ep FAIL触发) ----
    ws.add_event("planner", "replan", state="PLANNING",
                 reason="ep: s3指令模糊,更新instruction明确引用s4产出",
                 source="plan_review", changes="update s3.instruction")

    # ---- 当前状态: s4刚执行完,等待s3重跑 ----
    ws.record_step("s4", "pass",
                   observation="编码hello_world成功",
                   result={"encoded": "aGVsbG9fd29ybGQ="}, attempts=1)

    # 最新一轮评估意见(当前上下文)
    ws.add_event("evaluator_step", "step_eval", step_id="s4", verdict="pass",
                 opinion="s4: 编码完成")
    ws.add_event("evaluator_plan", "plan_review", verdict="pass",
                 opinion="计划修订后步骤清晰,可以继续执行")

    return ws


def main():
    ws = build_workspace_with_history()

    # ===== 用真实 planner 做一次 REVISE =====
    planner = Planner(workspace=ws)
    print(">>> 调用真实 DeepSeek planner (REVISE 模式)...")

    pin = PlannerInput(
        mode=PlannerMode.REVISE,
        task_input=TaskInput(
            raw_content={"title": "base64编码", "description": "对文本做base64编码并提交"},
            goal_list=[Goal(id="获取flag")],
        ),
        feedback=Feedback(
            dag={
                "steps": {
                    "s1": {"id": "s1", "instruction": "读题", "criterion": "拿到文本",
                           "depends_on": [], "status": "PASSED", "attempts": 1,
                           "result": {"text": "CTF{hello_world}"}},
                    "s2": {"id": "s2", "instruction": "编码", "criterion": "可逆",
                           "depends_on": ["s1"], "status": "PASSED", "attempts": 2,
                           "result": {"encoded": "Q1RGe2hlbGxvX3dvcmxkfQ=="}},
                    "s3": {"id": "s3", "instruction": "提交编码后的flag到平台",
                           "criterion": "平台返回正确", "depends_on": ["s2"], "status": "PENDING"},
                    "s4": {"id": "s4", "instruction": "仅编码hello_world(去掉CTF{}前缀)",
                           "criterion": "产出aGVsbG9fd29ybGQ=", "depends_on": ["s1"],
                           "status": "PASSED", "attempts": 1,
                           "result": {"encoded": "aGVsbG9fd29ybGQ="}},
                }
            },
            turn=[
                EvalEvent(source=EvalSource.STEP_EVAL,
                          opinion="s4: 编码hello_world完成,结果正确"),
                EvalEvent(source=EvalSource.PLAN_REVIEW,
                          opinion="当前计划各步骤指令明确,可继续执行s3提交"),
            ],
            state_context=StateContext(
                trigger="plan_review_fail",
                detail="ep在第2轮评审未通过(s3指令模糊),已修订;当前评审通过",
            ),
        ),
    )

    bp = planner.plan(pin)
    print(f">>> planner 返回 DAG, {len(bp.steps)} 步: {list(bp.steps)}")
    print(f"    reason: {bp.meta.get('reason', '')[:120]}")

    # ===== 导出 context =====
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_ctx_out")
    os.makedirs(out_dir, exist_ok=True)

    # 完整 ctx
    ctx, system, over = ws.assembler.assemble(
        "planner",
        raw_content={"title": "base64编码", "description": "对文本做base64编码并提交"},
        goal_list=[Goal(id="获取flag")],
        system="(system prompt omitted for brevity)",
    )

    with open(f"{out_dir}/ctx_full.txt", "w", encoding="utf-8") as f:
        f.write("=== SYSTEM ===\n")
        f.write(system)
        f.write("\n\n=== CTX ===\n")
        f.write(ctx)
        if over:
            f.write(f"\n\n=== OVER BUDGET: {over} chars ===\n")

    # 单独导出 history 组件(3个档位)
    for comp in ws.assembler.components("planner"):
        if comp.key == "history":
            comp.level = 0
            raw = comp.render()
            comp.level = 1
            index = comp.render()
            comp.level = 2
            summary = comp.render()

            with open(f"{out_dir}/ctx_history.txt", "w", encoding="utf-8") as f:
                f.write("=== HISTORY (raw) ===\n")
                f.write(raw)
                f.write("\n\n=== HISTORY (index) ===\n")
                f.write(index)
                f.write("\n\n=== HISTORY (summary) ===\n")
                f.write(summary)
            print(f">>> history 已写入 {out_dir}/ctx_history.txt ({len(raw)} chars)")
            break

    # agent_comm
    for comp in ws.assembler.components("planner"):
        if comp.key == "agent_comm":
            with open(f"{out_dir}/ctx_agent_comm.txt", "w", encoding="utf-8") as f:
                f.write(comp.render())
            break

    # trace
    for comp in ws.assembler.components("planner"):
        if comp.key == "trace":
            with open(f"{out_dir}/ctx_trace.txt", "w", encoding="utf-8") as f:
                f.write(comp.render())
            break

    print(f">>> 全部输出写入 {out_dir}/")
    print("    ctx_full.txt       — 完整上下文")
    print("    ctx_history.txt    — history 组件(3档)")
    print("    ctx_agent_comm.txt — 本轮评估意见")
    print("    ctx_trace.txt      — 本轮工具轨迹")


if __name__ == "__main__":
    main()
