"""规划 Agent 输出的 JSON 契约:统一 Patch(pydantic 校验 + 容错解析)。

安全(防反序列化漏洞):
- 只走 stdlib json.loads 解析纯 JSON,绝不用 eval/pickle/yaml;解析产物是
  PlanPatch 显式声明的字段,不反序列化任何自定义对象
- StepSpec 只暴露 id/instruction/criterion/depends_on,LLM 无法注入
  status/attempts/max_attempts/result 等引擎私有字段(to_patch 全部硬编码默认值)
- id 走白名单正则(字母/数字/下划线/点/连字符),杜绝路径穿越类 payload
- 未知字段 extra="ignore" 直接丢弃,不会被接受进任何字段
- 输出长度封顶 MAX_JSON_LEN,防超长 payload 撑爆解析
"""

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ===== Event detail 类型化协议 =====
# 每种 event kind 对应一个 detail dataclass;add_event 走 schema 校验,读方走 .字段 访问。
# 加新 kind 先注册 schema,否则 _SCHEMA.get(kind) 返回 None → 退化 dict 通道(日志警告)。


@dataclass
class ReplanDetail:
    """planner 产出/重规划落账:规划理由 + 触发源 + 变更摘要 + DAG 快照。

    dag 是 REPLAN 事件携带的完整 Blueprint.to_dict() 快照——事件流重放重建 DAG
    的素材;live path 引擎直接持有物化 blueprint,快照只服务 load/resume。
    """
    reason: str = ""
    source: str = ""       # EvalSource.value(plan_review/step_eval/reflect/scheduling)
    changes: str = ""      # _patch_summary 变更摘要
    dag: dict | None = None  # 当前 DAG 全量快照(重放重建用)


@dataclass
class OpinionDetail:
    """评估意见(ep/ee/et/scheduling):意见文本 + 可选观察 + 失败分类。"""
    opinion: str = ""
    observation: str | None = None
    diagnosis: str | None = None  # 仅 ee:未达成原因分类(incomplete/drift/planner_target/other)


@dataclass
class StepRecordDetail:
    """步骤验收记录:执行观察 + 产物 + 重试次数 + 任务完成标记 + 验收时 DAG 状态。"""
    observation: str = ""
    result: dict = field(default_factory=dict)
    attempts: int = 0
    is_completed: bool = False  # 该步验收时 ee 判定任务已完成
    status: str | None = None   # 验收时刻的 DAG 步骤状态(事件自洽)


@dataclass
class StepCancelDetail:
    """步骤中断事件:正在执行的 step 实例被打断(重规划重建/用户停跑)。

    运行侧前置契约(projections.py):并行下 replan 重建 RUNNING 实例必须先取消
    旧实例(token + SKIP + 抑制其 step_record);本事件是该中断的事件源落账,resume
    重放据此抑制迟到 step_record。step_id 在 Event 顶层,detail 只带 reason。
    """
    reason: str = ""       # 取消原因(如"用户停止"/"replan rebuild")


@dataclass
class SubmissionDetail:
    """提交判定事件:executor 提交 flag 后的平台结果(正确/错误/仅记录/异常)。"""
    flag: str = ""
    ok: bool | None = None
    correct: bool | None = None
    message: str | None = None


@dataclass
class LLMUsageDetail:
    """token 用量事件:单次 LLM 调用的记账(per-run run_tokens 的投影源)。"""
    role: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    ctx_size: int = 0
    ok: bool = True


@dataclass
class PlanReviewPassDetail:
    """计划评审 PASS 事件:评审通过后残留 REVISE 步骤回 PENDING 的状态迁移。"""
    reason: str = ""
    revised: list[str] = field(default_factory=list)  # 被清 REVISE 的步骤列表


@dataclass
class StepResult:
    """该步的执行产物/观察/verdict(每个 step 一条,存 state.json/投影缓存)。"""

    step_id: str
    verdict: str = ""
    observation: str = ""
    result: dict = field(default_factory=dict)
    attempts: int = 0
    is_completed: bool = False  # ee 判定任务是否已完成


# 步骤产物数据模型(可扩展,extra="allow" 允许 executor 添加任意字段)
StepResultData = dict  # 当前保持 dict 以向后兼容;未来可替换为 Pydantic BaseModel


