"""多源任务输入归一化。

来自远程 challenge_intake 的 normalize_sources 逻辑，统一收编到任务理解层。
"""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import Any


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
    """多源字段归一成 engine 可消费的 raw dict（尚未剥 goals）。"""
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
                    fname = (
                        f.get("name")
                        or f.get("filename")
                        or f.get("file_name")
                        or Path(f.get("path", "")).name
                    )
                    extra.append({
                        "name": fname,
                        "path": f.get("path", ""),
                        "size": f.get("size") or f.get("file_size"),
                        "mime": (
                            f.get("mime")
                            or f.get("file_type")
                            or mimetypes.guess_type(fname)[0]
                        ),
                    })
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
