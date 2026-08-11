# Engine 运行时与持久化

调度器主循环与状态机。实现：`agent/engine.py`。

---

## 1. 状态机

### EngineState — 引擎状态

```python
class EngineState(StrEnum):
    PLANNING = "PLANNING"        # 规划（初始 + 重规划）
    PLAN_REVIEW = "PLAN_REVIEW"  # 计划评审
    SCHEDULING = "SCHEDULING"    # 调度取步骤
    EXECUTING = "EXECUTING"      # 执行步骤
    STEP_EVAL = "STEP_EVAL"      # 步骤验收
    REFLECTING = "REFLECTING"    # 任务反思
    DONE = "DONE"                # 终态：完成
    FAILED = "FAILED"            # 终态：失败
```

### TRANSITIONS — 合法迁移

```
PLANNING     → PLAN_REVIEW | DONE | FAILED
PLAN_REVIEW  → PLANNING | SCHEDULING | FAILED
SCHEDULING   → EXECUTING | REFLECTING | PLANNING | FAILED
EXECUTING    → STEP_EVAL | FAILED
STEP_EVAL    → PLANNING | EXECUTING | SCHEDULING | FAILED
REFLECTING   → PLANNING | FAILED
DONE         → (无)
FAILED       → (无)
```

FAILED 从所有非终态可达（`_fail()` 不校验迁移表，直接写状态）。

`Scheduler` 持有当前状态，`go(target)` 校验转换合法性。

---

## 2. 分发逻辑（`_dispatch`）

| 状态 | 动作 |
|---|---|
| `PLANNING` | `bp is None` → `_do_initial_plan()` 初始规划；`bp is not None` → 跳过规划直接进 PLAN_REVIEW |
| `PLAN_REVIEW` | 调 ep.review(ctx)，FAIL → 重规划，其他 → `clear_revise` → SCHEDULING |
| `SCHEDULING` | `bp.next_step()`；无 ready 且任务完成/全部终态 → REFLECTING；死锁 → `_resolve_stuck` |
| `EXECUTING` | 若 step 绑 `skill_id`：先发 `ENV_CHECK`（探测分类 + 当前活动集）→ 调 executor.run(step, ctx)，记录结果，→ STEP_EVAL |
| `STEP_EVAL` | 调 ee.step_eval(ctx)；PASS → goal eval → SCHEDULING / RETRY → EXECUTING / ESCALATE → PLANNING |
| `REFLECTING` | 调 et.reflect(ctx)；DONE → PLANNING → DONE / REPLAN → PLANNING（终局修订） |

---

## 3. run() — 完整流程

```
RUN_STARTED
  │  env_check: probe_manifest → ENV_CHECK(scope=run_start)
  │
  ▼
_init_run ──understander.understand(raw)──▶ TaskInput(raw_content + goal_list)
  │
  ▼
PLANNING (initial) ──planner.plan(task_input)──▶ _record_plan
  │
  ▼
PLAN_REVIEW ──ep.review──▶
  ├── FAIL  → _mark_revise → _replan → PLANNING
  └── PASS  → _clear_revise → SCHEDULING
                │
                ▼
         ┌── SCHEDULING ◀──────────────┐
         │    │                          │
         │    ▼                          │
         │  EXECUTING ──executor──▶    │
         │    │                          │
         │    ▼                          │
         │  STEP_EVAL ──ee──▶          │
         │    ├── PASS → _eval_goals    │
         │    │   ├── all goals done → REFLECTING
         │    │   └── else →────────────┘
         │    ├── RETRY → EXECUTING
         │    └── ESCALATE → PLANNING
         │
         ▼
      REFLECTING ──et──▶
        ├── DONE → PLANNING → DONE
        └── REPLAN → PLANNING

RUN_END
```

`ENV_CHECK` 打点：run 起始（`RUN_STARTED` 后）发全量快照 `scope=run_start`；每步执行前（`STEP_STARTED` 后、executor.run 前，仅当 step 绑 `skill_id`）发 `scope=step`（探测分类 + 当前活动集 `ws.tools`）。`resume()` 不重复打点——`tool_catalog` 不持久化，load 后 checker 为 None。探测语义/触发时机见 contracts.md §1.7。

`run()`（及 `resume()`）在终态后填充 `engine.run_result`（`RunResult` dataclass）：
`state`（终态字符串）、`completed`（任务是否达成）、`fail_reason`、`replans`/`stalls`/
`cycles`、累计 LLM token 用量 `tokens`，以及 `product`——只聚合 verdict=pass 步骤的
最终产物 `{step_id: result}`，调用方无需再遍历 `ws.steps` 取交付物。

