"""ChallengeStore(SQLite 索引+flag) + AttachmentCache(LRU) 单测。"""

import pytest

from ctf_platform.errors import CacheIntegrityError
from ctf_platform.storage import (
    AttachmentCache,
    ChallengeMeta,
    ChallengeStore,
    FileRecord,
    connect,
)


def _meta(cid="c1", friendly="F-0001", name="题一", category="PWN", files=None):
    return ChallengeMeta(
        challenge_id=cid, platform="ctf2", friendly_id=friendly, name=name,
        category=category, difficulty="Easy", description="desc",
        files=files or [FileRecord("f1", "pwn1")],
    )


def _make_store(tmp_path):
    return ChallengeStore(connect(tmp_path))


# ===== ChallengeStore: challenges =====


def test_upsert_challenge_insert_then_update(tmp_path):
    store = _make_store(tmp_path)
    assert store.upsert_challenge(_meta()) == "insert"
    assert store.upsert_challenge(_meta()) == "update"
    assert len(store.query_challenges()) == 1


def test_get_challenge_by_id_or_friendly(tmp_path):
    store = _make_store(tmp_path)
    store.upsert_challenge(_meta())
    row = store.get_challenge("c1")
    assert row and row["name"] == "题一"
    row = store.get_challenge("F-0001")
    assert row and row["challenge_id"] == "c1"
    assert store.get_challenge("nope") is None


def test_query_filters(tmp_path):
    store = _make_store(tmp_path)
    store.upsert_challenge(_meta(cid="c1", friendly="F-1", category="PWN"))
    store.upsert_challenge(_meta(cid="c2", friendly="F-2", category="WEB", name="w"))
    store.upsert_challenge(_meta(cid="c3", friendly="F-3", category="PWN", name="p2"))
    assert [r["challenge_id"] for r in store.query_challenges(category="PWN")] == ["c1", "c3"]
    assert [r["challenge_id"] for r in store.query_challenges(platform="ctf2")] == [
        "c1", "c2", "c3",
    ]
    assert store.query_challenges(category="REVERSE") == []


# ===== ChallengeStore: files =====


def test_upsert_files_replace_and_keep_unchanged(tmp_path):
    store = _make_store(tmp_path)
    store.upsert_challenge(_meta())
    store.upsert_challenge_files("c1", [FileRecord("f1", "a.bin"), FileRecord("f2", "b.bin")])
    assert {f["file_name"] for f in store.files_for("c1")} == {"a.bin", "b.bin"}
    # 只改一份,另一份保持
    store.upsert_challenge_files("c1", [FileRecord("f1", "a.bin"), FileRecord("f3", "c.bin")])
    names = {f["file_name"] for f in store.files_for("c1")}
    assert names == {"a.bin", "c.bin"}
    assert store.file("f3") is not None
    assert store.file("f2") is None


# ===== ChallengeStore: flags =====


def test_flag_upsert_get(tmp_path):
    store = _make_store(tmp_path)
    store.upsert_challenge(_meta())
    store.upsert_flag("c1", "flag{x}", source="flag_rules", verified=True)
    f = store.get_flag("c1")
    assert f["flag"] == "flag{x}" and f["verified"] == 1 and f["source"] == "flag_rules"
    store.upsert_flag("c1", "flag{y}", source="manual", verified=False)
    assert store.get_flag("c1")["flag"] == "flag{y}"
    assert store.get_flag("c1")["verified"] == 0
    assert store.get_flag("unknown") is None


# ===== ChallengeStore: submissions 日志 =====


def test_submission_log_append_order(tmp_path):
    store = _make_store(tmp_path)
    store.upsert_challenge(_meta())
    store.log_submission("c1", "flag{a}", verdict="INCORRECT_FLAG", correct=False)
    store.log_submission("c1", "flag{b}", verdict="success", correct=True)
    logs = store.recent_submissions("c1")
    assert len(logs) == 2
    assert logs[0]["verdict"] == "success" and logs[0]["correct"] == 1  # 最新在前
    assert logs[1]["flag"] == "flag{a}" and logs[1]["correct"] == 0
    assert store.recent_submissions("nope") == []


# ===== AttachmentCache =====


