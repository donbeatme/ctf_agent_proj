# sandbox_env 沙箱环境管理器

## Scope

执行 agent(②)的沙箱执行环境:把 docker/ssh 目标的**容器生命周期**与**工具依赖**从 runner
中抽出来,做成类适配器的沙箱抽象。主架构(executor)仍只调 `runner.run/run_python`;
`SandboxManager` 在 runner 内部接管沙箱目标(docker/ssh)的生命周期与依赖安装。

**要解决的问题**(旧架构):`docker run --rm` 每条命令一个新容器,容器内 pip/apt 安装
立即丢弃 → 依赖永不持久;且 contracts/checks/runner 三处互相推诿"谁装依赖",无任何安装步骤。

**解耦边界**:主架构只依赖 `SandboxManager`(门面=API);换沙箱后端 = 写新
`SandboxBackend` 子类(本期 ssh + FakeBackend 测试证明平台无关),runner/executor 零改动。

## 目录结构

```text
sandbox_env/
├── __init__.py        # 公开导出
├── errors.py          # SandboxError / SandboxUnavailableError / SandboxExecError / ToolInstallError
├── config.py          # SandboxSettings.from_env()(CTF_SSH_* / CTF_SANDBOX_*)
├── base.py            # SandboxBackend(ABC) + SandboxManager(门面) + session_key_for
├── ssh_backend.py     # SshSandboxBackend(per-challenge 持久容器 over SSH)
├── tools.py           # ToolManager(探测 / 安装(OS 适配,持久) / 冲突与不兼容)
└── cli.py             # 3 条 CLI 命令,main.py 薄接线
```

## 后端接口(SandboxBackend)

```python
class SandboxBackend(ABC):
    name: str
    def ensure(self, session_key=None) -> str       # 容器就绪,返回容器标识(无容器后端 no-op '')
    @abstractmethod
    def exec(self, cmd_str, *, session_key=None, timeout=None) -> ExecOutcome
    def sync(self, local_dir, session_key=None)     # 默认 no-op
    def is_ready(self) -> bool
    def close(self)                                 # 释放连接
    def cleanup(self, session_key=None)             # 销毁会话容器(默认 no-op)
```

## 容器模型(per_challenge)

- 会话键 `session_key_for(cwd) = sha1(绝对 cwd)[:12]` → 容器名 `ctf-<hash>`(docker name 合法)。
- 每 challenge 目录一个**持久**容器:`docker run -d --name ctf-<hash> -v {workdir}/{key}:/work -w /work <image> sleep infinity`。
- 容器内安装持久(解决 `--rm` 无状态);不同 cwd → 不同容器 → 题目间隔离。
- 工作目录挂载会话子目录 `{ssh_workdir}/{key}`;sync 经 `SshBackend.sync_to(local_dir, remote_dir)` 增量上传。
- 生命周期:`docker ps -aq --filter name=^/ctf-<hash>$` 精确匹配(避免误复用到其它 ctf-* 容器);
  `docker exec <name> /bin/bash -lc <cmd>`;`docker rm -f <name>`。

## ToolManager(工具依赖管理)

依赖声明复用 `agent.ctf_skill_tools.CtfSkillToolCatalog`(TOOL_MANIFEST ~70 工具,纯声明)。

- **探测** `probe_tool(tool_id, session_key)`:`import X` → `python3 -c "import X"`;CLI 名 → `command -v <name>`。
  状态 `available|missing|incompatible|manual|unknown`(brew-only → incompatible;manual → manual)。
- **安装** `install_tools(tool_ids, session_key, force)`——**OS 适配**(Debian 容器):
  - pip: `pip install X==v` → `python3 -m pip install --break-system-packages X==v`
  - apt: → `DEBIAN_FRONTEND=noninteractive apt-get install -y X`,先跑一次 `apt-get update`
  - gem/go: 先装运行时(`apt-get install -y ruby` / `golang`)再 `gem install` / `go install`
  - brew → `incompatible`(Debian 无 Homebrew);manual → `skipped_manual`;未知 → `failed`
  - 报告 `{installed, failed, skipped_manual, incompatible}`;装完**重探测**确证。
- **冲突** `tool_conflicts() -> [{a, b, reason, severity: conflict|incompatible|warning}]`:
  1. 同 verify_check 且 >1 → conflict(现例:dnsutils/bind 都 `dig`)
  2. brew-only 且无 alt_methods → incompatible(ghidra/wireshark/bind)
  3. 已知约束 → warning(uncompyle6 需 py≤3.8、hashcat 需 GPU)
  4. 功能冗余 → warning(ROPgadget/ropper)

## 门面(SandboxManager)

