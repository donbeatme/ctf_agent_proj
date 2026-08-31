# CTF2 Agent 项目测试报告

## 1. 报告信息

| 项目 | 内容 |
|---|---|
| 测试对象 | CTF2 Agent 多智能体 CTF 解题平台（整个项目） |
| 测试日期 | 2026-08-30 |
| 报告生成时间 | 2026-08-30 15:28:32 CST（UTC+08:00） |
| 仓库路径 | `/Users/zth/match/ctf_agent_proj` |
| 测试分支 | `feat/real-agent-vm-20260829` |
| 基线提交 | `59b512bf7997d1e242f5caaffc219dc03841da3d` |
| 基线说明 | `docs: clarify local configuration boundary` |
| 工作树状态 | 基于当前工作树测试；测试开始前存在 6 个已跟踪但未提交的修改文件 |
| 总体结论 | **有条件通过** |

> 本报告面向项目整体能力，不只针对某次前端或后端改动。测试结果代表上述分支和当前工作树的组合状态，不等同于 `master` 分支的纯净提交结果。

## 2. 项目与测试范围

项目当前主要包含以下模块：

- 多智能体执行引擎：状态机、DAG、调度、重规划、恢复、超时和 Token 预算。
- Agent 角色：Planner、Executor、Evaluator 以及 Mock/离线实现。
- 上下文与工作区：事件账本、运行状态、历史、日志、产物和信号总线。
- 任务理解：本地题目、附件、JSON、题型识别、目标生成和多源输入归一化。
- CTF 平台适配：题目同步/物化、SQLite 索引、附件缓存、靶机控制和 Flag 提交。
- 沙箱环境：SSH 后端、任务容器、命令执行、文件同步、工具依赖和冲突分析。
- 审计与复盘：计划/步骤/任务评估、Flag 校验、经验记录和运行指标。
- Web 指令台：配置、任务接入、运行面板、可解释过程、复盘、用量及相关 API。
- 技能库：11 类、118 份 Markdown 技能文档。

本次统计到：

| 资产 | 数量 |
|---|---:|
| `tests/test_*.py` 测试模块 | 59 |
| 核心 Python 源文件 | 65 |
| CTF 技能 Markdown | 118 |
| `design/` 设计文档 | 16 |

## 3. 测试环境

| 项目 | 版本/状态 |
|---|---|
| 操作系统 | macOS 26.4.1（Build 25E253） |
| 系统架构 | Apple Silicon / arm64 |
| Python | 3.12.14 |
| Node.js | v22.23.1 |
| Git | 2.50.1（Apple Git-155） |
| Pytest | 9.1.1（`requirements-dev.txt` 声明 `pytest>=9`） |
| 虚拟环境 | 仓库内 `.venv`，可正常执行 Python 与 Pytest |
| 私有配置 | `model_config.json`、`config_adaptor.json`、`config_sandbox.json` 均存在；未读取或记录任何密钥值 |
| Lima VM | `ctf-sandbox` 已创建但测试时为 `Stopped` |
| VM 规格 | 4 CPU、8 GiB 内存、60 GiB 磁盘、SSH `127.0.0.1:60022` |

## 4. 执行结果

### 4.1 当前本地配置下的全量测试

命令：

```bash
PYTHONPATH=. .venv/bin/pytest -q
```

结果：

```text
702 passed, 4 skipped, 1 failed in 14.65s
```

唯一失败项：

```text
tests/test_audit_flag_delegation.py::test_settings_auto_offline_without_key
期望: Settings.from_env().mode == "offline"
实际: Settings.from_env().mode == "online"
```

原因分析：

- 用例只删除了 `CTF_AUDIT_MODE`、`LLM_API_KEY` 和 `DEEPSEEK_API_KEY` 环境变量。
- 产品代码按“环境变量优先、`model_config.json` 兜底”读取配置。
- 当前本地私有 `model_config.json` 仍配置了可用模型 Key，因此产品代码按设计选择 `online`。
- 该失败属于测试未隔离本地配置文件导致的环境相关失败，不是 Agent 主流程执行失败。

