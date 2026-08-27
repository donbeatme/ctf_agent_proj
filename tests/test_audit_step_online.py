"""audit StepEvaluator 在线路径:模型 JSON 缺 score 时不得 KeyError,降级前先矫正。"""

import json

from audit.integrations.llm_chat import (
    LlmApiAgentEvalsClient,
    _LlmApiCompletions,
    _score_safe_content,
)


def _json_loads(text: str) -> dict:
    return json.loads(text)


def test_score_safe_valid_json_passthrough():
    content = '{"score": 0.8, "reasoning": "工具执行成功"}'
    assert _score_safe_content(content) == content


def test_score_safe_verdict_style_json_gets_score():
    """模型按 STEP_PROMPT 口径返回 verdict 风格 → 按 verdict 推断 score,不缺键。"""
    out = _score_safe_content('{"verdict": "retry", "opinion": "未达成 criterion"}')
    parsed = _json_loads(out)
    assert parsed["score"] == 0.5
    assert "未达成 criterion" in parsed["reasoning"]


def test_score_safe_markdown_fence_stripped():
    out = _score_safe_content('```json\n{"score": 0.9, "reasoning": "ok"}\n```')
    assert _json_loads(out)["score"] == 0.9


def test_score_safe_garbage_defaults_conservative():
    out = _score_safe_content("完全不是 JSON 的散文输出")
    parsed = _json_loads(out)
    assert parsed["score"] == 0.5
    assert "散文输出" in parsed["reasoning"]


def test_score_safe_score_string_guarded():
    out = _score_safe_content('{"score": "pass", "reasoning": "x"}')
    assert _json_loads(out)["score"] == 0.5


def test_score_safe_escalate_maps_low():
    out = _score_safe_content('{"verdict": "escalate", "opinion": "证据不足"}')
    assert _json_loads(out)["score"] == 0.1


def test_step_prompt_is_formattable():
    """STEP_PROMPT 经 openevals prompt.format(**params) 填充 {outputs} 不得 KeyError。

    JSON 契约里的字面花括号必须转义({{score}}),否则 .format() 把 {"score": 解析成
    带引号字段名 → KeyError('"score"')。回归 rev1_0826——2 在线 step 评判全降级事故。
    """
    from audit.evaluators.step import STEP_PROMPT

    rendered = STEP_PROMPT.format(outputs="<trajectory>")
    assert "<trajectory>" in rendered
    assert '{"score": 0..1' in rendered  # 转义花括号渲染回单层,模型仍看到合法 JSON 契约
    assert "pass|retry|escalate" in rendered


async def test_step_judge_full_path_never_keyerrors(monkeypatch):
    """create_async_trajectory_llm_as_judge(STEP_PROMPT) 全链路:模型回合法 JSON 返回 score,
    不抛 KeyError('\"score\"')(prompt.format + create + parse 三段串起来才覆盖根因)。"""
    from agentevals.trajectory.llm import create_async_trajectory_llm_as_judge

    import agent.llm_api as real_llm_api
    from audit.evaluators.step import STEP_PROMPT

    def _fake_chat(messages, model=None, temperature=None, **kwargs):
        return '{"score": 0.9, "reasoning": "ok"}'

    monkeypatch.setattr(real_llm_api, "chat", _fake_chat)
    monkeypatch.setattr(real_llm_api, "role_model", lambda role: "test-model")
    monkeypatch.setattr(real_llm_api, "resolve_key", lambda: "x")
    monkeypatch.setattr(real_llm_api, "pop_token_log", lambda: [])

    judge = LlmApiAgentEvalsClient("evaluator_step")
    ev = create_async_trajectory_llm_as_judge(
        prompt=STEP_PROMPT, judge=judge, model="test-model", continuous=True
    )
    res = await ev(outputs=[{"role": "user", "content": "grade this"}])
    assert res["score"] == 0.9


async def test_create_never_returns_content_without_score(monkeypatch):
    """create() 返回的 content 经矫正后必有 score/reasoning(缺键场景 openevals 不炸)。"""
    import agent.llm_api as real_llm_api

    class _FakeLlmApi:
        def __init__(self, content):
            self._content = content

        def chat(self, messages, model=None, temperature=None, **kwargs):
            return self._content

        def role_model(self, role):
            return "test-model"

    fake = _FakeLlmApi('{"verdict": "escalate"}')
    monkeypatch.setattr(real_llm_api, "chat", fake.chat)
    monkeypatch.setattr(real_llm_api, "role_model", fake.role_model)

    comp = _LlmApiCompletions("evaluator_step")
    resp = await comp.create(
        messages=[{"role": "user", "content": "grade this"}],
        model="test-model",
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "score", "schema": {"type": "object"}},
        },
    )
    parsed = _json_loads(resp.choices[0].message.content)
    assert parsed["score"] == 0.1
    assert "reasoning" in parsed
