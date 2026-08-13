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
├── planner.py        # 规划 Agent：LLM 调用 + PlanPatch 解析 + 只读 lookup 工具 + DocStore 契约
├── skills.py         # ctf-skills 技能库加载器：SkillLibrary + CtfSkillsDocStore（关键词路由检索）
├── ctf_skill_tools.py # ctf-skills 工具/依赖声明：TOOL_MANIFEST(~70 条,手抄自 install_ctf_tools.sh)+ CtfSkillToolCatalog(frontmatter allowed-tools/compatibility + get_tool 查询)；纯声明,不接执行
├── checks.py          # 环境检查钩子：SkillEnvProbe 只读探测工具(which/find_spec)/分类/沙箱就绪度 + SANDBOX_CATEGORIES；结果经 Signal.ENV_CHECK 写 run.log
├── blueprint.py      # 计划 DAG：Step/Blueprint/Patch + 拓扑排序 + 补丁合并
├── schema.py         # 类型系统：枚举/事件协议/Pydantic 模型/PlanPatch 契约
├── workspace.py      # 工作区：状态持久化 + 事件流 + 上下文组装注册 + 活动工具集增删
├── ctx.py            # 上下文组装器：组件注册/渲染/压缩/装填（含 ToolDirectoryComponent 全量工具目录）
├── signals.py        # 事件总线：SignalBus pub/sub
├── logging.py        # 日志层：run.log 人类可读格式 + 汇总表
├── evaluator.py      # 评估 Agent 接口桩（ep/ee/et）
├── executor.py       # 执行 Agent 接口桩（run(step, ctx, tool_exec=None)）
├── llm_api.py        # LLM 网关：chat/chat_with_tools + token 计算 + role_model
├── timing.py         # PhaseTimer 阶段超时
└── tools.py          # 工具协议：@tool 装饰器/openai_tool_specs/call_tool + lookup 工具 + apply_tool/remove_tool 动态申请

