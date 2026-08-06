# CTF2 Agent

多智能体 CTF 解题引擎。规划 → 执行 → 评估 → 反思闭环，目标驱动自动化解题。

---

## 架构总览

```
【核心引擎 (单任务)】
│
├── 1. 规划阶段
│   ├── 规划 Agent (多轮检索 + 制定计划)
│   │   └── 可调用【工具层】的只读检索工具 → 按 ID 查技能详情
│   └── 评估 Agent - 计划评审 (结构校验)
│       ├── 参考【经验沉淀】中的成功/失败模式 ← 提高评审准确率
│       └── 结果：pass / revise ──→ 进入下一步
│
├── 2. 计划 DAG
│   └── 包含：可检验标准 + 步骤依赖关系
│       └── 每个步骤可绑定【技能库】中的 Skill ID（由 Planner 检索后填入）
│
├── 3. 执行子系统 (MVP 阶段为串行)
│   ├── 调度器 (获取策略步骤 + 组织上下文)
│   │   └── 向【追踪审计面板】写入调度事件 ← 便于实时监控
│   ├── 执行 Agent (思考 → 调试工具 → 观察结果)
│   │   ├── 调用【工具层】的实际执行工具（读写文件/调API等）
│   │   ├── 读取【技能库】中的经验文档以指导执行（若需要）
│   │   └── 读写【记忆-工作区】中的状态/动作/环境变量
│   └── 评估 Agent - 步骤验收
│       ├── 参考【经验沉淀】中的历史失败案例 ← 辅助判断 retry 或 escalate
│       └── 结果 pass ──→ 进入交付
│          结果 retry ──→ 返回【执行 Agent】重试
│          结果 escalate ─→ 触发【评估 Agent - 任务反思】
│
├── 4. 计划修补与闭环
│   └── 评估 Agent - 任务反思 (全局校验，生成计划补丁)
│       ├── 查询【经验沉淀】中类似场景的补丁策略 ← 借鉴历史
│       ├── 更新【计划 DAG】
│       └── 记录本次反思事件到【追踪审计面板】
│
└── 5. 交付结果
    └── 输出：最终产物 + 审计报告
        └── 审计报告内容来自【追踪审计面板】收集的事件流


【资源与支撑层 (横向支撑关系说明)】
│
├── 技能库 (SKILL 经验文档)
│   └── 支撑：规划 Agent（检索ID）、执行 Agent（查阅详细步骤）
│
├── 工具层 (统一接口，分级授权)
│   ├── 支撑：规划 Agent（只读检索 Skill 详情）
│   └── 支撑：执行 Agent（读写操作/实际执行工具）
│
├── 记忆-工作区 (状态 / 动作 / 执行环境)
│   └── 支撑：执行 Agent（记录运行时上下文，供后续步骤复用）
│
├── 经验沉淀 (成功与失败案例库)
│   ├── 支撑：评估 Agent-计划评审（避免重复踩坑）
│   ├── 支撑：评估 Agent-步骤验收（判断重试还是升级）
│   └── 支撑：评估 Agent-任务反思（提供补丁参考）
│
└── 追踪审计面板 (事件流实时日志)
    ├── 支撑：调度器（记录调度事件）
    ├── 支撑：所有 Agent（记录关键动作）
    └── 最终输出：审计报告（交付物之一）
```

---

## 角色分工

### 第一组：Workflow 与 Agent 核心组

| 角色 | 职责 |
|---|---|
| ① 总体 Workflow 架构＋技术统筹 | 整体技术路线设计、Workflow 流程编排、模块接口规范、任务状态流转、异常处理机制、两组系统集成与进度协调 |
| ② **任务理解层** | 题面、图片、附件、代码、压缩包和目标地址的多模态解析，输出任务目标、约束条件和结构化任务 |
| ③ **认知决策＋Agent 主循环（本仓库）** | 规划 Agent、计划 DAG、模型调用、状态管理、动态重规划，以及任务理解、Skill、工具执行和评估模块之间的统一调度 |

### 第二组：CTF 能力、执行与评估组

| 角色 | 职责 |
|---|---|
| ① SKILL 库＋认知决策支持 | 复现和整理 ctf-skills，将 CTF 经验转化为题型判断规则、解题 Skill、候选策略、工具建议和计划模板 |
| ② 执行层＋工具编排 | CTF 工具环境、工具插件、Docker 沙箱、跨工具编排、执行结果解析和真实 CTF 题目的运行验证 |
| ③ 审计复盘＋RAG 经验库 | 步骤评估、Flag 验证、任务反思、审计日志和评估指标，将验证过的成功路径/失败原因/执行经验构建为 RAG 知识库 |

