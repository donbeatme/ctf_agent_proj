"""资源提供层:Requirement / Capability / Provider / Lease / Handle。

actor 模式并行执行器的资源抽象,五层职责各管一段:

- Requirement:一次执行的需求描述(能力匹配键 + 预装工具 + 内存估算)。由 engine 在调度步时生成。
- Capability:Provider 自报的"可调度能力"广告。**不是扩展点**,只用于调度匹配,
  不携带任何实现细节——调度器永远看不到 docker / ssh / paramiko。
- Provider:能力的具体实现 + 资源实例 + 生命周期(创建/初始化/启动/连接/健康/清理/销毁)。
  Scheduler 只依赖 acquire / release / health,不碰底层 API。
- Lease:一次执行会话暂时拥有的资源(调度器/协调器侧视角:谁持有、何时取得、句柄在哪)。
- Handle:执行器实际可调用的受限句柄。权限边界 = 能力边界,绝不裸露底层 client。

层边界:事件溯源(workspace/blueprint)、ctx 组件、LLM(llm_api 单例)、opslog 都各有自己的
生命周期,不进本层;本层只覆盖"并行执行时每 actor 需要租用的东西"(sandbox/ssh/workspace)。

Scheduler 职责(不在本模块实现):需求 → select_provider 匹配 → acquire → 只把 handle 交给
Executor → 完成后 release。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from time import time as _now


@dataclass(frozen=True)
class Capability:
    """Provider 暴露的可调度能力广告(不可变,只用于匹配,不带实现)。"""

    keys: frozenset[str] = frozenset()

    def satisfies(self, required: frozenset[str]) -> bool:
        return required <= self.keys


@dataclass(frozen=True)
class Requirement:
    """一次执行的需求描述(DAG 步派生,engine 生成)。"""

    capabilities: frozenset[str] = frozenset()   # 能力匹配键,如 {"isolated_exec", "linux"}
    tools: tuple[str, ...] = ()                  # 预装工具需求(gdb 等;非能力,交给 Provider 装)
    memory_mb: int | None = None                 # 估算内存(MB),调度准入用
    actor_id: str | None = None                  # 持有者身份 → 容器 key / lease holder
    cwd: str | None = None                       # 工作目录 → 容器 key / 同步
    labels: dict[str, str] = field(default_factory=dict)  # 可读描述(题号/目录),审计用


class Provider(ABC):
    """能力提供者:封装能力实现 + 资源实例 + 生命周期。

    Scheduler 只依赖本接口与 capability 广告;底层 docker/ssh 的具体实现、资源实例与
    生命周期(创建/启动/连接/健康/重置/清理/销毁)全部收在子类内部。
    """

    name: str = "provider"
    capability: Capability = Capability()

    def matches(self, req: Requirement) -> bool:
        """能力匹配:需求能力键 ⊆ 本 Provider 广告的能力键。"""
        return self.capability.satisfies(req.capabilities)

    async def health(self) -> dict:
        """活性/监控(可选覆盖)。返回 {"ok": bool, ...} 或描述性 dict。"""
        return {"ok": True, "name": self.name}

    @abstractmethod
    async def acquire(self, req: Requirement) -> Lease:
        """按需求租出一个 Lease:创建/初始化/启动资源实例,装配受限 Handle。"""

    @abstractmethod
    async def release(self, lease: Lease) -> None:
        """回收 Lease:清理/销毁实例,之后该 Lease 失效。"""


@dataclass
class Lease:
    """一次执行会话暂时拥有的资源(调度器/协调器侧视角)。"""

    provider: Provider
    requirement: Requirement
    holder: str                     # 持有者标识(actor id / run id)
    acquired_at: float = field(default_factory=_now)
    handle: Handle | None = None    # 执行器侧受限句柄(acquire 时装配)

    async def release(self) -> None:
        """便捷释放:委托给 provider.release(self)。"""
        await self.provider.release(self)


class Handle(ABC):
    """执行器实际可调用的受限句柄。

    具体子类只暴露被授予的能力方法(如 exec / upload / reset),绝不裸露底层
    client(docker_client / ssh_client / sftp)——权限边界 = 能力边界。
    """

    name: str = "handle"


def select_provider(providers, req: Requirement) -> Provider | None:
    """在提供者列表里按能力匹配选第一个可满足需求的(不建注册表类,调度器直接用)。"""
    for p in providers:
        if p.matches(req):
            return p
    return None


__all__ = [
    "Capability", "Requirement", "Provider", "Lease", "Handle", "select_provider",
]
