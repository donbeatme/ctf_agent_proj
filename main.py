import argparse
import json
import time
from pathlib import Path

from agent.evaluator import build_evaluator
from agent.llm_api import make_compress
from model_config import get as cfg_get
from ctf_platform import cli as ctf_cli
from opslog import attach, detach, emit
from sandbox_env import cli as sandbox_cli

_LOCAL_RUNS_ROOT = Path(__file__).resolve().parent / "runs"

_MOCK_TASK = {
    "task_id": "mock-0001",
    "ground_id": "g-mock",
    "challenge_id": "c-mock",
    "title": "base64 编码",
    "description": "给定一段文本,base64 编码后作为 flag 提交。",
}


def _evaluator_mode(args) -> str:
    """评估器选择:config 优先(env EVALUATOR / model_config.json EVALUATOR),CLI --evaluator 兜底。

    用户要求评估器用 config 开关而非命令行参数,故 config 是主开关,CLI 仅作旧调用兼容。
    """
    mode = (cfg_get("EVALUATOR") or "").strip().lower()
    if mode:
        return mode
    return getattr(args, "evaluator", "smoke") or "smoke"


def _load_flag_rules(args) -> dict:
    """从 --flag-rules JSON 文件加载 flag 规则；未提供时返回空规则。"""
    path = getattr(args, "flag_rules", None)
    if not path:
        return {}
    from pathlib import Path
    import json as _json
    data = _json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("flag-rules must be a JSON object")
    return data

def _workspace_event_sink(ws):
    """把 audit 事件写入 workspace.events.jsonl。"""
    from agent.schema import Role

    def sink(kind: str, detail: dict) -> None:
        try:
            ws.add_event(Role.SYSTEM, kind, **detail)
        except Exception:
            pass

    return sink


def _deterministic_goal_eval(ctx, goals, dag_summary):
    """audit eval_goals 兜底:从 dag_summary 抽 PASSED 步骤为证据(比照 StepLLMEvaluator.eval_goals)。"""
    import re

    from agent.schema import GoalEvalDetail

    passed = [
        m.group(1) for m in re.finditer(r"\[(\w+)\]\s+status=PASSED", dag_summary or "")
    ]
    return [
        GoalEvalDetail(
            goal_id=str(g.get("id", "")),
            complete=bool(passed),
            evidence=passed,
            reasoning=f"evidence: {passed}" if passed else "尚无 PASSED 步骤证据",
        )
        for g in goals
    ]


def _ops_sink(ws, engine, run_id):
    """把 canonical 流(ops.log)投影进 workspace.events.jsonl + run.log。

    事件源合一后,adapter/sandbox/ssh 等外围事件已由 opslog.emit 写 canonical 流;
    这里只做投影,不再 re-emit:
    - ws.* 决策链事件:workspace.add_event 自身已落 events.jsonl,run.log 由 EngineLogger
      渲染,这里跳过防双写。
    - engine.* 信号:EngineLogger 渲染 run.log;只把生命周期 run_started/run_ended
      投影进 events.jsonl(其余 engine 信号量大,留在 canonical 流 + run.log)。
    - 其余外围事件:经 ws.ingest_external 投影 events.jsonl(run 账本跨域链路)+
      run.log [ops] 行。
    """
    def sink(kind: str, detail: dict) -> None:
        domain = detail.get("domain")
        if domain == "ws":
            return
        rec = dict(detail)
        rec.setdefault("run_id", run_id)
        if not (domain == "engine" and kind not in ("engine.run_started", "engine.run_ended")):
            try:
                ws.ingest_external(kind, rec)
            except Exception:
                pass
        if domain not in ("engine", "ws"):
            try:
                fields = "  ".join(
                    f"{k}={v}" for k, v in rec.items()
                    if k not in ("ts", "domain", "event", "run_id", "seq",
                                 "node_id", "round", "_uuid")
                )
                engine._log.engine_action(f"ops[{kind}] run_id={run_id}  {fields}")
            except Exception:
                pass

    return sink


