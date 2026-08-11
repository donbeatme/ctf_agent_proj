# 上下文组装（CtxAssembler / CtxComponent）

上下文组装子系统：把 workspace 状态按角色渲染成上下文文本，并在超预算时压缩。实现：`agent/ctx.py`。

相关文档：`design/workspace.md`（状态源 / 组件注册表）、`design/engine.md` §8（引擎侧压缩接线）、`design/signals.md`（CTX_* 信号）、`design/contracts.md`（ingest 落账契约）。

---

## 1. 投影模型（关键不变量）

**组件是 workspace 的只读投影，不持独立数据副本。** 数据唯一真值在 workspace（`ws.blueprint` / `ws.events` / `ws.docs` / `ws.tools`），引擎只改 workspace；每次 `assemble` 组件从 workspace 现投影——改 workspace 即自动反映，无需手动喂数据，不存在"更新 dag 又更新 dag 组件"的双重写入。组件自己只剩瞬态压缩档位 `level`（每次 assemble 重置）。

数据流向：

```
store (workspace)  ──assemble 前向投影──▶  ctx（给 agent 的文本）
     ▲
     └───────────────ingest 反向装填───────── 模型返回
```

- **前向**：`assemble(role, ...)` 把 workspace 状态渲染成上下文文本（组件只读）。
- **反向**：`ingest(role, ...)` 把 LLM / 执行 / 评估的返回写回 workspace（见 §4）。

生命周期事件（引擎打点，组装器机械分发）：`on_replan` / `on_plan_review_pass` / `on_step_record` / `on_run_end`。**每个组件子类声明自己的生命周期**（system/task 永存、AgentComm 重规划即清、Docs 计划评审通过即清），组装器只机械执行。

---

## 2. CtxComponent 基类

### 2.1 压缩元数据（类属性或构造参数覆盖）

| 字段 | 默认 | 含义 |
|---|---|---|
| `key` | `""` | 组件唯一标识（压缩保护 `protect` 点名、日志） |
| `priority` | `0` | 压缩保护顺序，**越小越先压** |
| `floor` | `0` | 不可再压的原文量下限（token）；`at_floor()` 判断 |
| `LEVELS` | `("raw",)` | 压缩档位序列；单档即 `can_advance` 恒 False → 永不压 |
| `target` | `"ctx"` | `"ctx"` 上下文正文（受预算压缩）/ `"system"` 并入系统提示词（永不压、不进预算） |
| `compress_methods` | `""` | 该组件可用的"按需压缩方式"说明（喂给压缩 LLM 的提示词；空 = 不可压，保留原文） |
| `anchor` | `False` | 锚点组件：永不压（`can_advance` 恒 False + 排除在压缩候选外，显式声明不靠 priority 碰运气） |
| `level` | `0` | 当前档位索引（0 = raw；每次 assemble 重置） |

### 2.2 生命周期 API

| 方法 | 作用 |
|---|---|
| `create(ws, **kw)` | 进入作用域：注入 workspace + 从 kw 投影本轮输入 |
| `delete()` | 离开作用域：释放引用 |
| `created` | 是否在作用域内 |
| `update(**kw)` / `clear()` | 内容增长 / 清空但组件保留（投影模型下 `update` 已废弃） |
| `sync(ws)` | 内容写回 workspace 存储（投影模型下无独立内容，默认无操作） |
| `render()` | 按当前档位拼文本（子类覆盖） |
| `size()` | 当前渲染文本的 token 数（`count_tokens(render())`，溢出压缩的度量） |
| `can_advance()` | 能否再压一档：锚点 / 已到最高档 / 已到下限（再压就是假信息）→ False |
| `advance_level()` | 推进一档；已到最高档返回 False |
| `at_floor()` | `size() <= floor` |
| `precompress(ws, compress)` | 预热语义压缩缓存（engine 非决策时刻调用），不改变档位 |
| `on_replan` / `on_plan_review_pass` / `on_step_record` / `on_run_end` | 生命周期事件（子类声明自己的行为） |

---

## 3. CtxAssembler

组装器无业务逻辑——机械执行组件声明的生命周期，数据真值都在 workspace。

### 3.1 注册与组件表

