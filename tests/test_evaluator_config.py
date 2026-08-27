"""分角色评估器:轻量 LLM 评审(ep/ee/et)+ ConfigurableEvaluator + build_evaluator。"""

import json

import pytest

from agent.evaluator import (
    ConfigurableEvaluator,
    Diagnosis,
    EvalResult,
    MockEvaluator,
    PlanLLMEvaluator,
    SmokeEvaluator,
    StepLLMEvaluator,
    TaskLLMEvaluator,
    Verdict,
    _parse_json,
    build_evaluator,
)
from agent.schema import GoalEvalDetail, Role


@pytest.fixture
def stub_llm(monkeypatch):
    """把 agent.evaluator.llm_api 的 chat/role_model/pop_token_log 换成可控桩。"""
    import agent.evaluator as ev

    calls = {"count": 0}

    def _stub(content=None, usage=True):
        def chat(system=None, prompt=None, model=None, **kw):
            calls["count"] += 1
            return content(system=system, prompt=prompt, model=model) if callable(content) else content

        monkeypatch.setattr(ev.llm_api, "chat", chat)
        monkeypatch.setattr(ev.llm_api, "role_model", lambda role=None: "stub-model")
        monkeypatch.setattr(ev.llm_api, "pop_token_log", lambda: (
            [{"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}] if usage else []
        ))
        return calls

    return _stub


# ── _parse_json ──────────────────────────────────────────────

def test_parse_json_strips_fences():
    assert _parse_json("```json\n{\"verdict\": \"pass\"}\n```") == {"verdict": "pass"}
    assert _parse_json("not json") == {}
    assert _parse_json("[1, 2]") == {}  # 非 dict 视作无效
    assert _parse_json("") == {}


# ── ROLE_SYSTEMS 软判子句 ────────────────────────────────────

def test_role_systems_soft_judgment_clauses():
    from agent.evaluator import ROLE_SYSTEMS

    for role in (Role.EVALUATOR_STEP, Role.EVALUATOR_TASK):
        sys_txt = ROLE_SYSTEMS[role]
        assert "已验证经验" in sys_txt          # 动态 flag 题软鉴定参照
        assert "correct=None" in sys_txt       # 平台未确认也能收口
        assert "has_container" in sys_txt or "动态 flag 容器题" in sys_txt


# ── PlanLLMEvaluator(ep) ─────────────────────────────────────

async def test_plan_llm_verdict_and_opinion(stub_llm):
    stub_llm(json.dumps({"verdict": "pass", "opinion": "计划可执行"}))
    r = await PlanLLMEvaluator().review("ctx")
    assert r.verdict == Verdict.PASS
    assert r.opinion == "计划可执行"


async def test_plan_llm_invalid_verdict_defaults_pass(stub_llm):
    stub_llm(json.dumps({"verdict": "weird"}))
    assert (await PlanLLMEvaluator().review("ctx")).verdict == Verdict.PASS


async def test_plan_llm_llm_failure_defaults_pass(stub_llm):
    def _boom(**kw):
        raise RuntimeError("boom")

    stub_llm(_boom)
    r = await PlanLLMEvaluator().review("ctx")
    assert r.verdict == Verdict.PASS
    assert "LLM 调用失败" in r.opinion
    assert r.total_usage is None


async def test_llm_usage_aggregated(stub_llm):
    stub_llm(json.dumps({"verdict": "pass"}))
    r = await PlanLLMEvaluator().review("ctx")
    assert r.total_usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


async def test_llm_calls_ctx_and_role_model(stub_llm):
    calls = stub_llm(json.dumps({"verdict": "fail"}), usage=False)

    def _capture(**kw):
        calls["seen"] = kw
        return json.dumps({"verdict": "fail"})

    stub_llm(_capture, usage=False)
    await PlanLLMEvaluator().review("原始 ctx 文本")
    assert calls["seen"]["prompt"] == "原始 ctx 文本"
    assert calls["seen"]["model"] == "stub-model"


# ── StepLLMEvaluator(ee) ─────────────────────────────────────

async def test_step_llm_parses_is_completed(stub_llm):
    stub_llm(json.dumps({"verdict": "pass", "is_completed": True, "opinion": "已确认"}))
    r = await StepLLMEvaluator().step_eval("ctx")
    assert r.verdict == Verdict.PASS
    assert r.is_completed is True


async def test_step_llm_invalid_verdict_defaults_retry(stub_llm):
    stub_llm(json.dumps({"verdict": "nope"}))
    r = await StepLLMEvaluator().step_eval("ctx")
    assert r.verdict == Verdict.RETRY
    assert r.is_completed is False


async def test_step_llm_parses_diagnosis(stub_llm):
    stub_llm(json.dumps({"verdict": "retry", "diagnosis": "drift", "opinion": "方向偏"}))
    r = await StepLLMEvaluator().step_eval("ctx")
    assert r.verdict == Verdict.RETRY
    assert r.diagnosis == Diagnosis.DRIFT


async def test_step_llm_invalid_diagnosis_defaults_other(stub_llm):
    stub_llm(json.dumps({"verdict": "retry", "diagnosis": "bogus"}))
    assert (await StepLLMEvaluator().step_eval("ctx")).diagnosis == Diagnosis.OTHER


async def test_step_llm_missing_diagnosis_defaults_other(stub_llm):
    stub_llm(json.dumps({"verdict": "retry"}))
    assert (await StepLLMEvaluator().step_eval("ctx")).diagnosis == Diagnosis.OTHER


def test_role_systems_step_has_diagnosis_contract():
    from agent.evaluator import ROLE_SYSTEMS

    sys_txt = ROLE_SYSTEMS[Role.EVALUATOR_STEP]
    assert '"diagnosis"' in sys_txt
    for v in ("incomplete", "drift", "planner_target", "other"):
        assert v in sys_txt


async def test_step_llm_eval_goals(stub_llm):
    """逐 goal LLM 软鉴定:按 goal id 返回 JSON,结果保序、字段解析正确。"""
    def _chat(system=None, prompt=None, model=None, **kw):
        if "g1" in (prompt or ""):
            return json.dumps({"complete": True, "evidence": ["s1"], "reasoning": "g1 达成"})
        return json.dumps({"complete": False, "evidence": [], "reasoning": "g2 证据不足"})

    stub_llm(_chat)
    results = await StepLLMEvaluator().eval_goals("ctx", [{"id": "g1"}, {"id": "g2"}],
                                                  "[s1] status=PASSED")
    assert len(results) == 2
    assert [r.goal_id for r in results] == ["g1", "g2"]   # 保序
    assert results[0].complete is True and results[0].evidence == ["s1"]
    assert results[0].reasoning == "g1 达成"
    assert results[1].complete is False and results[1].evidence == []
    assert results[1].reasoning == "g2 证据不足"


async def test_step_llm_eval_goals_empty():
    assert (await StepLLMEvaluator().eval_goals("ctx", [], "[s1] status=PASSED")) == []


async def test_step_llm_eval_goals_llm_failure_conservative(stub_llm):
    """LLM 失败:保守收口 complete=False + 失败信息,不臆测达成。"""
    def _boom(**kw):
        raise RuntimeError("llm down")

    stub_llm(_boom)
    results = await StepLLMEvaluator().eval_goals("ctx", [{"id": "g1"}], "[s1] status=PASSED")
    assert len(results) == 1
    r = results[0]
    assert r.complete is False and r.evidence == []
    assert "LLM 调用失败" in r.reasoning


async def test_step_llm_eval_goals_does_not_drain_log(monkeypatch):
    """drain_usage=False:逐 goal 线程不 drain 全局 token log(聚合留给引擎 _llm_wrap)。"""
    import agent.evaluator as ev_mod

    monkeypatch.setattr(ev_mod.llm_api, "chat",
                        lambda **kw: json.dumps({"complete": True, "reasoning": "ok"}))
    monkeypatch.setattr(ev_mod.llm_api, "role_model", lambda role=None: "stub")
    drained = []
    monkeypatch.setattr(ev_mod.llm_api, "pop_token_log",
                        lambda: drained.append(1) or [])
    await StepLLMEvaluator().eval_goals("ctx", [{"id": "g1"}, {"id": "g2"}], "dag")
    assert drained == []   # 逐 goal 调用不 drain


async def test_step_llm_eval_goals_parallel(monkeypatch):
    """多 goal asyncio 并发:确实并发(max 活跃数 >=2)且墙钟 < 串行和。"""
    import asyncio
    import time as _time
    import agent.evaluator as ev_mod

    active = {"n": 0, "max": 0}

    async def _chat(**kw):
        active["n"] += 1
        active["max"] = max(active["max"], active["n"])
        await asyncio.sleep(0.2)
        active["n"] -= 1
        return json.dumps({"complete": False, "reasoning": "x"})

    monkeypatch.setattr(ev_mod.llm_api, "chat", _chat)
    monkeypatch.setattr(ev_mod.llm_api, "role_model", lambda role=None: "stub")
    monkeypatch.setattr(ev_mod.llm_api, "pop_token_log", lambda: [])
    t0 = _time.monotonic()
    await StepLLMEvaluator().eval_goals("ctx", [{"id": g} for g in "abcd"], "dag")
    wall = _time.monotonic() - t0
    assert active["max"] >= 2          # 确实并发
    assert wall < 0.2 * 4 - 0.05       # 墙钟明显低于串行和(0.8s)


# ── TaskLLMEvaluator(et) ─────────────────────────────────────

async def test_task_llm_verdicts(stub_llm):
    stub_llm(json.dumps({"verdict": "replan", "opinion": "重规划"}))
    r = await TaskLLMEvaluator().reflect("ctx")
    assert r.verdict == Verdict.REPLAN
    stub_llm(json.dumps({"verdict": "done"}))
    assert (await TaskLLMEvaluator().reflect("ctx")).verdict == Verdict.DONE


async def test_task_llm_invalid_verdict_defaults_done(stub_llm):
    stub_llm(json.dumps({"verdict": "wat"}))
    assert (await TaskLLMEvaluator().reflect("ctx")).verdict == Verdict.DONE


# ── ConfigurableEvaluator ────────────────────────────────────

async def test_configurable_dispatch():
    ep = MockEvaluator({Role.EVALUATOR_PLAN: EvalResult(Verdict.FAIL, "ep 意见")})
    ee = MockEvaluator({Role.EVALUATOR_STEP: EvalResult(Verdict.RETRY, "ee 意见")})
    et = MockEvaluator({Role.EVALUATOR_TASK: EvalResult(Verdict.REPLAN, "et 意见")})
    ev = ConfigurableEvaluator({
        Role.EVALUATOR_PLAN: ep, Role.EVALUATOR_STEP: ee, Role.EVALUATOR_TASK: et,
    })
    assert (await ev.review("x")).verdict == Verdict.FAIL
    assert (await ev.step_eval("x")).verdict == Verdict.RETRY
    assert (await ev.reflect("x")).verdict == Verdict.REPLAN


async def test_configurable_eval_goals_delegates_to_ee():
    class EEDelegate(MockEvaluator):
        async def eval_goals(self, ctx, goals, dag_summary):
            return [GoalEvalDetail(goal_id="g1", complete=True, evidence=["s1"])]

    ev = ConfigurableEvaluator({Role.EVALUATOR_STEP: EEDelegate({})})
    results = await ev.eval_goals("ctx", [{"id": "g1"}], "x")
    assert results[0].complete is True
    assert results[0].evidence == ["s1"]


def test_configurable_system_for():
    ev = ConfigurableEvaluator({
        Role.EVALUATOR_PLAN: PlanLLMEvaluator(),
        Role.EVALUATOR_STEP: MockEvaluator({}),
        Role.EVALUATOR_TASK: MockEvaluator({}),
    })
    assert "计划评审" in ev.system_for(Role.EVALUATOR_PLAN)
    assert ev.system_for(Role.EVALUATOR_STEP) == ""
    assert ev.system_for(Role.EVALUATOR_TASK) == ""


# ── build_evaluator 工厂 ─────────────────────────────────────

async def test_build_evaluator_all_mock_smoke(monkeypatch):
    monkeypatch.setattr("agent.evaluator.cfg_get", lambda name, default=None: "mock")
    from agent.workspace import MockWorkspace

    ev = build_evaluator(MockWorkspace())
    assert (await ev.review("x")).verdict == Verdict.FAIL  # 空 blueprint → FAIL(驱动重规划)
    assert (await ev.step_eval("x")).verdict == Verdict.PASS
    assert (await ev.reflect("x")).verdict == Verdict.DONE
    for role in (Role.EVALUATOR_PLAN, Role.EVALUATOR_STEP, Role.EVALUATOR_TASK):
        assert isinstance(ev._get(role), SmokeEvaluator)


def test_build_evaluator_modes_override(monkeypatch):
    from agent.workspace import MockWorkspace

    ev = build_evaluator(MockWorkspace(), modes={
        Role.EVALUATOR_PLAN: "real",
        Role.EVALUATOR_STEP: "mock",
        Role.EVALUATOR_TASK: "REAL",
    })
    assert isinstance(ev._get(Role.EVALUATOR_PLAN), PlanLLMEvaluator)
    assert isinstance(ev._get(Role.EVALUATOR_STEP), SmokeEvaluator)
    assert isinstance(ev._get(Role.EVALUATOR_TASK), TaskLLMEvaluator)


async def test_build_evaluator_real_calls_llm(monkeypatch):
    import agent.evaluator as ev_mod

    monkeypatch.setattr(ev_mod.llm_api, "chat",
                        lambda **kw: json.dumps({"verdict": "fail", "opinion": "结构问题"}))
    monkeypatch.setattr(ev_mod.llm_api, "role_model", lambda role=None: "stub-model")
    monkeypatch.setattr(ev_mod.llm_api, "pop_token_log", lambda: [])
    from agent.workspace import MockWorkspace

    ev = build_evaluator(MockWorkspace(), modes={Role.EVALUATOR_PLAN: "real"})
    r = await ev.review("ctx")
    assert r.verdict == Verdict.FAIL
    assert r.opinion == "结构问题"
