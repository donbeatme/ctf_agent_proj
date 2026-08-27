"""规划 Agent:PlannerInput → 上下文 → LLM(JSON)→ PlanPatch → Blueprint。

ctx 组装:
- planner 已接入 CtxAssembler:经 workspace.assembler.assemble("planner", raw_content/goal_list/
  turn/system) 组装,组件是 workspace 只读投影(dag=ws.blueprint、history=ws.events、
  docs=ws.docs 注册表)——引擎先写 workspace(引擎打点/落账),Planner 只读。
- 默认走 llm_api.chat_with_tools + 只读 lookup 工具(get_doc 取文档全文),不碰执行工具层;
  llm_call 可插拔,冒烟/测试用 mock 返回预置 JSON。
"""

from agent import llm_api, tools
from agent.blueprint import Blueprint, DAGError
from agent.schema import PlanError, PlannerInput, PlannerMode, Trigger, parse_plan
from agent.workspace import MockWorkspace
from model_config import get

PLAN_SYSTEM = (
    "你是 CTF 解题规划 Agent。根据任务、当前计划与评估意见,输出一份 JSON 修改计划,只输出 JSON。"
    '格式:{"add":[{"id":"s1","instruction":"做什么","criterion":"可检验标准","skill_id?":"可绑定技能库文档id","depends_on":[]}],'
    '"update":[{"id":"s1","instruction?":"","criterion?":"","skill_id?":"","depends_on?":[]}],'
    '"remove":["s1"],"reason":"修改原因"}。'
    "id 只含字母/数字/下划线;方案不变则 add/update/remove 全为空。"
    "技能库只列出 id 与一句话描述;需要某文档完整内容时,调用 get_doc(doc_id) 获取全文。"
)

# 状态注入提示词:只解释触发原因与状态语义,不下方向指令——评估与决策由模型自行完成。
# plan_review_fail 特别要求独立评估评审意见(意见可能不准确)并给出理由(写进 patch.reason)。
TRIGGER_NOTES = {
    Trigger.PLAN_REVIEW_FAIL: (
        "计划评审未通过,触发重规划;评审把未通过步骤标记为 REVISE(待修订)。"
        "评审意见见\"评估意见\"节——意见不一定正确,请自行评估其合理性,"
        "给出你的判断;采纳/不采纳的理由可写进补丁 reason。"
    ),
    Trigger.STEP_ESCALATED: (
        "有步骤处于 ESCALATED(重试耗尽或判定升级):不再自动重跑,"
        "且会阻塞依赖它的后续步骤,故触发重规划。"
    ),
    Trigger.STEP_TARGET_REDESIGN: (
        "有步骤的目标/验收标准被评估判定为设计有误(planner 设计问题,非执行问题):"
        "重跑该步也无法达成,本次仅重设计该步骤的 instruction/criterion。"
    ),
    Trigger.DEADLOCK: (
        "调度死锁:没有可执行步骤但任务未完成,通常是前置步骤被升级/阻塞"
        "导致依赖链断裂,触发重规划以重新设计依赖关系。"
    ),
    Trigger.REFLECT: (
        "任务反思完成,进入终局修订,本次重规划后任务收尾。"
    ),
}

STATUS_GLOSSARY = (
    "- REVISE:评审未通过的标记,该步骤待修订(补丁可改其 instruction/criterion/依赖)。\n"
    "- ESCALATED:步骤升级(重试耗尽/判定升级),不再自动重跑,会阻塞依赖它的步骤。"
)


def _render_state_context(sc) -> str:
    """把调度器注入的状态上下文渲染成系统提示词段落(触发原因 + 状态语义)。"""
    parts = ["# 重规划背景"]
    note = TRIGGER_NOTES.get(sc.trigger)
    if note:
        parts.append(note)
    parts.append("# 状态语义\n" + STATUS_GLOSSARY)
    if sc.detail:
        parts.append("# 具体原因\n" + sc.detail)
    if sc.budget:
        parts.append("# 预算\n" + sc.budget)
    return "\n\n".join(parts)