def run_task(args):
    """端到端冒烟:真实 Planner(默认 llm_call 走 llm_api.chat_with_tools)+ 评估器按 config 分发。

    主循环完全真实:planner 规划→评审→调度→执行→验收→重规划→反思;评估器经
    build_evaluator 按 EVALUATOR_PLAN/STEP/TASK 分角色 real|mock(默认全 mock:ep 按真实
    blueprint 判空,ee 恒 PASS,et 恒 DONE)。跑的是真模型,需要已配 LLM key。
    """
    import time

    from agent.engine import Engine
    from agent.planner import CombinedDocStore, Planner
    from agent.skills import CtfSkillsDocStore
    from agent.workspace import Workspace

    task = json.loads(args.task) if args.task else _MOCK_TASK
    run_id = args.run_id or f"run-{time.strftime('%Y%m%d-%H%M%S')}"
    emit("engine", "run_started", run_id=run_id,
         task=(task.get("title") or task.get("name") or "")[:120])
    ws = Workspace.create(run_id, task)
    parallel_kw = _parallel_engine_kw(args)
    understander = None
    if getattr(args, "understander", "mock") == "real":
        from task_understanding.real_understander import RealTaskUnderstander

        understander = RealTaskUnderstander()
    if _evaluator_mode(args) == "audit":
        from audit import AgentAuditService, AgentRuntimeBindings
        from audit.settings import Settings

        settings = Settings.from_env()
        service = AgentAuditService(
            settings=settings,
            flag_rules=_load_flag_rules(args),
            run_id=run_id,
            agent_id="ctf-agent",
            event_sink=_workspace_event_sink(ws),
        )
        holder: dict = {}
        bindings = AgentRuntimeBindings(
            blueprint=lambda: holder["engine"].bp,
            task=lambda: (
                holder["engine"].task_input.raw_content
                if holder.get("engine") and getattr(holder["engine"], "task_input", None)
                else task
            ),
            current_step=lambda: holder["engine"].current,
            observation=lambda: holder["engine"]._obs or "",
            submitted_flag=lambda: holder["engine"].submitted_flag if holder.get("engine") else None,
            completed=lambda: holder["engine"].task_completed,
            goal_evaluator=_deterministic_goal_eval,
            submission_result=lambda: (ws.meta or {}).get("submission") if holder.get("engine") else None,
        )
        evaluator = service.bind_evaluator(bindings, audit_output=ws.root / "audit.json")
        engine = Engine(
            Planner(
                workspace=ws,
                docs=CombinedDocStore((CtfSkillsDocStore(), service.planner_docs)),
            ),
            _build_executor(args, ws=ws),
            evaluator,
            workspace=ws,
            understander=understander,
            compress=make_compress(),
            **parallel_kw,
        )
        holder["engine"] = engine
        sink = _ops_sink(ws, engine, run_id)
        attach(sink)
        try:
            _clean_challenge_dir(task)
            engine.run(task)
        finally:
            evaluator.close()
            detach(sink)
            _close_scheduler(parallel_kw.get("scheduler"))
        emit("engine", "run_ended", run_id=run_id, state=engine.scheduler.state.value)
        print(f"run_id: {run_id}")
        print(f"终态: {engine.scheduler.state.value}  重规划 {engine.replans} 次")
        if engine.fail_reason:
            print(f"失败原因: {engine.fail_reason}")
        print(f"audit: {evaluator.audit_written or (ws.root / 'audit.json')}")
        print("\n最终计划:")
        if engine.bp is None:
            print("  (无计划)")
            return
        for sid, s in engine.bp.steps.items():
            print(f"  {sid}\t{s.status.value}\tattempts={s.attempts}\t依赖={s.depends_on}")
            print(f"       instruction: {s.instruction}")
            print(f"       criterion:   {s.criterion}")
        return
    engine = Engine(
        Planner(workspace=ws, docs=CtfSkillsDocStore()),
        _build_executor(args, ws=ws),
        build_evaluator(ws),
        workspace=ws,
        understander=understander,
        compress=make_compress(),
        **parallel_kw,
    )
    sink = _ops_sink(ws, engine, run_id)
    attach(sink)
    try:
        _clean_challenge_dir(task)
        engine.run(task)
    except Exception as exc:
        detach(sink)
        emit("engine", "run_ended", run_id=run_id, state="FAILED")
        print(f"run 异常(模型输出/计划非法?): {type(exc).__name__}: {exc}")
        _close_scheduler(parallel_kw.get("scheduler"))
        return
    detach(sink)
    emit("engine", "run_ended", run_id=run_id, state=engine.scheduler.state.value)
    print(f"run_id: {run_id}")
    print(f"终态: {engine.scheduler.state.value}  重规划 {engine.replans} 次")
    if engine.fail_reason:
        print(f"失败原因: {engine.fail_reason}")
    print("\n最终计划:")
    if engine.bp is None:
        print("  (无计划)")
        _close_scheduler(parallel_kw.get("scheduler"))
        return
    for sid, s in engine.bp.steps.items():
        print(f"  {sid}\t{s.status.value}\tattempts={s.attempts}\t依赖={s.depends_on}")
        print(f"       instruction: {s.instruction}")
        print(f"       criterion:   {s.criterion}")
    _close_scheduler(parallel_kw.get("scheduler"))

