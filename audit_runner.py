"""Standalone wiring for the existing agent and the audit/RAGFlow service."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Optional

from agent.blueprint import DAGError
from agent.engine import Engine
from agent.evaluator import Evaluator
from agent.executor import Executor
from agent.planner import DocStore, Planner
from agent.skills import CtfSkillsDocStore
from agent.workspace import Workspace

from audit import AgentAuditService, AgentRuntimeBindings
from audit.flag_verifier import FlagVerifier
from audit.settings import Settings


DEFAULT_FLAG_PATTERN = re.compile(
    r"\b[A-Za-z][A-Za-z0-9_]{1,31}\{[^{}\r\n]{1,512}\}"
)
FLAG_RESULT_KEYS = ("submitted_flag", "flag", "final_flag", "candidate_flag")


class CombinedDocStore(DocStore):
    """Combine the existing skill store with retrieved audit experiences."""

    def __init__(self, stores: Iterable[DocStore]):
        self._stores = tuple(stores)

    def search(self, task: dict) -> list[tuple[str, str]]:
        results = []
        seen = set()
        for store in self._stores:
            for doc_id, content in store.search(task):
                if doc_id in seen:
                    continue
                seen.add(doc_id)
                results.append((doc_id, content))
        return results

    def load_doc(self, doc_id: str) -> Optional[str]:
        for store in self._stores:
            content = store.load_doc(doc_id)
            if content is not None:
                return content
        return None


class AuditedPlanner(Planner):
    """Retry one structurally invalid patch with explicit DAG feedback."""

    def plan(self, planner_input: Any) -> Any:
        try:
            return super().plan(planner_input)
        except DAGError as exc:
            original_llm = self.llm_call

            def corrected_llm(**kwargs: Any) -> str:
                prompt = str(kwargs.get("prompt") or "")
                kwargs["prompt"] = (
                    prompt
                    + "\n\n[Previous patch could not be applied: %s]\n" % str(exc)
                    + "Return a corrected patch for the CURRENT DAG. "
                    + "For an initial empty DAG, create every step with add. "
                    + "Only update or remove step IDs that already exist."
                )
                return original_llm(**kwargs)

            self.llm_call = corrected_llm
            try:
                return super().plan(planner_input)
            finally:
                self.llm_call = original_llm


@dataclass
class SubmittedFlagState:
    """Mutable flag handoff shared by the executor and FlagVerifier."""

    value: Optional[str] = field(default=None, repr=False)

    def submit(self, value: Optional[str]) -> None:
        candidate = str(value or "").strip()
        if candidate:
            self.value = candidate

    def get(self) -> Optional[str]:
        return self.value


class FlagCapturingExecutor(Executor):
    """Delegate execution and capture a submitted flag without changing it."""

    def __init__(
        self,
        executor: Executor,
        state: SubmittedFlagState,
        verifier: FlagVerifier,
        task_id: str,
        flag_pattern: Optional[str] = None,
    ):
        self._executor = executor
        self._state = state
        self._verifier = verifier
        self._task_id = task_id
        self._pattern = re.compile(flag_pattern) if flag_pattern else DEFAULT_FLAG_PATTERN

    def run(self, step: Any, ctx: str, tool_exec: Any = None) -> Any:
        result = self._executor.run(step, ctx, tool_exec=tool_exec)
        self._capture(result)
        return result

    def _capture(self, result: Any) -> None:
        candidates = list(self._structured_candidates(getattr(result, "result", None)))
        candidates.extend(self._text_candidates(getattr(result, "observation", "")))
        candidates.extend(self._text_candidates(getattr(result, "tool_calls", None)))
        candidates = list(dict.fromkeys(candidates))
        if not candidates:
            return

        for candidate in candidates:
            if self._verifier.verify(self._task_id, candidate).valid:
                self._state.submit(candidate)
                return
        self._state.submit(candidates[0])

    def _structured_candidates(self, value: Any) -> Iterable[str]:
        if isinstance(value, dict):
            for key in FLAG_RESULT_KEYS:
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    yield candidate.strip()
            for nested in value.values():
                yield from self._structured_candidates(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                yield from self._structured_candidates(nested)

    def _text_candidates(self, value: Any) -> Iterable[str]:
        if value is None:
            return
        if not isinstance(value, str):
            value = str(value)
        for match in self._pattern.finditer(value):
            yield match.group(0)


@dataclass
class AuditedRuntime:
    """Own an Engine and guarantee evaluator cleanup after each run."""

    task: Dict[str, Any]
    engine: Engine
    evaluator: Evaluator
    service: AgentAuditService
    submitted_flag: SubmittedFlagState
    _closed: bool = field(default=False, init=False, repr=False)

    def submit_flag(self, value: str) -> None:
        self.submitted_flag.submit(value)

    @property
    def closed(self) -> bool:
        return self._closed

    def run(self) -> Any:
        try:
            return self.engine.run(self.task)
        finally:
            self.close()

    def close(self) -> None:
        if not self._closed:
            close = getattr(self.evaluator, "close", None)
            if callable(close):
                close()
            self._closed = True

    def __enter__(self) -> "AuditedRuntime":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


def create_audited_runtime(
    task: Dict[str, Any],
    executor: Executor,
    flag_rules: Dict[str, Dict[str, Any]],
    *,
    run_id: Optional[str] = None,
    agent_id: str = "ctf-agent",
    settings: Optional[Settings] = None,
    workspace: Optional[Workspace] = None,
    skill_docs: Optional[DocStore] = None,
    planner_llm: Optional[Callable[..., str]] = None,
    goal_evaluator: Optional[Callable[[str, list[dict], str], list]] = None,
    engine_options: Optional[Dict[str, Any]] = None,
) -> AuditedRuntime:
    """Create the complete Planner -> Engine -> audit -> RAGFlow runtime."""

    task_id = str(task.get("task_id") or task.get("id") or "").strip()
    if not task_id:
        raise ValueError("task must contain task_id or id")
    actual_run_id = run_id or "run-%s" % time.strftime("%Y%m%d-%H%M%S")
    actual_settings = settings or Settings.from_env()
    actual_workspace = workspace or Workspace.create(actual_run_id, task)

    service = AgentAuditService(
        settings=actual_settings,
        flag_rules=flag_rules,
        run_id=actual_run_id,
        agent_id=agent_id,
    )
    docs = CombinedDocStore((skill_docs or CtfSkillsDocStore(), service.planner_docs))
    planner = AuditedPlanner(
        llm_call=planner_llm,
        docs=docs,
        workspace=actual_workspace,
    )

    submitted_flag = SubmittedFlagState()
    capturing_executor = FlagCapturingExecutor(
        executor=executor,
        state=submitted_flag,
        verifier=service.verifier,
        task_id=task_id,
        flag_pattern=task.get("flag_pattern"),
    )
    holder: Dict[str, Engine] = {}
    bindings = AgentRuntimeBindings(
        blueprint=lambda: holder["engine"].bp,
        task=lambda: task,
        current_step=lambda: holder["engine"].current,
        observation=lambda: holder["engine"]._obs or "",
        submitted_flag=submitted_flag.get,
        completed=lambda: holder["engine"].task_completed,
        goal_evaluator=goal_evaluator,
    )
    evaluator = service.bind_evaluator(bindings)

    options = dict(engine_options or {})
    reserved = {"planner", "executor", "evaluator", "workspace"}
    conflict = reserved.intersection(options)
    if conflict:
        raise ValueError("engine_options cannot override: %s" % ", ".join(sorted(conflict)))
    engine = Engine(
        planner=planner,
        executor=capturing_executor,
        evaluator=evaluator,
        workspace=actual_workspace,
        **options,
    )
    holder["engine"] = engine
    return AuditedRuntime(
        task=task,
        engine=engine,
        evaluator=evaluator,
        service=service,
        submitted_flag=submitted_flag,
    )


def run_audited_task(
    task: Dict[str, Any],
    executor: Executor,
    flag_rules: Dict[str, Dict[str, Any]],
    **kwargs: Any,
) -> Any:
    """Create, run, and close an audited runtime in one call."""

    return create_audited_runtime(task, executor, flag_rules, **kwargs).run()
