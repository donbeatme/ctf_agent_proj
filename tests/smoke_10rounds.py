"""真实 Planner 10 轮批量冒烟:每轮不同 mock task + 不同 executor/evaluator 数据。

覆盖引擎路径:revise / escalate / retry / reflect-replan / deadlock / happy-path。
跑的是真模型(Planner 默认 llm_call 走 llm_api.chat_with_tools),其余 agent 全部 mock。
需要已配 LLM key(环境变量)。
"""

import json
import sys
import time
from pathlib import Path

# 直跑脚本时把项目根挂上 sys.path(python tests/smoke_10rounds.py)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.engine import Engine, EngineState
from agent.evaluator import EvalResult, Verdict
from agent.executor import ExecResult, MockExecutor
from agent.schema import GoalEvalDetail
from agent.planner import Planner
from agent.workspace import Workspace

OUT = Path(__file__).resolve().parent / "_rounds_out"
OUT.mkdir(exist_ok=True)
ROOT = Path(__file__).resolve().parent.parent


class StatefulEvaluator:
    """按预置响应列表依次返回;越界取最后一条(反复用)。review/step_eval/reflect 均支持列表。"""

    def __init__(self, *, review=None, step_eval=None, reflect=None, goals=None):
        self._review = review or [EvalResult(Verdict.PASS, "plan ok")]
        self._step = step_eval or [EvalResult(Verdict.PASS, "step ok")]
        self._reflect = reflect or [EvalResult(Verdict.DONE, "done")]
        self._goals = goals   # callable(ctx, goals, dag) -> list[GoalEvalDetail]; None=不评估
        self.ri = 0
        self.si = 0
        self.ri_last = 0

    def _next(self, arr, i):
        return arr[min(i, len(arr) - 1)]

    def review(self, ctx):
        self.ri_last = self.ri
        self.ri += 1
        return self._next(self._review, self.ri_last)

    def step_eval(self, ctx):
        self.si += 1
        return self._next(self._step, self.si - 1)

    def reflect(self, ctx):
        # reflect 按 review 的调用次数对齐:REPLAN 后再次走到 reflect 时取下一条
        r = self._next(self._reflect, self.ri_last)
        return r

    def eval_goals(self, ctx, goals, dag_summary):
        if self._goals is not None:
            return self._goals(ctx, goals, dag_summary)
        steps = self._ws.blueprint.steps if getattr(self, "_ws", None) and self._ws.blueprint else {}
        evidence = [sid for sid, s in steps.items() if s.status.value == "PASSED"]
        return [GoalEvalDetail(goal_id=g["id"], complete=bool(evidence), evidence=evidence,
                               reasoning="mock: 步骤全 PASS")
                for g in goals]


def _default_eval_goals(ws):
    """把未完成 goal 判为:当前有 PASSED 步骤即 complete(mock,只验证链路)。
    注意 reasoning 不能为空(EvalEvent.opinion 契约要求非空)。"""
    def f(ctx, goals, dag_summary):
        steps = ws.blueprint.steps if ws.blueprint else {}
        evidence = [sid for sid, s in steps.items() if s.status.value == "PASSED"]
        return [GoalEvalDetail(goal_id=g["id"], complete=bool(evidence), evidence=evidence,
                               reasoning="mock: 步骤全 PASS")
                for g in goals]
    return f


# ===== 10 轮任务 + 其它 agent 数据 =====

