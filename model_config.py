import json
import os
from pathlib import Path

_CONFIG_FILE = Path(__file__).resolve().parent / "model_config.json"


def _load():
    if not _CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


_config = _load()


def get(name, default=None):
    value = _config.get(name)
    if value:
        return value
    return os.environ.get(name, default)


def set(name, value):
    _config[name] = value
    try:
        tmp = _CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(_config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(_CONFIG_FILE)
    except OSError:
        import logging
        logging.warning(f"model_config: 写入 {_CONFIG_FILE} 失败")


def require(name):
    value = get(name)
    if not value:
        raise RuntimeError(f"未设置 {name},请填写 model_config.json 或设置环境变量 {name}")
    return value


# ===== 引擎配置(可经 model_config.json 的 "engine" 段覆盖) =====

_ENGINE_DEFAULTS = {
    "max_cycles": 100,
    "max_replans": 8,
    "max_stalls": 3,
    "max_deadlock_attempts": 3,
    "max_step_attempts": 3,
    "context_budget_tokens": None,         # token 级上下文预算: None=自动 / int=全局 / dict={"role":int|None}
    "context_budget_ratio": 0.9,           # 自动计算时,(context_window - max_output) 的占比
    "max_json_len": 64 * 1024,
    "run_token_budget_tokens": None,       # run 级累计 LLM token 预算上限(None=不限)
    "llm_rpm": 60,                         # LLM 请求速率上限(每分钟)
    "llm_circuit_breaker_threshold": 5,    # 连续失败 N 次触发熔断
    "llm_circuit_breaker_recovery": 60,    # 熔断后 N 秒进入半开探测恢复
    "llm_total_timeout_ms": 300_000,         # 单次 LLM 调用总超时毫秒(含重试)
    "llm_stream": False,                     # 是否启用 LLM 流式响应
    "llm_stream_include_usage": False,        # 流式时是否请求服务端返回 usage(仅 OpenAI 支持,DeepSeek 等需关闭)
    # —— 引擎阶段超时(ms),None = 不限时 ——
    "run_timeout_ms": None,                # 单次 run 全局超时(毫秒)
    "phase_timeout_ms": {                  # 各阶段超时(毫秒)
        "planning": 120_000,
        "plan_review": 60_000,
        "executing": 180_000,
        "step_eval": 60_000,
        "reflecting": 120_000,
    },
}


def get_engine_config() -> dict:
    """返回引擎配置字典:JSON 文件 "engine" 段覆盖默认值。"""
    cfg = dict(_ENGINE_DEFAULTS)
    cfg.update(_config.get("engine", {}))
    return cfg
