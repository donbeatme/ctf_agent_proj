"""统一操作日志(ops.log):adapter/sandbox 等外围组件事件,落盘 + 可转发。

分工:run.log(EngineLogger,人类可读)与 workspace.events.jsonl(agent 决策链)归主
循环;ops.log 是外围组件(平台适配器/沙箱/工具依赖/引擎运行)的操作审计线,一次
一行 JSON,不随 run 生命周期消失,解完题仍可复查。引擎 run 期间经 attach() 把
事件转进 workspace.events.jsonl 与 run.log,形成统一审计链。

用法:
    from opslog import emit
    emit("adapter", "submit", challenge_id=..., verdict=...)

写入路径:默认 ./data/ops.log,CTF_OPS_LOG 可覆盖(env 优先,model_config.json 兜底)。绝不抛异常。
"""

from __future__ import annotations

import json
import os
import threading
import time
from enum import Enum
from pathlib import Path

from model_config import get as _cfg

_log_path = Path(_cfg("CTF_OPS_LOG") or "data/ops.log")
_lock = threading.Lock()
_sinks: list = []
_MAX_FIELD = 500


class ErrorLevel(str, Enum):
    """错误处理策略(severity 是策略,不是错误本质)。

    FATAL      阻断 run 的硬失败(能力缺失/不可恢复,引擎 gate 收口);
    RECOVERABLE 记录后继续(瞬态/非阻断,后续按退避重试);
    CLEANUP    收尾阶段失败(不阻断 run,但需可复查,如关靶机)。
    """

    FATAL = "fatal"
    RECOVERABLE = "recoverable"
    CLEANUP = "cleanup"


def set_log_path(path: str | os.PathLike) -> None:
    """覆盖日志文件路径(测试/定制用)。"""
    global _log_path
    _log_path = Path(path)


def attach(sink) -> None:
    """注册转发回调 sink(kind, detail):引擎 run 期间把 ops 事件转进统一审计链。"""
    if sink not in _sinks:
        _sinks.append(sink)


def detach(sink) -> None:
    if sink in _sinks:
        _sinks.remove(sink)


def reset() -> None:
    """清空转发器(测试隔离)。"""
    _sinks.clear()


def emit(domain: str, event: str, **fields) -> None:
    """写一条 ops 事件:JSONL 落盘 + 转发到已注册 sinks。绝不抛异常。"""
    record = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "domain": domain,
        "event": event,
    }
    for k, v in fields.items():
        record[k] = _scalar(v)
    line = json.dumps(record, ensure_ascii=False)
    with _lock:
        try:
            _log_path.parent.mkdir(parents=True, exist_ok=True)
            with _log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass
    for s in _sinks:
        try:
            s(kind=f"{domain}.{event}", detail=record)
        except Exception:
            pass


def record_error(domain: str, subject: str, exc: BaseException | None = None,
                 level: ErrorLevel = ErrorLevel.RECOVERABLE, **fields) -> None:
    """记录一条错误事件(统一审计线):失败必须进事件,不能静默吞掉。

    level 表达处理策略(FATAL 阻断 run / RECOVERABLE 记录后继续 / CLEANUP 收尾告警),
    错误本质由 domain/subject + error 串表达。与 emit 一致绝不抛异常。
    """
    err = f"{type(exc).__name__}: {exc}" if exc is not None else ""
    emit(domain, f"{subject}_failed", level=level.value, error=err, **fields)


def _scalar(v):
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    if isinstance(v, str):
        return v[:_MAX_FIELD] if len(v) > _MAX_FIELD else v
    if isinstance(v, (dict, list)):
        s = json.dumps(v, ensure_ascii=False, default=str)
        return s[:_MAX_FIELD] if len(s) > _MAX_FIELD else s
    return v
