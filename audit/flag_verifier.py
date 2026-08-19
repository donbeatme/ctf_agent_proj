"""本项目自行实现的 flag 验证器。

支持明文、SHA-256 和正则三种离线规则。生产环境建议只保存 SHA-256；
验证器只返回布尔值和原因，不把正确 flag 带入日志、反思或 RAG。
"""

import hashlib
import hmac
import json
import re
from pathlib import Path
from typing import Any, Dict, Optional

from .schemas import FlagResult


class FlagVerifier:
    def __init__(self, rules: Dict[str, Dict[str, Any]]):
        self.rules = rules

    @classmethod
    def from_file(cls, path: Path) -> "FlagVerifier":
        with path.open("r", encoding="utf-8") as handle:
            rules = json.load(handle)
        if not isinstance(rules, dict):
            raise ValueError("expected flag file must contain a JSON object")
        return cls(rules)

    @staticmethod
    def _normalize(value: str) -> str:
        # 只去掉复制/终端常见的首尾空白，不改变 flag 内部字符。
        return value.strip()

    def verify(self, task_id: str, submitted_flag: Optional[str]) -> FlagResult:
        rule = self.rules.get(task_id)
        if rule is None:
            return FlagResult(task_id, False, "missing", "该题没有配置验证规则")
        if not submitted_flag:
            return FlagResult(task_id, False, str(rule.get("mode", "unknown")), "agent 未提交 flag")

        mode = str(rule.get("mode", "exact"))
        expected = str(rule.get("value", ""))
        actual = self._normalize(submitted_flag)
        case_sensitive = bool(rule.get("case_sensitive", True))

        if mode == "exact":
            left = actual if case_sensitive else actual.casefold()
            right = expected if case_sensitive else expected.casefold()
            valid = hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
        elif mode == "sha256":
            digest = hashlib.sha256(actual.encode("utf-8")).hexdigest()
            valid = hmac.compare_digest(digest.lower(), expected.lower())
        elif mode == "regex":
            flags = 0 if case_sensitive else re.IGNORECASE
            valid = re.fullmatch(expected, actual, flags=flags) is not None
        else:
            raise ValueError("unsupported flag verification mode: %s" % mode)

        reason = "flag 验证通过" if valid else "flag 不匹配"
        return FlagResult(task_id, valid, mode, reason)


def sha256_flag(flag: str) -> str:
    """为 expected_flags.json 生成不暴露明文的值。"""
    return hashlib.sha256(flag.strip().encode("utf-8")).hexdigest()
