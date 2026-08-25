"""上下文组装:CtxComponent(组件基类)+ CtxAssembler(组装器)+ 10 个组件。

设计见 design/ctx.md;状态源与注册表见 design/workspace.md。

**投影模型(关键不变量)**:组件是 workspace 的**只读投影**,不持独立数据副本。
数据唯一真值在 workspace(ws.blueprint / ws.events / ws.docs 注册表),引擎只改
workspace;每次 assemble 组件从 workspace 现投影,**改 workspace 即自动反映,无需
手动喂数据**——不存在"更新 dag 又更新 dag 组件"的双重写入。组件自己只剩瞬态
压缩档位 level(每次 assemble 重置)。

生命周期:
- 作用域  create / delete        进出该 role 的作用域
- 内容    update / clear         内容增长 / 清空但组件保留
- 持久化  sync                   内容写回 workspace 存储
- 渲染压缩 render / advance_level / can_advance  按档位拼文本 / 溢出推进一档

生命周期事件(引擎打点,组装器机械分发):on_replan / on_plan_review_pass / on_step_record /
on_run_end。**每个组件子类声明自己的生命周期**(system/task 永存、AgentComm 重规划、
Docs 计划评审通过即清),组装器只机械执行。

压缩元数据:
- anchor    锚点组件(task/agent通信/系统提示词):永不压——can_advance 恒 False,
  且排除在 LLM 可压与机械候选之外(显式声明,不靠 priority/LEVELS 碰运气)
- priority  压缩保护顺序,越小越先压(优先级阶梯见 design/ctx.md §4)
- floor     不可再压的原文量下限(token);at_floor() 判断是否已到下限
- LEVELS    压缩档位;LEVELS=("raw",) 即单档,can_advance 恒 False → 永不压
- target    "ctx" 上下文正文(受预算压缩)/ "system" 并入系统提示词(永不压)
- compress_methods  该组件可用的"按需压缩方式"说明(喂给压缩 LLM 的提示词;空 = 不可压,保留原文)

压缩 API(溢出时,design/ctx.md §5):
- 注入 compress 回调 (prompt: str, content: str) -> str(可用更便宜模型)
- 溢出**优先 LLM 压缩**:组装器把超预算内容 + 压缩提示词(压缩目的 / 优先级 / 占比 /
  当前触发压缩的 agent 目的 / 按需压缩方式)交给 LLM,LLM 决定怎么压、压到多少
- 未注入 compress 或 LLM 输出超限 → **机械降级**(advance_level 逐档:索引替换 / 骨架化 /
  摘要),确定性兜底:不再付 LLM 往返,摘要档只读 precompress 预热缓存,压无可压返回 over 信号

判重:组装器 _join 跨组件**按行判重**,重复行只保留第一个出现——双渲染(同数据源被
多组件投影 / 同数据两通道进 ctx)的兜底,组件各自保持原文语义,不做一刀切过滤。
"""

import json

from agent.evaluator import Verdict
from agent.llm_api import count_tokens
from agent.schema import (
    EvalSource, EventKind, OpinionDetail, ReplanDetail, Role,
    Signal, StepRecordDetail, ToolCallDetail, ToolResultDetail,
)


class CtxComponent:
    """上下文组件基类。子类覆盖生命周期/渲染方法,压缩元数据可经构造参数或类属性覆盖。"""

    key: str = ""
    priority: int = 0
    floor: int = 0
    LEVELS: tuple[str, ...] = ("raw",)
    target: str = "ctx"          # "ctx" 上下文正文 / "system" 系统提示词(不进预算)
    _role: str = ""              # 注册时由组装器写入(摘要缓存 key 作用域)
    compress_methods: str = ""   # 按需压缩方式(压缩 LLM 提示词用;空 = 不可压,保留原文)
    anchor: bool = False         # 锚点组件:永不压(can_advance 恒 False + 排除在压缩候选外)

    def __init__(self, key=None, *, priority=None, floor=None, target=None):
        if key is not None:
            self.key = key
        if priority is not None:
            self.priority = priority
        if floor is not None:
            self.floor = floor
        if target is not None:
            self.target = target
        self.level = 0          # 当前压缩档位索引(0 = raw;每次 assemble 重置)
        self._ws = None         # create 时注入的 workspace(投影数据源)
        self._created = False

    # ===== 作用域生命周期 =====

    def create(self, ws, **kw) -> "CtxComponent":
        """进入作用域:注入 workspace / 从 kw 投影本轮输入。子类覆盖。"""
        self._ws = ws
        self._created = True
        return self

    def delete(self) -> "CtxComponent":
        """离开作用域:释放引用。子类覆盖。"""
        self._created = False
        self._ws = None
        return self

    @property
    def created(self) -> bool:
        return self._created

    # ===== 生命周期事件(引擎打点,组装器机械分发;子类声明自己的行为) =====

    def on_replan(self, **kw) -> None:
        """每轮重规划后调用(AgentComm 清空本轮;其余忽略)。"""

    def on_plan_review_pass(self, **kw) -> None:
        """计划评审通过后调用(Docs 清空文档注册表;其余忽略)。"""

    def on_step_record(self, **kw) -> None:
        """每步验收落账后调用(投影模型下无需喂数据,默认无操作)。"""

    def on_run_end(self, **kw) -> None:
        """run 收尾调用:默认释放组件(子类可在 delete 前做额外收尾)。"""
        self.delete()

    # ===== 内容生命周期 =====

    def update(self, **kw) -> None:
        """内容增长(投影模型下已废弃,数据在 workspace;保留给非投影子类)。"""

    def clear(self) -> None:
        """内容清空但组件保留(agent通信 每轮清 / docs 计划评审通过清)。"""
        self.level = 0

    # ===== 持久化 =====

    def sync(self, ws) -> None:
        """把内容写回 workspace 存储(投影模型下无独立内容,默认无操作)。"""

    # ===== 渲染 / 压缩 =====

    def render(self) -> str:
        """按当前档位拼文本。基类空实现,子类覆盖。"""
        return ""

    def size(self) -> int:
        """当前渲染文本的 token 数(溢出压缩的度量)。"""
        return count_tokens(self.render())

    def can_advance(self) -> bool:
        """能否再压一档:锚点永不压;其余未到最高档 且 未到下限(再压就是假信息)。"""
        if self.anchor:
            return False
        if self.level >= len(self.LEVELS) - 1:
            return False
        return not self.at_floor()

    def precompress(self, ws, compress=None) -> None:
        """预热语义压缩缓存(engine 在非决策时刻调用);不改变档位。基类无操作。"""
        del ws, compress  # 投影模型下仅需语义压缩的组件(如 history)覆盖

    def advance_level(self) -> bool:
        """推进一档压缩;已到最高档返回 False。溢出时由组装器逐档调用。"""
        if self.level >= len(self.LEVELS) - 1:
            return False
        self.level += 1
        return True

    def at_floor(self) -> bool:
        """是否已压到下限(当前渲染 ≤ floor,不可再压)。"""
        return self.size() <= self.floor

    def __repr__(self) -> str:
        return f"<{type(self).__name__} key={self.key} level={self.level}/{len(self.LEVELS)-1}>"


