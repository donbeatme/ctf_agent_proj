"""任务理解层（Task Understanding）。

将本地 CTF challenge 输入归一化为 ctf_agent_proj 的 TaskInput。
"""

from task_understanding.artifact_adapter import (
    artifact_to_json_safe,
    artifacts_to_json_safe,
    contains_binary,
)
from task_understanding.classify import classify_challenge, parse_challenge
from task_understanding.goals import default_goals
from task_understanding.image_understanding import (
    ImageUnderstander,
    OllamaImageUnderstander,
)
from task_understanding.normalize import normalize_sources
from task_understanding.real_understander import RealTaskUnderstander

__all__ = [
    "RealTaskUnderstander",
    "ImageUnderstander",
    "OllamaImageUnderstander",
    "classify_challenge",
    "parse_challenge",
    "default_goals",
    "normalize_sources",
    "artifact_to_json_safe",
    "artifacts_to_json_safe",
    "contains_binary",
]
