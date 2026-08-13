# CTF Agent 解题指令台（前端说明）

面向操作者与联调同学：用浏览器完成配置 → 检视能力 → 发布题目 → 观察引擎主循环 → 导出审计交付。  
本前端对接本仓库 **认知决策主循环**（Plan → Review → DAG → Execute → Step Eval → Reflect）；**执行 / 评估当前多为 Mock 接口桩**，真实沙箱与 Flag 校验由外部组交付后替换。

| 项 | 说明 |
|---|---|
| 入口文件 | `web/index.html` · `web/app.js` · `web/styles.css` |
| 后端网关 | `web_server.py`（`python main.py serve`） |
| 默认地址 | http://127.0.0.1:8765 |
| 设计对照 | `design/contracts.md` · `design/engine.md` · `design/signals.md` |

---

## 30 秒上手

```bash
# 仓库根目录
python main.py serve --port 8765
# 浏览器打开 http://127.0.0.1:8765
```

**推荐最短路径（第一次解题）：**

1. **模型与预算** → 填 BASE URL / 模型名 / API Key →「保存配置」  
2. **任务运行** → 选「题面文本」→「① 解析题型」→「② 确认并启动」  
3. 同一页下方看 **状态机 / DAG / SignalBus / events / run.log**  
4. **审计与交付** →「用当前 run」→「生成报告」  
5. **历史记录** → 回看、续跑或删除

左侧导航可直接跳步；底部「上一步 / 下一步」按 1→7 顺序走完全流程。

---

## 界面总览（7 个模块）

```
┌─────────────────────────────────────────────────────────────┐
│  CTF2 Agent · 解题指令台                                      │
├──────────┬──────────────────────────────────────────────────┤
│ 1 模型与预算 │  LLM 网关 · 引擎预算只读 · 本机工具探测            │
│ 2 技能·工具·沙箱 │ 技能库浏览 · 工具声明清单 · 沙箱运行时探测     │
│ 3 Agent 接口 │  能力地图（wired/stub/reserved）· 角色契约速查   │
│ 4 经验 RAG │  查询/写回 ExperienceStore（前端预留）            │
│ 5 任务运行 │  多源发布题目 · 题型判定 · Engine.run 实时观测     │
│ 6 审计与交付 │  Markdown 报告 · product · Flag/HITL 预留        │
│ 7 历史记录 │  runs/ 列表 · 详情 · resume · 删除                 │
└──────────┴──────────────────────────────────────────────────┘
```

### 状态色含义（页面徽章）

| 徽章 | 含义 |
|---|---|
| **已接线** / wired | 前端 + 网关 + 引擎侧已可用 |
| **声明已接线** / wired_declare | 清单/探测已通，真实执行仍属执行层 |
| **接口桩** / stub | 契约存在，当前为 Mock/Smoke 实现 |
| **未接线** / reserved | 设计已声明，引擎未接；API 返回占位 |
| **前端预留** / frontend_reserved | 技术路线需要，仓库尚无独立契约；仅 UI + 占位 API |

预留接口统一返回形如：`{ "wired": false, "reserved": true, "message": "...", ... }`，便于联调时区分「未实现」与「请求失败」。

---

## 模块 1 · 模型与预算

**作用：** 配置 OpenAI 兼容网关，让 Planner 能真正调 LLM；预览引擎 token/步数等预算；在跑题前确认本机工具是否齐备。

### 1.1 LLM 网关

| 控件 | 作用 |
|---|---|
| BASE URL | 兼容 `/v1` 的网关地址（如 `http://host:port/v1`） |
| 模型名称 | 默认 Planner 使用的模型 ID |
| API Key | 写入 `model_config.json`（已 gitignore）；留空表示沿用已存密钥；界面只显示「已设置/未设置」 |
| 启用 Planner 工具调用 | 对应 `LLM_ENABLE_TOOLS`。网关不支持 `tool_choice=auto` 时请关闭 |
| 保存配置 | `POST /api/config`，后续 LLM 调用热读生效 |
| 重新加载 | `GET /api/config`，从文件/环境变量回填表单 |

环境变量优先于 `model_config.json`。

### 1.2 引擎预算（只读）

展示 `model_config.get_engine_config()`（如最大步数、token 上限等）。本页不直接改预算；需改 `model_config.json` 的 `engine` 段后保存/重载。