class CtxAssembler:
    """上下文组装器:机械执行组件声明的生命周期;数据真值在 workspace,组件是只读投影。

    - register(role, *comps):每个 role 一份组件表(组装时按注册序)
    - assemble(role, budget, protect, purpose, **kw):重置档位 → create → 拼 ctx → 溢出压缩 → 拼 system
      返回 (ctx, system, over);over = 压缩后仍超预算的字符数(0 = 收进预算;压无可压时返回信号)
    - dispatch(event, **kw):生命周期事件广播,逐组件调 on_<event>
    - clear(scope):按 role 重置组件内容(组件保留;scope=None 清全部)
    - precompress(role):预热某 role 组件的语义压缩缓存(engine 在非决策时刻调用),不改变档位

    压缩 API(溢出时):
    - 注入 compress 回调 (prompt: str, content: str) -> str(可用更便宜模型)
    - 溢出**优先 LLM 压缩**:把超预算内容 + 压缩提示词(压缩目的 / 优先级 / 占比 /
      当前触发压缩的 agent 目的 / 按需压缩方式)交给 LLM,LLM 决定怎么压
    - 未注入或 LLM 超限 → 机械降级(advance_level 逐档),确定性兜底:不重付 LLM,
      摘要档只读预热缓存,压无可压返回 over 信号
    """

    def __init__(self, ws=None, compress=None):
        self._ws = ws
        self.compress = compress   # 注入的语义压缩回调;None = 无 LLM 压缩(溢出走机械降级)
        self._registry: dict[str, list[CtxComponent]] = {}
        self._class_registry: dict[str, list] = {}  # 懒加载:组件类在首次 assemble 时才实例化
        self._last_compression: list[dict] | None = None  # 最近一次压缩记录(供 logger 取用)
        self.signals = None  # 可选 SignalBus(引擎注入后,assemble 自动 emit CTX_ASSEMBLED)

    def register(self, role, *comps) -> "CtxAssembler":
        comps = list(comps)
        for c in comps:
            c._role = role
        self._registry.setdefault(role, []).extend(comps)
        return self

    def register_class(self, role, *specs) -> "CtxAssembler":
        """注册组件类(懒加载):specs 为 CtxComponent 子类或 (cls, args, kwargs) 元组。
        组件在首次 assemble 该 role 时才实例化,避免 Workspace/MockWorkspace 构造时
        预先创建全部 28 个组件实例。
        """
        self._class_registry.setdefault(role, []).extend(specs)
        return self

    def _materialize(self, role):
        """将 _class_registry 中的组件类实例化并移到 _registry。"""
        specs = self._class_registry.pop(role, [])
        if not specs:
            return
        instances = []
        for spec in specs:
            if isinstance(spec, type):
                instances.append(spec())
            else:
                cls, args, kwargs = spec
                instances.append(cls(*args, **kwargs))
        self.register(role, *instances)

    def components(self, role=None) -> list[CtxComponent]:
        """某 role 的组件表;role=None 返回全部(按注册序)。
        首次访问时触发懒加载:从 _class_registry 实例化组件类。
        """
        if role is not None:
            if role in self._class_registry:
                self._materialize(role)
            return list(self._registry.get(role, []))
        # role=None:先 materialize 所有 role
        for r in list(self._class_registry):
            self._materialize(r)
        return [c for comps in self._registry.values() for c in comps]

    def component_state(self, role) -> list[dict]:
        """返回某 role 的组件状态快照:供 logger 记录 ctx_asm 明细。"""
        return [
            {"key": c.key, "level": c.LEVELS[c.level], "size": c.size(), "target": c.target}
            for c in self.components(role)
        ]

    def dispatch(self, event, **kw) -> None:
        """生命周期事件分发:逐组件调 on_<event>(基类默认无操作,子类声明自己的行为)。"""
        for c in self.components():
            getattr(c, f"on_{event}")(**kw)

    # SignalBus 订阅者接口:每个信号 → dispatch 到组件
    def on_replan(self, **kw):
        self.dispatch("replan", **kw)

    def on_plan_review_pass(self, **kw):
        self.dispatch("plan_review_pass", **kw)

    def on_run_end(self, **kw):
        self.dispatch("run_end", **kw)

    def clear(self, scope=None) -> None:
        for c in self.components(scope):
            c.clear()

    def precompress(self, role) -> None:
        """预热某 role 组件的语义压缩缓存(engine 在非决策时刻调用);不改变档位。

        提前把 history 的 delta 折成摘要并落盘,决策时 assemble 命中摘要档只读缓存,
        零 LLM 往返。需要压缩 api 的组件覆盖 precompress;其余基类无操作。
        """
        for c in self.components(role):
            c.precompress(self._ws, self.compress)

    def ingest(self, role, **returns) -> None:
        """模型的返回装填回存储(反向通道):把本轮 LLM 输出按角色契约落账。

        assemble(store→ctx) 是前向投影(组件只读);ingest(ctx→store) 是反向投影,
        把模型的返回写进 workspace——planner 落 blueprint+replan 边界、executor 落
        trace 轨迹 + dag.step.result、评估落 agent_comm 意见。写盘细节归 workspace
        (组件不持数据副本),组装器只做机械分发。
        """
        handler = getattr(self, f"_ingest_{role}", None)
        if handler is None:
            raise ValueError(f"未知 role 的返回装填: {role}")
        handler(**returns)

    def _ingest_planner(self, blueprint=None, reason="", source="", changes="", **kw):
        """planner 返回 → 当前 DAG:写 blueprint + 推进 replan 边界(本轮意见落回 history)。"""
        ws = self._ws
        if ws is None:
            return
        if blueprint is not None:
            ws.set_blueprint(blueprint)
        ws.add_event(Role.PLANNER, EventKind.REPLAN,
                     reason=reason, source=source, changes=changes)
        ws.sync()

    def _ingest_executor(self, step_id=None, tool_calls=None, result=None, **kw):
        """executor 返回 → trace 通道(use_tool + tool_result 落账)+ dag.step.result(报告结果,供 ee)。

        工具调用条目契约(design/contracts.md §2):{"tool", "args", "result"};
        容错别名 name/output(OpenAI 风格),result 缺省只记 use_tool(工具无输出)。
        """
        ws = self._ws
        if ws is None:
            return
        for call in tool_calls or []:
            tool, args, output, rnd = self._tool_call_fields(call)
            ws.record_tool_call(step_id, tool, args, round=rnd)
            if output is not None:
                ws.record_tool_result(step_id, tool, output, args=args, round=rnd)
        if result is not None and step_id and ws.blueprint is not None:
            step = ws.blueprint.steps.get(step_id)
            if step is not None:
                step.result = result

    @staticmethod
    def _tool_call_fields(call) -> tuple:
        """归一工具调用条目 → (tool, args, output, round):dict 取 tool/name、args、
        result/output、round(事件编码定位字段,可缺省);对象取同名属性。"""
        if isinstance(call, dict):
            return (call.get("tool", call.get("name")),
                    call.get("args", {}),
                    call.get("result", call.get("output")),
                    call.get("round"))
        return (getattr(call, "tool", None),
                getattr(call, "args", {}),
                getattr(call, "result", getattr(call, "output", None)),
                getattr(call, "round", None))

    def _ingest_evaluator_plan(self, **kw):
        self._ingest_eval(EvalSource.PLAN_REVIEW, **kw)

    def _ingest_evaluator_step(self, **kw):
        self._ingest_eval(EvalSource.STEP_EVAL, **kw)

    def _ingest_evaluator_task(self, **kw):
        self._ingest_eval(EvalSource.REFLECT, **kw)

    def _ingest_eval(self, source, verdict=None, opinion="", observation=None,
                     step_id=None, **kw):
        """评估返回 → agent_comm 通道(record_opinion;pass 是闸门,非 pass 进 ctx)。"""
        ws = self._ws
        if ws is None or source is None or verdict is None:
            return
        ws.record_opinion(source, verdict, opinion,
                          observation=observation, step_id=step_id)

    def assemble(self, role, budget=None, protect=None, purpose=None, **kw):
        """组装某 role 的上下文。

        - 每个组件重置档位(level=0)→ create 投影本轮输入/workspace 状态(压缩 api 经 kw 注入)
        - target="ctx" 拼正文,超预算则溢出压缩:LLM 优先,机械降级兜底(protect 保护点名 key)
        - target="system" 拼系统提示词,永不压、不进 ctx 预算
        - purpose: 当前触发压缩的 agent 目的(默认取 ROLE_PURPOSE,可覆盖)
        返回 (ctx, system, over)。
        """
        ctx_comps, sys_comps = [], []
        for c in self.components(role):
            c.level = 0
            c.create(self._ws, compress=self.compress, **kw)
            (sys_comps if c.target == "system" else ctx_comps).append(c)
        ctx = self._join(ctx_comps)
        over = 0
        if budget is not None and count_tokens(ctx) > budget:
            ctx = self._compress_overflow(ctx_comps, budget, protect, role, purpose)
            over = max(0, count_tokens(ctx) - budget)
        # 若引擎注入了 signals,自动发射上下文组装事件
        if self.signals is not None:
            sys_text = self._join(sys_comps)
            total = count_tokens(ctx) + count_tokens(sys_text)
            comps_state = [
                {"key": c.key, "level": c.LEVELS[c.level], "size": c.size(), "target": c.target}
                for c in ctx_comps + sys_comps
            ]
            self.signals.emit(Signal.CTX_ASSEMBLED, role=role,
                              total_tokens=total, budget=budget or 0, overflow=over,
                              components=comps_state, system_tokens=count_tokens(sys_text))
            if over:
                self.signals.emit(Signal.CTX_OVERFLOW, role=role, overflow=over,
                                  method="mechanical")
            if self._last_compression:
                self.signals.emit(Signal.CTX_COMPRESSED, role=role,
                                  method="mechanical", compressed=self._last_compression,
                                  total_after=total, overflow_after=over)
                self._last_compression = None
        return ctx, self._join(sys_comps), over

    # ===== 压缩内部 =====

    def _join(self, comps) -> str:
        """拼 ctx:跨组件按行判重,重复行只保留第一个出现。

        双渲染兜底(同数据源被多组件投影/同数据两通道进入 ctx 时),
        不去修改组件语义做一刀切过滤——保持原文,仅去重。
        """
        seen = set()
        blocks = []
        for c in comps:
            text = c.render()
            if not text:
                continue
            lines = []
            for ln in text.split("\n"):
                if ln.strip() and ln in seen:
                    continue  # 判重:重复行只保留第一个
                if ln.strip():
                    seen.add(ln)
                lines.append(ln)
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def _size(self, comps) -> int:
        return count_tokens(self._join(comps))

    def _compress_overflow(self, comps, budget, protect, role, purpose) -> str:
        """溢出压缩:注入 compress 则 LLM 压缩,否则/超限走机械降级。返回压缩后的 ctx 文本。"""
        if self.compress is not None:
            result = self._compress_llm(comps, budget, protect, role, purpose)
            if result is not None and count_tokens(result) <= budget:
                return result
        return self._compress_mechanical(comps, budget, protect)

    def _compress_mechanical(self, comps, budget, protect) -> str:
        """机械降级:按"优先级低 + 占比大"逐档推进(索引替换/骨架化/摘要),确定性兜底。

        收敛确定:档位有限且单调,同输入同输出,不振荡。全部到下限仍超预算不硬压,
        由调用方读 over 调大预算或报错。
        """
        protect = set(protect or ())
        compressed = []
        while self._size(comps) > budget:
            cand = [c for c in comps if not c.anchor and c.key not in protect and c.can_advance()]
            if not cand:
                break
            cand.sort(key=lambda c: (c.priority, -c.size()))
            old_level = cand[0].LEVELS[cand[0].level]
            old_size = cand[0].size()
            cand[0].advance_level()
            new_level = cand[0].LEVELS[cand[0].level]
            compressed.append({
                "key": cand[0].key,
                "from_level": old_level,
                "to_level": new_level,
                "delta": old_size - cand[0].size(),
            })
        self._last_compression = compressed if compressed else None
        return self._join(comps)

    def _compress_llm(self, comps, budget, protect, role, purpose):
        """把超预算内容 + 压缩提示词交给 LLM 压缩。

        - 可压部分(声明了 compress_methods 且未受保护)→ 送 LLM 压缩
        - 保留部分(受保护 / 不可压如 task、agent_comm)→ 原文保留,与 LLM 输出拼回
        - LLM 输出 + 保留原文仍超预算 → 返回 None,交给机械降级
        """
        protect = set(protect or ())
        compressible = [c for c in comps
                        if not c.anchor and c.key not in protect
                        and c.compress_methods and c.render()]
        kept = [c for c in comps if (c.anchor or not c.compress_methods) and c.render()]
        if not compressible:
            return None
        total = self._size(comps)
        prompt = self._build_compress_prompt(role, purpose, budget, total, compressible, kept)
        content = "\n\n".join(f"===== {c.key} =====\n{c.render()}" for c in compressible)
        try:
            result = self.compress(prompt, content)
        except Exception:  # 压缩失败 → 兜底机械
            return None
        kept_text = "\n\n".join(part for part in (c.render() for c in kept) if part)
        return f"{kept_text}\n\n{result}".strip() if kept_text else result

    def _build_compress_prompt(self, role, purpose, budget, total, compressible, kept) -> str:
        """压缩提示词:压缩目的 / 优先级 / 占比 / 当前触发压缩的 agent 目的 / 按需压缩方式。"""
        agent_purpose = purpose or ROLE_PURPOSE.get(role, f"{role} 正在做决策,需要保住决策必需信息")
        lines = [
            f"上下文超出预算:当前 {total} token,需压缩到 {budget} token 内。",
            '只输出压缩后的可压部分内容;标为"保留原文"的部分不得删改。',
            "",
            "# 压缩目的",
            f"{role} 的上下文超预算,压缩以适配预算,同时保住该角色决策必需的信息。",
            "",
            "# 当前触发压缩的 agent 目的",
            agent_purpose,
            "",
            "# 压缩优先级(数值越小越先压)",
        ]
        for c in sorted(compressible, key=lambda c: c.priority):
            lines.append(f"- {c.key} (priority={c.priority})")
        lines.append("")
        lines.append("# 占比")
        for c in compressible:
            size = count_tokens(c.render())
            pct = size * 100 // total if total else 0
            lines.append(f"- {c.key}: {size} token ({pct}%)")
        lines.append("")
        lines.append("# 按需压缩方式")
        for c in compressible:
            lines.append(f"- {c.key}: {c.compress_methods}")
        if kept:
            lines.append("")
            lines.append("# 保留原文(不得删改)")
            lines.append(", ".join(c.key for c in kept))
        return "\n".join(lines)


