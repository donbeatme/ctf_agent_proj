"""CtxComponent 基类生命周期/渲染/压缩测试(design/workspace.md §5)。"""

import pytest

from agent.ctx import CtxComponent


class _Comp(CtxComponent):
    LEVELS = ("raw", "index", "summary")

    def __init__(self, text, **kw):
        kw.setdefault("key", "c")
        super().__init__(**kw)
        self._text = text

    def create(self, ws, **kw):
        super().create(ws, **kw)
        self._text = kw.get("text", self._text)
        return self

    def render(self):
        if self.level == 0:
            return self._text
        if self.level == 1:
            return f"[idx:{self.key}]"
        return "摘要"


def test_create_delete_lifecycle():
    c = _Comp("hello")
    assert c.created is False
    c.create(None, text="world")
    assert c.created is True
    assert c.render() == "world"
    c.delete()
    assert c.created is False


def test_create_returns_self_and_accepts_kwargs():
    c = _Comp("x").create(None, text="loaded")
    assert c.render() == "loaded"


def test_advance_level_monotonic():
    c = _Comp("hi")
    assert c.advance_level() is True    # 0 -> 1
    assert c.render() == "[idx:c]"
    assert c.advance_level() is True    # 1 -> 2
    assert c.render() == "摘要"
    assert c.advance_level() is False   # 已到顶
    assert c.level == 2


def test_clear_resets_level():
    c = _Comp("hi")
    c.advance_level()
    assert c.level == 1
    c.clear()
    assert c.level == 0
    assert c.render() == "hi"


def test_size_and_floor():
    c = _Comp("The quick brown fox jumps over the lazy dog twice today", floor=5)
    assert c.size() == 11
    assert c.at_floor() is False        # 11 > floor 5
    c.level = 2
    assert c.size() == 3                # "摘要" = 3 tokens
    assert c.at_floor() is True


def test_priority_floor_override():
    c = _Comp("x", priority=5, floor=3)
    assert c.priority == 5
    assert c.floor == 3
    assert c.key == "c"


def test_subclass_can_override_methods():
    calls = []

    class C(CtxComponent):
        def create(self, ws, **kw):
            calls.append("create")
            return super().create(ws, **kw)

        def delete(self):
            calls.append("delete")
            return super().delete()

    c = C()
    c.create(None)
    c.delete()
    assert calls == ["create", "delete"]


def test_base_render_empty():
    assert CtxComponent().render() == ""


def test_target_default_and_override():
    assert CtxComponent().target == "ctx"
    assert CtxComponent(target="system").target == "system"


def test_can_advance_respects_floor_and_top():
    c = _Comp("The quick brown fox jumps over the lazy dog", floor=2)
    assert c.can_advance() is True
    c.advance_level()                    # 0 -> 1
    assert c.can_advance() is True
    c.advance_level()                    # 1 -> 2(顶)
    assert c.can_advance() is False      # 已到顶
    c2 = _Comp("hi", floor=5)
    assert c2.can_advance() is False     # 已到下限(1 <= 5),再压是假信息


def test_on_run_end_default_deletes():
    c = _Comp("x")
    c.create(None)
    assert c.created is True
    c.on_run_end()
    assert c.created is False
