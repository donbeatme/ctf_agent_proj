"""audit 正确性权威让位 _local_verify/平台:reflect 三层判定 + 动态 flag 不回环。"""

import pytest

from agent.blueprint import Blueprint, Step
from agent.evaluator import Verdict
from audit import AgentAuditEvaluator, AgentRuntimeBindings
from audit.evaluators.plan import PlanEvaluator
from audit.flag_verifier import FlagVerifier
from audit.integrations.experience import LocalExperienceStore
from audit.schemas import PlanEvaluation
from audit.settings import Settings


def _offline_settings(tmp_path):
    return Settings(
        mode="offline",
        data_dir=tmp_path / "data",
        langsmith_enabled=False,
        llm_api_key=None,
        llm_base_url="",
        llm_model="",
        ragflow_enabled=False,
        ragflow_api_key=None,
        ragflow_base_url="",
        ragflow_dataset_name="",
        experience_search_limit=3,
    )


def _make_evaluator(tmp_path, verifier=None, submission=None, submitted_flag=None):
    state = {"submission": submission, "flag": submitted_flag}
    bindings = AgentRuntimeBindings(
        blueprint=lambda: Blueprint(meta={}),
        task=lambda: {"task_id": "t1", "title": "delegation test"},
        current_step=lambda: None,
        observation=lambda: "",
        submitted_flag=lambda: state["flag"],
        completed=lambda: False,
        submission_result=lambda: state["submission"],
    )
    evaluator = AgentAuditEvaluator(
        settings=_offline_settings(tmp_path),
        verifier=verifier or FlagVerifier({}),
        experience_store=LocalExperienceStore(tmp_path / "data" / "exp.jsonl"),
        run_id="delegation-test",
        agent_id="ctf-agent",
        bindings=bindings,
    )
    # 跳过结构评审,只测 reflect 的 flag 合并判定
    evaluator.plan_evaluation = PlanEvaluation(
        decision="pass", score=1.0, issues=[], suggestions=[], evaluator="test"
    )
    return evaluator


async def test_correct_true_platform_verdict_done(tmp_path):
    sub = {"flag": "flag{x}", "ok": True, "correct": True, "message": "ok"}
    ev = _make_evaluator(tmp_path, submission=sub, submitted_flag="flag{x}")
    res = await ev.reflect("ctx")
    assert res.verdict == Verdict.DONE
    flag = ev.last_record.flag
    assert flag.valid is True and flag.mode == "platform" and flag.submitted is True


async def test_correct_false_replan(tmp_path):
    sub = {"flag": "flag{wrong}", "ok": True, "correct": False, "message": "提交错误"}
    ev = _make_evaluator(tmp_path, submission=sub, submitted_flag="flag{wrong}")
    res = await ev.reflect("ctx")
    assert res.verdict == Verdict.REPLAN
    assert ev.last_record.flag.valid is False


async def test_dynamic_flag_missing_rule_submitted_no_loop(tmp_path):
    """Hack World 场景:correct=None + 无规则 + 已提交(ok=True)→ DONE,不回环。"""
    sub = {"flag": "flag{uuid}", "ok": True, "correct": None, "message": "无法本地判定"}
    ev = _make_evaluator(tmp_path, submission=sub, submitted_flag="flag{uuid}")
    res = await ev.reflect("ctx")
    assert res.verdict == Verdict.DONE
    flag = ev.last_record.flag
    assert flag.valid is None and flag.mode == "missing" and flag.submitted is True


async def test_never_submitted_missing_rule_replan(tmp_path):
    ev = _make_evaluator(tmp_path, submission=None, submitted_flag=None)
    res = await ev.reflect("ctx")
    assert res.verdict == Verdict.REPLAN
    flag = ev.last_record.flag
    assert flag.valid is None and flag.submitted is False


async def test_static_rule_fallback_when_correct_none(tmp_path):
    verifier = FlagVerifier({"t1": {"mode": "exact", "value": "flag{good}"}})
    ev = _make_evaluator(
        tmp_path, verifier=verifier,
        submission={"flag": "flag{good}", "ok": True, "correct": None},
        submitted_flag="flag{good}",
    )
    res = await ev.reflect("ctx")
    assert res.verdict == Verdict.DONE
    flag = ev.last_record.flag
    assert flag.valid is True and flag.mode == "exact"


