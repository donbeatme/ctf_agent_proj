"""评估 Agent 接口桩(ep 计划评审 / ee 步骤校验 / et 任务反思)。

评估 Agent 由外部团队实现,③ 只调用接口并处理其结果(verdict/意见)。
未接入前用 MockEvaluator:构造时按角色传入要返回的内容,方便测试不同场景。
"""

from dataclasses import dataclass
from enum import StrEnum

from agent.schema import GoalEvalDetail, Role


class Verdict(StrEnum):
    """评估判定。按角色合法取值:
    ep → PASS|FAIL;ee → PASS|RETRY|ESCALATE;et → DONE|REPLAN。"""
    PASS = "pass"
    FAIL = "fail"
    RETRY = "retry"
    ESCALATE = "escalate"
    DONE = "done"
    REPLAN = "replan"


@dataclass
class EvalResult:
    """评估 Agent 的输出。engine 组装 PlannerInput.turn 时映射成 EvalEvent。"""
    verdict: Verdict
    opinion: str                 # 意见正文;ee 以 "sN:" 点名步骤
    observation: str | None = None  # 仅 ee 携带执行观察/产物摘要
    is_completed: bool = False   # 仅 ee:任务是否已完成(不以全部节点终态为准,由 ee 判定)
    total_usage: dict | None = None  # {prompt_tokens, completion_tokens, total_tokens}


class Evaluator:
    """接口桩:按角色评估,返回 EvalResult。

    eval_goals 由 step_eval agent 在步骤 PASS 后调用:比对 goal list 与当前世界模型
    (DAG),引用 DAG 节点作为证据,返回每个 goal 的完成判定。
    """

    def review(self, ctx: str) -> EvalResult:      # ep 计划评审
        raise NotImplementedError

    def step_eval(self, ctx: str) -> EvalResult:   # ee 步骤校验
        raise NotImplementedError

    def reflect(self, ctx: str) -> EvalResult:     # et 任务反思
        raise NotImplementedError

    def eval_goals(self, ctx: str, goals: list[dict], dag_summary: str) -> list[GoalEvalDetail]:
        """评估 goal list 中未完成的 goal 是否已达成(引用 DAG 节点作证据)。"""
        raise NotImplementedError


class MockEvaluator(Evaluator):
    """可配置返回内容的评估 mock。responses: {角色名: EvalResult 或 callable(ctx)->EvalResult}。
    goal_responses: 可选,goal 评估返回值列表或 callable(ctx, goals, dag_summary)->list[GoalEvalDetail]。
    """

    def __init__(self, responses: dict[str, EvalResult] | None = None,
                 goal_responses: list[GoalEvalDetail] | None = None):
        self._responses = responses or {}
        self._goal_responses = goal_responses or []

    def _get(self, role: str, ctx: str) -> EvalResult:
        r = self._responses.get(role)
        if r is None:
            raise RuntimeError(f"MockEvaluator 未配置 {role} 的返回")
        return r(ctx) if callable(r) else r

    def review(self, ctx: str) -> EvalResult:
        return self._get(Role.EVALUATOR_PLAN, ctx)

    def step_eval(self, ctx: str) -> EvalResult:
        return self._get(Role.EVALUATOR_STEP, ctx)

    def reflect(self, ctx: str) -> EvalResult:
        return self._get(Role.EVALUATOR_TASK, ctx)

    def eval_goals(self, ctx: str, goals: list[dict], dag_summary: str) -> list[GoalEvalDetail]:
        if callable(self._goal_responses):
            return self._goal_responses(ctx, goals, dag_summary)
        return list(self._goal_responses)
