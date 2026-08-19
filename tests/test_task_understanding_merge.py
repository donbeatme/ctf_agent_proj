from task_understanding.classify import classify_challenge, parse_challenge
from task_understanding.normalize import normalize_sources


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
    assert out["goals_preview"] == ["obtain_flag"]  # 无用户 goals → 默认单目标