ROUNDS = [
    # --- 1. happy path:全 PASS ---
    dict(
        name="r1-base64",
        task={"task_id": "t1", "title": "base64 编码",
              "description": "给定一段文本,base64 编码后作为 flag 提交。",
              "goals": [{"id": "g1"}]},
        executor=MockExecutor(observation="(mock) 执行完成", result={"artifact": "b64.txt"}),
        evaluator=StatefulEvaluator(
            review=[EvalResult(Verdict.PASS, "计划可执行")],
            step_eval=[EvalResult(Verdict.PASS, "步骤验收通过")],
            reflect=[EvalResult(Verdict.DONE, "反思:无问题")]),
    ),
    # --- 2. revise:ep 首次 FAIL 后修订 PASS ---
    dict(
        name="r2-revise",
        task={"task_id": "t2", "title": "SQL 注入",
              "description": "登录接口存在 SQL 注入,绕过认证拿到 flag。",
              "goals": [{"id": "g1"}]},
        executor=MockExecutor(observation="(mock) 注入执行完成"),
        evaluator=StatefulEvaluator(
            review=[
                EvalResult(Verdict.FAIL,
                           "计划粒度太粗:s1 未说明用哪种注入手法(单引号报错探测/UNION),"
                           "验收标准应写明\"能读到 users 表里 admin 的密码或直接绕过登录\"。"),
                EvalResult(Verdict.PASS, "修订后计划包含注入手法与验证方式,可执行"),
            ],
            step_eval=[EvalResult(Verdict.PASS, "步骤验收通过")],
            reflect=[EvalResult(Verdict.DONE, "反思:无问题")]),
    ),
    # --- 3. escalate:ee 判 ESCALATE(工具不可用)→ 重规划 ---
    dict(
        name="r3-escalate",
        task={"task_id": "t3", "title": "端口扫描",
              "description": "扫描目标 10.0.0.5 的开放端口,识别服务后找到 flag 服务。",
              "goals": [{"id": "g1"}]},
        executor=MockExecutor(observation="(mock) 执行完成"),
        evaluator=StatefulEvaluator(
            review=[EvalResult(Verdict.PASS, "计划可执行")],
            step_eval=[
                EvalResult(Verdict.ESCALATE,
                           "s1: nmap 工具不可用(本地未安装,且无权限安装),"
                           "建议改用 python socket 逐端口探测或 nc 手工探测",
                           observation="exec: nmap: command not found"),
                EvalResult(Verdict.PASS, "改用 python 探测后的验收通过"),
            ],
            reflect=[EvalResult(Verdict.DONE, "反思:无问题")]),
    ),
    # --- 4. retry:ee 判 RETRY(上传失败)后重试 PASS ---
    dict(
        name="r4-retry",
        task={"task_id": "t4", "title": "文件上传",
              "description": "上传页校验不严,构造 webshell 上传后访问拿 flag。",
              "goals": [{"id": "g1"}]},
        executor=MockExecutor(observation="(mock) 上传执行完成"),
        evaluator=StatefulEvaluator(
            review=[EvalResult(Verdict.PASS, "计划可执行")],
            step_eval=[
                EvalResult(Verdict.RETRY,
                           "s1: 上传 shell.php 被拒(扩展名黑名单),"
                           "改用双扩展名 shell.php.jpg 或改 Content-Type 重试",
                           observation="HTTP 403: 文件类型不允许"),
                EvalResult(Verdict.PASS, "改用 .phtml 后上传成功,验收通过"),
            ],
            reflect=[EvalResult(Verdict.DONE, "反思:无问题")]),
    ),
    # --- 5. reflect:et REPLAN(补本地验证步骤)后 DONE ---
    dict(
        name="r5-reflect",
        task={"task_id": "t5", "title": "JWT 伪造",
              "description": "网站用 JWT 做认证,需伪造 admin token 访问 /admin 拿 flag。",
              "goals": [{"id": "g1"}]},
        executor=MockExecutor(observation="(mock) 执行完成"),
        evaluator=StatefulEvaluator(
            review=[EvalResult(Verdict.PASS, "计划可执行")],
            step_eval=[EvalResult(Verdict.PASS, "步骤验收通过")],
            reflect=[
                EvalResult(Verdict.REPLAN,
                           "反思:当前流程伪造 token 后直接访问,建议在访问前增加一步"
                           "在 jwt.io 或本地解析校验 token 结构(alg/签名段),避免伪造失败浪费次数"),
                EvalResult(Verdict.DONE, "补验证步骤后反思无问题"),
            ]),
    ),
    # --- 6. happy + 多目标:全 PASS,2 goals ---
    dict(
        name="r6-dirb",
        task={"task_id": "t6", "title": "目录爆破",
              "description": "爆破目标隐藏路径,找到 /backup 下的 flag 文件并下载。",
              "goals": [{"id": "g1"}, {"id": "g2"}]},
        executor=MockExecutor(observation="(mock) 爆破完成", result={"paths": ["/backup", "/admin"]}),
        evaluator=StatefulEvaluator(
            review=[EvalResult(Verdict.PASS, "计划可执行")],
            step_eval=[EvalResult(Verdict.PASS, "步骤验收通过")],
            reflect=[EvalResult(Verdict.DONE, "反思:无问题")]),
    ),
    # --- 7. deadlock:s1 ESCALATE 阻塞 s2(s2 依赖 s1)→ 死锁重规划 ---
    dict(
        name="r7-deadlock",
        task={"task_id": "t7", "title": "XOR 异或解密",
              "description": "密文文件 xor.bin 用单字节异或加密,爆破密钥后解密拿 flag。",
              "goals": [{"id": "g1"}]},
        executor=MockExecutor(observation="(mock) 执行完成"),
        evaluator=StatefulEvaluator(
            review=[EvalResult(Verdict.PASS, "计划可执行")],
            step_eval=[
                EvalResult(Verdict.ESCALATE,
                           "s1: 读取 xor.bin 失败(题目环境里附件不存在,密文只在描述里给了十六进制),"
                           "该步骤无法继续",
                           observation="文件不存在: xor.bin"),
                EvalResult(Verdict.PASS, "改从描述提取十六进制后验收通过"),
            ],
            reflect=[EvalResult(Verdict.DONE, "反思:无问题")]),
    ),
    # --- 8. revise 两次:ep FAIL→FAIL→PASS(反复修订) ---
    dict(
        name="r8-revise-x2",
        task={"task_id": "t8", "title": "命令注入",
              "description": "ping 参数拼接到系统命令,注入读取 /flag。",
              "goals": [{"id": "g1"}]},
        executor=MockExecutor(observation="(mock) 注入执行完成"),
        evaluator=StatefulEvaluator(
            review=[
                EvalResult(Verdict.FAIL, "计划缺参数探测:s1 应先说明用 ; 还是 | 拼接,以及去哪读 flag"),
                EvalResult(Verdict.FAIL,
                           "仍不充分:未说明注入后如何把 /flag 内容带出来"
                           "(cat /flag / 回显 / 反弹),验收标准应写明\"拿到 flag 明文\""),
                EvalResult(Verdict.PASS, "修订后包含探测/注入/取数三步,可执行"),
            ],
            step_eval=[EvalResult(Verdict.PASS, "步骤验收通过")],
            reflect=[EvalResult(Verdict.DONE, "反思:无问题")]),
    ),
    # --- 9. happy + 分步产物:executor 按 step 返回不同 result ---
    dict(
        name="r9-artifacts",
        task={"task_id": "t9", "title": "robots.txt 敏感文件",
              "description": "按 robots.txt 的 Disallow 提示找到 /flag.txt 并读取提交。",
              "goals": [{"id": "g1"}]},
        executor=MockExecutor(fn=lambda step, ctx: ExecResult(
            observation=f"(mock) {step.id} 执行完成",
            result={step.id: f"artifact-of-{step.id}"})),
        evaluator=StatefulEvaluator(
            review=[EvalResult(Verdict.PASS, "计划可执行")],
            step_eval=[EvalResult(Verdict.PASS, "步骤验收通过")],
            reflect=[EvalResult(Verdict.DONE, "反思:无问题")]),
    ),
    # --- 10. 签到题:单步 happy,无 goals ---
    dict(
        name="r10-checkin",
        task={"task_id": "t10", "title": "签到题",
              "description": "打开首页,查看页面源码注释中的 flag 并提交。"},
        executor=MockExecutor(observation="(mock) 已读取页面源码", result={"flag": "flag{checkin}"}),
        evaluator=StatefulEvaluator(
            review=[EvalResult(Verdict.PASS, "计划可执行")],
            step_eval=[EvalResult(Verdict.PASS, "步骤验收通过")],
            reflect=[EvalResult(Verdict.DONE, "反思:无问题")]),
    ),
]


