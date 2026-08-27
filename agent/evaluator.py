"""评估 Agent 接口桩(ep 计划评审 / ee 步骤校验 / et 任务反思)。

评估 Agent 由外部团队实现,③ 只调用接口并处理其结果(verdict/意见)。
未接入前用 MockEvaluator:构造时按角色传入要返回的内容,方便测试不同场景。
"""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum

from agent import llm_api
from agent.schema import GoalEvalDetail, Role
from model_config import get as cfg_get, get_engine_config


class Verdict(StrEnum):
    """评估判定。按角色合法取值:
    ep → PASS|FAIL;ee → PASS|RETRY|ESCALATE;et → DONE|REPLAN。"""
    PASS = "pass"
    FAIL = "fail"
    RETRY = "retry"
    ESCALATE = "escalate"
    DONE = "done"
    REPLAN = "replan"


class Diagnosis(StrEnum):
    """ee 对"未达成"的结构化分类:失败属于哪一类,引擎据此分流处理。

    INCOMPLETE    执行 Agent 未在 8 轮工具调用内达成验收标准(进度不足/卡壳)
    DRIFT         执行方向偏了(解题路径偏离,但步骤目标本身正确)
    PLANNER_TARGET 步骤目标/验收标准本身设计有误,重跑也达不成,需重设计该步
    OTHER         其它情况(默认)
    """

    INCOMPLETE = "incomplete"
    DRIFT = "drift"
    PLANNER_TARGET = "planner_target"
    OTHER = "other"


@dataclass
class EvalResult:
    """评估 Agent 的输出。engine 组装 PlannerInput.turn 时映射成 EvalEvent。"""
    verdict: Verdict
    opinion: str                 # 意见正文;ee 以 "sN:" 点名步骤
    observation: str | None = None  # 仅 ee 携带执行观察/产物摘要
    is_completed: bool = False   # 仅 ee:任务是否已完成(不以全部节点终态为准,由 ee 判定)
    total_usage: dict | None = None  # {prompt_tokens, completion_tokens, total_tokens}
    diagnosis: Diagnosis = Diagnosis.OTHER  # 仅 ee:失败分类(引擎路由重试策略/单节点重设计)


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

    def system_for(self, role) -> str:
        """该评估器在指定角色下使用的系统提示词(engine 装配 SystemPromptComponent 用)。"""
        return ""


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


class SmokeEvaluator(Evaluator):
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

    def system_for(self, role):
        return ""

    def eval_goals(self, ctx, goals, dag_summary):
        """mock:全部 PASSED 步骤作为证据,认为 goal 已达成(冒烟只验证链路不验证判定)。"""
        steps = self._ws.blueprint.steps if self._ws.blueprint else {}
        evidence = [sid for sid, s in steps.items() if s.status.value == "PASSED"]
        return [GoalEvalDetail(goal_id=g["id"], complete=bool(evidence), evidence=evidence,
                               reasoning="mock: 步骤全 PASS")
                for g in goals]


# ── 轻量 LLM 评审(ep/ee/et)─────────────────────────────────────────
# 每个角色单轮 llm_api.chat 评引擎装配好的 ctx,输出严格 JSON;verdict 收进角色合法集。

