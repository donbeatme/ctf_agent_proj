"""计划 DAG:Step / Blueprint / Patch。

核心资产:蓝图 = 步骤(含可检验标准 + 依赖关系)的 DAG,支持拓扑排序、
结构校验、补丁合并(动态重规划)。设计见 design/dag.md。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.schema import UpdateSpec


class StepStatus(StrEnum):
    """步骤状态。READY 为派生状态(不存储):PENDING 且依赖全 PASSED。"""
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    RETRY = "RETRY"
    REVISE = "REVISE"        # 计划评审不过,待修订
    ESCALATED = "ESCALATED"
    SKIPPED = "SKIPPED"


# 终态:不再自动重排
DONE_STATUSES = frozenset({StepStatus.PASSED, StepStatus.SKIPPED})


class DAGError(ValueError):
    pass


def _default_max_attempts() -> int:
    try:
        from model_config import get_engine_config
        return get_engine_config()["max_step_attempts"]
    except Exception:
        return 3


@dataclass
class Step:
    id: str
    instruction: str      # 做什么,给执行 Agent
    criterion: str        # 可检验标准,给步骤验收 Agent
    depends_on: list[str] = field(default_factory=list)  # 上游依赖,[]=入口
    skill_id: str | None = None   # 绑定技能库文档 id(planner 检索后填入,executor 执行时查阅)
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    max_attempts: int = field(default_factory=_default_max_attempts)
    result: dict | None = None

    def __post_init__(self):
        # 反序列化(JSON 读出来是字符串)时强制校验并转枚举;非法值直接抛错
        self.status = StepStatus(self.status)
        self.depends_on = list(self.depends_on)


@dataclass
class Patch:
    add: list[Step] | None = None          # 新增步骤
    update: list[UpdateSpec] | None = None   # [{id, instruction?|criterion?|depends_on?}]
    remove: list[str] | None = None        # 删除的 step id
    reason: str = ""                       # 修改原因(审计用)


class Blueprint:
    """计划 DAG。steps 保持插入序。"""

    def __init__(self, meta=None):
        self.meta = meta or {}
        self.steps: dict[str, Step] = {}

    # ===== 构建 =====

    def add_step(self, step: Step):
        if step.id in self.steps:
            raise DAGError(f"step id 重复: {step.id}")
        self.steps[step.id] = step

    def update_instruction(self, step_id, text):
        """内容变更:只改执行描述。非终态步骤回 PENDING 重跑;不级联后代。"""
        s = self.steps.get(step_id)
        if s is None:
            raise DAGError(f"step 不存在: {step_id}")
        s.instruction = text
        if s.status not in DONE_STATUSES:
            s.status = StepStatus.PENDING

    def update_criterion(self, step_id, text):
        """内容变更:只改验收标准。非终态步骤回 PENDING 重跑;不级联后代。"""
        s = self.steps.get(step_id)
        if s is None:
            raise DAGError(f"step 不存在: {step_id}")
        s.criterion = text
        if s.status not in DONE_STATUSES:
            s.status = StepStatus.PENDING

    def update_skill_id(self, step_id, skill_id):
        """绑定变更:改技能文档引用。非终态步骤回 PENDING 重跑;不级联后代。"""
        s = self.steps.get(step_id)
        if s is None:
            raise DAGError(f"step 不存在: {step_id}")
        s.skill_id = skill_id or None
        if s.status not in DONE_STATUSES:
            s.status = StepStatus.PENDING

    def set_depends_on(self, step_id, depends_on):
        """结构变更:改依赖。非终态步骤回 PENDING 并级联非终态后代。"""
        s = self.steps.get(step_id)
        if s is None:
            raise DAGError(f"step 不存在: {step_id}")
        descendants = self._descendants(step_id)
        s.depends_on = list(depends_on)
        if s.status not in DONE_STATUSES:
            s.status = StepStatus.PENDING
            for did in descendants:
                d = self.steps[did]
                if d.status not in DONE_STATUSES:
                    d.status = StepStatus.PENDING

    def remove_step(self, step_id):
        """结构变更:删除 + 清他人引用 + 非终态后代回 PENDING。"""
        if step_id not in self.steps:
            raise DAGError(f"step 不存在: {step_id}")
        descendants = self._descendants(step_id)
        del self.steps[step_id]
        for s in self.steps.values():
            s.depends_on = [d for d in s.depends_on if d != step_id]
        for did in descendants:
            d = self.steps.get(did)
            if d and d.status not in DONE_STATUSES:
                d.status = StepStatus.PENDING

    def _descendants(self, step_id) -> set[str]:
        """沿 depends_on 传递闭包:所有直接或间接依赖 step_id 的步骤。"""
        result: set[str] = set()
        changed = True
        while changed:
            changed = False
            for sid, s in self.steps.items():
                if sid == step_id or sid in result:
                    continue
                if any(d == step_id or d in result for d in s.depends_on):
                    result.add(sid)
                    changed = True
        return result

    # ===== 校验 =====

    def validate(self) -> list[str]:
        """返回错误列表,空列表表示合法。"""
        errors = []
        for sid, s in self.steps.items():
            if not (s.instruction or "").strip():
                errors.append(f"{sid}: instruction 为空")
            if not (s.criterion or "").strip():
                errors.append(f"{sid}: criterion 为空")
            for d in s.depends_on:
                if d not in self.steps:
                    errors.append(f"{sid}: 依赖 {d} 不存在")
        try:
            self.topological_order()
        except DAGError as e:
            errors.append(str(e))
        return errors

    # ===== 拓扑排序(Kahn,按插入序保证确定性) =====

    def topological_order(self) -> list[str]:
        pos = {sid: i for i, sid in enumerate(self.steps)}
        indeg = {sid: len(s.depends_on) for sid, s in self.steps.items()}
        dependents = {sid: [] for sid in self.steps}
        for sid, s in self.steps.items():
            for d in s.depends_on:
                dependents[d].append(sid)

        queue = sorted((sid for sid, deg in indeg.items() if deg == 0), key=pos.get)
        order = []
        while queue:
            sid = queue.pop(0)
            order.append(sid)
            for nxt in dependents[sid]:
                indeg[nxt] -= 1
                if indeg[nxt] == 0:
                    queue.append(nxt)
                    queue.sort(key=pos.get)
        if len(order) != len(self.steps):
            raise DAGError("存在环,无法拓扑排序")
        return order

    # ===== 调度 =====

    def ready_steps(self) -> list[Step]:
        """PENDING 且所有依赖 PASSED,按拓扑序返回。"""
        topo = {sid: i for i, sid in enumerate(self.topological_order())}
        ready = [
            s for s in self.steps.values()
            if s.status == StepStatus.PENDING
            and all(self.steps[d].status == StepStatus.PASSED for d in s.depends_on)
        ]
        ready.sort(key=lambda s: topo[s.id])
        return ready

    def next_step(self) -> Step | None:
        ready = self.ready_steps()
        return ready[0] if ready else None

    def is_done(self) -> bool:
        """全部步骤进入终态(PASSED/SKIPPED)。"""
        return all(s.status in DONE_STATUSES for s in self.steps.values())

    # ===== 状态变更(公开 API,供外部 evaluator/engine 调用) =====

    # 合法迁移表(与 design/dag.md §2 状态机一致)
    _ALLOWED_TRANSITIONS = {
        StepStatus.PENDING: {StepStatus.RUNNING, StepStatus.SKIPPED, StepStatus.REVISE},
        StepStatus.RUNNING: {StepStatus.PASSED, StepStatus.RETRY, StepStatus.ESCALATED, StepStatus.SKIPPED, StepStatus.REVISE},
        StepStatus.RETRY: {StepStatus.RUNNING, StepStatus.SKIPPED, StepStatus.REVISE},
        StepStatus.ESCALATED: {StepStatus.PENDING, StepStatus.SKIPPED, StepStatus.REVISE},  # 反思后回 PENDING 重跑
        StepStatus.REVISE: {StepStatus.PENDING, StepStatus.SKIPPED},  # 评审通过回 PENDING 重跑;补丁可跳过
        StepStatus.PASSED: set(),
        StepStatus.SKIPPED: set(),
    }

    def set_status(self, step_id, status, *, force=False) -> Step:
        """公开状态变更 API:校验 id 与迁移,终态(PASSED/SKIPPED)不可覆盖。

        force=True 跳过迁移表与 READY 校验(终态保护仍生效),供引擎特殊场景。
        非法 status 抛 ValueError;非法迁移抛 DAGError。
        """
        s = self.steps.get(step_id)
        if s is None:
            raise DAGError(f"step 不存在: {step_id}")
        new = StepStatus(status)
        if s.status in DONE_STATUSES and new != s.status:
            raise DAGError(f"{step_id} 已终态 {s.status.value},不可改为 {new.value}")
        if not force and new != s.status and new not in self._ALLOWED_TRANSITIONS[s.status]:
            allowed = ", ".join(x.value for x in self._ALLOWED_TRANSITIONS[s.status]) or "无"
            raise DAGError(f"非法状态迁移 {step_id}: {s.status.value} -> {new.value}(允许: {allowed})")
        if not force and s.status == StepStatus.PENDING and new == StepStatus.RUNNING:
            unmet = [d for d in s.depends_on if self.steps[d].status != StepStatus.PASSED]
            if unmet:
                raise DAGError(f"{step_id} 依赖未就绪,不能置 RUNNING: {unmet}")
        s.status = new
        return s

    # ===== 补丁合并(动态重规划) =====

    def apply_patch(self, patch: Patch):
        backup = self.to_dict()
        try:
            for step in patch.add or []:
                if step.id in self.steps:
                    raise DAGError(f"add 重复 id: {step.id}")
                self.steps[step.id] = step
            for upd in patch.update or []:
                sid = upd["id"]
                # 按字段分发到语义方法,失效范围各自内聚(内容不级联,结构级联非终态后代)
                if "depends_on" in upd:
                    self.set_depends_on(sid, upd["depends_on"])
                if "instruction" in upd:
                    self.update_instruction(sid, upd["instruction"])
                if "criterion" in upd:
                    self.update_criterion(sid, upd["criterion"])
                if "skill_id" in upd:
                    self.update_skill_id(sid, upd["skill_id"])
            for sid in patch.remove or []:
                self.remove_step(sid)
            errors = self.validate()
            if errors:
                raise DAGError("; ".join(errors))
        except Exception:
            self._load(backup)
            raise

    # ===== 序列化 =====

    def to_dict(self) -> dict:
        return {
            "meta": self.meta,
            "steps": {sid: asdict(s) for sid, s in self.steps.items()},
        }

    def _load(self, d: dict):
        self.meta = dict(d.get("meta") or {})
        self.steps = {
            sid: Step(**sdict)
            for sid, sdict in (d.get("steps") or {}).items()
        }
        return self

    @classmethod
    def from_dict(cls, d: dict) -> "Blueprint":
        return cls()._load(d)
