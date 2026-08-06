import argparse
import json

from agent.evaluator import EvalResult, Verdict
from agent.schema import GoalEvalDetail

_MOCK_TASK = {
    "task_id": "mock-0001",
    "ground_id": "g-mock",
    "challenge_id": "c-mock",
    "title": "base64 编码",
    "description": "给定一段文本,base64 编码后作为 flag 提交。",
}


class SmokeEvaluator:
    """端到端冒烟的 mock 评估:ep 按真实 blueprint 判空(空计划要重规划),
    ee/et 固定放行——主循环用真实 planner 跑通,其余 agent 全部 mock。"""

    def __init__(self, ws):
        self._ws = ws

    def review(self, ctx):
        bp = self._ws.blueprint
        if bp is None or not bp.steps:
            return EvalResult(Verdict.FAIL, "计划为空,请重新规划")
        return EvalResult(Verdict.PASS, "计划可执行(mock)")

    def step_eval(self, ctx):
        return EvalResult(Verdict.PASS, "步骤验收通过(mock)")

    def reflect(self, ctx):
        return EvalResult(Verdict.DONE, "反思: 无问题(mock)")

    def eval_goals(self, ctx, goals, dag_summary):
        """mock:全部 PASSED 步骤作为证据,认为 goal 已达成(冒烟只验证链路不验证判定)。"""
        steps = self._ws.blueprint.steps if self._ws.blueprint else {}
        evidence = [sid for sid, s in steps.items() if s.status.value == "PASSED"]
        return [GoalEvalDetail(goal_id=g["id"], complete=bool(evidence), evidence=evidence,
                               reasoning="mock: 步骤全 PASS")
                for g in goals]


def run_task(args):
    """端到端冒烟:真实 Planner(默认 llm_call 走 llm_api.chat_with_tools)+ mock 其他 agent。

    主循环完全真实:planner 规划→评审→调度→执行→验收→重规划→反思;ep 按真实
    blueprint 判空驱动重规划,executor/ee/et 走 mock。跑的是真模型,需要已配 LLM key。
    """
    import time

    from agent.engine import Engine
    from agent.executor import MockExecutor
    from agent.planner import Planner
    from agent.workspace import Workspace

    task = json.loads(args.task) if args.task else _MOCK_TASK
    run_id = args.run_id or f"run-{time.strftime('%Y%m%d-%H%M%S')}"
    ws = Workspace.create(run_id, task)
    engine = Engine(
        Planner(workspace=ws),
        MockExecutor(observation="(mock) 执行完成"),
        SmokeEvaluator(ws),
        workspace=ws,
    )
    try:
        engine.run(task)
    except Exception as exc:
        print(f"run 异常(模型输出/计划非法?): {type(exc).__name__}: {exc}")
        return
    print(f"run_id: {run_id}")
    print(f"终态: {engine.scheduler.state.value}  重规划 {engine.replans} 次")
    if engine.fail_reason:
        print(f"失败原因: {engine.fail_reason}")
    print("\n最终计划:")
    for sid, s in engine.bp.steps.items():
        print(f"  {sid}\t{s.status.value}\tattempts={s.attempts}\t依赖={s.depends_on}")
        print(f"       instruction: {s.instruction}")
        print(f"       criterion:   {s.criterion}")


def main():
    parser = argparse.ArgumentParser(description="CTF2 ReAct agent")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("run-task", help="端到端冒烟:真实 planner + mock 执行/评估")
    p.add_argument("--task", help="任务 dict(JSON);缺省用内置示例题")
    p.add_argument("--run-id", help="run 目录名(缺省按时间生成)")

    args = parser.parse_args()

    if args.cmd == "run-task":
        run_task(args)


if __name__ == "__main__":
    main()
