"""ChallengeAdapter 基类平台无关性:FakeAdapter 驱动 ingest/submit/persist。"""

from pathlib import Path

import pytest
import yaml

from ctf_platform.base import (
    AdapterError,
    ChallengeAdapter,
    SubmitResult,
    clean_challenge_dir,
    verify_challenge_dir,
)
from ctf_platform.config import StoreSettings
from ctf_platform.storage import ChallengeMeta, FileRecord


class FakeAdapter(ChallengeAdapter):
    platform = "fake"

    def __init__(self, settings):
        self.download_calls = 0
        super().__init__(settings)

    def parse(self, source):
        return ChallengeMeta(
            challenge_id="c-fake", platform=self.platform, friendly_id="FAKE-1",
            name="fake", category="WEB", description="desc",
            files=[FileRecord("f1", "x.bin")],
        )

    def download(self, file_id, challenge_id):
        self.download_calls += 1
        return b"data"

    def submit(self, challenge_id, flag):
        return SubmitResult(ok=True, correct=True, message="ok")


def _adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("CTF_STORE_DIR", str(tmp_path))
    return FakeAdapter(StoreSettings.from_env())


def test_ingest_materializes_and_indexes(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    dest = a.ingest("whatever")
    assert isinstance(dest, Path)
    assert (dest / "metadata.yml").is_file()
    assert (dest / "distfiles" / "x.bin").read_bytes() == b"data"
    assert a.download_calls == 1
    row = a.store.get_challenge("FAKE-1")
    assert row and row["name"] == "fake" and row["challenge_type"] == "ctf-web"
    assert a.store.file("f1") is not None


def test_ingest_cache_hit_does_not_redownload(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    a.ingest("whatever")
    a.ingest("whatever")  # 缓存命中
    assert a.download_calls == 1


def test_ingest_materializes_to_dest(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    dest = a.ingest("whatever", dest_dir=tmp_path / "custom")
    assert (dest / "metadata.yml").is_file()
    assert str(dest) == str((tmp_path / "custom").resolve())


def test_submit_and_persist_flag(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    a.ingest("whatever")  # 先物化落库:c-fake 才有行(FK),同真实提交前必有 ingest
    res = a.submit("c-fake", "flag{x}")
    assert res.ok and res.correct is True
    a.persist_flag("c-fake", "flag{x}", verified=True)
    f = a.get_flag("c-fake")
    assert f["flag"] == "flag{x}" and f["verified"] == 1


def test_cache_stats_and_purge(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    a.ingest("whatever")
    s = a.cache_stats()
    assert s["file_count"] == 1 and s["total_bytes"] == 4
    freed = a.cache_purge()
    assert freed >= 4
    assert a.cache_stats()["file_count"] == 0


def test_target_defaults_noop(tmp_path, monkeypatch):
    """靶机开/关基类桩:不支持的平台返回空 dict,不抛异常。"""
    a = _adapter(tmp_path, monkeypatch)
    assert a.start_target("c-1") == {}
    assert a.stop_target("c-1") == {}


# ── 环境打开清理:非依赖/非附件遗留产物 ──────────────────────────────

def test_clean_challenge_dir_keeps_metadata_and_distfiles(tmp_path):
    """仅保留 metadata.yml + distfiles/,删除其余顶层文件与子目录。"""
    root = tmp_path / "ch"
    (root / "distfiles").mkdir(parents=True)
    (root / "metadata.yml").write_text("m", encoding="utf-8")
    (root / "distfiles" / "x.bin").write_bytes(b"data")
    (root / "solve_extract.py").write_text("s", encoding="utf-8")
    (root / "_ctf_exec.py").write_text("e", encoding="utf-8")
    (root / "junk_dir").mkdir()
    (root / "junk_dir" / "f").write_text("j", encoding="utf-8")

    removed = clean_challenge_dir(root)

    assert (root / "metadata.yml").is_file()
    assert (root / "distfiles" / "x.bin").read_bytes() == b"data"
    assert not (root / "solve_extract.py").exists()
    assert not (root / "_ctf_exec.py").exists()
    assert not (root / "junk_dir").exists()
    assert sorted(removed) == sorted([
        str(root / "solve_extract.py"), str(root / "_ctf_exec.py"),
        str(root / "junk_dir"),
    ])


def test_clean_challenge_dir_missing_dir_returns_empty(tmp_path):
    assert clean_challenge_dir(tmp_path / "nope") == []


# ── 物化完整性守卫:run 启动前 fail-fast ─────────────────────────────

def test_verify_challenge_dir_ready(tmp_path):
    """目录 + metadata.yml + 附件齐全 → 无问题(空列表)。"""
    root = tmp_path / "ch"
    (root / "distfiles").mkdir(parents=True)
    (root / "distfiles" / "x.bin").write_bytes(b"data")
    (root / "metadata.yml").write_text(
        yaml.safe_dump({"id": "c1", "name": "n"}), encoding="utf-8"
    )
    assert verify_challenge_dir(root, ["x.bin"]) == []


def test_verify_challenge_dir_missing_dir(tmp_path):
    assert "不存在" in verify_challenge_dir(tmp_path / "nope")[0]


def test_verify_challenge_dir_missing_metadata(tmp_path):
    root = tmp_path / "ch"
    root.mkdir()
    assert "metadata.yml" in verify_challenge_dir(root)[0]


def test_verify_challenge_dir_bad_metadata(tmp_path):
    root = tmp_path / "ch"
    root.mkdir()
    (root / "metadata.yml").write_text("id: 只有一半", encoding="utf-8")
    problems = verify_challenge_dir(root)
    assert any("id/name" in p for p in problems)


def test_verify_challenge_dir_missing_attachment(tmp_path):
    root = tmp_path / "ch"
    (root / "distfiles").mkdir(parents=True)
    (root / "metadata.yml").write_text(
        yaml.safe_dump({"id": "c1", "name": "n"}), encoding="utf-8"
    )
    problems = verify_challenge_dir(root, ["x.bin"])
    assert any("x.bin" in p and "附件缺失" in p for p in problems)


def test_ingest_fails_fast_when_attachment_missing(tmp_path, monkeypatch):
    """声明的附件没落盘(物化不完整)→ ingest 抛 AdapterError,不启动 run。"""
    a = _adapter(tmp_path, monkeypatch)
    a.cache.materialize = lambda *a, **k: None  # 附件没写进 distfiles/
    with pytest.raises(AdapterError, match="附件缺失"):
        a.ingest("whatever")


def test_ingest_fails_fast_records_materialize_event(tmp_path, monkeypatch):
    """物化守卫失败:抛 AdapterError 同时进 adapter.materialize_failed(FATAL)。"""
    from opslog import attach, detach

    a = _adapter(tmp_path, monkeypatch)
    a.cache.materialize = lambda *a, **k: None
    seen = []
    sink = lambda kind, detail: seen.append((kind, detail))
    attach(sink)
    try:
        with pytest.raises(AdapterError, match="附件缺失"):
            a.ingest("whatever")
    finally:
        detach(sink)
    fail_ev = [d for k, d in seen if k == "adapter.materialize_failed"]
    assert len(fail_ev) == 1
    assert fail_ev[0]["level"] == "fatal"


def test_adapter_clean_challenge_dir_resolves_by_friendly_id(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    dest = a.ingest("whatever")
    (dest / "leak.py").write_text("leak", encoding="utf-8")

    removed = a.clean_challenge_dir("c-fake")

    assert removed == [str(dest / "leak.py")]
    assert (dest / "metadata.yml").is_file()
    assert (dest / "distfiles" / "x.bin").is_file()


def test_reingest_cleans_previous_run_artifacts(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    dest = a.ingest("whatever")
    (dest / "solve_extract.py").write_text("leftover", encoding="utf-8")

    a.ingest("whatever")  # 缓存命中,不重新下载

    assert not (dest / "solve_extract.py").exists()
    assert a.download_calls == 1
