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
├── executor.py       # 执行 Agent：Executor 契约 + MockExecutor + RealExecutor（LLM 工具循环 → CommandRunner 路由 → ExecResult）
├── runner.py         # 沙箱唯一执行面：CommandRunner 全部委托 SandboxManager(SSH→VM 容器),无沙箱绝不回退宿主 + 超时/截断
├── ssh.py            # 远程执行后端：SshBackend(paramiko exec + SFTP 增量同步题目目录到 VM)
├── llm_api.py        # LLM 网关：chat/chat_with_tools + token 计算 + role_model
├── timing.py         # PhaseTimer 阶段超时
└── tools.py          # 工具协议：@tool 装饰器/openai_tool_specs/call_tool + lookup 工具 + apply_tool/remove_tool 动态申请

ctf_platform/         # 平台适配器层（与主架构解耦,换平台/靶场只换子类）
├── base.py           # ChallengeAdapter(ABC)：4 能力(物化/下载管理/提交/持久化) + ingest 模板方法
├── ctf2.py           # Ctf2Adapter：ctf2 平台实现(parse/download URL模板回退/submit/sync/靶机开关)
├── storage.py        # ChallengeStore(SQLite 索引+flag) + AttachmentCache(LRU+md5)
├── config.py         # StoreSettings.from_env()（env 优先）
├── cli.py            # 7 条命令: challenge-fetch/sync, flag-submit, flags-import, cache-stats/purge, challenge-target(开/关靶机)
└── errors.py         # AdapterError/AuthError/DownloadError/ParseError/CacheIntegrityError

sandbox_env/          # 沙箱环境管理器(类适配器):SandboxBackend(ABC) + SandboxManager 门面 + ToolManager
├── base.py           # SandboxBackend 接口 + SandboxManager 门面(exec/run_python/工具委托) + session_key_for
├── ssh_backend.py    # SshSandboxBackend:per-challenge 持久容器(docker run -d sleep infinity) over SSH
├── tools.py          # ToolManager:沙箱内探测(verify_check)/安装(OS 适配,持久)/冲突与不兼容分析
├── config.py         # SandboxSettings.from_env()(CTF_SSH_* / CTF_SANDBOX_*)
├── cli.py            # 3 条命令: sandbox-probe/conflicts/deps
└── errors.py         # SandboxError/SandboxUnavailableError/SandboxExecError/ToolInstallError

opslog.py             # 统一操作日志:adapter/sandbox/engine 事件 JSONL 落盘(./data/ops.log)+ attach 转发
config_adaptor.py     # 平台适配器配置：env → config_adaptor.json → CTF2_CONFIG_JSON 外部文件兜底（配对 Ctf2Adapter→StoreSettings）
config_sandbox.py     # 沙箱配置：env → config_sandbox.json（配对 SandboxManager→SandboxBackend）

task_understanding/   # 任务理解层：本地 challenge 物化输入 → Engine 契约
├── real_understander.py # RealTaskUnderstander:raw_content → TaskInput(goal_list)
├── classify.py       # 题型判定:关键词 + 附件扩展名启发,回填 challenge_type（收编自 challenge_intake）
├── goals.py          # 目标生成策略:goal_list(obtain_flag)
├── normalize.py      # 多源任务输入归一化:字段归一成 engine 可消费的 raw dict（收编自 challenge_intake）
├── artifact_adapter.py # attachments Artifact → JSON-safe
├── image_understanding.py # ImageUnderstander / OllamaImageUnderstander
└── loaders/
    └── local.py      # 本地 challenge_dir / metadata.yml / distfiles

audit/                # 可观测性层：PlanStep 字段扩展 + 评估器 + 集成（与主架构解耦）
├── service.py        # AgentAuditService:Plan 字段扩展 + bind_evaluator + flag 验证 + 经验回流
├── settings.py       # CTF_AUDIT_MODE offline/online（model_config 兜底）
├── schemas.py        # AuditPlan/PlanStep/… 模型
├── flag_verifier.py  # FlagVerifier:正确 flag 判定
├── metrics.py        # 评估权重
├── agent_adapter.py  # AgentRuntimeBindings + audit_plan_fields 往返转换
├── evaluators/       # plan/step/task 三个评估器（计划评审/步骤验收/任务反思）
├── integrations/     # deepseek/experience/langsmith_logger/llm_chat/ragflow
└── README.md

skills/ctf-skills/    # vendored ctf-skills 技能库（Agent Skills 格式，11 类 117 文档）
scripts/              # 沙箱环境准备：provision_alpine.sh（VM 上检测+自动装 docker/构建镜像）+ Dockerfile.ctf-sandbox（Debian 沙箱）；真跑冒烟 harness：rerun/resume_hackworld.py + run_one_challenge/run_six_categories.py
design/               # 设计文档（15 份）
tests/                # 测试
```

### 外部依赖（其它角色交付）

| 接口 | 交付方 | 本仓库状态 |
|---|---|---|
| 任务理解层输出 API | ② 任务理解层 | `TaskUnderstander.understand(raw) → TaskInput` 已接线；本仓提供 `ChallengeUnderstander`（`agent/challenge_intake.py`）多源摄入 + `RealTaskUnderstander`（`task_understanding/real_understander.py`）本地 challenge 目录/显式路径解析 → `challenge_type` / `goal_list`；默认 Mock 仍可用 |
| 技能文档（Skill） | —（③ 自持,原第二组 ① 交付） | 已落地：`agent/skills.py` 加载器 + vendored `skills/ctf-skills`（11 类 118 文档）。检索经 `DocStore.search(task)→[(doc_id,text)]` + `load_doc(doc_id)`（契约见 design/contracts.md §1）；命中分类只注册 SKILL.md，子文档经 `get_doc` 按需取 |
| 执行 Agent | 第二组 ② | `Executor.run(step, ctx, tool_exec) → ExecResult` 已实现：`MockExecutor` + `RealExecutor`（LLM 工具循环 + `CommandRunner` 沙箱唯一执行）。命令默认经 SSH 到远程 VM 的 Docker（Debian 沙箱镜像 `ctf-sandbox:latest`），容器生命周期与工具依赖由 `SandboxManager`（`sandbox_env/`）接管——每 challenge 持久容器 + 缺失工具自动装进容器；无沙箱绝不回退宿主；`--executor real` 接线见 main.py |
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
  "EVALUATOR": "smoke",
  "engine": { }
}
```

