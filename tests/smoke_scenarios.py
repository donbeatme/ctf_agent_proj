"""端到端场景测试:真实 Planner + 精心配 evaluator 触发 revise/escalate/deadlock/reflect。"""

import json
import sys
import time
from pathlib import Path

from agent.engine import Engine, EngineState
from agent.evaluator import EvalResult, Verdict
from agent.executor import ExecResult, MockExecutor
from tests.mock_data import MOCK_TASK
from agent.planner import Planner
from agent.workspace import Workspace

_SCENARIO_OUT = Path(__file__).resolve().parent / "_scenario_out"
_SCENARIO_OUT.mkdir(exist_ok=True)

ROOT = Path(__file__).resolve().parent.parent


# ===== 工具:编排 + 事件流 dump =====

class StatefulEvaluator:
    """按预置响应列表依次返回;越界取最后一条(反复用)。"""

    def __init__(self, *, review=None, step_eval=None, reflect=None):
        self._review = review or [EvalResult(Verdict.PASS, "plan ok")]
        self._step = step_eval or [EvalResult(Verdict.PASS, "step ok")]
        r = reflect or EvalResult(Verdict.DONE, "done")
        self._reflect = r if isinstance(r, list) else [r]
        self.ri = 0
        self.si = 0
        self.ti = 0

    def review(self, ctx):
        r = self._review[min(self.ri, len(self._review) - 1)]
        self.ri += 1
        return r

    def step_eval(self, ctx):
        r = self._step[min(self.si, len(self._step) - 1)]
        self.si += 1
        return r

    def reflect(self, ctx):
        r = self._reflect[min(self.ti, len(self._reflect) - 1)]
        self.ti += 1
        return r


def _detail(e, name, default=""):
    """detail 兼容访问:新版为类型化 dataclass(读 .字段),旧 events 可能是 dict。"""
    d = e.detail
    if isinstance(d, dict):
        return d.get(name, default)
    return getattr(d, name, default)


def _render_events(ws) -> str:
    """把 events.jsonl 渲成可读文本流程:每行时间/agent/kind/摘要。"""
    lines = []
    for e in ws.events:
        agent = e.agent or "?"
        detail = ""
        if e.kind == "replan":
            detail = _detail(e, "reason")
        elif e.kind == "step_record":
            detail = f"{e.step_id} verdict={e.verdict} obs={_detail(e, 'observation')[:60]}"
        elif e.kind in ("plan_review", "step_eval", "reflect"):
            detail = f"verdict={e.verdict} opinion={_detail(e, 'opinion')[:80]}"
        elif e.kind == "scheduling":
            detail = _detail(e, "opinion")[:80]
        elif e.kind in ("use_tool", "tool_result"):
            detail = f"{_detail(e, 'tool')} {_detail(e, 'output')[:60]}"
        lines.append(f"[{e.ts}] {agent:8s} {e.kind:14s} {detail}")
    return "\n".join(lines)


def _render_steps(bp) -> str:
    if bp is None or not bp.steps:
        return "(无步骤)"
    rows = []
    for sid, s in bp.steps.items():
        rows.append(
            f"  {sid}  {s.status.value:10s}  attempts={s.attempts}  depends={s.depends_on}\n"
            f"       instruction: {s.instruction}\n"
            f"       criterion:   {s.criterion}"
        )
    return "\n".join(rows)


def run_scenario(name, evaluator, task=None):
    """运行一个场景,返回 (engine, workspace);事件流+步骤打印到 REPORTS/<name>.txt。"""
    task = task or MOCK_TASK
    ws = Workspace.create(f"scenario-{name}", task, root=ROOT / "runs")
    executor = MockExecutor(observation="(mock) 执行完成")
    engine = Engine(Planner(workspace=ws), executor, evaluator, workspace=ws)

    t0 = time.time()
    engine.run(task)
    elapsed = time.time() - t0

    report = _SCENARIO_OUT / f"{name}.txt"
    parts = [
        f"===== 场景: {name} =====",
        f"耗时: {elapsed:.1f}s",
        f"终态: {engine.scheduler.state.value}  重规划: {engine.replans} 次",
        f"失败原因: {engine.fail_reason or '—'}",
        "",
        "--- 最终计划 ---",
        _render_steps(engine.bp),
        "",
        "--- 事件流 ---",
        _render_events(ws),
    ]
    report.write_text("\n".join(parts), encoding="utf-8")
    print(f"  [{name}] → {engine.scheduler.state.value}  replan={engine.replans}  ({elapsed:.1f}s)  →  {report}")
    return engine, ws


