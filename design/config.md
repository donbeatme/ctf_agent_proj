# 配置架构(config_adaptor / config_sandbox / model_config)

## Scope

配置按**消费方**拆分三模块,与各自的适配器/沙箱子类实现配对,主 config(model_config)
只承载 LLM/引擎/评估器配置。安全边界:敏感凭证与主配置分离、走 gitignore 的私有 JSON、
**不进 DB、不进日志、不进错误信息**。

## 三模块总览

| 模块 | JSON 文件 | 消费方 | 配对 | 敏感项 |
|---|---|---|---|---|
| `config_adaptor.py` | `config_adaptor.json` | `StoreSettings.from_env()` | `Ctf2Adapter`(平台适配器子类) | `CTF2_SESSION_TOKEN` / `CTF2_TOKEN` / `CTF2_COOKIE` / `CTF2_API_KEY` |
| `config_sandbox.py` | `config_sandbox.json` | `SandboxSettings.from_env()` | `SandboxManager` → `SandboxBackend`(沙箱子类) | `CTF_SSH_PASSWORD` |
| `model_config.py` | `model_config.json` | `llm_api` / engine / evaluator | —(主配置) | `LLM_API_KEY`(env,`DEEPSEEK_*` 兜底) |

`config_adaptor.json` / `config_sandbox.json` / `model_config.json` 均已加入 `.gitignore`。

## 取值优先级

三模块统一「**env 优先 → 各自 JSON 兜底**」;`config_adaptor` 额外保留
`CTF2_CONFIG_JSON` 指向的外部文件作兼容兜底:

```text
config_adaptor.get(name)   =  env  →  config_adaptor.json  →  CTF2_CONFIG_JSON 外部文件  →  default
config_sandbox.get(name)   =  env  →  config_sandbox.json                              →  default
model_config.get(name)     =  env  →  model_config.json                                →  default
```

`config_sandbox.get` 用 `if value:` 判空(空串落到 JSON);`config_adaptor` 用 `value is not None`。

## 配对关系

```text
平台适配器:  Ctf2Adapter  ──uses──▶  StoreSettings.from_env()  ──reads──▶  config_adaptor
沙箱管理器:  SandboxManager ──uses──▶ SandboxSettings.from_env()  ──reads──▶  config_sandbox
命令执行:    CommandRunner(懒建 SandboxManager) ──reads──▶ config_sandbox(CTF_SSH_HOST 就绪则建)
```

- 换平台 = 写新 `ChallengeAdapter` 子类;换沙箱后端 = 写新 `SandboxBackend` 子类;
  各自读自己的配置模块,主架构零改动。
- `CommandRunner` 无显式注入 `sandbox` 时,懒建 `SandboxManager()`(读 `config_sandbox`);
  构造失败(缺 `CTF_SSH_HOST`/凭据/paramiko)→ 命令返回 `ok=False` 错误结果,**绝不回退宿主**。

## key 清单

### config_adaptor(适配器,配对 Ctf2Adapter→StoreSettings)

| key | 敏感 | 默认 | 说明 |
|---|---|---|---|
| `CTF2_SESSION_TOKEN` | **是** | — | 网页登录态 JWT(Bearer,会话 API);等价旧 `CTF2_TOKEN` |
| `CTF2_API_KEY` | **是** | — | 个人访问令牌 PAT(open API,仅开靶机需要) |
| `CTF2_COOKIE` | **是** | — | 会话 cookie(token 优先) |
| `CTF2_SESSION_BASE` | 否 | — | **会话 API base**(`/api/v1`),会话端点优先用它 |
| `CTF2_BASE_URL` | 否 | `https://ctf2.dasctf.com/api/v1` | 会话 base 兜底(旧布局);**别填 open 基址**(open API 由 origin 派生,填了会 404) |
| `CTF2_ORIGIN` | 否 | `https://ctf2.dasctf.com` | 浏览器头 Origin/Referer(防风控) |
| `CTF2_PRACTICE_GROUND_ID` | 否 | — | 靶场 id |
| `CTF2_SUBMIT_URL_TEMPLATE` | 否 | — | 覆盖默认提交 URL |
| `CTF2_DOWNLOAD_URL_TEMPLATE` | 否 | — | 详情无 download_url 时兜底模板 |
| `CTF2_LIST_PAGE_SIZE` | 否 | `100` | sync 分页大小 |
| `CTF2_AUTO_START_TARGET` | 否 | `true` | 物化含容器题自动开靶机 |
| `CTF_STORE_DIR` | 否 | `./data` | 库+缓存根目录 |
| `CTF_ATTACHMENT_CACHE_BYTES` | 否 | `2*1024**3` | 附件缓存上限 |

