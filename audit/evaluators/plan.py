"""评估点 1：计划评审 Agent。

在执行前检查计划 DAG 的结构、依赖和验收步骤。离线模式使用确定性规则；在线模式
追加 DeepSeek 语义评审，但结构错误始终会强制返回 revise。
"""

import json
from dataclasses import asdict
from typing import Any, Dict, List, Set

from ..integrations.deepseek import DeepSeekChat
from ..schemas import CTFAttempt, PlanEvaluation, PlanStep


PLAN_REVIEW_SYSTEM = """You review an authorized CTF agent plan before execution.
Check goal coverage, dependency ordering, evidence collection, fallback strategy, and final answer
verification. Do not solve the CTF or invent a flag. Return strict JSON with decision (pass/revise),
score (0..1), issues (array), and suggestions (array)."""


class PlanEvaluator:
    def __init__(self, llm: DeepSeekChat):
        self.llm = llm

    def evaluate(self, attempt: CTFAttempt) -> PlanEvaluation:
        structural = self._structural_review(attempt.plan)
        if not self.llm.available:
            return structural

        try:
            raw = self.llm.complete([
                {"role": "system", "content": PLAN_REVIEW_SYSTEM},
                {"role": "user", "content": json.dumps({
                "objective": attempt.metadata.get("problem_statement", ""),
                "retrieved_memory": attempt.metadata.get(
                    "planning_memory_context", ""
                ),
                "plan": [asdict(step) for step in attempt.plan],
                }, ensure_ascii=False)},
            ])
        except Exception as exc:
            # 在线模型不可用不能阻塞规划器；保留结构评审，并显式标记降级。
            structural.evaluator = (
                "PlanEvaluator/offline-rules (online-fallback:%s)"
                % type(exc).__name__
            )
            structural.suggestions = self._unique(structural.suggestions + [
                "DeepSeek 计划语义评审失败，当前结果来自本地结构规则",
            ])
            return structural
        parsed = self._parse_json(raw)
        try:
            semantic_score = self._clamp(float(parsed.get("score", structural.score)))
        except (TypeError, ValueError):
            semantic_score = structural.score
        semantic_decision = str(parsed.get("decision", "revise")).lower()
        issues = structural.issues + [str(item) for item in parsed.get("issues", [])]
        suggestions = structural.suggestions + [str(item) for item in parsed.get("suggestions", [])]
        # LLM 不能覆盖确定性的 DAG/字段错误。
        decision = "revise" if structural.decision == "revise" or semantic_decision != "pass" else "pass"
        return PlanEvaluation(
            decision=decision,
            score=round(min(structural.score, semantic_score), 4),
            issues=self._unique(issues),
            suggestions=self._unique(suggestions),
            evaluator="PlanEvaluator/DeepSeek",
        )

    @staticmethod
    def _structural_review(plan: List[PlanStep]) -> PlanEvaluation:
        issues: List[str] = []
        suggestions: List[str] = []
        score = 0.0
        if plan:
            score += 0.15
        else:
            issues.append("计划为空")
            suggestions.append("至少提供一个带 goal 和 action 的计划节点")

        ids = [step.plan_step_id for step in plan]
        if plan and all(ids) and len(ids) == len(set(ids)):
            score += 0.15
        else:
            issues.append("计划节点 ID 为空或重复")

        known = set(ids)
        invalid_dependencies = [
            dependency for step in plan for dependency in step.depends_on
            if dependency not in known or dependency == step.plan_step_id
        ]
        if not invalid_dependencies:
            score += 0.15
        else:
            issues.append("存在无效或自引用依赖：%s" % ", ".join(invalid_dependencies))

        if not PlanEvaluator._has_cycle(plan):
            score += 0.15
        else:
            issues.append("计划依赖存在环")
            suggestions.append("把计划改为可拓扑排序的 DAG")

        if plan and all(
            step.goal.strip()
            and step.action.strip()
            and step.instruction.strip()
            and step.criterion.strip()
            for step in plan
        ):
            score += 0.15
        else:
            issues.append(
                "部分计划节点缺少 goal、action、instruction 或 criterion"
            )
            suggestions.append("为每个节点补充明确执行指令和可检验验收标准")

        verification_markers = ("verify", "check", "validate", "验证", "校验", "核对")
        has_verification = any(
            any(marker in (
                "%s %s %s %s %s" % (
                    step.goal,
                    step.action,
                    step.instruction,
                    step.criterion,
                    step.tool or "",
                )
            ).lower()
                for marker in verification_markers)
            for step in plan
        )
        if has_verification:
            score += 0.25
        else:
            issues.append("计划缺少最终答案验证步骤")
            suggestions.append("在提交前增加独立 flag_verifier 节点")

        decision = "pass" if score >= 0.8 and not issues else "revise"
        return PlanEvaluation(decision, round(score, 4), issues, suggestions, "PlanEvaluator/offline-rules")

    @staticmethod
    def _has_cycle(plan: List[PlanStep]) -> bool:
        graph = {step.plan_step_id: step.depends_on for step in plan}
        visiting: Set[str] = set()
        visited: Set[str] = set()

        def visit(node: str) -> bool:
            if node in visiting:
                return True
            if node in visited:
                return False
            visiting.add(node)
            for dependency in graph.get(node, []):
                if dependency in graph and visit(dependency):
                    return True
            visiting.remove(node)
            visited.add(node)
            return False

        return any(visit(node) for node in graph)

    @staticmethod
    def _parse_json(raw: str) -> Dict[str, Any]:
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
        try:
            result = json.loads(text)
            return result if isinstance(result, dict) else {}
        except json.JSONDecodeError:
            return {"decision": "revise", "issues": [raw], "suggestions": []}

    @staticmethod
    def _clamp(value: float) -> float:
        return max(0.0, min(1.0, value))

    @staticmethod
    def _unique(items: List[str]) -> List[str]:
        return list(dict.fromkeys(item for item in items if item))
