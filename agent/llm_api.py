"""通用 LLM 调用 API(网关)。

- 兼容任意 OpenAI 兼容接口:base_url + model 走 URL 模式,支持按调用覆盖
- key 环境变量优先,config.json 兜底(LLM_API_KEY / DEEPSEEK_API_KEY)
- 指数退避+jitter 重试,含总超时 budget;重试耗尽抛 LLMError
- Token bucket 速率限制 + circuit breaker 熔断保护
- Client 单例复用连接池
- 文档注入:`docs=` 拼进系统消息,超预算按文档边界截断
- 工具循环:`chat_with_tools` 走 function calling,循环执行工具直到模型不再要工具
- token 计算:根据模型动态选择 tokenizer,无匹配时回落字符估算
- token 用量追踪:每次调用记录 prompt/completion tokens
"""

import json
import random
import re
import time
from dataclasses import dataclass, field
from functools import lru_cache

import openai
import requests

from model_config import get, require, get_engine_config
from agent.tools import call_tool
from opslog import get_run_context, set_run_context

def current_base_url():
    """每次调用现读配置,前端改 model_config 后无需重启进程。"""
    return get("LLM_BASE_URL", get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))


def current_model():
    return get("LLM_MODEL", get("DEEPSEEK_MODEL", "deepseek-v4-flash"))


DEFAULT_BASE_URL = current_base_url()
DEFAULT_MODEL = current_model()
DEFAULT_MAX_RETRIES = int(get("LLM_MAX_RETRIES", 3))
DEFAULT_TIMEOUT = float(get("LLM_TIMEOUT", 60))
DEFAULT_MAX_DOCS_CHARS = int(get("LLM_MAX_DOCS_CHARS", 4000))


def _llm_config() -> dict:
    try:
        return get_engine_config()
    except Exception:
        return {}


# ═══════════════════════════════════════════════════════════
# Client 单例复用连接池(item 2)
# ═══════════════════════════════════════════════════════════

_client: openai.OpenAI | None = None
_client_key: tuple | None = None


def _get_client(api_key, base_url, timeout):
    global _client, _client_key
    key = (api_key, base_url, timeout)
    if _client is None or _client_key != key:
        _client = openai.OpenAI(api_key=api_key, base_url=base_url,
                                timeout=timeout, max_retries=0)
        _client_key = key
    return _client


# ═══════════════════════════════════════════════════════════
# Rate Limiter: token bucket(item 3)
# ═══════════════════════════════════════════════════════════

class RateLimiter:
    """Token bucket 速率限制器。rpm=0 表示不限速(不阻塞)。"""

    def __init__(self, rpm: int = 60):
        self._rate = rpm / 60.0 if rpm > 0 else 0.0
        self._capacity = max(1.0, rpm / 10.0) if rpm > 0 else float("inf")
        self._tokens = self._capacity
        self._last = time.monotonic()

    def acquire(self) -> float:
        """阻塞直到获取一个 token,返回等待秒数(0 = 无需等待/rpm=0 不限速)。"""
        if self._rate == 0.0:
            return 0.0
        now = time.monotonic()
        elapsed = now - self._last
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return 0.0
        wait = (1.0 - self._tokens) / self._rate
        time.sleep(wait)
        self._tokens = 0.0
        self._last = time.monotonic()
        return wait


# 模块级单例(config key: llm_rpm)
_rate_limiter: RateLimiter | None = None


def _get_rate_limiter() -> RateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        cfg = _llm_config()
        rpm = cfg.get("llm_rpm", 60)
        _rate_limiter = RateLimiter(rpm=rpm)
    return _rate_limiter


# ═══════════════════════════════════════════════════════════
# Circuit Breaker(item 4)
# ═══════════════════════════════════════════════════════════

class CircuitBreakerOpen(RuntimeError):
    pass