def _platform_adapter():
    """按 env 构造平台适配器(凭证未配返回 None → 提交回退为仅记录)。"""
    from ctf_platform.config import StoreSettings
    from ctf_platform.ctf2 import Ctf2Adapter

    settings = StoreSettings.from_env()
    if not (settings.ctf2_session_token or settings.ctf2_cookie):
        return None
    return Ctf2Adapter(settings)


def _build_executor(args, ws=None, workdir=None):
    """按 --executor 构建执行 Agent:mock=MockExecutor;real=RealExecutor + CommandRunner。"""
    if getattr(args, "executor", "mock") == "real":
        from agent.executor import RealExecutor
        from agent.runner import CommandRunner

        # 命令只经沙箱执行:CommandRunner 懒建 SandboxManager(config_sandbox 提供凭据)
        return RealExecutor(runner=CommandRunner(), workspace=ws, workdir=workdir,
                            adapter=_platform_adapter())
    from agent.executor import MockExecutor

    return MockExecutor(observation="(mock) 执行完成")


def _parallel_engine_kw(args, settings=None) -> dict:
    """actor mode(--actors N>1)注入 Engine 的并行参数;ssh 未配置回退串行(打印警告不抛)。

    并行:每步独立容器租约并发执行,依赖 SshProvider 连接池 + SandboxProvider 容器会话,
    ExecutionScheduler 编排;Engine 注入 {scheduler, max_concurrency}。串行/不可用返回
    空 dict(Engine **{} 无效果,保持原路径)。settings 可注入(测试/配置),None 从环境读。
    """
    n = int(getattr(args, "actors", 1) or 1)
    if n <= 1:
        return {}
    try:
        from agent.env_providers import SandboxProvider, SshProvider
        from agent.scheduler import ExecutionScheduler

        ssh = SshProvider(settings=settings, max_connections=n)
        sched = ExecutionScheduler(providers=[SandboxProvider(ssh, settings=settings)])
    except ValueError as exc:
        print(f"actor mode 不可用({exc}),回退串行")
        return {}
    print(f"actor mode: {n} 并行执行器(ssh 连接池 {n} 条,每 actor 独立容器)")
    return {"scheduler": sched, "max_concurrency": n}


