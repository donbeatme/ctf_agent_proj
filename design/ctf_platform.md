# ctf_platform 平台适配器 + 本地存储

## Scope

真实 CTF 平台接入层：把任务理解层的输入（URL / 题目 JSON / friendly_id）解析、下载附件、物化成本地 challenge 目录，供 `RealTaskUnderstander` 消费；并把正确 flag 落本地 SQLite 复用。

**解耦边界（核心）**：主架构只依赖 `ChallengeAdapter` 基类（接口=API）；ctf2 是它的一个实现子类，换平台/靶场只换子类。正确 flag 存适配器侧库；提交记录/验证历史归主架构 history（workspace/audit/run.log），**本模块不存提交历史**。

**存储策略**：buuctf ~8000 题全量附件必然爆盘。本地只存**索引摘要**（challenges 表，不存 distfiles），附件走 **LRU + 容量上限**（`attachment_cache`），超限淘汰最久未用。

## 目录结构

```text
ctf_platform/
├── __init__.py        # 公开导出
├── config.py          # StoreSettings.from_env()（env 优先，config_adaptor 兜底）
├── errors.py          # AdapterError / AuthError / DownloadError / ParseError / CacheIntegrityError
├── base.py            # ChallengeAdapter(ABC)：4 能力 + ingest 模板方法 + 物化契约
├── storage.py         # ChallengeStore(SQLite) + AttachmentCache(LRU)
├── ctf2.py            # Ctf2Adapter(ChallengeAdapter)：ctf2 平台实现子类
└── cli.py             # 7 条 CLI 命令，main.py 薄接线
```

## 适配器 4 能力（基类接口）

1. **输入 → 本地物化** `ingest(source, dest_dir=None) -> Path`：`parse(source)` 抽象 hook → 索引落库 → 附件入缓存 → copy 到 `{dest}/distfiles/` + 写 `metadata.yml`。返回 challenge 目录，understander 直接消费。
2. **下载管理** `download(file_id, challenge_id) -> bytes`：抽象方法；共享 `AttachmentCache`（LRU + md5 校验 + 容量上限）。物化一律 **copy**（agent 会 patch 二进制，硬链接会污染缓存；Windows symlink 受限）。
3. **提交/交互** `submit(challenge_id, flag) -> SubmitResult`：抽象方法；`start_target/stop_target` 容器靶机开/关（基类默认桩返回 `{}`，ctf2 子类已实测实现，见下）。
4. **持久化** `persist_flag(...)` / `get_flag(...)` / `cache_stats()` / `cache_purge()`：共享实现，正确 flag 落 `challenge_flags`。

## SQLite schema（`{CTF_STORE_DIR}/ctf_platform.db`，默认 `./data`）

```sql
PRAGMA user_version = 1;
CREATE TABLE challenges (
  challenge_id TEXT PRIMARY KEY, platform TEXT NOT NULL,
  friendly_id TEXT NOT NULL, practice_ground_id TEXT,
  name TEXT NOT NULL, category TEXT, difficulty TEXT, description TEXT,
  summary TEXT, challenge_type TEXT, points INTEGER,
  has_container INTEGER DEFAULT 0, target TEXT,
  solve_count INTEGER DEFAULT 0, is_solved INTEGER DEFAULT 0,
  extra_json TEXT, last_synced_at TEXT NOT NULL);
CREATE UNIQUE INDEX ux_challenges_platform_friendly ON challenges(platform, friendly_id);
CREATE TABLE challenge_files (
  file_id TEXT PRIMARY KEY,
  challenge_id TEXT NOT NULL REFERENCES challenges(challenge_id) ON DELETE CASCADE,
  file_name TEXT NOT NULL, file_size INTEGER, file_md5 TEXT, file_type TEXT,
  path TEXT, updated_at TEXT);
CREATE INDEX ix_challenge_files_challenge ON challenge_files(challenge_id);
CREATE TABLE challenge_flags (
  challenge_id TEXT PRIMARY KEY REFERENCES challenges(challenge_id) ON DELETE CASCADE,
  flag TEXT NOT NULL, flag_format TEXT,
  source TEXT NOT NULL,            -- 'flag_rules' | 'verified_submission' | 'manual'
  verified INTEGER DEFAULT 0, updated_at TEXT NOT NULL);
CREATE TABLE attachment_cache (
  file_id TEXT PRIMARY KEY REFERENCES challenge_files(file_id) ON DELETE CASCADE,
  challenge_id TEXT NOT NULL, rel_path TEXT NOT NULL,
  size_bytes INTEGER NOT NULL, md5 TEXT, last_access REAL NOT NULL);
CREATE INDEX ix_attachment_cache_last_access ON attachment_cache(last_access);
```