| 方法 | 作用 |
|---|---|
| `register(role, *comps)` | 注册组件**实例** |
| `register_class(role, *specs)` | 注册组件**类**（懒加载：首次 assemble 该 role 才实例化；specs 为子类或 `(cls, args, kwargs)` 元组） |
| `components(role=None)` | 取组件表（按注册序；触发懒加载） |
| `component_state(role)` | 组件状态快照（key / 当前档位 / size / target，供 logger 记录 `ctx_asm` 明细） |

懒加载避免 Workspace/MockWorkspace 构造时预先创建全部组件实例。

### 3.2 assemble — 前向投影

```python
ctx, system, over = assembler.assemble(role, budget=None, protect=None, purpose=None, **kw)
```

流程：

1. 每个组件重置档位 `level=0` → `create(ws, compress=..., **kw)` 投影本轮输入
2. `target="ctx"` 组件拼正文；超预算 → 溢出压缩（LLM 优先，机械降级兜底）
3. `target="system"` 组件拼系统提示词，永不压、不进 ctx 预算
4. 注入 signals 时自动 emit `CTX_ASSEMBLED` / `CTX_OVERFLOW` / `CTX_COMPRESSED`（见 §6）

- `protect`：点名压缩保护（这些 key 不参与压缩，保留原文）
- `purpose`：当前触发压缩的 agent 目的（默认取 `ROLE_PURPOSE[role]`，可覆盖）
- 返回 `(ctx, system, over)`；`over > 0` = 压无可压仍超预算

### 3.3 ingest — 反向装填

```python
assembler.ingest(role, **returns)
```

把模型返回写回 workspace（assemble 的逆，写盘细节归 workspace，组件不持副本）。角色处理器：

| role | 落账 |
|---|---|
| `planner` | `set_blueprint` + 追加 REPLAN 边界事件（`ReplanDetail`） |
| `executor` | `record_tool_call` / `record_tool_result`（trace 通道）+ `dag.step.result`（供 ee 读产物） |
| `evaluator_plan` / `evaluator_step` / `evaluator_task` | `record_opinion`（agent_comm 通道） |

`CTX_INGEST` 信号由 **engine** 在 ingest 后发射（组装器自身不发射）。

### 3.4 事件分发与预热

- `dispatch(event, **kw)` — 生命周期事件广播，逐组件 `on_<event>`。组装器作为 SignalBus 订阅者：`on_replan` / `on_plan_review_pass` / `on_run_end` 转发到组件
- `clear(scope=None)` — 按 role 重置组件内容（组件保留；scope=None 清全部）
- `precompress(role)` — 预热某 role 组件的语义压缩缓存（engine 在非决策时刻调用），提前把 history/trace 的 delta 折成摘要并落盘，决策时摘要档只读缓存、零 LLM 往返

### 3.5 判重

`_join` 跨组件**按行判重**：重复行只保留第一个出现。双渲染（同数据源被多组件投影 / 同数据两通道进 ctx）的兜底，组件保持原文语义，不做一刀切过滤。

---

## 4. 组件清单（9 个）

| 组件 | key | priority | anchor | LEVELS | 投影源 |
|---|---|---|---|---|---|
| SystemPrompt | `system_prompt` | 99 | ✓ | 单档 | `kw["system"]` |
| Task | `task` | 99 | ✓ | 单档 | `kw["raw_content"]` + `kw["goal_list"]` |
| AgentComm | `agent_comm` | 98 | ✓ | 单档 | 最近 replan 之后的非 pass 意见事件 |
| Dag | `dag` | 5 | ✗ | raw / skeleton | `ws.blueprint` |
| History | `history` | 2 | ✗ | raw / index / summary | STEP_RECORD + REPLAN 事件 |
| Docs | `docs` | 3 | ✗ | raw / ref | `ws.docs` |
| ToolDirectory | `tool_dir` | 4 | ✗ | raw / ref | `ws.tool_catalog` |
| Tool | `tools` | 4 | ✗ | raw / ref | `ws.tools` |
| Trace | `trace` | 1 | ✗ | raw / index / summary | 最近 replan 之后的 USE_TOOL / TOOL_RESULT 事件 |

### 压缩优先级阶梯

```
trace=1  >  history=2  >  docs=3  >  tools/tool_dir=4  >  dag=5
(先压)                                          (最后压)
agent_comm=98 / task=99 / system_prompt=99  — 锚点，永不压
```

