"""题型判定。

来自远程 challenge_intake 的 classify_challenge 逻辑，统一收编到任务理解层。
"""

from __future__ import annotations

import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from agent.skills import CATEGORY_KEYWORDS, _task_text
from task_understanding.goals import default_goals


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

# 强取证标记:平台常把取证题(流量分析/pcap/stego/隐写)归入通用 MISC 桶,
# 出现这些内容信号时把该题路由到 ctf-forensics,而不是泛化成 ctf-misc。
_FORENSICS_MARKERS = (
    "嗅探", "抓包", "取证", "流量分析", "流量包", "wireshark", "pcap",
    "stego", "steganography", "隐写", "lsb",
)


def _forensics_marker_hits(text: str, attachments: list | None) -> int:
    hits = sum(1 for m in _FORENSICS_MARKERS if m in text)
    for att in attachments or []:
        name = (att.get("name") or att.get("filename") or "").lower()
        if Path(name).suffix in (".pcap", ".pcapng", ".cap") or any(
            m in name for m in _FORENSICS_MARKERS
        ):
            hits += 1
    return hits


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


def classify_challenge(
    task: dict | str,
    *,
    attachments: list[dict] | None = None,
    top_n: int = 5,
) -> ClassifyResult:
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
    # 取证提升:平台把取证题(流量/pcap/stego/隐写)归入通用 MISC 桶。
    # 仅当通用 MISC 桶当前是主类、且内容出现强取证标记时,把 ctf-forensics 提升为主类,
    # 保证 6 类经典题型(含 Forensics)可区分路由,而不是把 pcap/流量分析题泛化成 misc。
    if primary == "ctf-misc":
        hits = _forensics_marker_hits(text, attachments or [])
        if hits:
            scores["ctf-forensics"] = scores.get("ctf-forensics", 0) + 12 + hits
            evidence.setdefault("ctf-forensics", []).append(
                f"forensics-marker:x{hits}(MISC 桶内取证内容信号)"
            )
            ranked = [
                TypeHit(category=c, score=sc, evidence=evidence.get(c, [])[:8])
                for c, sc in scores.items() if sc > 0
            ]
            ranked.sort(key=lambda h: (-h.score, h.category))
            ranked = ranked[:top_n]
            primary = ranked[0].category
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
    return ClassifyResult(
        primary=primary,
        ranked=ranked,
        text_used=text[:500],
        confidence=round(confidence, 3),
    )


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
            primary=primary,
            ranked=ranked,
            text_used=result.text_used,
            confidence=1.0,
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
        "goals_preview": [g.id for g in default_goals(result.primary, raw)],
    }
