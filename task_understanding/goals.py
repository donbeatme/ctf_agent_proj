"""目标生成策略。

用户 raw.goals 原样保留为 goal_list;未提供时默认 [obtain_flag]。
不再按题型追加模板目标(find_flag/solve_*/analyze_attachments/reach_target):
多目标无 agent 支撑,模板目标不可执行,只会让目标集虚增。
"""

from __future__ import annotations

from agent.schema import Goal


def default_goals(primary: str | None, raw: dict) -> list[Goal]:
    """按用户输入生成 goal_list(仅 id)。

    raw["goals"] 提供时原样保留(去重);未提供时回退单目标 [obtain_flag]。
    """
    goals: list[Goal] = []
    seen = set()
    for g in raw.get("goals") or []:
        gid = g["id"] if isinstance(g, dict) else str(g)
        gid = gid.strip()
        if gid and gid not in seen:
            goals.append(Goal(id=gid))
            seen.add(gid)
    if not goals:
        goals.append(Goal(id="obtain_flag"))
    return goals
