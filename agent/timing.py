"""计时与超时检测:PhaseTimer(上下文管理器 + check 协作式查询)。

设计见 design/engine.md §10。不走 threading.Timer 中断——Python 线程中断不可靠。
上下文管理器在退出时自动算耗时并判断超时;长循环内部用 check() 主动查询,超时由
调用方自行决定退出/降级。
"""

import time


class PhaseTimer:
    """阶段计时器,带超时检测。

    用法:
        t = PhaseTimer("executing", deadline_ms=60_000)
        with t:
            result = do_work()
        if t.timed_out:
            handle_timeout(t)
    """

    def __init__(self, phase: str, deadline_ms: int | None = None):
        self.phase = phase
        self.deadline_ms = deadline_ms          # None = 不限时
        self.elapsed_ms: float = 0
        self.timed_out: bool = False
        self._t0: float = 0
        self._stopped: bool = False

    def __enter__(self) -> "PhaseTimer":
        self._t0 = time.perf_counter()
        self._stopped = False
        self.elapsed_ms = 0
        self.timed_out = False
        return self

    def __exit__(self, *exc) -> None:
        self._stop()

    def _stop(self) -> None:
        if self._stopped:
            return
        self.elapsed_ms = (time.perf_counter() - self._t0) * 1000
        self.timed_out = (
            self.deadline_ms is not None and self.elapsed_ms >= self.deadline_ms
        )
        self._stopped = True

    def check(self) -> bool:
        """主动查询是否仍在时限内。返回 True=未超时, False=已超时。

        长循环内每次迭代调用一次:超时后调用方自行 break/return。
        """
        now = time.perf_counter()
        self.elapsed_ms = (now - self._t0) * 1000
        if self.deadline_ms is not None and self.elapsed_ms >= self.deadline_ms:
            self.timed_out = True
            return False
        return True

    @property
    def remaining_ms(self) -> float | None:
        if self.deadline_ms is None:
            return None
        return max(0, self.deadline_ms - self.elapsed_ms)

    @property
    def elapsed_s(self) -> float:
        return self.elapsed_ms / 1000