### 1.3 环境探测

按钮「探测本机工具」→ `GET /api/env-check` → `SkillEnvProbe.probe_manifest()`。  
只读统计 available / missing，**不安装依赖**；与一次 run 启动时的 `ENV_CHECK` 信号同源。

---

## 模块 2 · 技能 · 工具 · 沙箱

**作用：** 浏览 Planner 会检索的 CTF 技能文档；查看工具声明清单；探测 docker/podman 是否可用（创建沙箱仍属执行层，未接线）。

### 2.1 技能库

| 控件 | 作用 |
|---|---|
| 分类筛选 | 按 `ctf-web` / `ctf-pwn` 等过滤 |
| 搜索 | 按 `doc_id` / 描述模糊过滤 |
| 列表点击 | `GET /api/skills/:id`，右侧预览 SKILL.md 全文 |

数据源：vendored `skills/ctf-skills`，经 `agent/skills.py` 暴露。Planner 按题面关键词检索分类文档，子文档可经 `get_doc` 按需拉取。

### 2.2 工具清单

`GET /api/tools`：展示 `tool_id`、安装方式、校验方式与 installer 路径。  
此处是**声明与探测**，不直接执行工具；真实调用由 Executor（执行层②）负责。

### 2.3 沙箱探测

「探测沙箱运行时」→ `GET /api/sandbox`。  
对 `SANDBOX_CATEGORIES`（如 pwn/reverse/malware）调用 `probe_sandbox`，检测 docker/podman CLI。**创建容器沙箱未接线**，仅作环境就绪检查。

---

## 模块 3 · Agent 接口

**作用：** 一眼看清「哪些能力真能用、哪些是桩、哪些只是前端预留」，对照 `design/contracts.md`，避免把 Mock 当成完整解题链路。

### 3.1 能力状态地图

「刷新能力地图」→ `GET /api/capabilities`。每层包含：名称、契约、实现类、status、说明。典型分层：

| 能力层 | 典型状态 | 说明 |
|---|---|---|
| 任务理解 Understander | wired | `ChallengeUnderstander` 多源摄入 + 题型判定 |
| 规划 Planner | wired | 真 LLM + 技能库检索 |
| 执行 Executor | stub | 当前 `MockExecutor` |
| 计划评审 / 步骤验收 / 任务反思 | stub | `SmokeEvaluator` / `MockEvaluator` |
| 技能库 / 环境探测 / 审计报告 | wired | 前端可直接用 |
| 工具编排 | wired_declare | 声明已通，执行未接 |
| 经验 RAG | reserved | 契约已有，引擎未接 `experience_store` |
| Flag 验证 / HITL | frontend_reserved | 技术路线需要，独立契约未声明 |

### 3.2 角色契约速查

固定四格：Understander / Planner / Executor / Evaluator 当前落地实现。外部组交付真实 Executor/Evaluator 后，**引擎主循环无需改结构**，替换注入即可。

---

## 模块 4 · 经验 RAG

**作用：** 按 `contracts.md §6 ExperienceStore` 预留「查经验 / 写经验」联调入口。引擎尚未接线，按钮只会拿到 `reserved: true` 占位响应。

### 4.1 查询经验

| 字段 | 作用 |
|---|---|
| topics | 逗号分隔主题，如 `SQL注入, 文件上传` |
| role | `evaluator_plan` / `evaluator_step` / `evaluator_task`（对应 ep/ee/et） |
| 试查 | `POST /api/experience/query` |

设计意图：评审/验收/反思前检索历史可执行结论。

### 4.2 写回经验

| 字段 | 作用 |
|---|---|
| topic / outcome / summary | 一条经验事件草稿 |
| 试写 | `POST /api/experience/record` |

设计写回时机：replan、ESCALATED、FAILED 等。当前 `accepted: false`。

---

## 模块 5 · 任务运行（核心）

**作用：** 把比赛题变成引擎可跑的 `TaskInput`，启动 `Engine.run`，并实时观测状态机与事件流。

### 5.1 发布题目（多源摄入）

先选来源 Tab，再「① 解析题型」，确认后「② 确认并启动」。