def run_round(round_spec) -> dict:
    name = round_spec["name"]
    task = round_spec["task"]
    ws = Workspace.create(f"round-{name}", task, root=ROOT / "runs")
    ev = round_spec["evaluator"]
    ev._ws = ws
    ev._goals = ev._goals or _default_eval_goals(ws)
    engine = Engine(Planner(workspace=ws), round_spec["executor"], ev, workspace=ws)

    t0 = time.time()
    engine.run(task)
    elapsed = time.time() - t0

    steps = engine.bp.steps if engine.bp else {}
    statuses = [s.status.value for s in steps.values()]
    rr = engine.run_result
    result = dict(
        name=name, title=task.get("title", ""),
        state=engine.scheduler.state.value,
        fail_reason=engine.fail_reason,
        replans=engine.replans, stalls=getattr(engine, "_stalls", 0),
        cycles=rr.cycles if rr else None, tokens=rr.tokens if rr else None,
        completed=rr.completed if rr else None,
        steps=len(steps), statuses=",".join(statuses),
        elapsed=f"{elapsed:.1f}s",
    )
    # 详细报告落盘
    parts = [
        f"===== 轮次: {name}  [{task.get('title','')}] =====",
        f"耗时: {elapsed:.1f}s  终态: {result['state']}  重规划: {result['replans']}  停摆: {result['stalls']}",
        f"cycles: {rr.cycles}  tokens: {rr.tokens}  completed: {rr.completed}  fail_reason: {engine.fail_reason or '—'}",
        "--- 步骤状态 ---",
        *[f"  {sid}  {s.status.value:10s}  depends={s.depends_on}" for sid, s in steps.items()],
        "--- 产物 product ---",
        json.dumps(rr.product if rr else {}, ensure_ascii=False),
    ]
    (OUT / f"{name}.txt").write_text("\n".join(parts), encoding="utf-8")
    return result


def main():
    print("=" * 90)
    print("真实 Planner 10 轮批量冒烟(mock task + mock executor/evaluator,Planner 走真模型)")
    print("=" * 90)
    rows = []
    for spec in ROUNDS:
        try:
            r = run_round(spec)
            rows.append(r)
            print(f"[{r['name']:12s}] {r['state']:8s} replans={r['replans']} "
                  f"stalls={r['stalls']} cycles={r['cycles']} tokens={r['tokens']} "
                  f"completed={r['completed']} steps={r['steps']} ({r['elapsed']})  {r['title']}")
        except Exception as exc:
            rows.append(dict(name=spec["name"], state="EXC",
                             fail_reason=f"{type(exc).__name__}: {exc}"))
            print(f"[{spec['name']:12s}] 异常: {type(exc).__name__}: {exc}")

    print("-" * 90)
    print(f"完成 {len(rows)}/10 轮。汇总: DONE={sum(1 for r in rows if r['state']=='DONE')}  "
          f"FAILED={sum(1 for r in rows if r['state']=='FAILED')}  异常={sum(1 for r in rows if r['state']=='EXC')}")
    print("详细报告:", OUT.resolve())


if __name__ == "__main__":
    main()