class CircuitBreaker:
    """三态熔断器:CLOSED(正常) → OPEN(fast-fail) → HALF_OPEN(探测)。"""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self, threshold: int = 5, recovery: float = 60.0):
        self._threshold = threshold
        self._recovery = recovery
        self._state = self.CLOSED
        self._failures = 0
        self._opened_at = 0.0

    def before_call(self):
        if self._state == self.CLOSED:
            return
        if self._state == self.OPEN:
            if time.monotonic() - self._opened_at >= self._recovery:
                self._state = self.HALF_OPEN
            else:
                raise CircuitBreakerOpen(
                    f"熔断器 OPEN(已熔断 {(time.monotonic() - self._opened_at):.0f}s,"
                    f" {self._recovery}s 后探测恢复)")
        # HALF_OPEN: 放行一个探测请求

    def on_success(self):
        if self._state == self.HALF_OPEN:
            self._state = self.CLOSED
        self._failures = 0

    def on_failure(self):
        self._failures += 1
        if self._state == self.HALF_OPEN:
            self._state = self.OPEN
            self._opened_at = time.monotonic()
        elif self._failures >= self._threshold:
            self._state = self.OPEN
            self._opened_at = time.monotonic()


# 模块级单例(config keys: llm_circuit_breaker_threshold / llm_circuit_breaker_recovery)
_circuit_breaker: CircuitBreaker | None = None


def _get_circuit_breaker() -> CircuitBreaker:
    global _circuit_breaker
    if _circuit_breaker is None:
        cfg = _llm_config()
        threshold = cfg.get("llm_circuit_breaker_threshold", 5)
        recovery = cfg.get("llm_circuit_breaker_recovery", 60)
        _circuit_breaker = CircuitBreaker(threshold=threshold, recovery=recovery)
    return _circuit_breaker

# ═══════════════════════════════════════════════════════════
# 模型信息表:模型名片段 → ModelSpec(encoding, context_window, max_output)
# ═══════════════════════════════════════════════════════════


@dataclass
class ModelSpec:
    encoding: str = "cl100k_base"
    context_window: int = 131_072
    max_output: int = 4_096


MODELS: dict[str, ModelSpec] = {
    # DeepSeek 系列
    "deepseek-v4": ModelSpec(encoding="cl100k_base", context_window=131_072, max_output=32_768),
    "deepseek-r1": ModelSpec(encoding="cl100k_base", context_window=131_072, max_output=32_768),
    "deepseek-chat": ModelSpec(encoding="cl100k_base", context_window=65_536, max_output=8_192),
    "deepseek-reasoner": ModelSpec(encoding="cl100k_base", context_window=65_536, max_output=8_192),
    "deepseek-coder": ModelSpec(encoding="cl100k_base", context_window=65_536, max_output=8_192),
    "deepseek": ModelSpec(encoding="cl100k_base", context_window=131_072, max_output=32_768),
    # OpenAI 系列
    "gpt-4.5": ModelSpec(encoding="o200k_base", context_window=131_072, max_output=16_384),
    "gpt-4o": ModelSpec(encoding="o200k_base", context_window=131_072, max_output=16_384),
    "gpt-4-turbo": ModelSpec(encoding="cl100k_base", context_window=131_072, max_output=4_096),
    "gpt-4-32k": ModelSpec(encoding="cl100k_base", context_window=32_768, max_output=4_096),
    "gpt-4": ModelSpec(encoding="cl100k_base", context_window=8_192, max_output=4_096),
    "gpt-3.5-turbo-16k": ModelSpec(encoding="cl100k_base", context_window=16_384, max_output=4_096),
    "gpt-3.5-turbo": ModelSpec(encoding="cl100k_base", context_window=4_096, max_output=4_096),
    "gpt-3": ModelSpec(encoding="p50k_base", context_window=4_096, max_output=4_096),
    "davinci": ModelSpec(encoding="p50k_base", context_window=2_048, max_output=2_048),
    "o1": ModelSpec(encoding="o200k_base", context_window=200_000, max_output=100_000),
    "o3": ModelSpec(encoding="o200k_base", context_window=200_000, max_output=100_000),
    "o4": ModelSpec(encoding="o200k_base", context_window=200_000, max_output=100_000),
    "text-embedding": ModelSpec(encoding="cl100k_base", context_window=8_192, max_output=8_192),
    # Anthropic (无官方 tiktoken,用 cl100k 近似)
    "claude-opus-4": ModelSpec(encoding="cl100k_base", context_window=200_000, max_output=32_768),
    "claude-sonnet-4": ModelSpec(encoding="cl100k_base", context_window=200_000, max_output=16_384),
    "claude-haiku-4": ModelSpec(encoding="cl100k_base", context_window=200_000, max_output=16_384),
    "claude-3.5": ModelSpec(encoding="cl100k_base", context_window=200_000, max_output=16_384),
    "claude-3": ModelSpec(encoding="cl100k_base", context_window=200_000, max_output=4_096),
    "claude": ModelSpec(encoding="cl100k_base", context_window=200_000, max_output=16_384),
    # Google (同上)
    "gemini-2.5": ModelSpec(encoding="cl100k_base", context_window=1_048_576, max_output=65_536),
    "gemini-2.0": ModelSpec(encoding="cl100k_base", context_window=1_048_576, max_output=8_192),
    "gemini-1.5": ModelSpec(encoding="cl100k_base", context_window=1_048_576, max_output=8_192),
    "gemini": ModelSpec(encoding="cl100k_base", context_window=1_048_576, max_output=8_192),
    # 国产模型
    "qwen3": ModelSpec(encoding="cl100k_base", context_window=131_072, max_output=8_192),
    "qwen2.5": ModelSpec(encoding="cl100k_base", context_window=131_072, max_output=8_192),
    "qwen": ModelSpec(encoding="cl100k_base", context_window=131_072, max_output=8_192),
    "glm": ModelSpec(encoding="cl100k_base", context_window=131_072, max_output=8_192),
    "llama-4": ModelSpec(encoding="cl100k_base", context_window=131_072, max_output=16_384),
    "llama-3.1": ModelSpec(encoding="cl100k_base", context_window=131_072, max_output=4_096),
    "llama-3": ModelSpec(encoding="cl100k_base", context_window=8_192, max_output=4_096),
    "llama": ModelSpec(encoding="cl100k_base", context_window=131_072, max_output=16_384),
}