@dataclass
class ToolCallDetail:
    """工具调用轨迹:工具名 + 参数。"""
    tool: str = ""
    args: dict = field(default_factory=dict)


@dataclass
class ToolResultDetail:
    """工具调用结果:工具名 + 参数 + 返回。"""
    tool: str = ""
    args: dict = field(default_factory=dict)
    output: str = ""


@dataclass
class GoalEvalDetail:
    """目标评估记录:step_eval agent 评完 step 后比对 goal list,引用 DAG 节点作证据。"""
    goal_id: str = ""
    complete: bool = False
    evidence: list[str] = field(default_factory=list)   # DAG step_id 列表,支撑该判定
    reasoning: str = ""
    gated: bool = False  # 引擎门控:flag 目标未提交 flag 时,complete 软判定降级为建议


@dataclass
class PlanReviewAuditDetail:
    """audit 计划评审事件:决策 + 评分 + 问题/建议(富详情,区别于引擎的 OpinionDetail)。"""
    decision: str = ""
    score: float = 0.0
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    opinion: str = ""


@dataclass
class StepEvalAuditDetail:
    """audit 步骤验收事件:步骤 id + 决策 + 评分 + 推理 + 未达成原因三分类。"""
    step_id: str = ""
    decision: str = ""
    score: float = 0.0
    reasoning: str = ""
    diagnosis: str = ""


@dataclass
class ReflectAuditDetail:
    """audit 任务反思事件:终局决策 + 理由 + flag 校验结果 + 经验落库错误。"""
    decision: str = ""
    reason: str = ""
    flag_valid: bool = False
    store_error: str | None = None


# kind → detail 类型注册表(键值与 EventKind 枚举值一致)
EVENT_SCHEMA: dict[str, type] = {
    "replan":           ReplanDetail,
    "plan_review":      OpinionDetail,
    "plan_note":        OpinionDetail,
    "step_eval":        OpinionDetail,
    "reflect":          OpinionDetail,
    "scheduling":       OpinionDetail,
    "step_record":      StepRecordDetail,
    "step_cancel":      StepCancelDetail,
    "use_tool":         ToolCallDetail,
    "tool_result":      ToolResultDetail,
    "goal_eval":        GoalEvalDetail,
    "submission":       SubmissionDetail,
    "llm_usage":        LLMUsageDetail,
    "plan_review_pass": PlanReviewPassDetail,
    "audit_plan_review": PlanReviewAuditDetail,
    "audit_step_eval":  StepEvalAuditDetail,
    "audit_reflect":    ReflectAuditDetail,
}


def normalize_event_detail(kind: str, detail) -> object:
    """旧 events.jsonl 的 dict detail → 当前 schema 的 dataclass;已是 dataclass 或 schema 未知则不处理。"""
    schema = EVENT_SCHEMA.get(kind)
    if schema is None or isinstance(detail, schema):
        return detail
    if isinstance(detail, dict):
        try:
            return schema(**detail)
        except Exception:
            return detail
    return detail

def _get_max_json_len() -> int:
    try:
        from model_config import get_engine_config
        return get_engine_config().get("max_json_len", 64 * 1024)
    except Exception:
        return 64 * 1024


MAX_JSON_LEN = _get_max_json_len()  # 防止 LLM 输出超大 payload

ID_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}$"


class PlanError(ValueError):
    """解析/校验失败,str(e) 可直接喂回模型要求重出。"""


class StepSpec(BaseModel):
    """规划产出的步骤。只暴露规划字段;status/attempts/max_attempts/result 由引擎接管。"""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(pattern=ID_PATTERN)
    instruction: str
    criterion: str
    depends_on: list[str] = Field(default_factory=list)
    skill_id: str | None = None   # 可选:绑定技能库文档 id(planner 检索后填入)

    @field_validator("instruction", "criterion")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("instruction/criterion 不能为空")
        return v

    @field_validator("depends_on")
    @classmethod
    def _no_self_dep(cls, v: list[str], info) -> list[str]:
        if info.data.get("id") in v:
            raise ValueError("depends_on 不能包含自身 id")
        return v


