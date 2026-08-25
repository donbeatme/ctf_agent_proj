# 事件编码与单源设计

事件系统统一为**单源**:所有事件(外围组件 / 引擎信号 / agent 决策链)只经
`opslog.emit` 写一份 canonical 流(ops.log),带全进程单调 `seq` 与
`run_id / node_id / round` 三个定位字段;run.log(人类可读)与 events.jsonl
(断点续跑账本)是它的投影,不再各自独立成源。实现:`opslog.py`、
`agent/signals.py`、`agent/workspace.py`、`main._ops_sink`。

---

## 1. 单源架构

```
        ┌─ 外围组件(adapter / sandbox / ssh / executor) ─ emit("adapter","submit",...)
        │─ 引擎信号(SignalBus.emit) ── bridge → emit("engine", signal, **kw)
        │─ 决策链(workspace.add_event) ─────→ emit("ws", kind, ...)
        ▼
   opslog.emit ── 唯一写入点 ──► ops.log(canonical 追加式,seq 单调)
        │
        └─ sinks 投影:
             run.log        ← EngineLogger(行为订阅 SignalBus,人类可读,职责不变)
             events.jsonl   ← workspace.add_event 直写(决策链)+ _ops_sink 投影(外围)
```

### 1.1 定位字段

| 字段 | 含义 | 来源 |
|---|---|---|
| `seq` | 全进程单调序号,ops.log 追加顺序即事件顺序 | `emit` 在 `_lock` 内自增 |
| `run_id` | run 归属 | `set_run_context(run_id=...)` 线程环境,可显式覆盖 |
| `node_id` | DAG 步骤 id | 引擎进入步骤时 `set_run_context(node_id=step.id)`;决策链默认=step_id |
| `round` | 执行轮次(步骤 attempt 或步骤内工具调用轮) | 引擎 attempt 递增 / `chat_with_tools` 每轮设 |

`set_run_context` 语义:显式传参覆盖环境值;显式 `None` **省略该字段**(不留环境残留);
不传则回落环境。`get_run_context()` 返回当前线程环境(工具循环保存/恢复用)。

### 1.2 三条旧线的收编

| 旧线 | 收编方式 |
|---|---|
| `opslog.emit` → ops.log(外围审计) | 保留为 canonical 源,新增 seq / run_id / node_id / round |
| `SignalBus` → run.log(引擎信号) | `SignalBus.emit` 先镜像 `engine.<signal>` 进 ops.log,再分发给订阅者(EngineLogger / CtxAssembler 行为不变);run.log 仍是其人类可读投影 |
| `workspace.add_event` → events.jsonl(决策链) | 持久化实例把 `ws.<kind>` 镜像进 ops.log;events.jsonl 只保留为断点续跑投影 |

### 1.3 投影分流(`main._ops_sink`)

| domain | events.jsonl | run.log `[ops]` | 说明 |
|---|---|---|---|
| `ws.*` | 跳过 | 跳过 | add_event 已直写 events.jsonl;EngineLogger 渲染 run.log |
| `engine.*` | 仅 `run_started/run_ended` | 跳过 | 其余引擎信号量大,留在 canonical + run.log(EngineLogger) |
| 其余(adapter/sandbox/ssh/executor) | `ws.ingest_external` 投影 | 写 `[ops]` 行 | 外围事件,run 账本跨域链路 |

`ingest_external` 只落投影**不 re-emit**:canonical 已由原始 `emit` 写入,`node_id/round`
落到 Event 顶层,`step_id` 映射 `node_id` 支持按步查询。

### 1.4 断点续跑契约不变

`Workspace.load()` 仍读 `state.json + events.jsonl` 重建。Event dataclass 新增
`node_id / round` 顶层字段(默认 None),旧账本行缺省不受影响;事件编码的 `seq`
只在 canonical 流里,events.jsonl 不携带(那是投影的重放索引,不是账本的)。

### 1.5 语义约定

- **绝不抛异常**:emit / record_error 全路径兜底,`_scalar` 把任意对象(str 截断 500、
  dict/list JSON 截断 64KB、其余 str 化)保证可 JSON 序列化——引擎信号会带异常对象等
  非标量,不能因此打断主循环。
- **失败必进事件**:`record_error(domain, subject, exc, level)` → `{domain}.{subject}_failed`,
  `level` 是策略(FATAL 阻断 / RECOVERABLE 记录继续 / CLEANUP 收尾告警)。
- **run 作用域由引擎持有**:`Engine.run` 设 `set_run_context(run_id=ws.run_id)`,
  `finally` 清空;步骤切换 / attempt / 工具轮由引擎设 `node_id/round`,外围组件无需逐处传参。

---

## 2. 事件编码清单

### 2.1 engine — 引擎生命周期(经 SignalBus 桥接)

