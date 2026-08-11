# Workspace 持久化布局

状态管理与持久化。实现：`agent/workspace.py`。

---

## 1. 持久化布局

```
runs/<run_id>/
  state.json       # 可恢复的完整状态
  events.jsonl     # 审计事件流（即时追加）
  run.log           # 人类可读日志（EngineLogger 写入）
```

无 `memories.json` — 有界消息历史已移除，由 CtxAssembler 按需从事件流重建。

---

## 2. state.json 结构

```json
{
  "meta": {
    "run_id": "run-20260805-131323",
    "task": {"title": "...", "description": "..."},
    "goal_list": [{"id": "g1"}],
    "created_at": "2026-08-05T13:13:23",
    "run_status": "DONE",
    "current_step": null,
    "fail_reason": null
  },
  "blueprint": {
    "meta": {"reason": "..."},
    "steps": {
      "s1": {"id": "s1", "instruction": "...", "criterion": "...",
             "depends_on": [], "status": "PASSED", "attempts": 1,
             "max_attempts": 3, "result": null}
    }
  },
  "steps": {
    "s1": {"step_id": "s1", "verdict": "pass", "observation": "...",
           "result": null, "attempts": 1, "ts": "..."}
  },
  "env_state": {},
  "docs": {},
  "tools": [],
  "summaries": {
    "planner:dag": {"text": "...", "passes": 3},
    "evaluator_step:s2": {"sig": "abc123"}
  }
}
```

### 字段说明

| 字段 | 说明 |
|---|---|
| `meta` | run_id、task（原始内容，即 TaskInput.raw_content）、goal_list（任务理解层固定目标，断点续跑重建 TaskInput 用）、created_at、run_status、current_step、fail_reason |
| `blueprint` | Blueprint.to_dict() 输出 |
| `steps` | dict[step_id → StepResult] |
| `env_state` | 执行环境状态（target_url / container_id 等，Executor 写入） |
| `docs` | 技能文档注册表 `{doc_id: content}` |
| `tools` | 活动工具集（**动态**：默认空，`apply_tool`/`remove_tool` 增删） |
| `summaries` | CtxAssembler 压缩缓存 `{key: {text/passes} | {sig}}` |

### sync — 原子写入

`Workspace.sync()` 将当前状态序列化为 `state.json`：
1. 写入临时文件 `<state.json>.tmp`
2. 原子 rename 到 `state.json`

---

## 3. events.jsonl — 即时追加

```python
def add_event(agent, kind, **detail) -> Event
```

- `kind` 为 `EventKind` 枚举值
- 已注册 kind → detail 类型校验（`EVENT_SCHEMA`）
- 未注册 kind → 退化 dict 通道 + 日志警告
- 即时追加一行 JSON 到 `events.jsonl`，不等待 `sync()`

---

## 4. Workspace API

### 生命周期

```python
ws = Workspace.create(run_id, task, root=None)   # 新建 + 写初始 checkpoint
ws = Workspace.load(run_id, root=None)            # 从 state.json + events.jsonl 恢复
ws.sync()                                         # 原子落盘
```

### 状态读写

```python
ws.set_blueprint(bp)                              # 设置 blueprint(规划产出后)
ws.record_step(step_id, verdict, observation, result, attempts)
ws.record_tool_call(step_id, tool, args)
ws.record_tool_result(step_id, tool, output, args)
ws.record_opinion(agent, source, kind, verdict, opinion, step_id)
ws.set_env(key, value) / ws.get_env(key)
ws.set_doc(doc_id, content) / ws.get_doc(doc_id)
ws.set_tools(tools)               # 静态批量注入(兼容旧用法)
ws.add_tools(specs)               # 按需注入(apply_tool 经此增长活动集):归一后并入,不覆盖已有
ws.remove_tools(tool_ids)         # 从活动集移除(remove_tool 经此收缩):幂等,不存在的 id 忽略
```

`ws.tool_catalog`（运行时静态工具目录加载器，apply_tool 校验用）**不持久化**——`resume()` 恢复后为 None，环境检查全跳过（见 engine.md §9 / contracts.md §1.7）。

### 查询

```python
ws.get_record(uuid) -> Event | None               # 按 uuid 取事件全文
ws.query(agent, step_id, kind, verdict, time_range) -> list[Event]  # 多条件查询
```

### 上下文组装

```python
ws.assembler  # CtxAssembler 实例(自动创建,已注册所有组件)
```

---

## 5. StepResult — 步骤产物

```python
@dataclass
class StepResult:
    step_id: str
    verdict: str           # pass | retry | escalate(引擎 record_step 写入的 Verdict 值)
    observation: str       # 执行观察(Executor 输出)
    result: dict | None    # 结构化的执行产物
    attempts: int = 0
    ts: str = ""           # ISO 时间戳
```

---

## 6. Event — 事件

```python
@dataclass
class Event:
    uuid: str              # uuid4
    agent: str | None      # Role 值
    kind: str              # EventKind 值
    step_id: str | None
    verdict: str | None    # Verdict 值
    detail: dict           # 类型化的 detail（见 schema.md）
    ts: str                # ISO 时间戳
```

---

## 7. MockWorkspace

```python
class MockWorkspace(Workspace):
    """_persist=False，所有操作仅内存，不落盘。"""
```

- 自动创建 CtxAssembler（含 Mock 专用组件）
- 构造时不需要 root 参数
- 事件流仅存内存列表

---

## 8. Assembler 组件注册

Workspace 在 `_init_assembler()` 中按角色注册 CtxComponent：

| 角色 | 组件（按拼接序） |
|---|---|
| planner | SystemPrompt / Task / AgentComm / Dag / History / Docs / ToolDirectory / Tool / Trace |
| executor | SystemPrompt / Task / AgentComm / Dag / Docs / ToolDirectory / Tool / Trace（Trace agent=EXECUTOR） |
| evaluator_plan | SystemPrompt / Task / Dag / History |
| evaluator_step | SystemPrompt / Task / AgentComm / Dag / History |
| evaluator_task | SystemPrompt / Task / AgentComm / Dag / History |

每个组件有独立的 level（渲染档位）、priority（压缩优先级）、compress_methods（压缩策略）。详见 `design/ctx.md`。