class UpdateSpec(BaseModel):
    """修改步骤:至少改一个字段;depends_on 给 [] 表示清空依赖。"""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(pattern=ID_PATTERN)
    instruction: str | None = None
    criterion: str | None = None
    depends_on: list[str] | None = None
    skill_id: str | None = None   # 改绑定技能文档(id 置空串可解除绑定)

    @model_validator(mode="after")
    def _checks(self):
        if (self.instruction is None and self.criterion is None
                and self.depends_on is None and self.skill_id is None):
            raise ValueError("update 至少修改一个字段")
        if self.depends_on is not None and self.id in self.depends_on:
            raise ValueError("depends_on 不能包含自身 id")
        return self


class PlanPatch(BaseModel):
    """规划 Agent 输出的 JSON 契约:add/update/remove 三种修改动作,统一覆盖生成与重规划。

    全空 add/update/remove 的语义(如"方案不变直接重跑")由调度规则阶段定义。
    """

    model_config = ConfigDict(extra="ignore")

    add: list[StepSpec] = Field(default_factory=list)
    update: list[UpdateSpec] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)
    reason: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def _conflicts(self):
        add_ids = [s.id for s in self.add]
        upd_ids = [u.id for u in self.update]
        if len(set(add_ids)) != len(add_ids):
            raise ValueError("add 内 id 重复")
        if len(set(upd_ids)) != len(upd_ids):
            raise ValueError("update 内 id 重复")
        if set(add_ids) & set(self.remove):
            raise ValueError("add 与 remove 重叠")
        if set(upd_ids) & set(self.remove):
            raise ValueError("update 与 remove 重叠")
        return self

    def to_patch(self) -> "Patch":
        from agent.blueprint import Patch, Step

        return Patch(
            add=[
                Step(
                    id=s.id,
                    instruction=s.instruction,
                    criterion=s.criterion,
                    depends_on=list(s.depends_on),
                    skill_id=s.skill_id,
                )
                for s in self.add
            ],
            update=[u.model_dump(exclude_none=True) for u in self.update],
            remove=list(self.remove),
            reason=self.reason,
        )


_FENCE = re.compile(r"^```[A-Za-z0-9_-]*\s*$")


def parse_plan(text: str) -> PlanPatch:
    """容错解析 LLM 输出的 JSON → PlanPatch。失败抛 PlanError(可喂回模型)。

    仅用 stdlib json.loads 解析纯数据;不做 eval/pickle/yaml 等任何代码执行路径。
    """
    if not text or not text.strip():
        raise PlanError("输出为空")
    t = text.strip()
    if len(t) > MAX_JSON_LEN:
        raise PlanError(f"输出超长(>{MAX_JSON_LEN} 字符),拒绝解析")
    if t.startswith("```"):
        lines = t.splitlines()
        if _FENCE.match(lines[0].strip()):
            lines = lines[1:]
        if lines and _FENCE.match(lines[-1].strip()):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end < 0:
        raise PlanError("输出中找不到 JSON 对象")
    t = t[start : end + 1]
    try:
        data = json.loads(t)
    except json.JSONDecodeError as e:
        raise PlanError(f"JSON 解析失败: {e}") from e
    if not isinstance(data, dict):
        raise PlanError("JSON 顶层必须是对象")
    try:
        return PlanPatch.model_validate(data)
    except Exception as e:  # pydantic.ValidationError
        raise PlanError(f"字段校验失败: {e}") from e


class EvalSource(StrEnum):
    """评估意见来源。planner 据此理解意见视角。

    其中 PLAN_REVIEW/STEP_EVAL/REFLECT/SCHEDULING/GOAL_EVAL 的值恰好等于对应 opinion
    事件的 EventKind——两个维度:EventKind 回答"什么类型的事件",EvalSource
    回答"谁触发的重规划"。
    """
    PLAN_REVIEW = "plan_review"  # 计划评审
    STEP_EVAL = "step_eval"      # 步骤校验
    REFLECT = "reflect"          # 任务反思
    SCHEDULING = "scheduling"    # 调度死锁(引擎结构检测,非评估 Agent 产出)
    GOAL_EVAL = "goal_eval"      # 目标评估(step_eval agent 评完 step 后比对 goal list)


