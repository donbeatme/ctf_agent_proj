"""Reusable, flag-safe CTF experience records and the local fallback store."""

import json
import re
from pathlib import Path
from typing import Any, Dict, List

from ..schemas import AuditRecord, utc_now
from .langsmith_logger import redact


def build_experience(record: AuditRecord) -> Dict[str, Any]:
    """Build the only payload allowed to leave the audit pipeline."""
    reflection = record.task_evaluation.reflection
    risky_steps = [
        {
            "step_id": item.step_id,
            "tool": item.tool,
            "decision": item.decision,
            "reasoning": item.reasoning,
        }
        for item in record.step_evaluation.items
        if item.decision != "pass"
    ]
    if reflection is None:
        diagnosis = "任务和独立 Flag 验证均通过，未发现必须反思的问题。"
        lessons = ["复用通过计划评审、逐步验收和独立验证的执行结构。"]
        next_plan: List[str] = []
        reflection_source = "none"
    else:
        diagnosis = reflection.diagnosis
        lessons = reflection.lessons
        next_plan = reflection.next_plan
        reflection_source = reflection.source

    return redact({
        "schema_version": "1.0",
        "experience_type": "ctf_task_experience",
        "created_at": utc_now(),
        "attempt_id": record.attempt.attempt_id,
        "task_id": record.attempt.task_id,
        "agent_id": record.attempt.agent_id,
        "category": record.attempt.category,
        "outcome": record.task_evaluation.decision,
        "flag_valid": record.flag.valid,
        "plan": {
            "decision": record.plan_evaluation.decision,
            "score": record.plan_evaluation.score,
            "issues": record.plan_evaluation.issues,
            "suggestions": record.plan_evaluation.suggestions,
        },
        "execution": {
            "score": record.step_evaluation.score,
            "pass_count": record.step_evaluation.pass_count,
            "retry_count": record.step_evaluation.retry_count,
            "escalate_count": record.step_evaluation.escalate_count,
            "tool_success_rate": record.metrics.tool_success_rate,
            "total_steps": record.metrics.total_steps,
            "risky_steps": risky_steps,
        },
        "reflexion": {
            "diagnosis": diagnosis,
            "lessons": lessons,
            "next_plan": next_plan,
            "source": reflection_source,
        },
    })


def experience_markdown(experience: Dict[str, Any]) -> str:
    """Serialize one experience as searchable Markdown with canonical JSON."""
    return "# CTF Agent Experience\n\n```json\n%s\n```\n" % json.dumps(
        redact(experience), ensure_ascii=False, indent=2, sort_keys=True
    )


def _terms(text: str) -> set:
    lowered = text.lower()
    terms = set(re.findall(r"[a-z0-9_]+", lowered))
    for sequence in re.findall(r"[\u4e00-\u9fff]+", lowered):
        if len(sequence) == 1:
            terms.add(sequence)
        else:
            terms.update(
                sequence[index:index + 2]
                for index in range(len(sequence) - 1)
            )
    return terms


class LocalExperienceStore:
    """JSONL fallback with the same lifecycle as the RAGFlow store."""

    source = "local-jsonl"

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def store_experience(self, record: AuditRecord) -> Dict[str, Any]:
        experience = build_experience(record)
        rows = self._read_rows()
        if any(
            row.get("experience", {}).get("attempt_id")
            == record.attempt.attempt_id
            for row in rows
        ):
            return {"status": "skipped", "count": 0, "error": None}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"experience": experience}, ensure_ascii=False) + "\n")
        return {"status": "stored", "count": 1, "error": None}

    def retrieve_experience(
        self,
        query: str,
        limit: int = 5,
        agent_id: str = "",
    ) -> List[Dict[str, Any]]:
        query_terms = _terms(query)
        matches = []
        for row in self._read_rows():
            experience = row.get("experience", {})
            if agent_id and experience.get("agent_id") != agent_id:
                continue
            content = json.dumps(experience, ensure_ascii=False)
            score = len(query_terms & _terms(content))
            if score:
                matches.append((score, experience))
        matches.sort(key=lambda item: item[0], reverse=True)
        return [
            {
                "id": str(experience.get("attempt_id", "")),
                "memory": experience_markdown(experience),
                "score": score,
                "metadata": {
                    "task_id": experience.get("task_id"),
                    "category": experience.get("category"),
                    "agent_id": experience.get("agent_id"),
                },
            }
            for score, experience in matches[:max(1, limit)]
        ]

    def _read_rows(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
        return rows