# 各角色默认"当前触发压缩的 agent 目的"(assemble 可用 purpose 覆盖)
ROLE_PURPOSE = {
    Role.PLANNER: "正在规划/修订计划:需要当前 DAG、历史证据、本轮评估意见与任务目标。",
    Role.EXECUTOR: "正在执行某步:需要该步 instruction/criterion、相关技能文档与环境状态。",
    Role.EVALUATOR_STEP: "正在验收某步:需要该步 instruction/criterion、该步执行观察与工作结果。",
    Role.EVALUATOR_TASK: "正在任务反思:需要全 DAG、各步结果与失败/卡点信息。",
    Role.EVALUATOR_PLAN: "正在评审计划:需要当前 DAG 全貌与执行历史证据,判断计划可否执行。",
}


# ===== planner 组件(design/workspace.md §7) =====
# 全部为 workspace 只读投影:不持独立数据副本,改 workspace 即自动反映。


class SystemPromptComponent(CtxComponent):
    """系统提示词:契约 base + 状态上下文渲染(触发原因/状态语义/预算)。

    每次 plan() 重建,只带本轮触发;永不压、不进 ctx 预算(target="system")。
    渲染文本由 planner 预先拼好经 kw["system"] 传入(契约文案归 planner 单一持有)。
    """

    key = "system_prompt"
    priority = 99
    target = "system"
    anchor = True

    def create(self, ws, **kw):
        super().create(ws, **kw)
        self._system = kw.get("system", "")
        return self

    def delete(self):
        self._system = ""
        return super().delete()

    def render(self):
        return self._system