| 事件 | 触发时机 | 关键字段 |
|---|---|---|
| `run_started` | run 启动(main.py 显式;SignalBus 镜像各一条,字段不同) | run_id / task |
| `run_ended` | run 结束 | run_id / state |
| `state_transition` | 状态机迁移 | from_state / to_state / reason |
| `step_started` / `step_ended` | 步骤执行/验收结束 | step_id / attempt / verdict / attempts |
| `llm_call_start` / `llm_call_end` / `llm_response` | 一次外部 agent LLM 调用 | role / ctx_size / latency_ms / tokens |
| `replan_start` / `replan` / `replan_end` | 重规划边界 | source / changes / reason |
| `ctx_assembled` / `ctx_overflow` / `ctx_compressed` / `ctx_ingest` | 上下文组装/溢出/压缩/反向装填 | role / total_tokens / overflow |
| `plan_review_pass` | 评审通过清 REVISE | — |
| `deadlock_detected` / `oscillation_risk` | 调度卡壳 | report / stalls |
| `failed` | run 失败 | reason |
| `phase_timeout` / `run_timeout` | 阶段/全局超时 | phase / elapsed_ms |
| `token_budget_exceeded` | token 预算耗尽 | — |
| `env_check` | 工具/沙箱/分类就绪度探测 | scope / report |
| `run_failed` | record_error(FATAL) | reason |

### 2.2 ws — agent 决策链(EventKind)

| 事件 | detail 类型 | 触发时机 |
|---|---|---|
| `replan` | ReplanDetail | planner 产出/重规划落账 |
| `plan_review` | OpinionDetail | ep 计划评审 |
| `step_eval` | OpinionDetail | ee 步骤验收 |
| `reflect` | OpinionDetail | et 任务反思 |
| `scheduling` | dict | 调度死锁(引擎结构检测) |
| `step_record` | StepRecordDetail | 每步验收落账(带 round=attempts) |
| `use_tool` / `tool_result` | ToolCallDetail / ToolResultDetail | 工具调用与返回(带 round=工具轮) |
| `goal_eval` | GoalEvalDetail | step_eval 评完 step 后比对 goal list |
| `audit_plan_review` / `audit_step_eval` / `audit_reflect` | audit detail | audit 评估器富详情(经 `_workspace_event_sink` → add_event) |

### 2.3 adapter — 平台适配器(ctf_platform)

| 事件 | 触发时机 | 关键字段 |
|---|---|---|
| `ingest` | 题面物化落库 | challenge_id / name / category |
| `download` | 附件下载成功 | challenge_id / file_id / size |
| `token_refreshed` | token 轮换 | status_code |
| `submit` | 提交 flag(多个 verdict:success/INCORRECT_FLAG/request_error/...) | challenge_id / verdict |
| `target_started` / `target_stopped` | 靶机开关 | challenge_id / host |
| `sync` | 练习场同步 | practice_ground_id / total |
| `challenge_dir_cleaned` / `flag_persisted` / `procedure_recorded` / `cache_purge` | 目录清理/flag 落库/procedure 记录/缓存清理 | — |
| `*_failed` | parse / download / target_stop / target_auto_start / **materialize(FATAL)** | error / level |

### 2.4 sandbox — 沙箱

| 事件 | 触发时机 | 关键字段 |
|---|---|---|
| `probe` / `install` / `conflicts` | 工具探测/安装/冲突 | tool_id / result / count |
| `container_created` / `container_reused` / `container_removed` | 容器生命周期 | session_key / container |
| `ensure` / `cleanup` | 门面 ensure/cleanup | session_key / container |
| `sync` | 附件同步成功 | cwd / session_key |
| `exec` / `run_python` | 沙箱内执行 | cmd / cwd / session_key / tool_id |
| `*_failed` | sync(RECOVERABLE) / container_removed(CLEANUP) / init(RECOVERABLE) | error / level |

### 2.5 ssh / executor

| domain | 事件 | 触发时机 |
|---|---|---|
| `ssh` | `exec_failed` | SSH 传输层异常(区别于命令失败),RECOVERABLE |
| `executor` | `target_resolve_failed` / `experience_match_failed` | 靶机解析 / 经验匹配失败 |

### 2.6 node_id / round 覆盖度

- **引擎信号**:步骤执行期自动带(引擎在 dispatch 设 `node_id+round`);LLM 调用/评估
  阶段 node_id 保持,round=最近 attempt。
- **决策链**:`step_record`/`use_tool`/`tool_result` 显式带 round(attempt / 工具轮);
  node_id=step_id。
- **外围组件**:`chat_with_tools` 每轮设 `round=rnd`,该轮内 sandbox.exec 等自动带
  工具轮次;步骤外(如 ingest/submit)无 node/round,只有 run_id。

---

## 3. 消费方

- **重放**:按 `run_id` 过滤 ops.log、按 `seq` 排序即得该 run 的完整有序事件流,
  是未来事件源重放(继承前 N 轮 ctx、评估 agent 溯源诊断)的输入。
- **断点续跑**:仍只依赖 `runs/<run_id>/state.json + events.jsonl`(契约不变)。
- **人类排查**:run.log 不变(EngineLogger);外围组件动作另有 `[ops]` 行可交叉定位。