def _cache(tmp_path, capacity=10**6, dl=None, store=None):
    store = store or _make_store(tmp_path)
    dl = dl or {"f1": b"data"}
    store.upsert_challenge(_meta())
    # FK:attachment_cache.file_id → challenge_files.file_id;为每个下载键登记一行
    store.upsert_challenge_files("c1", [FileRecord(fid, fid) for fid in dl])
    return store, AttachmentCache(
        store, tmp_path / "blobs", capacity,
        downloader=lambda fid, cid: dl[fid],
    )


def test_ensure_hit_returns_blob_and_touch(tmp_path):
    store, cache = _cache(tmp_path)
    p1 = cache.ensure("f1", "c1", "a", None)
    assert p1.is_file() and p1.read_bytes() == b"data"
    p2 = cache.ensure("f1", "c1", "a", None)  # 命中,不重下载
    assert p2 == p1


def test_lru_evicts_oldest_to_threshold(tmp_path):
    store, cache = _cache(tmp_path, capacity=2500, dl={"a": b"a" * 1000, "b": b"b" * 1000, "c": b"c" * 1000})
    cache.ensure("a", "c1", "a", None)
    cache.ensure("b", "c1", "b", None)
    # 显式控制 last_access,避免时序抖动
    store.execute("UPDATE attachment_cache SET last_access=1.0 WHERE file_id='a'")
    store.execute("UPDATE attachment_cache SET last_access=2.0 WHERE file_id='b'")
    store.commit()
    cache.ensure("c", "c1", "c", None)  # 3000 > 2500 → 淘汰 a(最旧) 到 <=2000
    assert not cache._blob_path("a").is_file()
    assert cache._blob_path("b").is_file() and cache._blob_path("c").is_file()
    assert cache.total_bytes() == 2000


def test_lru_touch_preserves_recent(tmp_path):
    store, cache = _cache(tmp_path, capacity=2500, dl={"a": b"a" * 1000, "b": b"b" * 1000, "c": b"c" * 1000})
    cache.ensure("a", "c1", "a", None)
    cache.ensure("b", "c1", "b", None)
    cache.ensure("a", "c1", "a", None)  # touch a → a 变最新
    store.execute("UPDATE attachment_cache SET last_access=1.0 WHERE file_id='b'")
    store.commit()
    cache.ensure("c", "c1", "c", None)  # 淘汰 b(现在最旧)
    assert not cache._blob_path("b").is_file()
    assert cache._blob_path("a").is_file() and cache._blob_path("c").is_file()


def test_md5_mismatch_does_not_land(tmp_path):
    store, cache = _cache(tmp_path, dl={"f1": b"hello"})
    with pytest.raises(CacheIntegrityError):
        cache.ensure("f1", "c1", "a", "deadbeef")
    assert not cache._blob_path("f1").is_file()


def test_materialize_subdir(tmp_path):
    store, cache = _cache(
        tmp_path, dl={"f1": b"x", "f2": b"y"},
        store=ChallengeStore(connect(tmp_path)),
    )
    cache.ensure("f1", "c1", "sub/a.bin", None)
    cache.ensure("f2", "c1", "b.bin", None)
    n = cache.materialize("c1", tmp_path / "ch" / "distfiles")
    assert n == 2
    assert (tmp_path / "ch" / "distfiles" / "sub" / "a.bin").read_bytes() == b"x"
    assert (tmp_path / "ch" / "distfiles" / "b.bin").read_bytes() == b"y"


def test_stats_and_purge(tmp_path):
    store, cache = _cache(tmp_path)
    cache.ensure("f1", "c1", "a", None)
    orphan = cache._blob_path("zz")
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan")
    s = cache.stats()
    assert s["file_count"] == 1 and s["total_bytes"] == 4 and s["capacity_bytes"] == 10**6
    freed = cache.purge()
    assert freed >= 4 + len(b"orphan")
    assert cache.stats()["file_count"] == 0
    assert not cache._blob_path("f1").is_file() and not orphan.exists()


def test_single_file_over_capacity_kept(tmp_path, caplog):
    store, cache = _cache(tmp_path, capacity=10, dl={"big": b"z" * 100})
    cache.ensure("big", "c1", "big", None)
    assert cache._blob_path("big").is_file()
    assert any("超过缓存容量" in r.message for r in caplog.records)
