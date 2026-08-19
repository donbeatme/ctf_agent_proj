"""ctf_platform → RealTaskUnderstander 离线整链集成测试。

FakeDlAdapter(Ctf2Adapter) 覆写 download/parse:用真实 fixture 元数据 +
固定假附件驱动 ingest → 缓存 → 物化 → understander 消费,
断言 challenge_type/产物非空/JSON-safe;跑两遍验证缓存命中不重下载。
"""

import hashlib
import json
from pathlib import Path

from ctf_platform.config import StoreSettings
from ctf_platform.ctf2 import Ctf2Adapter
from task_understanding.real_understander import RealTaskUnderstander

REAL = Path(__file__).resolve().parent / "fixtures" / "real"
# 带 1 个附件的 PWN 题(rip)
PWN = "ctf2_pwn_PCHAL-2026-0063.json"
PWN_FILE = "pwn1"


class FakeDlAdapter(Ctf2Adapter):
    """download 返回固定假字节;parse 把 file_md5/file_size 对齐该字节。

    基类在 __init__ 里把 cache.downloader 绑定到 self.download,子类覆写
    download 即可让缓存走假下载,全程无网络。
    """

    payload = b"FAKE-PWN-BINARY-0123456789" * 64

    def __init__(self, settings):
        self.download_calls = 0
        super().__init__(settings)

    def parse(self, source):
        meta = super().parse(source)
        digest = hashlib.md5(self.payload).hexdigest()
        for f in meta.files:
            f.file_md5 = digest
            f.file_size = len(self.payload)
        return meta

    def download(self, file_id, challenge_id):
        self.download_calls += 1
        return self.payload


def _adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("CTF_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("CTF2_PRACTICE_GROUND_ID", "pg-integration")
    return FakeDlAdapter(StoreSettings.from_env())


def _load_raw():
    return json.loads((REAL / PWN).read_text(encoding="utf-8"))


def test_ingest_materializes_and_understander_consumes(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    dest = a.ingest(_load_raw())

    assert (dest / "metadata.yml").is_file()
    dist = dest / "distfiles" / PWN_FILE
    assert dist.is_file()
    assert dist.read_bytes() == FakeDlAdapter.payload

    task = RealTaskUnderstander().understand({"challenge_dir": str(dest)})
    rc = task.raw_content
    assert rc["name"] == "rip"
    assert rc["challenge_type"] == "ctf-pwn"
    assert rc["artifacts"], "物化目录产物不应为空"
    # understander 契约:files 约束 + JSON-safe 序列化
    constraint_values = [c["value"] for c in rc["constraints"] if c["type"] == "provided_files"]
    assert constraint_values == [[PWN_FILE]]
    assert [g.id for g in task.goal_list] == ["obtain_flag"]
    json.dumps(task.model_dump(), ensure_ascii=False)


def test_ingest_twice_cache_hit_no_redownload(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    a.ingest(_load_raw())
    a.ingest(_load_raw())
    assert a.download_calls == 1
    assert a.cache_stats()["file_count"] == 1


def test_ingest_web_challenge_without_files(tmp_path, monkeypatch):
    """无附件题也走通:files 空 → 物化目录无 distfiles,understander 仍可用。"""
    from ctf_platform.base import build_summary

    a = _adapter(tmp_path, monkeypatch)
    raw = _load_raw()
    raw = {**raw, "category": "WEB", "name": "web-no-files", "files": []}
    dest = a.ingest(raw)
    assert not (dest / "distfiles").exists() or not any((dest / "distfiles").iterdir())
    task = RealTaskUnderstander().understand({"challenge_dir": str(dest)})
    assert task.raw_content["challenge_type"] == "ctf-web"
    assert build_summary("x", "WEB", None, None, []) == "[WEB] x"