class TaskComponent(CtxComponent):
    """任务输入:raw_content(原始内容)+ goal_list(任务理解层输出)。run 内不变,全局保留。

    goal_list 只接受 Goal 对象(understander 产出)。LEVELS 单档(raw)→ can_advance
    恒 False → 永不压。
    """

    key = "task"
    priority = 99
    anchor = True

    def create(self, ws, **kw):
        super().create(ws, **kw)
        self._raw_content = kw.get("raw_content")
        self._goal_list = kw.get("goal_list") or []
        return self

    def delete(self):
        self._raw_content = None
        self._goal_list = []
        return super().delete()

    def render(self):
        parts = []
        if self._goal_list:
            items = [f"- [{g.id}]" for g in self._goal_list]
            parts.append("# 任务目标\n" + "\n".join(items))
        if self._raw_content:
            parts.append("# 任务\n" + json.dumps(self._raw_content, ensure_ascii=False, indent=2))
        return "\n\n".join(parts)


class AgentCommComponent(CtxComponent):
    """本轮评估意见(agent通信):最近一次 replan 之后非 pass 意见的只读投影。

    意见由评估 Agent 产出,engine 经 ws.record_opinion 落账进事件流(kind =
    source.value:plan_review/step_eval/reflect/scheduling);**pass 是闸门**
    (不产出内容),非 pass(FAIL/RETRY/ESCALATE/REPLAN)才进 ctx。作用域从
    事件流推导(最近一次 replan 之后),不持瞬态——断点续跑后边界照样可推导。

    生命周期:on_replan 清空本轮(每轮重规划后只留本轮)。
    """

    key = "agent_comm"
    priority = 98          # 锚点,不压
    anchor = True
    _KINDS = (EventKind.PLAN_REVIEW, EventKind.STEP_EVAL, EventKind.REFLECT, EventKind.SCHEDULING)

    def _cut(self) -> int:
        """最近一次 replan 事件的事件列表索引;无 replan 则 -1(渲染全部)。"""
        cut = -1
        if self._ws is not None:
            for i, e in enumerate(self._ws.events):
                if e.kind == EventKind.REPLAN:
                    cut = i
        return cut

    def _events(self):
        ws = self._ws
        if ws is None:
            return []
        return [e for e in ws.events[self._cut() + 1:]
                if e.kind in self._KINDS and e.verdict != Verdict.PASS]

    def on_replan(self, **kw):
        self.clear()

    def clear(self):
        super().clear()

    def render(self):
        evs = self._events()
        if not evs:
            return ""
        lines = []
        for i, e in enumerate(evs):
            d = e.detail
            opinion = d.opinion if isinstance(d, OpinionDetail) else ""
            obs = d.observation if isinstance(d, OpinionDetail) else None
            step = f"  step={e.step_id}" if e.step_id else ""
            lines.append(f"[{i}] {e.kind}{step}: {opinion}")
            if obs:
                lines.append(f"    观察: {obs}")
        return "# 本轮评估意见\n" + "\n".join(lines)