- 附件缓存布局：`{CTF_STORE_DIR}/cache/blobs/{file_id[:2]}/{file_id}`（扁平两级分片）。
- 淘汰：`last_access ASC` 取最旧逐条删，删到 ≤ `capacity * 0.8`（留余量）；**不淘汰最后一件**——单件超容量时保留并 warning（无法满足阈值但缓存仍持有）。
- FK 已启用（`PRAGMA foreign_keys=ON`）：attachment_cache.file_id 必须存在于 challenge_files（真实流程 ingest 先 upsert challenge + files 再下载）。

## Ctf2Adapter（ctf2 平台子类）

- `platform = "ctf2"`，`base_url = https://ctf2.dasctf.com/api/v1`（会话 API base，实测自 `D:/pythonProject/ctf2/api.py` + 真实联调）。
- `parse(source)`：① ctf2 题目 JSON（fixtures 形态）→ 字段映射；② 题目 URL（`/practice/{gid}/challenges/{cid}` 抽 UUID）→ 拉取；③ friendly_id/challenge_id → 先查本地索引，miss 则拉取。
  - **详情端点只接受 UUID**：fresh 库上 friendly_id 会 400 `INVALID_ID`，报错提示先 `challenge-sync` 建索引（或改用 URL/UUID）。
- `download(file_id, challenge_id)`：**真实流程 = 详情 GET（会话 API）→ `files[].download_url`（CDN 直链，`ctf2-files.dasctf.com`，无需鉴权）**。详情 404 或缺失该附件时回退 env `CTF2_DOWNLOAD_URL_TEMPLATE` 自定义模板。401/403 立即抛 `AuthError`（含缺凭证提示）。**CDN 请求不带任何凭证头**（token 绝不发给第三方域名）。附件已实测：md5 与平台 file_md5 一致。
- `submit(challenge_id, flag)`：**先查本地答案库——已有该题 `verified` 正确 flag → 直接本地比对判定（`verdict=LOCAL_VERIFIED`），跳过平台往返**（平台对已解出的题不再判分，本地 success 落库的 flag 即权威答案，实测：已解出的题平台只回 `ALREADY_SOLVED`）；无 verified 记录才 POST `/practice/{gid}/challenges/{cid}/submit/`，body `{"flag": ...}`（`api.py` 确认），宽泛解析 correct/success/accepted 等键 + `handle_submit_result` 语义（success→正确 / INCORRECT_FLAG→错误 / ALREADY_SOLVED→本地基准比对 / SUBMISSION_RISK_CHALLENGE_REQUIRED→风控验证码未判定）。每次提交落 `submissions` 日志（支撑本地比对）。
- `sync_challenges()`：分页拉全量索引 → 批量 upsert（供索引建库，不下载附件）。分页用响应 `data.pagination.total` 终止，兼容 `len<page_size` 兜底。真实联调已拉取 6133 题。
- **靶机开/关（容器动态 spawn，已实测）**：
  - `start_target(challenge_id, timeout=120)`：**open API** `POST /api/open/v1/user/practice/{gid}/challenges/{cid}/environment/start/`（Bearer **PAT**，即 `CTF2_API_KEY`）→ 平台异步 `status: starting` → 轮询（3s 间隔，幂等重 POST）直至 `access_ready: true` → 返回 `{host, port, access_url, access_urls[], environment_id, expires_at, status}`。访问地址形态 `{hash}.tcp-ctf2.dasctf.com:9999`（`nc_ssl` 标记 TLS）。host:port 写回 `challenges.target` 供理解层/执行层读取；环境 1 小时过期自动回收。
  - `stop_target(challenge_id, confirmation=True)`：**会话 API** `DELETE /practice/{gid}/challenges/{cid}/target/`（浏览器头）。`confirmation` 语义同平台（CLI 侧 `--yes` 确认才发）；成功后清除 `challenges.target`。
  - 状态查询：会话 API `GET .../target/` 返回同一 access_url（无环境时 `data: null`）。
