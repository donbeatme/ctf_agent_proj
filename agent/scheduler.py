"""执行环境调度器:step 需求 → Provider 匹配 → acquire → 把 handle 交给执行器 → release。

providers.py 明确把 "Scheduler 职责"(需求 → select_provider → acquire → 只把 handle
交给 Executor → 完成后 release)留到本层实现。单 actor 串行阶段:会话级租约在 run 开始
acquire、run 结束 release,容器工具/文件状态横跨步骤持久(不每步重建);actor mode 阶段
每 actor 各自开/关会话,跨 actor 容器互不复用(隔离本身)。

执行器拿到的只有受限 handle(SandboxHandle),不裸露 provider / 容器 / ssh 底层。
"""

from __future__ import annotations

from agent.env_providers import SandboxProvider
from agent.providers import Lease, Requirement, select_provider

# 沙箱步骤的能力匹配键(与 SandboxProvider 广告一致)
_SANDBOX_CAP = frozenset({"isolated_exec", "linux", "docker"})


class ExecutionScheduler:
    """执行环境调度器:需求派生 + Provider 匹配 + 会话租约生命周期。

    - requirement_for(actor_id, cwd, step):组装 Requirement(能力匹配键 + actor/cwd 上下文;
      tools 置空——沙箱 install_auto 依赖钩子按 tool_id 动态装,不阻塞调度)。
    - acquire / release:select_provider 匹配后委托 provider 借还租约。
    - close:关闭持有的所有 provider(关连接池等底层资源)。

    测试注入 FakeSsh 工厂的 providers;缺省 `[SandboxProvider()]`(ssh 后端从环境配置)。
    """

    def __init__(self, providers=None):
        self._providers = list(providers) if providers is not None else [SandboxProvider()]

    def requirement_for(self, *, actor_id=None, cwd=None, step=None) -> Requirement:
        return Requirement(
            capabilities=_SANDBOX_CAP,
            actor_id=actor_id,
            cwd=cwd,
            labels={"step_id": step.id} if step is not None else {},
        )

    def _select(self, req: Requirement):
        provider = select_provider(self._providers, req)
        if provider is None:
            raise RuntimeError(f"无 Provider 满足需求 {sorted(req.capabilities)}")
        return provider

    async def acquire(self, req: Requirement) -> Lease:
        return await self._select(req).acquire(req)

    async def release(self, lease: Lease) -> None:
        await lease.release()

    async def close(self) -> None:
        for p in self._providers:
            close = getattr(p, "close", None)
            if close is not None:
                await close()


__all__ = ["ExecutionScheduler"]
