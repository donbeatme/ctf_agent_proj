"""通用 LLM 客户端：统一走 agent/llm_api.py。

替代原来的 DeepSeekChat / DeepSeekAgentEvalsClient，避免把实现绑定到单一厂商。
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, MutableMapping, Optional

from agent import llm_api

from ..settings import Settings


@dataclass
class LlmChatResult:
    content: str
    usage: dict | None


def _sum_usage(usage_log: List[dict]) -> dict | None:
    if not usage_log:
        return None
    total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for item in usage_log:
        total["prompt_tokens"] += int(item.get("prompt_tokens", 0))
        total["completion_tokens"] += int(item.get("completion_tokens", 0))
        total["total_tokens"] += int(item.get("total_tokens", 0))
    return total


class LlmChatClient:
    """面向评估器角色的 OpenAI 兼容客户端，底层走 agent.llm_api。"""

    def __init__(self, settings: Settings, role: str = "evaluator_plan"):
        self.settings = settings
        self.role = role
        self.last_usage: dict | None = None

    @property
    def available(self) -> bool:
        if self.settings.mode == "offline":
            return False
        try:
            llm_api.resolve_key()
            return True
        except Exception:
            return False

    def complete(self, messages: List[Dict[str, str]], temperature: float = 0.2) -> LlmChatResult:
        if not self.available:
            raise RuntimeError("LLM 不可用：请设置 online 模式和 LLM_API_KEY/DEEPSEEK_API_KEY")
        model = llm_api.role_model(self.role)
        content = llm_api.chat(
            messages=messages,
            model=model,
            temperature=temperature,
        )
        usage = _sum_usage(llm_api.pop_token_log())
        self.last_usage = usage
        return LlmChatResult(content=content, usage=usage)

    def complete_text(self, messages: List[Dict[str, str]], temperature: float = 0.2) -> str:
        return self.complete(messages, temperature=temperature).content


_SCORE_KEYS = ("score", "质量分", "quality")
_REASON_KEYS = ("reasoning", "reason", "comment", "opinion", "说明", "依据")
_VERDICT_KEYS = ("verdict", "decision", "结论")
_VERDICT_SCORE = {"pass": 0.9, "retry": 0.5, "escalate": 0.1}


def _first_field(parsed: dict, keys: tuple, default: Any) -> Any:
    for key in keys:
        value = parsed.get(key)
        if value is not None and value != "":
            return value
    return default


def _score_safe_content(content: str) -> str:
    """openevals 解析路径直接 parsed["score"]/parsed["reasoning"],缺键即 KeyError。

    模型未必遵守注入的 JSON schema(可能按 STEP_PROMPT 口径返回 verdict 风格 JSON,
    或带 markdown 围栏)。这里保证返回给它的 JSON 必有 score/reasoning 两字段:
    score 缺失时按 verdict 推断,再兜底 0.5;reasoning 缺失用任意解释字段或原文。
    """
    try:
        parsed = json.loads(content or "")
    except (TypeError, ValueError):
        block = re.search(r"\{.*\}", content or "", re.S)
        try:
            parsed = json.loads(block.group(0)) if block else None
        except (TypeError, ValueError):
            parsed = None
    if not isinstance(parsed, dict):
        return json.dumps({"score": 0.5, "reasoning": (content or "")[:500]}, ensure_ascii=False)
    score = _first_field(parsed, _SCORE_KEYS, None)
    if score is None:
        verdict = str(_first_field(parsed, _VERDICT_KEYS, "")).lower()
        score = _VERDICT_SCORE.get(verdict, 0.5)
    try:
        score = max(0.0, min(1.0, float(score)))
    except (TypeError, ValueError):
        score = 0.5
    reasoning = _first_field(parsed, _REASON_KEYS, None) or (content or "")[:500]
    return json.dumps(
        {"score": round(score, 4), "reasoning": str(reasoning)}, ensure_ascii=False
    )


class LlmApiAgentEvalsClient:
    """满足 AgentEvals ModelClient 协议的客户端，底层走 agent.llm_api。"""

    def __init__(self, role: str):
        self.chat = _LlmApiChat(role)


class _LlmApiChat:
    def __init__(self, role: str):
        self.completions = _LlmApiCompletions(role)


class _LlmApiCompletions:
    def __init__(self, role: str):
        self.role = role

    def create(self, **params: Any) -> Any:
        request: MutableMapping[str, Any] = dict(params)
        response_format = request.pop("response_format", None)
        schema = self._response_schema(response_format) if response_format else None
        messages = deepcopy(list(request.get("messages", [])))
        if schema is not None:
            messages = self._with_json_instruction(messages, schema)
            request["response_format"] = {"type": "json_object"}
            request.setdefault("temperature", 0)

        model = llm_api.role_model(self.role)
        content = llm_api.chat(
            messages=messages,
            model=model,
            temperature=float(request.get("temperature", 0)),
        )
        # token 日志保留在 llm_api 全局日志中，由外层 StepAcceptanceEvaluator 统一 pop。
        # 仅 schema 驱动的 agentevals 路径做矫正;普通 LlmChatClient 调用不受影响。
        if schema is not None:
            content = _score_safe_content(content)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0),
        )

    @staticmethod
    def _response_schema(response_format: Mapping[str, Any]) -> Mapping[str, Any]:
        schema_wrapper = response_format.get("json_schema", response_format)
        if not isinstance(schema_wrapper, Mapping):
            return {}
        schema = schema_wrapper.get("schema", schema_wrapper)
        return schema if isinstance(schema, Mapping) else {}

    @staticmethod
    def _with_json_instruction(messages: Any, schema: Mapping[str, Any]) -> Any:
        copied = deepcopy(list(messages))
        instruction = (
            "You must return JSON.\n"
            "Return ONLY exactly one valid JSON object.\n"
            "Do not use Markdown code fences.\n"
            "Do not output explanations before or after the JSON.\n"
            "Do not rename, omit, or add required fields.\n"
            "Follow this JSON schema exactly:\n"
            + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        )
        copied.insert(0, {"role": "system", "content": instruction})
        return copied
