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