> 评估器用 **config 开关**（env `EVALUATOR` 优先，其次 model_config.json `EVALUATOR`），`smoke`=SmokeEvaluator（mock，链路冒烟）、`audit`=AgentAuditEvaluator（真实评估，见 `audit/`）；CLI `--evaluator` 仅作旧调用兜底。
>
> `smoke` 路径下按 **分角色开关** `EVALUATOR_PLAN` / `EVALUATOR_STEP` / `EVALUATOR_TASK`（env 优先，默认 `mock`）分发 ep/ee/et：`real`=轻量 LLM 评审（单轮 `llm_api.chat` 评 ctx，输出 JSON verdict，见 `agent/evaluator.py`）、`mock`=SmokeEvaluator（ep 按 blueprint 判空、ee 恒 PASS、et 恒 DONE）。`EVALUATOR=audit` 时忽略分角色开关。
>
> `EVALUATOR=audit` 走 `audit/` 真实评估：`review`→PlanEvaluator（结构评审）、`step_eval`→StepAcceptanceEvaluator、`reflect`→FlagVerifier+metrics+TaskReflectionEvaluator+经验入库。**正确性权威是平台/_local_verify 的提交判定**（`submission_result` binding 读 `ws.meta["submission"]`），静态 FlagVerifier 只在提交无判定时兜底；动态 flag（Hack World）无规则 → `flag.valid=None` + 已提交 → pass，避免 REPLAN 死循环。`CTF_AUDIT_MODE`（env/`model_config.json`，默认 `offline`）控制 LLM 评审是否启用：`offline`=纯确定性规则，`online`=走 `llm_api`（需配 key）。每次 reflect 的评估派生字段原子写 `runs/<run_id>/audit.json`（不含原始轨迹，轨迹真源是 `events.jsonl`）。

**敏感配置按适配器/沙箱拆分**（与主 config(model_config) 分开，env 优先，各配对其 JSON 兜底；gitignore，不入库）：
- `config_adaptor.py` + `config_adaptor.json`：平台适配器凭证（`CTF2_SESSION_TOKEN`/`CTF2_API_KEY`/`CTF2_COOKIE`）与 URL（`CTF2_BASE_URL`/`CTF2_SESSION_BASE`/`CTF2_ORIGIN`）；`CTF2_CONFIG_JSON` 指向的外部文件作兼容兜底。
- `config_sandbox.py` + `config_sandbox.json`：沙箱凭据（`CTF_SSH_HOST`/`CTF_SSH_USER`/`CTF_SSH_PASSWORD`）与沙箱项（backend/镜像/容器模型）。

ctf_platform 平台接入（见 [design/ctf_platform.md](design/ctf_platform.md)）：

```bash
export CTF2_CONFIG_JSON="D:/pythonProject/ctf2/config.json"   # 兼容兜底(可选);新布局写 config_adaptor.json
export CTF2_PRACTICE_GROUND_ID="..."   # 靶场 id（详情/下载/提交/拉取/靶机需要）
export CTF2_AUTO_START_TARGET="true"   # 物化含容器题自动开靶机(host:port 写 metadata.yml target)
export CTF_STORE_DIR="./data"          # 本地库+缓存根目录（默认 ./data）
export CTF_ATTACHMENT_CACHE_BYTES="2147483648"   # 附件缓存上限,超限 LRU 淘汰
```

> 也可直接 `export CTF2_SESSION_TOKEN="..."`（网页登录态 JWT，等价旧 `CTF2_TOKEN`）或写 `config_adaptor.json`。下载走真实 API：详情 → `files[].download_url` CDN 直下（免鉴权），md5 校验落缓存。friendly_id 拉取需先 `challenge-sync` 建索引（详情端点只接受 UUID）。

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
| [design/config.md](design/config.md) | 配置架构：model_config / config_adaptor / config_sandbox 三模块拆分 + 配对 + 凭证获取截图 |
| [design/workspace.md](design/workspace.md) | Workspace 持久化布局 + Event + StepResult |
| [design/ctx.md](design/ctx.md) | 上下文组装：CtxComponent 基类 + CtxAssembler + 10 组件 + 压缩 |
| [design/testing.md](design/testing.md) | 测试与 Mock 范围 + 职责边界声明 |
| [design/ctf_platform.md](design/ctf_platform.md) | 平台适配器：ChallengeAdapter 4 能力 + SQLite schema + LRU + CLI + 解耦边界 |
| [design/sandbox_env.md](design/sandbox_env.md) | 沙箱环境管理器：SandboxBackend/SandboxManager + 容器模型(per_challenge) + 工具依赖/冲突规则 + CLI/env |
| [design/task_understanding.md](design/task_understanding.md) | 任务理解层：本地 challenge → metadata/attachments/goals/target 结构化输入 |
| [design/verification.md](design/verification.md) | Flag 验证分层：静态/动态 flag 本地判定（T0/T1/T2 + procedure 重跑） |
