"""工具注册:ToolRegistry(实例级)+ 模块级 fallback(向后兼容)。

ToolRegistry 持有实例级 _registry / _docs / _workspace, 消除进程级全局变量污染。
外部工具可通过 @tool 装饰器注册到模块级 _REGISTRY,
ToolRegistry 在 call_tool/openai_tool_specs 时先查自身再 fallback 到全局。
"""

import re

# 模块级全局注册表(外部工具经 @tool 装饰器注册)
_REGISTRY: dict[str, object] = {}

# 按需包名安全校验(与 sandbox_env.tools._SAFE_NAME_RE 一致):非目录名称要拼进沙箱
# shell 命令(apt-get/pip),先拦掉元字符,防工具名注入。
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}$")


# 只读 lookup 工具规格(OpenAI function-calling 格式):skills 只渲染 id+一句话描述,
# 决策前按需拉全文。planner/executor 等角色复用同一份,避免逐角色重复定义。
GET_DOC_SPEC = {
    "type": "function",
    "function": {
        "name": "get_doc",
        "description": (
            "技能库在上下文中只列出 id 与一句话描述;决策前若需要某文档完整内容,"
            "按 doc_id 取全文。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string", "description": "文档注册表中的 id"},
            },
            "required": ["doc_id"],
        },
    },
}


APPLY_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "apply_tool",
        "description": (
            "工具目录在上下文中只列菜单;决策后按 tool_id 申请激活工具,申请成功的工具"
            "加入可用工具集(经 get_doc 之外的工具需先申请)。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tool_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要申请的清单 tool_id 列表",
                },
            },
            "required": ["tool_ids"],
        },
    },
}

REMOVE_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "remove_tool",
        "description": (
            "从可用工具集移除先前申请的工具(有申请就有删除);未激活的 id 忽略(幂等)。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tool_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要移除的 tool_id 列表",
                },
            },
            "required": ["tool_ids"],
        },
    },
}


def tool(name, description, parameters):
    """装饰器:注册到模块级 _REGISTRY(向后兼容)。"""

    def deco(fn):
        fn._tool_name = name
        fn._tool_description = description
        fn._tool_parameters = parameters
        _REGISTRY[name] = fn
        return fn

    return deco


def call_tool(name, arguments, *, registry: "ToolRegistry | None" = None):
    """执行工具调用。先查 instance registry,再 fallback 到模块级 _REGISTRY。"""
    fn = None
    if registry is not None:
        fn = registry._registry.get(name)
    if fn is None:
        fn = _REGISTRY.get(name)
    if fn is None:
        return {"error": f"unknown tool: {name}"}
    try:
        return fn(**arguments)
    except TypeError as e:
        return {"error": f"bad arguments: {e}"}


def _to_spec(fn) -> dict:
    """工具对象(带 _tool_name/_tool_description/_tool_parameters)→ OpenAI function-calling 规格。"""
    return {
        "type": "function",
        "function": {
            "name": fn._tool_name,
            "description": fn._tool_description,
            "parameters": fn._tool_parameters,
        },
    }


def openai_tool_specs(names: set[str] | None = None) -> list[dict]:
    """以 OpenAI function-calling 格式导出模块级注册表(@tool 注册)的工具规格。

    names 省略 → 全部;给定 → 只导出集合内的工具(如 planner 白名单 get_doc)。
    规格来自装饰器注册的 _REGISTRY;执行仍走各角色注入的 tool_exec(get_doc 由
    planner._lookup / ToolRegistry 闭包处理),模块级 fallback 只提供规格不参与调用。
    """
    merged = dict(_REGISTRY)
    if names is not None:
        merged = {n: fn for n, fn in merged.items() if n in names}
    return [_to_spec(fn) for fn in merged.values()]