# ===== 四个场景 =====

def scenario_revise():
    """ep 第一次评审 FAIL(计划不够细)→ planner 修订 → 二次评审 PASS → 继续执行。"""
    return run_scenario("revise", StatefulEvaluator(
        review=[
            EvalResult(Verdict.FAIL,
                       "计划粒度太粗:s1 没说明要用什么编码工具(base64 命令/Python),"
                       "验收标准应写明\"本地解码验证可逆\"。"),
            EvalResult(Verdict.PASS, "修订后的计划包含了工具选择和可逆验证,可执行"),
        ],
        step_eval=[EvalResult(Verdict.PASS, "步骤验收通过")],
        reflect=EvalResult(Verdict.DONE, "反思:无问题"),
    ))


def scenario_escalate():
    """s1 执行完但 ee 判定 ESCALATE(工具不可用)→ planner 需绕开该步骤重新规划。"""
    return run_scenario("escalate", StatefulEvaluator(
        review=[EvalResult(Verdict.PASS, "计划可执行")],
        step_eval=[
            EvalResult(Verdict.ESCALATE,
                       "s1: base64 编码工具不可用(本地无 base64 命令,Python 未安装),"
                       "建议改用在线编码服务或内置 b64encode 函数",
                       observation="exec: base64: command not found"),
            EvalResult(Verdict.PASS, "新方案下的步骤验收通过"),
        ],
        reflect=EvalResult(Verdict.DONE, "反思:无问题"),
    ))


def scenario_deadlock():
    """s1 被升级 → s2 依赖 s1 无法调度 → 死锁 → planner 收到 deadlock 上下文重构依赖。"""
    return run_scenario("deadlock", StatefulEvaluator(
        review=[EvalResult(Verdict.PASS, "计划可执行")],
        step_eval=[
            EvalResult(Verdict.ESCALATE,
                       "s1: 获取原始文本失败(题目附件不存在,纯文本描述中未找到待编码内容),"
                       "该步骤无法继续",
                       observation="附件下载返回 404,题目描述中无可编码文本"),
            # s2 depends on s1, s1 ESCALATED → deadlock
            EvalResult(Verdict.PASS, "重构后的方案验收通过"),
        ],
        reflect=EvalResult(Verdict.DONE, "反思:无问题"),
    ))


def scenario_reflect():
    """正常执行全部步骤 → reflect 终局修订(REPLAN)→ 计划加验证步骤 → reflect 确认(DONE)→ 收尾。"""
    return run_scenario("reflect", StatefulEvaluator(
        review=[EvalResult(Verdict.PASS, "计划可执行")],
        step_eval=[EvalResult(Verdict.PASS, "步骤验收通过")],
        reflect=[
            EvalResult(Verdict.REPLAN,
                       "反思:当前流程编码后直接提交,"
                       "建议在提交前增加本地验证步骤(编码结果 base64 -d 可逆验证),"
                       "避免提交错误 flag 浪费次数。"),
            EvalResult(Verdict.DONE, "反思:验证步骤已加入,收尾"),
        ],
    ))


def main():
    print("===== 场景测试(真实 planner + mock executor/evaluator) =====\n")
    scenarios = [
        ("revise",   scenario_revise),
        ("escalate", scenario_escalate),
        ("deadlock", scenario_deadlock),
        ("reflect",  scenario_reflect),
    ]
    for name, fn in scenarios:
        try:
            fn()
        except Exception as exc:
            print(f"  [{name}] 异常: {type(exc).__name__}: {exc}")
    print("\n输出目录:", _SCENARIO_OUT.resolve())


if __name__ == "__main__":
    main()