---

## 本仓库范围（③ 认知决策＋Agent 主循环）

### 已实现

```
agent/
├── engine.py         # 主循环：状态机 + run/resume + 调度分发 + budget 管理
├── understander.py   # 任务理解层输出 API：TaskUnderstander.understand(raw) → TaskInput
├── planner.py        # 规划 Agent：LLM 调用 + PlanPatch 解析 + 只读 lookup 工具
├── blueprint.py      # 计划 DAG：Step/Blueprint/Patch + 拓扑排序 + 补丁合并
├── schema.py         # 类型系统：枚举/事件协议/Pydantic 模型/PlanPatch 契约
├── workspace.py      # 工作区：状态持久化 + 事件流 + 上下文组装注册
├── ctx.py            # 上下文组装器：组件注册/渲染/压缩/装填
├── signals.py        # 事件总线：SignalBus pub/sub
├── logging.py        # 日志层：run.log 人类可读格式 + 汇总表
├── evaluator.py      # 评估 Agent 接口桩（ep/ee/et）
├── executor.py       # 执行 Agent 接口桩
├── llm_api.py        # LLM 网关：chat/chat_with_tools + token 计算 + role_model
├── timing.py         # PhaseTimer 阶段超时
└── tools.py          # 工具协议：@tool 装饰器/openai_tool_specs/call_tool + lookup 工具

design/               # 设计文档（9 份）
tests/                # 163 个测试
```

### 外部依赖（其它角色交付）

| 接口 | 交付方 | 本仓库状态 |
|---|---|---|
| 任务理解层输出 API | ② 任务理解层 | `TaskUnderstander.understand(raw) → TaskInput` 已接线（`MockTaskUnderstander` 消费 `raw["goals"]` → `goal_list`，engine 在 run() 起始调用） |
| 技能文档（Skill） | 第二组 ① | `ws.docs` 注册表 + `get_doc` 只读查询已接线 |
| 执行 Agent | 第二组 ② | `Executor.run(step, ctx) → ExecResult` 接口桩 + `MockExecutor`（step 可带 `skill_id`，ctx 含绑定技能文档索引） |
| 评估 Agent | 第二组 ③ | `Evaluator.review/step_eval/reflect` 接口桩 + `MockEvaluator` |
| 工具执行 | 第二组 ② | `@tool` 注册 + `call_tool` 已接线，CTF2 工具已实现 |
| 经验沉淀（RAG） | 第二组 ③ | 未接入，接口桩见 [design/contracts.md §6](design/contracts.md) |

---

## 快速开始

### 配置

```bash
# model_config.json — LLM 配置
{
  "DEEPSEEK_API_KEY": "sk-...",
  "DEEPSEEK_MODEL": "deepseek-v4-flash",
  "LLM_MODEL_PLANNER": "deepseek-v4-flash",  # 可选：planner 专用模型
  "LLM_MODEL_EP": "qwen3-235b-a22b"          # 可选：计划评审专用模型
}
```

### 运行

```bash
# 冒烟测试（真实 Planner + mock 执行/评估）
python main.py run-task

# 全量测试
python -m pytest tests/ -x -q

# 场景测试
python tests/smoke_scenarios.py
```

---

## 设计文档

| 文档 | 内容 |
|---|---|
| [design/dag.md](design/dag.md) | DAG 数据结构：Step/Blueprint/Patch + 状态机 + 补丁合并 |
| [design/contracts.md](design/contracts.md) | Agent 接口契约：Planner/Executor/Evaluator |
| [design/schema.md](design/schema.md) | 类型系统：枚举、事件协议、Pydantic 模型 |
| [design/signals.md](design/signals.md) | SignalBus pub/sub + run.log 格式 |
| [design/engine.md](design/engine.md) | Engine 状态机 + run/resume + budget + 持久化 |
| [design/tools.md](design/tools.md) | 工具协议：@tool 装饰器 + lookup + 归一化 |
| [design/model_config.md](design/model_config.md) | 模型配置 + role_model + token API |
| [design/workspace.md](design/workspace.md) | Workspace 持久化布局 + Event + StepResult |
| [design/testing.md](design/testing.md) | 测试与 Mock 范围 + 职责边界声明 |
