"""多源 CTF 题目摄入 + 题型判定。

对接契约:
- 任务理解层 `TaskUnderstander.understand(raw) → TaskInput`（design/contracts.md §0）
- 题型路由复用 `agent.skills.CATEGORY_KEYWORDS`（与 CtfSkillsDocStore.search 同源）

流程: 多源输入(文本/JSON/URL/附件) → 归一 raw dict → classify → raw 写入
challenge_type / type_scores → Understander 产出 goal_list → Engine.run。
"""

from __future__ import annotations

import json
import mimetypes
import re
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from agent.schema import Goal, TaskInput
from agent.skills import CATEGORY_KEYWORDS, _task_text
from agent.understander import TaskUnderstander

# 文件扩展名 → 题型启发(与 CATEGORY_KEYWORDS 关键词叠加计分)
_EXT_HINTS: dict[str, list[tuple[str, int]]] = {
    ".pcap": [("ctf-forensics", 5)],
    ".pcapng": [("ctf-forensics", 5)],
    ".cap": [("ctf-forensics", 4)],
    ".bin": [("ctf-reverse", 2), ("ctf-pwn", 2)],
    ".elf": [("ctf-pwn", 4), ("ctf-reverse", 3)],
    ".exe": [("ctf-reverse", 3), ("ctf-malware", 2)],
    ".dll": [("ctf-reverse", 3), ("ctf-malware", 2)],
    ".so": [("ctf-pwn", 3), ("ctf-reverse", 2)],
    ".apk": [("ctf-reverse", 3), ("ctf-mobile", 1)],
    ".py": [("ctf-crypto", 1), ("ctf-misc", 1), ("ctf-pwn", 1)],
    ".sage": [("ctf-crypto", 4)],
    ".c": [("ctf-pwn", 2), ("ctf-reverse", 1)],
    ".cpp": [("ctf-pwn", 2), ("ctf-reverse", 1)],
    ".rs": [("ctf-pwn", 2), ("ctf-reverse", 1)],
    ".php": [("ctf-web", 4)],
    ".jsp": [("ctf-web", 3)],
    ".html": [("ctf-web", 2)],
    ".js": [("ctf-web", 2)],
    ".wasm": [("ctf-web", 2), ("ctf-reverse", 2)],
    ".jpg": [("ctf-forensics", 3)],
    ".jpeg": [("ctf-forensics", 3)],
    ".png": [("ctf-forensics", 3)],
    ".gif": [("ctf-forensics", 2)],
    ".bmp": [("ctf-forensics", 2)],
    ".wav": [("ctf-forensics", 3)],
    ".mp3": [("ctf-forensics", 2)],
    ".zip": [("ctf-misc", 1), ("ctf-forensics", 1)],
    ".7z": [("ctf-misc", 1)],
    ".gz": [("ctf-misc", 1)],
    ".tar": [("ctf-misc", 1)],
    ".rar": [("ctf-misc", 1)],
    ".emem": [("ctf-forensics", 4)],
    ".raw": [("ctf-forensics", 3)],
    ".vmem": [("ctf-forensics", 4)],
    ".dmp": [("ctf-forensics", 3), ("ctf-malware", 1)],
}

_CATEGORY_LABELS = {
    "ctf-web": "Web",
    "ctf-pwn": "Pwn",
    "ctf-crypto": "Crypto",
    "ctf-reverse": "Reverse",
    "ctf-forensics": "Forensics",
    "ctf-osint": "OSINT",
    "ctf-malware": "Malware",
    "ctf-misc": "Misc",
    "ctf-ai-ml": "AI/ML",
    "ctf-writeup": "Writeup",
    "solve-challenge": "General",
}


@dataclass
class TypeHit:
    category: str
    score: int
    label: str = ""
    evidence: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.label:
            self.label = _CATEGORY_LABELS.get(self.category, self.category)


@dataclass
class ClassifyResult:
    primary: str | None
    ranked: list[TypeHit]
    text_used: str
    confidence: float  # 0~1, 相对分差