机械压缩候选按 `(priority, -size())` 排序：**优先级低 + 占比大**先压。

### SystemPromptComponent

`target="system"`，锚点。每次 `plan()` 重建，只带本轮触发；渲染文本由 planner 预先拼好经 `kw["system"]` 传入（契约文案归 planner 单一持有）。

### TaskComponent

锚点。run 内不变，全局保留。`goal_list` 只接受 Goal 对象（任务理解层产出）；`raw_content` 原样 JSON 渲染。单档 → `can_advance` 恒 False → 永不压。

### AgentCommComponent

本轮评估意见（agent 通信）：**pass 是闸门**（不产出内容），非 pass（FAIL / RETRY / ESCALATE / REPLAN）才进 ctx。投影最近一次 replan 事件之后的非 pass 意见事件（kind ∈ `plan_review` / `step_eval` / `reflect` / `scheduling`）。作用域从事件流推导，不持瞬态——断点续跑后边界照样可推导。生命周期：`on_replan` 清空本轮（每轮重规划后只留本轮）。

### DagComponent

当前计划 DAG（`ws.blueprint` 只读投影）。决策核心最后压。`kw["step_id"]` 给定则只投影该步（executor 的 `dag.step` 上下文），否则全量。档位：raw（完整 blueprint JSON）→ skeleton。**骨架化硬约束**：保 id / status / attempts / depends_on 边（+ skill_id），只切 instruction/criterion 散文——`apply_patch` 对真实 blueprint 合并，模型看不见被压掉的 id 就不会非法引用。

### HistoryComponent

执行历史（run 的账本轨迹）：全局 STEP_RECORD 轨迹 + replan 边界。工具轨迹（trace）与评估意见（agent_comm）走各自通道，不进 history。

**按 verdict 折叠**：只有 PASS 证据（`verdict="pass"`）可压——原文 → uuid 引用（`get_record` 可展开）→ LLM 摘要；其余（失败/升级/评审/重规划等非 PASS）永远保留原文逐条透传。折叠的选择由 verdict 决定，与事件位置/数量无关。

档位：raw（全文）→ index（PASS 换 uuid 引用）→ summary（PASS 折成 LLM 摘要）。

语义压缩（摘要档）关键约束：

- **增量**：只把自上次折叠以来新出现的 PASS 事件发给 compress；折叠标记 `_folded_passes` 持久化到 `ws.summaries`——跨 replan / 断点续跑不重付 LLM
- **降级**：未注入 compress → 停在 index 档，渲染索引，不装假摘要；注入但自标记以来有新 PASS 事件（需新鲜折叠）→ 机械兜底同样停在 index 档，摘要只由 `precompress` 预热填充，保证机械兜底确定性
- 折叠标记取 `min`：events 追加写只可能落后，不会超前（截断兜底）

### DocsComponent

技能库文档索引（`ws.docs` 只读投影）。这些是技能库检索出的**参考文档片段**，不是 agent 自己的 skill——ctx 只渲染紧凑索引（id + 一句话描述，取 doc 首行），**全文经 `get_doc` 原生工具按需取**，不在 ctx 里塞整篇文档。档位：raw（id + 一句话）→ ref（仅 id）。

生命周期：planning ↔ plan_review 循环内不清空；`on_plan_review_pass` 清掉**未绑定到步骤**的参考文档（计划用毕释放 ctx），绑定 `skill_id` 的保留供 executor 执行时查阅。

### ToolDirectoryComponent

工具目录菜单（`ws.tool_catalog` 只读投影）。渲染 TOOL_MANIFEST 全量清单（id + 一句话描述）——是"可申请清单"，**不写进 `ws.tools`**（申请前工具不可用）。不做分类过滤、不按 step 绑定门槛：题目需要什么工具由 agent 现场判断，apply_tool 对完整清单全开放（消费语义见 contracts.md §1.6）。planner 只读参考；executor 经 apply_tool/remove_tool 申请删除。档位：raw（全量菜单）→ ref（仅 id）。

### ToolComponent