---

## 4. resume() — 断点续跑

```python
@classmethod
def resume(cls, run_id: str, planner, executor, evaluator, *, root=None) -> "Engine":
```

恢复流程：
1. `Workspace.load(run_id, root=root)` 恢复 workspace
2. 从 `ws.meta` 恢复 raw_content（`meta["task"]`）、goal_list（`meta["goal_list"]`）、blueprint、run_status、current_step、fail_reason；goal_list 续跑时重建 `TaskInput`，不重调 understander
3. `_rebuild_from_events()` 从事件流重建 replans/_stalls/_deadlock_attempts/task_completed/_goal_complete
4. `_rebuild_turn(ws)` 从最后一次 REPLAN 之后的非 PASS 意见事件重建 self.turn
5. 短路径：DONE/FAILED 终态直接返回
6. 重入 `_dispatch` 循环从当前状态继续

局限性（已知）：
- PLANNING 状态可恢复：`_dispatch` PLANNING 分支；`bp is None` 时执行初始规划，`bp is not None` 时跳过规划直接进入评审
- 未接入 CLI

---

## 5. Robustness Budgets

所有参数可通过 `model_config.json` 的 `"engine"` 段或构造函数传参覆盖。默认值来自 `model_config.get_engine_config()`。

| 参数 | 默认 | 超限行为 |
|---|---|---|
| `max_cycles` | 100 | 总调度次数兜底，超限 → FAILED + fail_reason |
| `max_replans` | 8 | 重规划次数，超限 → FAILED + fail_reason |
| `max_stalls` | 3 | DAG 签名连续无变化次数（振荡检测），超限 → FAILED |
| `max_deadlock_attempts` | 3 | 调度死锁连续解不开次数，超限 → FAILED |
| `context_budget_tokens` | None (不限) | 上下文 token 预算上限，超过触发 CTX_OVERFLOW 信号；可为 `dict[role→int|None]` 按角色配置 |
| `context_budget_ratio` | 0.9 | 自动计算时 (context_window - max_output) 的占比 |
| `run_token_budget_tokens` | None (不限) | run 级累计 LLM token 用量上限，超限 → FAILED + TOKEN_BUDGET_EXCEEDED |
| `run_timeout_ms` | None (不限) | run 全局超时（ms），超时 → FAILED + RUN_TIMEOUT |
| `phase_timeout_ms` | `{planning:120000, plan_review:60000, executing:180000, step_eval:60000, reflecting:120000}` | 各阶段超时（ms，值可为 None 不限时）；超时 → 阶段降级（见 §10） |

所有 FAILED 终态记录 `fail_reason`，优雅返回不崩进程。run 级累计用量（`engine._run_tokens`）在每次 LLM 调用后累加，随 `_persist_run_state` 落 `ws.meta.run_tokens`，断点续跑续计。

---

## 6. 持久化时机

- **每次状态迁移**：`_go()` → `_persist_run_state()` → 更新 `ws.meta`（run_status/current_step/fail_reason）→ `ws.sync()`
- **每次重规划落账**：`_record_plan()` → `ws.sync()`
- **events.jsonl**：`add_event` 即时追加，不等待 sync
- **RUN_END**：最终 `_persist_run_state()` 确保终态落盘

---

## 7. 死锁处理

`_resolve_stuck()` — SCHEDULING 无 ready 时的处置：
1. 检查是否有 REVISE 状态节点 → 必须先清 REVISE（评审通过后才调度）
2. 否则 → 真死锁 → 注入重构提示重规划
3. 连续 `max_deadlock_attempts` 次解不开 → FAILED

---

## 8. 上下文装填与压缩

`_assemble_ctx(role, **kw)` → 委托 `workspace.assembler.assemble(role, **kw)`，返回 `(ctx, system, over)`。

引擎侧仅合并 `system + ctx` 为单串文本传给外部 Agent。assembler 已自动 emit `CTX_ASSEMBLED` / `CTX_OVERFLOW` 等信号。

### 压缩接线

Engine 通过构造函数 `compress` 参数传入 LLM 语义压缩回调：

```python
engine = Engine(..., compress=llm_compress_fn)
```