class SubmissionComponent(CtxComponent):
    """提交状态(公用组件):executor 提交 flag 后的平台判定。

    workspace.meta["submission"] 的只读投影:{flag, ok, correct, message}。
    判定来自 adapter.submit(正确/错误/仅记录/异常)。**提交判定是 ee 判任务完成
    (is_completed)的核心证据**——ee 看到 correct=true 即可认定"该步产出已被平台
    确认",故注册给 ee/et;executor 自己的判定经 TraceComponent(tool_result)已可见。
    锚点组件永不压。
    """

    key = "submission"
    priority = 99
    anchor = True

    def render(self):
        sub = (self._ws.meta or {}).get("submission") if self._ws else None
        if not isinstance(sub, dict) or not sub.get("flag"):
            return ""
        correct = sub.get("correct")
        if correct is True:
            verdict = "正确(平台确认)"
        elif correct is False:
            verdict = "错误(平台拒绝)"
        elif sub.get("ok") is False:
            verdict = "未判定(提交异常)"
        else:
            verdict = "未判定(仅记录,无平台确认)"
        lines = ["# 已提交 flag", f"flag: {sub.get('flag')}", f"判定: {verdict}"]
        msg = sub.get("message")
        if msg:
            lines.append(f"message: {msg}")
        return "\n".join(lines)


class DagComponent(CtxComponent):
    """当前计划 DAG:ws.blueprint 的只读投影(不持副本)。step 带 doc_id。

    优先级最低(数据组件里最后压):raw 全文 → skeleton 骨架化。
    骨架化硬约束:保 id / status / attempts / depends_on 边,只切 instruction/criterion
    散文——apply_patch 对真实 blueprint 合并,模型看不见被压掉的 id 就不会非法引用。
    """

    key = "dag"
    priority = 5            # 决策核心最后压(在 trace/history/docs/tools 之后)
    LEVELS = ("raw", "skeleton")
    compress_methods = "骨架化:只保留 step id + status + attempts + depends_on 边,去掉 instruction/criterion 散文"

    def create(self, ws, **kw):
        super().create(ws, **kw)
        self._step_id = kw.get("step_id")   # 给定则只投影该步(executor 的 dag.step)
        return self

    def delete(self):
        self._step_id = None
        return super().delete()

    def render(self):
        bp = self._ws.blueprint if self._ws else None
        if bp is None:
            return ""
        if self._step_id is not None:
            return self._render_step(bp)
        if self.level == 0:
            return "# 当前计划\n" + json.dumps(bp.to_dict(), ensure_ascii=False, indent=2)
        rows = [
            f"{sid}\t{s.status.value}\tattempts={s.attempts}\tdepends_on={s.depends_on}"
            + (f"\tskill={s.skill_id}" if s.skill_id else "")
            for sid, s in bp.steps.items()
        ]
        return "# 当前计划(骨架)\n" + "\n".join(rows)

    def _render_step(self, bp) -> str:
        s = bp.steps.get(self._step_id)
        if s is None:
            return ""
        lines = [
            f"# 当前步骤 {s.id}",
            f"instruction: {s.instruction}",
            f"criterion: {s.criterion}",
            f"status: {s.status.value} attempts: {s.attempts}",
        ]
        if s.skill_id:
            lines.append(f"skill: {s.skill_id}")
        return "\n".join(lines)