### 4.2 清空内存配置缓存后的全量复测

本次复测仅在 Pytest 进程内清空 `model_config` 缓存，没有修改磁盘配置文件：

```bash
PYTHONPATH=. .venv/bin/python -c \
  'import model_config, pytest; model_config._config = {}; raise SystemExit(pytest.main(["-q", "-rs"]))'
```

结果：

```text
703 passed, 4 skipped in 10.59s
```

按已执行测试计算，通过率为 **100%（703/703）**。

跳过项及原因：

| 测试 | 原因 |
|---|---|
| `test_csaw_maze_real_challenge_ingestion_is_json_safe` | 缺少真实 ELF maze fixture |
| `test_csaw_maze_real_challenge_offline_workflow_reaches_done` | 缺少真实 ELF maze fixture |
| `test_image_understander_adds_semantics_to_image_artifact` | 当前 fixture 没有图片 artifact |
| `test_image_understander_failure_degrades_to_error_metadata` | 当前 fixture 没有图片 artifact |

### 4.3 静态与语法检查

执行内容：

```bash
PYTHONPATH=. .venv/bin/python -m compileall -q \
  agent audit ctf_platform sandbox_env task_understanding \
  web_server.py main.py config_adaptor.py config_sandbox.py opslog.py
node --check web/app.js
git diff --check
```

结果：全部通过。

- Python 核心模块可编译。
- Web JavaScript 无语法错误。
- Git 补丁无空白字符错误。

### 4.4 CLI 冒烟

`main.py --help` 可正常运行，成功注册以下主要命令：

- `run-task`、`run-local-challenge`
- `challenge-fetch`、`challenge-sync`、`challenge-list`
- `flag-submit`、`challenge-target`、`flags-import`
- `cache-stats`、`cache-purge`
- `sandbox-probe`、`sandbox-conflicts`、`sandbox-deps`
- `serve`

本地题库查询成功：`Medium` 难度共返回 208 条索引记录。

### 4.5 Web 服务冒烟

在独立端口 `127.0.0.1:8766` 临时启动服务，完成检查后已停止。以下入口全部返回 HTTP 200：

| 路径 | 结果 |
|---|---:|
| `/` | 200 |
| `/api/config` | 200 |
| `/api/capabilities` | 200 |
| `/api/runs` | 200 |
| `/api/sandbox/runtime` | 200 |

说明：`/api/sandbox/runtime` 返回 200 表示接口可用，不代表 SSH/Docker 运行时已就绪。

### 4.6 真实沙箱探测

命令：

```bash
PYTHONPATH=. .venv/bin/python main.py sandbox-probe
```

结果：失败，`127.0.0.1:60022` 拒绝连接。根因是 Lima VM 在测试时处于 `Stopped`，与状态查询一致。

本次报告没有自动启动 VM，因为启动虚拟机和创建容器会改变本地运行状态。该项记为“环境未就绪/未完成真实链路验证”，不记为单元测试缺陷。

## 5. 子系统结论

| 子系统 | 自动化结果 | 本次结论 |
|---|---|---|
| Engine 状态机、DAG、调度、恢复 | 通过 | 核心控制流可用 |
| Planner/Executor/Evaluator | 通过 | Mock、离线规则及工具循环契约通过 |
| Workspace、事件、日志、上下文 | 通过 | 持久化与投影逻辑通过 |
| 任务理解与题型识别 | 通过，4 项跳过 | 常规输入通过；真实 ELF/图片夹具覆盖不足 |
| CTF2 平台适配与存储 | 通过 | FakeSession/离线 fixture、SQLite、缓存及 CLI 通过 |
| SSH/Docker 沙箱代码 | 通过 | Mock/FakeBackend 自动化通过；真实 VM 链路未验证 |
| 审计、Flag、经验与评估 | 有条件通过 | 干净配置全过；本地配置会污染一个测试 |
| Web 服务与前端脚本 | 通过 | 关键接口 200，Python/JavaScript 语法通过 |
| 技能库与工具目录 | 通过 | 文档加载、检索、工具声明和依赖检查通过 |