class EventKind(StrEnum):
    """事件类型(events.jsonl 的 kind 字段)。"""
    REPLAN = "replan"
    PLAN_REVIEW = "plan_review"
    PLAN_NOTE = "plan_note"  # planner 计划级引导(pass 级 plan-notes,进 agent_comm 供兄弟 ex 共享)
    STEP_EVAL = "step_eval"
    REFLECT = "reflect"
    SCHEDULING = "scheduling"
    STEP_RECORD = "step_record"
    STEP_CANCEL = "step_cancel"            # 正在执行的 step 实例被打断(重规划/用户停跑)
    USE_TOOL = "use_tool"
    TOOL_RESULT = "tool_result"
    GOAL_EVAL = "goal_eval"
    SUBMISSION = "submission"            # executor 提交 flag 后的平台判定
    LLM_USAGE = "llm_usage"              # 单次 LLM 调用的 token 记账
    PLAN_REVIEW_PASS = "plan_review_pass"  # 计划评审通过后 REVISE→PENDING 状态迁移
    AUDIT_PLAN_REVIEW = "audit_plan_review"  # audit 评估器富详情通道(经 event_sink 落 events.jsonl)
    AUDIT_STEP_EVAL = "audit_step_eval"
    AUDIT_REFLECT = "audit_reflect"


class Role(StrEnum):
    """Agent 角色名称。engine 上下文组装/日志/信号的角色参数统一用此枚举。"""
    PLANNER = "planner"
    EXECUTOR = "executor"
    EVALUATOR_PLAN = "evaluator_plan"
    EVALUATOR_STEP = "evaluator_step"
    EVALUATOR_TASK = "evaluator_task"
    SYSTEM = "system"


# EvalSource → 评估角色(engine._EVAL_ROLE 的替代);SCHEDULING 是引擎结构检测,无评估角色
EVAL_ROLE: dict[EvalSource, Role | None] = {
    EvalSource.PLAN_REVIEW: Role.EVALUATOR_PLAN,
    EvalSource.STEP_EVAL: Role.EVALUATOR_STEP,
    EvalSource.REFLECT: Role.EVALUATOR_TASK,
    EvalSource.SCHEDULING: None,
    EvalSource.GOAL_EVAL: Role.EVALUATOR_STEP,   # goal 评估由 step_eval agent 执行
}

# EvalSource → 事件 agent 名(workspace._SOURCE_AGENT 的替代)
SOURCE_AGENT: dict[EvalSource, Role] = {
    EvalSource.PLAN_REVIEW: Role.EVALUATOR_PLAN,
    EvalSource.STEP_EVAL: Role.EVALUATOR_STEP,
    EvalSource.REFLECT: Role.EVALUATOR_TASK,
    EvalSource.SCHEDULING: Role.SYSTEM,
    EvalSource.GOAL_EVAL: Role.EVALUATOR_STEP,
}


class Trigger(StrEnum):
    """重规划触发原因(StateContext.trigger)。"""
    PLAN_REVIEW_FAIL = "plan_review_fail"
    STEP_ESCALATED = "step_escalated"
    STEP_TARGET_REDESIGN = "step_target_redesign"  # ee 判定步骤目标设计有误,仅重设计该步
    DEADLOCK = "deadlock"
    REFLECT = "reflect"


class Signal(StrEnum):
    """引擎生命周期信号(经由 SignalBus 广播)。"""
    RUN_STARTED = "run_started"
    RUN_END = "run_end"
    STATE_TRANSITION = "state_transition"
    LLM_CALL_START = "llm_call_start"
    LLM_CALL_END = "llm_call_end"
    REPLAN_START = "replan_start"
    REPLAN = "replan"
    REPLAN_END = "replan_end"
    STEP_STARTED = "step_started"
    STEP_ENDED = "step_ended"
    DEADLOCK_DETECTED = "deadlock_detected"
    OSCILLATION_RISK = "oscillation_risk"
    FAILED = "failed"
    PLAN_REVIEW_PASS = "plan_review_pass"
    CTX_ASSEMBLED = "ctx_assembled"
    CTX_OVERFLOW = "ctx_overflow"
    CTX_COMPRESSED = "ctx_compressed"
    CTX_INGEST = "ctx_ingest"
    LLM_RESPONSE = "llm_response"
    PHASE_TIMEOUT = "phase_timeout"
    RUN_TIMEOUT = "run_timeout"
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"
    ENV_CHECK = "env_check"  # 环境检查:工具/沙箱/分类就绪度探测结果(run_start/step)


