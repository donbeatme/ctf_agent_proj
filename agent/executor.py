"""执行 Agent 接口桩(② 职责)。③ 只调用,不实现。"""

from dataclasses import dataclass


@dataclass
class ExecResult:
    observation: str            # 执行观察(喂给步骤校验)
    result: dict | None = None  # 该步产物(可选,写 dag.step.result 供 ee)
    tool_calls: list[dict] | None = None  # 工具调用轨迹 [{tool, args, result}](喂 trace 通道)
    total_usage: dict | None = None  # {prompt_tokens, completion_tokens, total_tokens}


class Executor:
    def run(self, step, ctx: str, tool_exec=None) -> ExecResult:
        raise NotImplementedError


class MockExecutor(Executor):
    """可配置返回内容的执行 mock。
    传 fn 则用 callable(step, ctx, tool_exec=None) -> ExecResult;否则固定返回 observation/result/tool_calls。"""

    def __init__(self, observation: str = "", result: dict | None = None,
                 tool_calls: list[dict] | None = None, fn=None):
        self._observation = observation
        self._result = result
        self._tool_calls = tool_calls
        self._fn = fn

    def run(self, step, ctx: str, tool_exec=None) -> ExecResult:
        if self._fn is not None:
            try:
                return self._fn(step, ctx, tool_exec)
            except TypeError:
                return self._fn(step, ctx)  # 兼容旧 2 参 fn(step, ctx)
        return ExecResult(observation=self._observation, result=self._result,
                          tool_calls=self._tool_calls)
