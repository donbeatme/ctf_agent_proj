# audit 可观测性模块

与主架构（agent/）解耦的评估 + 集成层：扩展 PlanStep 字段、提供三个评估器
（计划评审 / 步骤验收 / 任务反思）、flag 验证、经验回流（RAGFlow / 本地 JSONL）。

## 目录结构

```text
audit/
├── __init__.py            # 公开导出（AgentAuditService / AgentRuntimeBindings 等）
├── service.py             # AgentAuditService:编排入口 + bind_evaluator
├── settings.py            # Settings(CTF_AUDIT_MODE offline/online;model_config 兜底)
├── schemas.py             # AuditPlan/PlanStep 等模型 + SchemaError
├── flag_verifier.py       # FlagVerifier:正确 flag 判定
├── metrics.py             # 评估权重(DEFAULT_WEIGHTS)
├── agent_adapter.py       # AgentRuntimeBindings + audit_plan_fields 往返转换
├── evaluators/
│   ├── plan.py            # PlanEvaluator(计划评审)
│   ├── step.py            # StepAcceptanceEvaluator(步骤验收)
│   └── task.py            # TaskReflectionEvaluator(任务反思 + FlagVerifier + Reflexion)
└── integrations/
    ├── deepseek.py        # DeepSeekChat:LLM 网关
    ├── llm_chat.py        # LlmChatResult:统一聊天结果
    ├── ragflow.py         # RAGFlowExperienceStore:经验存取
    ├── experience.py      # build_experience:AuditRecord → 经验字典
    └── langsmith_logger.py # LangSmith 观测(FLAG_PATTERN 过滤敏感 flag)
```

## AgentAuditService（`service.py`）

```python
service = AgentAuditService(
    settings,          # audit.Settings(CTF_AUDIT_MODE: offline/online)
    flag_rules,        # {flag 规则…} → FlagVerifier
    run_id="run-001",
    agent_id="ctf-agent",
    event_sink=None,   # 可选事件接收
)
```

- 构造时建 `FlagVerifier`、经验库（`ragflow_enabled` → `RAGFlowExperienceStore`，
  否则 `LocalExperienceStore(data_dir/experiences.jsonl)`）、`AuditExperienceDocStore`
  （历史经验检索,注入 Planner 的 docs）。
- `bind_evaluator(bindings)` → 返回审计版 `Evaluator`（ep/ee/et 三评估器）。
- `service.close()` 释放连接。

## Settings（`settings.py`）

环境变量统一走 `model_config.get`（env 优先,model_config.json 兜底）:

| 变量 | 默认 | 说明 |
|---|---|---|
| `CTF_AUDIT_MODE` | `offline` | `offline` / `online`（非法值抛 `ValueError`） |
| `CTF_AUDIT_DATA_DIR` | `./data` | 本地经验/数据根目录 |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | LLM 模型 |
| `LANGSMITH_ENABLED` / `RAGFLOW_ENABLED` | `false` | 外部观测/经验开关 |
| `RAGFLOW_API_KEY` / `RAGFLOW_BASE_URL` / `RAGFLOW_DATASET_NAME` / `RAGFLOW_TIMEOUT_SECONDS` | — | RAGFlow 连接 |
| `EXPERIENCE_SEARCH_LIMIT` | `5` | 经验检索条数上限 |

## Plan 字段

Audit `PlanStep` 保留原有字段，新增两个新增字段：

```json
{
  "plan_step_id": "s1",
  "goal": "Inspect target",
  "action": "Request the landing page",
  "instruction": "Request the page and record all exposed assets",
  "criterion": "HTTP status and asset names are recorded",
  "tool": "ctf-web.field-notes",
  "depends_on": []
}
```

`blueprint_to_plan()` 将 `Step.id/instruction/criterion/skill_id/depends_on`
映射到 Audit plan。`plan_to_blueprint()` 将 Audit plan 转回 Blueprint，并把
`goal/action/tool` 保存在 `Blueprint.meta.audit_plan_fields`，因此往返转换不会丢字段。

## 接线

```python
from agent.engine import Engine
from agent.planner import Planner
from audit import AgentAuditService, AgentRuntimeBindings

service = AgentAuditService(
    settings,
    flag_rules,
    run_id="run-001",
    agent_id="ctf-agent",
)

holder = {}
bindings = AgentRuntimeBindings(
    blueprint=lambda: holder["engine"].bp,
    task=lambda: task,
    current_step=lambda: holder["engine"].current,
    observation=lambda: holder["engine"]._obs,
    submitted_flag=lambda: submitted_flag,
    completed=lambda: holder["engine"].task_completed,
)

planner = Planner(docs=service.planner_docs, workspace=workspace)
evaluator = service.bind_evaluator(bindings)
engine = Engine(planner, executor, evaluator, workspace=workspace)
holder["engine"] = engine
engine.run(task)
evaluator.close()
```

调用关系：

```text
Planner -> DocStore.search -> RAGFlow 历史经验
Engine  -> Evaluator.review -> Audit PlanEvaluator
Engine  -> Evaluator.step_eval -> Audit StepAcceptanceEvaluator
Engine  -> Evaluator.reflect -> FlagVerifier + Reflexion + 经验存储
```