class DocStore:
    """技能库文档检索接口桩(上游实现)。③ 只调用,不实现;未接入时传 None 跳过。

    search 返回 [(doc_id, text)],planner 原样 set_doc(doc_id) 保留可绑定的 id;
    load_doc 供 get_doc 按需取未注册文档(如子文档),返回 None 表示不存在。
    """

    def search(self, task: dict) -> list[tuple[str, str]]:
        raise NotImplementedError

    def load_doc(self, doc_id: str) -> str | None:
        return None


class CombinedDocStore(DocStore):
    """合并多个 DocStore(如技能库 + 审计经验库),按 doc_id 去重。

    audit 模式用它同时检索技能文档与已验证经验。search 结果照旧写进 ws.docs,
    DocsComponent 只渲染 id + 一句话描述,全文经 get_doc 工具按需取——不全量塞进上下文。
    """

    def __init__(self, stores):
        self._stores = tuple(stores)

    def search(self, task: dict) -> list[tuple[str, str]]:
        results: list[tuple[str, str]] = []
        seen = set()
        for store in self._stores:
            for doc_id, content in store.search(task):
                if doc_id in seen:
                    continue
                seen.add(doc_id)
                results.append((doc_id, content))
        return results

    def load_doc(self, doc_id: str) -> str | None:
        for store in self._stores:
            content = store.load_doc(doc_id)
            if content is not None:
                return content
        return None


class MockPlannerLLM:
    """返回预置 PlanPatch JSON 的 mock LLM(网关接入前的占位)。"""

    def __init__(self, response: str):
        self._response = response

    def __call__(self, *, system=None, prompt=None, messages=None, **kwargs) -> str:
        return self._response


# planner 的原生工具:只读 lookup,读 ③ 自己的 workspace 注册表,不是执行工具层。
# get_doc:技能库在 ctx 只渲染 id + 一句话描述,决策前按需拉全文。
# 规格从工具库导出(白名单 get_doc),不硬编码;不与执行工具目录混用(design/tools.md §4)。
PLANNER_TOOLS = tools.openai_tool_specs(names={"get_doc"})


