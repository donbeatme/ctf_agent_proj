"""LangSmith SDK 日志适配器，并始终保留一份本地 JSONL 审计日志。"""

import json
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from ..schemas import utc_now


FLAG_PATTERN = re.compile(r"(?i)(?:flag|ctf)\{[^}\r\n]+\}")


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return FLAG_PATTERN.sub("[REDACTED_FLAG]", value)
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items() if key != "submitted_flag"}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class AuditSpan:
    def __init__(self, logger: "AuditLogger", run_id: str, remote_tree: Any = None):
        self.logger = logger
        self.run_id = run_id
        self.remote_tree = remote_tree

    def event(self, name: str, payload: Dict[str, Any]) -> None:
        safe = redact(payload)
        self.logger.write({"time": utc_now(), "run_id": self.run_id, "event": name, "payload": safe})
        if self.remote_tree is not None:
            self.remote_tree.add_event({"name": name, "payload": safe})


class AuditLogger:
    def __init__(self, path: Path, enable_langsmith: bool = False):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.enable_langsmith = enable_langsmith

    def write(self, record: Dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    @contextmanager
    def run(self, name: str, run_id: str, inputs: Dict[str, Any], metadata: Dict[str, Any]) -> Iterator[AuditSpan]:
        safe_inputs, safe_metadata = redact(inputs), redact(metadata)
        remote_tree: Optional[Any] = None
        if self.enable_langsmith:
            try:
                from langsmith.run_trees import RunTree
            except ImportError as exc:
                raise RuntimeError("LangSmith 已启用，但未安装：pip install -e '.[eval]'") from exc
            remote_tree = RunTree(
                name=name,
                run_type="chain",
                inputs=safe_inputs,
                project_name=__import__("os").getenv("LANGSMITH_PROJECT", "ctf-agent-audit"),
                extra={"metadata": safe_metadata},
            )
            remote_tree.post()

        self.write({"time": utc_now(), "run_id": run_id, "event": "run_start", "payload": safe_inputs})
        span = AuditSpan(self, run_id, remote_tree)
        try:
            yield span
        except Exception as exc:
            self.write({"time": utc_now(), "run_id": run_id, "event": "run_error", "payload": {"error": str(exc)}})
            if remote_tree is not None:
                remote_tree.end(error=str(exc))
                remote_tree.patch()
            raise
        else:
            self.write({"time": utc_now(), "run_id": run_id, "event": "run_end", "payload": {"ok": True}})
            if remote_tree is not None:
                remote_tree.end(outputs={"ok": True})
                remote_tree.patch()

