"""ExecState 状态化系统提示词:render_exec_state / Executor.system_for 单测。

覆盖:
- 各 status 渲染出对应硬指令(first/retry_incomplete/retry_drift/retry_other)
- retry 含尝试进度与上一步 ee 判定(verdict/diagnosis);first 无 ee 段
- attempts==max_attempts 时追加"最后一次尝试"警示
- Executor.system_for:base 追加状态段 / 无状态上下文原样返回 / Mock(system="")恒 ""
"""

from agent.executor import (
    ExecState,
    Executor,
    MockExecutor,
    RealExecutor,
    render_exec_state,
)


def test_first_state_no_ee_section():
    sc = ExecState(status="first", attempts=1, max_attempts=3)
    out = render_exec_state(sc)
    assert "首次执行" in out
    assert "当前尝试: 1/3" in out
    assert "上一步 ee 判定" not in out
    assert "最后一次尝试" not in out


def test_retry_incomplete_has_verdict_and_diagnosis():
    sc = ExecState(status="retry_incomplete", attempts=2, max_attempts=3,
                   verdict="retry", diagnosis="incomplete")
    out = render_exec_state(sc)
    assert "必须先产出结论" in out
    assert "禁止重复已执行过的全量命令" in out
    assert "当前尝试: 2/3" in out
    assert "上一步 ee 判定: retry / incomplete" in out
    assert "最后一次尝试" not in out


def test_retry_drift_note():
    sc = ExecState(status="retry_drift", attempts=2, max_attempts=3,
                   verdict="retry", diagnosis="drift")
    out = render_exec_state(sc)
    assert "回到 criterion" in out
    assert "drift" in out


def test_retry_other_fallback_note():
    sc = ExecState(status="retry_other", attempts=2, max_attempts=3,
                   verdict="retry", diagnosis="other")
    out = render_exec_state(sc)
    assert "先说明上轮为何失败" in out


def test_last_chance_flag_at_max_attempts():
    sc = ExecState(status="retry_incomplete", attempts=3, max_attempts=3,
                   verdict="retry", diagnosis="incomplete")
    out = render_exec_state(sc)
    assert "最后一次尝试" in out


class _TinyExecutor(Executor):
    system = "BASE_SYSTEM"


def test_system_for_appends_state():
    out = _TinyExecutor().system_for(
        ExecState(status="first", attempts=1, max_attempts=3))
    assert out.startswith("BASE_SYSTEM")
    assert "首次执行" in out
    assert "当前尝试: 1/3" in out


def test_system_for_none_state_returns_base():
    ex = _TinyExecutor()
    assert ex.system_for(None) == "BASE_SYSTEM"
    assert ex.system_for(ExecState(status="first", attempts=1, max_attempts=3)) != "BASE_SYSTEM"


def test_system_for_empty_base_is_noop():
    assert MockExecutor().system_for(
        ExecState(status="first", attempts=1, max_attempts=3)) == ""
    assert MockExecutor().system_for(None) == ""


def test_real_executor_system_still_exex_system():
    # RealExecutor 只改 system 类属性;状态化组合由基类 system_for 完成,引擎级测试覆盖
    assert RealExecutor.system.startswith("你是 CTF 解题执行 Agent")
