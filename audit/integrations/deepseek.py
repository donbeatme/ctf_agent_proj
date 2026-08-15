"""DeepSeek 的 OpenAI-compatible Chat Completions 适配器。

AgentEvals 当前会通过 OpenEvals 强制发送 ``response_format=json_schema``。
部分 DeepSeek 模型/端点不支持该格式，所以这里提供一个很薄的兼容客户端：
删除不兼容参数，并把同一份 JSON Schema 作为系统提示传给模型。这样仍由
AgentEvals 组织轨迹、评分和 LangSmith trace，而不是绕过 AgentEvals。
"""

import json
from copy import deepcopy
from typing import Any, Dict, List, Mapping, MutableMapping

from ..settings import Settings


class DeepSeekChat:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def available(self) -> bool:
        return bool(self.settings.deepseek_api_key) and self.settings.mode != "offline"

    def complete(self, messages: List[Dict[str, str]], temperature: float = 0.2) -> str:
        if not self.available:
            raise RuntimeError("DeepSeek 不可用：请设置 online 模式和 DEEPSEEK_API_KEY")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("OpenAI-compatible SDK 未安装：pip install -e '.[eval]'") from exc
        client = OpenAI(
            api_key=self.settings.deepseek_api_key,
            base_url=self.settings.deepseek_base_url,
        )
        response = client.chat.completions.create(
            model=self.settings.deepseek_model,
            messages=messages,
            temperature=temperature,
        )
        return response.choices[0].message.content or ""

    def agentevals_client(self) -> "DeepSeekAgentEvalsClient":
        """创建供 AgentEvals 使用的 DeepSeek 兼容客户端。"""
        if not self.available:
            raise RuntimeError("DeepSeek 不可用：请设置 online 模式和 DEEPSEEK_API_KEY")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("OpenAI-compatible SDK 未安装：pip install -e '.[eval]'") from exc
        client = OpenAI(
            api_key=self.settings.deepseek_api_key,
            base_url=self.settings.deepseek_base_url,
        )
        return DeepSeekAgentEvalsClient(client)


class _CompatibleCompletions:
    """代理 ``chat.completions``，只修正 DeepSeek 不兼容的结构化输出参数。"""

    def __init__(self, completions: Any):
        self._completions = completions

    def create(self, **params: Any) -> Any:
        request: MutableMapping[str, Any] = dict(params)
        response_format = request.pop("response_format", None)
        schema = None

        if response_format:
            schema = self._response_schema(response_format)
            # AgentEvals/OpenEvals 传入 json_schema；DeepSeek 使用 json_object。
            request["messages"] = self._with_json_instruction(
                request.get("messages", []),
                schema,
            )
            request["response_format"] = {"type": "json_object"}
            request.setdefault("temperature", 0)

        response = self._completions.create(**request)
        if schema is None or self._matches_schema(response, schema):
            return response

        repair_request = dict(request)
        repair_request["messages"] = self._repair_messages(
            request.get("messages", []),
            response,
            schema,
        )
        repaired = self._completions.create(**repair_request)
        if not self._matches_schema(repaired, schema):
            raise ValueError("DeepSeekStructuredOutputInvalid")
        return repaired

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

    @staticmethod
    def _matches_schema(response: Any, schema: Mapping[str, Any]) -> bool:
        try:
            content = response.choices[0].message.content or ""
            parsed = json.loads(content)
        except (AttributeError, IndexError, TypeError, json.JSONDecodeError):
            return False
        if not isinstance(parsed, dict):
            return False
        required = schema.get("required", [])
        return not isinstance(required, list) or all(key in parsed for key in required)

    @staticmethod
    def _repair_messages(
        messages: Any,
        response: Any,
        schema: Mapping[str, Any],
    ) -> Any:
        copied = deepcopy(list(messages))
        try:
            invalid_content = str(response.choices[0].message.content or "")[:4000]
        except (AttributeError, IndexError, TypeError):
            invalid_content = ""
        if invalid_content:
            copied.append({"role": "assistant", "content": invalid_content})
        required = schema.get("required", [])
        copied.append({
            "role": "user",
            "content": (
                "The previous JSON did not match the required schema. "
                "Return ONLY one corrected JSON object containing every required field: "
                + json.dumps(required, ensure_ascii=False, separators=(",", ":"))
            ),
        })
        return copied


class _CompatibleChat:
    def __init__(self, chat: Any):
        self.completions = _CompatibleCompletions(chat.completions)


class DeepSeekAgentEvalsClient:
    """满足 OpenEvals ``ModelClient`` 协议的 OpenAI 客户端代理。"""

    def __init__(self, client: Any):
        self.chat = _CompatibleChat(client.chat)
