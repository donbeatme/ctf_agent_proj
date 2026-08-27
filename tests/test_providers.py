"""资源提供层父类(providers.py)测试:能力匹配 / acquire-release 契约 / 调度选择。"""

import asyncio

from agent.providers import (
    Capability, Handle, Lease, Provider, Requirement, select_provider,
)


class _FakeHandle(Handle):
    name = "fake"


class _FakeProvider(Provider):
    name = "fake"
    capability = Capability(keys=frozenset({"isolated_exec", "linux"}))

    def __init__(self):
        self.acquired = []
        self.released = []

    async def acquire(self, req):
        self.acquired.append(req)
        return Lease(provider=self, requirement=req, holder="ex1",
                     handle=_FakeHandle())

    async def release(self, lease):
        self.released.append(lease)


def _run(coro):
    return asyncio.run(coro)


def test_capability_satisfies_subset():
    cap = Capability(keys=frozenset({"isolated_exec", "linux"}))
    assert cap.satisfies(frozenset({"linux"}))
    assert cap.satisfies(frozenset({"isolated_exec", "linux"}))
    assert not cap.satisfies(frozenset({"linux", "gpu"}))


def test_provider_matches_requirement():
    p = _FakeProvider()
    assert p.matches(Requirement(capabilities=frozenset({"linux"})))
    assert p.matches(Requirement(capabilities=frozenset({"isolated_exec", "linux"})))
    assert not p.matches(Requirement(capabilities=frozenset({"gpu"})))


def test_requirement_defaults():
    req = Requirement()
    assert req.capabilities == frozenset()
    assert req.tools == ()
    assert req.memory_mb is None
    assert req.labels == {}


def test_acquire_release_roundtrip():
    p = _FakeProvider()
    req = Requirement(capabilities=frozenset({"linux"}), tools=("gdb",), memory_mb=256)

    async def _flow():
        lease = await p.acquire(req)
        assert isinstance(lease, Lease)
        assert lease.provider is p
        assert lease.requirement is req
        assert lease.holder == "ex1"
        assert isinstance(lease.handle, _FakeHandle)
        await lease.release()

    _run(_flow())
    assert len(p.acquired) == 1 and p.acquired[0] is req
    assert len(p.released) == 1


def test_provider_health_default():
    p = _FakeProvider()
    assert _run(p.health()) == {"ok": True, "name": "fake"}


def test_select_provider_first_match():
    gpu = _FakeProvider()  # 不含 gpu
    gpu.capability = Capability(keys=frozenset({"isolated_exec", "linux", "gpu"}))
    linux = _FakeProvider()
    req = Requirement(capabilities=frozenset({"linux"}))
    assert select_provider([gpu, linux], req) is gpu
    assert select_provider([linux], Requirement(capabilities=frozenset({"gpu"}))) is None