class EvalEvent(BaseModel):
    """评估 Agent 给 planner 的单条意见:只读证据,planner 翻译成 Patch。

    意见是自由文本;若针对具体步骤,opinion 以 "sN:" 点名。step_id 为可选
    结构化关联,STEP_EVAL 来源时由 engine 填入当前步骤。
    """

    model_config = ConfigDict(extra="ignore")

    source: EvalSource
    opinion: str
    observation: str | None = None
    step_id: str | None = None
    diagnosis: str | None = None  # 仅 ee:失败分类(引擎路由单节点重设计的证据)

    @field_validator("opinion")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("opinion 不能为空")
        return v


class PlannerMode(StrEnum):
    INITIAL = "initial"  # 首次规划:无 DAG、无意见
    REVISE = "revise"    # 修订:有 DAG、至少一条触发意见


class Goal(BaseModel):
    """任务理解层下发的固定目标标识:字段由上游契约定义,当前仅 id。

    complete/evidence 等运行时状态不走此模型,走 GoalEvalDetail 事件——
    goal 只是任务理解层给的 key,完成判定与证据链全量记录在 events.jsonl 中。
    """

    model_config = ConfigDict(extra="ignore")

    id: str


class TaskInput(BaseModel):
    """任务理解层输出:raw_content(原始内容)+ goal_list(解析出的固定目标)。

    goal_list 由任务理解层分解并固定,planner 不可修改,格式 list[Goal]。
    engine 只从 TaskInput 取 goal_list,不做二次解析(raw_content 不含目标)。
    """

    model_config = ConfigDict(extra="ignore")

    raw_content: dict = Field(default_factory=dict)       # 原始内容(题面等,任务理解层解析输入)
    goal_list: list[Goal] = Field(default_factory=list)   # 任务理解层分解出的固定目标


class StateContext(BaseModel):
    """调度器注入 planner 的状态上下文(系统提示词修改 API 的载体)。

    只陈述事实(触发类型/受影响步骤/预算),**不下方向指令**——评审意见是否合理、
    如何绕过升级步骤等评估与决策由 planner 模型自行完成并给出理由。
    """

    model_config = ConfigDict(extra="ignore")

    trigger: str | None = None   # Trigger 枚举值
    detail: str | None = None    # 具体说明:哪些步骤受影响 / 死锁报告等
    budget: str | None = None    # 剩余预算(濒临 FAILED 时)


class Feedback(BaseModel):
    """③ 自产自维护的 run 反馈:修订时注入 planner。

    当前 DAG + 评估意见 + 补丁历史(振荡感知)+ 状态上下文(并入系统提示词)。
    执行证据(status/attempts/result/观察)从 dag 快照与 turn 观察派生,不单设字段。
    """

    model_config = ConfigDict(extra="ignore")

    dag: dict | None = None                                    # 当前计划快照(revise 必填)
    turn: list[EvalEvent] = Field(default_factory=list)        # 评估意见(当轮保底)
    state_context: StateContext | None = None                  # 调度器状态注入(触发类型/预算)
    scope_step_id: str | None = None                           # 单节点重设计:仅允许修改该步,其余不动


class PlannerInput(BaseModel):
    """planner 输入 = 两条路:
    1. task_input: 任务理解层输出(上游交付前 mock)——原始内容 + goal_list
    2. feedback:   ③ 自己维护的反馈(引擎累积)——当前 DAG + 评估意见 + 补丁历史 + 方向提示

    文档不预置在输入里:planner 规划时经 DocStore 检索(task_input 为检索输入)。
    """

    model_config = ConfigDict(extra="ignore")

    mode: PlannerMode
    task_input: TaskInput | None = None
    feedback: Feedback | None = None

    @model_validator(mode="after")
    def _mode_checks(self):
        if self.mode == PlannerMode.REVISE:
            if self.feedback is None:
                raise ValueError("revise 模式必须有 feedback")
            if self.feedback.dag is None:
                raise ValueError("revise 模式必须有 dag")
            if not self.feedback.turn:
                raise ValueError("revise 模式必须携带至少一条触发意见")
        else:
            if self.feedback is not None:
                raise ValueError("initial 模式不应携带 feedback")
        return self