| 来源 | 填什么 | 用途 |
|---|---|---|
| **题面文本** | 标题、题面、challenge_id、可选 goal ID | 最快上手；默认示例为 base64 编码题 |
| **JSON 导入** | CTFd / 平台导出 JSON | 解析 name/title、description、category、files 等 |
| **目标 URL** | 服务地址 + 可选说明 | Web/远程题，URL 参与题型启发 |
| **附件上传** | 多文件（建议单文件 &lt; 20MB）+ 可选标题说明 | 上传至 `downloads/uploads/`；扩展名启发题型（如 `.pcap`→Forensics，`.elf`→Pwn） |

**手动覆盖题型：** 可选强制 `ctf-web` / `ctf-pwn` / …；留空则走 `CATEGORY_KEYWORDS` 自动判定。

**两步按钮：**

1. **① 解析题型** → `POST /api/challenge/parse`（附件会先 `POST /api/challenge/upload`）  
   - 展示主类型、置信度、候选排序、`goal_list`、归一化预览  
2. **② 确认并启动** → `POST /api/runs`  
   - 使用解析结果中的 `challenge_type` 等字段启动；改题面后需重新解析（启动按钮会再次禁用）

### 5.2 运行状态（实时观测）

| 区域 | 作用 |
|---|---|
| 进行中 / 最近 | 切换关注的 `run_id`；「刷新」重拉列表 |
| run_id / 状态 / 当前步骤 / tokens | 来自 workspace 快照轮询 |
| 状态机 pills | PLANNING → … → DONE / FAILED |
| 停止 | `POST /api/runs/:id/stop` → `request_stop`（仅 live 显示） |
| 计划 DAG | 步骤 ID、指令、criterion、depends_on、skill、状态 |
| SignalBus | 运行中总线信号增量（`/signals?after=`） |
| events.jsonl | 结构化事件时间线（`/events?after=`） |
| run.log | 文本日志尾部（`/log?tail=`） |

轮询约 900ms；终态后仍可从历史页回看同一套数据。

---

## 模块 6 · 审计与交付

**作用：** 把一次 run 的状态 + 事件组装成可交付 Markdown；抽取 PASS 产物；为 Flag 校验与人机审批预留联调入口。

### 6.1 审计报告（已接线）

| 控件 | 作用 |
|---|---|
| run_id | 手动粘贴，或「用当前 run」填入任务页正在关注的 ID |
| 生成报告 | `GET /api/runs/:id/report` → Markdown（目标、DAG、评估摘要、工具轨迹、product） |
| 查看 product | `GET /api/runs/:id/product` → 仅 PASS 步骤的 `result` |

适合复盘、对外交付、对照技术路线「审计交付」。

### 6.2 Flag 验证（前端预留）

`POST /api/flag/verify`：提交 `flag` + 可选 `run_id`。  
仓库无独立 Flag 契约；真实校验设计上落在 Evaluator 步骤验收（ee）。当前返回 `valid: null` 占位。

### 6.3 人机协同 HITL（前端预留）

| 控件 | 作用 |
|---|---|
| 拉取待审 | `GET /api/hitl/pending`（当前 `pending: []`） |
| decision | approve / reject / replan / manual |
| 提交决策 | `POST /api/hitl/decide` |

引擎仅有 ee **escalate** 判定，尚无人工审批闸门；本页用于对齐技术路线「人机接管」接口形状。

---

## 模块 7 · 历史记录

**作用：** 扫描磁盘 `runs/`，回看任意一次运行，未终态可续跑，终态可删除。

| 操作 | 作用 |
|---|---|
| 刷新历史 | `GET /api/runs` |
| 点击一条 | 加载快照、DAG、events、log |
| 在任务页关注 | 跳到模块 5 并开始轮询该 run |
| 续跑 resume | `POST /api/runs/:id/resume` → `Engine.resume`（未终态） |
| 删除 | `DELETE /api/runs/:id`（**运行中不可删**） |

---

## 推荐操作流（按角色）

### A. 首次联调 / Demo

1. 模型与预算：配好网关并保存；可选「探测本机工具」  
2. Agent 接口：刷新能力地图，确认 Planner=wired、Executor=stub  
3. 任务运行：用默认 base64 题走「解析 → 启动」  
4. 审计：生成报告；历史：确认 `runs/` 有记录  

### B. 换真题（Web / 附件题）

