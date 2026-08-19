"""CLI 命令:沙箱管理器能力经命令行暴露。main.py 薄接线。"""

from __future__ import annotations

import sys

from .errors import SandboxError, SandboxUnavailableError


def register(sub):
    p = sub.add_parser("sandbox-probe", help="探测沙箱后端就绪状态与会话容器")
    p.set_defaults(func=cmd_sandbox_probe)

    p = sub.add_parser("sandbox-conflicts", help="列出工具依赖冲突/不兼容/冗余清单")
    p.set_defaults(func=cmd_sandbox_conflicts)

    p = sub.add_parser(
        "sandbox-deps", help="探测缺失工具 → 安装进沙箱容器(持久)→ 重校验"
    )
    p.add_argument(
        "tools", nargs="+", help="category 或 tool_id(如 ctf-pwn 或 ROPgadget)"
    )
    p.add_argument("--force", action="store_true", help="跳过已可用探测,强制重装")
    p.set_defaults(func=cmd_sandbox_deps)


def _manager():
    from .base import SandboxManager

    return SandboxManager()


def cmd_sandbox_probe(args) -> None:
    try:
        m = _manager()
    except SandboxUnavailableError as e:
        print(f"sandbox-probe 失败: {e}", file=sys.stderr)
        sys.exit(1)
    ready = m.backend.is_ready()
    try:
        name = m.ensure(m.session_key())
    except SandboxError as e:
        print(f"sandbox-probe: backend ready={ready} 会话容器就绪失败: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"backend={m.backend.name} ready={ready}")
    print(f"session_container={name or '(无容器后端)'}")


def cmd_sandbox_conflicts(args) -> None:
    from .tools import ToolManager  # 纯元数据分析,无需 SSH 后端

    items = ToolManager().tool_conflicts()
    if not items:
        print("tool_conflicts: 无冲突/不兼容")
        return
    for it in items:
        pair = it["a"] if not it["b"] else f"{it['a']} × {it['b']}"
        print(f"[{it['severity']}] {pair}: {it['reason']}")


def cmd_sandbox_deps(args) -> None:
    try:
        m = _manager()
    except SandboxUnavailableError as e:
        print(f"sandbox-deps 失败: {e}", file=sys.stderr)
        sys.exit(1)
    catalog = m.tools.catalog
    categories = set(catalog.categories())
    tool_ids: list[str] = []
    for t in args.tools:
        if t in categories:
            tool_ids.extend(catalog.allowed_tools(t))
        else:
            tool_ids.append(t)
    if not tool_ids:
        print("sandbox-deps: 无待处理工具", file=sys.stderr)
        sys.exit(1)
    report = m.install_tools(tool_ids, session_key=m.session_key(), force=args.force)
    print(
        f"installed={report['installed']} failed={report['failed']} "
        f"skipped_manual={report['skipped_manual']} incompatible={report['incompatible']}"
    )
    for tid in report["failed"]:
        print(f"  FAILED     {tid}")
    for tid in report["incompatible"]:
        print(f"  INCOMPAT   {tid}")
    for tid in report["skipped_manual"]:
        print(f"  MANUAL     {tid} (无法自动安装)")