def _close_scheduler(sched) -> None:
    """run 结束后关掉并行调度器:连接池 idle 连接 close、live 清零。引擎不负责关,入口收尾。

    Engine.run 已跑完自身事件循环,这里另起一个关连接池(asyncssh 连接可能绑定旧循环,
    关闭失败被 provider 内部吞掉,非致命)。
    """
    if sched is not None:
        import asyncio

        asyncio.run(sched.close())


def _clean_challenge_dir(raw: dict) -> None:
    """环境打开前清理挑战目录遗留产物(仅保留 metadata.yml + distfiles)。

    防止上次 run 写入的脚本/临时文件(如 solve_extract.py)泄漏到本次执行 ctx。
    """
    ch_dir = (raw or {}).get("challenge_dir")
    if not ch_dir:
        return
    from ctf_platform.base import clean_challenge_dir

    try:
        removed = clean_challenge_dir(ch_dir)
        if removed:
            print(f"已清理挑战目录遗留产物: {removed}")
    except Exception as exc:  # noqa: BLE001 — 清理失败不阻塞运行
        print(f"挑战目录清理跳过: {type(exc).__name__}: {exc}")


def _mock_workflow_plan() -> str:
    return json.dumps(
        {
            "add": [
                {
                    "id": "s1",
                    "instruction": "Review the structured local challenge input.",
                    "criterion": "The structured challenge information is available to the workflow.",
                    "depends_on": [],
                }
            ],
            "update": [],
            "remove": [],
            "reason": "Local workflow reproduction smoke test.",
        },
        ensure_ascii=False,
    )


def _mock_noop_plan() -> str:
    return json.dumps(
        {
            "add": [],
            "update": [],
            "remove": [],
            "reason": "Local workflow reflection complete.",
        },
        ensure_ascii=False,
    )


class _SequencedMockPlannerLLM:
    def __init__(self, *responses):
        from agent.planner import MockPlannerLLM

        self._responses = [MockPlannerLLM(response) for response in responses]
        self.calls = 0

    def __call__(self, **kwargs) -> str:
        response = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return response(**kwargs)


def _local_planner(mode, ws):
    from agent.planner import Planner

    if mode == "mock":
        return Planner(
            llm_call=_SequencedMockPlannerLLM(_mock_workflow_plan(), _mock_noop_plan()),
            workspace=ws,
        )
    if mode == "real":
        from agent.skills import CtfSkillsDocStore

        return Planner(workspace=ws, docs=CtfSkillsDocStore())
    raise ValueError(f"unknown planner mode: {mode}")