- `compress` 回调签名为 `(prompt: str, content: str) -> str`——`prompt` 是组装器生成的压缩提示词（压缩目的/优先级/占比/agent 目的/按需压缩方式），`content` 是超预算的可压缩组件原文；返回压缩后的文本
- 上下文预算按 **token** 计：`_role_budget(role)` 取 构造传参 `context_budget` > per-role config（`context_budget_tokens` dict）> 全局标量 config > 自动计算 `(context_window - max_output) × context_budget_ratio`；经 `assemble(role, budget=...)` 传入组装器
- 缺少 `compress` 回调、或 LLM 输出仍超限/抛异常时，溢出走机械降级（按 优先级+占比 逐档推进），无 LLM 往返

`_state_context(budget)` — 当某 budget 接近上限时，注入"临近 FAILED"的 detail 文本给 planner。

---

## 9. 工具注入

Engine 持有 `ToolRegistry` 实例（`self._tool_registry`），在 `_init_run()` 和 `resume()` 中注入上下文：

```python
self._tool_registry.set_docs(self.workspace.docs)
self._tool_registry.set_workspace(self.workspace)
```

`get_doc` / `get_record` 是 `ToolRegistry.__init__` 中注册的闭包，捕获 `self` 引用，无需外部注入即可工作。工具规格通过 `tool_registry.openai_tool_specs()` 生成，供 `chat_with_tools` 使用。

**环境检查器**（`agent/checks.py` `SkillEnvProbe`）经 `Engine(checker=...)` 显式注入，或按 `tool_catalog` 自动派生 `SkillEnvProbe(tool_catalog)`；**无 catalog → checker=None 全跳过**（不探测、不发 `ENV_CHECK`）。apply_tool 在申请时对每工具做只读探测并随返回带 `probe`（见 §1.6/§1.7）。

---

## 10. 阶段超时机制

实现：`agent/timing.py`（PhaseTimer）。**协作式超时，不走 `threading.Timer` 中断**——Python 线程中断不可靠。上下文管理器退出时自动算耗时并判断超时；长循环内用 `check()` 主动查询，超时由调用方决定退出/降级。

### PhaseTimer API

```python
t = PhaseTimer("executing", deadline_ms=60_000)   # deadline_ms=None 不限时
with t:                                           # __enter__ 计时起点;__exit__ 自动算耗时 + 判断超时
    result = do_work()
if t.timed_out:
    handle_timeout(t)                             # 读 timed_out / elapsed_ms(ms) / remaining_ms
```

| 成员 | 说明 |
|---|---|
| `deadline_ms` | 超时上限（ms）；None = 不限时 |
| `elapsed_ms` | 已耗时（ms，`_stop()` / `check()` 刷新） |
| `timed_out` | 是否超时 |
| `check()` | 主动查询：返回 True = 未超时，False = 已超时（并置 `timed_out`） |
| `remaining_ms` | 剩余毫秒（无 deadline 返回 None） |

### 引擎接线

- **run 级**：`run()` / `resume()` 主循环顶部 `run_timer.check()` 逐轮检查 `run_timeout_ms`；超时 → `RUN_TIMEOUT` 信号 + `_fail("run 全局超时")`
- **阶段级**：每个非调度状态用 `PhaseTimer(phase, deadline_ms=_phase_deadline(phase))` 包住 agent 调用；`_phase_deadline(phase)` 从 `phase_timeout_ms` 配置读 deadline；超时 → `PHASE_TIMEOUT` 信号（phase / elapsed_ms / step_id）

### 各阶段超时兜底语义

| 阶段 | 超时处置 |
|---|---|
| planning | `PHASE_TIMEOUT`；初始规划超时且无 bp 产出 → `_fail("初始规划超时")` |
| plan_review | 置 `EvalResult(FAIL, "计划评审超时")` → 走 FAIL 分支：标记 revise + replan |
| executing | 置 `ExecResult(observation="执行超时")` → 正常进 STEP_EVAL（验收看到超时观察，据此判 retry / escalate） |
| step_eval | 置 `EvalResult(ESCALATE, "步骤校验超时")` → 走 ESCALATE 分支：标记 ESCALATED + replan |
| reflecting | 置 `EvalResult(REPLAN, "反思超时")` → 走 REPLAN 分支 |

设计意图：**超时不中断进程，而是降级为一条可被下游处理的观察/判语**——每个超时都转成状态机里的一个正常输入，不新增异常路径。执行超时的 observation、验收超时的 ESCALATE 都会落进事件流（history / agent_comm 投影），可审计、可断点续跑。

配置键见 `design/model_config.md`：`run_timeout_ms` / `phase_timeout_ms`；单次 LLM 调用总超时 `llm_total_timeout_ms`（含重试）由 `agent/llm_api.py` 处理，属 LLM 层，不在此。
