"""CtxAssembler 压缩 API 测试:LLM 驱动溢出压缩、机械降级兜底、增量摘要、预热、持久化。

设计见 design/workspace.md §5.3/§6.1。
核心不变量:
- 溢出压缩优先 LLM:超预算内容 + 压缩提示词(压缩目的 / 优先级 / 占比 /
  当前触发压缩的 agent 目的 / 按需压缩方式)交给 compress 回调,LLM 决定怎么压
- 未注入 compress 或 LLM 输出超限 → 机械降级(advance_level 逐档),确定性兜底
- History 增量摘要:折叠标记 _folded_passes(已折入的 PASS 条数),只压新出现的
  PASS 事件;非 PASS(失败/升级/评审)保留原文。旧摘要缓存 + 持久化到
  ws.summaries,跨 replan / 断点续跑不重付 LLM
"""

import pytest

from agent.blueprint import Blueprint, Step
from agent.ctx import (
    AgentCommComponent,
    CtxAssembler,
    DagComponent,
    DocsComponent,
    HistoryComponent,
    SystemPromptComponent,
    TaskComponent,
)
from agent.workspace import Workspace


@pytest.fixture
def ws(tmp_path):
    return Workspace.create("run-comp", {"q": "x"}, root=tmp_path)


def make_assembler(ws, compress=None):
    a = CtxAssembler(ws, compress=compress)
    a.register(
        "planner",
        SystemPromptComponent(),
        TaskComponent(),
        AgentCommComponent(),
        DagComponent(),
        HistoryComponent(),
        DocsComponent(),
    )
    return a


def comp(a, key):
    return next(c for c in a.components("planner") if c.key == key)


def seeded(ws):
    bp = Blueprint(meta={"task": "t"})
    bp.add_step(Step(id="s1", instruction="做", criterion="可验收"))
    bp.add_step(Step(id="s2", instruction="做二", criterion="可验收", depends_on=["s1"]))
    ws.set_blueprint(bp)
    ws.add_event("planner", "replan")
    ws.record_step("s1", "retry", attempts=1)
    ws.record_step("s1", "pass")
    return bp


async def test_overflow_llm_gets_content_and_rich_prompt(ws):
    """溢出压缩:LLM 收到待压缩内容 + 压缩提示词(目的/优先级/占比/agent 目的/按需方式)。"""
    seeded(ws)
    calls = []

    async def fake_compress(prompt, content):
        calls.append((prompt, content))
        return "[压]"

    a = make_assembler(ws, compress=fake_compress)
    ctx, _, over = await a.assemble("planner", raw_content={"q": "x"}, budget=100,
                                    purpose="正在修订计划,关注 s1 为何 retry")

    assert over == 0
    assert "[压]" in ctx                          # LLM 输出作为 ctx
    assert len(calls) == 1
    prompt, content = calls[0]
    assert "压缩目的" in prompt
    assert "当前触发压缩的 agent 目的" in prompt
    assert "正在修订计划,关注 s1 为何 retry" in prompt     # purpose 覆盖生效
    assert "压缩优先级" in prompt
    assert "占比" in prompt
    assert "按需压缩方式" in prompt
    assert "骨架化" in prompt                       # dag 的按需压缩方式
    assert "索引替换" in prompt                     # history 的按需压缩方式
    assert "保留原文" in prompt and "task" in prompt
    assert "kind=replan" in content                # 待压缩内容含 history 原文
    assert '"s1"' in content                        # 待压缩内容含 dag 原文
    assert "任务" not in content                    # task 保留原文,不进 LLM


async def test_overflow_llm_overshoot_falls_back_to_mechanical(ws):
    seeded(ws)
    calls = []

    async def fake_compress(prompt, content):
        calls.append(content)
        return "oversized " * 200                  # LLM 输出超限(~200 token,不可用)

    a = make_assembler(ws, compress=fake_compress)
    ctx, _, over = await a.assemble("planner", raw_content={"q": "x"}, budget=200)

    assert len(calls) == 1                          # 溢出只试一次 LLM,失败不重试
    assert "oversized" not in ctx                   # 没用超限的 LLM 输出
    assert comp(a, "history").level >= 1            # 兜底机械降级(索引替换,只压 PASS)
    assert comp(a, "dag").level >= 1                # 兜底机械降级(骨架化)
    assert over == 0                                # 机械兜底确定性:不重付 LLM,
                                                    # 索引/骨架档收进预算


async def test_overflow_no_compress_uses_mechanical(ws):
    seeded(ws)
    a = make_assembler(ws)                          # 不注入 compress
    ctx, _, over = await a.assemble("planner", raw_content={"q": "x"}, budget=205)
    assert over == 0
    assert comp(a, "history").level >= 1            # 纯机械降级(索引替换,只压 PASS)


async def test_history_precompress_incremental(ws):
    """增量摘要:只压自折叠标记以来新出现的 PASS 事件;非 PASS 事件不进折叠。"""
    seeded(ws)
    calls = []

    async def fake_compress(prompt, content):
        calls.append(content)
        return "[摘要]"

    a = make_assembler(ws, compress=fake_compress)
    await a.precompress("planner")
    assert len(calls) == 1
    assert ws.summaries["planner:history"]["passes"] == 1   # 只折了 PASS 事件
    assert "retry" not in calls[0] and "replan" not in calls[0]  # 非 PASS 不进折叠

    await a.precompress("planner")                  # 折叠标记未动 → 不再调
    assert len(calls) == 1

    ws.record_step("s2", "pass")
    await a.precompress("planner")                  # 只折新 PASS delta
    assert len(calls) == 2
    assert "step=s2" in calls[1]
    assert "step=s1" not in calls[1]


async def test_summary_cache_persists_across_load(tmp_path, ws):
    seeded(ws)
    calls = []

    async def fake_compress(prompt, content):
        calls.append(content)
        return "[摘要]"

    a = make_assembler(ws, compress=fake_compress)
    await a.precompress("planner")
    assert len(calls) == 1
    ws.sync()

    ws2 = Workspace.load("run-comp", root=tmp_path)
    a2 = make_assembler(ws2, compress=fake_compress)
    await a2.precompress("planner")
    assert len(calls) == 1                          # 断点续跑后折叠标记恢复,不重折


async def test_precompress_warms_cache_for_mechanical_fallback(ws):
    """预热缓存 → LLM 超限后机械兜底,history 摘要档直接读缓存,不再付 LLM 往返。"""
    seeded(ws)
    calls = []

    async def fake_compress(prompt, content):
        calls.append(content)
        return "[摘要]" if len(calls) == 1 else "X" * 500

    a = make_assembler(ws, compress=fake_compress)
    await a.precompress("planner")                  # 预热:折一次
    ctx, _, _ = await a.assemble("planner", raw_content={"q": "x"}, budget=40)
    assert len(calls) == 2                          # 1 预热 + 1 溢出尝试(超限)
    assert "[摘要]" in ctx                          # 摘要档渲染缓存,零额外 LLM