### config_sandbox(沙箱,配对 SandboxManager→SandboxSettings)

| key | 敏感 | 默认 | 说明 |
|---|---|---|---|
| `CTF_SSH_HOST` | 否 | — | SSH 目标;配置后 runner 懒建 SandboxManager |
| `CTF_SSH_USER` | 否 | `root` | SSH 用户 |
| `CTF_SSH_PASSWORD` | **是** | — | SSH 密码 |
| `CTF_SSH_WORKDIR` | 否 | `/root/ctf` | 远程工作目录根(会话子目录 `{workdir}/{session_key}`) |
| `CTF_SANDBOX_BACKEND` | 否 | `ssh` | 后端类型(本期只实现 ssh) |
| `CTF_SANDBOX_IMAGE` | 否 | `ctf-sandbox:latest` | 沙箱镜像 |
| `CTF_SANDBOX_CONTAINER_MODEL` | 否 | `per_challenge` | 容器模型(预留 shared/ephemeral) |
| `CTF_SANDBOX_INSTALL_AUTO` | 否 | `true` | exec 前自动装缺失工具(进会话容器,持久) |
| `CTF_SANDBOX_KEEP_CONTAINER` | 否 | `true` | 解完是否保留容器 |

## 凭证获取

ctf2 平台两类凭证的获取方式:

- **`CTF2_SESSION_TOKEN`(网页登录态 JWT)**:登录 https://ctf2.dasctf.com 后,按
  F12 打开 DevTools → Application(应用程序)面板,找到登录态里名为 `token` 的值
  (JWT,`eyJ...` 开头),复制进 `config_adaptor.json`。
- **`CTF2_API_KEY`(PAT,开靶机需要)**:在
  https://ctf2.dasctf.com/dashboard/developer/open-api 创建个人访问令牌,复制
  `ctf2_...` 开头的值进 `config_adaptor.json`。

拿到后写入 `config_adaptor.json`(或 export 同名 env),不提交进 git。

## 交接模板

`config_adaptor.json.example` / `config_sandbox.json.example` 是**可提交**的占位模板
(敏感 key 留空,非敏感 key 带默认值),用于新环境交接:

```bash
cp config_adaptor.json.example config_adaptor.json
cp config_sandbox.json.example config_sandbox.json
# 再填入真实值
```

空串占位符安全:`StoreSettings.from_env`/`SandboxSettings.from_env` 均以 `or None`/`or ""`
把空值落到默认,照抄不填只得到 `None`(沙箱未配置 → 命令返回错误结果,绝不回退宿主);
env 变量始终优先,模板照抄不影响 `CTF2_CONFIG_JSON` 外部兜底。

## 安全边界

- 敏感凭证 **不进 DB、不进错误信息、不写日志**(`challenge_flags`/`events.jsonl`/run.log 均不含)。
- CDN 下载请求(`ctf2-files.dasctf.com`)**不带任何凭证头**(token/PAT 绝不发给第三方域名)。
- 三 JSON 均 gitignore;`CTF2_CONFIG_JSON` 外部文件仅作兼容兜底(新布局优先写 `config_adaptor.json`)。