- `session` 可注入（测试传 `FakeSession`，不引 requests-mock）。

## 物化目录契约（understander 接缝）

`{challenge_dir}/metadata.yml` + `{challenge_dir}/distfiles/`。metadata.yml 字段：

```yaml
id, platform, friendly_id, name, category, difficulty,
description, points, has_container, files: [rel_path, ...]
# target 可选(含容器题自动开靶后写入 host:port)
```

`files` 为 rel_path 清单 → understander 转 `provided_files` 约束；`category` → `category` 约束；`target` → understander `_parse_target` 转 `target_info` → 执行上下文注入 `# 目标地址(靶机)`（见 executor `_target()`）。

**含容器题自动开靶**：`Ctf2Adapter._materialize` 覆写——`has_container && CTF2_AUTO_START_TARGET && 无 target` 时调 `start_target` 把 `host:port` 写回 `meta.target`/`challenges.target` 再物化；开靶失败（缺 PAT/超时）记 `adapter.target_auto_start_failed` 事件、物化照常（无 target）。关闭该开关或题带静态 target 时不自动开。

## CLI（main.py 接线）

| 命令 | 参数 | 行为 |
|---|---|---|
| `challenge-fetch <source>` | URL/JSON 路径/friendly_id、`--dest` | `ingest` → 物化目录，打印路径；鉴权失败非零退出 |
| `challenge-sync` | `--practice-ground-id` | 拉全量索引落库，打印新增/更新/总数 |
| `flag-submit <id> <flag>` | challenge_id | `submit` 提交，打印结果；正确则 `persist_flag` |
| `challenge-target <start\|stop> <id>` | `--yes`(stop 需确认) | start：open API `environment/start` 轮询到就绪，打印 access_url 并写回 `challenges.target`；stop：`DELETE .../target/` 关容器 |
| `flags-import <rules.json>` | — | 导入 `--flag-rules` 格式 → `challenge_flags`(source=flag_rules)；sha256/regex 无法还原明文,跳过 |
| `cache-stats` | — | `{total_bytes, file_count, capacity_bytes}` |
| `cache-purge` | — | 清空缓存，打印释放字节 |

## 配置（`StoreSettings.from_env()`，env 优先 → `config_adaptor`）

适配器配置统一走 `config_adaptor.py`（env 优先 → `config_adaptor.json` → `CTF2_CONFIG_JSON`
外部文件兼容兜底）；与主 config(model_config) 分离，敏感项不进库。完整配置架构与
凭证获取截图见 [config.md](config.md)。

