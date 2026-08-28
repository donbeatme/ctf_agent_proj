"""工作记忆:Workspace(本地全量存储 + 索引化历史)+ MockWorkspace(非持久化)。

设计见 design/workspace.md §3/§4。
- Workspace:每个 run 一个实例,自管 runs/<run_id>/ 目录(state.json / events.jsonl)。
  全量历史(uuid-keyed 事件流)本地存储,按 agent/step/kind/时间查询;**只存不解析**,
  语义判断交给各 Agent。压缩只改渲染,不改存储——账本读 events.jsonl/state.json 原文。
- MockWorkspace:非持久化(不落盘),供测试和引擎默认占位。
"""

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from model_config import get as _cfg
from opslog import emit as _emit_ops

from agent.blueprint import Blueprint
from agent.ctx import (
    AgentCommComponent,
    CtxAssembler,
    DagComponent,
    DocsComponent,
    ExperienceComponent,
    HistoryComponent,
    SubmissionComponent,
    SystemPromptComponent,
    TaskComponent,
    ToolComponent,
    ToolDirectoryComponent,
    TraceComponent,
)
from agent.projections import Projection, apply as _apply_proj, replay as _replay_proj
from agent.schema import (
    EVENT_SCHEMA, EventKind, Role, SOURCE_AGENT, StepResult, normalize_event_detail,
)

_RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"

# 已验证经验可见范围(env 开关,默认仅 ee):CTF_EXPERIENCE_SCOPE=ee|agent|et|all|none,逗号可组合。
# 测试模式默认 ee —— agent 新鲜解题,不靠已验证 procedure 走捷径;ee 拿经验当软鉴定参照核对。
_EXP_ROLE_TOKEN = {
    Role.EXECUTOR: "agent",
    Role.EVALUATOR_STEP: "ee",
    Role.EVALUATOR_TASK: "et",
}


def _exp_scope() -> set[str]:
    return {s.strip().lower() for s in _cfg("CTF_EXPERIENCE_SCOPE", "ee").split(",") if s.strip()}


def _exp_enabled(role: Role) -> bool:
    """该角色是否装配 ExperienceComponent。none/未列出 → 不装配。
    "ee" 默认覆盖评估侧(ee 步骤验收 + et 任务反思)两处软鉴定参照;"both" 等价 ee+et。"""
    scope = _exp_scope()
    if "all" in scope:
        return True
    if "both" in scope:
        scope = scope - {"both"} | {"ee", "et"}
    token = _EXP_ROLE_TOKEN.get(role)
    if token is None:
        return False
    if "ee" in scope and token in ("ee", "et"):
        return True
    return token in scope


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class Event:
    """一条历史记录(审计/上下文投影的原子单元)。uuid 为全局索引。

    node_id / round 是事件编码的定位字段(与 opslog canonical 流的字段对齐):
    node_id=DAG 步骤 id(等价 step_id),round=执行轮次(步骤 attempt 或工具调用轮)。
    兼容旧账本:缺省 None,Workspace.load 读旧 events.jsonl 行不受影响。
    """

    uuid: str
    agent: str | None    # 哪个 agent 发的;system = 引擎系统行为(如调度器置 ready)
    kind: str            # step_record | verdict | replan | use_tool | tool_result | ...
    step_id: str | None = None
    verdict: str | None = None
    detail: dict = field(default_factory=dict)
    ts: str = ""
    node_id: str | None = None
    round: int | None = None


