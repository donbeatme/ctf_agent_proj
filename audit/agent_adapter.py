"""Adapters from the existing agent interfaces to the audit implementation.

This module depends on the public ``DocStore`` and ``Evaluator`` contracts. It
does not patch or mutate the agent package.
"""

from __future__ import annotations

import asyncio
import json
import os
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from agent.blueprint import Blueprint, Step
from agent.evaluator import Diagnosis, EvalResult, Evaluator, Verdict
from agent.planner import DocStore
from agent.schema import Role

from .evaluators import PlanEvaluator, StepAcceptanceEvaluator, TaskReflectionEvaluator
from .evaluators.plan import PLAN_REVIEW_SYSTEM
from .evaluators.step import STEP_PROMPT
from .evaluators.task import REFLEXION_SYSTEM
from .flag_verifier import FlagVerifier
from .integrations.llm_chat import LlmChatClient
from .metrics import calculate_attempt_metrics
from .schemas import (
    AuditRecord,
    CTFAttempt,
    FlagResult,
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
        category = str(task.get("challenge_type") or task.get("category") or "unknown")
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
    submission_result: Optional[
        Callable[[], Optional[dict]]
    ] = lambda: None  # 返回 ws.meta["submission"] {flag, ok, correct, message}:正确性权威=平台/_local_verify


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
        event_sink: Optional[Callable[[str, dict], None]] = None,
        audit_output: Optional[Path] = None,
    ):
        self.settings = settings
        self.verifier = verifier
        self.experience_store = experience_store
        self.run_id = run_id
        self.agent_id = agent_id
        self.bindings = bindings
        self.event_sink = event_sink
        self.audit_output = audit_output
        self.plan_llm = LlmChatClient(settings, role="evaluator_plan")
        self.task_llm = LlmChatClient(settings, role="evaluator_task")
        self.plan_evaluator = PlanEvaluator(self.plan_llm)
        self.step_evaluator = StepAcceptanceEvaluator(settings)
        self.task_evaluator = TaskReflectionEvaluator(self.task_llm)
        self.step_evaluator.begin_attempt(run_id)
        self.attempt: Optional[CTFAttempt] = None
        self.plan_evaluation: Optional[PlanEvaluation] = None
        self.step_items: List[StepEvaluationItem] = []
        self.seen_calls = set()
        self.last_record: Optional[AuditRecord] = None
        self.store_error: Optional[str] = None
        self.audit_written: Optional[Path] = None

    def system_for(self, role) -> str:
        """该评估器在指定角色下使用的系统提示词(engine 装配 SystemPromptComponent 用)。"""
        return {
            Role.EVALUATOR_PLAN: PLAN_REVIEW_SYSTEM,
            Role.EVALUATOR_STEP: STEP_PROMPT,
            Role.EVALUATOR_TASK: REFLEXION_SYSTEM,
        }.get(role, "")

    async def review(self, ctx: str) -> EvalResult:
        attempt = self._ensure_attempt()
        attempt.plan = blueprint_to_plan(self.bindings.blueprint())
        self.plan_evaluation = await self.plan_evaluator.evaluate(attempt, ctx=ctx)
        verdict = (
            Verdict.PASS
            if self.plan_evaluation.decision == "pass"
            else Verdict.FAIL
        )
        opinion = self._plan_opinion(self.plan_evaluation)
        self._emit("audit_plan_review", {
            "decision": self.plan_evaluation.decision,
            "score": self.plan_evaluation.score,
            "issues": self.plan_evaluation.issues,
            "suggestions": self.plan_evaluation.suggestions,
            "opinion": self.plan_evaluation.opinion,
        })
        return EvalResult(
            verdict,
            opinion,
            total_usage=self.plan_evaluator.last_usage,
        )

    async def step_eval(self, ctx: str) -> EvalResult:
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
        item = await self.step_evaluator.evaluate_step(
            attempt,
            call,
            result,
            step.id,
            repeated=repeated,
            position=len(self.step_items),
            ctx=ctx,
        )
        self.step_items.append(item)
        # 平台已确认提交正确 → 该步(含赢后冗余步)直接验收通过并判任务完成:
        # 修正 _infer_success 关键词误判(observation 含 error/forbidden 也被判失败),
        # 也让 is_completed 有真实语义(不再自引用 engine.task_completed 的死代码)。
        submission = self.bindings.submission_result() or {}
        if submission.get("correct") is True:
            item.decision = "pass"
            item.score = max(item.score, 0.9)
            item.reasoning = f"{item.reasoning}；平台已确认提交正确，强制验收通过"
            diagnosis = Diagnosis.OTHER
            is_completed = True
        else:
            is_completed = bool(self.bindings.completed())
            diagnosis = self._classify(item, observation, step, repeated)
        self._emit("audit_step_eval", {
            "step_id": step.id,
            "decision": item.decision,
            "score": item.score,
            "reasoning": item.reasoning,
            "diagnosis": diagnosis.value,
        })
        return EvalResult(
            Verdict(item.decision),
            item.reasoning,
            observation=observation,
            is_completed=is_completed,
            diagnosis=diagnosis,
            total_usage=self.step_evaluator.last_usage,
        )

    @staticmethod
    def _classify(item: StepEvaluationItem, observation: str, step, repeated: bool) -> Diagnosis:
        """未达成原因三分类,驱动引擎分流(engine.STEP_EVAL 路由)。离线启发式。

        - 工具循环达上限 → INCOMPLETE:执行未完成,retry 继承前几轮完整 ctx(不按关键词判 pass)。
        - 同工具+参数反复失败 → DRIFT:方向偏,retry 走压缩 ctx,纠偏意见已随 reasoning 落 agent_comm。
        - 该步重试耗尽仍失败 → PLANNER_TARGET:步骤目标/验收设计有问题,escalate 单节点重设计。
        """
        obs = observation or ""
        if "工具循环超上限" in obs:
            item.decision = "retry"
            item.reasoning = f"{item.reasoning}；执行 Agent 工具循环达上限,步骤未完成,retry 继承前几轮 ctx".strip("；")
            return Diagnosis.INCOMPLETE
        if item.success is False and item.decision == "escalate":
            if step is not None and step.attempts >= step.max_attempts:
                return Diagnosis.PLANNER_TARGET
            if repeated:
                item.decision = "retry"
                item.reasoning = f"{item.reasoning}；执行方向偏离,retry 继承压缩 ctx 纠偏".strip("；")
                return Diagnosis.DRIFT
        return Diagnosis.OTHER

    async def reflect(self, ctx: str) -> EvalResult:
        attempt = self._ensure_attempt()
        if self.plan_evaluation is None:
            attempt.plan = blueprint_to_plan(self.bindings.blueprint())
            self.plan_evaluation = await self.plan_evaluator.evaluate(attempt)
        attempt.ended_at = utc_now()
        attempt.submitted_flag = self.bindings.submitted_flag()
        steps = self.step_evaluator.summarize(self.step_items)
        flag = self._effective_flag(attempt)
        metrics = calculate_attempt_metrics(
            attempt,
            flag.valid,
            steps.score,
        )
        task = await self.task_evaluator.evaluate(
            attempt,
            self.plan_evaluation,
            steps,
            flag,
            metrics,
            ctx=ctx,
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
        self._emit("audit_reflect", {
            "decision": task.decision,
            "reason": task.reason,
            "flag_valid": flag.valid,
            "store_error": self.store_error,
        })
        return EvalResult(
            verdict,
            opinion,
            total_usage=self.task_evaluator.last_usage,
        )

    async def eval_goals(
        self,
        ctx: str,
        goals: list[dict],
        dag_summary: str,
    ) -> list:
        if self.bindings.goal_evaluator is not None:
            r = self.bindings.goal_evaluator(ctx, goals, dag_summary)
            return await r if asyncio.iscoroutine(r) else r
        return []

    def _emit(self, kind: str, detail: dict) -> None:
        if self.event_sink is not None:
            try:
                self.event_sink(kind, detail)
            except Exception:
                pass

    def close(self) -> None:
        try:
            self._dump_audit()
        except Exception as exc:  # noqa: BLE001 — 落盘失败不阻塞 close
            self.store_error = "audit_dump:%s" % type(exc).__name__
        self.step_evaluator.end_attempt(self.run_id)

    def _dump_audit(self) -> Optional[Path]:
        """把最终 AuditRecord 的派生字段原子写 audit.json(不含原始轨迹,轨迹真源是 events.jsonl)。

        只落 evaluation/metrics(plan/step/flag/task),不落 attempt.steps —— 避免与 history 双写。
        """
        if self.audit_output is None or self.last_record is None:
            return None
        record = self.last_record
        payload = {
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "task_id": record.attempt.task_id,
            "submitted_flag": record.attempt.submitted_flag,
            "plan_evaluation": asdict(record.plan_evaluation),
            "step_evaluation": asdict(record.step_evaluation),
            "flag": asdict(record.flag),
            "metrics": asdict(record.metrics),
            "task_evaluation": asdict(record.task_evaluation),
        }
        path = Path(self.audit_output)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)
        self.audit_written = path
        return path

    def _new_attempt(self) -> CTFAttempt:
        task = self.bindings.task()
        task_id = str(task.get("task_id") or task.get("id") or self.run_id)
        return CTFAttempt(
            attempt_id=self.run_id,
            task_id=task_id,
            agent_id=self.agent_id,
            category=str(task.get("challenge_type") or task.get("category") or "unknown"),
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

    def _effective_flag(self, attempt: CTFAttempt) -> FlagResult:
        """合并三层判定,正确性权威 = 平台/本地(_local_verify)提交判定,静态规则只兜底。

        1. submission.correct is True/False → 平台或 _local_verify 已判对/错(动态题 T1/T2 亦如此);
        2. correct is None → 回退静态 FlagVerifier(静态题有规则即用);
        3. 规则也 missing → valid=None(unknown,不回环,杜绝 Hack World 动态 flag REPLAN 死循环);
           submitted 标记该 run 是否真提交过(决定 None 时 pass 还是 fail)。
        """
        sub = self.bindings.submission_result() or {}
        correct = sub.get("correct")
        submitted = bool(sub.get("flag")) or bool(attempt.submitted_flag)
        if correct is True:
            return FlagResult(attempt.task_id, True, "platform",
                              "平台/本地判定:提交正确", submitted=submitted)
        if correct is False:
            return FlagResult(attempt.task_id, False, "platform",
                              "平台/本地判定:提交错误", submitted=submitted)
        flag = self.verifier.verify(attempt.task_id, attempt.submitted_flag)
        if flag.mode == "missing":
            reason = "动态 flag/未配置规则:无本地判定来源"
            if sub.get("ok") is True:
                reason += "，提交已受理(ok=True)"
            else:
                reason += "，且无有效提交"
            return FlagResult(attempt.task_id, None, "missing", reason, submitted=submitted)
        return FlagResult(attempt.task_id, flag.valid, flag.mode, flag.reason,
                          submitted=submitted)

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
        # 结构问题优先(确定性、可信):结构强制 revise 时 LLM 的 pass-opinion 不得覆盖。
        if evaluation.issues:
            return "；".join(evaluation.issues)
        # LLM 语义理由(无结构问题时),其次 suggestions,最后确定性兜底——理由恒非空。
        if evaluation.opinion:
            return evaluation.opinion
        if evaluation.suggestions:
            return "；".join(evaluation.suggestions)
        # 兜底文本要跟随决策:revise 不能说"结构完整"(否则日志自相矛盾)。
        if evaluation.decision == "revise":
            return "判定为 revise,但评审未提供具体修订原因/建议"
        return "计划结构和验收条件完整"
