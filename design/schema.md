# Schema 与事件协议

类型系统与事件流契约。实现：`agent/schema.py`。

---

## 1. 枚举总览

### Role — Agent 角色

```python
class Role(StrEnum):
    PLANNER = "planner"
    EXECUTOR = "executor"
    EVALUATOR_PLAN = "evaluator_plan"
    EVALUATOR_STEP = "evaluator_step"
    EVALUATOR_TASK = "evaluator_task"
    SYSTEM = "system"
```

### EventKind — 事件类型

| 值 | 携带 detail 类型 | 触发时机 |
|---|---|---|
| `replan` | `ReplanDetail` | Planner 产出/重规划落账 |
| `plan_review` | `OpinionDetail` | ep 计划评审 |
| `step_eval` | `OpinionDetail` | ee 步骤验收 |
| `reflect` | `OpinionDetail` | et 任务反思 |
| `step_record` | `StepRecordDetail` | 步骤结果记录 |
| `use_tool` | `ToolCallDetail` | 工具调用开始 |
| `tool_result` | `ToolResultDetail` | 工具调用返回 |
| `scheduling` | dict 通道 | 调度决策（引擎结构检测） |
| `goal_eval` | `GoalEvalDetail` | step_eval agent 评完 step 后比对 goal list，引用 DAG 节点作证据 |

### EvalSource — 评估来源

```python
class EvalSource(StrEnum):
    PLAN_REVIEW = "plan_review"
    STEP_EVAL = "step_eval"
    REFLECT = "reflect"
    SCHEDULING = "scheduling"
    GOAL_EVAL = "goal_eval"
```

### Trigger — 触发原因

```python
class Trigger(StrEnum):
    PLAN_REVIEW_FAIL = "plan_review_fail"
    STEP_ESCALATED = "step_escalated"
    DEADLOCK = "deadlock"
    REFLECT = "reflect"
    STALL = "stall"
```

### Signal — 信号枚举（~23 个）

用于 SignalBus pub/sub，覆盖完整引擎生命周期：`RUN_STARTED`、`STATE_TRANSITION`、`LLM_CALL_START/END/RESPONSE`、`CTX_ASSEMBLED/OVERFLOW/COMPRESSED/INGEST`、`STEP_STARTED/ENDED`、`PLAN_REVIEW_PASS`、`FAILED`、`RUN_END`、`ENV_CHECK`（环境检查：工具/沙箱/分类就绪度，run_start/step 打点）等。

### PlannerMode

`INITIAL` — 初始规划；`REVISE` — 修订规划。

---

## 2. 事件流契约

### Event（`agent/workspace.py`）

```python
@dataclass
class Event:
    uuid: str           # 唯一标识
    agent: str | None   # 生产者角色(Role 值)
    kind: str           # 类型(EventKind 值)
    step_id: str | None # 关联步骤
    verdict: str | None # 关联判定
    detail: dict        # 类型化 detail（dataclass → asdict）
    ts: str             # ISO 时间戳
```

### 类型化 detail 协议（`EVENT_SCHEMA`）

每种 `EventKind` 对应一个 detail dataclass，`add_event` 时校验：

```python
EVENT_SCHEMA = {
    EventKind.REPLAN: ReplanDetail,         # {reason, source, changes}
    EventKind.PLAN_REVIEW: OpinionDetail,   # {opinion, ...}
    EventKind.STEP_EVAL: OpinionDetail,     # {opinion, observation?, ...}
    EventKind.REFLECT: OpinionDetail,       # {opinion, ...}
    EventKind.STEP_RECORD: StepRecordDetail, # {observation, result?, attempts, is_completed}
    EventKind.USE_TOOL: ToolCallDetail,     # {tool, args}
    EventKind.TOOL_RESULT: ToolResultDetail,# {tool, output, args}
    EventKind.GOAL_EVAL: GoalEvalDetail,    # {goal_id, complete, evidence, reasoning}
}
```

`scheduling` 等未注册 kind → 退化 dict 通道 + 日志警告。加新 kind 需先在 `EVENT_SCHEMA` 注册。

### normalize_event_detail

```python
def normalize_event_detail(kind: str, detail: dict) -> dict
```

旧版 dict 格式 → 新版 dataclass → asdict，保证存储一致性。

---

## 3. 核心 pydantic 模型

### StepSpec / UpdateSpec / PlanPatch

见 [dag.md](dag.md) §5。

### EvalEvent

```python
class EvalEvent(BaseModel):
    source: EvalSource
    opinion: str
    step_id: str | None = None
```

引擎将 `EvalResult` 映射为 `EvalEvent`，纳入 `Feedback.turn`。

### Goal — 任务理解层固定目标

```python
class Goal(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str        # 目标唯一标识，由任务理解层固定
```

Goal 只是任务理解层下发的 key。运行时完成状态与证据链通过 `GoalEvalDetail` 事件记录，不在 Goal 模型上存储。

### GoalEvalDetail — 目标评估记录

```python
@dataclass
class GoalEvalDetail:
    goal_id: str = ""
    complete: bool = False
    evidence: list[str] = field(default_factory=list)   # DAG step_id 列表
    reasoning: str = ""
```

step_eval agent 评完 step 后比对 goal_list 与 DAG，引用 DAG 节点作为证据，记录此事件。

### TaskInput — 任务理解层输出

```python
class TaskInput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    raw_content: dict = Field(default_factory=dict)        # 原始内容（understander 输出，不做解析）
    goal_list: list[Goal] = Field(default_factory=list)    # 任务理解层分解出的固定目标
```

TaskInput 由任务理解层输出 API（`agent/understander.py` 的 `TaskUnderstander.understand`）产出，
engine 在 `run()` 起始调用一次并缓存；goal_list 只从这里来，不做二次解析。

### PlannerInput

```python
class PlannerInput(BaseModel):
    mode: PlannerMode
    task_input: TaskInput
    feedback: Feedback | None = None
```

校验：INITIAL 模式时 `feedback` 必须为 None；REVISE 模式时 `feedback.dag` 必填且 `turn` 非空。

---

## 4. ID_PATTERN

```python
ID_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}$"
```

Step id 白名单：字母/数字/下划线/点/连字符，1-32 字符。路径穿越字符（`/`、`\`、`..`）全部拒绝。

---

## 5. 映射表

### EVAL_ROLE — 评估来源 → 角色

```python
EVAL_ROLE = {
    EvalSource.PLAN_REVIEW: Role.EVALUATOR_PLAN,
    EvalSource.STEP_EVAL: Role.EVALUATOR_STEP,
    EvalSource.REFLECT: Role.EVALUATOR_TASK,
    EvalSource.SCHEDULING: None,        # 引擎结构检测，非评估 Agent
    EvalSource.GOAL_EVAL: Role.EVALUATOR_STEP,  # goal 评估由 step_eval agent 执行
}
```

### SOURCE_AGENT — 评估来源 → ingest agent 名

用于上下文装填：`SOURCE_AGENT[source]` → 对应 `record_opinion` 调用时写入的 agent 字段。

```python
SOURCE_AGENT = {
    EvalSource.PLAN_REVIEW: Role.EVALUATOR_PLAN,
    EvalSource.STEP_EVAL: Role.EVALUATOR_STEP,
    EvalSource.REFLECT: Role.EVALUATOR_TASK,
    EvalSource.SCHEDULING: Role.SYSTEM,
    EvalSource.GOAL_EVAL: Role.EVALUATOR_STEP,
}
```
