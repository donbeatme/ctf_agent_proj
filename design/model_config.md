# 模型配置与 Token API

实现：`model_config.py`、`agent/llm_api.py`。

> **配置拆分**：本文件只覆盖主配置（`model_config`，模型/引擎/评估器）。平台适配器与沙箱的
> **敏感配置已拆出**，与主 config 分开、各配对其子类实现：
> - `config_adaptor.py` + `config_adaptor.json`（配对 `Ctf2Adapter`→`StoreSettings`）：
>   `CTF2_SESSION_TOKEN`/`CTF2_API_KEY`/`CTF2_COOKIE`/URL 等。
> - `config_sandbox.py` + `config_sandbox.json`（配对 `SandboxManager`→`SandboxSettings`）：
>   `CTF_SSH_HOST`/`CTF_SSH_USER`/`CTF_SSH_PASSWORD` 等。
>
> 三模块统一「env 优先 → 各自 JSON 兜底」；`config_adaptor` 另保留 `CTF2_CONFIG_JSON` 外部
> 文件兼容兜底。`model_config.json` 不再承载 `CTF_SSH_*`/`CTF2_*`。

---

## 1. 配置（`model_config.json`）

```json
{
  "DEEPSEEK_API_KEY": "",
  "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
  "DEEPSEEK_MODEL": "deepseek-v4-flash",
  "engine": {
    "max_cycles": 100,
    "max_replans": 8,
    "max_stalls": 3,
    "max_deadlock_attempts": 3,
    "max_step_attempts": 3,
    "context_budget_tokens": {
      "planner": null,
      "evaluator_plan": null,
      "executor": null,
      "evaluator_step": null,
      "evaluator_task": null
    },
    "context_budget_ratio": 0.9,
    "max_json_len": 65536,
    "run_token_budget_tokens": null,
    "llm_rpm": 60,
    "llm_circuit_breaker_threshold": 5,
    "llm_circuit_breaker_recovery": 60,
    "llm_total_timeout_ms": 300000,
    "llm_stream": false,
    "llm_stream_include_usage": false,
    "run_timeout_ms": 600000,
    "phase_timeout_ms": {
      "planning": 120000,
      "plan_review": 60000,
      "executing": 180000,
      "step_eval": 60000,
      "reflecting": 120000
    }
  }
}
```

### model_config.py — 通用配置读写

```python
def get(name, default=None)    # 环境变量 > config 文件 > default
def require(name)              # get + 不存在抛 RuntimeError
def set(name, value)           # 写回文件（原子 tmp + replace）
def get_engine_config() -> dict  # 返回引擎配置字典，JSON "engine" 段覆盖 Python 默认值
```

### 引擎配置（`get_engine_config()`）

`_ENGINE_DEFAULTS` 定义 Python 侧的默认值，`model_config.json` 的 `"engine"` 段可覆盖。构造函数传参优先级最高。

优先级链：**构造函数参数 > JSON engine 段 > Python _ENGINE_DEFAULTS**

| 键 | 默认 | 说明 |
|---|---|---|
| `max_cycles` | 100 | 总调度次数兜底 |
| `max_replans` | 8 | 重规划次数上限 |
| `max_stalls` | 3 | DAG 签名连续无变化次数（振荡检测） |
| `max_deadlock_attempts` | 3 | 调度死锁连续解不开次数 |
| `max_step_attempts` | 3 | 单步最大重试次数（Step.max_attempts 默认值） |
| `context_budget_tokens` | None | 上下文 token 预算：None=按 ratio 自动算 / int=全局标量 / dict[role→int|None] 按角色（替代旧字符版 `context_budget`） |
| `context_budget_ratio` | 0.9 | context_budget_tokens=None 时，取 `(context_window - max_output)` 的占比 |
| `max_json_len` | 65536 | LLM 输出的 JSON 最大字符数（防超长 payload） |
| `run_token_budget_tokens` | None | run 级累计 LLM token 用量上限（None=不限），超限 → FAILED + TOKEN_BUDGET_EXCEEDED |
| `llm_rpm` | 60 | LLM 请求速率上限（每分钟，TokenBucket） |
| `llm_circuit_breaker_threshold` | 5 | 连续失败 N 次触发熔断 OPEN |
| `llm_circuit_breaker_recovery` | 60 | 熔断后 N 秒进入 HALF_OPEN 探测 |
| `llm_total_timeout_ms` | 300000 | 单次 LLM 调用总超时（含重试，毫秒） |
| `llm_stream` | False | 是否启用 LLM 流式响应 |
| `llm_stream_include_usage` | False | 流式时是否请求服务端返回 usage（仅 OpenAI 支持，DeepSeek 等需关闭） |
| `run_timeout_ms` | None | 单次 run 全局超时（毫秒，None=不限） |
| `phase_timeout_ms` | `{planning:120000, plan_review:60000, executing:180000, step_eval:60000, reflecting:120000}` | 各阶段超时（毫秒，值可为 None 不限时） |

