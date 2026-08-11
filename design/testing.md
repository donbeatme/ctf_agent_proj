# 测试与 Mock 范围

---

## 1. 职责边界声明

以下组件由**外部团队交付**，当前仓库中的相关接口定义和测试数据**仅作本地开发参考**，不代表最终契约：

| 组件 | 角色 | 说明 |
|---|---|---|
| ep (Evaluator Plan) | 计划评审 | 判定计划是否可执行 |
| ee (Evaluator Step) | 步骤验收 | 判定步骤执行结果是否通过 |
| et (Evaluator Task) | 任务反思 | 终局反思，判定任务是否完成 |
| ex (Executor) | 步骤执行 | 实际执行步骤指令 |
| Task 输入 | 任务定义 | 题目描述、环境信息等 |
| Skill Doc | 技能文档 | 参考文档，注入规划上下文 |

---

## 2. Mock 组件清单（`agent/` 和 `tests/mock_data/`）

### 2.1 MockExecutor（`agent/executor.py`）

```python
class MockExecutor:
    def __init__(self, observation="", result=None, tool_calls=None, fn=None)
    def run(step, ctx) -> ExecResult
```

- `observation` — 固定返回的观察文本
- `result` — 固定返回的产物 dict
- `tool_calls` — 固定返回的工具调用轨迹 `[{tool, args, result}]`（喂 trace 通道）
- `fn` — 自定义函数 `(step, ctx, tool_exec=None) -> ExecResult`，覆盖前两者（兼容旧 2 参 `(step, ctx)`）

### 2.2 MockEvaluator（`agent/evaluator.py`）

```python
class MockEvaluator:
    def __init__(self, responses: dict[str, EvalResult] | None = None,
                 goal_responses: list[GoalEvalDetail] | None = None)
    def review(ctx) -> EvalResult       # key="evaluator_plan"
    def step_eval(ctx) -> EvalResult    # key="evaluator_step"
    def reflect(ctx) -> EvalResult      # key="evaluator_task"
    def eval_goals(ctx, goals, dag_summary) -> list[GoalEvalDetail]
```

`responses` 的值可以是 `EvalResult` 或 `callable(ctx) -> EvalResult`。
`goal_responses` 为 `eval_goals` 的预设返回列表（或 `callable(ctx, goals, dag_summary)`），按调用顺序消费。

### 2.3 MockPlannerLLM（`agent/planner.py`）

```python
class MockPlannerLLM:
    def __init__(self, response='{"add":[...],"reason":"mock"}')
```

按 `system=`/`prompt=` 键取对应响应，无匹配返回构造函数默认值。注入为 `Planner(llm_call=mock_llm)`。

### 2.4 Mock 数据（`tests/mock_data/__init__.py`）

| 常量 | 说明 |
|---|---|
| `MOCK_TASK` | 示例任务 dict（base64 编码题） |
| `MOCK_PLAN_INITIAL` | 初始规划 JSON |
| `MOCK_PLAN_REVISE` | 修订规划 JSON |
| `MOCK_EVAL_EP_FAIL` | 计划评审不通过 |
| `MOCK_EVAL_EE_ESCALATE` | 步骤执行升级 |
| `MOCK_EVAL_ET_REPLAN` | 反思要求重规划 |
| `MOCK_EXEC_OK` / `MOCK_EXEC_FAIL` | 执行结果 |
| `MOCK_SKILL_DOCS` | 示例技能文档（base64/nmap/SQL注入等，仅参考） |
| `mock_exec_by_step(results)` | 按 step.id 返回不同 ExecResult 的工厂函数 |

### 2.5 MockWorkspace（`agent/workspace.py`）

```python
class MockWorkspace:
    """_persist=False，不落盘。assembler 自动创建，无需外部注入。"""
```

---

## 3. 测试文件

| 文件 | 覆盖范围 |
|---|---|
| `tests/test_blueprint.py` | DAG 构建、拓扑排序、状态迁移、补丁合并、回滚 |
| `tests/test_workspace.py` | Workspace 创建/加载/同步、事件流、mock |
| `tests/test_engine.py` | Engine 状态机全场景：正常流程、死锁、escalate、revise、reflect、序列矩阵、异常保护、复用 |
| `tests/test_ctx.py` | 上下文组装、组件注册、压缩 |
| `tests/test_assembler.py` | CtxAssembler 信号响应 |
| `tests/test_compress.py` | 机械压缩策略 |
| `tests/test_llm_api.py` | Token 计算、模型匹配、消息计数 |
| `tests/test_tool_components.py` | 工具组件渲染 |
| `tests/test_skills.py` | 技能库加载器 / DocStore 检索（CtfSkillsDocStore） |
| `tests/test_ctf_skill_tools.py` | 工具/依赖声明包装：CtfSkillToolCatalog / TOOL_MANIFEST / apply_tool 动态申请 |
| `tests/test_checks.py` | 环境检查：SkillEnvProbe 探测 + ENV_CHECK 打点 + run.log 输出 |
| `tests/test_timing.py` | PhaseTimer 阶段超时 |
| `tests/smoke_scenarios.py` | 端到端场景（revise/escalate/deadlock/reflect），真实 Planner + mock 其他 |
| `tests/test_history_ctx.py` | History 组件渲染导出（需真实 LLM） |

---

## 4. 测试运行

```bash
python -m pytest tests/ -x -q          # 全量（213 个）
python -m pytest tests/test_engine.py  # 仅引擎测试
```

场景测试（需 LLM key）：
```bash
python tests/smoke_scenarios.py
```
