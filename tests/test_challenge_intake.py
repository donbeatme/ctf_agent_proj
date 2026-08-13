"""多源题目摄入 + 题型判定 + ChallengeUnderstander。"""

from agent.challenge_intake import (
    ChallengeUnderstander,
    classify_challenge,
    normalize_sources,
    parse_challenge,
)
from agent.skills import CtfSkillsDocStore


def test_classify_crypto_from_text():
    r = classify_challenge({"title": "Easy RSA", "description": "RSA oracle decrypt n e c"})
    assert r.primary == "ctf-crypto"
    assert r.ranked[0].score > 0


def test_classify_forensics_from_pcap_ext():
    r = classify_challenge(
        {"title": "cap", "description": "analyze traffic"},
        attachments=[{"name": "traffic.pcap", "path": ""}],
    )
    assert r.primary == "ctf-forensics"


def test_classify_web_from_url_field():
    raw = normalize_sources(target_url="http://127.0.0.1:8080", description="sql injection login")
    out = parse_challenge(raw)
    assert out["classification"]["primary"] == "ctf-web"
    assert "challenge_type" in out["task"]


def test_parse_ctfd_json():
    blob = {
        "name": "baby-bof",
        "category": "pwn",
        "description": "buffer overflow ret2libc",
        "files": ["chal"],
    }
    raw = normalize_sources(json_blob=blob)
    out = parse_challenge(raw)
    assert out["task"]["title"] == "baby-bof"
    assert out["classification"]["primary"] == "ctf-pwn"
    assert "find_flag" in out["goals_preview"]


def test_challenge_understander_strips_goals():
    u = ChallengeUnderstander()
    ti = u.understand({
        "title": "stego",
        "description": "lsb steganography png",
        "goals": [{"id": "g_custom"}],
        "attachments": [{"name": "flag.png"}],
    })
    assert "goals" not in ti.raw_content
    assert ti.raw_content["challenge_type"] == "ctf-forensics"
    ids = [g.id for g in ti.goal_list]
    assert ids[0] == "g_custom"
    assert "find_flag" in ids
    assert "analyze_attachments" in ids


def test_docstore_prefers_challenge_type():
    store = CtfSkillsDocStore(top_n=2)
    hits = store.search({
        "title": "x",
        "description": "base64 encoding misc",
        "challenge_type": "ctf-crypto",
    })
    assert hits and hits[0][0] == "ctf-crypto"


def test_override_category():
    raw = normalize_sources(title="t", description="base64", category_override="ctf-web")
    out = parse_challenge(raw, category_override="ctf-web")
    assert out["classification"]["primary"] == "ctf-web"
    assert out["classification"]["confidence"] == 1.0
