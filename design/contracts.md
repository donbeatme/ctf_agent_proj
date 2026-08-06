# Agent 接口契约

引擎与三个外部 Agent 的接口约定。实现：`agent/planner.py`、`agent/executor.py`、`agent/evaluator.py`。

---

## 0. 任务理解层 — 输出 API

任务理解层（外部 ②）向引擎交付结构化任务的输出 API。实现：`agent/understander.py`。

```python
class TaskUnderstander:
    """任务理解层输出 API（外部 ② 实现，③ 只消费不产出）。"""
    def understand(self, raw: dict) -> TaskInput: ...

class MockTaskUnderstander(TaskUnderstander):
    # mock:消费 raw["goals"]（list[dict]，各含 id）→ goal_list;raw 其余原样作 raw_content
    def understand(self, raw: dict) -> TaskInput: ...
```

- **调用时机**：engine 在 `run()` 起始、`_init_run` 内调用一次；`resume()` 从 `ws.meta["goal_list"]` 恢复，不重调。
- **契约**：`raw` 为任务原始输入 dict；返回 `TaskInput`，其中 `goal_list`（`list[Goal]`）为解析出的固定目标，字段名/格式不可改。
- **engine 只消费不产出**：engine 不自行解析 `raw_content` 里的目标，goal_list 只来自 understander。
- **边界保护**：understander 抛异常时 engine 回退 `TaskInput(raw_content=raw)`（goal_list 为空），不让 run 崩溃。

---

## 1. Planner — 规划 Agent

```python
class Planner:
    def __init__(self, llm_call=None, docs=None, workspace=None)
    def plan(self, pin: PlannerInput) -> Blueprint
```

### 输入：PlannerInput

```python
class PlannerInput(BaseModel):
    mode: PlannerMode           # INITIAL | REVISE
    task_input: TaskInput       # 任务描述
    feedback: Feedback | None   # 上下文反馈（REVISE 模式必填）
```

#### PlannerMode

| 值 | 说明 |
|---|---|
| `INITIAL` | 初始规划，feedback 必须为 None |
| `REVISE` | 修订规划，feedback 必须提供 dag + 至少一个 turn 事件 |

#### TaskInput

```python
class TaskInput(BaseModel):
    raw_content: dict            # 原始内容 {title, description, challenge_id, ...}，不做解析
    goal_list: list[Goal] = []   # 任务理解层解析出的固定目标（仅 id，不可修改）
```

**goal_list 单一来源**：goal_list 只能由任务理解层（§0 输出 API）产出。engine 在 `run()`
起始调用 understander 获取 TaskInput 实例，INITIAL/REVISE 两种模式都转发同一 goal_list，
下游（ctx TaskComponent、goal 评估）统一读 `goal_list`，无其他入口，不做二次解析。

`Goal` 只有 `id: str` 字段，是任务理解层下发的固定 key。运行时完成状态与证据链通过 `GoalEvalDetail` 事件记录。

#### Feedback（REVISE 模式）

```python
class Feedback(BaseModel):
    dag: dict | None            # 当前 DAG 状态（Blueprint.to_dict()）
    turn: list[EvalEvent]       # 当前轮评估意见
    state_context: StateContext | None  # 触发原因 + 摘要
```

`StateContext`：`trigger: str`（plan_review_fail / step_escalate / deadlock / reflect）、`detail: str`、`budget: str | None`（濒临 FAILED 时的剩余预算描述文本，如"剩余预算:重规划 2/8"）。

### 输出：Blueprint

- `bp.meta["reason"]` — 规划/修订理由
- `bp.meta["_response"]` — 原始 LLM 返回文本（日志用）
- `Step.skill_id`（可选）— 绑定技能库文档 id，由 planner 检索后填入（add/update 的 `skill_id` 字段）；executor 执行时经此查阅技能文档

### 内部 LLM 调用