# 按 key 长度降序(最长前缀优先匹配)
_MODEL_KEYS_BY_LEN = sorted(MODELS, key=len, reverse=True)

# 默认规格(未匹配到模型时)
_DEFAULT_SPEC = ModelSpec()


def _match_model(model: str) -> ModelSpec:
    """模型名小写后按最长前缀匹配 MODELS;无匹配返回默认规格。"""
    m = (model or current_model()).lower().strip()
    for key in _MODEL_KEYS_BY_LEN:
        if key in m:
            return MODELS[key]
    return _DEFAULT_SPEC


def _get_encoding_name(model: str | None = None) -> str:
    """模型 → tiktoken encoding 名。"""
    return _match_model(model).encoding


@lru_cache(maxsize=4)
def _load_tokenizer(encoding_name: str):
    """惰性加载 tiktoken encoding;加载失败返回 None(走字符估算)。"""
    try:
        import tiktoken
        return tiktoken.get_encoding(encoding_name)
    except Exception:
        return None


# 消息角色 token 开销(OpenAI 协议固定开销)
_TOKENS_PER_MESSAGE = 3
_TOKENS_PER_NAME = 1  # 如果 name 字段存在
# 每条消息的额外开销(不同模型略有差异,这里是近似值)
_MESSAGE_OVERHEAD = {
    "gpt-4o": 3,
    "gpt-4.5": 3,
    "o1": 3,
    "o3": 3,
    "o4": 3,
    "cl100k_base": 3,
    "p50k_base": 3,
    "o200k_base": 3,
}

# CJK 字符权重(字符估算时:CJK 约占 2 token,其他约 0.25 token)
_CJK_RE = re.compile(r"[一-鿿㐀-䶿豈-﫿　-〿＀-￯]")


def _estimate_tokens(text: str) -> int:
    """字符级 token 估算:CJK 2 token/字,英文 0.25 token/字,取整。"""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return max(1, int(cjk * 2 + other * 0.25 + 0.5))


def count_tokens(text: str = "", *, model: str | None = None) -> int:
    """计算文本 token 数:优先用 tiktoken(模型对应 encoding),加载失败回落字符估算。

    >>> count_tokens("hello world")
    2
    >>> count_tokens("你好世界")
    4
    """
    if not text:
        return 0
    enc_name = _get_encoding_name(model)
    tok = _load_tokenizer(enc_name)
    if tok is not None:
        return len(tok.encode(text))
    return _estimate_tokens(text)