def classify_challenge(task: dict | str, *, attachments: list[dict] | None = None,
                       top_n: int = 5) -> ClassifyResult:
    """题型判定:关键词(CATEGORY_KEYWORDS)+附件扩展名启发。与 DocStore.search 同源。"""
    text = _task_text(task).lower()
    scores: dict[str, int] = {}
    evidence: dict[str, list[str]] = {}

    def _add(cat: str, pts: int, why: str):
        if cat not in CATEGORY_KEYWORDS and cat not in _CATEGORY_LABELS:
            return
        scores[cat] = scores.get(cat, 0) + pts
        evidence.setdefault(cat, []).append(why)

    for cat, kws in CATEGORY_KEYWORDS.items():
        for kw in kws:
            if kw and kw in text:
                _add(cat, 1, f"keyword:{kw}")

    # 题面里显式 category / type 字段加权
    if isinstance(task, dict):
        for key in ("category", "challenge_type", "type", "tags"):
            val = task.get(key)
            vals = val if isinstance(val, list) else [val] if val else []
            for v in vals:
                s = str(v).strip().lower()
                if not s:
                    continue
                if s in CATEGORY_KEYWORDS:
                    _add(s, 8, f"field:{key}={s}")
                else:
                    for cat, label in _CATEGORY_LABELS.items():
                        if s == label.lower() or s == cat.replace("ctf-", ""):
                            _add(cat, 8, f"field:{key}={s}")

    for att in attachments or []:
        name = (att.get("name") or att.get("filename") or "").lower()
        path = att.get("path") or ""
        ext = Path(name or path).suffix.lower()
        for cat, pts in _EXT_HINTS.get(ext, []):
            _add(cat, pts, f"ext:{ext}")
        # zip 内文件名再扫一层(只读目录,不落盘解压正文)
        if ext == ".zip" and path and Path(path).is_file():
            try:
                with zipfile.ZipFile(path) as zf:
                    for info in zf.infolist()[:40]:
                        inner = info.filename.lower()
                        iext = Path(inner).suffix
                        for cat, pts in _EXT_HINTS.get(iext, []):
                            _add(cat, max(1, pts - 1), f"zip:{inner}")
                        for cat, kws in CATEGORY_KEYWORDS.items():
                            if any(kw in inner for kw in kws if len(kw) > 3):
                                _add(cat, 1, f"zip-name:{Path(inner).name}")
            except (OSError, zipfile.BadZipFile):
                pass

    ranked = [
        TypeHit(category=c, score=sc, evidence=evidence.get(c, [])[:8])
        for c, sc in scores.items() if sc > 0
    ]
    ranked.sort(key=lambda h: (-h.score, h.category))
    ranked = ranked[:top_n]
    primary = ranked[0].category if ranked else None
    if not ranked:
        # 无信号时回落 misc(开放式输入兜底)
        primary = "ctf-misc"
        ranked = [TypeHit(category="ctf-misc", score=0, evidence=["fallback:no-signal"])]
        confidence = 0.0
    elif len(ranked) == 1:
        confidence = min(1.0, 0.4 + ranked[0].score / 20)
    else:
        gap = ranked[0].score - ranked[1].score
        confidence = min(1.0, 0.35 + gap / 10 + ranked[0].score / 30)
    return ClassifyResult(primary=primary, ranked=ranked, text_used=text[:500],
                          confidence=round(confidence, 3))


def _default_goals(primary: str | None, raw: dict) -> list[Goal]:
    """按题型生成固定 goal_list(仅 id)。用户 raw.goals 优先保留并前置。"""
    goals: list[Goal] = []
    seen = set()
    for g in raw.get("goals") or []:
        gid = g["id"] if isinstance(g, dict) else str(g)
        gid = gid.strip()
        if gid and gid not in seen:
            goals.append(Goal(id=gid))
            seen.add(gid)
    for gid in ("find_flag", f"solve_{ (primary or 'misc').replace('ctf-', '') }"):
        if gid not in seen:
            goals.append(Goal(id=gid))
            seen.add(gid)
    if raw.get("attachments") or raw.get("files"):
        if "analyze_attachments" not in seen:
            goals.append(Goal(id="analyze_attachments"))
    if raw.get("target_url"):
        if "reach_target" not in seen:
            goals.insert(1, Goal(id="reach_target"))
    return goals