`Planner._default_llm()` 生成闭包，签名 `(*, system=None, prompt=None, messages=None, **kw) -> str`：
- 调用 `llm_api.chat_with_tools(model=role_model("planner"), tools=PLANNER_TOOLS)`
- `PLANNER_TOOLS = tools.openai_tool_specs(names={"get_doc"})` — 从工具库按白名单导出，**不与执行工具目录混用**（design/tools.md §4）
- 工具：`get_doc`（从 workspace.docs 注册表取技能文档全文）
- `**kw` 透传给 LLM，允许测试注入 mock（`Planner(llm_call=mock_fn)`）

---

## 2. Executor — 执行 Agent

```python
class Executor:
    def run(self, step: Step, ctx: str) -> ExecResult

@dataclass
class ExecResult:
    observation: str              # 人类可读的执行描述
    result: dict | None = None    # 结构化的执行产物
    tool_calls: list[dict] | None = None  # 工具调用记录
```

- `ctx` — Engine 通过 `assembler.assemble("executor", step_id=..., ...)` 组装的上下文文本
- `result` — 写入 `step.result`，供后续步骤和评估使用
- `tool_calls` — 喂入 TraceComponent 的 trace 通道，投影执行决策链
- `step.skill_id` — 可选绑定技能文档 id；executor 上下文含 Docs 组件（绑定文档的索引，id + 一句话），全文按需经 `get_doc` 取

**接口单发**：`run(step, ctx)` 是一次调用返回 `ExecResult`。执行层内部的多轮 ReAct
循环（思考 → 调工具 → 观察结果）由 executor 内部封装（第二组②），引擎不感知；
循环的迭代上限/重试由执行层自管，超出由 ee 判定 retry/escalate。工具调用轨迹通过
`ExecResult.tool_calls` 回传。

`tool_calls` 条目**精确 shape**（engine 落 trace 读取的字段名，勿改名）：

```python
{"tool": str, "args": dict, "result": str}
```

- `tool` — 工具名，与工具目录 id 一致
- `args` — 调用参数 dict
- `result` — 工具返回文本；**缺省该 key 则只记 use_tool（工具无输出），不记 tool_result**
- 别名兼容（engine 归一）：`name`/`output`（OpenAI 风格）；但契约只保证 `tool`/`args`/`result` 一种写法

### MockExecutor

```python
class MockExecutor:
    def __init__(self, observation="(mock)", result=None, fn=None)
```

`fn` 为 `(step, ctx) -> ExecResult`，优先级高于 observation/result。

---

## 3. Evaluator — 评估 Agent 接口

```python
class Evaluator:
    def review(self, ctx: str) -> EvalResult       # ep: 计划评审
    def step_eval(self, ctx: str) -> EvalResult    # ee: 步骤验收
    def reflect(self, ctx: str) -> EvalResult      # et: 任务反思
    def eval_goals(self, step_ctx: str, goals: list[dict], dag_summary: str) -> list[GoalEvalDetail]
```

### Verdict — 判定枚举

```python
class Verdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    RETRY = "retry"
    ESCALATE = "escalate"
    DONE = "done"
    REPLAN = "replan"
```

**按角色合法值**：

| 角色 | 可用 verdict | 不合法值行为 |
|---|---|---|
| ep (review) | PASS, FAIL | 其他 → Engine 按 FAIL 处理 |
| ee (step_eval) | PASS, RETRY, ESCALATE | 其他 → Engine 按 FAIL 处理 |
| et (reflect) | DONE, REPLAN | 其他 → Engine 按 FAIL 处理 |

### EvalResult

```python
@dataclass
class EvalResult:
    verdict: Verdict
    opinion: str                 # 意见正文；ee 以 "sN:" 点名步骤
    observation: str | None = None  # 仅 ee：执行观察/产物摘要
    is_completed: bool = False   # 仅 ee：任务是否已完成
```

`is_completed` 仅 ee 使用：置 true 时，引擎认为任务已完成，残留未完成节点不再触发死锁，直接进 REFLECTING。

### eval_goals — 目标评估（ee 内部）