def count_message_tokens(messages: list[dict], *, model: str | None = None) -> int:
    """计算 OpenAI 格式 messages 的 token 数(含角色/格式开销)。

    messages 中每条消息 {"role":..., "content":...} 按 OpenAI 计费公式:
      tokens = 每条消息 3 token 基础 + content token + name token(如有)
    """
    if not messages:
        return 0
    model = model or current_model()
    enc_name = _get_encoding_name(model)
    tok = _load_tokenizer(enc_name)
    overhead = _MESSAGE_OVERHEAD.get(enc_name, 3)

    total = 0
    for msg in messages:
        total += overhead
        content = msg.get("content", "")
        if isinstance(content, str):
            total += count_tokens(content, model=model) if tok is None else len(tok.encode(content))
        elif isinstance(content, list):
            # 多模态 content([{"type":"text","text":"..."}, ...])
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    t = part.get("text", "")
                    total += count_tokens(t, model=model) if tok is None else len(tok.encode(t))
        role = msg.get("role", "")
        total += count_tokens(role, model=model) if tok is None else len(tok.encode(role))
        if msg.get("name"):
            total += _TOKENS_PER_NAME
            name = msg["name"]
            total += count_tokens(name, model=model) if tok is None else len(tok.encode(name))
    # 每条回复以 <|start|> 开头(3 token)
    total += 3
    return total


def model_context_window(model: str | None = None) -> int:
    """返回模型的最大 context window(token 数)。未匹配到则返回默认 131072。"""
    return _match_model(model).context_window


def model_max_output(model: str | None = None) -> int:
    """返回模型的最大输出 token 数。未匹配到则返回默认 4096。"""
    return _match_model(model).max_output


def model_info(model: str | None = None) -> dict:
    """返回模型的 token 相关信息汇总。"""
    spec = _match_model(model)
    return {
        "model": model or current_model(),
        "encoding": spec.encoding,
        "context_window": spec.context_window,
        "max_output": spec.max_output,
    }


# 角色 → config key 后缀
_ROLE_MODEL_KEYS: dict[str, str] = {
    "planner": "LLM_MODEL_PLANNER",
    "executor": "LLM_MODEL_EXECUTOR",
    "evaluator_plan": "LLM_MODEL_EP",
    "evaluator_step": "LLM_MODEL_EE",
    "evaluator_task": "LLM_MODEL_ET",
}


def role_model(role: str | None = None) -> str:
    """按角色返回对应的模型名。未配置则回落到 LLM_MODEL → DEEPSEEK_MODEL → 默认。

    config.json 示例:
      "LLM_MODEL": "deepseek-v4-flash",
      "LLM_MODEL_PLANNER": "deepseek-v4-flash",
      "LLM_MODEL_EP": "qwen3-235b-a22b"
    """
    if role:
        key = _ROLE_MODEL_KEYS.get(role)
        if key:
            val = get(key)
            if val:
                return val
    return current_model()


class LLMError(RuntimeError):
    pass


class ToolLoopError(RuntimeError):
    """工具循环超上限。携带已达成的部分工具轨迹,供上层保留日志/提取 flag。"""

    def __init__(self, message: str, trace: list[dict] | None = None):
        super().__init__(message)
        self.trace = trace or []


@dataclass
class ToolResult:
    content: str                              # 最终模型回复
    trace: list[dict] = field(default_factory=list)   # 每轮工具调用记录
    rounds: int = 0                           # 实际调用轮数(含首轮非工具回复)
    total_usage: dict | None = None           # 累计 token 用量 {prompt_tokens, completion_tokens, total_tokens}


def resolve_key():
    """从 config.json 或环境变量取 key,兼容通用与 DeepSeek 两套命名。"""
    for name in ("LLM_API_KEY", "DEEPSEEK_API_KEY"):
        try:
            return require(name)
        except RuntimeError:
            continue
    raise LLMError("未设置 LLM_API_KEY 或 DEEPSEEK_API_KEY(环境变量或 config.json)")


def _should_retry(e):
    # 配置类错误重试无意义,直接失败
    if isinstance(e, (openai.AuthenticationError, openai.BadRequestError,
                      openai.NotFoundError, openai.PermissionDeniedError)):
        return False
    return isinstance(e, (openai.APIError, openai.APIConnectionError,
                          openai.APITimeoutError, requests.exceptions.RequestException))


# ═══════════════════════════════════════════════════════════
# Token 使用量追踪(item 1.7)
# ═══════════════════════════════════════════════════════════

_token_log: list[dict] = []


def _record_usage(model: str, usage) -> None:
    if usage is not None:
        _token_log.append({
            "model": model,
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
        })


def pop_token_log() -> list[dict]:
    """取出并清空 token 使用量日志。"""
    u = list(_token_log)
    _token_log.clear()
    return u


