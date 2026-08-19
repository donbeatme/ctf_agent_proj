"""Optional local image understanding for CTF task inputs."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from openai import OpenAI


IMAGE_UNDERSTANDING_SYSTEM = (
    "You are a CTF task input parser. Only describe objective visual facts in "
    "the image: visible text, UI/screenshot content, diagrams, or other details "
    "that help understand the task. Do not solve the CTF, infer vulnerabilities, "
    "plan attacks, suggest tools, or output commands. Return strict JSON only."
)

IMAGE_UNDERSTANDING_PROMPT = (
    'Return exactly a JSON object with keys "summary" and "visible_text".'
)


class ImageUnderstander:
    """Small interface for optional image semantic parsing."""

    def analyze(self, path: Path) -> dict[str, Any]:
        raise NotImplementedError


class OllamaImageUnderstander(ImageUnderstander):
    """Use a local Ollama OpenAI-compatible vision model for image summaries."""

    def __init__(
        self,
        *,
        model: str = "qwen3-vl:32b",
        base_url: str = "http://127.0.0.1:11434/v1/",
        api_key: str = "ollama",
        max_tokens: int = 512,
    ):
        self.model = model
        self.max_tokens = max_tokens
        self._client = OpenAI(base_url=base_url, api_key=api_key, max_retries=0)

    def analyze(self, path: Path) -> dict[str, Any]:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": IMAGE_UNDERSTANDING_SYSTEM},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": IMAGE_UNDERSTANDING_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{encoded}",
                            },
                        },
                    ],
                },
            ],
            temperature=0,
            max_tokens=self.max_tokens,
        )
        content = response.choices[0].message.content or "{}"
        data = self._parse_json(content)
        return {
            "status": "ok",
            "summary": str(data.get("summary", "")),
            "visible_text": str(data.get("visible_text", "")),
            "model": self.model,
        }

    def _parse_json(self, content: str) -> dict[str, Any]:
        text = content.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines:
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end >= start:
            text = text[start : end + 1]
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("image understanding response must be a JSON object")
        return data