## 6. 未覆盖或未执行项目

以下能力未在本次报告中宣称通过：

- 真实 LLM 联网调用、限流、长时间稳定性及费用结算准确性。
- 真实 CTF2 平台同步、开靶和 Flag 提交，避免产生外部平台副作用。
- Lima VM 启动后的 SSH、Docker 镜像、任务容器创建、命令执行和销毁全链路。
- 缺失 ELF 与图片 fixture 对应的 4 个测试场景。
- 并发压力、性能基准、浏览器兼容性、渗透测试和依赖漏洞扫描。
- 自动化代码覆盖率；当前项目未配置覆盖率门槛或本次环境未运行覆盖率工具。

## 7. 问题与风险

### P1：测试受本地私有配置污染

`test_settings_auto_offline_without_key` 没有隔离 `model_config.json`，导致开发机配置了 Key 时全量测试无法零失败。

建议：测试中同时清空/替换 `model_config._config`，或为配置加载器提供可注入的临时配置路径。CI 必须使用无私有配置的干净环境。

### P1：真实运行链路本次未验收

VM 已创建但处于停止状态，SSH 探测失败，因此本报告不能确认当前机器上的真实 Agent → SSH → Docker → 工具执行链路可用。

建议：发布或比赛前执行一次独立的真实链路验收，至少验证容器创建、命令执行、事件写入、Flag 提交判定和容器清理。

### P2：关键多模态夹具缺失

真实 ELF maze 和图片 artifact 用例被跳过，会降低二进制摄入和图片 JSON 安全转换的回归保障。

建议：补充小型、许可证清晰、可提交仓库的 fixture，或在 CI 中通过受控制品下载恢复这些测试。

### P2：沙箱 CLI 失败信息不够友好

VM 停止时 `sandbox-probe` 输出完整 Python traceback。虽然退出码正确为非零，但对比赛现场诊断不够直接。

建议：CLI 捕获连接异常，输出简短的 SSH 地址、失败原因和 `scripts/match_vm.sh start` 修复提示。

### P2：缺少覆盖率与发布门禁

现有测试规模较大，但没有可见的覆盖率阈值、静态类型检查、依赖漏洞扫描或固定 CI 质量门禁。

建议：增加 `pytest-cov`、Ruff/类型检查和依赖审计，并规定核心模块覆盖率及零失败发布条件。

## 8. 发布建议

当前代码级自动化测试在干净配置下全部通过，核心模块具备较好的回归基础；Web 与 CLI 基础入口可用。因此项目可进入下一阶段联调，但暂不建议仅凭本报告直接认定“真实比赛环境完全可发布”。

发布前建议至少完成：

1. 修复审计模式测试的配置隔离，确保普通本地环境和 CI 都能直接 `pytest -q` 零失败。
2. 启动 Match 专用 VM，确认 `sandbox-probe` 成功且 `ctf-sandbox:latest` 镜像存在。
3. 选一道不需要在线靶机的附件型题目，执行真实 Agent 全链路。
4. 核对事件流包含 `sandbox.container_created`、`sandbox.exec`、`tool_result`、`submission correct=true` 和 `sandbox.container_removed`。
5. 补齐 4 个缺失 fixture 对应的测试，增加覆盖率与 CI 门禁。

## 9. 最终结论

**测试结论：有条件通过。**

- 代码级全量自动化：干净配置下 `703 passed`，无失败。
- 当前开发机默认配置：存在 1 个测试隔离失败，不影响已验证的核心逻辑，但必须修复以保证可重复测试。
- Web/CLI/静态检查：通过。
- 真实 VM/SSH/Docker/外部平台链路：本次未完成，发布前必须专项验收。