def _build_messages(prompt, system, messages):
    """归一化输入:给 messages 原样返回;否则拼 [system?, user]。"""
    if messages is not None:
        return list(messages)
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    if prompt is not None:
        msgs.append({"role": "user", "content": prompt})
    return msgs


def _with_docs(messages, docs, budget=DEFAULT_MAX_DOCS_CHARS):
    """文档注入:拼成 "# 可用文档" 段,并入首个 system 消息;超预算按文档边界截断。"""
    if not docs:
        return messages
    docs = [d.strip() for d in docs if d and d.strip()]
    if not docs:
        return messages
    text = "# 可用文档\n\n" + "\n\n".join(docs)
    if len(text) > budget:
        text = text[:budget]
        cut = text.rfind("\n\n")
        if cut > 0:
            text = text[:cut]
    msgs = list(messages)
    if msgs and msgs[0].get("role") == "system":
        first = dict(msgs[0])
        first["content"] = (first.get("content") or "") + "\n\n" + text
        msgs[0] = first
    else:
        msgs.insert(0, {"role": "system", "content": text})
    return msgs


def _request(client, messages, *, model, temperature, max_tokens=None, tools=None,
             max_retries=DEFAULT_MAX_RETRIES, retry_backoff=1.5, total_timeout=None,
             stream=False):
    """带指数退避+jitter重试的请求,含速率限制+熔断+token记录。

    返回 (response, usage_dict)。重试耗尽抛 LLMError。
    stream=True 时走流式累积,不支持重试(流式失败直接抛异常)。
    """
    # 速率限制
    _get_rate_limiter().acquire()
    # 熔断检查:熔断开启属调用侧失败,统一包装成 LLMError(调用方无需感知 CircuitBreakerOpen)
    try:
        _get_circuit_breaker().before_call()
    except Exception as e:  # noqa: BLE001
        raise LLMError(f"LLM 熔断,拒绝调用: {e}") from e

    if total_timeout is None:
        cfg = _llm_config()
        total_timeout = cfg.get("llm_total_timeout_ms", 300_000) / 1000  # ms→秒

    if stream:
        return _stream_request(client, messages, model=model, temperature=temperature,
                               max_tokens=max_tokens, tools=tools,
                               total_timeout=total_timeout)

    kwargs = {"model": model, "messages": messages, "temperature": temperature}
    kwargs["max_tokens"] = max_tokens or model_max_output(model)    # item 7: 默认输出cap
    if tools:
        kwargs["tools"] = tools

    last_err = None
    attempt = 0
    started = time.monotonic()
    for attempt in range(1, max_retries + 1):
        try:
            resp = client.chat.completions.create(**kwargs)
            _record_usage(model, resp.usage)       # item 1.7: token追踪
            _get_circuit_breaker().on_success()
            return resp
        except Exception as e:  # noqa: BLE001
            last_err = e
            _get_circuit_breaker().on_failure()
            if attempt >= max_retries or not _should_retry(e):
                break
            # item 5: jitter + 总耗时budget
            elapsed = time.monotonic() - started
            if elapsed >= total_timeout:
                break
            delay = retry_backoff * (2 ** (attempt - 1)) + random.uniform(0, 1)
            if elapsed + delay > total_timeout:
                delay = max(0, total_timeout - elapsed)
            if delay > 0:
                # 429 时优先用服务端 Retry-After 头(item 3)
                if hasattr(e, 'response') and e.response is not None:
                    ra = e.response.headers.get("Retry-After")
                    if ra is not None:
                        try:
                            delay = max(delay, float(ra))
                        except ValueError:
                            pass
                time.sleep(delay)
    raise LLMError(f"LLM 调用失败(尝试 {attempt} 次后): {last_err}") from last_err