class Workspace:
    """每个 run 一份:本地全量存储 + 索引化历史。

    - 状态:meta / blueprint / steps / env_state / docs / events / summaries
    - events 追加写 events.jsonl(即时落盘,审计流不依赖 sync)
    - sync() 全量写 state.json(原子替换)
    - load() 从 state.json + events.jsonl 重建,供引擎断点续跑
    - 只存不解析:查询只是过滤/拼文本,不做语义判断
    """

    def __init__(self, run_id, root=None):
        self.run_id = run_id
        self.root = Path(root) / run_id if root else _RUNS_DIR / run_id
        self.meta = {"run_id": run_id, "task": {}, "created_at": _now(), "run_status": "PLANNING"}
        self.proj = Projection()
        self.blueprint: Blueprint | None = None
        self.steps: dict[str, StepResult] = self.proj.steps   # 纯投影缓存(add_event 时折叠)
        self.env_state: dict = {}
        self.docs: dict[str, str] = {}           # 技能库检索出的参考文档 {doc_id: 文本}
        self.tools: dict[str, dict] = {}         # 活动工具集(统一形式 {id: {description, parameters}};动态按需注入,默认空)
        self.tool_catalog = None                 # 运行时静态工具目录加载器引用(apply_tool 校验用;**不持久化**)
        self.summaries: dict = {}                # 语义压缩摘要缓存 {f"{role}:{key}": {"text":..., "passes":...}}
        self.experience: list[dict] = []         # 已匹配的解题经验(engine 启动时从 procedure 库装载)
        self.events: list[Event] = []
        self._persist = True                     # MockWorkspace 关掉,不落盘
        self.assembler = CtxAssembler(self)
        # 角色组件表(design/workspace.md §5.1/§7):组件是 workspace 只读投影,引擎只改
        # workspace;trace/ac 按 replan 边界投影当轮,history 投影全局 step_record。
        # ep/ex/ee/et 表已注册;engine 已接线:外部 agent 上下文经 assembler.assemble
        # 组装(前向),返回经 ingest 装回(反向)。
        # 懒加载:组件类在首次 assemble 时才实例化,避免 Workspace 构造时预创建 28 个实例。
        self.assembler.register_class(
            Role.PLANNER,
            SystemPromptComponent,
            TaskComponent,
            AgentCommComponent,
            DagComponent,
            HistoryComponent,
            DocsComponent,
            ToolDirectoryComponent,
            ToolComponent,
            TraceComponent,
        )
        self.assembler.register_class(
            Role.EVALUATOR_PLAN,
            SystemPromptComponent,
            TaskComponent,
            DagComponent,
            HistoryComponent,
        )
        self.assembler.register_class(
            Role.EXECUTOR,
            SystemPromptComponent,
            TaskComponent,
            AgentCommComponent,
            DagComponent,
            HistoryComponent,
            DocsComponent,
            ToolDirectoryComponent,
            ToolComponent,
            *((ExperienceComponent,) if _exp_enabled(Role.EXECUTOR) else ()),
            (TraceComponent, (), {"agent": Role.EXECUTOR}),
        )
        self.assembler.register_class(
            Role.EVALUATOR_STEP,
            SystemPromptComponent,
            TaskComponent,
            SubmissionComponent,
            AgentCommComponent,
            DagComponent,
            HistoryComponent,
            *((ExperienceComponent,) if _exp_enabled(Role.EVALUATOR_STEP) else ()),
        )
        self.assembler.register_class(
            Role.EVALUATOR_TASK,
            SystemPromptComponent,
            TaskComponent,
            SubmissionComponent,
            AgentCommComponent,
            DagComponent,
            HistoryComponent,
            *((ExperienceComponent,) if _exp_enabled(Role.EVALUATOR_TASK) else ()),
        )

    # ===== 生命周期 =====

    @classmethod
    def create(cls, run_id, task, meta=None, root=None) -> "Workspace":
        """引擎启动 run 时新建:初始化 meta 并落初始检查点。"""
        ws = cls(run_id, root)
        ws.meta.update({"task": task, "run_status": "PLANNING"})
        ws.meta.update(meta or {})
        ws.sync()
        return ws

    @classmethod
    def load(cls, run_id, root=None) -> "Workspace":
        """从 runs/<run_id>/ 重建实例(断点续跑)。

        事件流(events.jsonl)是唯一事实源:steps / blueprint / 计数器从事件重放重建;
        崩溃窗口(REPLAN 事件丢失但 state.json 有快照)回退 state.json 的 blueprint。
        """
        ws = cls(run_id, root)
        ws._ensure_dir()
        st = json.loads(ws._read("state.json"))
        ws.meta = st.get("meta") or {}
        ws.env_state = st.get("env_state") or {}
        ws.docs = st.get("docs") or {}
        ws.tools = st.get("tools") or {}
        ws.summaries = st.get("summaries") or {}
        ws.experience = st.get("experience") or []
        raw = []
        for line in ws._read_lines("events.jsonl"):
            try:
                raw.append(Event(**json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                pass  # 丢弃损坏行(崩溃残留半行),不阻塞整个 run 的恢复
        for e in raw:
            e.detail = normalize_event_detail(e.kind, e.detail)
        ws.events = raw
        goal_ids = [g.get("id", "") for g in (ws.meta.get("goal_list") or [])]
        ws.proj = _replay_proj(raw, goal_ids=goal_ids)
        if ws.proj.blueprint is None and st.get("blueprint"):
            # 崩溃窗口:REPLAN 事件(含 dag 快照)丢失,回退 state.json 的 blueprint
            ws.proj.blueprint = Blueprint.from_dict(st["blueprint"])
        ws.blueprint = ws.proj.blueprint
        ws.steps = ws.proj.steps
        return ws

    def sync(self):
        """检查点落盘:state.json(原子替换)。events 已在 add_event 即时落盘。
        非持久化(MockWorkspace)跳过写盘,仅内存态。"""
        if not self._persist:
            return
        self._ensure_dir()
        self._atomic_write("state.json", {
            "meta": self.meta,
            "blueprint": self.blueprint.to_dict() if self.blueprint else None,
            "steps": {sid: asdict(sr) for sid, sr in self.steps.items()},
            "env_state": self.env_state,
            "docs": self.docs,
            "tools": self.tools,
            "summaries": self.summaries,
            "experience": self.experience,
        })

    # ===== 状态接口 =====

    def reset(self):
        """重置 run 级状态(复用同一实例跑新 run 时调用):清 blueprint/events/steps/summaries/env_state。
        保留 docs/tools 等静态配置与 meta.run_id。持久化实例同步清空 events.jsonl 并写空 state.json。
        """
        self.blueprint = None
        self.proj = Projection()
        self.steps = self.proj.steps
        self.env_state = {}
        self.summaries = {}
        self.experience = []
        self.events = []
        self.meta["run_status"] = "PLANNING"
        self.meta.pop("submission", None)   # run 级提交状态,新 run 不残留
        if self._persist:
            self._ensure_dir()
            with (self.root / "events.jsonl").open("w", encoding="utf-8") as fh:
                fh.write("")
            self._atomic_write("state.json", {
                "meta": self.meta,
                "blueprint": None,
                "steps": {},
                "env_state": {},
                "docs": self.docs,
                "tools": self.tools,
                "summaries": {},
                "experience": [],
            })

    def set_blueprint(self, bp: Blueprint, reason="", source="", changes="",
                      dag_snapshot=None) -> Event:
        """规划产出 / 补丁合并后写入当前 DAG(单一 DAG 写路径)。

        物化聚合:引擎 live path 直接改 bp 对象,这里只登记物化实例;同时发一条带
        DAG 快照的 REPLAN 事件——事件流是唯一事实源,重放靠快照重建 DAG。
        调用方不再额外 add_event(避免双写);reason/source/changes 经此透传。
        """
        self.blueprint = bp
        return self.add_event(
            Role.PLANNER, EventKind.REPLAN, reason=reason, source=source, changes=changes,
            dag=dag_snapshot if dag_snapshot is not None else (bp.to_dict() if bp else None),
        )

    def record_submission(self, info: dict):
        """记录 executor 提交 flag 后的判定(正确/错误/仅记录/异常),供 ee/et 组件投影。

        事件化:写 submission 事件(事实源),折叠由 add_event→apply 完成
        (proj.submission / submitted_flag 已是权威 correct 优先的结果);
        meta["submission"] 保留供 SubmissionComponent 投影(兼容期),镜像 proj。
        """
        self.add_event(
            Role.EXECUTOR, EventKind.SUBMISSION,
            flag=str(info.get("flag") or ""),
            ok=info.get("ok"),
            correct=info.get("correct"),
            message=info.get("message"),
        )
        p = self.proj.submission or {}
        self.meta["submission"] = {
            "flag": str(p.get("flag") or ""),
            "ok": p.get("ok"),
            "correct": p.get("correct"),
            "message": p.get("message"),
        }

    def record_step(self, step_id, verdict, observation="", *, result=None, attempts=0,
                    is_completed=False, status=None, agent=Role.EVALUATOR_STEP, **kw) -> StepResult:
        """每步执行+验收后落账:写 steps(投影折叠)+ 追加一条 step_record 事件。

        step_record 是 history 唯一投影源(design/workspace.md §7),事件 detail 需携带
        该步的 observation / result / attempts / is_completed / status——观察、产物与
        验收时 DAG 状态进事件流,历史投影与 resume 才读得到。ws.steps 由投影维护。
        """
        self.add_event(agent, EventKind.STEP_RECORD, step_id=step_id, verdict=verdict,
                       observation=observation, result=result or {},
                       attempts=attempts, is_completed=is_completed, status=status, **kw)
        if step_id in self.proj.steps:
            return self.proj.steps[step_id]
        # 取消抑制(step_cancel 后旧实例迟到记录):折叠不落投影,返回构造产物满足调用方契约
        return StepResult(step_id=step_id, verdict=verdict, observation=observation,
                          result=result or {}, attempts=attempts, is_completed=is_completed)

    def set_env(self, key, val):
        self.env_state[key] = val

    def get_env(self, key, default=None):
        return self.env_state.get(key, default)

    def set_doc(self, doc_id, text):
        self.docs[doc_id] = text

    def get_doc(self, doc_id):
        return self.docs.get(doc_id)

    def set_experience(self, records):
        """装载匹配到的解题经验(procedure 记录列表);空/None 清空。"""
        self.experience = list(records or [])

    def get_experience(self):
        return list(self.experience)

    def get_tool(self, tool_id):
        """取统一形式的工具定义 dict({description, parameters});不存在返回 None。"""
        return self.tools.get(tool_id)

    def get_tool_description(self, tool_id, default=""):
        """取工具一句话描述(目录投影用)。"""
        return self.tools.get(tool_id, {}).get("description", default)

    def set_tools(self, tools):
        """整表注入工具目录(**统一接口**):只接受标准工具定义列表(OpenAI function-calling /
        MCP),经 ToolComponent.normalize 归一到统一形式后合并。本地 {id: 描述} 映射不在
        此接口——先把它转成标准格式再传。engine 启动时由调用方注入,run 内静态。
        """
        self.tools.update(ToolComponent.normalize(tools))

    def add_tools(self, specs) -> dict[str, dict]:
        """按需注入工具到活动集(**apply_tool 经此增长 ws.tools**):标准工具定义列表归一并
        并入,不覆盖已有条目。返回新增的归一映射。"""
        added = ToolComponent.normalize(specs)
        for tid, td in added.items():
            self.tools.setdefault(tid, td)
        return added

    def remove_tools(self, tool_ids) -> list[str]:
        """从活动集移除工具(**remove_tool 经此收缩 ws.tools**):幂等,不存在的 id 忽略。"""
        removed = [tid for tid in tool_ids if tid in self.tools]
        for tid in removed:
            self.tools.pop(tid, None)
        return removed

    def record_tool_call(self, step_id, tool, args=None, agent=Role.EXECUTOR, **kw) -> Event:
        """工具调用轨迹落账:追加 kind="use_tool" 事件(trace 的 ut 半段,agent 的决定)。"""
        kw.update(tool=tool, args=args or {})
        return self.add_event(agent, EventKind.USE_TOOL, step_id=step_id, **kw)

    def record_tool_result(self, step_id, tool, output, *, args=None, agent=Role.EXECUTOR,
                           **kw) -> Event:
        """工具调用结果落账:追加 kind="tool_result" 事件(trace 的 tr 半段,世界的响应)。

        工具结果存事件流(非独立存储),TraceComponent 按最近一次 replan 事件
        之后的 use_tool/tool_result 投影为本轮轨迹。args 缺省为空 dict,输出过长由调用方先剪。
        """
        kw.update(tool=tool, args=args or {}, output=output)
        return self.add_event(agent, EventKind.TOOL_RESULT, step_id=step_id, **kw)

    def record_opinion(self, source, verdict, opinion, observation=None, step_id=None,
                       **kw) -> Event:
        """评估意见落账:追加 kind=source.value 的 opinion 事件(agent 通信/审计)。

        意见是"当前对话返回文本",agent_comm 按最近一次 replan 之后的非 pass 意见投影;
        pass 是闸门(不产出内容),由 engine 在非 pass 时调用。source 为 EvalSource,
        agent 取对应评估角色(ep/ee/et;SCHEDULING 为引擎结构检测,agent=system)。
        """
        agent = SOURCE_AGENT[source]
        verdict = verdict.value if hasattr(verdict, "value") else verdict
        diagnosis = kw.pop("diagnosis", None)
        diagnosis = diagnosis.value if hasattr(diagnosis, "value") else diagnosis
        if diagnosis is not None:
            kw["diagnosis"] = diagnosis
        return self.add_event(agent, source.value, step_id=step_id, verdict=verdict,
                              opinion=opinion, observation=observation, **kw)

    # ===== 索引化历史 =====

    def add_event(self, agent, kind, step_id=None, verdict=None, **kw) -> Event:
        """追加一条历史记录:生成 uuid,内存 + events.jsonl 即时落盘。

        agent="system" 标记引擎系统行为(调度器置 ready 等);ctx 渲染时过滤,
        审计/断点续跑仍保留原文。非持久化(MockWorkspace)仅内存态,不写 events.jsonl。
        detail 按 EVENT_SCHEMA 校验:已注册 kind → dataclass(漏字段/类型错当场炸);
        未注册 kind → 退化 dict 通道(日志警告)。

        事件源合一:持久化实例把该决策链事件镜像进 opslog canonical 流(domain=ws,
        kind 原样),带 node_id(默认=step_id)与 round(可经 kw 传入,如工具调用轮次);
        events.jsonl 是断点续跑的投影,不重复成源。node_id/round 也落在 Event 顶层。
        """
        node_id = kw.pop("node_id", None)
        rnd = kw.pop("round", None)
        schema = EVENT_SCHEMA.get(kind)
        if schema is not None:
            detail = schema(**kw)
        else:
            detail = kw
        if node_id is None:
            node_id = step_id
        ev = Event(uuid=uuid.uuid4().hex, agent=agent, kind=kind,
                   step_id=step_id, verdict=verdict, detail=detail, ts=_now(),
                   node_id=node_id, round=rnd)
        self.events.append(ev)
        _apply_proj(self.proj, ev, len(self.events) - 1)
        if self._persist:
            d = detail if isinstance(detail, dict) else asdict(detail)
            _emit_ops("ws", kind, run_id=self.run_id,
                      agent=str(agent) if hasattr(agent, "value") else agent,
                      step_id=step_id, verdict=verdict, node_id=node_id, round=rnd,
                      **{k: v for k, v in d.items()
                         if k not in ("run_id", "node_id", "round", "domain", "event",
                                      "ts", "seq", "agent", "step_id", "verdict", "dag")})
            self._ensure_dir()
            with (self.root / "events.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(ev), ensure_ascii=False) + "\n")
                fh.flush()
        return ev

    def ingest_external(self, kind: str, rec: dict) -> Event:
        """外围 ops 事件投影进事件账本(events.jsonl):canonical 已由 opslog.emit
        写入,这里只落投影,不 re-emit。供 main._ops_sink 转发 adapter/sandbox/ssh
        等事件,使 run 账本包含跨域链路。node_id/round 从 canonical 记录落到
        Event 顶层(step_id 映射 node_id,支持按步查询)。
        """
        d = {k: v for k, v in rec.items()
             if k not in ("seq", "ts", "run_id", "node_id", "round",
                          "domain", "event", "_uuid")}
        ev = Event(uuid=rec.get("_uuid") or uuid.uuid4().hex,
                   agent=Role.SYSTEM, kind=kind,
                   step_id=rec.get("node_id"),
                   verdict=rec.get("verdict"),
                   detail=d, ts=rec.get("ts") or _now(),
                   node_id=rec.get("node_id"), round=rec.get("round"))
        self.events.append(ev)
        if self._persist:
            self._ensure_dir()
            with (self.root / "events.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(ev), ensure_ascii=False) + "\n")
                fh.flush()
        return ev

    def get_record(self, uuid_str) -> Event | None:
        """按索引取全文(展开压缩投影用);不存在返回 None。"""
        return next((e for e in self.events if e.uuid == uuid_str), None)

    def query(self, agent=None, step_id=None, kind=None, verdict=None, time_range=None) -> list[Event]:
        """按 agent / step_id / kind / verdict / 时间窗过滤,返回原序列表。

        time_range=(start, end),闭区间,两端可 None(不限)。
        ts 为 "%Y-%m-%d %H:%M:%S" 定宽字符串,字典序即时间序。
        """
        evs = self.events
        if agent is not None:
            evs = [e for e in evs if e.agent == agent]
        if step_id is not None:
            evs = [e for e in evs if e.step_id == step_id]
        if kind is not None:
            evs = [e for e in evs if e.kind == kind]
        if verdict is not None:
            evs = [e for e in evs if e.verdict == verdict]
        if time_range is not None:
            start, end = time_range
            evs = [e for e in evs
                   if (start is None or e.ts >= start) and (end is None or e.ts <= end)]
        return evs

    # ===== 持久化内部 =====

    def _ensure_dir(self):
        self.root.mkdir(parents=True, exist_ok=True)

    def _read(self, name):
        f = self.root / name
        if not f.exists():
            raise KeyError(f"run {self.run_id} 缺少 {name}")
        return f.read_text(encoding="utf-8")

    def _read_lines(self, name):
        f = self.root / name
        if not f.exists():
            return []
        return [ln for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def _atomic_write(self, name, data):
        tmp = self.root / f".{name}.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.root / name)


class MockWorkspace(Workspace):
    """mock 工作记忆:不落盘,供测试和引擎默认占位。"""

    def __init__(self, run_id="mock"):
        super().__init__(run_id)
        self._persist = False
