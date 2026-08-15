"""Adapters from the existing agent interfaces to the audit implementation.

This module depends on the public ``DocStore`` and ``Evaluator`` contracts. It
does not patch or mutate the agent package.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional

from agent.blueprint import Blueprint, Step
from agent.evaluator import EvalResult, Evaluator, Verdict
from agent.planner import DocStore

from .evaluators import PlanEvaluator, StepAcceptanceEvaluator, TaskReflectionEvaluator
from .flag_verifier import FlagVerifier
from .integrations.deepseek import DeepSeekChat
from .metrics import calculate_attempt_metrics
from .schemas import (
    AuditRecord,
    CTFAttempt,
    PlanEvaluation,
    PlanStep,
    StepEvaluationItem,
    StepKind,
    TrajectoryStep,
    utc_now,
)
from .settings import Settings


AUDIT_PLAN_META_KEY = "audit_plan_fields"


def blueprint_to_plan(blueprint: Blueprint) -> List[PlanStep]:
    """Convert the colleague-owned Blueprint without changing its classes."""
    audit_fields = blueprint.meta.get(AUDIT_PLAN_META_KEY, {})
    if not isinstance(audit_fields, dict):
        audit_fields = {}
    plan = []
    for step_id, step in blueprint.steps.items():
        preserved = audit_fields.get(step_id, {})
        if not isinstance(preserved, dict):
            preserved = {}
        plan.append(PlanStep(
            plan_step_id=step_id,
            goal=str(preserved.get("goal") or step.instruction),
            action=str(preserved.get("action") or step.instruction),
            instruction=str(step.instruction),
            criterion=str(step.criterion),
            tool=(
                str(preserved["tool"])
                if preserved.get("tool") is not None
                else step.skill_id
            ),
            depends_on=list(step.depends_on),
        ))
    return plan


def plan_to_blueprint(
    plan: Iterable[PlanStep],
    meta: Optional[Dict[str, Any]] = None,
) -> Blueprint:
    """Convert an audit plan while preserving audit-only fields in metadata."""
    blueprint = Blueprint(meta=deepcopy(meta or {}))
    audit_fields = {}
    for item in plan:
        blueprint.add_step(Step(
            id=item.plan_step_id,
            instruction=item.instruction,
            criterion=item.criterion,
            depends_on=list(item.depends_on),
            skill_id=item.tool,
        ))
        audit_fields[item.plan_step_id] = {
            "goal": item.goal,
            "action": item.action,
            "tool": item.tool,
        }
    blueprint.meta[AUDIT_PLAN_META_KEY] = audit_fields
    return blueprint


class AuditExperienceDocStore(DocStore):
    """Expose audit experiences through the Planner's existing DocStore API."""

    def __init__(self, experience_store: Any, agent_id: str, limit: int = 5):
        self._store = experience_store
        self._agent_id = agent_id
        self._limit = max(1, limit)
        self._documents: Dict[str, str] = {}
        self.last_error: Optional[str] = None

    def search(self, task: dict) -> list[tuple[str, str]]:
        objective = str(
            task.get("objective")
            or task.get("description")
            or task.get("title")
            or task.get("task_id")
            or json.dumps(task, ensure_ascii=False)
        )
        category = str(task.get("category") or "unknown")
        query = (
            "查找可复用的 CTF 规划和执行经验；类别：%s；任务：%s；"
            "重点关注 instruction、criterion、失败恢复和最终验证。"
            % (category, objective)
        )
        try:
            rows = self._store.retrieve_experience(
                query=query,
                limit=self._limit,
                agent_id=self._agent_id,
            )
            self.last_error = None
        except Exception as exc:
            cause = exc.__cause__ or exc
            self.last_error = type(cause).__name__
            return []
        documents = []
        for index, row in enumerate(rows, 1):
            doc_id = str(row.get("id") or "audit-experience-%d" % index)
            content = str(row.get("memory") or "")
            if content:
                self._documents[doc_id] = content
                documents.append((doc_id, content))
        return documents

    def load_doc(self, doc_id: str) -> Optional[str]:
        return self._documents.get(doc_id)


@dataclass
class AgentRuntimeBindings:
    """Read-only callbacks exposing current Engine state to the audit adapter."""

    blueprint: Callable[[], Blueprint]
    task: Callable[[], Dict[str, Any]]
    current_step: Callable[[], Step]
    observation: Callable[[], str]
    submitted_flag: Callable[[], Optional[str]]
    completed: Callable[[], bool] = lambda: False
    goal_evaluator: Optional[
        Callable[[str, list[dict], str], list]
    ] = None