def _stream_request(client, messages, *, model, temperature, max_tokens=None, tools=None,
                     total_timeout=None):
    """流式调用 LLM,累积 chunks 返回合成 response(兼容非流式 response 结构)。

    流式不支持重试(已通过 _request 的速率限制/熔断检查)。
    total_timeout 逐 chunk 检查,超时抛 LLMError。
    usage: config llm_stream_include_usage 为 True 时走 stream_options;
           否则按 token 估算(跨所有 provider 安全)。
    """
    from types import SimpleNamespace

    cfg = _llm_config()
    include_usage = cfg.get("llm_stream_include_usage", False)

    kwargs = {
        "model": model, "messages": messages, "temperature": temperature,
        "stream": True,
    }
    if include_usage:
        kwargs["stream_options"] = {"include_usage": True}
    kwargs["max_tokens"] = max_tokens or model_max_output(model)
    if tools:
        kwargs["tools"] = tools

    content_parts = []
    tool_calls: dict[int, dict] = {}  # index → {id, function_name, arguments}
    usage = None

    started = time.monotonic()
    try:
        for chunk in client.chat.completions.create(**kwargs):
            if total_timeout is not None:
                elapsed = time.monotonic() - started
                if elapsed >= total_timeout:
                    _get_circuit_breaker().on_failure()
                    raise LLMError(f"流式 LLM 调用超时({total_timeout:.0f}s)")
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta is None:
                continue
            if delta.content:
                content_parts.append(delta.content)
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_calls:
                        tool_calls[idx] = {"id": "", "function_name": "", "arguments": ""}
                    entry = tool_calls[idx]
                    if tc_delta.id:
                        entry["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            entry["function_name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            entry["arguments"] += tc_delta.function.arguments
            if hasattr(chunk, 'usage') and chunk.usage:
                usage = chunk.usage
    except LLMError:
        raise
    except Exception as e:  # noqa: BLE001
        _get_circuit_breaker().on_failure()
        raise LLMError(f"流式 LLM 调用失败: {e}") from e

    _get_circuit_breaker().on_success()

    content = "".join(content_parts)
    if usage is None:
        # 估算:prompt → count_message_tokens, completion → count_tokens(content)
        prompt_est = count_message_tokens(messages, model=model)
        completion_est = count_tokens(content, model=model) if content else 0
        usage = SimpleNamespace(
            prompt_tokens=prompt_est,
            completion_tokens=completion_est,
            total_tokens=prompt_est + completion_est,
        )
    _record_usage(model, usage)

    tc_objs = []
    for idx in sorted(tool_calls):
        tc = tool_calls[idx]
        tc_objs.append(SimpleNamespace(
            id=tc["id"],
            type="function",
            function=SimpleNamespace(name=tc["function_name"], arguments=tc["arguments"]),
        ))

    message = SimpleNamespace(content=content, tool_calls=tc_objs or None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def chat(prompt=None, system=None, *, messages=None, model=None, base_url=None, api_key=None,
         temperature=0.7, max_tokens=None, timeout=DEFAULT_TIMEOUT,
         max_retries=DEFAULT_MAX_RETRIES, retry_backoff=1.5,
         docs=None, max_docs_chars=DEFAULT_MAX_DOCS_CHARS, stream=None):
    """调用 LLM,返回模型回复文本。token 用量通过 pop_token_log() 获取。

    两种输入方式:
    - prompt + system:单轮,内部拼成 [system?, user]
    - messages:直接给完整对话历史(如工作记忆渲染出的),prompt/system 忽略
    stream=None 时从 config llm_stream 读取默认值。
    """
    if stream is None:
        stream = _llm_config().get("llm_stream", False)
    model = model or current_model()
    base_url = base_url or current_base_url()
    api_key = api_key or resolve_key()
    max_retries = max(1, max_retries)

    client = _get_client(api_key, base_url, timeout)
    msgs = _with_docs(_build_messages(prompt, system, messages), docs, max_docs_chars)
    resp = _request(client, msgs, model=model, temperature=temperature,
                    max_tokens=max_tokens, max_retries=max_retries,
                    retry_backoff=retry_backoff, stream=stream)
    return resp.choices[0].message.content or ""


def make_compress(model=None, max_tokens=1024, temperature=0.2,
                  fallback_chars=8000):
    """构造上下文压缩回调 compress(prompt, content) -> str,供 CtxAssembler 溢出压缩用。

    prompt 是组装器算好的压缩提示词,content 是待压内容;返回压缩后文本。
    LLM 调用失败时兜底截断 content(不抛异常)——assembler 溢出路径本来就会
    在异常时走机械降级,但 TraceComponent._fold 等同步路径需要回调永不炸。
    """
    _system = (
        "你是上下文压缩器。输入为 [压缩提示词] 与 [待压内容] 两部分。"
        "严格按提示词要求压缩:保留决策必需的关键事实、数字与结论,删除冗余表述;"
        "只输出压缩结果本身,不要任何解释或前缀。"
    )

    def compress(prompt: str, content: str) -> str:
        try:
            return chat(
                system=_system,
                prompt=f"{prompt}\n\n# 待压内容\n{content}",
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception:
            return content if len(content) <= fallback_chars else content[:fallback_chars] + "…(压缩失败,截断)"

    return compress


def _assistant_message(msg):
    return {
        "role": "assistant",
        "content": msg.content,
        "tool_calls": [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in msg.tool_calls
        ],
    }


def _run_tool(tool_exec, name, arguments):
    """执行一个工具调用:坏 JSON 参数与执行异常都转 {"error": ...} 喂回模型。"""
    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        args = {}
    try:
        return tool_exec(name, args)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def chat_with_tools(prompt=None, system=None, *, messages=None, docs=None, tools=None,
                    max_tool_rounds=None, tool_exec=call_tool, model=None, base_url=None,
                    api_key=None, temperature=0.7, max_tokens=None, timeout=DEFAULT_TIMEOUT,
                    max_retries=DEFAULT_MAX_RETRIES, retry_backoff=1.5,
                    stream=None) -> ToolResult:
    """工具调用循环:模型要工具就执行并把结果喂回,直到给纯文本回复。

    tools 为空时退化为 chat(plain)。工具执行经 tool_exec(name, arguments)
    注入(默认为 agent.tools.call_tool),单次工具异常不会中断循环。
    超过 max_tool_rounds 仍未给出纯文本回复 → 抛 ToolLoopError。

    total_usage 累计所有轮次的 token 用量。
    stream=None 时从 config llm_stream 读取默认值。
    """
    if stream is None:
        stream = _llm_config().get("llm_stream", False)

    if max_tool_rounds is None:
        from model_config import get_engine_config

        max_tool_rounds = get_engine_config().get("max_tool_rounds", 24)

    if not tools:
        content = chat(prompt=prompt, system=system, messages=messages, docs=docs,
                       model=model, base_url=base_url, api_key=api_key, temperature=temperature,
                       max_tokens=max_tokens, timeout=timeout, max_retries=max_retries,
                       retry_backoff=retry_backoff, stream=stream)
        usage_log = pop_token_log()
        usage = _sum_usage(usage_log) if usage_log else None
        return ToolResult(content=content, trace=[], rounds=0, total_usage=usage)

    model = model or current_model()
    base_url = base_url or current_base_url()
    api_key = api_key or resolve_key()
    max_retries = max(1, max_retries)

    client = _get_client(api_key, base_url, timeout)
    msgs = _with_docs(_build_messages(prompt, system, messages), docs)
    trace = []

    prev = get_run_context()
    try:
        for rnd in range(1, max_tool_rounds + 1):
            set_run_context(round=rnd)  # 本轮内所有 opslog 事件自动带 round 定位
            resp = _request(client, msgs, model=model, temperature=temperature,
                            max_tokens=max_tokens, tools=tools,
                            max_retries=max_retries, retry_backoff=retry_backoff,
                            stream=stream)
            msg = resp.choices[0].message
            if not msg.tool_calls:
                usage_log = pop_token_log()
                return ToolResult(
                    content=msg.content or "", trace=trace, rounds=rnd,
                    total_usage=_sum_usage(usage_log) if usage_log else None,
                )
            msgs.append(_assistant_message(msg))
            for tc in msg.tool_calls:
                name = tc.function.name
                result = _run_tool(tool_exec, name, tc.function.arguments)
                trace.append({"name": name, "arguments": tc.function.arguments,
                              "result": result, "round": rnd})
                msgs.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })
    finally:
        set_run_context(round=prev.get("round"))  # 恢复调用方 round(如步骤 attempt)
    raise ToolLoopError(f"工具循环超过上限 {max_tool_rounds} 轮", trace=trace)


def _sum_usage(logs: list[dict]) -> dict:
    """聚合多条 token 记录为一条汇总。"""
    return {
        "prompt_tokens": sum(u["prompt_tokens"] for u in logs),
        "completion_tokens": sum(u["completion_tokens"] for u in logs),
        "total_tokens": sum(u["total_tokens"] for u in logs),
    }


def chat_result(prompt, **kwargs):
    """chat 的字典版:成功返回 content,失败返回 error,供工具/脚本直接使用。"""
    try:
        return {"success": True, "content": chat(prompt, **kwargs)}
    except LLMError as e:
        return {"success": False, "error": str(e)}