class HistoryComponent(CtxComponent):
    """执行历史(run 的账本轨迹):ws.events 里 step_record + replan 的只读投影。

    history = 全局 step_record 轨迹(哪步过了/重试/升级,含观察与产物)+ replan 边界。
    工具调用轨迹(trace)与评估意见(agent_comm)走各自通道,不进 history。
    按 verdict 折叠:只有 PASS 证据(verdict="pass")可压——原文 → uuid 引用 → LLM 摘要;
    其余(失败/升级/评审/重规划等非 PASS)永远保留原文,逐条透传。折叠的选择由
    verdict 决定,与事件位置/数量无关。

    档位:raw(全文)→ index(PASS 换 uuid 引用,get_record 可展开;其余原文)→
    summary(PASS 折成 LLM 摘要;其余原文)。

    语义压缩(摘要档):
    - 注入:assemble/precompress 经 ws/compress 注入 _compress 回调 (prompt: str, content: str) -> str
    - 增量:只把"自上次折叠以来新出现的 PASS 事件"发给 compress;折叠标记 _folded_passes
      (已折入摘要的 PASS 条数)持久化到 ws.summaries——跨 replan / 断点续跑不重付 LLM;
      非 PASS 事件不进折叠,永远从原文渲染
    - 降级:未注入 compress → can_advance 停在索引档,渲染索引,不装假摘要;
      注入但自标记以来有新 PASS 事件(需新鲜折叠)→ 机械兜底同样停在索引档,
      摘要只由 precompress 预热填充,保证机械兜底确定性(不重付 LLM 往返)
    """

    key = "history"
    priority = 2            # 先压
    LEVELS = ("raw", "index", "summary")
    compress_methods = "索引替换:仅 PASS 证据可换成 uuid 引用(get_record 可展开全文),失败/升级/评审保留原文;或把 PASS 证据摘要成'哪步过了、关键证据是什么'式叙述"

    def __init__(self, **kw):
        super().__init__(**kw)
        self._compress = None
        self._summary = ""
        self._folded_passes = 0

    def create(self, ws, **kw):
        super().create(ws, **kw)
        self._compress = kw.get("compress")
        self._load_cache()
        return self

    def delete(self):
        self._summary = ""
        self._folded_passes = 0
        self._compress = None
        return super().delete()

    def can_advance(self):
        if self._compress is None and self.level >= 1:
            return False        # 无压缩 api → 摘要档不可用,index 即下限
        if not super().can_advance():
            return False
        # 摘要档需新鲜折叠(自标记以来有新 PASS 事件)→ 机械兜底不重付 LLM,只读预热缓存
        if self.level == 1 and len(self._pass_events()) > self._folded_passes:
            return False
        return True

    def precompress(self, ws, compress=None):
        """预热:engine 在非决策时刻调用,提前折叠 delta 并落盘,决策路径零 LLM 往返。"""
        if compress is None:
            return
        self._ws = ws
        self._compress = compress
        self._load_cache()
        self._fold()

    def _events(self):
        ws = self._ws
        if ws is None:
            return []
        return [e for e in ws.events
                if e.agent != Role.SYSTEM and e.kind in (EventKind.STEP_RECORD, EventKind.REPLAN)]

    def _pass_events(self):
        return [e for e in self._events() if e.verdict == Verdict.PASS]

    def _fold(self):
        """增量折叠:只把自标记以来新出现的 PASS 事件发给 compress;非 PASS 不进来。"""
        if self._compress is None:
            return
        evs = self._pass_events()
        new = evs[self._folded_passes:]
        if not new:
            return
        chunk = "\n".join(self._render_raw(new))
        prompt = (
            "把下面的 PASS 证据(已通过验收的步骤/检查)增量压缩成摘要;"
            "说明哪步过了、关键证据是什么;失败/升级/评审记录不在这里,无需保留。"
        )
        try:
            folded = self._compress(prompt, chunk)
        except Exception:  # 压缩失败:不推进折叠进度,下轮重试
            return
        if folded and folded.strip():
            self._summary = f"{self._summary}\n{folded.strip()}".strip()
        self._folded_passes = len(evs)
        self._persist()

    def _persist(self):
        if self._ws is not None:
            self._ws.summaries[f"{self._role}:{self.key}"] = {
                "text": self._summary,
                "passes": self._folded_passes,
            }

    def _load_cache(self):
        self._summary = ""
        self._folded_passes = 0
        if self._ws is None:
            return
        c = self._ws.summaries.get(f"{self._role}:{self.key}")
        if c:
            self._summary = c.get("text", "")
            # 折叠标记取 min:events 追加写,只可能落后,不会超前(截断兜底)
            self._folded_passes = min(c.get("passes", 0), len(self._pass_events()))

    def _render_raw(self, evs):
        lines = []
        for e in evs:
            cols = [e.ts, e.uuid, f"agent={e.agent}", f"kind={e.kind}"]
            if e.step_id:
                cols.append(f"step={e.step_id}")
            if e.verdict:
                cols.append(f"verdict={e.verdict}")
            d = e.detail
            if isinstance(d, ReplanDetail):
                if d.reason:
                    cols.append(f"reason={d.reason}")
                if d.source:
                    cols.append(f"source={d.source}")
                if d.changes:
                    cols.append(f"changes={d.changes}")
            elif isinstance(d, StepRecordDetail):
                if d.observation:
                    cols.append(f"observation={d.observation}")
                if d.result:
                    cols.append(f"result={d.result}")
                if d.attempts:
                    cols.append(f"attempts={d.attempts}")
            lines.append("[{}]".format(" | ".join(cols)))
        return lines

    def _iter_lines(self, pass_renderer):
        """按时间序逐条渲染:非 PASS 永远原文透传;PASS 走 pass_renderer(引用/摘要)。"""
        lines = []
        for e in self._events():
            if e.verdict == Verdict.PASS:
                line = pass_renderer(e)
                if line:
                    lines.append(line)
            else:
                lines.append(self._render_raw([e])[0])
        return lines

    def _pass_ref(self, e):
        return f"[ref {e.uuid}] {e.agent}:{e.kind} step={e.step_id or '-'}"

    def render(self):
        evs = self._events()
        if not evs:
            return ""
        if self.level == 0:
            return "# 执行历史\n" + "\n".join(self._render_raw(evs))
        if self.level == 1:
            return "# 执行历史(索引)\n" + "\n".join(self._iter_lines(self._pass_ref))
        # 摘要档:增量折叠 PASS;降级(无压缩 api)回索引样式,不装假摘要
        self._fold()
        if self._compress is None:
            return "# 执行历史(索引)\n" + "\n".join(self._iter_lines(self._pass_ref))
        body = [ln for ln in self._iter_lines(lambda e: "") if ln]
        body.append(f"## PASS 证据(摘要)\n{self._summary or '[暂无]'}")
        return "# 执行历史(摘要)\n" + "\n".join(body)


class DocsComponent(CtxComponent):
    """技能库文档索引:ws.docs 的只读投影({doc_id: 文档});step 只存 id。

    这些是**技能库检索出的参考文档片段**,不是 agent 自己的 skill——语境是"该用什么
    技能",文档只是参考材料。上下文只渲染紧凑索引(id + 一句话描述,取 doc 首行),
    **全文经 get_doc 原生工具按需取**,不在 ctx 里塞整篇文档。
    生命周期:planning↔plan_review 循环内不清空;on_plan_review_pass 清掉未绑定到步骤
    的参考文档(计划用毕,释放 ctx),**绑定 skill_id 的保留**供 executor 执行时查阅。
    档位:raw(id+一句话)→ ref(仅 id)。
    """

    key = "docs"
    priority = 3            # 可索引替换(在 history 之后压)
    LEVELS = ("raw", "ref")
    compress_methods = "索引里只剩 id(全文经 get_doc 工具按需取),连一句话描述也去掉"

    def render(self):
        docs = self._ws.docs if self._ws else {}
        if not docs:
            return ""
        if self.level == 0:
            lines = ["# 技能库文档"]
            for doc_id, doc in docs.items():
                desc = (doc.splitlines() or [""])[0].strip() or doc_id
                lines.append(f"- {doc_id}: {desc}")
            return "\n".join(lines)
        return "# 技能库文档(索引)\n" + ", ".join(f"`{doc_id}`" for doc_id in docs)

    def on_plan_review_pass(self, **kw):
        if self._ws is not None:
            bp = self._ws.blueprint
            bound = {s.skill_id for s in bp.steps.values()} if bp else set()
            self._ws.docs = {k: v for k, v in self._ws.docs.items() if k in bound}
        self.clear()


