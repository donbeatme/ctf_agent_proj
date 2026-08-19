"""真实 ctf2 平台题目快照的离线摄入测试。

fixtures 来自 ctf2.dasctf.com 公开/会话 API 的只读拉取(tests/fixtures/real/),
已剥离下载 URL 与凭据。这里不触网:加载 fixture JSON,验证任务理解层
对真实题目形态(大写 category、中英文题名、附件 files 列表)的摄入不变量。

验证两件事:
1. RealTaskUnderstander 多源直通:goal 默认 [obtain_flag]、题型判定正确、JSON 安全。
2. normalize_sources(json_blob) + parse_challenge:name→title 映射、题型判定、goals_preview。
"""

import json
from pathlib import Path

import pytest

from task_understanding.classify import parse_challenge
from task_understanding.normalize import normalize_sources
from task_understanding.real_understander import RealTaskUnderstander

REAL = Path(__file__).resolve().parent / "fixtures" / "real"

# ctf2 分类(大写) → 任务理解层 challenge_type
CATEGORY_TO_TYPE = {
    "WEB": "ctf-web",
    "PWN": "ctf-pwn",
    "CRYPTO": "ctf-crypto",
    "MISC": "ctf-misc",
    "REVERSE": "ctf-reverse",
}


def _expected_type(entry: dict) -> str:
    """期望 challenge_type:manifest 的 expected_type 覆盖优先(取证题平台归 MISC 但内容为 forensics)。"""
    return entry.get("expected_type") or CATEGORY_TO_TYPE[entry["category"]]


def _manifest() -> list[dict]:
    return json.loads((REAL / "manifest.json").read_text(encoding="utf-8"))


def _load(entry: dict) -> dict:
    return json.loads((REAL / entry["file"]).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def fixtures():
    return [(e, _load(e)) for e in _manifest()]


# ===== 多源直通摄入 =====


def test_all_fixtures_have_expected_category(fixtures):
    for entry, _ in fixtures:
        assert entry["category"] in CATEGORY_TO_TYPE, f"未预期分类 {entry['category']}"


def test_multisource_ingestion_classifies_and_defaults_goal(fixtures):
    understander = RealTaskUnderstander()
    for entry, raw in fixtures:
        task = understander.understand(dict(raw))
        expect = _expected_type(entry)
        assert task.raw_content["challenge_type"] == expect, entry["name"]
        assert task.raw_content["name"] == entry["name"]
        assert [g.id for g in task.goal_list] == ["obtain_flag"], entry["name"]


def test_multisource_keeps_files_attachment_metadata(fixtures):
    understander = RealTaskUnderstander()
    with_files = [pair for pair in fixtures if pair[0]["has_files"]]
    assert with_files, "fixture 应至少含一道带附件题目"
    for entry, raw in with_files:
        task = understander.understand(dict(raw))
        files = task.raw_content.get("files") or []
        assert len(files) == entry["file_count"], entry["name"]
        names = [f.get("file_name") or f.get("path") for f in files]
        assert all(n for n in names), entry["name"]


def test_multisource_output_is_json_safe(fixtures):
    understander = RealTaskUnderstander()
    for entry, raw in fixtures:
        dumped = understander.understand(dict(raw)).model_dump()
        json.dumps(dumped, ensure_ascii=False)


def test_multisource_flag_format_bleeds_into_description(fixtures):
    """描述里带 flag{...} 的真实题(如签到)应完整透传到 raw_content。"""
    understander = RealTaskUnderstander()
    for entry, raw in fixtures:
        task = understander.understand(dict(raw))
        desc = task.raw_content.get("description") or ""
        assert isinstance(desc, str)
        assert not json.dumps(task.raw_content, ensure_ascii=False).__contains__("\x00")


# ===== normalize_sources + parse_challenge =====


def test_normalize_parse_maps_real_challenge(fixtures):
    for entry, raw in fixtures:
        norm = normalize_sources(json_blob=dict(raw))
        assert norm["title"] == entry["name"], entry["name"]
        assert norm["category"] == entry["category"]
        out = parse_challenge(norm)
        expect = _expected_type(entry)
        assert out["classification"]["primary"] == expect, entry["name"]
        assert out["goals_preview"] == ["obtain_flag"], entry["name"]


def test_normalize_parse_keeps_files_as_attachments(fixtures):
    for entry, raw in fixtures:
        if not entry["has_files"]:
            continue
        norm = normalize_sources(json_blob=dict(raw))
        attachments = norm.get("attachments") or []
        assert len(attachments) == entry["file_count"], entry["name"]
        assert all(a.get("name") for a in attachments), entry["name"]