```python
class SandboxManager:
    def __init__(self, settings=None, backend=None, catalog=None, max_out=4000, max_err=2000)
    def session_key(self, cwd=None) -> str
    def exec(self, cmd, *, cwd, category, tool_id, target, timeout) -> RunOutcome
    def run_python(self, code, *, ...) -> RunOutcome
    def ensure(self, session_key=None) -> str
    def cleanup(self, session_key=None) -> None
    def probe_tool / install_tools / tool_conflicts   # 委托 ToolManager
```

`exec()` 流程:派生会话键 → `backend.ensure` → `backend.sync`(上传工作区)→ **依赖钩子**
(`tool_id` 在沙箱内缺失时先 `install_tools([tool_id])` 装进该会话容器)→ `backend.exec`。
`install_auto` 关掉时跳过钩子。返回与 `agent.runner.RunOutcome` 同形对象(runner 直传)。

## CLI(main.py 接线)

| 命令 | 行为 |
|---|---|
| `sandbox-probe` | 后端就绪 + 会话容器状态(SSH 未配非零退出) |
| `sandbox-conflicts` | 打印 `tool_conflicts()` 清单 |
| `sandbox-deps <category\|tool_id ...>` | 探测缺失 → 安装(持久)→ 重校验,打印报告 |

## 环境变量(`SandboxSettings.from_env()`,配置架构见 [config.md](config.md))

| 变量 | 默认 | 说明 |
|---|---|---|
| `CTF_SSH_HOST` | — | SSH 目标(读 `config_sandbox`;配置后 runner 懒建 SandboxManager) |
| `CTF_SSH_PORT` | `22` | SSH 端口（支持 Lima 等本机转发 VM） |
| `CTF_SSH_USER` | `root` | SSH 用户 |
| `CTF_SSH_PASSWORD` | — | SSH 密码 |
| `CTF_SSH_WORKDIR` | `/root/ctf` | 远程工作目录根(会话子目录 `{workdir}/{session_key}`) |
| `CTF_SANDBOX_IMAGE` | `ctf-sandbox:latest` | 沙箱镜像 |
| `CTF_SANDBOX_INSTALL_AUTO` | `true` | exec 前自动装缺失工具(进会话容器,持久) |
| `CTF_SANDBOX_KEEP_CONTAINER` | `true` | 解完是否保留容器(便于复查;`cleanup` 显式触发) |
| `CTF_SANDBOX_BACKEND` | `ssh` | 后端类型(本期只实现 ssh) |
| `CTF_SANDBOX_CONTAINER_MODEL` | `per_challenge` | 容器模型(预留 shared/ephemeral) |

## 与 runner/executor 接线边界

- `executor` 零改动:仍只调 `runner.run/run_python`(cwd 收口到题目附件目录)。
- `CommandRunner.__init__` 增 `sandbox=None`;未传且 `config_sandbox` 有 `CTF_SSH_HOST` → 懒建 `SandboxManager`。
- `run()/run_python()` 沙箱唯一执行:sandbox 存在 → 委托 `sandbox.exec/run_python`
  (内部处理生命周期+依赖);sandbox 为 None(无凭据/构造失败)→ 返回 `ok=False` 错误结果,
  **绝不回退宿主** subprocess/WSL/本机 docker。
- `SshBackend` 增 `sync_to(local_dir, remote_dir)`;`sync()` 委托之(向后兼容)。

## 决策记录

- **适配器门面**而非服务化:换沙箱 = 新后端子类;FakeBackend 测试证明平台无关。
- **per-challenge 持久容器**而非每命令 `--rm`:容器内安装持久,题目间隔离。
- **依赖钩子在 manager 内**:工具缺失才装,装在会话容器内,后续命令复用同一容器。
- **brew 直接标 incompatible**:Debian 容器无 Homebrew,不浪费时间重试。
- **沙箱唯一执行,无宿主回退**:sandbox=None(凭据缺失)→ 错误结果;Windows 宿主
  subprocess / WSL / 本机 docker 路径已全部删除,不回退。

## 风险 / 待实测

- **远程 docker 容器生命周期未实测**:`docker run -d` + `docker exec` + `docker rm -f`
  在目标 Alpine VM 上的真实行为(sleep infinity 常驻)需真实联调;SSH 通道已有(paramiko)。
- **镜像仍缺多数工具**:首条命令遇缺失工具会触发一次安装(容器内持久);brew-only 继续报 incompatible。
- **go/gem 工具 PATH**:`go install` 产物在 `$(go env GOPATH)/bin`,安装命令已 export PATH;gem bin 依赖系统 gem bin 目录。
- **容器清理**:`CTF_SANDBOX_KEEP_CONTAINER` 控制解完是否销毁;默认保留便于复查,`cleanup` 显式触发。
