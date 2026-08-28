"""Reusable, flag-safe CTF experience records and the local fallback store."""

import json
import re
from pathlib import Path
from typing import Any, Dict, List

from ..schemas import AuditRecord, utc_now
from .langsmith_logger import redact


# 无信息量的"无问题"默认诊断:判断反思是否有实质内容用。
_NO_ISSUE_MARKERS = ("未发现必须反思", "验证均通过", "无问题")


def should_store_experience(record: AuditRecord) -> bool:
    """经验入库质量门：有确定性信号才回流，避免失败 run 的 LLM 幻觉教训污染 ctx。

    - flag.valid 非 None（平台/本地已确认对错）→ 有确定性结论，存。
    - 反思有实质内容（LLM 真产出诊断/教训，非"无问题"默认）→ 存。
    - 否则（flag.valid None 且无实质反思 = no-signal）→ 跳过。
    """
    if record.flag.valid is not None:
        return True
    reflection = record.task_evaluation.reflection
    if reflection is None:
        return False
    diagnosis = (reflection.diagnosis or "").strip()
    if not diagnosis:
        return False
    return not any(marker in diagnosis for marker in _NO_ISSUE_MARKERS)


def _build_search_text(record, diagnosis, lessons, next_plan, risky_steps) -> str:
    """紧凑可检索文本：只含类别/任务/结论/诊断/教训/补丁方向/风险步，不含分数与计数。"""
    parts = [
        "category=%s" % record.attempt.category,
        "task=%s" % record.attempt.task_id,
        "outcome=%s" % record.task_evaluation.decision,
    ]
    if record.flag.valid is True:
        parts.append("flag 确认正确")
    elif record.flag.valid is False:
        parts.append("flag 确认错误")
    if diagnosis:
        parts.append("diagnosis=%s" % diagnosis)
    for item in lessons:
        parts.append("lesson=%s" % item)
    for item in next_plan:
        parts.append("next=%s" % item)
    for item in risky_steps:
        parts.append("risky tool=%s decision=%s" % (item["tool"], item["decision"]))
    return " | ".join(parts)


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

    search_text = _build_search_text(record, diagnosis, lessons, next_plan, risky_steps)

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
        "search_text": search_text,
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
    """Serialize one experience as compact searchable Markdown:只含 search_text。

    这是注入 planner ctx 的 memory 体与 RAGFlow 上传文档体——不含分数/计数/步详情,
    避免整段 JSON 污染 ctx 与词重叠检索刷分。
    """
    text = str(experience.get("search_text") or "").strip()
    if not text:
        # 旧记录/缺 search_text:退化到原内容首段,避免空文档。
        text = str(experience.get("content") or "")[:2000]
    return "# CTF Agent Experience\n\n%s\n" % redact(text)


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
        if not should_store_experience(record):
            return {"status": "skipped", "count": 0, "reason": "no-signal", "error": None}
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
            # 只对 search_text(诊断/教训/类别/任务)打分,不再匹配整段 JSON 的字段名与分数。
            content = str(experience.get("search_text") or "")
            if not content:
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