活动工具集（`ws.tools` 只读投影）。**动态**：默认空，`apply_tool` 申请后经 `ws.add_tools` 并入、`remove_tool` 经 `ws.remove_tools` 收缩——有申请就有删除。与本地协议解耦：`normalize` 接收标准工具格式（OpenAI function-calling / MCP），`ws.tools` 只存归一结果，本地 `@tool` 结构不泄漏进来。档位：raw（全目录）→ ref（仅 id）。

### TraceComponent

本轮工具调用轨迹（USE_TOOL + TOOL_RESULT）：模型知道自己正在干什么的过程记录，和最终的 output（决策文本）区分开。作用域从事件流推导（最近一次 replan 之后），replan 事件自身推进边界，不持瞬态清理状态，`Workspace.load` 断点续跑后边界照样可推导。构造参数 `agent` 限定只投影某角色的轨迹（executor 注册时传 `agent=Role.EXECUTOR`），None = 全角色。

档位：raw（全文）→ index（每条一行 uuid 引用）→ summary（LLM 摘要）。**摘要按事件集签名缓存**：本轮轨迹没变就读缓存，跨 replan 边界变化即重算；未注入 compress 停在 index 档，不装假摘要。

---

## 5. 压缩策略

溢出时（`count_tokens(ctx) > budget`）按 `_compress_overflow` 走：

1. **LLM 优先**：注入 compress 回调时，把超预算内容 + 压缩提示词交给 LLM（可用更便宜模型），LLM 决定怎么压、压到多少
   - 可压部分（声明了 `compress_methods` 且未受保护）→ 送 LLM
   - 保留部分（受保护 / 不可压如 task、agent_comm）→ 原文保留，与 LLM 输出拼回
   - LLM 输出 + 保留原文**仍超预算** → 返回 None，交给机械降级
2. **机械降级兜底**：按 优先级 + 占比 逐档推进（索引替换 / 骨架化 / 摘要）
   - 收敛确定：档位有限且单调，同输入同输出，不振荡
   - 全部到下限仍超预算不硬压，由调用方读 `over` 调大预算或报错
   - 摘要档只读 `precompress` 预热缓存，不重付 LLM 往返

压缩提示词（`_build_compress_prompt`）包含：压缩目的 / 压缩优先级 / 占比 / 当前触发压缩的 agent 目的（`ROLE_PURPOSE`，assemble 可覆盖）/ 按需压缩方式 / 保留原文名单。

---

## 6. 信号

组装器注入 signals（engine 构造时 `assembler.signals = engine.signals`）后，`assemble` 自动发射：

| Signal | 参数 | 触发 |
|---|---|---|
| `CTX_ASSEMBLED` | role, total_tokens, budget, overflow, components（[{key, level, size, target}]）, system_tokens | 每次 assemble |
| `CTX_OVERFLOW` | role, overflow, method | 压缩后仍超预算（over > 0） |
| `CTX_COMPRESSED` | role, method, compressed（[{key, from_level, to_level, delta}]）, total_after, overflow_after | 本次 assemble 发生了机械降档 |

`CTX_INGEST`（role, detail）由 **engine** 在 ingest 后发射。信号字段与 run.log 的 `[ctx_asm]` / `[compress]` 行格式见 `design/signals.md`。

---

## 7. 角色注册表

Workspace 在 `_init_assembler()` 中按角色注册（懒加载）：

| 角色 | 组件（按拼接序） |
|---|---|
| planner | SystemPrompt / Task / AgentComm / Dag / History / Docs / ToolDirectory / Tool / Trace |
| executor | SystemPrompt / Task / AgentComm / Dag / Docs / ToolDirectory / Tool / Trace（agent=EXECUTOR） |
| evaluator_plan | SystemPrompt / Task / Dag / History |
| evaluator_step | SystemPrompt / Task / AgentComm / Dag / History |
| evaluator_task | SystemPrompt / Task / AgentComm / Dag / History |

---

## 8. 相关测试

| 测试 | 覆盖 |
|---|---|
| `tests/test_ctx.py` | 上下文组装、组件注册、压缩 |
| `tests/test_assembler.py` | CtxAssembler 信号响应 |
| `tests/test_compress.py` | 机械压缩策略 |
| `tests/test_tool_components.py` | 工具组件渲染 |
| `tests/test_history_ctx.py` | History 组件渲染导出（需真实 LLM） |
