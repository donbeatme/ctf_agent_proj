# 攻防 Agent 指令台（前端状态）

面向操作者、演示方和联调同学：当前前端已经从普通 workflow 编排页升级为攻防任务作战台，入口覆盖登录、战情大屏、任务接入、Agent 实时工作区、成果审核、复盘、能力库、模型用量、MCP 工具和用户管理。

> 注意：页面可见文案已统一转换为“攻防任务 / 场景类型 / 任务情报 / 成果审核 / 复盘报告”等表达。`ctf-web`、`challenge_type`、`flag-value`、`/api/flag/verify` 等仍保留为内部协议、DOM id、API 参数或 CSS 类名，不能直接改成中文文案，否则会破坏前后端联动。

| 项 | 当前状态 |
|---|---|
| 入口文件 | `web/index.html` · `web/app.js` · `web/styles.css` |
| 后端网关 | `web_server.py` |
| 默认地址 | `http://127.0.0.1:8765` |
| 当前推荐启动 | `.venv/bin/python web_server.py --port 8765` |
| 静态资源版本 | `20260820-ops-copy` |
| 页面语境 | 攻防任务、攻防研判、成果审核、证据交付 |

---

## 30 秒启动

```bash
# 仓库根目录
.venv/bin/python web_server.py --port 8765

# 浏览器打开
http://127.0.0.1:8765
```

如果使用 `python main.py serve --port 8765`，必须确保当前 Python 环境已安装 `requirements.txt` 里的依赖。当前本地验证使用的是项目 `.venv`。

---

## 当前页面模块

| 导航 | 页面定位 | 当前状态 |
|---|---|---|
| 战情总览 | 攻防任务大屏，展示运行槽、队列、完成量、成果待审、正确率、趋势图、场景分布、用量与审核漏斗 | 前端 mock 动态图表，带悬浮提示 |
| 攻防任务接入 | 文本、JSON、URL、附件、平台任务库、本地任务目录接入；识别场景并派发 Agent | 已接 `/api/challenge/parse`、`/api/challenge/upload`、`/api/challenge/understand`、`/api/platform/*` |
| Agent 工作区 | 能力角色、状态地图、执行链路、作战角色卡片 | 已接 `/api/capabilities` |
| 成果审核 | 复盘报告、product、成果口令本地核验/平台提交、审核队列 | 报告/product 已接；成果核验已接平台适配器 |
| 赛后复盘 | runs 历史、详情、DAG、events、log、关注、续跑、删除 | 已接 `/api/runs/*` |
| 技能镜像 | 战术卡片、工具矩阵、沙箱矩阵，均按场景类型导航 + 卡片 + 弹窗展示 | 已接 `/api/skills`、`/api/tools`、`/api/sandbox`、`/api/sandbox/runtime` |
| 战术黑板 | 线索、失败路径、复用打法、人工提示 | 前端展示为主，经验写回仍是预留接口 |
| 模型用量 | 总览、阶段、模型、场景维度用量展示 | 前端展示为主，后续接真实聚合 |
| MCP 工具 | 核心工作区、平台桥接、沙箱管理、浏览器、专项工具连接状态 | 前端能力声明 + 详情弹窗 |
| 用户管理 | 登录/注册演示、队伍资料、偏好设置、主题切换 | 本地演示态，不上传凭据 |

---

## 最近前端改动记录

### 视觉和产品形态

- 放弃“按步骤 workflow”心智，改成攻防任务作战台。
- 登录/注册作为进入系统前置页面，不再放在左侧业务目录中。
- 左侧导航铺满页面高度，主内容铺满工作区，不再只在页面中间显示内容。
- 战情总览升级为大屏：折线/柱状/环形/漏斗等图表，带悬浮突出和说明。
- 技能镜像中的战术卡片、工具矩阵、沙箱矩阵都改为“横向场景导航 + 卡片列表 + 详情弹窗”。
- 各模块逐步改成卡片、导航、弹窗、状态块组合，减少纯文字堆叠。
- 可见文案已从“CTF 解题”语境切换为“攻防任务 / 攻防研判”语境。

### 真实能力对接

- 新增平台桥接区：
  - `GET /api/platform/status`
  - `POST /api/platform/sync`
  - `POST /api/platform/fetch`
  - `POST /api/platform/target`
- 新增真实任务理解入口：
  - `POST /api/challenge/understand`
  - 当传入 `challenge_dir` 或 `metadata_path` 时走 `RealTaskUnderstander`
- 新增沙箱运行时状态：
  - `GET /api/sandbox/runtime`
  - 展示 SSH/Pi 配置、镜像、工作目录、工具冲突
- 成果核验不再只是占位：
  - `POST /api/flag/verify`
  - 默认本地核验，勾选后才调用平台 submit
- 修复 `SmokeEvaluator` 导入：
  - 当前 `SmokeEvaluator` 来自 `agent.evaluator`
  - `web_server.py` 不再从 `main` 导入它

---

## 关键交互说明

### 登录 / 注册

当前是本地演示登录，不上传凭据。注册按钮第一次点击进入注册态，第二次校验密码后进入系统。