class ExperienceComponent(CtxComponent):
    """已验证解题经验:ws.experience 的只读投影(精确匹配到的 procedure 记录)。

    经验 = 同一题(friendly_id/template_id 完全一致)在其它实例/场地**已被平台验证过**
    的解题过程——动态 flag 题用它当"重放参照":ee/et 软鉴定解题步骤与漏洞信息(不依赖
    平台每次提交确认)。raw 档渲染紧凑索引 + trace_json 解题细节(oracle/标记/提取方法/
    flag 格式),**不渲染过期 hint flag**(实例相关,防误导);ref 档仅 procedure_id 索引。
    装配范围由 workspace._exp_enabled(CTF_EXPERIENCE_SCOPE,默认仅 ee)决定。
    生命周期:run 启动由 engine 装填(经 adapter.match_procedures),run 内不清理。
    """

    key = "experience"
    priority = 4            # 参考类,与 tools/tool_dir 同区(docs=3 之后压)
    LEVELS = ("raw", "ref")
    compress_methods = "索引里只剩 procedure_id(脚本路径可重跑验证),连描述也去掉"

    # trace_json 里可入 ctx 的解题证据字段(flag 本身/verified_flag 是实例相关,不渲染)
    _TRACE_KEYS = ("oracle", "true_mark", "false_mark", "waf_mark", "extraction", "flag_format")

    def render(self):
        records = self._ws.experience if self._ws else []
        if not records:
            return ""
        if self.level == 0:
            lines = ["# 已验证解题经验"]
            for r in records:
                verified = "是" if r.get("platform_verified") else "否"
                ok_at = r.get("last_ok_at") or "-"
                vp = r.get("verifier_path") or "-"
                lines.append(
                    f"- {r.get('friendly_id') or r.get('challenge_id')} "
                    f"[{r.get('method')}] 已验证={verified} 上次成功={ok_at} 脚本={vp}"
                )
                lines.extend(self._trace_lines(r))
            return "\n".join(lines)
        return "# 已验证解题经验(索引)\n" + ", ".join(
            f"`{r['procedure_id']}`" for r in records
        )

    @classmethod
    def _trace_lines(cls, r: dict) -> list[str]:
        """trace_json 的解题步骤/漏洞信息明细(ee 软鉴定参照);无/无法解析则退化空。"""
        t = r.get("trace_json")
        if isinstance(t, str):
            try:
                t = json.loads(t)
            except (ValueError, TypeError):
                t = None
        if not isinstance(t, dict):
            return []
        out = []
        for k in cls._TRACE_KEYS:
            v = t.get(k)
            if v not in (None, ""):
                out.append(f"    {k}: {v}")
        if t.get("verified_at"):
            out.append(f"    verified_at: {t['verified_at']}")
        return out


def _one_tool_spec(spec) -> tuple[str, dict] | None:
    """单条标准工具定义 → (tool_id, 统一 dict);无法识别(非 dict / 无 id)返回 None。"""
    if not isinstance(spec, dict):
        return None
    if spec.get("type") == "function" and isinstance(spec.get("function"), dict):
        spec = spec["function"]          # OpenAI function-calling 形态
    tid = spec.get("name") or spec.get("id")
    if not tid:
        return None
    return tid, {
        "description": spec.get("description", ""),
        "parameters": spec.get("parameters") or spec.get("inputSchema") or {},
    }


def normalize_tool_specs(specs) -> dict[str, dict]:
    """把标准工具定义归一到统一内部形式 {tool_id: {"description": str, "parameters": dict}}。

    与本地协议解耦的**统一入口**:只接受标准工具格式,不认本地 @tool / {id: 描述} 映射。
    接受标准工具定义**列表**(list[dict]):
        OpenAI function-calling:[{"type": "function", "function": {"name", "description", "parameters"}}]
        MCP / 裸 schema:[{"name", "description", "inputSchema" / "parameters"}]
    无法识别的条目(非 dict / 无 id)丢弃,不抛错;specs 为空返回空目录。
    非列表输入抛 TypeError——本地映射形态不在统一接口,防止静默混入。
    """
    out: dict[str, dict] = {}
    if specs is None:
        return out
    if not isinstance(specs, list):
        raise TypeError(
            f"工具目录需为标准工具定义列表(list[dict]),收到 {type(specs).__name__};"
            "不接受本地 {id: 描述} 映射,请传 OpenAI function-calling / MCP 定义"
        )
    for spec in specs:
        item = _one_tool_spec(spec)
        if item is not None:
            out[item[0]] = item[1]
    return out


class ToolDirectoryComponent(CtxComponent):
    """工具目录菜单:ws.tool_catalog(清单加载器)的只读投影,planner/executor 共用。

    目录是"可申请清单"——只渲染 id + 一句话描述,**不写进 ws.tools**(申请前工具不可用)。
    全量展示 catalog.manifest,不做分类过滤也不按 step 绑定门槛——题目需要什么工具由
    agent 现场判断,漏掉分类只是少个提示,不影响能力(apply_tool 对完整清单全开放)。
    planner 只读(规划时参考有哪些工具),executor 经 apply_tool/remove_tool 申请删除。
    档位:raw(全量菜单)→ ref(仅 id)。
    """

    key = "tool_dir"
    priority = 4            # 参考目录,与 ToolComponent 同区
    LEVELS = ("raw", "ref")
    compress_methods = "菜单里只剩 id(工具描述可再申请时获取),连一句话描述也去掉"

    def render(self):
        ws = self._ws
        if ws is None or ws.tool_catalog is None:
            return ""
        manifest = ws.tool_catalog.manifest
        if not manifest:
            return ""
        if self.level == 0:
            lines = ["# 工具目录"]
            for e in manifest:
                lines.append(f"- {e['tool_id']}: {e['description']}")
            return "\n".join(lines)
        return "# 工具目录(索引)\n" + ", ".join(f"`{e['tool_id']}`" for e in manifest)


class ToolComponent(CtxComponent):
    """工具目录:ws.tools 的只读投影(统一形式 {tool_id: {"description", "parameters"}})。

    工具是 executor 的**常备能力集**,run 内静态——不像技能文档每轮按需检索,
    planning↔review 循环里不清空。ctx 渲染紧凑目录(id + 描述),供 planner 参考
    可用能力写"用 nmap 扫"这类步骤。③ 只消费目录,不碰执行工具层。
    与本地协议解耦:`normalize` 接收标准工具格式(OpenAI function-calling / MCP),
    ws.tools 只存归一结果,本地 @tool 结构不泄漏进来。
    档位:raw(全目录)→ ref(仅 id)。
    """

    key = "tools"
    priority = 4            # 参考目录,在 history/docs 之后、dag 之前压
    LEVELS = ("raw", "ref")
    compress_methods = "目录里只剩 id(工具静态,描述可再注入),连一句话描述也去掉"

    @classmethod
    def normalize(cls, specs) -> dict[str, dict]:
        """与本地协议解耦的入口:接收标准工具定义,归一到统一形式(workspace.set_tools 写盘用)。"""
        return normalize_tool_specs(specs)

    def render(self):
        tools = self._ws.tools if self._ws else {}
        if not tools:
            return ""
        if self.level == 0:
            lines = ["# 可用工具"]
            for tid, td in tools.items():
                lines.append(f"- {tid}: {td.get('description', '')}")
            return "\n".join(lines)
        return "# 可用工具(索引)\n" + ", ".join(f"`{tid}`" for tid in tools)