async def test_static_rule_fallback_replans_on_mismatch(tmp_path):
    verifier = FlagVerifier({"t1": {"mode": "exact", "value": "flag{good}"}})
    ev = _make_evaluator(
        tmp_path, verifier=verifier,
        submission={"flag": "flag{bad}", "ok": True, "correct": None},
        submitted_flag="flag{bad}",
    )
    res = await ev.reflect("ctx")
    assert res.verdict == Verdict.REPLAN
    assert ev.last_record.flag.valid is False


def test_settings_mode_invalid_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("CTF_AUDIT_MODE", "bogus")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_settings_auto_online_when_key_present(monkeypatch):
    """未显式配 CTF_AUDIT_MODE 但有 LLM key → 自动 online(评估 agent 走 LLM 并计入 token 结算)。"""
    monkeypatch.delenv("CTF_AUDIT_MODE", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    assert Settings.from_env().mode == "online"


def test_settings_auto_offline_without_key(monkeypatch):
    """无 key 也无显式模式 → 保持 offline 确定性规则。"""
    monkeypatch.delenv("CTF_AUDIT_MODE", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert Settings.from_env().mode == "offline"


def test_settings_explicit_offline_respected(monkeypatch):
    """显式 CTF_AUDIT_MODE=offline 即使有 key 也保持 offline。"""
    monkeypatch.setenv("CTF_AUDIT_MODE", "offline")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    assert Settings.from_env().mode == "offline"


# ===== step_eval:平台确认 correct=True → 强制 pass + is_completed =====

def _step_eval_evaluator(tmp_path, submission=None, step=None,
                         observation="Connection denied: access forbidden"):
    """离线 step_eval 评估器:observation 默认含失败关键词(denied/forbidden),
    离线规则会判失败——用来验证 diagnosis 三分类与 correct=True 强制验收。"""
    bindings = AgentRuntimeBindings(
        blueprint=lambda: Blueprint(meta={}),
        task=lambda: {"task_id": "t1", "title": "step eval"},
        current_step=lambda: step or Step(id="s1", instruction="提交 flag", criterion="平台确认"),
        observation=lambda: observation,
        submitted_flag=lambda: "flag{x}" if submission and submission.get("correct") is True else None,
        completed=lambda: False,
        submission_result=lambda: submission,
    )
    return AgentAuditEvaluator(
        settings=_offline_settings(tmp_path),
        verifier=FlagVerifier({}),
        experience_store=LocalExperienceStore(tmp_path / "data" / "exp.jsonl"),
        run_id="step-eval-test",
        agent_id="ctf-agent",
        bindings=bindings,
    )


async def test_step_eval_force_pass_on_confirmed_correct(tmp_path):
    """correct=True → 关键词误判被覆盖,强制 pass + is_completed=True,落账 item 同步修正。"""
    ev = _step_eval_evaluator(
        tmp_path, submission={"flag": "flag{x}", "ok": True, "correct": True,
                              "message": "提交成功,答案正确"})
    res = await ev.step_eval("ctx")
    assert res.verdict == Verdict.PASS
    assert res.is_completed is True
    assert ev.step_items[-1].decision == "pass"
    assert "强制验收通过" in ev.step_items[-1].reasoning


async def test_step_eval_not_forced_without_confirmation(tmp_path):
    """无提交判定 → 离线规则照常判失败(retry),is_completed 不置位。"""
    ev = _step_eval_evaluator(tmp_path, submission=None)
    res = await ev.step_eval("ctx")
    assert res.verdict == Verdict.RETRY
    assert res.is_completed is False


async def test_step_eval_not_forced_when_correct_none(tmp_path):
    """动态 flag 题(correct=None,未确认)→ 不强制 pass,避免误判完成。"""
    ev = _step_eval_evaluator(
        tmp_path, submission={"flag": "flag{uuid}", "ok": True, "correct": None,
                              "message": "无法本地判定"})
    res = await ev.step_eval("ctx")
    assert res.verdict == Verdict.RETRY
    assert res.is_completed is False


# ===== diagnosis 三分类:驱动引擎 retry 继承 ctx / 压缩纠偏 / 单节点重设计 =====

async def test_step_eval_round_limit_incomplete_retry(tmp_path):
    """executor 8 轮工具循环超上限 → 不再误判 pass,降为 INCOMPLETE retry 继承前几轮 ctx。"""
    from agent.evaluator import Diagnosis

    ev = _step_eval_evaluator(
        tmp_path, submission=None,
        observation="执行 Agent 工具循环超上限(8 轮),已执行 16 次工具调用")
    res = await ev.step_eval("ctx")
    assert res.verdict == Verdict.RETRY
    assert res.diagnosis == Diagnosis.INCOMPLETE
    assert "工具循环达上限" in res.opinion
    assert ev.step_items[-1].decision == "retry"


async def test_step_eval_drift_on_repeated_failure(tmp_path):
    """同工具+参数反复失败 → 方向偏,DRIFT retry(压缩 ctx),不再直接 escalate。"""
    from agent.evaluator import Diagnosis

    ev = _step_eval_evaluator(tmp_path, submission=None)
    await ev.step_eval("ctx")           # 首次失败 → 基线 retry
    res = await ev.step_eval("ctx")     # 相同 tool+args 重复 → 方向偏
    assert res.verdict == Verdict.RETRY
    assert res.diagnosis == Diagnosis.DRIFT
    assert "执行方向偏离" in res.opinion
    assert ev.step_items[-1].decision == "retry"


async def test_step_eval_planner_target_on_attempts_exhausted(tmp_path):
    """步骤重试耗尽仍失败 → PLANNER_TARGET,escalate 单节点重设计(scope 当前步)。"""
    from agent.evaluator import Diagnosis

    step = Step(id="s1", instruction="提取 flag", criterion="平台确认", max_attempts=2)
    step.attempts = 2                   # 已到上限
    ev = _step_eval_evaluator(
        tmp_path, submission=None, step=step,
        observation="guess attempt: connection error")
    res = await ev.step_eval("ctx")
    assert res.verdict == Verdict.ESCALATE
    assert res.diagnosis == Diagnosis.PLANNER_TARGET
    assert ev.step_items[-1].decision == "escalate"


async def test_step_eval_normal_pass_other(tmp_path):
    """正常通过 → PASS,diagnosis OTHER(不干扰引擎分流)。"""
    from agent.evaluator import Diagnosis

    ev = _step_eval_evaluator(tmp_path, submission=None,
                              observation="任务完成,flag 已提交")
    res = await ev.step_eval("ctx")
    assert res.verdict == Verdict.PASS
    assert res.diagnosis == Diagnosis.OTHER
    assert res.is_completed is False


# ===== PlanEvaluator:markdown 自由文本判定解析(ep 全不过回环的根因) =====

def test_plan_parse_markdown_pass_not_misread_as_revise():
    """LLM 以 markdown 说 '判定：**pass**' → 不得误判 revise(否则 ep 无限回环)。"""
    raw = "判定：**pass**\n\n整体计划结构完整,依赖无环,当前计划可通过评审并进入执行。"
    assert PlanEvaluator._parse_json(raw)["decision"] == "pass"


def test_plan_parse_markdown_revise():
    raw = "## 评审判定：revise\n\n计划存在回退路径结构缺陷,需要修订。"
    assert PlanEvaluator._parse_json(raw)["decision"] == "revise"


def test_plan_parse_plain_json():
    assert PlanEvaluator._parse_json(
        '{"decision":"pass","score":0.95,"issues":[],"suggestions":[]}'
    )["decision"] == "pass"


def test_plan_parse_json_missing_decision_defaults_conservative():
    """JSON 无 decision 字段 → 保守 revise(不臆测 pass)。"""
    assert PlanEvaluator._parse_json('{"score":0.9,"issues":["x"]}')["decision"] == "revise"


def test_plan_parse_no_signal_defaults_conservative():
    assert PlanEvaluator._parse_json("完全无关的文本")["decision"] == "revise"


# ===== plan opinion:revise 无理由时不得误报"结构完整" =====


def test_plan_opinion_revise_no_reasons_not_complete():
    """revise + 空 issues/suggestions → 兜底文本不得说"结构完整",要显式承认无理由。"""
    op = AgentAuditEvaluator._plan_opinion(
        PlanEvaluation("revise", 1.0, [], [], "x"))
    assert "完整" not in op
    assert "revise" in op and "未提供" in op


def test_plan_opinion_pass_no_issues_keeps_original():
    assert AgentAuditEvaluator._plan_opinion(
        PlanEvaluation("pass", 1.0, [], [], "x")) == "计划结构和验收条件完整"


def test_plan_opinion_issues_take_precedence():
    op = AgentAuditEvaluator._plan_opinion(
        PlanEvaluation("revise", 0.5, ["回退路径被阻塞"], [], "x"))
    assert "回退路径被阻塞" in op


class _StubPlanLlm:
    """available=True 且固定返回给定文本的计划评审 LLM 桩。"""

    def __init__(self, content):
        self._content = content
        self.available = True
        self.last_usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

    async def complete(self, messages, temperature=0.2):
        import types
        return types.SimpleNamespace(content=self._content, usage=self.last_usage)


def _plan_attempt():
    from audit.schemas import CTFAttempt, PlanStep

    step = PlanStep(
        plan_step_id="s1",
        goal="完成逆向",
        action="执行逆向流程",
        instruction="对目标做静态分析",
        criterion="通过校验函数 verify 确认 flag 提取成功",
    )
    return CTFAttempt(
        attempt_id="a1", task_id="t1", agent_id="ctf-agent",
        category="reverse", started_at="", ended_at="", steps=[], plan=[step],
        metadata={},
    )


async def test_plan_evaluator_revise_without_reasons_carries_raw():
    """LLM 只回 {\"decision\":\"revise\"}(无 issue/suggestion/opinion)→ 结构 1.0 但决策 revise,
    issues 落原始输出,opinion 不再误报"结构完整"。"""
    ev = PlanEvaluator(_StubPlanLlm('{"decision": "revise"}'))
    res = await ev.evaluate(_plan_attempt())
    assert res.decision == "revise"
    assert res.score == 1.0                       # 结构评审满分(pass 来源)
    assert res.issues and "评审未给出结构化修订项" in res.issues[0]
    assert "完整" not in AgentAuditEvaluator._plan_opinion(res)


async def test_plan_evaluator_revise_empty_string_issue_carries_raw():
    """pwn_t5 路径:LLM 回 {\"decision\":\"revise\",\"issues\":[\"\"]}。

    issues=[""] 是真值列表但全空串,旧 guard 的 `not issues` 为假而跳过,`_unique` 滤空后
    issues 归零 → opinion 退化成无信息兜底。回归该盲区:any() 判定必须触发。
    """
    ev = PlanEvaluator(_StubPlanLlm('{"decision": "revise", "issues": [""], "suggestions": []}'))
    res = await ev.evaluate(_plan_attempt())
    assert res.decision == "revise"
    assert res.issues and "评审未给出结构化修订项" in res.issues[0]
    assert "完整" not in AgentAuditEvaluator._plan_opinion(res)


async def test_plan_evaluator_revise_with_opinion_uses_opinion():
    """LLM 给了 opinion 视为有理由:guard 不再追加诊断,opinion 直接成为评审意见。"""
    ev = PlanEvaluator(_StubPlanLlm(
        '{"decision": "revise", "opinion": "计划缺少回退策略", "issues": [], "suggestions": []}'))
    res = await ev.evaluate(_plan_attempt())
    assert res.decision == "revise"
    assert res.opinion == "计划缺少回退策略"
    assert "评审未给出" not in " ".join(res.issues)
    assert AgentAuditEvaluator._plan_opinion(res) == "计划缺少回退策略"


def test_plan_opinion_prefers_opinion_over_fallback():
    """_plan_opinion:opinion 非空且无结构 issues → 用 opinion(非兜底)。"""
    op = AgentAuditEvaluator._plan_opinion(
        PlanEvaluation("revise", 1.0, [], [], "x", opinion="结构需补强"))
    assert op == "结构需补强"