| 变量 | 默认 | 说明 |
|---|---|---|
| `CTF_STORE_DIR` | `./data` | 库 + 缓存根目录 |
| `CTF_ATTACHMENT_CACHE_BYTES` | `2*1024**3` | 附件缓存容量上限 |
| `CTF2_CONFIG_JSON` | — | 凭证 JSON 文件（如 `D:/pythonProject/ctf2/config.json`），env 与 config_adaptor.json 均未设时读其中同名键 |
| `CTF2_SESSION_TOKEN` | — | 网页登录态 JWT（Bearer，会话 API 鉴权；只走 env/配置文件，不进 DB） |
| `CTF2_TOKEN` | — | 旧别名，等价于 CTF2_SESSION_TOKEN |
| `CTF2_COOKIE` | — | 会话 cookie（token 优先） |
| `CTF2_ORIGIN` | `https://ctf2.dasctf.com` | 浏览器头 Origin/Referer（防平台风控验证码）；submit/detail 的 browser 头还带 Chrome UA + `Accept: application/json`（无浏览器特征会被风控弹验证码） |
| `CTF2_PRACTICE_GROUND_ID` | — | 靶场 id；缺省时详情/下载/提交/拉取/靶机不可用 |
| `CTF2_API_KEY` | — | 个人访问令牌 PAT（open API 鉴权，`Bearer <PAT>`）；**仅开靶机需要**（`environment/start`，需 `environment:write` 权限，平台 开发者→Open API 页签发） |
| `CTF2_BASE_URL` | `https://ctf2.dasctf.com/api/v1` | 会话 API base；config.json 的 `CTF2_SESSION_BASE` 优先于其 `CTF2_BASE_URL`（后者是 open base） |
| `CTF2_DOWNLOAD_URL_TEMPLATE` | — | 详情无 download_url 时的兜底 URL 模板 |
| `CTF2_SUBMIT_URL_TEMPLATE` | — | 覆盖默认提交 URL |
| `CTF2_LIST_PAGE_SIZE` | `100` | sync 分页大小（平台上限 100） |
| `CTF2_AUTO_START_TARGET` | `true` | 物化含容器题时自动开靶机（open API `environment/start`，host:port 写 metadata.yml `target`）；关闭后由 executor 惰性开靶（`_target()`） |

## 决策记录

- **SQLite 单文件**而非目录/对象存储：零依赖、事务、索引、friendly_id 唯一约束，足够测试复用。
- **适配器基类**而非服务化：换平台 = 新子类，主架构零改动；测试用 `FakeAdapter(ChallengeAdapter)` 证明平台无关。
- **LRU 淘汰到阈值**而非全清：`flags-import` 导入的索引永不删，只淘汰附件 blob。
- **copy 不硬链接**：agent 会 patch 附件二进制，硬链接污染缓存；Windows symlink 受限。
- **详情 download_url 是权威下载路径**：CDN 直链无需鉴权；401/403 立即抛 `AuthError` 不回退；详情 404/缺附件才回退自定义模板。
- **CDN 请求不带凭证头**：`ctf2-files.dasctf.com` 是第三方域名，绝不发送 Bearer/Cookie。
- **凭证只走 env / config_adaptor（JSON）**：不进 DB、不进错误信息、不写日志。

## 风险 / 待实测

- **submit 已实测**：错 flag 首次得 `INCORRECT_FLAG`；连续多次提交被风控弹 `SUBMISSION_RISK_CHALLENGE_REQUIRED`（验证码，base64 图不可程序解），adapter 判"未判定"并本地落日志，不重试（重试会加剧风控）。间隔后或换浏览器特征可恢复。
- **detail/list 已实测**：下载 md5 与 file_md5 一致；sync 已拉取 6133 题索引。分页 total 终止可靠。
- **container 题无静态 target，已实测动态开/关**：`challenge-target start <id>`（open API `environment/start` 轮询到就绪）返回 `{hash}.tcp-ctf2.dasctf.com:port` 并写回 `challenges.target`；`challenge-target stop <id> --yes` 关容器并清除。环境 1 小时过期自动回收。开靶需 `CTF2_API_KEY`（PAT，`environment:write` 权限）。
- **正确 flag 来源**：平台不发布答案；经 `flags-import` 或解题验证 `persist_flag` 写回。
