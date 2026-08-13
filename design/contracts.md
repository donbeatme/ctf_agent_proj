# Agent 接口契约

引擎与三个外部 Agent 的接口约定。实现：`agent/planner.py`、`agent/executor.py`、`agent/evaluator.py`。

---

## 0. 任务理解层 — 输出 API

任务理解层（外部 ②）向引擎交付结构化任务的输出 API。实现：`agent/understander.py`。
本仓库另提供可替换实现 `agent/challenge_intake.py`：

```python
class ChallengeUnderstander(TaskUnderstander):
    # 多源 raw → classify(CATEGORY_KEYWORDS+附件启发) → raw_content 含 challenge_type
    # + goal_list(find_flag / solve_<type> / …)
    def understand(self, raw: dict) -> TaskInput: ...

def parse_challenge(raw: dict) -> dict:
    # 前端「题型判定」预览：{task, classification, goals_preview}
```

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

#### 技能检索（DocStore）契约

`Planner(docs=...)` 注入技能库检索器。接口桩 + 参考实现见 `agent/planner.py` / `agent/skills.py`：

```python
class DocStore:
    def search(self, task: dict) -> list[tuple[str, str]]: ...
    def load_doc(self, doc_id: str) -> str | None: ...
```

- **search** — 按题面检索，返回 `[(doc_id, 全文)]`；planner 原样 `set_doc(doc_id, text)`，**保留真实 doc_id** 才能把 `skill_id` 绑到技能文档上（勿用 `doc{i}` 重命名）。
- **load_doc** — 按 doc_id 取未注册文档全文（如子文档），`None` 表示不存在；planner 的 `get_doc` 工具在 `ws.docs` 未命中时兜底调用它。
- **参考实现** `CtfSkillsDocStore`（`agent/skills.py`）：扫 vendored `skills/ctf-skills`（Agent Skills 库，11 类 ~114 文档），按分类关键词路由，**只注册命中分类的 SKILL.md**（含 quick reference）；子文档正文不预灌，经 `load_doc`/`get_doc` 按需取。doc_id 拍平目录层级且受 `ID_PATTERN` 约束（如 `ctf-crypto`、`ctf-crypto.rsa-attacks`）；SKILL.md 内相对链接改写为可 `get_doc` 的引用。

### 内部 LLM 调用

`Planner._default_llm()` 生成闭包，签名 `(*, system=None, prompt=None, messages=None, **kw) -> str`：
- 调用 `llm_api.chat_with_tools(model=role_model("planner"), tools=PLANNER_TOOLS)`
- `PLANNER_TOOLS = tools.openai_tool_specs(names={"get_doc"})` — 从工具库按白名单导出，**不与执行工具目录混用**（design/tools.md §4）
- 工具：`get_doc`（从 workspace.docs 注册表取技能文档全文）
- `**kw` 透传给 LLM，允许测试注入 mock（`Planner(llm_call=mock_fn)`）

---

## 1.5 CTF 工具目录（声明式）— ctf-skills 工具与依赖包装

把 ctf-skills 的工具/依赖声明包装进现有体系，供执行层/沙箱与规划层消费。**纯声明，不接执行**——目录工具不可调用、不注册执行函数；executor 怎么调工具属于第二组② 的交付。实现：`agent/ctf_skill_tools.py`。