### 攻防任务接入

支持五类入口：

| 入口 | 说明 |
|---|---|
| 任务情报文本 | 标题、描述、任务 ID、目标列表 |
| JSON 导入 | 平台导出 JSON 或通用 JSON |
| 目标 URL | 远程服务地址和补充说明 |
| 附件上传 | 多文件上传到 `downloads/uploads/` |
| 平台任务库 / 本地物化任务 | 通过平台适配器拉取，或直接理解本地任务目录 |

派发前必须先识别场景。识别结果会写入内部 `challenge_type` 字段，这是引擎和技能检索协议的一部分，页面文案不会直接展示为“CTF 解题”语境。

### 实时作战面板

派发 Agent 后，会像浏览器子页一样打开一个与“投递攻防任务”并列的任务标签。该面板展示：

- run_id、状态、当前步骤、tokens
- 状态机
- 计划 DAG
- SignalBus
- events.jsonl
- run.log
- 可解释过程、工具状态和文件工作区示意

### 成果审核

复盘报告和 product 已接线：

- `GET /api/runs/:id/report`
- `GET /api/runs/:id/product`

成果提交通道：

- 默认只做本地答案库核验
- 勾选“真实提交到平台”后才调用平台提交
- 需要平台适配器凭证已配置，否则会返回配置或鉴权相关错误

---

## API 速查

### 已接线

| 方法 | 路径 | 用途 |
|---|---|---|
| GET/POST | `/api/config` | 读写模型配置 |
| GET | `/api/env-check` | 本机工具探测 |
| GET | `/api/skills` · `/api/skills/:id` | 能力文档列表与详情 |
| GET | `/api/tools` | 工具声明清单 |
| GET | `/api/sandbox` | 按场景的沙箱能力探测 |
| GET | `/api/sandbox/runtime` | 沙箱运行时、镜像、工具冲突 |
| GET | `/api/platform/status` | 平台配置、索引、缓存状态 |
| POST | `/api/platform/sync` | 同步平台任务索引 |
| POST | `/api/platform/fetch` | 拉取、物化并理解平台任务 |
| POST | `/api/platform/target` | 开关平台靶机/目标环境 |
| POST | `/api/challenge/parse` | 多源输入归一化和场景识别 |
| POST | `/api/challenge/understand` | 真实任务目录理解 |
| POST | `/api/challenge/upload` | 附件落盘 |
| GET/POST | `/api/runs` | 历史列表 / 启动 run |
| GET | `/api/runs/:id` | run 快照 |
| GET | `/api/runs/:id/log` | 文本日志 |
| GET | `/api/runs/:id/events` | 结构化事件 |
| GET | `/api/runs/:id/signals` | 运行中信号 |
| GET | `/api/runs/:id/report` | 复盘报告 |
| GET | `/api/runs/:id/product` | PASS 步骤产物 |
| POST | `/api/runs/:id/stop` | 请求停止 |
| POST | `/api/runs/:id/resume` | 续跑 |
| DELETE | `/api/runs/:id` | 删除历史 |
| GET/POST | `/api/flag/verify` | 本地核验 / 平台提交成果口令 |

### 仍是预留或展示态

| 路径 / 模块 | 当前状态 |
|---|---|
| `/api/experience/query` | 返回预留响应，经验库未真正接入 Engine |
| `/api/experience/record` | 返回预留响应，写回未落库 |
| `/api/hitl/pending` | 返回预留响应，人工审批队列未接 |
| `/api/hitl/decide` | 返回预留响应，人工决策未接 |
| 战术黑板 | 当前以前端卡片展示为主 |
| 模型用量 | 当前以前端统计展示为主，后续可接真实 runs 聚合 |

---

## 已知边界

- 平台任务同步、拉取、提交依赖 `config_adaptor.json` 或环境变量中的平台登录态 / token。
- 沙箱运行时探测依赖 `config_sandbox.json` 或环境变量中的 SSH/Pi 配置。
- 页面文案已攻防化，但内部协议仍沿用原项目字段名，例如 `challenge_id`、`challenge_type`、`flag`。这些字段是后端契约，不是展示文案。
- 若本地缺依赖，优先使用 `.venv/bin/python` 启动；当前本地已经补装 `PyYAML`。

---

## 常见问题

| 现象 | 处理 |
|---|---|
| 点击派发时报 `SmokeEvaluator from main` | 已修复；确认服务已重启，`web_server.py` 应从 `agent.evaluator` 导入 |
| 启动按钮灰色 | 先点击“识别攻防任务”或通过平台/本地任务理解生成识别结果 |
| 平台显示未配置 | 配置 `CTF2_SESSION_TOKEN` / `CTF2_COOKIE` / `CTF2_API_KEY` 或 `config_adaptor.json` |
| 沙箱显示未配置 | 配置 `CTF_SSH_HOST` 等沙箱参数或 `config_sandbox.json` |
| 成果提交失败 | 先确认平台状态已配置；默认本地核验不会真实提交 |
| 页面仍显示旧文案 | 强刷新浏览器；当前资源版本为 `20260820-ops-copy` |