ROLE_SYSTEMS: dict[str, str] = {
    Role.EVALUATOR_PLAN: (
        "你是计划评审 Agent。评审下面给出的执行计划 DAG 是否结构完整、可执行、覆盖任务目标。\n"
        "【输出】只返回一行 JSON:{\"verdict\":\"pass\"|\"fail\",\"opinion\":\"评审意见\"}\n"
        "- pass: 计划可执行;fail: 计划需重规划。不解题、不猜 flag。"
    ),
    Role.EVALUATOR_STEP: (
        "你是步骤验收 Agent。验收执行 Agent 对当前步骤的产出是否达成验收标准。\n"
        "【可用上下文】任务、当前步骤(instruction/criterion/status/attempts)、"
        "提交判定(submission correct=true 表示该步产出已被平台确认)、本轮评估意见、执行历史。\n"
        "【输出】只返回一行 JSON:{\"verdict\":\"pass\"|\"retry\"|\"escalate\","
        "\"diagnosis\":\"incomplete\"|\"drift\"|\"planner_target\"|\"other\","
        "\"is_completed\":true|\"false\",\"opinion\":\"验收意见\"}\n"
        "- pass: 步骤达成。is_completed 是'任务整体达成'信号,不是'本步骤达成':"
        "仅当任务核心目标(拿到格式合法 flag)已达成时才置 true,单步 PASS 默认置 false。\n"
        "- 提交判定 correct=true(平台已确认 flag)→ is_completed 置 true。\n"
        "- 动态 flag 容器题(has_container)且存在已验证经验:若执行 Agent 的解题步骤与漏洞信息"
        "(注入点、漏洞类型、oracle、过滤绕过、提取方法)与已验证经验一致,且已提取格式合法的 flag,"
        "即使提交判定 correct=None(平台未确认/鉴权失败),也可置 is_completed=true。\n"
        "- 侦察/探测等前置步骤即使 PASS 也不置 is_completed=true(任务未达核心目标)。\n"
        "- retry: 未达成但有修复空间;escalate: 证据不足/无法达成。\n"
        "- diagnosis 是'未达成原因'的三分类分析,pass 时置 other;未达成必须归入下列之一:\n"
        "  - incomplete:执行 Agent 在 8 轮工具调用内未达成验收标准(进度不足/卡壳/工具循环超上限)→ verdict 应置 retry。\n"
        "  - drift:执行 Agent 解题方向偏了(路径/思路偏离,但步骤目标本身正确)→ verdict 应置 retry。\n"
        "  - planner_target:步骤的 instruction/criterion 本身设计有误,重跑该步也无法达成,需重设计该步骤目标 → verdict 应置 escalate。\n"
        "  - other:其它情况(默认)。\n"
        "不解题、不猜 flag;基于证据给出 verdict + diagnosis + opinion。"
    ),
    Role.EVALUATOR_TASK: (
        "你是任务反思 Agent。任务结束(完成或失败)时全局校验,决定 DONE 或重规划。\n"
        "【可用上下文】任务、计划 DAG 全量步骤状态、执行历史、本轮评估意见、提交判定。\n"
        "【输出】只返回一行 JSON:{\"verdict\":\"done\"|\"replan\",\"opinion\":\"反思意见\"}\n"
        "- done: 目标已达成(如提交判定 correct=true);replan: 存在未达成关键步骤,给出重规划方向。\n"
        "- done 亦可在动态 flag 容器题下达成:解题过程经已验证经验核对一致且提取出格式合法 flag,"
        "即使平台提交判定未确认(correct=None)。\n"
        "不猜 flag;基于证据与过程指标全局评估。"
    ),
}


GOAL_EVAL_SYSTEM = (
    "你是目标达成评估 Agent。评估单个任务目标当前是否已达成。\n"
    "【可用上下文】任务、目标 id、DAG 各步骤状态与产物、最近执行上下文/观察、提交判定。\n"
    "【输出】只返回一行 JSON:{\"complete\":true|\"false\","
    "\"evidence\":[\"step_id\",...],\"reasoning\":\"推理\"}\n"
    "- complete 仅当有可引用证据:已提取格式合法 flag、提交判定 correct=true、"
    "或关键步骤 PASSED 且其产物达成该目标。\n"
    "- evidence 是支撑判定的 DAG step_id 列表,可为空。\n"
    "- 保守:证据不足置 complete=false,不臆测、不猜 flag。"
)