def run_local_challenge(args):
    """Run a local challenge through RealTaskUnderstander and the real Engine.

    The default planner mode is offline: real Planner, fixed MockPlannerLLM JSON,
    MockExecutor, and build_evaluator (per-role real|mock from config). It validates
    the workflow without calling an external model or CTF tooling.
    """

    from agent.engine import Engine
    from agent.workspace import Workspace
    from task_understanding.real_understander import RealTaskUnderstander

    raw = {"challenge_dir": args.challenge_dir}
    run_id = args.run_id or f"local-{time.strftime('%Y%m%d-%H%M%S')}"
    emit("engine", "run_started", run_id=run_id, task=str(args.challenge_dir)[:120])
    ws = Workspace.create(run_id, raw, root=_LOCAL_RUNS_ROOT)
    parallel_kw = _parallel_engine_kw(args)
    planner = _local_planner(args.planner_mode, ws)
    image_understander = None
    if args.image_understanding == "ollama":
        from task_understanding.image_understanding import OllamaImageUnderstander

        image_understander = OllamaImageUnderstander(model=args.image_model)
    understander = None
    if getattr(args, "understander", "real") == "real":
        understander = RealTaskUnderstander(image_understander=image_understander)
    if _evaluator_mode(args) == "audit":
        from audit import AgentAuditService, AgentRuntimeBindings
        from audit.settings import Settings
        from agent.planner import CombinedDocStore
        from agent.skills import CtfSkillsDocStore

        settings = Settings.from_env()
        service = AgentAuditService(
            settings=settings,
            flag_rules=_load_flag_rules(args),
            run_id=run_id,
            agent_id="ctf-agent",
            event_sink=_workspace_event_sink(ws),
        )
        holder: dict = {}
        bindings = AgentRuntimeBindings(
            blueprint=lambda: holder["engine"].bp,
            task=lambda: (
                holder["engine"].task_input.raw_content
                if holder.get("engine") and getattr(holder["engine"], "task_input", None)
                else raw
            ),
            current_step=lambda: holder["engine"].current,
            observation=lambda: holder["engine"]._obs or "",
            submitted_flag=lambda: holder["engine"].submitted_flag if holder.get("engine") else None,
            completed=lambda: holder["engine"].task_completed,
            goal_evaluator=_deterministic_goal_eval,
            submission_result=lambda: (ws.meta or {}).get("submission") if holder.get("engine") else None,
        )
        # 技能库 + 经验库合并检索:技能文档照旧经 DocsComponent(id+一句话)+ get_doc 按需取全文
        planner.docs = CombinedDocStore(
            (planner.docs or CtfSkillsDocStore(), service.planner_docs)
        )
        evaluator = service.bind_evaluator(bindings, audit_output=ws.root / "audit.json")
        engine = Engine(
            planner,
            _build_executor(args, ws=ws, workdir=args.challenge_dir),
            evaluator,
            workspace=ws,
            understander=understander,
            compress=make_compress(),
            **parallel_kw,
        )
        holder["engine"] = engine
        sink = _ops_sink(ws, engine, run_id)
        attach(sink)
        try:
            _clean_challenge_dir(raw)
            engine.run(raw)
        finally:
            evaluator.close()
            detach(sink)
            _close_scheduler(parallel_kw.get("scheduler"))
        emit("engine", "run_ended", run_id=run_id, state=engine.scheduler.state.value)
        raw_content = engine.task_input.raw_content
        print(f"run_id: {run_id}")
        print(f"challenge name: {raw_content.get('name', '')}")
        print(f"artifact count: {len(raw_content.get('artifacts') or [])}")
        print(f"goal count: {len(engine.goals)}")
        print(f"Engine final state: {engine.scheduler.state.value}")
        print(f"replans: {engine.replans}")
        print(f"Blueprint step count: {len(engine.bp.steps) if engine.bp else 0}")
        print(f"audit: {evaluator.audit_written or (ws.root / 'audit.json')}")
        return
    engine = Engine(
        planner,
        _build_executor(args, ws=ws, workdir=args.challenge_dir),
        build_evaluator(ws),
        workspace=ws,
        understander=understander,
        compress=make_compress(),
        **parallel_kw,
    )
    sink = _ops_sink(ws, engine, run_id)
    attach(sink)
    try:
        _clean_challenge_dir(raw)
        engine.run(raw)
    except Exception as exc:
        detach(sink)
        emit("engine", "run_ended", run_id=run_id, state="FAILED")
        print(f"run 异常: {type(exc).__name__}: {exc}")
        _close_scheduler(parallel_kw.get("scheduler"))
        return
    detach(sink)
    emit("engine", "run_ended", run_id=run_id, state=engine.scheduler.state.value)
    _close_scheduler(parallel_kw.get("scheduler"))

    raw_content = engine.task_input.raw_content
    print(f"run_id: {run_id}")
    print(f"challenge name: {raw_content.get('name', '')}")
    print(f"artifact count: {len(raw_content.get('artifacts') or [])}")
    print(f"goal count: {len(engine.goals)}")
    print(f"Engine final state: {engine.scheduler.state.value}")
    print(f"replans: {engine.replans}")
    print(f"Blueprint step count: {len(engine.bp.steps) if engine.bp else 0}")


