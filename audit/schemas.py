"""项目的规范数据模型与 JSON Schema。

核心层刻意只用标准库，确保拿到尚未清洗的 agent 日志时也能先完成验证、
转换和审计；第三方 SDK 只存在于 integrations 目录。
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class SchemaError(ValueError):
    """输入不符合审计 schema。"""


class StepKind(str, Enum):
    THOUGHT = "thought"
    ACTION = "action"
    OBSERVATION = "observation"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    FINAL = "final"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _required(data: Dict[str, Any], key: str, expected_type: Any) -> Any:
    if key not in data:
        raise SchemaError("missing required field: %s" % key)
    value = data[key]
    if not isinstance(value, expected_type):
        raise SchemaError("field %s must be %s" % (key, expected_type.__name__))
    return value


@dataclass
class TrajectoryStep:
    index: int
    kind: StepKind
    content: str
    timestamp: str = field(default_factory=utc_now)
    tool_name: Optional[str] = None
    tool_args: Dict[str, Any] = field(default_factory=dict)
    success: Optional[bool] = None
    tokens: int = 0
    duration_ms: int = 0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TrajectoryStep":
        if not isinstance(data, dict):
            raise SchemaError("each step must be an object")
        index = _required(data, "index", int)
        if index < 0:
            raise SchemaError("step index must be >= 0")
        try:
            kind = StepKind(_required(data, "kind", str))
        except ValueError as exc:
            raise SchemaError("unknown step kind: %s" % data.get("kind")) from exc
        tool_args = data.get("tool_args", {})
        if not isinstance(tool_args, dict):
            raise SchemaError("tool_args must be an object")
        return cls(
            index=index,
            kind=kind,
            content=_required(data, "content", str),
            timestamp=str(data.get("timestamp", utc_now())),
            tool_name=data.get("tool_name"),
            tool_args=tool_args,
            success=data.get("success"),
            tokens=int(data.get("tokens", 0)),
            duration_ms=int(data.get("duration_ms", 0)),
        )

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["kind"] = self.kind.value
        return result


@dataclass
class PlanStep:
    """规划 Agent 输出的一个 DAG 节点。"""

    plan_step_id: str
    goal: str
    action: str
    instruction: str
    criterion: str
    tool: Optional[str] = None
    depends_on: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlanStep":
        if not isinstance(data, dict):
            raise SchemaError("each plan step must be an object")
        dependencies = data.get("depends_on", [])
        if not isinstance(dependencies, list):
            raise SchemaError("plan step depends_on must be an array")
        return cls(
            plan_step_id=str(data.get("plan_step_id") or data.get("step_id") or ""),
            goal=str(data.get("goal", "")),
            action=str(data.get("action", "")),
            instruction=_required(data, "instruction", str),
            criterion=_required(data, "criterion", str),
            tool=None if data.get("tool") is None else str(data.get("tool")),
            depends_on=[str(item) for item in dependencies],
        )


@dataclass
class CTFAttempt:
    attempt_id: str
    task_id: str
    agent_id: str
    category: str
    started_at: str
    ended_at: str
    steps: List[TrajectoryStep]
    plan: List[PlanStep] = field(default_factory=list)
    submitted_flag: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CTFAttempt":
        if not isinstance(data, dict):
            raise SchemaError("attempt must be an object")
        raw_steps = _required(data, "steps", list)
        steps = [TrajectoryStep.from_dict(item) for item in raw_steps]
        raw_plan = data.get("plan", [])
        if not isinstance(raw_plan, list):
            raise SchemaError("plan must be an array")
        plan = [PlanStep.from_dict(item) for item in raw_plan]
        indices = [step.index for step in steps]
        if indices != sorted(indices) or len(indices) != len(set(indices)):
            raise SchemaError("step indices must be unique and ascending")
        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            raise SchemaError("metadata must be an object")
        return cls(
            attempt_id=_required(data, "attempt_id", str),
            task_id=_required(data, "task_id", str),
            agent_id=_required(data, "agent_id", str),
            category=_required(data, "category", str),
            started_at=_required(data, "started_at", str),
            ended_at=_required(data, "ended_at", str),
            steps=steps,
            plan=plan,
            submitted_flag=data.get("submitted_flag"),
            metadata=metadata,
        )

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["steps"] = [step.to_dict() for step in self.steps]
        result["plan"] = [asdict(step) for step in self.plan]
        return result


@dataclass
class FlagResult:
    task_id: str
    valid: Optional[bool]  # None = 无判定来源(动态 flag/未配置规则),不触发 REPLAN
    mode: str
    reason: str
    submitted: bool = False  # 本次 run 是否真的提交过 flag(ok=True)或至少携带 flag 串


@dataclass
class PlanEvaluation:
    """评估点 1：计划评审，决策为 pass 或 revise。"""

    decision: str
    score: float
    issues: List[str]
    suggestions: List[str]
    evaluator: str
    opinion: str = ""  # LLM 评审意见文本(理由必有;结构 issues 为空时经此落 opinion)


@dataclass
class PlanFeedback:
    """交给外部 Planning Agent 的稳定 JSON 契约。"""

    schema_version: str
    feedback_id: str
    attempt_id: str
    task_id: str
    review_attempt: int
    max_review_attempts: int
    remaining_attempts: int
    decision: str
    can_execute: bool
    can_revise: bool
    next_action: str
    score: float
    issues: List[str]
    suggestions: List[str]
    evaluator: str
    plan_fingerprint: str
    memory_source: str
    retrieved_memory_count: int
    memory_error: Optional[str]
    created_at: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class StepEvaluationItem:
    """一个执行步骤的验收结果。"""

    step_id: str
    decision: str
    score: float
    tool: str
    success: Optional[bool]
    reasoning: str
    evaluator: str


@dataclass
class StepEvaluation:
    """评估点 2：全部步骤的 pass/retry/escalate 汇总。"""

    score: float
    pass_count: int
    retry_count: int
    escalate_count: int
    items: List[StepEvaluationItem]
    evaluator: str


@dataclass
class AttemptMetrics:
    attempt_id: str
    flag_success: float
    step_evaluation_score: float
    total_steps: int
    trajectory_events: int
    effective_steps: int
    tool_success_rate: float
    tool_error_rate: float
    retry_rate: float
    duration_seconds: float
    token_count: int
    step_efficiency: float
    time_efficiency: float
    composite_score: float


@dataclass
class Reflection:
    attempt_id: str
    diagnosis: str
    lessons: List[str]
    next_plan: List[str]
    source: str


@dataclass
class PlanningMemoryContext:
    """规划 Agent 开始工作前，从经验库检索出的安全上下文。"""

    attempt_id: str
    query: str
    memories: List[Dict[str, Any]]
    prompt_context: str
    source: str
    error: Optional[str] = None


@dataclass
class TaskEvaluation:
    """评估点 3：任务级最终决策和 Reflexion。"""

    decision: str
    reason: str
    reflection: Optional[Reflection] = None


@dataclass
class AuditRecord:
    attempt: CTFAttempt
    plan_evaluation: PlanEvaluation
    step_evaluation: StepEvaluation
    flag: FlagResult
    metrics: AttemptMetrics
    task_evaluation: TaskEvaluation

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# 可导出且能被常见 JSON Schema 工具使用。flag 期望值使用独立 schema，
# 绝不写入 attempt 日志或 LangSmith trace。
JSON_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "plan_step": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": [
            "plan_step_id", "goal", "action", "instruction", "criterion"
        ],
        "properties": {
            "plan_step_id": {"type": "string", "minLength": 1},
            "goal": {"type": "string", "minLength": 1},
            "action": {"type": "string", "minLength": 1},
            "instruction": {"type": "string", "minLength": 1},
            "criterion": {"type": "string", "minLength": 1},
            "tool": {"type": ["string", "null"]},
            "depends_on": {"type": "array", "items": {"type": "string"}}
        },
        "additionalProperties": False,
    },
    "trajectory_step": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["index", "kind", "content"],
        "properties": {
            "index": {"type": "integer", "minimum": 0},
            "kind": {"enum": [item.value for item in StepKind]},
            "content": {"type": "string"},
            "timestamp": {"type": "string", "format": "date-time"},
            "tool_name": {"type": ["string", "null"]},
            "tool_args": {"type": "object"},
            "success": {"type": ["boolean", "null"]},
            "tokens": {"type": "integer", "minimum": 0},
            "duration_ms": {"type": "integer", "minimum": 0},
        },
        "additionalProperties": False,
    },
    "plan_feedback": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": [
            "schema_version", "feedback_id", "attempt_id", "task_id",
            "review_attempt", "max_review_attempts", "remaining_attempts",
            "decision", "can_execute", "can_revise", "next_action", "score",
            "issues", "suggestions", "evaluator", "plan_fingerprint",
            "memory_source", "retrieved_memory_count", "memory_error",
            "created_at",
        ],
        "properties": {
            "schema_version": {"const": "1.0"},
            "feedback_id": {"type": "string", "minLength": 1},
            "attempt_id": {"type": "string", "minLength": 1},
            "task_id": {"type": "string", "minLength": 1},
            "review_attempt": {"type": "integer", "minimum": 1, "maximum": 3},
            "max_review_attempts": {"const": 3},
            "remaining_attempts": {"type": "integer", "minimum": 0, "maximum": 2},
            "decision": {"enum": ["pass", "revise"]},
            "can_execute": {"type": "boolean"},
            "can_revise": {"type": "boolean"},
            "next_action": {
                "enum": ["start_execution", "revise_plan", "manual_review"]
            },
            "score": {"type": "number", "minimum": 0, "maximum": 1},
            "issues": {"type": "array", "items": {"type": "string"}},
            "suggestions": {"type": "array", "items": {"type": "string"}},
            "evaluator": {"type": "string"},
            "plan_fingerprint": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "memory_source": {"type": "string"},
            "retrieved_memory_count": {"type": "integer", "minimum": 0},
            "memory_error": {"type": ["string", "null"]},
            "created_at": {"type": "string", "format": "date-time"},
        },
        "additionalProperties": False,
    },
    "ctf_attempt": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": [
            "attempt_id", "task_id", "agent_id", "category",
            "started_at", "ended_at", "steps",
        ],
        "properties": {
            "attempt_id": {"type": "string", "minLength": 1},
            "task_id": {"type": "string", "minLength": 1},
            "agent_id": {"type": "string", "minLength": 1},
            "category": {"type": "string", "minLength": 1},
            "started_at": {"type": "string", "format": "date-time"},
            "ended_at": {"type": "string", "format": "date-time"},
            "plan": {"type": "array", "items": {"$ref": "plan_step.schema.json"}},
            "steps": {"type": "array", "items": {"$ref": "trajectory_step.schema.json"}},
            "submitted_flag": {"type": ["string", "null"]},
            "metadata": {"type": "object"},
        },
        "additionalProperties": False,
    },
    "expected_flags": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": {
            "type": "object",
            "required": ["mode", "value"],
            "properties": {
                "mode": {"enum": ["exact", "sha256", "regex"]},
                "value": {"type": "string"},
                "case_sensitive": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
    },
    "ctf_result": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["task_id", "category", "objective", "steps", "final_answer"],
        "properties": {
            "task_id": {"type": "string", "minLength": 1},
            "title": {"type": "string"},
            "category": {"type": "string", "minLength": 1},
            "difficulty": {"type": "string"},
            "objective": {"type": "string"},
            "expected_flag": {"type": ["string", "null"]},
            "flag_pattern": {"type": ["string", "null"]},
            "plan": {
                "type": "array",
                "items": {"$ref": "plan_step.schema.json"}
            },
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["step_id", "action", "observation", "status"],
                    "properties": {
                        "step_id": {"type": ["string", "integer"]},
                        "goal": {"type": "string"},
                        "action": {"type": "string"},
                        "tool": {"type": ["string", "null"]},
                        "tool_args": {"type": "object"},
                        "observation": {"type": "string"},
                        "status": {"type": "string"},
                        "duration_ms": {"type": "integer", "minimum": 0},
                        "tokens": {"type": "integer", "minimum": 0}
                    },
                    "additionalProperties": True
                }
            },
            "final_answer": {"type": ["string", "null"]},
            "source": {"type": "string"},
            "agent_id": {"type": "string"},
            "attempt_id": {"type": "string"},
            "started_at": {"type": "string", "format": "date-time"},
            "ended_at": {"type": "string", "format": "date-time"}
        },
        "additionalProperties": True
    },
    "planning_memory_context": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": [
            "attempt_id", "query", "memories", "prompt_context", "source"
        ],
        "properties": {
            "attempt_id": {"type": "string", "minLength": 1},
            "query": {"type": "string"},
            "memories": {"type": "array", "items": {"type": "object"}},
            "prompt_context": {"type": "string"},
            "source": {"type": "string"},
            "error": {"type": ["string", "null"]},
        },
        "additionalProperties": False,
    },
}
