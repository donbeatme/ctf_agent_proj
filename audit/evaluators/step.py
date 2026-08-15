"""评估点 2：执行步骤验收 Agent。

一个逻辑步骤由 ``tool_call + tool_result`` 构成。离线规则根据成功、重复和证据
决定 pass/retry/escalate；在线模式逐步骤调用 AgentEvals，并保留确定性状态约束。
任何在线异常都只影响当前评估器，不能中断 CTF 任务或整批审计。
"""

import json
from typing import Any, Dict, List, Optional, Tuple

from ..integrations.deepseek import DeepSeekChat
from ..integrations.langsmith_logger import redact
from ..schemas import (
    CTFAttempt,
    StepEvaluation,
    StepEvaluationItem,
    StepKind,
    TrajectoryStep,
)
from ..settings import Settings


STEP_PROMPT = """Evaluate one step of an authorized CTF agent execution. Judge whether the tool
choice and observed result make progress toward the objective. Do not solve the challenge or infer
a flag. Return a continuous process-quality score from 0 to 1. Trajectory: {outputs}"""


class StepAcceptanceEvaluator:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._cached_online_evaluator: Any = None
        self._online_disabled_reasons: Dict[str, str] = {}

    def begin_attempt(self, attempt_id: str) -> None:
        """开始任务时清除该任务遗留的熔断状态。"""
        self._online_disabled_reasons.pop(attempt_id, None)

    def end_attempt(self, attempt_id: str) -> None:
        """任务完成或中止时释放该任务的在线降级状态。"""
        self._online_disabled_reasons.pop(attempt_id, None)

    def evaluate(self, attempt: CTFAttempt) -> StepEvaluation:
        """批量兼容入口；事件驱动 pipeline 使用 evaluate_step。"""
        pairs = self.pairs(attempt)
        seen = set()
        items: List[StepEvaluationItem] = []
        step_ids = list(attempt.metadata.get("external_step_ids", []))

        for position, (call, result) in enumerate(pairs):
            signature = (
                call.tool_name or "",
                json.dumps(call.tool_args, sort_keys=True, ensure_ascii=False),
            )
            repeated = signature in seen
            seen.add(signature)
            step_id = (
                str(step_ids[position])
                if position < len(step_ids)
                else "step-%d" % (position + 1)
            )
            items.append(
                self.evaluate_step(
                    attempt,
                    call,
                    result,
                    step_id,
                    repeated,
                    position,
                )
            )
        return self.summarize(items)

    def evaluate_step(
        self,
        attempt: CTFAttempt,
        call: TrajectoryStep,
        result: Optional[TrajectoryStep],
        step_id: str,
        repeated: bool = False,
        position: int = 0,
    ) -> StepEvaluationItem:
        """工具执行完成事件的即时验收入口，只评价当前这一个步骤。"""
        if self.settings.mode == "offline":
            score, evaluator, llm_reason = self._offline_score(call, result, repeated)
        elif attempt.attempt_id in self._online_disabled_reasons:
            # 前一个步骤已经证明在线链路不可用，本次直接降级并保留原始原因。
            fallback_reason = self._online_disabled_reasons[attempt.attempt_id]
            score, _, offline_reason = self._offline_score(call, result, repeated)
            evaluator = "StepEvaluator/offline-rules (online-fallback:%s)" % fallback_reason
            llm_reason = "%s；在线 AgentEvals 已熔断，继续降级：%s" % (
                offline_reason,
                fallback_reason,
            )
        else:
            try:
                online_evaluator = self._online_evaluator()
                response = online_evaluator(
                    outputs=self._messages(attempt, call, result, position),
                )
                score = self._response_score(response)
                evaluator = "AgentEvals/DeepSeek"
                llm_reason = self._response_reason(response)
            except Exception as exc:  # 第三方 SDK/API 失败必须降级，不能终止任务。
                print("\n[StepEvaluator ONLINE ERROR]")
                print("type:", type(exc).__name__)
                print("message:", repr(str(exc)))
                print("repr:", repr(exc))
                fallback_reason = self._error_label(exc)
                self._online_disabled_reasons[attempt.attempt_id] = fallback_reason
                score, _, offline_reason = self._offline_score(call, result, repeated)
                evaluator = "StepEvaluator/offline-rules (online-fallback:%s)" % fallback_reason
                llm_reason = "%s；在线 AgentEvals 失败，已降级：%s" % (
                    offline_reason,
                    fallback_reason,
                )

        success = None if result is None else result.success
        observed_text = call.content + " " + (result.content if result else "")
        suspicious_guess = "guess" in observed_text.lower()
        if success is True and score >= 0.6:
            decision = "pass"
        elif success is False and (repeated or suspicious_guess or score < 0.15):
            decision = "escalate"
        else:
            decision = "retry"
        return StepEvaluationItem(
            step_id=str(step_id),
            decision=decision,
            score=round(score, 4),
            tool=call.tool_name or "unknown_tool",
            success=success,
            reasoning=self._reason(
                decision,
                success,
                repeated,
                suspicious_guess,
                llm_reason,
            ),
            evaluator=evaluator,
        )

    @staticmethod
    def summarize(items: List[StepEvaluationItem]) -> StepEvaluation:
        """任务结束时只做统计，不重新调用任何步骤 evaluator。"""
        normalized = list(items)
        if not normalized:
            normalized.append(
                StepEvaluationItem(
                    step_id="no-step",
                    decision="escalate",
                    score=0.0,
                    tool="none",
                    success=None,
                    reasoning="没有可验收的 tool_call/tool_result",
                    evaluator="StepEvaluator/offline-rules",
                )
            )
        online = any(item.evaluator == "AgentEvals/DeepSeek" for item in normalized)
        fallback = any("online-fallback:" in item.evaluator for item in normalized)
        if online and fallback:
            evaluator = "AgentEvals/DeepSeek + offline-fallback"
        elif online:
            evaluator = "AgentEvals/DeepSeek"
        elif fallback:
            evaluator = "StepEvaluator/offline-rules (online-fallback)"
        else:
            evaluator = "StepEvaluator/offline-rules"
        return StepEvaluation(
            score=round(sum(item.score for item in normalized) / len(normalized), 4),
            pass_count=sum(item.decision == "pass" for item in normalized),
            retry_count=sum(item.decision == "retry" for item in normalized),
            escalate_count=sum(item.decision == "escalate" for item in normalized),
            items=normalized,
            evaluator=evaluator,
        )

    def _online_evaluator(self) -> Any:
        if self._cached_online_evaluator is not None:
            return self._cached_online_evaluator
        if not self.settings.deepseek_api_key:
            raise RuntimeError("MissingDeepSeekApiKey")
        try:
            from agentevals.trajectory.llm import create_trajectory_llm_as_judge
        except ImportError as exc:
            raise RuntimeError("AgentEvalsNotInstalled") from exc
        judge = DeepSeekChat(self.settings).agentevals_client()
        self._cached_online_evaluator = create_trajectory_llm_as_judge(
            prompt=STEP_PROMPT,
            judge=judge,
            model=self.settings.deepseek_model,
            continuous=True,
        )
        return self._cached_online_evaluator

    @staticmethod
    def _response_score(response: Any) -> float:
        if not isinstance(response, dict):
            raise ValueError("AgentEvalsInvalidResponse")
        score = float(response.get("score", 0.0))
        return max(0.0, min(1.0, score))

    @staticmethod
    def _response_reason(response: Dict[str, Any]) -> str:
        return str(response.get("comment") or response.get("reasoning") or "")

    @staticmethod
    def _error_label(exc: Exception) -> str:
        """日志只记录稳定的异常类型，避免把 API 响应或密钥写入报告。"""
        message = str(exc)
        known_labels = ("MissingDeepSeekApiKey", "AgentEvalsNotInstalled")
        if message in known_labels:
            return message
        return type(exc).__name__

    @staticmethod
    def pairs(
        attempt: CTFAttempt,
    ) -> List[Tuple[TrajectoryStep, Optional[TrajectoryStep]]]:
        """公开给历史 JSON 回放器使用，不执行任何评价。"""
        pairs: List[Tuple[TrajectoryStep, Optional[TrajectoryStep]]] = []
        pending: Optional[TrajectoryStep] = None
        for event in attempt.steps:
            if event.kind == StepKind.TOOL_CALL:
                if pending is not None:
                    pairs.append((pending, None))
                pending = event
            elif event.kind == StepKind.TOOL_RESULT and pending is not None:
                pairs.append((pending, event))
                pending = None
        if pending is not None:
            pairs.append((pending, None))
        return pairs

    @staticmethod
    def _offline_score(
        call: TrajectoryStep,
        result: Optional[TrajectoryStep],
        repeated: bool,
    ) -> Tuple[float, str, str]:
        if result is None:
            return 0.0, "StepEvaluator/offline-rules", "工具调用没有对应结果"
        suspicious_guess = "guess" in (call.content + " " + result.content).lower()
        if result.success is True:
            return 0.9, "StepEvaluator/offline-rules", "工具执行成功并产生观察"
        if result.success is False and (repeated or suspicious_guess):
            return 0.05, "StepEvaluator/offline-rules", "失败步骤重复或基于猜测，应升级处理"
        if result.success is False:
            return 0.25, "StepEvaluator/offline-rules", "工具失败，可在修正策略后重试"
        return 0.5, "StepEvaluator/offline-rules", "步骤状态未知，需要人工或额外证据"

    @staticmethod
    def _messages(
        attempt: CTFAttempt,
        call: TrajectoryStep,
        result: Optional[TrajectoryStep],
        position: int,
    ) -> List[Dict[str, Any]]:
        call_id = "call_%s_%d" % (attempt.attempt_id, position)
        messages: List[Dict[str, Any]] = [
            {
                "role": "user",
                "content": str(
                    attempt.metadata.get("problem_statement", attempt.task_id)
                ),
            },
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": call.tool_name or "unknown_tool",
                            "arguments": json.dumps(
                                redact(call.tool_args),
                                ensure_ascii=False,
                            ),
                        },
                    }
                ],
            },
        ]
        if result is not None:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": redact(result.content),
                }
            )
        return messages

    @staticmethod
    def _reason(
        decision: str,
        success: Optional[bool],
        repeated: bool,
        guess: bool,
        detail: str,
    ) -> str:
        facts = ["decision=%s" % decision, "success=%s" % success]
        if repeated:
            facts.append("相同工具和参数已重复")
        if guess:
            facts.append("步骤包含无证据猜测")
        if detail:
            facts.append(detail)
        return "；".join(facts)