---

## 2. 按角色选模型（`role_model`）

```python
def role_model(role: str | None = None) -> str
```

config key 映射：

| role | config key | 示例值 |
|---|---|---|
| `planner` | `LLM_MODEL_PLANNER` | `deepseek-v4-flash` |
| `evaluator_plan` | `LLM_MODEL_EP` | `qwen3-235b-a22b` |
| `evaluator_step` | `LLM_MODEL_EE` | `deepseek-v4-flash` |
| `evaluator_task` | `LLM_MODEL_ET` | `deepseek-v4-flash` |

回落链：`LLM_MODEL_<ROLE>` → `LLM_MODEL` → `DEEPSEEK_MODEL` → `"deepseek-v4-flash"`

- 不传 role 直接返回默认模型
- 调用方负责传 model 参数：`llm_api.chat(model=role_model("planner"), ...)`
- Planner 已在 `_default_llm()` 中接线；Evaluator 真实实现中同理

---

## 3. LLM 调用 API

### chat — 单轮对话

```python
def chat(prompt=None, system=None, *, messages=None, model=None, base_url=None,
         api_key=None, temperature=0.7, max_tokens=None, timeout=DEFAULT_TIMEOUT,
         max_retries=DEFAULT_MAX_RETRIES, retry_backoff=1.5, docs=None,
         max_docs_chars=DEFAULT_MAX_DOCS_CHARS, stream=None) -> str
```

两种输入：`prompt + system`（拼为 [system?, user]）或 `messages`（完整对话历史）。

`docs` 注入：拼入首个 system 消息的 "# 可用文档" 段，超预算按文档边界截断（不截断在文档中间）。

`stream=None` 时从 config `llm_stream` 读默认值。token 用量经 `pop_token_log()` 获取。

### chat_with_tools — 工具循环

```python
def chat_with_tools(prompt=None, system=None, *, messages=None, docs=None, tools=None,
                    max_tool_rounds=8, tool_exec=call_tool, model=None, base_url=None,
                    api_key=None, temperature=0.7, max_tokens=None, timeout=DEFAULT_TIMEOUT,
                    max_retries=DEFAULT_MAX_RETRIES, retry_backoff=1.5,
                    stream=None) -> ToolResult

@dataclass
class ToolResult:
    content: str            # 最终模型回复
    trace: list[dict]       # 每轮工具调用 [{name, arguments, result}]
    rounds: int             # 实际调用轮数
    total_usage: dict | None  # 累计 token 用量 {prompt_tokens, completion_tokens, total_tokens}
```

- tools 为空 → 退化为 `chat()`
- 单次工具异常（JSON 解析失败/执行抛错）转 `{"error": ...}` 喂回模型，不中断循环
- 超过 `max_tool_rounds` 未出纯文本 → `ToolLoopError`
- `total_usage` 聚合全部轮次的 token 用量，供 engine `_llm_wrap` 追踪 run 级累计

### 重试策略

`_should_retry(e)` — 可重试：`APIError` / `APIConnectionError` / `APITimeoutError` / `RequestException`。
不可重试：`AuthenticationError` / `BadRequestError` / `NotFoundError` / `PermissionDeniedError`。

指数退避 + jitter：`delay = backoff * 2^(attempt-1) + random.uniform(0, 1)`，默认 backoff=1.5s。
- 总耗时 budget `llm_total_timeout_ms` 内重试，超预算直接抛 `LLMError`
- 429 优先采用服务端 `Retry-After` 头（取较大值）
- 调用前过 TokenBucket 限速 + CircuitBreaker 熔断检查（见下）

### 限速与熔断

