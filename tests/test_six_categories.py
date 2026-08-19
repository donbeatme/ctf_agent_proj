"""6 类经典 CTF 题型各测一道(真实平台 fixture 快照,离线)。

平台侧只分 5 类(WEB/PWN/CRYPTO/REVERSE/MISC),Forensics 被平台归到 MISC 桶;
这里用一道真实 pcap 流量题(被嗅探的流量,PCHAL-2026-0462)覆盖第 6 类,
依赖 classify 的取证提升(MISC 桶 + 强取证内容信号 → ctf-forensics)。

验证三类不变量:
1. 每类题 parse_challenge 的主类型路由正确;
2. RealTaskUnderstander 摄入后 challenge_type 正确、goal 默认 [obtain_flag];
3. 6 个期望类型互不相同(证明分类能区分,而非都泛化成 misc)。
"""

import json
from pathlib import Path

from task_understanding.classify import parse_challenge
from task_understanding.normalize import normalize_sources
from task_understanding.real_understander import RealTaskUnderstander

REAL = Path(__file__).resolve().parent / "fixtures" / "real"

# 6 类经典题型各挑一道真实平台快照。
# (platform_category, fixture_file, expected_challenge_type)
ONE_PER_CATEGORY = [
    ("WEB", "ctf2_web_PCHAL-2026-0024.json", "ctf-web"),
    ("PWN", "ctf2_pwn_PCHAL-2026-0062.json", "ctf-pwn"),
    ("CRYPTO", "ctf2_crypto_PCHAL-2026-0016.json", "ctf-crypto"),
    ("REVERSE", "ctf2_reverse_PCHAL-2026-0013.json", "ctf-reverse"),
    # 平台把取证题归 MISC,内容(pcap/流量)信号路由到 ctf-forensics
    ("MISC", "ctf2_misc_PCHAL-2026-0462.json", "ctf-forensics"),
    ("MISC", "ctf2_misc_PCHAL-2026-0007.json", "ctf-misc"),
]


def _load(fixture_file: str) -> dict:
    return json.loads((REAL / fixture_file).read_text(encoding="utf-8"))


def test_six_categories_expected_types_are_distinct():
    types = [expected for _, _, expected in ONE_PER_CATEGORY]
    assert len(set(types)) == 6, f"6 类期望类型未区分: {types}"
    assert set(types) == {
        "ctf-web", "ctf-pwn", "ctf-crypto", "ctf-reverse",
        "ctf-forensics", "ctf-misc",
    }


def test_one_per_category_parse_routes_correct_type():
    for platform_cat, fixture_file, expected in ONE_PER_CATEGORY:
        raw = _load(fixture_file)
        norm = normalize_sources(json_blob=dict(raw))
        assert norm["category"] == platform_cat, fixture_file
        out = parse_challenge(norm)
        assert out["classification"]["primary"] == expected, fixture_file
        assert out["classification"]["label"] is not None, fixture_file
        assert out["goals_preview"] == ["obtain_flag"], fixture_file


def test_one_per_category_understander_classifies_and_goals():
    understander = RealTaskUnderstander()
    for _, fixture_file, expected in ONE_PER_CATEGORY:
        task = understander.understand(_load(fixture_file))
        assert task.raw_content["challenge_type"] == expected, fixture_file
        assert [g.id for g in task.goal_list] == ["obtain_flag"], fixture_file
        json.dumps(task.model_dump(), ensure_ascii=False)  # JSON 安全
