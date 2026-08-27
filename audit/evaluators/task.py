"""评估点 3：任务级全局校验与 Reflexion。

最终 pass/fail 由独立 FlagVerifier 决定；计划/步骤评估用于诊断和计划补丁。失败或
过程存在 revise/retry/escalate 时生成 Reflexion，供经验库复用。
"""

import json
from dataclasses import asdict
from typing import Any, Dict, List

from ..integrations.langsmith_logger import redact
from ..integrations.llm_chat import LlmChatClient, LlmChatResult
from ..schemas import (
    AttemptMetrics, CTFAttempt, FlagResult, PlanEvaluation, Reflection,
    StepEvaluation, StepKind, TaskEvaluation,
)


REFLEXION_SYSTEM = """你是任务反思 Agent。在任务结束（完成或失败）时全局校验，决定 DONE 或重规划。

【可用的上下文】
- 任务：题面原文与目标列表（Task）
- 计划 DAG：全量步骤及其状态（Dag）
- 执行历史：全部步骤轨迹（History）
- 本轮评估意见：全部非 pass 意见（AgentComm）
- 提交判定：已提交 flag 的平台判定（Submission）

【决策依据】
- flag 已获平台确认（correct=true）且目标达成 → DONE
- 存在未达成的关键步骤 / 已升级 → REPLAN，给出最早的关键失误诊断与补丁方向

【输出】
- DONE / REPLAN 判定 + 诊断 diagnosis + 经验 lessons + 补丁方向 next_plan
- 不猜 flag；基于证据与过程指标全局评估"""


class TaskReflectionEvaluator:
    strategy = "LAST_ATTEMPT_AND_REFLEXION"

    def __init__(self, llm: LlmChatClient):
        self.llm = llm
        self.last_usage: dict | None = None

    async def evaluate(
        self,
        attempt: CTFAttempt,
        plan: PlanEvaluation,
        steps: StepEvaluation,
        flag: FlagResult,
        metrics: AttemptMetrics,
        ctx: str = "",
    ) -> TaskEvaluation:
        process_has_issue = plan.decision == "revise" or steps.retry_count > 0 or steps.escalate_count > 0
        # valid=None(无判定来源)不再算"失败":动态 flag/未配置规则时提交过即可 pass,避免 REPLAN 死循环
        needs_reflection = flag.valid is False or process_has_issue
        reflection = await self._reflect(attempt, plan, steps, flag, metrics, ctx=ctx) if needs_reflection else None
        if flag.valid is True:
            reason = "flag 验证通过"
            if process_has_issue:
                reason += "，但过程存在需修订或升级的步骤"
            decision = "pass"
        elif flag.valid is False:
            decision = "fail"
            reason = flag.reason
        elif flag.submitted:
            decision = "pass"
            reason = "无本地判定来源(动态 flag/未配置规则)，但已提交 flag，未确认正确性"
        else:
            decision = "fail"
            reason = "未提交 flag，任务未完成"
        return TaskEvaluation(decision=decision, reason=reason, reflection=reflection)

    async def _reflect(
        self,
        attempt: CTFAttempt,
        plan: PlanEvaluation,
        steps: StepEvaluation,
        flag: FlagResult,
        metrics: AttemptMetrics,
        ctx: str = "",
    ) -> Reflection:
        if not self.llm.available:
            return self._offline_reflection(attempt, plan, steps, flag, metrics)
        payload = {
            "task_id": attempt.task_id,
            "category": attempt.category,
            "engine_context": ctx,
            "plan_evaluation": asdict(plan),
            "step_evaluation": asdict(steps),
            "flag_valid": flag.valid,
            "metrics": asdict(metrics),
            "attempt": redact(attempt.to_dict()),
        }
        try:
            result: LlmChatResult = await self.llm.complete([
                {"role": "system", "content": REFLEXION_SYSTEM},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ])
            raw = result.content
            self.last_usage = result.usage
        except Exception as exc:
            # Reflexion 是增强项，模型/API 故障时必须返回确定性的本地反思。
            reflection = self._offline_reflection(attempt, plan, steps, flag, metrics)
            reflection.source += " (online-fallback:%s)" % type(exc).__name__
            return reflection
        parsed = self._parse_json(raw)
        return Reflection(
            attempt_id=attempt.attempt_id,
            diagnosis=str(parsed.get("diagnosis", "模型未给出有效诊断")),
            lessons=[str(item) for item in parsed.get("lessons", [])][:8],
            next_plan=[str(item) for item in parsed.get("next_plan", [])][:8],
            source="TaskReflection/Reflexion/%s/LlmApi" % self.strategy,
        )

    @staticmethod
    def _offline_reflection(
        attempt: CTFAttempt,
        plan: PlanEvaluation,
        steps: StepEvaluation,
        flag: FlagResult,
        metrics: AttemptMetrics,
    ) -> Reflection:
        failed_tools = [event for event in attempt.steps if event.kind == StepKind.TOOL_RESULT and event.success is False]
        if failed_tools:
            diagnosis = "工具失败后未能充分恢复：%s" % failed_tools[0].content
            lessons = ["失败后必须改变参数、路径或证据来源，不能原样重复"]
        elif flag.valid is False:
            diagnosis = "最终答案未通过独立 flag 验证"
            lessons = ["只提交能够被工具观察直接支持的候选值"]
        elif flag.valid is None:
            diagnosis = "最终答案缺少独立判定来源(动态 flag/未配置规则)"
            lessons = ["为任务补充静态 flag 规则或接入平台提交判定"]
        elif plan.decision == "revise":
            diagnosis = "计划在执行前缺少必要的验收或依赖约束"
            lessons = ["执行前先修订计划 DAG"]
        else:
            diagnosis = "执行过程存在需要重试或升级的步骤"
            lessons = ["把步骤验收结论反馈给调度器"]
        next_plan: List[str] = list(plan.suggestions) or [
            "补充证据采集与独立验证节点",
            "失败步骤先修正策略再重试",
            "连续失败时升级人工审核",
        ]
        return Reflection(
            attempt.attempt_id, diagnosis, lessons, next_plan,
            "TaskReflection/Reflexion/offline-rules",
        )

    @staticmethod
    def _parse_json(raw: str) -> Dict[str, Any]:
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
        try:
            value = json.loads(text)
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {"diagnosis": raw, "lessons": [], "next_plan": []}
