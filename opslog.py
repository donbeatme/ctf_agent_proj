"""统一操作日志:单事件源。所有事件(外围组件 / 引擎信号 / agent 决策链)统一经
emit 写 canonical 流(ops.log),带全进程单调 seq 与 run_id/node_id/round 字段;
run.log(人类可读)与 workspace.events.jsonl(断点续跑账本)是它的投影,不再各自
独立成源。一次一行 JSON,不随 run 生命周期消失,解完题仍可复查。引擎 run 期间
经 attach() 把事件投影进 workspace.events.jsonl 与 run.log,形成统一审计链。

事件编码三字段(用户要求 node_id + round 两个字段):
- seq     全进程单调序号,ops.log 追加顺序即事件顺序(事件源重放的索引)
- node_id DAG 步骤 id(引擎当前 step),未进入步骤时省略
- round   执行轮次(步骤 attempt 或步骤内工具调用轮),未进入执行轮时省略
node_id/round 通常经 set_run_context 以执行环境(随 task context)自动落到每条事件,也可在 emit
显式传参覆盖(显式 None 省略该字段)。

用法:
    from opslog import emit, set_run_context
    set_run_context(run_id="run-...", node_id="s1", round=1)
    emit("adapter", "submit", challenge_id=...)   # 自动带 run_id/node_id/round
    emit("engine", "llm_call_start", role="planner", round=3)  # 覆盖环境值

写入路径:默认 ./data/ops.log,CTF_OPS_LOG 可覆盖(env 优先,model_config.json 兜底)。绝不抛异常。
"""

from __future__ import annotations

import contextvars
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
_MAX_FIELD = 500          # 标量字段截断(短字段)
_MAX_DETAIL = 64 * 1024   # 结构化 payload(dict/list)截断(长 detail 保留足够保真)
_seq = 0                  # 全进程单调序号(单源有序流)
# run 作用域环境:run_id / node_id / round。
# 用 ContextVar 而非 threading.local:actor mode 下多个 actor 是同一线程上的并发
# async task,threading.local 被所有 task 共享会跨 actor 泄漏;ContextVar 随 task
# context 切换,按执行单元(step task)隔离。
_CTX: contextvars.ContextVar[dict | None] = contextvars.ContextVar("run_context", default=None)
_UNSET = object()         # 区分"未传(用环境)"与"显式 None(省略该字段)"


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


def set_run_context(*, run_id=_UNSET, node_id=_UNSET, round=_UNSET) -> None:
    """设置当前执行环境的 run 作用域环境:此后 emit 未显式指定的 run_id/node_id/round
    自动落到这些值。显式 None 清除对应字段;run_id 置 None 结束 run 作用域。
    引擎在 run 开始/步骤切换/执行轮切换时调用,使外围事件自动携带归属。
    """
    cur = dict(_CTX.get() or {})
    if run_id is not _UNSET:
        cur["run_id"] = run_id
    if node_id is not _UNSET:
        cur["node_id"] = node_id
    if round is not _UNSET:
        cur["round"] = round
    _CTX.set(cur)


def get_run_context() -> dict:
    """当前执行环境的 run 作用域环境(保存/恢复、测试断言用)。"""
    cur = _CTX.get() or {}
    return {
        "run_id": cur.get("run_id"),
        "node_id": cur.get("node_id"),
        "round": cur.get("round"),
    }


def reset() -> None:
    """清空转发器与序号、run 作用域环境(测试隔离)。"""
    global _seq
    _sinks.clear()
    _seq = 0
    _CTX.set({})


def emit(domain: str, event: str, *, run_id=_UNSET, node_id=_UNSET,
         round=_UNSET, **fields) -> None:
    """写一条事件:唯一 canonical 流(ops.log)追加带 seq 的 JSON 行 + 转发 sinks。

    run_id/node_id/round:显式传值覆盖线程环境;未传回落到环境;显式 None 省略该字段。
    绝不抛异常。
    """
    cur = _CTX.get() or {}
    if run_id is _UNSET:
        run_id = cur.get("run_id")
    if node_id is _UNSET:
        node_id = cur.get("node_id")
    if round is _UNSET:
        round = cur.get("round")
    record = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "domain": domain,
        "event": event,
    }
    if run_id is not None:
        record["run_id"] = _scalar(run_id)
    if node_id is not None:
        record["node_id"] = _scalar(node_id)
    if round is not None:
        record["round"] = round
    for k, v in fields.items():
        record[k] = _scalar(v)
    with _lock:
        global _seq
        _seq += 1
        record = {"seq": _seq, **record}
        line = json.dumps(record, ensure_ascii=False)
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
        return s[:_MAX_DETAIL] if len(s) > _MAX_DETAIL else s
    if v is None or isinstance(v, (bool, int, float)):
        return v
    s = str(v)  # 任意对象(异常/自定义类型)→ 字符串,保证 record 可 JSON 序列化
    return s[:_MAX_FIELD] if len(s) > _MAX_FIELD else s
