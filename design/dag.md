# DAG 数据结构

计划以有向无环图表示：节点为步骤(Step)，边为 `depends_on`。实现：`agent/blueprint.py`。

---

## 1. StepStatus — 状态

```python
class StepStatus(StrEnum):
    PENDING   = "PENDING"
    RUNNING   = "RUNNING"
    PASSED    = "PASSED"
    RETRY     = "RETRY"
    REVISE    = "REVISE"
    ESCALATED = "ESCALATED"
    SKIPPED   = "SKIPPED"

DONE_STATUSES = frozenset({PASSED, SKIPPED})  # 终态集合
```

READY 是派生状态，不存储：`PENDING 且所有 depends_on 均为 PASSED`。

### 合法迁移表（`_ALLOWED_TRANSITIONS`）

```
PENDING   → RUNNING | SKIPPED | REVISE
RUNNING   → PASSED | RETRY | ESCALATED | SKIPPED | REVISE
RETRY     → RUNNING | SKIPPED | REVISE
ESCALATED → PENDING | SKIPPED | REVISE
REVISE    → PENDING | SKIPPED
PASSED    → (无)
SKIPPED   → (无)
```

- PENDING → RUNNING 额外要求依赖全部 PASSED（即 READY 校验）
- 终态(PASSED/SKIPPED)不可覆盖，`force=True` 也不绕过
- `force=True` 跳过迁移表与 READY 校验，供引擎特殊场景

---

## 2. Step — 节点

```python
@dataclass
class Step:
    id: str                           # 唯一标识
    instruction: str                  # 做什么 → Executor
    criterion: str                    # 可检验标准 → Evaluator.step_eval
    depends_on: list[str] = []        # 上游依赖，[]=入口节点
    skill_id: str | None = None       # 绑定技能库文档 id(Planner 检索后填入)
    status: StepStatus = PENDING      # 状态
    attempts: int = 0
    max_attempts: int = 3
    result: dict | None = None        # 执行产物(Executor 写入)
```

字段归属：
- `instruction`/`criterion`/`depends_on`/`skill_id` — 规划域，Planner 产出，可经 Patch 修改
- `status`/`attempts`/`max_attempts`/`result` — 引擎域，Engine/Evaluator 驱动

反序列化时 `__post_init__` 自动将 status 字符串转为 StepStatus。

---

## 3. Blueprint — 图

```python
class Blueprint:
    meta: dict              # {reason, _response, ...}
    steps: dict[str, Step]  # 保持插入序
```

### 构建
- `add_step(step)` — id 重复抛 `DAGError`
- `from_dict(d)` / `to_dict()` — 序列化

### 校验
`validate() -> list[str]`：instruction/criterion 非空、depends_on 引用存在、无环。空列表 = 合法。

### 调度
- `topological_order()` — Kahn 算法，按插入序保证确定性；有环抛 DAGError
- `ready_steps()` — PENDING 且依赖全 PASSED，按拓扑序返回
- `next_step()` — 取第一个 ready；无则 None
- `is_done()` — 全部节点 ∈ DONE_STATUSES

### 公开状态变更
`set_status(step_id, status, *, force=False)` — 校验 id 存在、迁移合法、终态保护。外部 evaluator/engine 统一走此 API。

### 内容/结构变更

| 方法 | 范围 | 自身回 PENDING | 后代回 PENDING |
|---|---|---|---|
| `update_instruction(id, text)` | 内容 | 是(非终态) | 否 |
| `update_criterion(id, text)` | 内容 | 是(非终态) | 否 |
| `update_skill_id(id, skill_id)` | 内容 | 是(非终态) | 否 |
| `set_depends_on(id, deps)` | 结构 | 是(非终态) | 是(传递闭包) |
| `remove_step(id)` | 结构 | — | 是 + 清引用 |

`_descendants(step_id)` 沿 depends_on 做传递闭包，返回所有直接或间接依赖者。

---

## 4. Patch — 补丁

```python
@dataclass
class Patch:
    add: list[Step] | None              # 新增节点
    update: list[UpdateSpec] | None     # 修改 [{id, instruction?, criterion?, depends_on?}]
    remove: list[str] | None            # 删除的 step id
    reason: str = ""                    # 审计用
```

### apply_patch — 原子合并

1. 备份 `to_dict()`
2. add → update → remove 顺序应用（update 按字段分发到语义方法）
3. `validate()` 校验
4. 失败 → 回滚到备份，抛 `DAGError`

终态步骤的 update/remove 不改变其 status（也不会被级联重置）。

---

## 5. Planner 输出契约（`agent/schema.py`）

Planner 不直接调 Blueprint 方法，输出 JSON 经 `parse_plan()` 解析为 `PlanPatch` pydantic 模型。

```python
class StepSpec(BaseModel):           # 规划产出一个步骤
    id: str                          #   正则 ID_PATTERN: ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}$
    instruction: str                 #   非空
    criterion: str                   #   非空
    depends_on: list[str] = []       #   不含自身 id
    skill_id: str | None = None      #   可选绑定技能库文档 id

class UpdateSpec(BaseModel):         # 修改一个已有步骤
    id: str                          #   目标 step
    instruction: str | None = None   #   至少改一个字段
    criterion: str | None = None
    depends_on: list[str] | None = None   # [] = 清空依赖
    skill_id: str | None = None      #   改绑定(置空串解除)

class PlanPatch(BaseModel):          # 规划输出
    add: list[StepSpec] = []
    update: list[UpdateSpec] = []
    remove: list[str] = []
    reason: str = ""                 # ≤500 字符
```

校验：add/update 内 id 不重复、add 不与 update/remove 冲突、update 至少一个字段、depends_on 不含自身。`extra="ignore"` 丢弃未知字段。

### parse_plan — 容错解析

```python
def parse_plan(text: str) -> PlanPatch
```

流程：长度封顶 64KB → 去 markdown 围栏 → 取第一个 `{...}` → `json.loads`（纯 stdlib，无 eval）→ `PlanPatch.model_validate`。失败抛 `PlanError`，其 `str(e)` 可直接喂回 LLM。

### 防注入设计
- `StepSpec` 不暴露 `status/attempts/max_attempts/result`，LLM 无法注入引擎域字段
- id 白名单正则，杜绝路径穿越
- `extra="ignore"` 丢弃未知字段
- 输出超长直接拒

---

## 6. 设计决策

| 项 | 默认 |
|---|---|
| READY | 派生，不存储 |
| 执行序 | `next_step` 按拓扑序取第一个 ready（MVP 串行） |
| retry 上限 | `max_attempts=3`，超限转 ESCALATED |
| 补丁原子性 | 备份 → 应用 → 校验 → 回滚或提交 |
| 失效粒度 | 内容变更只重置自身；结构变更级联非终态后代 |
| 终态保护 | PASSED/SKIPPED 不可被任何操作覆盖 |
| 序列化 | `to_dict/from_dict`，内嵌于 `state.json` |
