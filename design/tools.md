# 工具协议

工具注册、调用、规格化。实现：`agent/tools.py`。

---

## 1. @tool 装饰器

```python
@tool(name, description, parameters)
def my_tool(arg1, arg2):
    ...
```

装饰器行为：
- 在函数上挂载 `_tool_name` / `_tool_description` / `_tool_parameters`
- 注册到全局 `_REGISTRY[name] = fn`
- 返回原函数（可正常直接调用）

### parameters 格式

OpenAI function calling 的 `parameters` 字段：JSON Schema 对象，含 `type`、`properties`、`required`。

```python
{
    "type": "object",
    "properties": {
        "arg1": {"type": "string", "description": "..."},
        "arg2": {"type": "integer", "default": 10},
    },
    "required": ["arg1"],
}
```

---

## 2. 工具规格生成与调用

### openai_tool_specs

```python
def openai_tool_specs(names: set[str] | None = None) -> list[dict]   # 模块级:只读 @tool 注册表
def ToolRegistry.openai_tool_specs(names: set[str] | None = None) -> list[dict]  # 实例级:注册表 + ws.tools 目录
```

生成 OpenAI function calling 格式的工具列表：
```python
[{"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}]
```

- `names` 省略 → 全量；给定 → 只导出集合内的工具（白名单过滤）。
- 模块级版本只导出 `@tool` 装饰器注册的全局 `_REGISTRY`（如 `openai_tool_specs(names={"get_doc"})`，planner 用）。
- 实例级版本额外合并 `ws.tools` 工具目录（归一形式 `{id: {"description", "parameters"}}`）——
  目录条目是**能力声明**（执行方是执行层/沙箱），注册表条目可在此实例 `call_tool` 直接执行，
  两份视图合成一份规格，消除目录与可调用注册表脱节。

### call_tool

```python
def call_tool(name: str, arguments: dict) -> dict
```

- unknown tool → `{"error": "unknown tool: <name>"}`
- TypeError（参数不匹配）→ `{"error": "bad arguments: ..."}`
- 正常 → 函数返回值

---

## 3. ToolRegistry — 实例级工具管理

```python
class ToolRegistry:
    def __init__(self):
        self._registry: dict[str, callable] = {}
        self._docs: dict[str, str] = {}
        self._workspace = None
        self._register_builtins()   # 注册 get_doc / get_record 闭包

    def register(name, fn, description="", parameters=None)
    def call_tool(name, arguments) -> dict
    def openai_tool_specs(names=None) -> list[dict]   # 注册表 + ws.tools 目录
    def set_docs(docs: dict[str, str])
    def set_workspace(workspace)
    def get_doc(doc_id) -> dict
    def get_record(uuid) -> dict
```

### 内置工具（`_register_builtins`）

`get_doc` 和 `get_record` 在 `__init__` 中注册为闭包，捕获 `self` 引用：

- `get_doc(doc_id)` — 从 `self._docs` 取文档全文，未找到返回 `{"error": "未知文档: ..."}`
- `get_record(uuid)` — 从 `self._workspace` 按 uuid 取事件全文（dataclass → asdict），无 workspace 或未找到返回 error

引擎在 `_init_run` / `resume` 时调用 `set_docs()` / `set_workspace()` 注入上下文，内置工具无需额外配置即可工作。

### 全局回退

`call_tool()` 先查实例 `_registry`，未命中则 fallback 到模块级 `_REGISTRY`（外部工具通过 `@tool` 装饰器注册）。

---

## 4. 工具规格归一化（`agent/ctx.py` — `ToolComponent`）

`ToolComponent.normalize(tool)` 接受并归一化工具定义：

| 输入格式 | 处理 |
|---|---|
| OpenAI 标准 `{type: "function", function: {name, description, parameters}}` | 直接取 function 字段 |
| MCP / 简化 `{name, description, inputSchema}` | 属性映射到标准格式 |
| 本地映射 `{id: 描述}` | 拒绝，抛 `TypeError` |
| 其他无法识别的 dict | 丢弃 |

输出统一为 `{name, description, parameters}`。

Planner 仅使用 Planner 专属工具（`PLANNER_TOOLS = tools.openai_tool_specs(names={"get_doc"})`，从工具库白名单导出），不与执行工具混用。