**来源与许可**：skill 库 vendored 自 [ljagiello/ctf-skills](https://github.com/ljagiello/ctf-skills)（MIT © 2026 Lukasz Jagiello，LICENSE 见 `skills/ctf-skills/LICENSE`）；`TOOL_MANIFEST` 手抄自 `scripts/install_ctf_tools.sh`。依赖工具（pwntools/angr/ghidra 等）为独立开源项目，遵循各自许可证。

**依赖更新**：技能库 re-vendor 与 `TOOL_MANIFEST` 同步流程（含漂移守卫测试）见 README「依赖与更新」。

### TOOL_MANIFEST schema

`list[dict]`，每工具一条（~70 条，跨安装方式去重，主方式优先级 `pip > apt > brew > gem > go > manual`，次要方式记入 `alt_methods` 不进 ws.tools）：

```python
{"tool_id": str,         # 目录 id = openai spec name;受 ID_PATTERN 约束
 "name": str,            # 显示名/包名
 "install_method": str,  # pip|apt|brew|gem|go|manual
 "install_command": str, # 单行安装命令(已编码下载)
 "verify_check": str,    # CLI 命令名 或 "import <模块>"(manual 为空)
 "description": str}     # 一行描述
```

### 查询 API（CtfSkillToolCatalog）

| 方法 | 说明 |
|---|---|
| `as_tools_list()` | ws.tools 可注入列表（OpenAI function-calling 形状），`Engine(tools=...)` 直接用 |
| `allowed_tools(category)` | frontmatter allowed-tools（agent 原生工具白名单） |
| `compatibility(category)` | frontmatter compatibility（运行时要求） |
| `verify_checks()` | 全清单校验项（CLI 名 + import 模块） |
| `install_commands(category)` | 该分类 SKILL.md `## Prerequisites` 安装命令行（含脚本外的额外依赖，如 torch） |
| `installer_path` | vendored `scripts/install_ctf_tools.sh` 路径（整库依赖下载/更新入口引用） |

### 消费路径

1. **引擎注入（声明式）**：`Engine(tools=CtfSkillToolCatalog().as_tools_list())` → `set_tools` → `ws.tools` → `ToolRegistry.openai_tool_specs` 合成 function-calling 规格；ToolComponent 渲染为 planner/executor 上下文"可用工具"列表。工具**可见但不可调用**。
2. **执行层/沙箱（第二组②）**：读 `ws.tools`（`get_tool(tool_id)`/`get_tool_description`）按 id 挑工具；环境准备用 `install_commands(category)`/`verify_checks()`/`installer_path` 触发下载更新（`bash install_ctf_tools.sh all` / `--verify` / `--force`）。**调用工具的执行体不属于本模块**。
3. **规划层参考**：`allowed_tools(category)`/`compatibility(category)` 供 planner 判断分类所需 agent 原生能力/运行时前提。

---

## 1.6 工具目录（动态申请）— apply_tool / remove_tool + 活动集

把 §1.5 的声明式目录从"静态全量注入"改为**动态按需注入**，镜像 docs 的"索引 + 按需取"模式。实现：`agent/ctx.py`（ToolDirectoryComponent）、`agent/tools.py`（apply_tool/remove_tool）、`agent/workspace.py`（活动集）。

- **目录（菜单）**：`ToolDirectoryComponent`（`agent/ctx.py`）按 `ws.tool_catalog` 渲染完整 TOOL_MANIFEST（id + 一句话描述），**只读投影不进活动集**；planner 与 executor 都接收全量目录，**不做分类过滤、不按 skill 绑定门槛**——题目需要什么工具由 agent 现场判断，apply_tool 对完整清单全开放。
- **活动集 `ws.tools`**：**默认空**；agent 经 `apply_tool(tool_ids)` 申请后组件把对应工具加进活动集（ToolComponent 渲染），`remove_tool(tool_ids)` 移除（有申请就有删除）。
- **内置工具**（`agent/tools.py` 注册到 ToolRegistry）：

  ```python
  apply_tool(tool_ids: list[str]) -> {"added": [...], "unknown": [...], "probe": {...}}
  remove_tool(tool_ids: list[str]) -> {"removed": [...], "missing": [...]}
  ```

  - `apply_tool`：从 `ws.tool_catalog`（`get_tool`）校验并取 description，逐 id 加入活动集；清单里没有的 id 进 `unknown`。返回附带 `"probe": {tid: {status, check}}`——每申请工具做**只读环境探测**（见 §1.7），只增 key 向后兼容。
  - `remove_tool`：从活动集移除，幂等；未激活的 id 进 `missing`。
  - catalog **单一来源**为 `ws.tool_catalog`（经 `Engine(tool_catalog=...)` 注入），**不持久化**（state.json 不含）。
- **通用接口**：`Executor.run(step, ctx, tool_exec=None)` —— `tool_exec: (name, args) -> dict` 为引擎注入的工具执行回调（`ToolRegistry.call_tool`），apply_tool/remove_tool/get_doc/get_record 经此可调；MockExecutor 忽略该参。
- **消费路径**：planner 只读目录（规划时参考可用能力，不调用 apply_tool）；executor（第二组②）在 ctx 里读目录 → 调 `apply_tool` 申请 → 从 `ws.tools`（ToolComponent 渲染的"可用工具"）取活动集执行。工具仍是声明式（无执行体），调用实现归第二组②。

## 1.7 环境检查（只读探测）— SkillEnvProbe → ENV_CHECK 写 run.log

工具动态分配（apply_tool）了，但**工具要求的资源（二进制/沙箱/依赖）未必配得了**——
有的环境就是没有 gdb、没有 docker。为此加**只读环境检查钩子**：真实探测工具可用性、
分类就绪度、沙箱运行时，把"缺工具/缺沙箱/分类配不了"写进 run.log 供审计。实现：
`agent/checks.py`（`SkillEnvProbe` + `SANDBOX_CATEGORIES` + `default_sandbox_probe`）。

- **③ 只读边界**：探测器**只做只读环境探测**——CLI 用 `shutil.which`，模块用
  `importlib.util.find_spec`（不真正 import，快且安全）；**不装依赖、不建沙箱、不执行任务**。
  真正安装依赖/建沙箱/执行仍是第二组②职责。
- **探测语义**（`probe_tool`）：读 manifest 的 `verify_check`——
  空（manual）→ `manual`；`import X` → `find_spec`；否则 CLI 名 → `which`。
  结果 `status ∈ available|missing|manual|unknown`。探测异常一律归 `unknown`，不崩调用方。
- **沙箱判定是"探测"不是"创建"**（`probe_sandbox`）：`{category, needed, available}`——
  `needed = category ∈ SANDBOX_CATEGORIES`（默认 `{ctf-pwn, ctf-reverse, ctf-malware}`）；
  `available = sandbox_probe(category)`（默认探测 docker/podman CLI 是否存在）；
  不需要沙箱的分类 `available=None`。`需要但不可用` 即记录为"沙箱缺失"。
- **分类就绪度**（`probe_category`）：`{category, exists, compatibility, allowed_tools, install_cmds(前3), sandbox}`，
  复用 `CtfSkillToolCatalog.compatibility/allowed_tools/install_commands`。
- **全量快照**（`probe_manifest`）：`{total, available, missing, manual, unknown, missing_list(tool_id+check), sandbox}`，
  遍历 `catalog.manifest`，sandbox 以任一需隔离分类（ctf-pwn）代表容器运行时在不在。
- **触发时机**：
  1. `apply_tool` 逐工具探测：返回追加 `"probe": {tid: {status, check}}`（只增 key，向后兼容）。
  2. **run 起始**：`Engine.run()` 发 `RUN_STARTED` 后，`probe_manifest()` → `Signal.ENV_CHECK scope="run_start"`。
  3. **每步执行前**：EXECUTING 分支 `STEP_STARTED` 后、executor.run 前，若 step 绑了 `skill_id`，
     取 `cat = skill_id.split(".")[0]`，探测**当前活动集**（`ws.tools`）+ 该分类 →
     `Signal.ENV_CHECK scope="step", step_id=...`（覆盖"缺工具"——申请了但环境没有）。
- **接线**：`Engine(checker=...)`——显式传入优先；否则按 `tool_catalog` 派生
  `SkillEnvProbe(tool_catalog)`；**无 catalog → checker=None 全跳过**（现有测试不受影响）。
- **落日志**：`EngineLogger.on_env_check` 写 `[engine] check[...]` 根级行——`run_start` 全量快照
  （可用 X/70、缺失 Y、manual Z、沙箱运行时 docker/podman 有/无、缺失明细截断前 15 条）；
  `step` 该步分类 compat/install_cmds 数/沙箱 needed+available + 活动集缺工具清单。
  run-end 汇总区加一行 `环境检查: 缺工具 Y/70  manual Z  sandbox=有|无`。

---

## 2. Executor — 执行 Agent

```python
class Executor:
    def run(self, step: Step, ctx: str, tool_exec=None) -> ExecResult

@dataclass
class ExecResult:
    observation: str              # 人类可读的执行描述
    result: dict | None = None    # 结构化的执行产物
    tool_calls: list[dict] | None = None  # 工具调用记录
```

- `ctx` — Engine 通过 `assembler.assemble("executor", step_id=..., ...)` 组装的上下文文本
- `tool_exec` — **可选**工具执行回调 `(name: str, args: dict) -> dict`，由引擎注入
  （`ToolRegistry.call_tool`）；经此可调 `apply_tool`/`remove_tool`/`get_doc`/`get_record` 等内置工具。
  向后兼容：缺省 `None`，MockExecutor 忽略该参
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

`fn` 为 `(step, ctx, tool_exec=None) -> ExecResult`，优先级高于 observation/result；兼容旧 2 参 `(step, ctx)`。

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
- **executor 工具执行入参已接线**：`Executor.run(step, ctx, tool_exec=None)` 已收口（§2），引擎在 EXECUTING
  分支注入 `tool_exec=ToolRegistry.call_tool`（apply_tool/remove_tool/get_doc/get_record 经此可调）；
  工具仍为声明式（ws.tools 无执行体），调用实现归第二组②。
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