class ToolRegistry:
    """实例级工具注册表。每个 Engine 持有独立实例,消除进程级全局变量污染。"""

    def __init__(self):
        self._registry: dict[str, object] = {}
        self._docs: dict[str, str] = {}
        self._workspace = None
        self._register_builtins()

    def _register_builtins(self):
        """注册内置只读 lookup 工具(依赖 _docs / _workspace,闭包捕获 self)。"""
        _self = self

        def get_doc(doc_id):
            doc = _self._docs.get(doc_id)
            if doc is None:
                return {"error": f"未知文档: {doc_id}"}
            return {"doc_id": doc_id, "content": doc}

        def get_record(uuid):
            ws = _self._workspace
            if ws is None:
                return {"error": "工具上下文未初始化(无 workspace 引用)"}
            ev = ws.get_record(uuid)
            if ev is None:
                return {"error": f"未找到记录: {uuid}"}
            from dataclasses import asdict
            return asdict(ev)

        def apply_tool(tool_ids):
            ws = _self._workspace
            catalog = ws.tool_catalog if ws else None
            if catalog is None:
                return {"error": "工具目录未初始化(Engine 未注入 tool_catalog)"}
            from agent.checks import SkillEnvProbe
            probe = SkillEnvProbe(catalog)
            added, pending, rejected, probe_map = [], [], [], {}
            for tid in tool_ids:
                if not isinstance(tid, str) or not _TOOL_NAME_RE.match(tid):
                    rejected.append(tid)
                    continue
                meta = catalog.get_tool(tid)
                if meta is None:
                    # 目录外工具(如 wine):接受为"按需申请",沙箱适配器首次使用时动态安装。
                    # agent 只声明意图,不直接装包(安装能力在沙箱适配器)。
                    pending.append(tid)
                    ws.add_tools([{"name": tid,
                                   "description": f"按需包:{tid}(目录外,首次使用由沙箱自动安装)"}])
                    continue
                ws.add_tools([{"name": tid, "description": meta["description"],
                               "parameters": {"type": "object", "properties": {}}}])
                added.append(tid)
                try:
                    probe_map[tid] = probe.probe_tool(tid)
                except Exception:
                    probe_map[tid] = {"tool_id": tid, "status": "unknown", "check": ""}
            # 返回追加 probe:每工具可用性探测(只读)。unknown 向后兼容(本次即 rejected)。
            return {"added": added, "pending": pending, "unknown": rejected,
                    "rejected": rejected, "probe": probe_map}

        def remove_tool(tool_ids):
            ws = _self._workspace
            if ws is None:
                return {"error": "工具上下文未初始化(无 workspace 引用)"}
            removed, missing = [], []
            for tid in tool_ids:
                if tid in ws.tools:
                    ws.remove_tools([tid])
                    removed.append(tid)
                else:
                    missing.append(tid)
            return {"removed": removed, "missing": missing}

        self.register(
            "get_doc", get_doc,
            GET_DOC_SPEC["function"]["description"],
            GET_DOC_SPEC["function"]["parameters"],
        )
        self.register(
            "get_record",
            get_record,
            "按 uuid 取历史事件全文(展开索引投影)。",
            {
                "type": "object",
                "properties": {"uuid": {"type": "string", "description": "历史事件的 uuid 索引"}},
                "required": ["uuid"],
            },
        )
        self.register(
            "apply_tool", apply_tool,
            APPLY_TOOL_SPEC["function"]["description"],
            APPLY_TOOL_SPEC["function"]["parameters"],
        )
        self.register(
            "remove_tool", remove_tool,
            REMOVE_TOOL_SPEC["function"]["description"],
            REMOVE_TOOL_SPEC["function"]["parameters"],
        )

    def register(self, name: str, fn, description="", parameters=None):
        """注册一个工具到此实例的注册表。"""
        fn._tool_name = name
        fn._tool_description = description
        fn._tool_parameters = parameters or {"type": "object", "properties": {}}
        self._registry[name] = fn

    def set_docs(self, docs: dict[str, str]):
        """注入技能文档注册表(供只读 lookup 工具使用)。"""
        self._docs = docs

    def set_workspace(self, workspace):
        """注入 workspace 引用(供 get_record 等工具使用)。"""
        self._workspace = workspace

    @property
    def docs(self) -> dict[str, str]:
        return self._docs

    @property
    def workspace(self):
        return self._workspace

    def openai_tool_specs(self, names: set[str] | None = None) -> list[dict]:
        """完整工具规格:可调用注册表(get_doc/get_record/外部 @tool)+ ws.tools 工具目录。

        names 省略 → 全量;给定 → 只导出集合内的工具。
        注册表条目可在此实例 call_tool 直接执行;工具目录条目是**能力声明**
        (执行方是执行层/沙箱,不在本注册表内)——两份视图合成一份规格,消除目录与
        可调用注册表脱节的问题。目录取 workspace.tools(归一形式),未注入 workspace 时跳过。
        """
        merged = dict(_REGISTRY)
        merged.update(self._registry)
        if names is not None:
            merged = {n: fn for n, fn in merged.items() if n in names}
        specs = [_to_spec(fn) for fn in merged.values()]
        catalog = self._workspace.tools if self._workspace else {}
        for tid, td in catalog.items():
            if names is not None and tid not in names:
                continue
            specs.append({
                "type": "function",
                "function": {
                    "name": tid,
                    "description": td.get("description", ""),
                    "parameters": td.get("parameters") or {},
                },
            })
        return specs

    def call_tool(self, name: str, arguments: dict) -> dict:
        """执行工具:本实例优先,fallback 到全局 _REGISTRY。"""
        return call_tool(name, arguments, registry=self)

    def get_doc(self, doc_id: str) -> dict:
        """只读 lookup:从技能文档注册表取全文。"""
        doc = self._docs.get(doc_id)
        if doc is None:
            return {"error": f"未知文档: {doc_id}"}
        return {"doc_id": doc_id, "content": doc}

    def get_record(self, uuid: str) -> dict:
        """只读 lookup:按 uuid 取历史事件全文。"""
        ws = self._workspace
        if ws is None:
            return {"error": "工具上下文未初始化(无 workspace 引用)"}
        ev = ws.get_record(uuid)
        if ev is None:
            return {"error": f"未找到记录: {uuid}"}
        from dataclasses import asdict
        return asdict(ev)


# ===== 模块级 fallback(向后兼容,无 ToolRegistry 注入时返回错误) =====


@tool(
    "get_doc",
    "技能库在上下文中只列出 id 与一句话描述;决策前若需要某文档完整内容,按 doc_id 取全文。",
    {
        "type": "object",
        "properties": {"doc_id": {"type": "string", "description": "文档注册表中的 id"}},
        "required": ["doc_id"],
    },
)
def _get_doc_fallback(doc_id):
    return {"error": "工具未初始化(无 ToolRegistry 注入)"}


@tool(
    "get_record",
    "按 uuid 取历史事件全文(展开索引投影)。",
    {
        "type": "object",
        "properties": {"uuid": {"type": "string", "description": "历史事件的 uuid 索引"}},
        "required": ["uuid"],
    },
)
def _get_record_fallback(uuid):
    return {"error": "工具上下文未初始化(无 workspace 引用)"}