1. 任务运行 → URL 或附件 Tab → 解析题型 → 必要时手动覆盖类型  
2. 技能库页确认对应分类文档存在  
3. 启动后盯 DAG 与 events；失败看 `fail_reason` 与 run.log  
4. 需要续跑时在历史页 resume  

### C. 与第二组对接（执行 / 评估 / RAG）

1. Agent 接口看 stub / reserved 列表  
2. 经验 RAG、Flag、HITL 页用「试*」按钮确认占位契约形状  
3. 对方实现后：能力地图 status 应变为 wired，预留页去掉 reserved 徽章即可  

---

## API 速查（前端实际调用）

### 已接线

| 方法 | 路径 | 用途 |
|---|---|---|
| GET/POST | `/api/config` | 读/写模型配置 |
| GET | `/api/env-check` | 本机工具探测 |
| GET | `/api/skills` · `/api/skills/:id` | 技能列表与正文 |
| GET | `/api/tools` | 工具声明清单 |
| GET | `/api/sandbox` | 沙箱运行时探测 |
| GET | `/api/capabilities` | 能力地图 |
| POST | `/api/challenge/parse` | 多源归一化 + 题型判定 |
| POST | `/api/challenge/upload` | 附件 base64 落盘 |
| GET/POST | `/api/runs` | 列表 / 启动 |
| GET | `/api/runs/:id` | 快照 |
| GET | `/api/runs/:id/log\|events\|signals` | 日志与增量观测 |
| GET | `/api/runs/:id/report\|product` | 审计报告与产物 |
| POST | `/api/runs/:id/stop\|resume` | 停止 / 续跑 |
| DELETE | `/api/runs/:id` | 删除历史 |

### 前端预留（占位）

| 方法 | 路径 | 设计目标 |
|---|---|---|
| GET | `/api/experience` | 经验总览占位 |
| POST | `/api/experience/query` | `ExperienceStore.query` |
| POST | `/api/experience/record` | `ExperienceStore.record` |
| GET/POST | `/api/flag/verify` | Flag 校验通道 |
| GET | `/api/hitl/pending` | 待人工审批队列 |
| POST | `/api/hitl/decide` | 人工决策回写 |

---

## 目录与实现边界

```
web/
  index.html   # 七步向导结构与表单
  app.js       # 路由步进、API 调用、轮询与渲染
  styles.css   # 指令台视觉（含 reserved / wired 徽章）
  README.md    # 本文档
web_server.py  # 静态资源 + /api/* 网关；包装 Engine / Workspace / Skills
```

- **本仓库前端负责：** 配置、检视、摄入、启动、观测、审计、历史。  
- **不负责：** 真实 Exploit 执行、Docker 沙箱编排、线上 Flag 提交平台对接（预留接口待接）。  
- 静态页由 `SimpleHTTPRequestHandler` 从 `web/` 目录提供；API 与静态资源同端口。

---

## 常见问题

| 现象 | 处理 |
|---|---|
| 启动按钮灰色 | 先点「① 解析题型」；改题面后需重新解析 |
| Planner 报 tool_choice 错误 | 模型页关闭「启用 Planner 工具调用」并保存 |
| 端口占用 | 换端口：`python main.py serve --port 8766` |
| 能力地图显示 stub | 正常：Executor/Evaluator 当前为 Mock，仍可跑通主循环 |
| 经验 / Flag / HITL 返回 reserved | 正常：前端预留；等引擎或第二组实现后替换 |
| 历史删不掉 | 该 run 仍在 live；先停止或等终态 |
| 密钥不回显 | 设计如此；只显示是否已设置 |

---

## 与主仓库文档的关系

| 文档 | 关系 |
|---|---|
| 仓库根 [README.md](../README.md) | 项目总览与 `serve` 启动入口 |
| [design/contracts.md](../design/contracts.md) | Agent / ExperienceStore 契约（模块 3、4） |
| [design/engine.md](../design/engine.md) | 状态机与 run/resume（模块 5、7） |
| [design/signals.md](../design/signals.md) | SignalBus 与 run.log（模块 5） |
| [design/model_config.md](../design/model_config.md) | 模型与预算（模块 1） |

有疑问时：先看本页「状态色含义」与「能力地图」，再决定是改配置、换 Mock，还是对接第二组真实实现。