def normalize_sources(
    *,
    title: str = "",
    description: str = "",
    challenge_id: str = "",
    task_id: str = "",
    goals: list | None = None,
    target_url: str = "",
    json_blob: str | dict | None = None,
    attachments: list[dict] | None = None,
    category_override: str | None = None,
) -> dict:
    """多源字段归一成 engine 可消费的 raw dict(尚未剥 goals)。"""
    raw: dict = {
        "task_id": task_id or "intake",
        "ground_id": "g-intake",
        "challenge_id": challenge_id or "c-intake",
        "title": (title or "").strip() or "未命名题目",
        "description": (description or "").strip(),
    }
    if target_url:
        raw["target_url"] = target_url.strip()
        if not raw["description"]:
            raw["description"] = f"目标地址: {target_url.strip()}"
    if goals:
        if isinstance(goals, str):
            goals = [{"id": g.strip()} for g in goals.split(",") if g.strip()]
        raw["goals"] = goals
    if attachments:
        raw["attachments"] = [
            {
                "name": a.get("name") or a.get("filename") or Path(a.get("path", "")).name,
                "path": a.get("path", ""),
                "size": a.get("size"),
                "mime": a.get("mime") or mimetypes.guess_type(a.get("name") or "")[0],
            }
            for a in attachments
        ]
        names = ", ".join(x["name"] for x in raw["attachments"] if x.get("name"))
        if names:
            raw["description"] = (raw["description"] + f"\n附件: {names}").strip()

    if json_blob:
        data = json.loads(json_blob) if isinstance(json_blob, str) else dict(json_blob)
        # CTFd / 通用导出: name/title, description/value, category, files
        if "name" in data and not title:
            raw["title"] = str(data["name"])
        if "title" in data and data["title"]:
            raw["title"] = str(data["title"])
        for k in ("description", "value", "content", "prompt"):
            if data.get(k) and not description:
                raw["description"] = str(data[k])
                break
        if data.get("description"):
            raw["description"] = str(data["description"])
        if data.get("category"):
            raw["category"] = data["category"]
        if data.get("id") is not None:
            raw["challenge_id"] = str(data["id"])
        if data.get("files"):
            extra = []
            for f in data["files"]:
                if isinstance(f, str):
                    extra.append({"name": Path(f).name, "path": f})
                elif isinstance(f, dict):
                    extra.append(f)
            if extra:
                raw.setdefault("attachments", []).extend(extra)
        # 其余字符串字段并入 description 供关键词检索
        extras = []
        for k, v in data.items():
            if k in ("name", "title", "description", "value", "content", "prompt",
                     "category", "id", "files", "goals") or not isinstance(v, str):
                continue
            if v.strip():
                extras.append(f"{k}: {v.strip()}")
        if extras:
            raw["description"] = (raw.get("description") or "") + "\n" + "\n".join(extras)
        raw["source_json"] = True

    if category_override:
        raw["challenge_type"] = category_override
        raw["category"] = category_override
    return raw


def parse_challenge(raw: dict, *, category_override: str | None = None) -> dict:
    """摄入 → 题型判定 → 回填 raw(含 challenge_type / type_scores)。不剥 goals。"""
    raw = dict(raw)
    if category_override:
        raw["challenge_type"] = category_override
        raw["category"] = category_override
    result = classify_challenge(raw, attachments=raw.get("attachments") or [])
    if category_override and category_override in CATEGORY_KEYWORDS:
        primary = category_override
        ranked = [TypeHit(category=primary, score=99, evidence=["user-override"])]
        for h in result.ranked:
            if h.category != primary:
                ranked.append(h)
        result = ClassifyResult(
            primary=primary, ranked=ranked, text_used=result.text_used, confidence=1.0,
        )
    raw["challenge_type"] = result.primary
    raw["challenge_type_label"] = _CATEGORY_LABELS.get(result.primary or "", result.primary)
    raw["type_confidence"] = result.confidence
    raw["type_scores"] = [
        {"category": h.category, "label": h.label, "score": h.score,
         "evidence": h.evidence}
        for h in result.ranked
    ]
    # 让 DocStore.search / 规划上下文直接吃到类型词
    label = raw["challenge_type_label"]
    tip = f"[题型判定] {label} ({result.primary}, confidence={result.confidence})"
    desc = raw.get("description") or ""
    if tip not in desc:
        raw["description"] = f"{tip}\n{desc}".strip()
    return {
        "task": raw,
        "classification": {
            "primary": result.primary,
            "label": raw["challenge_type_label"],
            "confidence": result.confidence,
            "ranked": [asdict(h) for h in result.ranked],
            "text_used": result.text_used,
        },
        "goals_preview": [g.id for g in _default_goals(result.primary, raw)],
    }


class ChallengeUnderstander(TaskUnderstander):
    """任务理解层实现:多源 raw → 题型判定写入 raw_content + goal_list。

    替换 MockTaskUnderstander 挂到 Engine(understander=...)。
    若 raw 已带 challenge_type(前端已 parse),不再重算;否则现场 classify。
    """

    def understand(self, raw: dict) -> TaskInput:
        raw = dict(raw or {})
        if not raw.get("challenge_type"):
            parsed = parse_challenge(raw)
            raw = parsed["task"]
        goals = _default_goals(raw.get("challenge_type"), raw)
        raw.pop("goals", None)  # 契约:goal_list 单独走,raw_content 不含 goals
        # 精简 attachments 落盘(只留元数据)
        if "attachments" in raw:
            raw["attachments"] = [
                {k: a.get(k) for k in ("name", "path", "size", "mime") if a.get(k) is not None}
                for a in raw["attachments"]
            ]
        return TaskInput(raw_content=raw, goal_list=goals)