模块级单例（config 读取一次）：
- `_get_rate_limiter()` — `RateLimiter(rpm=llm_rpm)`，`acquire()` 阻塞至可发送
- `_get_circuit_breaker()` — `CircuitBreaker(threshold, recovery)`，三态 CLOSED→OPEN→HALF_OPEN；OPEN 期间 fast-fail 抛 `CircuitBreakerOpen`

### resolve_key

```python
def resolve_key()
```

按 `LLM_API_KEY` → `DEEPSEEK_API_KEY` 顺序取 key，**环境变量优先**，`config.json` 兜底（12-factor：密钥等敏感项走环境变量，文件只放非敏感配置）。

---

## 4. Token 计算 API

### count_tokens

```python
def count_tokens(text: str = "", *, model: str | None = None) -> int
```

优先 tiktoken（模型对应 encoding），加载失败回落到字符估算（CJK ~2 token/字，ASCII ~0.25 token/字）。

### count_message_tokens

```python
def count_message_tokens(messages: list[dict], *, model: str | None = None) -> int
```

按 OpenAI 计费公式：每条消息 3 token 基础 + content token + role token + name token（如有）+ 回复起始 3 token。多模态 content（`[{type:"text", text:...}]`）逐 part 统计。

### 模型信息

```python
def model_context_window(model=None) -> int   # 最大 context window
def model_max_output(model=None) -> int       # 最大输出 token
def model_info(model=None) -> dict            # {model, encoding, context_window, max_output}
```

---

## 5. 模型匹配表（`MODELS: dict[str, ModelSpec]`）

统一模型信息表，值为 `ModelSpec(encoding, context_window, max_output)`。模型名小写后按**最长前缀**匹配。

| 片段 | encoding | context_window | max_output |
|---|---|---|---|
| `deepseek-v4` / `deepseek-r1` / `deepseek` | `cl100k_base` | 131,072 | 32,768 |
| `deepseek-chat` / `deepseek-reasoner` / `deepseek-coder` | `cl100k_base` | 65,536 | 8,192 |
| `gpt-4.5` / `gpt-4o` | `o200k_base` | 131,072 | 16,384 |
| `gpt-4-turbo` | `cl100k_base` | 131,072 | 4,096 |
| `gpt-4-32k` | `cl100k_base` | 32,768 | 4,096 |
| `gpt-4` | `cl100k_base` | 8,192 | 4,096 |
| `gpt-3.5-turbo-16k` | `cl100k_base` | 16,384 | 4,096 |
| `gpt-3.5-turbo` | `cl100k_base` | 4,096 | 4,096 |
| `gpt-3` | `p50k_base` | 4,096 | 4,096 |
| `davinci` | `p50k_base` | 2,048 | 2,048 |
| `o1` / `o3` / `o4` | `o200k_base` | 200,000 | 100,000 |
| `claude-opus-4` | `cl100k_base` | 200,000 | 32,768 |
| `claude-sonnet-4` / `claude-haiku-4` / `claude-3.5` | `cl100k_base` | 200,000 | 16,384 |
| `gemini-2.5` | `cl100k_base` | 1,048,576 | 65,536 |
| `gemini-2.0` / `gemini-1.5` / `gemini` | `cl100k_base` | 1,048,576 | 8,192 |
| `qwen3` / `qwen2.5` / `qwen` / `glm` | `cl100k_base` | 131,072 | 8,192 |
| `llama-4` | `cl100k_base` | 131,072 | 16,384 |
| `llama-3.1` | `cl100k_base` | 131,072 | 4,096 |
| `llama-3` | `cl100k_base` | 8,192 | 4,096 |

未匹配模型返回默认 `ModelSpec`（`cl100k_base` / 131,072 / 4,096）。

---

## 6. 默认常量

| 常量 | 值 | 说明 |
|---|---|---|
| `DEFAULT_MODEL` | `deepseek-v4-flash` | 默认模型 |
| `DEFAULT_BASE_URL` | `https://api.deepseek.com` | 默认 API 地址 |
| `DEFAULT_MAX_RETRIES` | 3 | 重试次数 |
| `DEFAULT_TIMEOUT` | 60 | 请求超时(秒) |
| `DEFAULT_MAX_DOCS_CHARS` | 4000 | 文档注入字符上限 |