def main():
    parser = argparse.ArgumentParser(description="CTF2 ReAct agent")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("run-task", help="端到端冒烟:真实 planner + mock 执行/评估")
    p.add_argument("--task", help="任务 dict(JSON);缺省用内置示例题")
    p.add_argument("--run-id", help="run 目录名(缺省按时间生成)")
    p.add_argument(
        "--understander",
        choices=("mock", "real"),
        default="mock",
        help="mock=MockTaskUnderstander; real=RealTaskUnderstander(需 task 含 challenge_dir/metadata_path)",
    )
    p.add_argument(
        "--evaluator",
        choices=("smoke", "audit"),
        default="smoke",
        help="评估器:smoke=build_evaluator(按 EVALUATOR_PLAN/STEP/TASK 分角色 real|mock); audit=AgentAuditEvaluator。config 主开关(EVALUATOR env/model_config.json)优先,此参数兜底",
    )
    p.add_argument(
        "--executor",
        choices=("mock", "real"),
        default="mock",
        help="mock=MockExecutor; real=RealExecutor+CommandRunner(真实命令路由)",
    )
    p.add_argument(
        "--actors",
        type=int,
        default=1,
        help="并行执行器数(actor mode):>1 时每步独立容器并发执行,需 CTF_SSH_HOST 已配置且 task 含 challenge_dir/workdir;=1 串行原路径",
    )
    p.add_argument(
        "--flag-rules",
        default=None,
        help="audit 评估使用的 flag 规则 JSON 文件路径",
    )

    p_local = sub.add_parser(
        "run-local-challenge",
        help="本地 challenge 冒烟:RealTaskUnderstander + Engine + mock 执行/评估",
    )
    p_local.add_argument("--challenge-dir", required=True, help="本地 challenge 目录")
    p_local.add_argument("--run-id", help="run 目录名(缺省按时间生成)")
    p_local.add_argument(
        "--understander",
        choices=("mock", "real"),
        default="real",
        help="mock=MockTaskUnderstander; real=RealTaskUnderstander(默认)",
    )
    p_local.add_argument(
        "--evaluator",
        choices=("smoke", "audit"),
        default="smoke",
        help="评估器:smoke=build_evaluator(按 EVALUATOR_PLAN/STEP/TASK 分角色 real|mock); audit=AgentAuditEvaluator。config 主开关(EVALUATOR env/model_config.json)优先,此参数兜底",
    )
    p_local.add_argument(
        "--executor",
        choices=("mock", "real"),
        default="mock",
        help="mock=MockExecutor; real=RealExecutor+CommandRunner(真实命令路由)",
    )
    p_local.add_argument(
        "--actors",
        type=int,
        default=1,
        help="并行执行器数(actor mode):>1 时每步独立容器并发执行,需 CTF_SSH_HOST 已配置;=1 串行原路径",
    )
    p_local.add_argument(
        "--flag-rules",
        default=None,
        help="audit 评估使用的 flag 规则 JSON 文件路径",
    )
    p_local.add_argument(
        "--planner-mode",
        choices=("mock", "real"),
        default="mock",
        help="mock=真实 Planner + MockPlannerLLM; real=真实 Planner + LLM",
    )
    p_local.add_argument(
        "--image-understanding",
        choices=("off", "ollama"),
        default="off",
        help="off=只保留图片metadata; ollama=使用本机VLM补充图片语义",
    )
    p_local.add_argument(
        "--image-model",
        default="qwen3-vl:32b",
        help="--image-understanding ollama 时使用的本机视觉模型",
    )

    ctf_cli.register(sub)
    sandbox_cli.register(sub)

    s = sub.add_parser("serve", help="启动指令台前端 (web_server)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)

    args = parser.parse_args()

    if args.cmd == "run-task":
        run_task(args)
    elif args.cmd == "serve":
        from web_server import serve
        serve(args.host, args.port)
    elif args.cmd == "run-local-challenge":
        run_local_challenge(args)
    elif getattr(args, "func", None):
        args.func(args)


if __name__ == "__main__":
    main()