skills/ctf-skills/    # vendored ctf-skills 技能库（Agent Skills 格式，11 类 ~114 文档）
design/               # 设计文档（10 份）
tests/                # 测试
```

### 外部依赖（其它角色交付）

| 接口 | 交付方 | 本仓库状态 |
|---|---|---|
| 任务理解层输出 API | ② 任务理解层 | `TaskUnderstander.understand(raw) → TaskInput` 已接线；本仓提供 `ChallengeUnderstander`（`agent/challenge_intake.py`）：多源摄入 + `CATEGORY_KEYWORDS` 题型判定 → `challenge_type` / `goal_list`；默认 Mock 仍可用 |
| 技能文档（Skill） | —（③ 自持,原第二组 ① 交付） | 已落地：`agent/skills.py` 加载器 + vendored `skills/ctf-skills`（11 类 ~114 文档）。检索经 `DocStore.search(task)→[(doc_id,text)]` + `load_doc(doc_id)`（契约见 design/contracts.md §1）；命中分类只注册 SKILL.md，子文档经 `get_doc` 按需取 |
| 执行 Agent | 第二组 ② | `Executor.run(step, ctx) → ExecResult` 接口桩 + `MockExecutor`（step 可带 `skill_id`，ctx 含绑定技能文档索引） |
| 评估 Agent | 第二组 ③ | `Evaluator.review/step_eval/reflect` 接口桩 + `MockEvaluator` |
| 工具执行 | 第二组 ② | `@tool` 注册 + `call_tool` 已接线，CTF2 工具已实现 |
| CTF 工具清单(动态申请) | —(③ 自持,原第二组① 交付) | 已落地：`agent/ctf_skill_tools.py` 声明式目录；经 `Engine(tool_catalog=...)` → `ws.tool_catalog` 供 `ToolDirectoryComponent` 渲染**全量菜单**（planner 只读 + executor 申请）+ `apply_tool`/`remove_tool` 动态增删活动集 `ws.tools`（默认空）。纯声明不接执行（executor 调用不在范围）。运行时可经 `agent/checks.py` **只读探测**缺工具/缺沙箱/分类就绪度并写 run.log（apply 时逐工具 + run 起始快照 + 每步按 skill 分类） |
| 经验沉淀（RAG） | 第二组 ③ | 未接入，接口桩见 [design/contracts.md §6](design/contracts.md) |

> `skills/ctf-skills` vendored 自 [ljagiello/ctf-skills](https://github.com/ljagiello/ctf-skills)（MIT License,© 2026 Lukasz Jagiello,LICENSE 见 `skills/ctf-skills/LICENSE`）。其依赖工具（pwntools/angr/ghidra 等）为独立开源项目，遵循各自许可证。

### 依赖与更新

**Python 运行依赖**（3 个，见 `requirements.txt`）：

```bash
pip install -r requirements.txt
```

| 包 | 版本下限 | 用途 | 位置 |
|---|---|---|---|
| `openai` | >=1 | LLM 网关（新版 client API，兼容任意 OpenAI 兼容 base_url） | `agent/llm_api.py` |
| `requests` | >=2 | HTTP 调用 | `agent/llm_api.py` |
| `pydantic` | >=2 | schema 校验（field_validator/model_validator） | `agent/schema.py` |

测试还需 dev 依赖 `pytest`（`pip install pytest`）。LLM/引擎配置见 [design/model_config.md](design/model_config.md)。

**ctf-skills 技能库更新**——上游变更需同步三处接入点，再跑漂移守卫：

| 接入点 | 路径 | 说明 |
|---|---|---|
| 技能库目录 | `agent/skills.py` `SKILLS_DIR` | vendored 库根目录 `skills/ctf-skills` |
| 安装脚本 | `skills/ctf-skills/scripts/install_ctf_tools.sh` | 整库依赖安装/更新入口（执行层② 经 `installer_path` 触发；本仓库只读不执行） |
| 工具清单 | `agent/ctf_skill_tools.py` `TOOL_MANIFEST` | **手抄**自安装脚本，脚本变更需手动同步 |

更新流程：
1. 从上游 [ljagiello/ctf-skills](https://github.com/ljagiello/ctf-skills) 拉取新版本，覆盖 `skills/ctf-skills/`（变更时核对 `LICENSE`）。
2. 若 `install_ctf_tools.sh` 变了，同步重抄 `TOOL_MANIFEST`（跨安装方式去重，主方式优先级 `pip > apt > brew > gem > go > manual`，次要方式进 `alt_methods`）。
3. 跑漂移守卫，清单与脚本脱节会失败：
   ```bash
   python -m pytest tests/test_ctf_skill_tools.py -x -q
   ```

本仓库对 ctf-skills 是**只读声明 + 探测**：目录工具经 `apply_tool` 动态申请进活动集，经 `agent/checks.py` 探测缺工具/缺沙箱/分类就绪度并写 run.log（见 [design/contracts.md §1.6/§1.7](design/contracts.md)）；真正装依赖是执行层/沙箱（第二组②）的职责，经 `installer_path` 触发 `install_ctf_tools.sh` 完成。

---

## 快速开始

### 配置

**API key 用环境变量设置**（密钥不进配置文件/仓库）：

```bash
export DEEPSEEK_API_KEY="sk-..."        # Linux/macOS
# Windows: set DEEPSEEK_API_KEY=sk-...   （永久生效用 setx）
```

其余 LLM/引擎配置经 `model_config.json`（可选，缺省用内置默认）；优先级 **环境变量 > model_config.json > 内置默认**：

```json
{
  "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
  "DEEPSEEK_MODEL": "deepseek-v4-flash",
  "LLM_MODEL_PLANNER": "deepseek-v4-flash",
  "LLM_MODEL_EP": "qwen3-235b-a22b",
  "engine": { }
}
```

### 运行

```bash
# 冒烟测试（真实 Planner + mock 执行/评估）
python main.py run-task

# 指令台前端（模型/技能沙箱/Agent 地图/经验 RAG/任务/审计交付/历史）
python main.py serve --port 8765
# 浏览器打开 http://127.0.0.1:8765
# 操作手册与模块说明见 web/README.md
# 能力地图 GET /api/capabilities；未接线接口返回 reserved 占位

# 全量测试
python -m pytest tests/ -x -q

# 场景测试
python tests/smoke_scenarios.py
```

---

## 设计文档

| 文档 | 内容 |
|---|---|
| [web/README.md](web/README.md) | 解题指令台：启动、七模块操作、子功能说明、API 速查 |
| [design/dag.md](design/dag.md) | DAG 数据结构：Step/Blueprint/Patch + 状态机 + 补丁合并 |
| [design/contracts.md](design/contracts.md) | Agent 接口契约：Planner/Executor/Evaluator |
| [design/schema.md](design/schema.md) | 类型系统：枚举、事件协议、Pydantic 模型 |
| [design/signals.md](design/signals.md) | SignalBus pub/sub + run.log 格式 |
| [design/engine.md](design/engine.md) | Engine 状态机 + run/resume + budget + 持久化 |
| [design/tools.md](design/tools.md) | 工具协议：@tool 装饰器 + lookup + 归一化 |
| [design/model_config.md](design/model_config.md) | 模型配置 + role_model + token API |
| [design/workspace.md](design/workspace.md) | Workspace 持久化布局 + Event + StepResult |
| [design/ctx.md](design/ctx.md) | 上下文组装：CtxComponent 基类 + CtxAssembler + 8 组件 + 压缩 |
| [design/testing.md](design/testing.md) | 测试与 Mock 范围 + 职责边界声明 |
