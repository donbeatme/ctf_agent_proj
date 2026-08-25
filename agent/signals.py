"""引擎级事件总线:pub/sub 信号分发,ctx 组件与 log 都作为 subscriber 接入。

Engine 只 emit signal,不关心谁来订阅——CtxAssembler 处理生命周期(AgentComm 清空/Docs 释放),
Logger 打 trace.log,未来可随意加 subscriber。

事件源合一:emit 的同时把信号镜像进 opslog 的 canonical 流(domain=engine),引擎
信号流因此不再是独立来源;run.log 的 EngineLogger 仍是行为订阅者,只负责人类可读
投影。
"""

from agent.schema import Signal
from opslog import emit as _record


class SignalBus:
    """引擎事件总线。subscribe(obj) → obj.on_<signal>(**kw) 自动分发。"""

    def __init__(self):
        self._subs: list[object] = []

    def subscribe(self, obj):
        """注册订阅者:订阅者需实现 on_<signal>(**kw) 方法,未实现则静默跳过。"""
        self._subs.append(obj)

    def emit(self, signal: Signal | str, **kw):
        """广播信号:先镜像进 canonical 流(ops.log),再逐订阅者 on_<signal> 分发。
        捕获异常防止一个订阅者炸掉其他订阅者。"""
        name = signal.value if isinstance(signal, Signal) else signal
        _record("engine", name, **kw)
        for s in self._subs:
            handler = getattr(s, f"on_{name}", None)
            if handler is not None:
                try:
                    handler(**kw)
                except Exception as exc:
                    # 一个订阅者异常不影响其他订阅者,但记录不静默
                    import logging
                    logging.warning(
                        f"SignalBus: subscriber {type(s).__name__}.on_{name} "
                        f"raised {type(exc).__name__}: {exc}"
                    )