class Planner:
    """规划 Agent。llm_call 签名兼容 agent.llm_api.chat(system=, prompt=) -> str。"""

    def __init__(self, llm_call=None, docs: DocStore | None = None,
                 workspace=None):
        self.workspace = workspace or MockWorkspace()
        self.docs = docs
        self.llm_call = llm_call or self._default_llm()

    def _default_llm(self):
        """默认 LLM 调用:chat_with_tools + 只读 lookup 工具(get_doc 取文档全文)。

        lookup 读 ③ 自己的 ws.docs 注册表(上游已灌入),不碰执行工具层。
        签名 (system=, prompt=) -> str,plan() 不变;测试注入 mock 即绕过。
        LLM_ENABLE_TOOLS=false 时退化为纯 chat(兼容未开 auto tool choice 的网关)。
        """
        model = llm_api.role_model("planner")
        enable_tools = str(get("LLM_ENABLE_TOOLS", "true")).lower() not in (
            "0", "false", "no", ""
        )
        planner_tools = PLANNER_TOOLS if enable_tools else []

        def call(*, system=None, prompt=None, messages=None, **kw) -> str:
            tr = llm_api.chat_with_tools(
                system=system, prompt=prompt, messages=messages,
                tools=planner_tools, tool_exec=self._lookup, model=model, **kw)
            self._last_usage = tr.total_usage  # token 用量供 plan() 写入 bp.meta
            return tr.content

        return call

    def _lookup(self, name, args):
        """只读 lookup:get_doc 从 ws.docs 取全文;找不到返回 error 喂回模型。
        同时写工具调用事件(use_tool + tool_result),供 TraceComponent 投影规划决策链。"""
        ws = self.workspace
        if name == "get_doc":
            doc_id = (args or {}).get("doc_id")
            doc = ws.get_doc(doc_id) if doc_id else None
            if doc is None and self.docs is not None:
                # 未注册子文档按需取:检索命中分类时只灌 SKILL.md,子文档经此兜底
                doc = self.docs.load_doc(doc_id)
                if doc is not None:
                    ws.set_doc(doc_id, doc)   # 取到即入注册表,可绑定 skill_id/持久化
            ws.record_tool_call(None, name, args or {})
            if doc is None:
                ws.record_tool_result(None, name, f"未知文档: {doc_id}", args=args or {})
                return {"error": f"未知文档: {doc_id}"}
            ws.record_tool_result(None, name, doc, args=args or {})
            return {"doc_id": doc_id, "content": doc}
        ws.record_tool_call(None, name, args or {})
        ws.record_tool_result(None, name, f"未知工具: {name}", args=args or {})
        return {"error": f"未知工具: {name}"}

    def plan(self, pin: PlannerInput) -> Blueprint:
        raw_content = pin.task_input.raw_content if pin.task_input else None
        goal_list = pin.task_input.goal_list if pin.task_input else []
        # 状态化系统提示词:契约 base(固定)+ 引擎注入的状态上下文(解释触发原因/状态语义)
        system = PLAN_SYSTEM
        if pin.feedback and pin.feedback.state_context:
            system += "\n\n" + _render_state_context(pin.feedback.state_context)
        if pin.feedback and pin.feedback.scope_step_id:
            scope = pin.feedback.scope_step_id
            system += (
                f"\n\n【重规划范围】只允许通过 update 修改步骤 {scope} 的 "
                "instruction/criterion(可附 skill_id/depends_on);"
                "其余步骤必须保持原样,不得 add/remove/update。"
                "在 reason 中说明该步骤目标为何有误及新目标。"
            )
        # 文档注册:检索结果写入 ws.docs 注册表(投影模型),Docs 组件渲染进 ctx。
        # 保留检索返回的真实 doc_id,planner 才能把 skill_id 绑到技能文档上。
        if self.docs is not None:
            for doc_id, doc in self.docs.search(raw_content or {}):
                self.workspace.set_doc(doc_id, doc)
        turn = list(pin.feedback.turn) if pin.feedback and pin.feedback.turn else []
        ctx, sys_text, _ = self.workspace.assembler.assemble(
            "planner", raw_content=raw_content, goal_list=goal_list,
            turn=turn, system=system)
        text = self.llm_call(system=sys_text or system, prompt=ctx)
        try:
            patch = parse_plan(text).to_patch()
        except PlanError as e:
            # 一次重试:PlanError 文本可直接喂回模型
            retry_prompt = (
                f"{ctx}\n\n[上一轮输出解析失败: {e}]\n"
                "请严格按要求的 JSON 格式重新输出。"
            )
            text = self.llm_call(system=sys_text or system, prompt=retry_prompt)
            patch = parse_plan(text).to_patch()
        bp = Blueprint.from_dict(pin.feedback.dag) if pin.mode == PlannerMode.REVISE \
            else Blueprint(meta={"task": raw_content})
        try:
            bp.apply_patch(patch)
        except DAGError as exc:
            # 结构性补丁错误重试一次:把 DAG 应用失败原因反馈给 LLM 修正(对齐 PlanError 重试)
            retry_prompt = (
                f"{ctx}\n\n[上一轮补丁无法应用: {exc}]\n"
                "请针对当前 DAG 返回修正后的补丁:初始空 DAG 用 add 建每一步;"
                "update/remove 只能针对已存在的 step id。"
            )
            text = self.llm_call(system=sys_text or system, prompt=retry_prompt)
            patch = parse_plan(text).to_patch()
            bp.apply_patch(patch)
        bp.meta["reason"] = patch.reason   # 规划理由落账,供 _record_plan 写入 replan 事件
        bp.meta["_response"] = text        # 原始 LLM 返回(供 log 记录)
        if getattr(self, "_last_usage", None):
            bp.meta["_usage"] = self._last_usage  # token 用量供 engine 追踪
        return bp
