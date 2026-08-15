"""评估点 3：任务级全局校验与 Reflexion。

最终 pass/fail 由独立 FlagVerifier 决定；计划/步骤评估用于诊断和计划补丁。失败或
过程存在 revise/retry/escalate 时生成 Reflexion，供经验库复用。
"""

import json
from dataclasses import asdict
from typing import Any, Dict, List

from ..integrations.deepseek import DeepSeekChat
from ..integrations.langsmith_logger import redact
from ..schemas import (
    AttemptMetrics, CTFAttempt, FlagResult, PlanEvaluation, Reflection,
    StepEvaluation, StepKind, TaskEvaluation,
)


REFLEXION_SYSTEM = """You are the final reflection evaluator for an authorized CTF agent.
Given plan review, step acceptance, final validation, and metrics, identify the earliest consequential
mistake and produce a safer plan patch. Never invent or guess a flag. Return strict JSON with
diagnosis (string), lessons (array), and next_plan (array)."""


class TaskReflectionEvaluator:
    strategy = "LAST_ATTEMPT_AND_REFLEXION"

    def __init__(self, llm: DeepSeekChat):
        self.llm = llm

    def evaluate(
        self,
        attempt: CTFAttempt,
        plan: PlanEvaluation,
        steps: StepEvaluation,
        flag: FlagResult,
        metrics: AttemptMetrics,
    ) -> TaskEvaluation:
        process_has_issue = plan.decision == "revise" or steps.retry_count > 0 or steps.escalate_count > 0
        needs_reflection = not flag.valid or process_has_issue
        reflection = self._reflect(attempt, plan, steps, flag, metrics) if needs_reflection else None
        if flag.valid:
            reason = "flag 验证通过"
            if process_has_issue:
                reason += "，但过程存在需修订或升级的步骤"
            decision = "pass"
        else:
            decision = "fail"
            reason = flag.reason
        return TaskEvaluation(decision=decision, reason=reason, reflection=reflection)

    def _reflect(
        self,
        attempt: CTFAttempt,
        plan: PlanEvaluation,
        steps: StepEvaluation,
        flag: FlagResult,
        metrics: AttemptMetrics,
    ) -> Reflection:
        if not self.llm.available:
            return self._offline_reflection(attempt, plan, steps, flag, metrics)
        payload = {
            "task_id": attempt.task_id,
            "category": attempt.category,
            "plan_evaluation": asdict(plan),
            "step_evaluation": asdict(steps),
            "flag_valid": flag.valid,
            "metrics": asdict(metrics),
            "attempt": redact(attempt.to_dict()),
        }
        try:
            raw = self.llm.complete([
                {"role": "system", "content": REFLEXION_SYSTEM},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ])
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
            source="TaskReflection/Reflexion/%s/DeepSeek" % self.strategy,
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
        elif not flag.valid:
            diagnosis = "最终答案未通过独立 flag 验证"
            lessons = ["只提交能够被工具观察直接支持的候选值"]
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
