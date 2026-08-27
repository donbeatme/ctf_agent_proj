"""评估点 1：计划评审 Agent。

在执行前检查计划 DAG 的结构、依赖和验收步骤。离线模式使用确定性规则；在线模式
追加 LLM 语义评审，但结构错误始终会强制返回 revise。
"""

import json
import re
from dataclasses import asdict
from typing import Any, Dict, List, Set

from ..integrations.llm_chat import LlmChatClient, LlmChatResult
from ..schemas import CTFAttempt, PlanEvaluation, PlanStep


PLAN_REVIEW_SYSTEM = """你是计划评审 Agent。在执行前评审规划 Agent 产出的计划 DAG，检查结构是否完整、可执行。

【可用的上下文】
- 任务：题面原文与目标列表（Task）
- 计划 DAG：全部步骤（instruction/criterion/status/attempts/depends_on/skill_id）
- 执行历史：已完成的步骤轨迹（如有，判断与计划的偏差）

【评审要点】
- 目标覆盖：步骤是否覆盖任务目标
- 依赖顺序：depends_on 是否合理、无环、无悬空引用
- 验收标准：每步 criterion 是否可检验、可自动化判定
- 回退策略：失败时是否有可执行的修复路径
- 工具/技能绑定：skill_id 是否与步骤内容匹配

【输出】
只返回一行 JSON：{"decision":"pass"|"revise","score":0..1,"issues":[...],"suggestions":[...]}
- pass：计划结构完整、可执行；revise：存在需修订的结构问题。
- 非阻塞性建议放入 suggestions，不阻塞通过。
- 不解题、不猜 flag；只评审计划结构合理性"""


class PlanEvaluator:
    def __init__(self, llm: LlmChatClient):
        self.llm = llm
        self.last_usage: dict | None = None

    async def evaluate(self, attempt: CTFAttempt, ctx: str = "") -> PlanEvaluation:
        structural = self._structural_review(attempt.plan)
        if not self.llm.available:
            return structural

        try:
            result: LlmChatResult = await self.llm.complete([
                {"role": "system", "content": PLAN_REVIEW_SYSTEM},
                {"role": "user", "content": json.dumps({
                "engine_context": ctx,
                "objective": attempt.metadata.get("problem_statement", ""),
                "retrieved_memory": attempt.metadata.get(
                    "planning_memory_context", ""
                ),
                "plan": [asdict(step) for step in attempt.plan],
                }, ensure_ascii=False)},
            ])
            raw = result.content
            self.last_usage = result.usage
        except Exception as exc:
            # 在线模型不可用不能阻塞规划器；保留结构评审，并显式标记降级。
            structural.evaluator = (
                "PlanEvaluator/offline-rules (online-fallback:%s)"
                % type(exc).__name__
            )
            structural.suggestions = self._unique(structural.suggestions + [
                "LLM 计划语义评审失败，当前结果来自本地结构规则",
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
        # 决策与理由一致性:revise 却无任何结构化 issue/suggestion → 保留原始输出诊断,
        # 避免 _plan_opinion 用"计划结构和验收条件完整"这类误导性兜底文本。
        if decision == "revise" and not issues and not suggestions:
            issues.append("评审未给出结构化修订项,原始输出: %s" % (raw or "").strip()[:500])
        return PlanEvaluation(
            decision=decision,
            score=round(min(structural.score, semantic_score), 4),
            issues=self._unique(issues),
            suggestions=self._unique(suggestions),
            evaluator="PlanEvaluator/LlmApi",
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

    # 匹配 markdown 自由文本里的明确判定,如 "判定：**pass**" / "评审结论：revise"。
    # 只认显式关键字:解析不出时若 LLM 明确说 pass,不能误判成 revise(否则 ep 无限回环)。
    _DECISION_RE = re.compile(
        r"(?:判定|结论|decision|verdict)[：:]\s*[*]{0,2}(pass|revise)[*]{0,2}",
        re.IGNORECASE,
    )

    @staticmethod
    def _parse_json(raw: str) -> Dict[str, Any]:
        text = (raw or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
        result = None
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                result = parsed
        except json.JSONDecodeError:
            pass
        if result is not None:
            if isinstance(result.get("decision"), str):
                return result
            # JSON 缺 decision 字段 → 用文本里的显式判定兜底
            extracted = PlanEvaluator._extract_decision(text)
            result["decision"] = extracted["decision"]
            return result
        return PlanEvaluator._extract_decision(raw or "")

    @staticmethod
    def _extract_decision(raw: str) -> Dict[str, Any]:
        m = PlanEvaluator._DECISION_RE.search(raw)
        if m:
            return {"decision": m.group(1).lower(), "issues": [raw], "suggestions": []}
        # 无明确判定 → 保守 revise(不臆测 pass),原文本留给 issues 诊断。
        return {"decision": "revise", "issues": [raw], "suggestions": []}

    @staticmethod
    def _clamp(value: float) -> float:
        return max(0.0, min(1.0, value))

    @staticmethod
    def _unique(items: List[str]) -> List[str]:
        return list(dict.fromkeys(item for item in items if item))
