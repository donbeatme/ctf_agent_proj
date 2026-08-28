"""调度器分发逻辑:Engine 主循环驱动状态机,把步骤分发给 executor/评估桩。

在雏形(状态 + 允许边)基础上,进入分发规则:
- 多步循环:STEP_EVAL(pass) → SCHEDULING 取下一个 ready;SCHEDULING 无 ready 且任务完成 → REFLECTING
- 触发规则:
  - PLAN_REVIEW:verdict=fail → PLANNING 重规划;否则 → SCHEDULING
  - STEP_EVAL:retry → EXECUTING 重放(超 max_attempts 转 escalate);escalate → PLANNING;pass → SCHEDULING
  - REFLECTING:反思修订 DAG → PLANNING → DONE(终局修订,不再重入评审/调度)
执行/评估均为接口桩(外部团队实现),当前可挂 MockExecutor / MockEvaluator。
并行 wave:max_concurrency=1 保持串行原路径(一次一个 ready 步骤);>1 时一拍并发
执行多个 ready step(每步独立容器租约/ctx,contextvars 隔离归因),STEP_EVAL 顺序
逐步骤评估,retry 退化为单步,replan 收口。
ctx 注入统一走 workspace:planner 与外部 agent(ep/ex/ee/et)都经 assembler.assemble
组装(组件是 workspace 只读投影;外部桩接口收单串 ctx 文本)。system 提示词经
SystemPromptComponent 渲染(角色各自把它当系统消息传给 LLM,engine 只传组件不并入 ctx,
避免与角色自持常量双写)。
模型返回走 assembler.ingest 反向装填:planner→blueprint+replan 边界 / executor→
use_tool + dag.step.result / 评估→record_opinion(agent_comm 通道投影源,pass 是
闸门不产内容)。生命周期 hook:engine 打点,assembler.dispatch(replan/
plan_review_pass/run_end)+ precompress(planner)。
外部 Agent 调用有异常保护:executor/评估抛错转成失败信号(意见入 turn),不让引擎崩掉;
调度卡壳区分处置:任务完成(ee 打 is_completed,或兜底全部节点终态)→ REFLECTING;
REVISE 是评审标记,评审通过即清回 PENDING,不会滞留到调度期;
前置 ESCALATED 的真死锁 → 注入重构提示重规划,连续限次(max_deadlock_attempts)解不开 → FAILED;
振荡(重规划/连续无改动超限)同样转 FAILED。FAILED 是终态,失败原因记录 fail_reason,优雅返回;
max_cycles 仅作总调度预算兜底,超限抛 EngineError(引擎结构性失序)。
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from enum import StrEnum

from opslog import ErrorLevel, get_run_context, record_error, set_run_context

from agent.blueprint import DONE_STATUSES, Step, StepStatus
from agent.schema import (
    EvalEvent,
    EvalSource,
    EVAL_ROLE,
    EventKind,
    Feedback,
    Goal,
    PlannerInput,
    PlannerMode,
    Role,
    Signal,
    StateContext,
    TaskInput,
    Trigger,
)
from agent.executor import ExecResult
from agent.evaluator import Diagnosis, EvalResult, Verdict
from agent.checks import SkillEnvProbe
from agent import tools
from agent.tools import ToolRegistry
from agent.logging import EngineLogger
from agent.signals import SignalBus
from agent.timing import PhaseTimer
from agent.understander import MockTaskUnderstander
from agent.workspace import MockWorkspace
from agent.llm_api import count_tokens


class EngineState(StrEnum):
    PLANNING = "PLANNING"        # 规划
    PLAN_REVIEW = "PLAN_REVIEW"  # 计划评审
    SCHEDULING = "SCHEDULING"    # 调度取步骤
    EXECUTING = "EXECUTING"      # 执行步骤
    STEP_EVAL = "STEP_EVAL"      # 步骤校验
    REFLECTING = "REFLECTING"    # 任务反思
    DONE = "DONE"                # 终态:任务完成
    FAILED = "FAILED"            # 终态:任务失败(死锁/振荡等不可自愈)


# 允许转换:多步循环 STEP_EVAL(pass)→ SCHEDULING 取下一步;完成/卡壳判断统一收在 SCHEDULING。
# SCHEDULING 无 ready 时:任务完成(ee is_completed 或全部节点终态)→ REFLECTING;
# 否则死锁 → PLANNING 重排(注入重构提示),限次解不开 → FAILED。
# 反思是终局修订:REFLECTING 只进 PLANNING(改 DAG),PLANNING 反思修订后收尾 DONE;
# 评审重规划(ep fail/ee escalate)仍走 PLANNING→PLAN_REVIEW。
TRANSITIONS: dict[EngineState, frozenset[EngineState]] = {
    EngineState.PLANNING: frozenset({EngineState.PLAN_REVIEW, EngineState.DONE, EngineState.FAILED}),
    EngineState.PLAN_REVIEW: frozenset({EngineState.PLANNING, EngineState.SCHEDULING, EngineState.FAILED}),
    EngineState.SCHEDULING: frozenset(
        {EngineState.EXECUTING, EngineState.REFLECTING, EngineState.PLANNING, EngineState.FAILED}
    ),
    EngineState.EXECUTING: frozenset({EngineState.STEP_EVAL, EngineState.FAILED}),
    EngineState.STEP_EVAL: frozenset(
        {EngineState.PLANNING, EngineState.EXECUTING, EngineState.SCHEDULING, EngineState.FAILED}
    ),
    EngineState.REFLECTING: frozenset({EngineState.PLANNING, EngineState.FAILED}),
    EngineState.DONE: frozenset(),
    EngineState.FAILED: frozenset(),
}


# 评估来源 → ingest 角色(从 schema.EVAL_ROLE 引用;SCHEDULING 是引擎结构检测,无评估角色)
# EVAL_ROLE 已从 schema 导入,直接使用


def can_transition(src: EngineState, dst: EngineState) -> bool:
    """是否允许从 src 一步迁到 dst。"""
    return dst in TRANSITIONS[src]


@dataclass
class RunResult:
    """一次 run 的结果汇总(§5.3):终态/达成标志/预算用量/通过步骤的最终产物。

    product 只聚合 verdict=pass 步骤的 result(执行产物),供调用方直接取最终交付物,
    无需再遍历 ws.steps。tokens 为本次 run 累计 LLM token 用量(§5.1)。
    """

    state: str                 # 终态 DONE/FAILED
    completed: bool            # 任务是否达成(goals 全完成或 ee 打 is_completed)
    fail_reason: str | None
    replans: int
    stalls: int
    cycles: int
    tokens: int
    product: dict = field(default_factory=dict)



class Scheduler:
    """状态机骨架:持有当前状态,go() 按转换表校验迁移。"""

    def __init__(self, start: EngineState = EngineState.PLANNING):
        self.state = start

    def go(self, target: EngineState) -> EngineState:
        if not can_transition(self.state, target):
            allowed = ", ".join(s.value for s in sorted(TRANSITIONS[self.state])) or "无"
            raise ValueError(f"非法状态转换: {self.state.value} -> {target.value}(允许: {allowed})")
        self.state = target
        return self.state


class Engine:
    """串行分发主循环:持有 scheduler + blueprint + 各角色桩,按状态分发给下一步。"""

    def __init__(self, planner, executor, evaluator, workspace=None, tools=None,
                 max_cycles=None, max_replans=None, max_stalls=None,
                 max_deadlock_attempts=None, compress=None, context_budget=None,
                 run_token_budget_tokens=None, understander=None, tool_catalog=None,
                 checker=None, subscribers=None, scheduler=None,
                 max_concurrency=1):
        from model_config import get_engine_config
        cfg = get_engine_config()
        self.workspace = workspace or MockWorkspace()
        self.planner = planner
        if hasattr(planner, "workspace"):
            planner.workspace = self.workspace
        if tools:
            self.workspace.set_tools(tools)
        if tool_catalog is not None:
            self.workspace.tool_catalog = tool_catalog
        # 环境检查器:显式传入优先;否则按 tool_catalog 派生(无 catalog → None 全跳过)
        self._checker = checker if checker is not None else (
            SkillEnvProbe(self.workspace.tool_catalog)
            if self.workspace.tool_catalog is not None else None)
        self.executor = executor
        self.evaluator = evaluator
        self.understander = understander or MockTaskUnderstander()
        self._scheduler = scheduler  # 可选执行环境调度器(None=执行器自建沙箱,旧路径)
        # 并行 wave:max_concurrency>1 时一拍拍多个 ready step 并发执行(每步独立容器租约),
        # 取消/重试退化为单步;=1 保持串行原路径(会话级租约,横跨步骤持久)
        self._max_concurrency = max(1, int(max_concurrency or 1))
        self._wave: list[Step] = []              # 当前调度拍的待执行步骤
        self._wave_results: list[tuple] = []     # 执行/评估结果 [(step, ExecResult)]
        self.scheduler = Scheduler()
        self.bp = None
        self.current: Step | None = None
        self.turn: list[EvalEvent] = []
        self._obs: str | None = None
        self.max_cycles = max_cycles if max_cycles is not None else cfg["max_cycles"]
        self.max_replans = max_replans if max_replans is not None else cfg["max_replans"]
        self.max_stalls = max_stalls if max_stalls is not None else cfg["max_stalls"]
        self.max_deadlock_attempts = (max_deadlock_attempts if max_deadlock_attempts is not None
                                      else cfg["max_deadlock_attempts"])
        self.replans = 0
        self._stalls = 0
        self._deadlock_attempts = 0
        self._retry_mode: str | None = None  # 下一步 EXECUTING 的 ctx 档位(raw/compressed),STEP_EVAL 分流后设置
        # 中断原语运行时状态(每 run 在 _run_loop 重建;cancel_current 线程安全请求)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._cancel_evt: asyncio.Event | None = None
        self._cancel_requested = False
        self._cancel_step_id: str | None = None
        self._cancel_reason: str | None = None
        self.task_completed = False
        self.submitted_flag: str | None = None  # 已提交的 flag(执行层报告后填充,审计/反思用)
        self.goals: list[Goal] = []            # 任务理解层下发的固定目标(仅 id)
        self._goal_complete: dict[str, list[str]] = {}  # goal_id → evidence step_ids
        self.fail_reason: str | None = None
        self.run_result: RunResult | None = None
        self._cycle = 0  # 当前调度循环计数
        self._turn_consumed = 0  # 已消费 turn 条目数(防止线性膨胀)
        self._compress = compress              # LLM 语义压缩回调(None=纯机械降级)
        # token 级上下文预算: 构造时传参 > per-role config > 全局 config > 自动计算
        # 自动计算: (model_context_window - model_max_output) × context_budget_ratio
        self._context_budget = context_budget
        self._budget_cfg = cfg.get("context_budget_tokens")      # None / int / dict[role→int|None]
        # 旧 key 兼容(context_budget 标量)
        if self._budget_cfg is None:
            self._budget_cfg = cfg.get("context_budget")
        # 阶段超时配置(None = 不限时)
        self._run_timeout_ms = cfg.get("run_timeout_ms")
        self._phase_timeout_ms: dict[str, int | None] = cfg.get("phase_timeout_ms", {})
        # run 级累计 token 预算上限(None = 不限);_run_tokens 在 _init_run 重置
        self._run_token_budget = (run_token_budget_tokens
                                  if run_token_budget_tokens is not None
                                  else cfg.get("run_token_budget_tokens"))
        # 实例级工具注册表(消除模块级全局变量污染)
        self._tool_registry = ToolRegistry()
        # 事件总线:ctx 组件 + log 都作为 subscriber 接入
        self.signals = SignalBus()
        a = self._assembler()
        if a is not None:
            a.signals = self.signals
            self.signals.subscribe(a)
            if compress is not None:
                a.compress = compress
        log_dir = getattr(self.workspace, "root", None)
        if log_dir is None:
            # MockWorkspace: log 不落盘
            log_dir = None
        self._log = EngineLogger(log_dir)
        self.signals.subscribe(self._log)
        self._stop_requested = False
        self._stop_reason = None
        for sub in subscribers or []:
            self.signals.subscribe(sub)

    def request_stop(self, reason="用户停止"):
        """协作式停跑:主循环下一拍检查后转 FAILED。前端/CLI 预留接口。
        顺带中断当前执行中的步骤(不再等长沙箱跑完),中断路径落 SKIPPED + step_cancel 事件。"""
        self._stop_requested = True
        self._stop_reason = reason
        self.cancel_current(reason)

    def cancel_current(self, reason="中断"):
        """中断当前执行中的步骤(线程安全;并行模式 replan 重建 RUNNING 实例的硬前提)。

        请求落 _cancel_requested + asyncio.Event(经 loop.call_soon_threadsafe 送进
        run 线程);EXECUTING 分支在子任务 wait 上唤醒 → task.cancel 硬中断 → 落
        SKIPPED + step_cancel 事件 + 抑制 step_record。current 为空时空转。
        """
        if self.current is None:
            return
        self._cancel_requested = True
        self._cancel_reason = reason
        self._cancel_step_id = self.current.id
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._cancel_evt.set)

    def _finish_cancelled_step(self):
        """取消落账:SKIPPED + step_cancel 事件 + STEP_ENDED,抑制 step_record(运行时纪律)。"""
        sid, attempts = self.current.id, self.current.attempts
        reason = self._cancel_reason or "中断"
        self.bp.set_status(sid, StepStatus.SKIPPED, force=True)
        ws = self.workspace
        if hasattr(ws, "add_event"):
            ws.add_event(Role.SYSTEM, EventKind.STEP_CANCEL, step_id=sid, reason=reason)
        self.signals.emit(Signal.STEP_ENDED, step_id=sid, verdict="skipped",
                          observation="", attempts=attempts)
        self._cancel_requested = False
        self._cancel_step_id = None
        self._cancel_reason = None
        if self._cancel_evt is not None:
            self._cancel_evt.clear()
        # 状态机约束:EXECUTING 只许转 FAILED/STEP_EVAL。转 STEP_EVAL 由分支守卫
        # 检测 SKIPPED current 跳到 SCHEDULING;request_stop 场景下一拍 _check_stop 即 FAILED。
        self._go(EngineState.STEP_EVAL, f"step {sid} cancelled")

    def _check_stop(self) -> bool:
        """若已 request_stop,转 FAILED 并返回 False。"""
        if not self._stop_requested:
            return True
        self._fail(self._stop_reason or "用户停止")
        return False

    def run(self, raw_content: dict):
        """同步桥:入口保持同步(main/web_server/scripts/测试同步调用点不改),
        内部经 _run_async 在单事件循环内跑完整 async 调度链。"""
        return asyncio.run(self._run_async(raw_content))

    async def _run_async(self, raw_content: dict):
        await self._init_run(raw_content)
        # 事件源合一:run 作用域上下文(run_id),外围/决策链事件经 opslog 自动带归属
        set_run_context(run_id=self.workspace.run_id)
        try:
            self.signals.emit(Signal.RUN_STARTED,
                              task=raw_content, max_cycles=self.max_cycles,
                              max_replans=self.max_replans, max_stalls=self.max_stalls,
                              max_deadlock_attempts=self.max_deadlock_attempts)
            if self._checker is not None:
                rep = await self._probe_manifest_snapshot()
                ctype = (self.raw_content or {}).get("challenge_type")
                if ctype:
                    rep["category"] = self._checker.probe_category(ctype)
                self.signals.emit(Signal.ENV_CHECK, scope="run_start", report=rep)
            await self._run_loop()
            return self.bp
        finally:
            set_run_context(run_id=None)

    async def _probe_manifest_snapshot(self) -> dict:
        """run 起始工具快照:有沙箱调度器时在真实镜像内探测;不可用回退本地 host 探测。

        镜像探测异常(ssh 不可达 / 容器失败 / 输出不可解析)一律回退 host 并标来源,
        不因探测失败中断 run(环境快照是审计辅助,不是前置门禁)。
        """
        ip = getattr(self._scheduler, "image_probe", None) if self._scheduler is not None else None
        if ip is not None:
            script = getattr(self._checker, "manifest_probe_script", None)
            parse = getattr(self._checker, "manifest_from_remote", None)
            if script is not None and parse is not None:
                try:
                    out = await ip(script())
                    if out:
                        return parse(out)
                except Exception:
                    pass
        rep = self._checker.probe_manifest()
        rep["probe"] = "host"
        return rep

    async def _run_loop(self):
        """主循环(初始 run 与 resume 共用):逐拍 _dispatch 直至终态,
        统一收尾 RUN_END + 持久化 + 结果聚合。PLANNING 是第一类 dispatch 状态,
        Scheduler 初始态即 PLANNING,无需 _go 自迁移。"""
        # 中断原语运行时状态:每次 run/resume 独立事件循环,干净重建(防跨 run 残留)
        self._loop = asyncio.get_running_loop()
        self._cancel_evt = asyncio.Event()
        self._cancel_requested = False
        self._cancel_step_id = None
        self._cancel_reason = None
        run_timer = PhaseTimer("run", deadline_ms=self._run_timeout_ms)
        run_timer.__enter__()
        # 串行:run 级会话租约(容器跨步骤持久);并行:每步各自租约,不开 run 级会话
        env_lease = None if self._max_concurrency > 1 else await self._open_env_session()
        try:
            for self._cycle in range(self.max_cycles):
                if self.scheduler.state in (EngineState.DONE, EngineState.FAILED):
                    break
                if not self._check_stop():
                    break
                if not run_timer.check():
                    self.signals.emit(Signal.RUN_TIMEOUT,
                                      elapsed_ms=run_timer.elapsed_ms)
                    self._fail(
                        f"run 全局超时 ({run_timer.elapsed_ms:.0f}ms)")
                    break
                if not self._token_budget_ok():
                    break
                await self._dispatch()
            else:
                self._fail(f"engine 循环超过 {self.max_cycles} 次仍未到终态")
            self.signals.emit(Signal.RUN_END,
                              state=self.scheduler.state.value,
                              fail_reason=self.fail_reason,
                              total_cycles=self._cycle)
            self._persist_run_state()
            self.run_result = self._make_run_result()
        finally:
            if env_lease is not None:
                await self._scheduler.release(env_lease)  # 删容器 + 还连接(会话结束)
            run_timer.__exit__()
            self._log.close()

    async def _open_env_session(self):
        """可选接线:配置了 scheduler 时开一个 actor 会话容器,把受限 handle 注入执行器。

        无 scheduler / 执行器不支持接管 / 无法确定工作目录 → 返回 None,走旧路径
        (执行器自建 SandboxManager)。会话级 lease 跨步骤持有,run 结束 release(删容器)。
        """
        if self._scheduler is None or not hasattr(self.executor, "set_sandbox"):
            return None
        cwd = getattr(self.executor, "allowed_cwd", None)
        if not cwd:
            return None
        req = self._scheduler.requirement_for(actor_id="ex1", cwd=cwd)
        lease = await self._scheduler.acquire(req)
        self.executor.set_sandbox(lease.handle)
        return lease

    async def _init_run(self, raw_content):
        """重置运行态(每次 run 前调用)。

        经 understander(任务理解层输出 API)获取 TaskInput 实例;goal_list 只从这里
        来,不做二次解析。
        """
        self.task_input = await self._safe_call(
            lambda: self.understander.understand(raw_content),
            lambda exc: TaskInput(raw_content=raw_content))
        self.raw_content = self.task_input.raw_content
        self.goals = self.task_input.goal_list
        # 持久化 raw_content 用理解层输出(goals 已剥离),保证 resume 重建的 raw_content
        # 与 live 路径一致;reset() 保留 meta["task"],故先写后清均可
        self.workspace.meta["task"] = self.raw_content
        # 清 workspace 的 run 级残留(blueprint/events/steps/summaries),避免复用实例时
        # ctx 组件投影上一个 run 的 DAG/历史(§5.4);docs/tools 静态配置保留
        self.workspace.reset()
        # 装载精确匹配到的已验证解题经验(ExperienceComponent 投影源;经适配器查询 procedure 库)
        self.workspace.set_experience(self.executor.match_experience())
        self.bp = None
        self.turn = []
        self.current = None
        set_run_context(node_id=None, round=None)  # 清上一步的定位字段
        self._obs = None
        self.replans = 0
        self._stalls = 0
        self._deadlock_attempts = 0
        self._retry_mode = None
        self._wave = []
        self._wave_results = []
        self.task_completed = False
        self.submitted_flag = None
        self.fail_reason = None
        self._goal_complete = {}
        self.scheduler = Scheduler()
        self._cycle = 0
        self._turn_consumed = 0
        self._run_tokens = 0  # per-run 累计 token 用量(§5.1)
        self.run_result = None
        # 注入工具上下文,供 get_doc/get_record 等只读 lookup 使用
        self._tool_registry.set_docs(self.workspace.docs)
        self._tool_registry.set_workspace(self.workspace)

    @classmethod
    def resume(cls, run_id, planner, executor, evaluator,
               root=None, max_cycles=None, max_replans=None,
               max_stalls=None, max_deadlock_attempts=None,
               compress=None, context_budget=None, subscribers=None,
               max_concurrency=1) -> "Engine":
        """从 Workspace.load 恢复引擎并继续 _dispatch 循环。

        恢复内容:
        - ws.blueprint / steps / events / docs / tools / summaries (Workspace.load)
        - scheduler.state / current / fail_reason (ws.meta)
        - replans / _stalls / task_completed / _goal_complete / turn / turn_consumed /
          submitted_flag / run_tokens (ws.proj 事件折叠投影)
        - _deadlock_attempts (ws.meta bootstrap,瞬态计数不事件化)

        不恢复: _obs(最后执行观察,该步重放时重新产生)。
        """
        from agent.workspace import Workspace

        ws = Workspace.load(run_id, root=root)
        engine = cls(planner, executor, evaluator, workspace=ws,
                     max_cycles=max_cycles, max_replans=max_replans,
                     max_stalls=max_stalls,
                     max_deadlock_attempts=max_deadlock_attempts,
                     compress=compress, context_budget=context_budget,
                     subscribers=subscribers, max_concurrency=max_concurrency)
        engine.raw_content = ws.meta.get("task", {})
        engine.task_input = TaskInput(
            raw_content=engine.raw_content,
            goal_list=[Goal(**g) for g in ws.meta.get("goal_list", [])])
        engine.goals = engine.task_input.goal_list
        engine.bp = ws.blueprint

        # 从 meta 恢复运行态
        run_status = ws.meta.get("run_status", "PLANNING")
        current_step_id = ws.meta.get("current_step")
        engine.fail_reason = ws.meta.get("fail_reason")
        # 从事件折叠投影恢复运行态(在终态早返之前,确保 task_completed 等被恢复);
        # 事件流是唯一事实源,resume 不再线性扫事件流
        proj = ws.proj
        engine.replans = proj.replans
        engine._stalls = proj.stalls
        engine.task_completed = proj.task_completed
        engine._goal_complete = dict(proj.goal_complete)
        engine.turn = list(proj.turn)
        engine._turn_consumed = proj.turn_consumed
        engine.submitted_flag = proj.submitted_flag
        engine._deadlock_attempts = int(ws.meta.get("deadlock_attempts", 0) or 0)
        # 续跑续计 token:事件折叠为主(完整),旧 run(无 llm_usage 事件)回退 meta 检查点
        engine._run_tokens = max(proj.run_tokens, ws.meta.get("run_tokens", 0))

        if run_status in ("DONE", "FAILED"):
            # 已经是终态,直接返回
            engine.scheduler = Scheduler(
                EngineState(run_status))
            engine.run_result = engine._make_run_result()
            return engine

        engine.scheduler = Scheduler(EngineState(run_status))
        if current_step_id and engine.bp:
            engine.current = engine.bp.steps.get(current_step_id)

        # 注入工具上下文,供 get_doc/get_record 等只读 lookup 使用
        engine._tool_registry.set_docs(ws.docs)
        engine._tool_registry.set_workspace(ws)

        engine._cycle = 0
        # 续跑:从当前状态继续(共享 _run_loop;resume 不设 run 作用域上下文)
        engine.signals.emit(Signal.RUN_STARTED,
                            task=engine.raw_content,
                            max_cycles=engine.max_cycles,
                            max_replans=engine.max_replans,
                            max_stalls=engine.max_stalls,
                            max_deadlock_attempts=engine.max_deadlock_attempts)
        asyncio.run(engine._run_loop())
        return engine

    @staticmethod
    async def _safe_call(fn, on_error):
        """执行外部 Agent 调用(executor/评估器);异常转成失败信号,不让引擎崩掉。
        兼容同步/异步 fn:返回协程时 await,同步直取。"""
        try:
            r = fn()
            return await r if asyncio.iscoroutine(r) else r
        except Exception as exc:
            return on_error(exc)

    def _phase_deadline(self, phase: str) -> int | None:
        """读取某阶段的超时 deadline(ms);未配置返回 None(不限时)。"""
        return self._phase_timeout_ms.get(phase)

    def _go(self, target: EngineState, reason=""):
        """状态迁移:go + emit signal + persist meta。"""
        prev = self.scheduler.state.value
        self.scheduler.go(target)
        self.signals.emit(Signal.STATE_TRANSITION,
                          from_state=prev, to_state=target.value, reason=reason)
        self._persist_run_state()

    def _persist_run_state(self):
        """持久化运行态到 ws.meta:断点续跑恢复用。"""
        ws = self.workspace
        if ws is None:
            return
        current_step = self.current.id if self.current else None
        ws.meta.update({
            "run_status": self.scheduler.state.value,
            "current_step": current_step,
            "fail_reason": self.fail_reason,
            "run_tokens": getattr(self, "_run_tokens", 0),
            "goal_list": [g.model_dump() for g in self.goals],
            # 瞬态计数不走事件流(纯调度计数器),resume 经 meta bootstrap 恢复
            "deadlock_attempts": getattr(self, "_deadlock_attempts", 0),
        })
        ws.sync()

    def _assembler(self):
        """引擎接线:取 workspace 的 CtxAssembler(生命周期 hook 分发目标)。"""
        return getattr(self.workspace, "assembler", None)

    def _role_budget(self, role) -> int | None:
        """按 role 计算上下文预算(token)。
        优先: 构造传参 → per-role config → 全局标量 config → 自动计算。
        自动计算: (model_context_window - model_max_output) × context_budget_ratio。
        """
        from agent.llm_api import role_model, model_context_window, model_max_output
        from model_config import get_engine_config

        # 构造时显式传参(全局 override)
        if self._context_budget is not None:
            return self._context_budget
        # per-role config
        role_key = role.value if hasattr(role, 'value') else str(role)
        if isinstance(self._budget_cfg, dict):
            val = self._budget_cfg.get(role_key)
            if isinstance(val, int) and val > 0:
                return val
            # None key in dict = auto for this role; fall through
        elif isinstance(self._budget_cfg, int) and self._budget_cfg > 0:
            # 全局标量(旧 key context_budget 或 context_budget_tokens 写整数)
            return self._budget_cfg
        # 自动计算
        cfg = get_engine_config()
        ratio = cfg.get("context_budget_ratio", 0.9)
        model = role_model(role_key)
        window = model_context_window(model)
        max_out = model_max_output(model)
        return int((window - max_out) * ratio)

    async def _assemble_ctx(self, role, budget=None, system=None, **kw) -> str:
        """调用 assembler.assemble 组装上下文;assembler 自带信号发射(若已注入 signals)。
        自动注入 goal_list + compress 回调 + budget,供组件使用。
        system 传给 SystemPromptComponent 渲染(只读投影,供 ctx_asm 日志/信号);
        返回 ctx 正文——system 不进返回串,角色各自把它当系统消息直传 LLM,避免双写。
        """
        a = self._assembler()
        if a is None:
            raise RuntimeError("engine 上下文组装需要 workspace.assembler")
        if "goal_list" not in kw and self.goals:
            kw["goal_list"] = self.goals
        budget = budget if budget is not None else self._role_budget(role)
        fresh = kw.pop("fresh", None)
        if fresh is None:
            # 并行执行:每个 actor 用全新组件实例组装,消除共享实例在异步压缩点的交错竞态
            fresh = role == Role.EXECUTOR and self._max_concurrency > 1
        ctx, _system, over = await a.assemble(role, budget=budget, system=system,
                                              fresh=fresh, **kw)
        # 溢出信号由 assembler 内发射(带 role/overflow/method 完整字段),这里不重复
        return ctx

    def _record_plan(self, reason="", source="", changes=""):
        """规划产出落账:经 assembler.ingest("planner") 反向装填(blueprint + replan 边界事件)。

        ingest 是前向 assemble 的逆:planner 返回写回 workspace(dag 投影源 + replan
        边界事件推进 agent_comm/trace 轮次作用域)。assembler 缺失时直写 workspace。
        reason/source/changes 写入 ReplanDetail,供 history 投影(规划决策链)。
        计划级 plan-note:把规划理由以 pass 级 PLAN_NOTE 落账(agent=planner),进
        agent_comm 供兄弟节点 executor 共享计划意图;replan 边界之前的事件被轮次裁剪。
        """
        a = self._assembler()
        if a is not None:
            a.ingest(Role.PLANNER, blueprint=self.bp, reason=reason,
                     source=source, changes=changes)
            self.signals.emit(Signal.CTX_INGEST, role=Role.PLANNER,
                              detail=f"blueprint ({len(self.bp.steps)} steps) + replan boundary")
        else:
            ws = self.workspace
            if hasattr(ws, "set_blueprint"):
                # set_blueprint 是单一 DAG 写路径(内部发 REPLAN 事件带 DAG 快照)
                ws.set_blueprint(self.bp, reason=reason, source=source, changes=changes)
                ws.sync()
        if reason and reason.strip():
            ws = self.workspace
            if hasattr(ws, "add_event"):
                ws.add_event(Role.PLANNER, EventKind.PLAN_NOTE,
                             verdict=Verdict.PASS, opinion=reason)

    @staticmethod
    def _step_sig(s):
        """用于比对两个 step 是否等价。"""
        return (s.instruction, s.criterion, tuple(s.depends_on), s.skill_id)

    def _log_dag_change(self, prev_bp, reason="", changes=""):
        """向 logger 写入 [dag] 块(planner 的 DAG 变更摘要)。"""
        bp = self.bp
        if bp is None:
            return
        log = self._log

        if prev_bp is None:
            # 初始规划:全部为新增
            added = list(bp.steps)
            parts = [f"+{sid}" for sid in added]
            log.agent_line(Role.PLANNER, "dag",
                           f"{' '.join(parts)}  reason=\"{reason}\"" if reason
                           else ' '.join(parts))
            for sid in added:
                s = bp.steps[sid]
                deps = f"  depends_on={s.depends_on}" if s.depends_on else ""
                log.agent_sub(Role.PLANNER,
                              f"{sid}: \"{s.instruction}\"  criterion=\"{s.criterion}\"{deps}")
            return

        # 修订:比对差异
        prev_ids = set(prev_bp.steps)
        new_ids = set(bp.steps)
        added = sorted(new_ids - prev_ids)
        removed = sorted(prev_ids - new_ids)
        changed = sorted(sid for sid in (prev_ids & new_ids)
                         if self._step_sig(prev_bp.steps[sid]) != self._step_sig(bp.steps[sid]))

        if not added and not removed and not changed:
            log.agent_line(Role.PLANNER, "dag",
                           f"no change  reason=\"{reason}\"" if reason else "no change")
            return

        parts = []
        if added:
            parts.extend(f"+{sid}" for sid in added)
        if removed:
            parts.extend(f"-{sid}" for sid in removed)
        if changed:
            parts.extend(f"~{sid}" for sid in changed)
        log.agent_line(Role.PLANNER, "dag",
                       f"{' '.join(parts)}  reason=\"{reason}\"" if reason
                       else ' '.join(parts))
        for sid in removed:
            s = prev_bp.steps[sid]
            log.agent_sub(Role.PLANNER,
                          f"-{sid}: {s.status.value}, removed")
        for sid in added:
            s = bp.steps[sid]
            deps = f"  depends_on={s.depends_on}" if s.depends_on else ""
            log.agent_sub(Role.PLANNER,
                          f"+{sid}: \"{s.instruction}\"  criterion=\"{s.criterion}\"{deps}")
        for sid in changed:
            old_s = prev_bp.steps[sid]
            new_s = bp.steps[sid]
            changes_parts = []
            if old_s.instruction != new_s.instruction:
                changes_parts.append("instruction")
            if old_s.criterion != new_s.criterion:
                changes_parts.append("criterion")
            if old_s.depends_on != new_s.depends_on:
                changes_parts.append("depends_on")
            if old_s.skill_id != new_s.skill_id:
                changes_parts.append("skill")
            log.agent_sub(Role.PLANNER,
                          f"{sid} ({'+'.join(changes_parts)})")
            if old_s.instruction != new_s.instruction:
                log.agent_sub(Role.PLANNER,
                              f"  instruction: \"{old_s.instruction}\"  \"{new_s.instruction}\"")
            if old_s.criterion != new_s.criterion:
                log.agent_sub(Role.PLANNER,
                              f"  criterion: \"{old_s.criterion}\"  \"{new_s.criterion}\"")
            if old_s.skill_id != new_s.skill_id:
                log.agent_sub(Role.PLANNER,
                              f"  skill: \"{old_s.skill_id}\"  \"{new_s.skill_id}\"")

    def _record_step(self, verdict, is_completed=False):
        """每步验收落账:写 ws.steps + 打 step_record 事件(审计/history 投影)。
        round=当前步骤 attempt,事件编码定位字段与节点一致。"""
        if hasattr(self.workspace, "record_step"):
            self.workspace.record_step(
                self.current.id, verdict, self._obs or "",
                result=self.current.result, attempts=self.current.attempts,
                is_completed=is_completed, status=self.current.status.value,
                round=self.current.attempts)

    def _record_opinion(self, source: EvalSource, res, step_id=None):
        """评估意见落事件流(agent_comm 通道投影源;pass 是闸门,引擎只在非 pass 时调用)。

        经 assembler.ingest(评估角色反向装填)→ ws.record_opinion;SCHEDULING(引擎
        结构检测,无评估角色)或 assembler 缺失时直写 workspace。
        """
        sid = step_id or (self.current.id if self.current else None)
        # 仅 ee(step_eval)的失败分类落账;其它来源意见不含 diagnosis,避免无关噪声
        diagnosis = (getattr(res, "diagnosis", None)
                     if source == EvalSource.STEP_EVAL else None)
        a = self._assembler()
        role = EVAL_ROLE.get(source)
        if a is not None and role is not None:
            a.ingest(role, verdict=res.verdict, opinion=res.opinion,
                     observation=res.observation, step_id=sid,
                     diagnosis=diagnosis)
            self.signals.emit(Signal.CTX_INGEST, role=role,
                              detail=f"record_opinion ({source.value}, verdict={res.verdict.value})")
            return
        if hasattr(self.workspace, "record_opinion"):
            self.workspace.record_opinion(
                source, res.verdict, res.opinion,
                observation=res.observation,
                step_id=sid,
                diagnosis=diagnosis,
            )

    def _step_tool_digest(self, step_id: str, max_calls=40, out_chars=200) -> str:
        """当前步骤 executor 工具调用紧凑摘要:拼进 step_eval 的 observation。

        评估器默认只看到 executor 的观察(如"超 24 轮/350s"),对执行过程两眼一抹黑,
        意见只能给模板话。这里把该步的 use_tool/tool_result 按时间序折叠成摘要喂给评估器,
        让它能基于真实过程给出方向性意见。dict 与 dataclass 两种 detail 都兼容。
        """
        evs = [e for e in self.workspace.events
               if e.step_id == step_id
               and e.kind in (EventKind.USE_TOOL, EventKind.TOOL_RESULT)
               and e.agent == Role.EXECUTOR]
        if not evs:
            return ""
        lines = []
        for e in evs[:max_calls]:
            d = e.detail
            if isinstance(d, dict):
                tool, args, output = d.get("tool", "?"), d.get("args", {}), d.get("output", "")
            else:
                tool = getattr(d, "tool", "?")
                args = getattr(d, "args", {})
                output = getattr(d, "output", "")
            if e.kind == EventKind.USE_TOOL:
                arg_s = json.dumps(args, ensure_ascii=False)
                lines.append(f"#call {tool} {arg_s[:120]}")
            else:
                text = str(output).replace("\n", " ")[:out_chars]
                lines.append(f"#result {tool} -> {text}")
        head = f"# 步骤 {step_id} 工具轨迹({len(evs)} 条)"
        tail = f"(仅列前 {max_calls} 条)" if len(evs) > max_calls else ""
        return "\n".join(x for x in [head, *lines, tail] if x)

    @staticmethod
    def _dag_signature(bp) -> tuple:
        """计划结构签名(不含 status/attempts 等运行态):id+指令+验收标准+依赖+技能绑定。"""
        return tuple(
            (sid, s.instruction, s.criterion, tuple(s.depends_on), s.skill_id)
            for sid, s in bp.steps.items()
        )

    def _state_context(self, source: EvalSource,
                       diagnosis=None) -> StateContext | None:
        """调度器状态注入:组装"本轮为何重规划"的结构化事实。只陈述,不下指令。

        提示词内容(触发解释/状态语义)在 planner 侧维护;引擎只给触发类型与具体事实。
        diagnosis 仅用于 STEP_EVAL 分流:判定步骤目标设计有误时走单节点重设计触发。
        """
        if source == EvalSource.PLAN_REVIEW:
            sc = StateContext(trigger=Trigger.PLAN_REVIEW_FAIL)
        elif source == EvalSource.STEP_EVAL:
            sid = self.current.id if self.current else "?"
            if diagnosis == Diagnosis.PLANNER_TARGET:
                sc = StateContext(
                    trigger=Trigger.STEP_TARGET_REDESIGN,
                    detail=f"步骤 {sid} 目标/验收标准设计有误(诊断 planner_target),仅重设计该步骤",
                )
            else:
                sc = StateContext(
                    trigger=Trigger.STEP_ESCALATED,
                    detail=f"步骤 {sid} 判定升级/失败,处于 ESCALATED",
                )
        elif source == EvalSource.SCHEDULING:
            sc = StateContext(trigger=Trigger.DEADLOCK, detail=self._blocked_report())
        elif source == EvalSource.REFLECT:
            sc = StateContext(trigger=Trigger.REFLECT)
        else:
            return None
        near = (self.replans >= self.max_replans - 1
                or self._stalls >= self.max_stalls - 1
                or self._deadlock_attempts >= self.max_deadlock_attempts)
        if near:
            sc.budget = (
                f"剩余预算:重规划 {self.replans}/{self.max_replans},"
                f"连续无改动 {self._stalls}/{self.max_stalls},"
                f"死锁重排 {self._deadlock_attempts}/{self.max_deadlock_attempts}。"
            )
        return sc

    @staticmethod
    def _patch_summary(prev_bp, new_bp) -> str:
        """重规划前后对比的变更摘要(不含 status/attempts 等运行态)。"""
        def sig(s):
            return (s.instruction, s.criterion, tuple(s.depends_on), s.skill_id)
        prev_ids = set(prev_bp.steps)
        new_ids = set(new_bp.steps)
        added = sorted(new_ids - prev_ids)
        removed = sorted(prev_ids - new_ids)
        changed = sorted(sid for sid in (prev_ids & new_ids)
                         if sig(prev_bp.steps[sid]) != sig(new_bp.steps[sid]))
        parts = []
        if added:
            parts.append("add " + ",".join(added))
        if removed:
            parts.append("remove " + ",".join(removed))
        if changed:
            parts.append("update " + ",".join(changed))
        return "; ".join(parts) or "无改动"

    def _mark_revise(self) -> None:
        """计划评审不过:未完成步骤置 REVISE(待修订),修订意见与状态一并给 planner。"""
        revised = []
        for sid, s in self.bp.steps.items():
            if s.status not in DONE_STATUSES:
                self.bp.set_status(sid, StepStatus.REVISE)
                revised.append(sid)
        if revised:
            self._log.engine_action(
                f"mark_revise  " + "  ".join(f"{sid}=REVISE" for sid in revised))

    def _clear_revise(self) -> list[str]:
        """修订后评审通过:残留 REVISE 步骤回 PENDING(评审通过即视为可执行)。返回被清步骤。"""
        cleared = []
        for sid, s in self.bp.steps.items():
            if s.status == StepStatus.REVISE:
                self.bp.set_status(sid, StepStatus.PENDING)
                cleared.append(sid)
        return cleared

    async def _resolve_stuck(self) -> None:
        self._deadlock_attempts += 1
        report = self._blocked_report()
        self.signals.emit(Signal.DEADLOCK_DETECTED,
                          report=report, deadlock_attempts=self._deadlock_attempts,
                          max_deadlock_attempts=self.max_deadlock_attempts)
        if self._deadlock_attempts > self.max_deadlock_attempts:
            self._fail(f"调度死锁:连续 {self._deadlock_attempts - 1} 次重排仍无可执行步骤")
            return
        await self._replan(EvalSource.SCHEDULING,
                           EvalResult(Verdict.FAIL, report))

    def _blocked_report(self) -> str:
        rows = []
        for sid, s in self.bp.steps.items():
            if s.status in DONE_STATUSES:
                continue
            unmet = [d for d in s.depends_on if self.bp.steps[d].status != StepStatus.PASSED]
            if unmet:
                states = ", ".join(f"{d}:{self.bp.steps[d].status.value}" for d in unmet)
                rows.append(f"{sid}:{s.status.value}(前置未满足: {states})")
            else:
                rows.append(f"{sid}:{s.status.value}")
        return "调度死锁:无 ready 步骤但任务未完成。非终态步骤: " + "; ".join(rows)

    def _fail(self, reason: str) -> None:
        self.fail_reason = reason
        record_error("engine", "run_failed", level=ErrorLevel.FATAL, reason=reason)
        self.signals.emit(Signal.FAILED, reason=reason, replans=self.replans,
                          stalls=self._stalls, deadlock_attempts=self._deadlock_attempts)
        self._go(EngineState.FAILED, reason)

    def _capability_blocked(self) -> bool:
        """能力探测:靶机或沙箱确认不可用(复查一次仍失败)→ True,收口 run。

        持续能力状态(非一次性异常)建模为 probe:失败后先按退避复查一次,
        瞬态故障恢复可解除阻塞继续执行;仍失败则不再让后续步骤盲目空转烧 token。
        """
        if self._probe_target():
            return True
        if self._probe_sandbox():
            return True
        return False

    def _probe_target(self) -> bool:
        ex = getattr(self.executor, "target_blocked", None)
        if not ex or not ex():
            return False
        retry = getattr(self.executor, "retry_target", None)
        if retry is not None:
            retry()  # 按退避再试一次,瞬态 429 恢复后可解除阻塞继续执行
        return bool(ex())

    def _probe_sandbox(self) -> bool:
        runner = getattr(self.executor, "runner", None)
        probe = getattr(runner, "sandbox_blocked", None)
        if probe is None:
            return False
        if not probe():
            return False
        ensure = getattr(runner, "_ensure_sandbox", None)
        if ensure is not None:
            ensure()  # 退避期内不真重试,只是按退避策略再查一次
        return bool(probe())

    def _dag_summary_for_goals(self) -> str:
        """为 goal 评估构造世界模型摘要:DAG 中所有步骤的状态、产物、观察。"""
        if not self.bp:
            return "(无计划)"
        lines = []
        for sid in self.bp.topological_order():
            s = self.bp.steps[sid]
            lines.append(f"[{sid}] status={s.status.value} instruction={s.instruction}")
            if s.result:
                lines.append(f"  result: {s.result}")
        return "\n".join(lines)

    async def _eval_goals_after_pass(self, step_ctx: str):
        """步骤 PASS 后,调用 evaluator 比对未完成 goal 与当前 DAG,引用证据记录完成状态。"""
        if not self.goals:
            return
        incomplete = [g for g in self.goals if g.id not in self._goal_complete]
        if not incomplete:
            return
        dag_summary = self._dag_summary_for_goals()
        raw_goals = [g.model_dump() for g in incomplete]
        try:
            # goal 评估是 ee 的 LLM 子能力(N 个独立调用,内部 asyncio 并发):
            # 包 _llm_wrap 统一记账(逐 goal 协程不 drain 全局 log,聚合用量在此兜底)
            results = await self._llm_wrap(
                Role.EVALUATOR_STEP,
                lambda: self.evaluator.eval_goals(step_ctx, raw_goals, dag_summary),
                ctx_size=count_tokens(step_ctx),
            )
        except Exception as exc:
            self._log.engine_error(f"goal eval 异常: {type(exc).__name__}: {exc}")
            return
        try:
            for gr in results:
                if gr.goal_id not in {g.id for g in self.goals}:
                    continue
                if gr.complete:
                    self._goal_complete[gr.goal_id] = list(gr.evidence)
                # 记录 goal_eval 事件(每个 goal 一条,无论 complete 与否)
                ws = self.workspace
                if hasattr(ws, "add_event"):
                    ws.add_event(
                        Role.EVALUATOR_STEP, EventKind.GOAL_EVAL,
                        goal_id=gr.goal_id, complete=gr.complete,
                        evidence=list(gr.evidence), reasoning=gr.reasoning,
                    )
                # 将 goal 评估意见加入 turn(planner 可感知目标进展)
                self.turn.append(
                    EvalEvent(
                        source=EvalSource.GOAL_EVAL,
                        opinion=gr.reasoning,
                        observation=f"goal_id={gr.goal_id} complete={gr.complete} evidence={list(gr.evidence)}",
                        step_id=self.current.id if self.current else None,
                    )
                )
        except Exception as exc:
            # 外部评估返回的 detail 契约违规(如空 reasoning)不应崩 run,按同路径降级
            self._log.engine_error(f"goal eval 异常: {type(exc).__name__}: {exc}")
            return
        if self._goals_all_complete() and self.submitted_flag:
            # 目标全部达成且已提交 flag(CTF 核心目标=拿到 flag)→ 置位任务完成,
            # 调度器据此早停收口:跳过剩余冗余步骤直接反思终局。
            self.task_completed = True

    def _goals_all_complete(self) -> bool:
        """全部任务目标已被 goal_eval 判为完成(goal_id 均在 _goal_complete)。"""
        return bool(self.goals) and all(g.id in self._goal_complete for g in self.goals)

    async def _llm_wrap(self, role, fn, ctx_size=0):
        """包裹一次外部 agent 调用,前后发 llm_call_start/end + response。

        token 用量: 优先从返回值提取(_extract_usage),其次从 _token_log(pop_token_log),
        覆盖所有 agent 类型(planner/executor/evaluator)。
        """
        from agent.llm_api import pop_token_log

        # 清理可能残留的上次 token 记录
        pop_token_log()

        t0 = time.perf_counter()
        self.signals.emit(Signal.LLM_CALL_START, role=role, ctx_size=ctx_size)
        try:
            result = fn()
            if asyncio.iscoroutine(result):
                result = await result
            ms = int((time.perf_counter() - t0) * 1000)
            # 优先从返回值提取,再从 _token_log 兜底(executor/evaluator 可能不自行上报)
            pt, ct = self._extract_usage(result)
            if pt == 0 and ct == 0:
                usage_log = pop_token_log()
                pt = sum(u["prompt_tokens"] for u in usage_log)
                ct = sum(u["completion_tokens"] for u in usage_log)
            else:
                pop_token_log()  # 已从返回值获取,清理 log 中重复记录
            self._run_tokens += pt + ct   # run 级累计(§5.1 预算上限依据)
            self._emit_llm_usage(role, pt, ct, ms, ctx_size, ok=True)
            self.signals.emit(Signal.LLM_CALL_END, role=role, latency_ms=ms,
                              prompt_tokens=pt, completion_tokens=ct)
            self.signals.emit(Signal.LLM_RESPONSE, role=role, result=result)
            return result
        except Exception as exc:
            ms = int((time.perf_counter() - t0) * 1000)
            pop_token_log()  # 丢弃失败调用的记录
            self._emit_llm_usage(role, 0, 0, ms, ctx_size, ok=False)
            self.signals.emit(Signal.LLM_CALL_END, role=role, latency_ms=ms, error=exc)
            raise

    def _emit_llm_usage(self, role, pt, ct, ms, ctx_size, ok):
        """单次 LLM 调用记账落事件流(llm_usage 事件;run_tokens 的投影源)。

        node_id/round 从执行环境(read opslog ContextVar)取:executor 的 llm_usage
        归到当前 step 与重试轮次;planner/evaluator(非步骤作用域)为空——run 级。
        """
        ws = self.workspace
        if ws is None or not hasattr(ws, "add_event"):
            return
        rc = get_run_context()
        ws.add_event(
            Role.SYSTEM, EventKind.LLM_USAGE,
            role=role.value if hasattr(role, "value") else str(role),
            prompt_tokens=pt, completion_tokens=ct, total_tokens=pt + ct,
            latency_ms=ms, ctx_size=ctx_size, ok=ok,
            node_id=rc.get("node_id"), round=rc.get("round"))

    @staticmethod
    def _extract_flag(res) -> str | None:
        """从执行结果提取已提交的 flag(规范落点:引擎 submitted_flag)。

        优先取执行结果 dict 的 flag 键;否则扫描提交类工具调用(submit_flag 等)的参数。
        未提交返回 None——FlagVerifier 依此判"agent 未提交 flag"。
        """
        result = res.result
        if isinstance(result, dict) and result.get("flag"):
            return str(result["flag"])
        for tc in res.tool_calls or []:
            tool = str(tc.get("tool") or "")
            if tool == "submit_flag" or tool.endswith("flag"):
                args = tc.get("args")
                if isinstance(args, dict) and args.get("flag"):
                    return str(args["flag"])
        return None

    @staticmethod
    def _extract_submission(res) -> dict | None:
        """从执行结果提取提交记录(flag + 平台判定:ok/correct/message)。

        submit_flag 工具调用的 result 是权威判定来源(adapter.submit 返回);
        无提交类调用返回 None。供 engine 写入 ws.meta["submission"],ee/et 经
        SubmissionComponent 投影可见。
        """
        for tc in res.tool_calls or []:
            tool = str(tc.get("tool") or "")
            if tool == "submit_flag" or tool.endswith("flag"):
                args = tc.get("args")
                flag = args.get("flag") if isinstance(args, dict) else None
                sub: dict = {"flag": flag}
                r = tc.get("result")
                if isinstance(r, dict):
                    for k in ("ok", "correct", "message"):
                        if k in r:
                            sub[k] = r[k]
                return sub
        return None

    @staticmethod
    def _extract_usage(result) -> tuple[int, int]:
        """从 agent 返回值中提取 token 用量 → (prompt_tokens, completion_tokens)。"""
        u = None
        if hasattr(result, "meta") and isinstance(result.meta, dict):
            u = result.meta.get("_usage")
        if not u and hasattr(result, "total_usage"):
            u = result.total_usage
        if u:
            return u.get("prompt_tokens", 0), u.get("completion_tokens", 0)
        return 0, 0

    def _token_budget_ok(self) -> bool:
        """run 级累计 token 预算检查:超限则发信号并转 FAILED,返回 False。"""
        if not self._run_token_budget:
            return True
        if self._run_tokens < self._run_token_budget:
            return True
        self.signals.emit(Signal.TOKEN_BUDGET_EXCEEDED,
                          tokens=self._run_tokens, budget=self._run_token_budget)
        self._fail(f"run token 预算超限 ({self._run_tokens} >= {self._run_token_budget} tok)")
        return False

    def _make_run_result(self) -> RunResult:
        """聚合一次 run 的结果:终态/达成标志/预算用量/通过步骤的最终产物(§5.3)。"""
        product = {}
        for sid, sr in self.workspace.steps.items():
            if str(sr.verdict) == "pass":
                product[sid] = sr.result
        return RunResult(
            state=self.scheduler.state.value,
            completed=self.task_completed,
            fail_reason=self.fail_reason,
            replans=self.replans,
            stalls=self._stalls,
            cycles=self._cycle,
            tokens=self._run_tokens,
            product=product,
        )

    # ===== 步骤执行 / 校验(串行与并行 wave 共用) =====

    async def _run_step(self, step) -> ExecResult | None:
        """单步完整执行(EXECUTING 主体)。返回 ExecResult;取消返回 None(已转 STEP_EVAL)。

        并行 wave(max_concurrency>1):每步各自 acquire 容器租约(actor=step.id,跨步骤
        不共享),executor.run 注入该步 runner;取消不接入 wave(下一拍 _check_stop 收口)。
        串行:无租约,用执行器会话 runner(_open_env_session 已注入 handle)。
        """
        self.bp.set_status(step.id, StepStatus.RUNNING)
        step.attempts += 1
        set_run_context(node_id=step.id, round=step.attempts)
        self.signals.emit(Signal.STEP_STARTED, step_id=step.id,
                          attempt=step.attempts, max_attempts=step.max_attempts)
        if self._checker is not None and step.skill_id:
            cat = step.skill_id.split(".")[0]
            rep = {
                "tools": self._checker.probe_tools(list(self.workspace.tools)),
                "category": self._checker.probe_category(cat),
            }
            self.signals.emit(Signal.ENV_CHECK, scope="step",
                              step_id=step.id, report=rep)
        a = self._assembler()
        if a is not None:
            await a.precompress(Role.PLANNER)
        retry_mode = self._retry_mode or "raw"
        self._retry_mode = None
        start_levels = {"history": "index"}     # executor 从索引档看全局台账(共享进度,控预算)
        if retry_mode == "compressed":
            start_levels["trace"] = "summary"
        assemble_kw = {"step_id": step.id, "system": self.executor.system,
                       "start_levels": start_levels}
        if self._max_concurrency > 1:
            assemble_kw["actor"] = step.id      # 并行:每 actor 独立折叠缓存/组件实例
        ctx = await self._assemble_ctx(Role.EXECUTOR, **assemble_kw)
        runner, lease = None, None
        if self._max_concurrency > 1:
            lease = await self._acquire_step_lease(step)
            if lease is None:
                return ExecResult(
                    observation="无法获取执行环境租约(无 Provider 或无法确定工作目录)")
            runner = self._runner_for(lease.handle)
        t = PhaseTimer("executing", deadline_ms=self._phase_deadline("executing"))
        with t:
            # executor 跑子任务 + 主循环 wait cancel 事件:request_stop/cancel_current
            # 触发 → task.cancel 硬中断(含长 ssh.exec),取消路径落 SKIPPED + step_cancel。
            # runner 仅并行 wave 注入(每步独立容器);串行缺省走执行器会话 runner,
            # 调用签名与旧路径一致(不传 runner,兼容测试自定义 run 覆写)。
            exec_kwargs = {"tool_exec": self._tool_registry.call_tool}
            if runner is not None:
                exec_kwargs["runner"] = runner
            exec_task = asyncio.create_task(
                self._safe_call(
                    lambda: self._llm_wrap(Role.EXECUTOR,
                        lambda: self.executor.run(step, ctx, **exec_kwargs),
                        ctx_size=count_tokens(ctx)),
                    lambda exc: ExecResult(
                        observation=f"执行异常: {type(exc).__name__}: {exc}"),
                ))
            if self._max_concurrency > 1:
                res: ExecResult | None = await exec_task   # 并行不接取消(wave 原子拍)
            else:
                cancel_wait = asyncio.create_task(self._cancel_evt.wait())
                done, pending = await asyncio.wait(
                    {exec_task, cancel_wait}, return_when=asyncio.FIRST_COMPLETED)
                for tsk in pending:
                    tsk.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if exec_task in done and not exec_task.cancelled():
                    res = exec_task.result()
                else:
                    res = None
        if res is None:
            if lease is not None:
                await lease.release()
            self._finish_cancelled_step()
            return None
        if t.timed_out:
            self.signals.emit(Signal.PHASE_TIMEOUT, phase="executing",
                              elapsed_ms=t.elapsed_ms, step_id=step.id)
            # 超时只追加标记,不丢弃执行产出:executor 可能已提取/提交 flag,
            # result/submission/tool_calls 是 ee 软鉴定判完成的关键证据。
            note = f"[执行超时({t.elapsed_ms:.0f}ms)]"
            res.observation = f"{res.observation}\n{note}".strip()
        self._obs = res.observation
        step.result = res.result
        submitted = self._extract_flag(res)
        if submitted is not None:
            self.submitted_flag = submitted
        submission = self._extract_submission(res)
        if submission is not None:
            self.workspace.record_submission(submission)
        if a is not None and res.tool_calls:
            a.ingest(Role.EXECUTOR, step_id=step.id, tool_calls=res.tool_calls)
            n = len(res.tool_calls)
            self.signals.emit(Signal.CTX_INGEST, role=Role.EXECUTOR,
                              detail=f"step_id={step.id} tool_calls={n}  {n} use_tool + {n} tool_result  trace 通道")
        if lease is not None:
            await lease.release()
        return res

    async def _run_wave(self):
        """并行 wave:每步独立任务并发执行(各持各的容器租约/ctx),结果按序收集。"""
        async def one(step):
            res = await self._run_step(step)
            return step, res if res is not None else ExecResult(
                observation=f"{step.id}: 执行被中断")
        self._wave_results = list(await asyncio.gather(*(one(s) for s in self._wave)))
        self._go(EngineState.STEP_EVAL,
                 f"wave of {len(self._wave)} steps executed")

    async def _step_eval_wave(self):
        """并行 wave 步骤校验:顺序逐步评估(pass 继续;retry 退化为该步单步重放;
        replan 收口)。中途 abandon 时把未评估的 RUNNING 残留步骤回 PENDING,避免死锁。"""
        last = self._wave_results[-1][0]
        for step, res in self._wave_results:
            self.current = step
            self._obs = res.observation or ""
            set_run_context(node_id=step.id, round=step.attempts)
            action, eval_res = await self._step_eval_one(step)
            if action == "pass":
                if step is not last:
                    continue
                self.current = None
                set_run_context(node_id=None, round=None)
                self._go(EngineState.SCHEDULING, "wave passed")
                return
            # 本拍中止(retry/replan):未评估的 RUNNING 兄弟步骤回 PENDING,等下一拍重调度
            for s, _ in self._wave_results:
                if s is not step and s.status == StepStatus.RUNNING:
                    self.bp.set_status(s.id, StepStatus.PENDING, force=True)
            if action == "retry":
                self._wave = [step]
                self._wave_results = [(step, res)]
                self._go(EngineState.EXECUTING,
                         f"step {step.id} retry {step.attempts}/{step.max_attempts}")
            else:
                await self._replan(EvalSource.STEP_EVAL, eval_res,
                                   scope_step_id=step.id if action == "replan_step" else None)
            return

    async def _step_eval_one(self, step) -> tuple[str, EvalResult]:
        """单个步骤校验(STEP_EVAL 主体)。返回 (action, res),不做状态迁移。

        action: "pass" / "retry"(重试重放) / "replan"(整图重排) / "replan_step"(单步重设计)。
        串行与并行 wave 共用,保证两者判定分支一致。
        """
        ctx = await self._assemble_ctx(
            Role.EVALUATOR_STEP, step_id=step.id,
            system=self.evaluator.system_for(Role.EVALUATOR_STEP))
        digest = self._step_tool_digest(step.id)
        obs_parts = [p for p in (self._obs, digest) if p]
        if obs_parts:
            obs = "\n\n".join(obs_parts).strip()
            ctx = f"{ctx}\n\nobservation: {obs}".strip()
        t = PhaseTimer("step_eval", deadline_ms=self._phase_deadline("step_eval"))
        with t:
            res = await self._safe_call(
                lambda: self._llm_wrap(Role.EVALUATOR_STEP,
                    lambda: self.evaluator.step_eval(ctx), ctx_size=count_tokens(ctx)),
                lambda exc: EvalResult(Verdict.ESCALATE,
                    f"步骤校验异常: {type(exc).__name__}: {exc}"),
            )
        if t.timed_out:
            self.signals.emit(Signal.PHASE_TIMEOUT, phase="step_eval",
                              elapsed_ms=t.elapsed_ms, step_id=step.id)
            res = EvalResult(Verdict.ESCALATE, f"步骤校验超时({t.elapsed_ms:.0f}ms)")
        if res.is_completed:
            self.task_completed = True
            # ee 已判任务达成:同步把未决 goal 置完成,消除报告矛盾
            for g in self.goals:
                self._goal_complete.setdefault(g.id, [])
        diagnosis = getattr(res, "diagnosis", Diagnosis.OTHER)
        if res.verdict == Verdict.RETRY:
            if diagnosis == Diagnosis.PLANNER_TARGET:
                # 目标本身错了:不浪费重试,升级并触发该步骤单节点重设计
                self.bp.set_status(step.id, StepStatus.ESCALATED)
                self._record_step(Verdict.ESCALATE, is_completed=self.task_completed)
                self.signals.emit(Signal.STEP_ENDED, step_id=step.id,
                                  verdict=Verdict.ESCALATE, observation=self._obs or "",
                                  attempts=step.attempts)
                return "replan_step", res
            if step.attempts >= step.max_attempts:
                self.bp.set_status(step.id, StepStatus.ESCALATED)
                self._record_step(Verdict.ESCALATE, is_completed=self.task_completed)
                self.signals.emit(Signal.STEP_ENDED, step_id=step.id,
                                  verdict=Verdict.ESCALATE, observation=self._obs or "",
                                  attempts=step.attempts)
                return "replan", res
            self.bp.set_status(step.id, StepStatus.RETRY)
            self._record_step(Verdict.RETRY, is_completed=self.task_completed)
            self._record_opinion(EvalSource.STEP_EVAL, res, step_id=step.id)
            self.signals.emit(Signal.STEP_ENDED, step_id=step.id,
                              verdict=Verdict.RETRY, observation=self._obs or "",
                              attempts=step.attempts)
            self._log.hint_tick_extra("retry")
            # 漂移重试继承压缩 ctx(旧轨迹摘要,避免被错误路径继续带偏);其余保留原始轨迹
            self._retry_mode = "compressed" if diagnosis == Diagnosis.DRIFT else "raw"
            return "retry", res
        if res.verdict == Verdict.ESCALATE:
            self.bp.set_status(step.id, StepStatus.ESCALATED)
            self._record_step(Verdict.ESCALATE, is_completed=self.task_completed)
            self.signals.emit(Signal.STEP_ENDED, step_id=step.id,
                              verdict=Verdict.ESCALATE, observation=self._obs or "",
                              attempts=step.attempts)
            return ("replan_step" if diagnosis == Diagnosis.PLANNER_TARGET
                    else "replan"), res
        if res.verdict == Verdict.PASS:
            self.bp.set_status(step.id, StepStatus.PASSED)
            self._record_step(Verdict.PASS, is_completed=self.task_completed)
            self.signals.emit(Signal.STEP_ENDED, step_id=step.id,
                              verdict=Verdict.PASS, observation=self._obs or "",
                              attempts=step.attempts)
            # 步骤通过后,评估 goal list:比对未完成 goal 与当前世界模型(DAG)
            await self._eval_goals_after_pass(ctx)
            return "pass", res
        self.bp.set_status(step.id, StepStatus.ESCALATED)
        self._record_step(Verdict.ESCALATE, is_completed=self.task_completed)
        self.signals.emit(Signal.STEP_ENDED, step_id=step.id,
                          verdict=Verdict.ESCALATE, observation=self._obs or "",
                          attempts=step.attempts)
        return "replan", res

    async def _acquire_step_lease(self, step):
        """并行 wave:为单步 acquire 一个容器租约(actor=step.id,隔离)。无 scheduler /
        无法确定 cwd → None(该步以失败观察收场,不拖垮整波)。"""
        if self._scheduler is None:
            return None
        cwd = getattr(self.executor, "allowed_cwd", None)
        if not cwd:
            return None
        req = self._scheduler.requirement_for(actor_id=step.id, cwd=cwd)
        return await self._scheduler.acquire(req)

    def _runner_for(self, handle):
        """用租约 handle 构造该步的 CommandRunner(复用执行器 runner 的时限/输出上限;
        mock 执行器无 runner 时用 CommandRunner 缺省)。"""
        from agent.runner import CommandRunner

        r = getattr(self.executor, "runner", None)
        if r is None:
            return CommandRunner(sandbox=handle)
        return CommandRunner(sandbox=handle, timeout=r.timeout,
                             max_out=r.max_out, max_err=r.max_err)

    async def _dispatch(self):
        s = self.scheduler.state
        if s == EngineState.PLANNING:
            if self.bp is not None:
                self._go(EngineState.PLAN_REVIEW, "resume: plan exists, skip to review")
            else:
                t = PhaseTimer("planning", deadline_ms=self._phase_deadline("planning"))
                with t:
                    await self._do_initial_plan()
                if t.timed_out:
                    self.signals.emit(Signal.PHASE_TIMEOUT, phase="planning",
                                      elapsed_ms=t.elapsed_ms)
                    if self.bp is None:
                        self._fail(
                            f"初始规划超时({t.elapsed_ms:.0f}ms)且无产出")
        elif s == EngineState.PLAN_REVIEW:
            ctx = await self._assemble_ctx(
                Role.EVALUATOR_PLAN,
                system=self.evaluator.system_for(Role.EVALUATOR_PLAN))
            t = PhaseTimer("plan_review", deadline_ms=self._phase_deadline("plan_review"))
            with t:
                res = await self._safe_call(
                    lambda: self._llm_wrap(Role.EVALUATOR_PLAN,
                        lambda: self.evaluator.review(ctx), ctx_size=count_tokens(ctx)),
                    lambda exc: EvalResult(Verdict.FAIL,
                        f"计划评审异常: {type(exc).__name__}: {exc}"),
                )
            if t.timed_out:
                self.signals.emit(Signal.PHASE_TIMEOUT, phase="plan_review",
                                  elapsed_ms=t.elapsed_ms)
                res = EvalResult(Verdict.FAIL, f"计划评审超时({t.elapsed_ms:.0f}ms)")
            if res.verdict == Verdict.FAIL:
                self._mark_revise()
                await self._replan(EvalSource.PLAN_REVIEW, res)
            else:
                cleared = self._clear_revise()
                self.signals.emit(Signal.PLAN_REVIEW_PASS)
                ws = self.workspace
                if ws is not None and hasattr(ws, "add_event"):
                    # REVISE→PENDING 状态迁移事件化(事件源不可见的状态变更补齐)
                    ws.add_event(
                        Role.EVALUATOR_PLAN, EventKind.PLAN_REVIEW_PASS,
                        reason="plan review passed", revised=cleared)
                self._go(EngineState.SCHEDULING, "plan review passed")
        elif s == EngineState.SCHEDULING:
            # 一拍取一批 ready step(并行 wave);max_concurrency=1 时与 next_step() 等价
            wave = self.bp.ready_steps()[: self._max_concurrency]
            if not wave or self.task_completed:
                # ee 已判任务完成(is_completed)→ 早停收口:跳过剩余 DAG 步骤直接反思终局
                if self.task_completed or self.bp.is_done():
                    self._go(EngineState.REFLECTING, "task completed")
                else:
                    await self._resolve_stuck()
            else:
                if self._capability_blocked():
                    self._fail("环境阻塞: 靶机不可达或沙箱不可用(能力探测复查仍失败),后续步骤无法执行")
                    return
                self._wave = wave
                self._wave_results = []
                self.current = wave[0]
                set_run_context(node_id=wave[0].id)
                self._deadlock_attempts = 0
                reason = (f"step {wave[0].id} ready" if len(wave) == 1
                          else f"wave of {len(wave)} steps ready")
                self._go(EngineState.EXECUTING, reason)
        elif s == EngineState.EXECUTING:
            if self._max_concurrency > 1:
                await self._run_wave()
            else:
                if (self._cancel_requested and self.current is not None
                        and self.current.id == self._cancel_step_id):
                    # 步骤刚调度即被取消(如 SCHEDULING 与 EXECUTING 之间 request_stop):不启动执行
                    self._finish_cancelled_step()
                    return
                res = await self._run_step(self.current)
                if res is None:
                    return  # 取消路径已转 STEP_EVAL
                self._wave_results = [(self.current, res)]
                self._go(EngineState.STEP_EVAL,
                         f"step {self.current.id} executed")
        elif s == EngineState.STEP_EVAL:
            if self._max_concurrency > 1:
                await self._step_eval_wave()
            elif self.current is not None and self.current.status.value == "SKIPPED":
                # 被取消的步骤:SKIPPED 即终态,跳过评估直接回调度(取消路径经 _finish_cancelled_step 转入)
                self.current = None
                set_run_context(node_id=None, round=None)
                self._go(EngineState.SCHEDULING, "step cancelled")
                return
            else:
                action, res = await self._step_eval_one(self.current)
                if action == "pass":
                    self.current = None
                    set_run_context(node_id=None, round=None)
                    self._go(EngineState.SCHEDULING, "step passed")
                elif action == "retry":
                    self._go(EngineState.EXECUTING,
                             f"step {self.current.id} retry {self.current.attempts}/{self.current.max_attempts}")
                elif action == "replan_step":
                    await self._replan(EvalSource.STEP_EVAL, res,
                                       scope_step_id=self.current.id)
                else:
                    await self._replan(EvalSource.STEP_EVAL, res)
        elif s == EngineState.REFLECTING:
            ctx = await self._assemble_ctx(
                Role.EVALUATOR_TASK,
                system=self.evaluator.system_for(Role.EVALUATOR_TASK))
            t = PhaseTimer("reflecting", deadline_ms=self._phase_deadline("reflecting"))
            with t:
                res = await self._safe_call(
                    lambda: self._llm_wrap(Role.EVALUATOR_TASK,
                        lambda: self.evaluator.reflect(ctx),
                        ctx_size=count_tokens(ctx)),
                    lambda exc: EvalResult(Verdict.REPLAN,
                        f"反思异常: {type(exc).__name__}: {exc}"),
                )
            if t.timed_out:
                self.signals.emit(Signal.PHASE_TIMEOUT, phase="reflecting",
                                  elapsed_ms=t.elapsed_ms)
                res = EvalResult(Verdict.REPLAN, f"反思超时({t.elapsed_ms:.0f}ms)")
            if res.verdict == Verdict.DONE:
                await self._replan(EvalSource.REFLECT, res, to_done=True)
            else:
                await self._replan(EvalSource.REFLECT, res, to_done=False)

    async def _do_initial_plan(self):
        """执行初始规划(PLANNING 状态的无 bp 分支):调用 planner → 记录 → 进入评审。"""
        self.bp = await self._safe_call(
            lambda: self._llm_wrap(Role.PLANNER,
                lambda: self.planner.plan(
                    PlannerInput(mode=PlannerMode.INITIAL,
                                 task_input=self.task_input)
                )),
            lambda exc: self._planner_failure(exc),
        )
        if self.scheduler.state == EngineState.FAILED:
            return
        if self.bp is None:
            self._fail("初始规划失败: planner 返回 None")
            return
        reason = self.bp.meta.get("reason", "") if self.bp else ""
        self._record_plan(reason=reason, source="", changes="")
        self._log_dag_change(None, reason=reason)
        self._go(EngineState.PLAN_REVIEW, "initial plan done")

    def _planner_failure(self, exc) -> None:
        """Planner LLM 调用失败:记录原因并转 FAILED 终态。"""
        self._fail(f"Planner LLM 调用失败: {type(exc).__name__}: {exc}")
        return None

    async def _replan(self, source: EvalSource, res, to_done=False,
                      scope_step_id=None) -> None:
        sid = self.current.id if self.current else None
        self.turn.append(
            EvalEvent(source=source, opinion=res.opinion,
                      observation=res.observation, step_id=sid,
                      diagnosis=getattr(res, "diagnosis", None))
        )
        self._record_opinion(source, res)
        self.replans += 1
        prev_bp = self.bp
        prev_sig = self._dag_signature(prev_bp)
        self.signals.emit(Signal.REPLAN_START, source=source.value,
                          turn_count=len(self.turn),
                          dag_step_count=len(prev_bp.steps))
        self._log.hint_tick_extra(
            f"replan #{self.replans}/{self.max_replans} stalls={self._stalls}")
        self._go(EngineState.PLANNING, f"replan triggered by {source.value}")
        self.signals.emit(Signal.REPLAN)
        pin = PlannerInput(
            mode=PlannerMode.REVISE,
            task_input=self.task_input,
            feedback=Feedback(
                dag=prev_bp.to_dict(),
                turn=list(self.turn[self._turn_consumed:]),
                state_context=self._state_context(
                    source, diagnosis=getattr(res, "diagnosis", None)),
                scope_step_id=scope_step_id,
            ),
        )
        self._turn_consumed = len(self.turn)
        t = PhaseTimer("planning", deadline_ms=self._phase_deadline("planning"))
        with t:
            self.bp = await self._safe_call(
                lambda: self._llm_wrap(Role.PLANNER,
                    lambda: self.planner.plan(pin)),
                lambda exc: self._planner_failure(exc),
            )
        if t.timed_out:
            self.signals.emit(Signal.PHASE_TIMEOUT, phase="planning",
                              elapsed_ms=t.elapsed_ms)
            if self.bp is None:
                self.bp = prev_bp  # 超时无产出,回退到旧计划
        if self.scheduler.state == EngineState.FAILED:
            return
        reason = self.bp.meta.get("reason", "") if self.bp else ""
        changes = self._patch_summary(prev_bp, self.bp)
        self._record_plan(reason=reason, source=source.value, changes=changes)
        self._log_dag_change(prev_bp, reason=reason, changes=changes)
        self.current = None
        set_run_context(node_id=None, round=None)
        if self._dag_signature(self.bp) == prev_sig:
            self._stalls += 1
        else:
            self._stalls = 0
        self.signals.emit(Signal.REPLAN_END, reason=reason, changes=changes,
                          stalls=self._stalls,
                          new_step_count=len(self.bp.steps) if self.bp else 0,
                          replans=self.replans)
        if not to_done and (self.replans >= self.max_replans
                            or self._stalls >= self.max_stalls):
            self.signals.emit(Signal.OSCILLATION_RISK, replans=self.replans,
                              stalls=self._stalls,
                              max_replans=self.max_replans,
                              max_stalls=self.max_stalls)
            self._fail(
                f"疑似振荡:重规划 {self.replans} 次"
                f"(连续无改动 {self._stalls} 次)超限中止"
            )
            return
        self._go(EngineState.DONE if to_done else EngineState.PLAN_REVIEW,
                 "reflect done" if to_done else "replan done")