`eval_goals(step_ctx, goals, dag_summary)` 在 step_eval 判定 PASS 后调用：
- `step_ctx` — 当前步骤的上下文文本
- `goals` — 尚未完成的 Goal 对象列表（dict 形式，含 `id`）
- `dag_summary` — 当前 DAG 的结构摘要（步骤 id + status + instruction 摘要）
- 返回 `list[GoalEvalDetail]`，每个元素包含 `goal_id`、`complete`、`evidence`（DAG step_id 列表）、`reasoning`

引擎据此更新 `_goal_complete` 字典并 emit `GOAL_EVAL` 事件。所有 goal 完成时设置 `task_completed = True`。

### MockEvaluator

```python
class MockEvaluator:
    def __init__(self, responses: dict[str, EvalResult] | None = None,
                 goal_responses: list[GoalEvalDetail] | None = None)
```

key 使用 Role 枚举值：`"evaluator_plan"` / `"evaluator_step"` / `"evaluator_task"`。
value 可以是 `EvalResult` 或 `callable(ctx) -> EvalResult`。
`goal_responses` 为 `eval_goals` 调用的预设返回值列表（或 callable），按调用顺序消费。

---

## 4. 引擎侧的调用保护

Engine 通过 `_safe_call(fn, fallback)` 包裹所有外部 Agent 调用：
- 正常返回 → 结果直通
- 异常 → 调用 fallback 生成错误 EvalResult，意见入 turn，不让引擎崩溃

通过 `_llm_wrap(role, fn, ctx_size)` 包裹，前后发 `LLM_CALL_START` / `LLM_CALL_END` / `LLM_RESPONSE` 信号。

---

## 5. 未交付组件的接口桩

当前仓库中 ep/ee/et/ex 的实际实现均为 mock/桩。真实 Agent 接入时只需实现上述接口，替换 Mock 即可，引擎代码无需改动。

**已知外部依赖缺口（对接口，非引擎侧 bug）**：
- **executor 无工具执行入参**：`Executor.run(step, ctx)` 只收 ctx 文本，不注入 `tool_exec`。
  第二组② 写真实 executor 时需在接口层加可选入参 `run(step, ctx, tool_exec=None)`，引擎调用处随之注入；
  当前 MockExecutor 不调用工具，契约暂未收口。
- **RAG 经验沉淀未接入**：评估/反思仅收 ctx 文本，无查询与写回通道，见 §6 接口桩。

---

## 6. RAG 经验沉淀 — 接口桩（第二组③，未接入）

架构承诺：评估 Agent（ep/ee/et）参考历史成功/失败模式提高评审准确率、判断 retry/escalate，
反思 Agent 参考类似场景补丁策略，验证过的路径沉淀为经验库。当前仓库未接入——本节省声明契约（③ 只消费不产出），待第二组③ 交付 RAG 服务后接线。

```python
class Experience:
    topic: str        # 题型/场景标签（如 "SQL注入"、"文件上传"）
    outcome: str      # success | failure
    summary: str      # 可执行的结论摘要
    detail: str = ""  # 完整案例文本

class ExperienceStore:
    """经验沉淀 API（外部③ 实现，③ 只调用）。"""
    def query(self, topics: list[str], role: str) -> list[Experience]:
        """按题型检索相关经验，role ∈ ep | ee | et。未命中返回 []。"""
        ...
    def record(self, event: dict) -> None:
        """审计事件 → 经验库落库（引擎在 REPLAN / 步骤升级 / FAILED 等节点调用）。"""
        ...
```

- **查询注入**：`Evaluator.review/step_eval/reflect` 的 `ctx` 末尾由引擎追加 `## 历史经验` 段落（经 `ExperienceStore.query` 预取）；未接入时段落为空，不影响接口。
- **写回时机**：引擎在 replan（含 plan_review_fail / step_escalate / reflect 补丁）、步骤 ESCALATED、任务 FAILED 等审计节点调用 `ExperienceStore.record`，沉淀成功路径与失败原因。未接入时跳过。
- **接线方式（预留）**：`Engine(..., experience_store=store)` 可选入参，为 None 则不注入、不写回。当前引擎未实现该参数，此为一侧契约声明，接入时随 §5 一同收口。