def _parse_json(raw: str) -> dict:
    """剥 ``` 围栏后解析 JSON;失败回 {}。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def _sum_usage(log: list[dict]) -> dict | None:
    if not log:
        return None
    return {
        "prompt_tokens": sum(u.get("prompt_tokens", 0) for u in log),
        "completion_tokens": sum(u.get("completion_tokens", 0) for u in log),
        "total_tokens": sum(u.get("total_tokens", 0) for u in log),
    }


class _LLMEvaluator(Evaluator):
    """轻量 LLM 评审基类:单轮 llm_api.chat(评 ctx),verdict 收进角色合法集,失败走默认。"""

    role: str = ""
    default_verdict: Verdict = Verdict.PASS
    legal: frozenset = frozenset()

    def _call(self, ctx: str, system: str | None = None,
              drain_usage: bool = True) -> tuple[dict, str, dict | None]:
        try:
            raw = llm_api.chat(system=system or ROLE_SYSTEMS.get(self.role, ""),
                               prompt=ctx, model=llm_api.role_model(self.role))
            usage = _sum_usage(llm_api.pop_token_log()) if drain_usage else None
            return _parse_json(raw), raw, usage
        except Exception as exc:  # noqa: BLE001 — LLM 故障不能阻塞评估,走兜底
            return {}, f"(LLM 调用失败: {type(exc).__name__}: {exc})", None

    def _coerce(self, value, default=None) -> Verdict:
        v = str(value or "").strip().lower()
        if v in self.legal:
            return Verdict(v)
        return default or self.default_verdict

    @staticmethod
    def _coerce_diagnosis(value) -> Diagnosis:
        v = str(value or "").strip().lower()
        if v in {d.value for d in Diagnosis}:
            return Diagnosis(v)
        return Diagnosis.OTHER

    def _result(self, parsed, raw, usage, opinion_field="opinion", **extra) -> EvalResult:
        verdict = self._coerce(parsed.get("verdict"))
        opinion = str(parsed.get(opinion_field) or "").strip()[:500]
        if not opinion:
            if raw.startswith("(LLM 调用失败"):
                opinion = raw[:500]
            else:
                opinion = raw[:200] if raw else f"(LLM {self.role} 未给出意见文本)"
        return EvalResult(verdict=verdict, opinion=opinion, total_usage=usage, **extra)

    def system_for(self, role) -> str:
        return ROLE_SYSTEMS.get(role, "")


class PlanLLMEvaluator(_LLMEvaluator):
    """ep 计划评审:pass/fail;解析失败 PASS(不阻塞执行)。"""

    role = Role.EVALUATOR_PLAN
    default_verdict = Verdict.PASS
    legal = frozenset({"pass", "fail"})

    def review(self, ctx: str) -> EvalResult:
        parsed, raw, usage = self._call(ctx)
        return self._result(parsed, raw, usage)


class StepLLMEvaluator(_LLMEvaluator):
    """ee 步骤验收:pass/retry/escalate + is_completed;解析失败 RETRY。"""

    role = Role.EVALUATOR_STEP
    default_verdict = Verdict.RETRY
    legal = frozenset({"pass", "retry", "escalate"})

    def step_eval(self, ctx: str) -> EvalResult:
        parsed, raw, usage = self._call(ctx)
        is_completed = bool(parsed.get("is_completed"))
        diagnosis = self._coerce_diagnosis(parsed.get("diagnosis"))
        return self._result(parsed, raw, usage, is_completed=is_completed,
                            diagnosis=diagnosis)

    def eval_goals(self, ctx: str, goals: list[dict], dag_summary: str) -> list[GoalEvalDetail]:
        """逐 goal LLM 软鉴定:每个未达成 goal 独立调用 LLM(可线程池并行),保序返回。

        每个 goal 的判定互不依赖 → 同一决策点多个独立 LLM 调用,ThreadPoolExecutor 并行。
        LLM 失败/解析失败保守收口 complete=False(不臆测达成),仅引擎层聚合用量(_llm_wrap)。
        """
        if not goals:
            return []
        n = max(1, min(len(goals),
                       int(get_engine_config().get("goal_eval_max_workers", 4) or 1)))
        if n == 1:
            return [self._call_goal(g, ctx, dag_summary) for g in goals]
        with ThreadPoolExecutor(max_workers=n) as pool:
            return list(pool.map(lambda g: self._call_goal(g, ctx, dag_summary), goals))

    def _call_goal(self, goal, ctx: str, dag_summary: str) -> GoalEvalDetail:
        """单个 goal 的 LLM 软鉴定。drain_usage=False:不抢全局 token log,用量留给引擎聚合。"""
        goal_id = str(goal.get("id", ""))
        prompt = (
            f"【目标】{goal_id}\n\n"
            f"【世界模型(DAG)】\n{dag_summary or '(无计划)'}\n\n"
            f"【执行上下文】\n{ctx}\n"
        )
        parsed, raw, _ = self._call(prompt, system=GOAL_EVAL_SYSTEM, drain_usage=False)
        complete = bool(parsed.get("complete"))
        evidence = parsed.get("evidence")
        evidence = [str(x) for x in evidence if x] if isinstance(evidence, list) else []
        reasoning = str(parsed.get("reasoning") or "").strip()[:500]
        if not reasoning:
            reasoning = raw[:200] if raw else f"(goal {goal_id} 评估未给出推理)"
        return GoalEvalDetail(goal_id=goal_id, complete=complete,
                              evidence=evidence, reasoning=reasoning)


class TaskLLMEvaluator(_LLMEvaluator):
    """et 任务反思:done/replan;解析失败 DONE。"""

    role = Role.EVALUATOR_TASK
    default_verdict = Verdict.DONE
    legal = frozenset({"done", "replan"})

    def reflect(self, ctx: str) -> EvalResult:
        parsed, raw, usage = self._call(ctx)
        return self._result(parsed, raw, usage)


class ConfigurableEvaluator(Evaluator):
    """按角色从 delegates 分发评估;eval_goals 委托 ee 委托者(引擎 GOAL_EVAL→EVALUATOR_STEP)。"""

    def __init__(self, delegates: dict[str, Evaluator]):
        self._delegates = delegates or {}
        self._fallback = MockEvaluator({})

    def _get(self, role: str) -> Evaluator:
        return self._delegates.get(role, self._fallback)

    def review(self, ctx: str) -> EvalResult:
        return self._get(Role.EVALUATOR_PLAN).review(ctx)

    def step_eval(self, ctx: str) -> EvalResult:
        return self._get(Role.EVALUATOR_STEP).step_eval(ctx)

    def reflect(self, ctx: str) -> EvalResult:
        return self._get(Role.EVALUATOR_TASK).reflect(ctx)

    def eval_goals(self, ctx: str, goals: list[dict], dag_summary: str) -> list[GoalEvalDetail]:
        return self._get(Role.EVALUATOR_STEP).eval_goals(ctx, goals, dag_summary)

    def system_for(self, role) -> str:
        return self._get(role).system_for(role)


def build_evaluator(ws, modes: dict[str, str] | None = None) -> ConfigurableEvaluator:
    """按 config 构造分角色评估器。modes: {role: 'real'|'mock'};缺省读
    EVALUATOR_PLAN/STEP/TASK(env 优先,model_config.json 兜底,默认 mock)。
    real → 轻量 LLM 评审;mock → SmokeEvaluator。"""
    if modes is None:
        modes = {
            Role.EVALUATOR_PLAN: cfg_get("EVALUATOR_PLAN") or "mock",
            # ee 默认常开 real:is_completed 由 ee 软鉴定置位,关掉(→mock)则无法收口 DONE
            Role.EVALUATOR_STEP: cfg_get("EVALUATOR_STEP") or "real",
            Role.EVALUATOR_TASK: cfg_get("EVALUATOR_TASK") or "mock",
        }
    real = {
        Role.EVALUATOR_PLAN: PlanLLMEvaluator(),
        Role.EVALUATOR_STEP: StepLLMEvaluator(),
        Role.EVALUATOR_TASK: TaskLLMEvaluator(),
    }
    mock = SmokeEvaluator(ws)
    delegates = {
        role: (real[role] if str(mode).strip().lower() == "real" else mock)
        for role, mode in modes.items()
    }
    return ConfigurableEvaluator(delegates)
