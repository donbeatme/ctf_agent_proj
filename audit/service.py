"""Public factory wiring audit components to the existing agent contracts."""

from typing import Any, Dict

from .agent_adapter import (
    AgentAuditEvaluator,
    AgentRuntimeBindings,
    AuditExperienceDocStore,
)
from .flag_verifier import FlagVerifier
from .integrations.experience import LocalExperienceStore
from .integrations.ragflow import RAGFlowExperienceStore
from .settings import Settings


class AgentAuditService:
    """Create Planner and Evaluator adapters without modifying ``agent/``."""

    def __init__(
        self,
        settings: Settings,
        flag_rules: Dict[str, Dict[str, Any]],
        run_id: str,
        agent_id: str,
        event_sink=None,
    ):
        self.settings = settings
        self.verifier = FlagVerifier(flag_rules)
        self.run_id = run_id
        self.agent_id = agent_id
        self.event_sink = event_sink
        self.experience_store = (
            RAGFlowExperienceStore(settings)
            if settings.ragflow_enabled
            else LocalExperienceStore(settings.data_dir / "experiences.jsonl")
        )
        self.planner_docs = AuditExperienceDocStore(
            self.experience_store,
            agent_id,
            settings.experience_search_limit,
        )

    def bind_evaluator(
        self,
        bindings: AgentRuntimeBindings,
        audit_output=None,
    ) -> AgentAuditEvaluator:
        return AgentAuditEvaluator(
            settings=self.settings,
            verifier=self.verifier,
            experience_store=self.experience_store,
            run_id=self.run_id,
            agent_id=self.agent_id,
            bindings=bindings,
            event_sink=self.event_sink,
            audit_output=audit_output,
        )