class AgentAuditEvaluator(Evaluator):
    """Implement the existing Evaluator API with the audit evaluators."""

    def __init__(
        self,
        settings: Settings,
        verifier: FlagVerifier,
        experience_store: Any,
        run_id: str,
        agent_id: str,
        bindings: AgentRuntimeBindings,
    ):
        self.settings = settings
        self.verifier = verifier
        self.experience_store = experience_store
        self.run_id = run_id
        self.agent_id = agent_id
        self.bindings = bindings
        self.llm = DeepSeekChat(settings)
        self.plan_evaluator = PlanEvaluator(self.llm)
        self.step_evaluator = StepAcceptanceEvaluator(settings)
        self.task_evaluator = TaskReflectionEvaluator(self.llm)
        self.step_evaluator.begin_attempt(run_id)
        self.attempt: Optional[CTFAttempt] = None
        self.plan_evaluation: Optional[PlanEvaluation] = None
        self.step_items: List[StepEvaluationItem] = []
        self.seen_calls = set()
        self.last_record: Optional[AuditRecord] = None
        self.store_error: Optional[str] = None

    def review(self, ctx: str) -> EvalResult:
        attempt = self._ensure_attempt()
        attempt.plan = blueprint_to_plan(self.bindings.blueprint())
        self.plan_evaluation = self.plan_evaluator.evaluate(attempt)
        verdict = (
            Verdict.PASS
            if self.plan_evaluation.decision == "pass"
            else Verdict.FAIL
        )
        opinion = self._plan_opinion(self.plan_evaluation)
        return EvalResult(verdict, opinion)

    def step_eval(self, ctx: str) -> EvalResult:
        attempt = self._ensure_attempt()
        step = self.bindings.current_step()
        observation = str(self.bindings.observation() or "")
        call = TrajectoryStep(
            index=len(attempt.steps),
            kind=StepKind.TOOL_CALL,
            content=step.instruction,
            tool_name=step.skill_id or "agent_executor",
            tool_args={"criterion": step.criterion},
        )
        result = TrajectoryStep(
            index=len(attempt.steps) + 1,
            kind=StepKind.TOOL_RESULT,
            content=observation,
            success=self._infer_success(observation),
        )
        signature = (
            call.tool_name,
            json.dumps(call.tool_args, ensure_ascii=False, sort_keys=True),
        )
        repeated = signature in self.seen_calls
        self.seen_calls.add(signature)
        attempt.steps.extend([call, result])
        item = self.step_evaluator.evaluate_step(
            attempt,
            call,
            result,
            step.id,
            repeated=repeated,
            position=len(self.step_items),
        )
        self.step_items.append(item)
        return EvalResult(
            Verdict(item.decision),
            item.reasoning,
            observation=observation,
            is_completed=bool(self.bindings.completed()),
        )

    def reflect(self, ctx: str) -> EvalResult:
        attempt = self._ensure_attempt()
        if self.plan_evaluation is None:
            attempt.plan = blueprint_to_plan(self.bindings.blueprint())
            self.plan_evaluation = self.plan_evaluator.evaluate(attempt)
        attempt.ended_at = utc_now()
        attempt.submitted_flag = self.bindings.submitted_flag()
        steps = self.step_evaluator.summarize(self.step_items)
        flag = self.verifier.verify(
            attempt.task_id,
            attempt.submitted_flag,
        )
        metrics = calculate_attempt_metrics(
            attempt,
            flag.valid,
            steps.score,
        )
        task = self.task_evaluator.evaluate(
            attempt,
            self.plan_evaluation,
            steps,
            flag,
            metrics,
        )
        self.last_record = AuditRecord(
            attempt=attempt,
            plan_evaluation=self.plan_evaluation,
            step_evaluation=steps,
            flag=flag,
            metrics=metrics,
            task_evaluation=task,
        )
        try:
            self.experience_store.store_experience(self.last_record)
            self.store_error = None
        except Exception as exc:
            self.store_error = type(exc).__name__
        verdict = Verdict.DONE if task.decision == "pass" else Verdict.REPLAN
        opinion = task.reason
        if task.reflection is not None:
            opinion += "；" + task.reflection.diagnosis
        return EvalResult(verdict, opinion)

    def eval_goals(
        self,
        ctx: str,
        goals: list[dict],
        dag_summary: str,
    ) -> list:
        if self.bindings.goal_evaluator is not None:
            return self.bindings.goal_evaluator(ctx, goals, dag_summary)
        return []

    def close(self) -> None:
        self.step_evaluator.end_attempt(self.run_id)

    def _new_attempt(self) -> CTFAttempt:
        task = self.bindings.task()
        task_id = str(task.get("task_id") or task.get("id") or self.run_id)
        return CTFAttempt(
            attempt_id=self.run_id,
            task_id=task_id,
            agent_id=self.agent_id,
            category=str(task.get("category") or "unknown"),
            started_at=utc_now(),
            ended_at=utc_now(),
            steps=[],
            plan=blueprint_to_plan(self.bindings.blueprint()),
            metadata={
                "problem_statement": str(
                    task.get("objective")
                    or task.get("description")
                    or task.get("title")
                    or task_id
                ),
                "difficulty": str(task.get("difficulty") or "unknown"),
                "source": "ctf_agent_proj",
            },
        )

    def _ensure_attempt(self) -> CTFAttempt:
        if self.attempt is None:
            self.attempt = self._new_attempt()
        return self.attempt

    @staticmethod
    def _infer_success(observation: str) -> bool:
        lowered = observation.lower()
        failure_markers = (
            "error", "fail", "timeout", "forbidden", "denied",
            "exception", "失败", "错误", "超时", "拒绝",
        )
        return bool(observation.strip()) and not any(
            marker in lowered for marker in failure_markers
        )

    @staticmethod
    def _plan_opinion(evaluation: PlanEvaluation) -> str:
        details = evaluation.issues or evaluation.suggestions
        return "；".join(details) if details else "计划结构和验收条件完整"