class TraceComponent(CtxComponent):
    """本轮工具调用轨迹(ut+tr):ws.events 里 use_tool + tool_result 的只读投影。

    "轨迹"是模型知道自己正在干什么的过程记录——use_tool(调用意图)与
    tool_result(世界响应)按时间序交替出现,和最终的 output(决策文本)区分开。
    作用域从事件流推导:**最近一次 replan 事件之后**的轨迹事件——replan 事件自身
    推进边界,组件不持瞬态清理状态,`Workspace.load` 断点续跑后边界照样可推导。
    上一轮轨迹不在当前视角,落回 history 审计。数据由 executor/评估执行时
    ws.record_tool_call / ws.record_tool_result 落账(复用 events.jsonl)。
    agent 参数限定只投影某角色的轨迹(如 executor/ep 各自的工具调用),None = 全角色。

    档位:raw(全文)→ index(每条一行 uuid 引用,get_record 可展开)→
    summary(本轮轨迹 LLM 摘要)。摘要按**事件集签名**缓存:本轮轨迹没变就读缓存,
    跨 replan 边界变化即重算;未注入 compress 停在 index 档,不装假摘要。
    """

    key = "trace"
    priority = 1            # 执行输出文本量大,最先压
    LEVELS = ("raw", "index", "summary")
    compress_methods = "索引替换:每条换成 uuid 引用(get_record 可展开全文);或把本轮工具调用轨迹摘要成'调用了哪些工具、关键输出与异常'式叙述"

    def __init__(self, agent=None, **kw):
        super().__init__(**kw)
        self._agent = agent        # 限定只投影该 agent 的轨迹;None = 全角色
        self._compress = None
        self._summary = ""
        self._sig = None

    def create(self, ws, **kw):
        super().create(ws, **kw)
        self._compress = kw.get("compress")
        self._load_cache()
        return self

    def delete(self):
        self._summary = ""
        self._sig = None
        self._compress = None
        return super().delete()

    def can_advance(self):
        if self._compress is None and self.level >= 1:
            return False        # 无压缩 api → 摘要档不可用,index 即下限
        if not super().can_advance():
            return False
        # 摘要档需新鲜折叠(本轮事件集变化)→ 机械兜底不重付 LLM,只读预热缓存
        if self.level == 1 and self._signature() != self._sig:
            return False
        return True

    def precompress(self, ws, compress=None):
        """预热:engine 在非决策时刻调用,提前折叠本轮轨迹并落盘,决策路径零 LLM 往返。"""
        if compress is None:
            return
        self._ws = ws
        self._compress = compress
        self._load_cache()
        self._fold()

    def _cut(self) -> int:
        """最近一次 replan 事件的事件列表索引;无 replan 则 -1(渲染全部)。"""
        cut = -1
        if self._ws is not None:
            for i, e in enumerate(self._ws.events):
                if e.kind == EventKind.REPLAN:
                    cut = i
        return cut

    def _events(self):
        ws = self._ws
        if ws is None:
            return []
        evs = [e for e in ws.events[self._cut() + 1:]
               if e.kind in (EventKind.USE_TOOL, EventKind.TOOL_RESULT)]
        if self._agent is not None:
            evs = [e for e in evs if e.agent == self._agent]
        return evs

    def _signature(self):
        return "|".join(e.uuid for e in self._events())

    def _fold(self):
        """按轮折叠:本轮事件集与缓存签名不一致 → 重新摘要本轮轨迹(替换,不累计)。"""
        if self._compress is None:
            return
        sig = self._signature()
        if sig == self._sig:
            return
        evs = self._events()
        if not evs:
            return
        chunk = "\n".join(self._render_raw(evs))
        prompt = (
            "把下面的工具调用轨迹(本轮执行产出)压缩成摘要;"
            "说明调用了哪些工具、关键输出与异常;保持可读。"
        )
        try:
            folded = self._compress(prompt, chunk)
        except Exception:  # 压缩失败不炸:摘要留空,render 走 [暂无]
            folded = ""
        self._summary = folded.strip() if folded and folded.strip() else ""
        self._sig = sig
        self._persist()

    def _persist(self):
        if self._ws is not None:
            self._ws.summaries[f"{self._role}:{self.key}"] = {
                "text": self._summary, "sig": self._sig}

    def _load_cache(self):
        self._summary = ""
        self._sig = None
        if self._ws is None:
            return
        c = self._ws.summaries.get(f"{self._role}:{self.key}")
        if c:
            self._summary = c.get("text", "")
            self._sig = c.get("sig")

    @staticmethod
    def _fmt_output(out) -> str:
        """工具结果 → 单行文本:str 原样;dict/list json(截断)保可读;其余 str()。"""
        if isinstance(out, str):
            return out.replace("\n", " ")
        if out is None:
            return ""
        if isinstance(out, (dict, list)):
            s = json.dumps(out, ensure_ascii=False, default=str).replace("\n", " ")
            return s if len(s) <= 400 else s[:397] + "..."
        return str(out).replace("\n", " ")

    def _render_raw(self, evs):
        lines = []
        for e in evs:
            d = e.detail
            if isinstance(d, ToolCallDetail):
                args = json.dumps(d.args, ensure_ascii=False)
                lines.append(f"- step={e.step_id or '-'} call {d.tool} {args}")
            elif isinstance(d, ToolResultDetail):
                args = json.dumps(d.args, ensure_ascii=False)
                output = self._fmt_output(d.output)
                lines.append(f"- step={e.step_id or '-'} result {d.tool} {args} -> {output}")
            elif isinstance(d, dict):
                args = json.dumps(d.get("args", {}), ensure_ascii=False)
                tool = d.get("tool", "?")
                if e.kind == EventKind.USE_TOOL:
                    lines.append(f"- step={e.step_id or '-'} call {tool} {args}")
                else:
                    output = self._fmt_output(d.get("output"))
                    lines.append(f"- step={e.step_id or '-'} result {tool} {args} -> {output}")
        return lines

    def _render_index(self, evs):
        lines = []
        for e in evs:
            d = e.detail
            if isinstance(d, (ToolCallDetail, ToolResultDetail)):
                tool = d.tool
            elif isinstance(d, dict):
                tool = d.get("tool", "?")
            else:
                tool = "?"
            lines.append(f"[ref {e.uuid}] step={e.step_id or '-'} tool={tool}")
        return lines

    def render(self):
        evs = self._events()
        if not evs:
            return ""
        if self.level == 0:
            return "# 本轮工具轨迹\n" + "\n".join(self._render_raw(evs))
        if self.level == 1:
            return "# 本轮工具轨迹(索引)\n" + "\n".join(self._render_index(evs))
        self._fold()
        if self._compress is None:
            return "# 本轮工具轨迹(索引)\n" + "\n".join(self._render_index(evs))
        return f"# 本轮工具轨迹(摘要)\n{self._summary or '[暂无]'}"
