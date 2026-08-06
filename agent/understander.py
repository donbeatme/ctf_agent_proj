"""任务理解层输出 API:原始输入 → TaskInput。外部 ② 交付,③ 只消费不产出。

契约见 design/contracts.md §0。engine 在 run() 起始调用 understander 获取
TaskInput 实例,goal_list(格式 list[Goal])只从这里来,不做二次解析。

Mock 模拟 ② 的结构化输出:消费输入 dict 里的 "goals" 键(模拟 ② 已解析出的
目标)生成 goal_list,其余字段原样作 raw_content——与 raw 的 "goals" 键解耦。
"""

from agent.schema import Goal, TaskInput


class TaskUnderstander:
    """任务理解层输出 API(外部 ② 实现)。"""

    def understand(self, raw: dict) -> TaskInput:
        raise NotImplementedError


class MockTaskUnderstander(TaskUnderstander):
    """Mock:raw["goals"] → goal_list(list[Goal]),raw 其余原样作 raw_content。"""

    def understand(self, raw: dict) -> TaskInput:
        raw = dict(raw)
        goals = [Goal(**g) for g in raw.pop("goals", [])]
        return TaskInput(raw_content=raw, goal_list=goals)
