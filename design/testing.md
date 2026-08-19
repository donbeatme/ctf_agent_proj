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
| `tests/test_planner.py` | Planner 文档合并检索（CombinedDocStore 去重/透传）+ DAG 失败重试一次 |
| `tests/test_timing.py` | PhaseTimer 阶段超时 |
| `tests/smoke_scenarios.py` | 端到端场景（revise/escalate/deadlock/reflect），真实 Planner + mock 其他 |
| `tests/test_history_ctx.py` | History 组件渲染导出（需真实 LLM） |
| `tests/test_runner.py` | CommandRunner 沙箱唯一执行：委托 SandboxManager；无沙箱 → 错误结果,绝不回退宿主 |
| `tests/test_ssh_sync.py` | SshBackend(paramiko) SFTP 增量同步题目目录 |
| `tests/test_real_executor.py` | RealExecutor LLM 工具循环 + `_cwd` 收口（越界拒绝/默认 challenge_dir/错误返回） |
| `tests/test_config_split.py` | 配置拆分：config_adaptor/config_sandbox 各读自己 JSON、env 优先、model_config 不再含 CTF_SSH_*/CTF2_* |
| `tests/test_sandbox_env_base.py` | SandboxManager 门面 + FakeBackend（平台无关） |
| `tests/test_sandbox_env_ssh.py` | SshSandboxBackend：per-challenge 持久容器生命周期（docker run/exec/rm） |
| `tests/test_sandbox_env_tools.py` | ToolManager：探测 / OS 适配安装 / 冲突与不兼容分析 |
| `tests/test_sandbox_env_integration.py` | 沙箱 ↔ runner 集成（exec/run_python 委托、依赖钩子） |
| `tests/test_real_understander.py` | RealTaskUnderstander 真实理解（metadata/distfiles/题型判定/目标生成） |
| `tests/test_real_understander_engine_integration.py` | 理解层 → Engine 集成（challenge_dir 传透） |
| `tests/test_task_understanding_merge.py` | 多源任务输入归一化 / 合并 |
| `tests/test_local_challenge_workflow.py` | 本地 challenge 物化 → 理解 → 解题工作流（沙箱） |
| `tests/test_local_verify_tiers.py` | 本地验证层级（探针/软/硬判定） |
| `tests/test_offline_workflow_reproduction.py` | 离线工作流复现（无 LLM/沙箱,mock 全链路） |
| `tests/test_procedures.py` | 规程/流程工具 |
| `tests/test_opslog.py` | opslog 统一操作日志（adapter/sandbox/engine 事件 JSONL + attach 转发） |
| `tests/test_submission_component.py` | flag 提交组件（本地比对/提交状态） |
| `tests/test_evaluator_config.py` | 评估器配置（ep/ee/et 权重等） |
| `tests/test_experience_ctx.py` | 经验上下文组件渲染 |
| `tests/test_experience_matching.py` | 历史经验匹配（检索/排序） |
| `tests/test_audit_flag_delegation.py` | audit FlagVerifier 委托（正确 flag 判定） |
| `tests/test_audit_goals_binding.py` | audit 目标绑定 |
| `tests/test_audit_persistence.py` | audit 持久化（经验/记录落盘） |
| `tests/test_audit_wiring_integration.py` | audit ↔ Engine/Planner 接线集成（AgentAuditService） |
| `tests/test_ctf_platform_base.py` | ChallengeAdapter 基类契约（FakeAdapter 平台无关） |
| `tests/test_ctf_platform_storage.py` | ChallengeStore(SQLite) + AttachmentCache(LRU+md5+淘汰) |
| `tests/test_ctf_platform_ctf2.py` | Ctf2Adapter：parse/download/submit/sync/靶机开关（FakeSession,mock 离线） |
| `tests/test_ctf_platform_cli.py` | ctf_platform CLI 命令（challenge-fetch/sync、flag-submit、cache-*、challenge-target） |
| `tests/test_ctf_platform_integration.py` | 适配器 ↔ 主架构集成（物化目录契约/metadata.yml） |
| `tests/test_real_ctf_ingestion.py` | 真实 ctf2 平台 ingestion（需网络+凭证） |
| `tests/test_real_ctf_maze_ingestion.py` | 真实 ctf2 maze 题型 ingestion（需网络+凭证） |
| `tests/test_six_categories.py` | 六类题型冒烟（scripts/run_six_categories.py） |

---

## 4. 测试运行

```bash
python -m pytest tests/ -q             # 全量（471 passed, 4 skipped）
python -m pytest tests/test_engine.py  # 仅引擎测试
```

场景测试（需 LLM key）：
```bash
python tests/smoke_scenarios.py
```
