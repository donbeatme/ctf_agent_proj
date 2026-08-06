"""LLM 网关(agent/llm_api.py)测试:文档注入、工具循环、重试、降级。

用 monkeypatch 替换 `_request`,不触网;api_key 显式传 "test" 跳过 resolve_key。
"""

import json
from types import SimpleNamespace

import pytest
import requests

from agent import llm_api


# ===== 假响应构造 =====

def _msg(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def _resp(content=None, tool_calls=None):
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    return SimpleNamespace(choices=[SimpleNamespace(message=_msg(content, tool_calls))],
                           usage=usage)


def _tc(name, args="{}", id="call_1"):
    return SimpleNamespace(id=id, function=SimpleNamespace(name=name, arguments=args))


def _capture_request(captured):
    """返回把 messages 存进 captured 并回固定响应的假 _request。"""
    def fake(client, messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return _resp(content="ok")
    return fake


def _fake_client(create):
    """包装 create 为 client.chat.completions.create 的假客户端。"""
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


# ===== 文档注入 =====

def test_chat_injects_docs_into_system(monkeypatch):
    captured = {}
    monkeypatch.setattr(llm_api, "_request", _capture_request(captured))
    llm_api.chat("问题", docs=["文档1", "文档2"], api_key="test")
    msgs = captured["messages"]
    assert msgs[0]["role"] == "system"
    assert "# 可用文档" in msgs[0]["content"]
    assert "文档1" in msgs[0]["content"] and "文档2" in msgs[0]["content"]
    assert msgs[1] == {"role": "user", "content": "问题"}


def test_chat_appends_docs_to_existing_system(monkeypatch):
    captured = {}
    monkeypatch.setattr(llm_api, "_request", _capture_request(captured))
    llm_api.chat("问题", system="你是助手", docs=["文档"], api_key="test")
    assert captured["messages"][0]["role"] == "system"
    assert captured["messages"][0]["content"].startswith("你是助手")
    assert "# 可用文档" in captured["messages"][0]["content"]


def test_chat_docs_truncated_to_budget(monkeypatch):
    captured = {}
    monkeypatch.setattr(llm_api, "_request", _capture_request(captured))
    big = "x" * 500
    llm_api.chat("问题", docs=[big, big], max_docs_chars=100, api_key="test")
    sys = captured["messages"][0]["content"]
    assert len(sys) <= 100
    # 按文档边界截断:不残留半个文档,大文档整体被丢弃
    assert "x" not in sys


def test_chat_docs_empty_list_noop(monkeypatch):
    captured = {}
    monkeypatch.setattr(llm_api, "_request", _capture_request(captured))
    llm_api.chat("问题", docs=[], api_key="test")
    assert captured["messages"] == [{"role": "user", "content": "问题"}]


def test_chat_with_docs_passes_messages_as_is(monkeypatch):
    captured = {}
    monkeypatch.setattr(llm_api, "_request", _capture_request(captured))
    hist = [{"role": "user", "content": "你好"}]
    llm_api.chat(messages=hist, docs=["文档"], api_key="test")
    msgs = captured["messages"]
    assert msgs[0]["role"] == "system"          # 无 system 时前置文档段
    assert msgs[1] == {"role": "user", "content": "你好"}


# ===== chat 基本行为 =====

def test_chat_returns_content(monkeypatch):
    def fake(client, messages, **kwargs):
        return _resp(content="你好")
    monkeypatch.setattr(llm_api, "_request", fake)
    assert llm_api.chat("hi", api_key="test") == "你好"


def test_chat_request_kwargs(monkeypatch):
    captured = {}
    monkeypatch.setattr(llm_api, "_request", _capture_request(captured))
    llm_api.chat("hi", system="s", model="m", temperature=0.2, max_tokens=10, api_key="test")
    kw = captured["kwargs"]
    assert kw["model"] == "m"
    assert kw["temperature"] == 0.2
    assert kw["max_tokens"] == 10
    assert "tools" not in kw  # 纯文本不走工具


# ===== 工具循环 =====

def test_chat_with_tools_runs_one_round(monkeypatch):
    seen = []

    def fake(client, messages, **kwargs):
        seen.append(messages)
        if len(seen) == 1:
            return _resp(tool_calls=[_tc("ls", '{"a": 1}')])
        return _resp(content="最终答案")
    monkeypatch.setattr(llm_api, "_request", fake)

    executed = []
    def tool_exec(name, args):
        executed.append((name, args))
        return {"ok": True}
    result = llm_api.chat_with_tools("任务", tools=[{}], tool_exec=tool_exec,
                                     api_key="test", max_tool_rounds=8)

    assert executed == [("ls", {"a": 1})]
    assert result.content == "最终答案"
    assert result.rounds == 2
    assert len(result.trace) == 1
    assert result.trace[0]["name"] == "ls"
    assert result.trace[0]["arguments"] == '{"a": 1}'
    assert result.trace[0]["result"] == {"ok": True}
    # tool 结果消息已喂回
    assert seen[-1][-1]["role"] == "tool"
    assert json.loads(seen[-1][-1]["content"]) == {"ok": True}


def test_chat_with_tools_multi_round(monkeypatch):
    seen = []

    def fake(client, messages, **kwargs):
        seen.append(messages)
        n = len(seen)
        if n == 1:
            return _resp(tool_calls=[_tc("t1", id="c1")])
        if n == 2:
            return _resp(tool_calls=[_tc("t2", id="c2")])
        return _resp(content="done")
    monkeypatch.setattr(llm_api, "_request", fake)

    result = llm_api.chat_with_tools("任务", tools=[{}], tool_exec=lambda n, a: {"r": n},
                                     api_key="test")
    assert [t["name"] for t in result.trace] == ["t1", "t2"]
    assert result.rounds == 3
    assert len(seen) == 3


def test_chat_with_tools_exceeds_rounds_raises(monkeypatch):
    def fake(client, messages, **kwargs):
        return _resp(tool_calls=[_tc("t1")])
    monkeypatch.setattr(llm_api, "_request", fake)

    with pytest.raises(llm_api.ToolLoopError):
        llm_api.chat_with_tools("任务", tools=[{}], tool_exec=lambda n, a: {},
                                api_key="test", max_tool_rounds=3)


def test_tool_exec_exception_recorded_not_fatal(monkeypatch):
    seen = []

    def fake(client, messages, **kwargs):
        seen.append(messages)
        if len(seen) == 1:
            return _resp(tool_calls=[_tc("boom")])
        return _resp(content="继续")
    monkeypatch.setattr(llm_api, "_request", fake)

    def tool_exec(name, args):
        raise ValueError("炸了")
    result = llm_api.chat_with_tools("任务", tools=[{}], tool_exec=tool_exec, api_key="test")
    assert result.content == "继续"
    assert result.trace[0]["result"] == {"error": "ValueError: 炸了"}


def test_tool_bad_json_arguments(monkeypatch):
    seen = []

    def fake(client, messages, **kwargs):
        seen.append(messages)
        if len(seen) == 1:
            return _resp(tool_calls=[_tc("t1", "not-json")])
        return _resp(content="done")
    monkeypatch.setattr(llm_api, "_request", fake)

    executed = []
    def tool_exec(name, args):
        executed.append(args)
        return {"ok": True}
    result = llm_api.chat_with_tools("任务", tools=[{}], tool_exec=tool_exec, api_key="test")
    assert executed == [{}]  # 坏 JSON 降级为空参数,不抛错
    assert result.content == "done"


def test_chat_with_tools_injects_docs(monkeypatch):
    seen = []

    def fake(client, messages, **kwargs):
        seen.append(messages)
        if len(seen) == 1:
            return _resp(tool_calls=[_tc("t1")])
        return _resp(content="done")
    monkeypatch.setattr(llm_api, "_request", fake)

    llm_api.chat_with_tools("任务", docs=["技能甲文档"], tools=[{}],
                            tool_exec=lambda n, a: {}, api_key="test")
    assert "# 可用文档" in seen[0][0]["content"]
    assert "技能甲文档" in seen[0][0]["content"]


# ===== 降级:无工具 =====

def test_chat_with_tools_no_tools_falls_back(monkeypatch):
    captured = {}
    monkeypatch.setattr(llm_api, "_request", _capture_request(captured))
    result = llm_api.chat_with_tools("问题", api_key="test")
    assert isinstance(result, llm_api.ToolResult)
    assert result.content == "ok"
    assert result.rounds == 0
    assert result.trace == []
    assert captured["messages"][-1] == {"role": "user", "content": "问题"}


# ===== 重试(_request) =====

def test_request_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def create(**kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("抖动")
        return _resp(content="ok")
    monkeypatch.setattr(llm_api, "_should_retry", lambda e: True)
    resp = llm_api._request(_fake_client(create), [{"role": "user", "content": "x"}],
                            model="m", temperature=0.7, max_retries=3, retry_backoff=0)
    assert calls["n"] == 3
    assert resp.choices[0].message.content == "ok"


def test_request_raises_when_retries_exhausted(monkeypatch):
    calls = {"n": 0}

    def create(**kwargs):
        calls["n"] += 1
        raise RuntimeError("一直失败")
    monkeypatch.setattr(llm_api, "_should_retry", lambda e: True)
    with pytest.raises(llm_api.LLMError):
        llm_api._request(_fake_client(create), [{"role": "user", "content": "x"}],
                         model="m", temperature=0.7, max_retries=3, retry_backoff=0)
    assert calls["n"] == 3


def test_request_non_retryable_single_attempt():
    calls = {"n": 0}

    def create(**kwargs):
        calls["n"] += 1
        raise ValueError("配置错")
    with pytest.raises(llm_api.LLMError):
        llm_api._request(_fake_client(create), [{"role": "user", "content": "x"}],
                         model="m", temperature=0.7, max_retries=3)
    assert calls["n"] == 1  # 不可重试错误不重试


def test_should_retry_classifies_errors():
    assert llm_api._should_retry(RuntimeError("x")) is False
    assert llm_api._should_retry(requests.exceptions.Timeout("x")) is True
