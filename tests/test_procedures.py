"""challenge_procedures 表 CRUD + 精确匹配(design/verification.md)。"""

import pytest

from ctf_platform.storage import ChallengeMeta, ChallengeStore, connect


def _make_store(tmp_path):
    return ChallengeStore(connect(tmp_path))


def _meta(cid="c1", friendly="F-0001", tpl=None, has_container=False):
    return ChallengeMeta(
        challenge_id=cid, platform="ctf2", friendly_id=friendly, name="题",
        category="WEB", has_container=has_container, extra={"template_id": tpl} if tpl else None,
    )


def _seed(store):
    store.upsert_challenge(_meta("c1", "F-0001", "tpl-A", has_container=True))
    store.upsert_challenge(_meta("c2", "F-0002", "tpl-B", has_container=True))
    store.upsert_procedure(
        "p1", "c1", method="procedure", flag="F1", flag_format="F{}",
        verifier_path="solve.py", trace={"m": "x"}, platform_verified=True,
    )
    store.upsert_procedure(
        "p2", "c2", method="procedure", verifier_path="s.py", platform_verified=True,
    )
    store.upsert_procedure(
        "p3", "c1", method="procedure", verifier_path="unverified.py", platform_verified=False,
    )
    store.upsert_procedure(
        "p4", "c1", method="literal", flag="L1", platform_verified=True,
    )


def test_upsert_autofills_keys_and_updates(tmp_path):
    store = _make_store(tmp_path)
    _seed(store)
    p = store.get_procedures("c1")
    p1 = next(x for x in p if x["procedure_id"] == "p1")
    assert p1["friendly_id"] == "F-0001"
    assert p1["template_id"] == "tpl-A"
    assert p1["platform_verified"] == 1
    assert p1["trace_json"] and '"m": "x"' in p1["trace_json"]
    # upsert 同一 procedure_id 覆盖,不产生新行
    store.upsert_procedure("p1", "c1", method="procedure", verifier_path="new.py",
                           platform_verified=True)
    assert len(store.get_procedures("c1")) == 3
    assert next(x for x in store.get_procedures("c1") if x["procedure_id"] == "p1")["verifier_path"] == "new.py"


def test_get_validated_only_procedure_verified(tmp_path):
    store = _make_store(tmp_path)
    _seed(store)
    v = store.get_validated_procedures("c1")
    # 排除 literal 与未验证
    assert {x["procedure_id"] for x in v} == {"p1"}


def test_match_procedures_exact_only(tmp_path):
    store = _make_store(tmp_path)
    _seed(store)
    # friendly_id 精确命中
    assert {x["procedure_id"] for x in store.match_procedures("F-0001", None)} == {"p1"}
    # template_id 精确命中(跨场地同题)
    assert {x["procedure_id"] for x in store.match_procedures(None, "tpl-B")} == {"p2"}
    # 无精确匹配键 → 空(不做跨题召回)
    assert store.match_procedures("F-NOPE", None) == []
    assert store.match_procedures(None, "tpl-NA") == []
    assert store.match_procedures(None, None) == []
    # 仅精确:相似而非全等不命中
    assert store.match_procedures("F-000", None) == []


def test_match_order_by_last_ok_desc(tmp_path):
    store = _make_store(tmp_path)
    store.upsert_challenge(_meta("c1", "F-0001", "tpl-A"))
    store.upsert_procedure("old", "c1", method="procedure", platform_verified=True)
    store.conn.execute(
        "UPDATE challenge_procedures SET last_ok_at='2020-01-01' WHERE procedure_id='old'")
    store.upsert_procedure("new", "c1", method="procedure", platform_verified=True)
    rows = store.match_procedures("F-0001", None)
    assert rows[0]["procedure_id"] == "new"


def test_promote_and_mark_ok(tmp_path):
    store = _make_store(tmp_path)
    _seed(store)
    store.promote_procedure("p3")
    p3 = next(x for x in store.get_procedures("c1") if x["procedure_id"] == "p3")
    assert p3["platform_verified"] == 1
    store.mark_procedure_ok("p2")
    row = next(x for x in store.get_procedures("c2") if x["procedure_id"] == "p2")
    assert row["used_count"] == 1
    assert row["last_ok_at"] is not None


def test_delete_challenge_cascades_procedures(tmp_path):
    store = _make_store(tmp_path)
    _seed(store)
    store.conn.execute("DELETE FROM challenges WHERE challenge_id='c1'")
    store.conn.commit()
    assert store.get_procedures("c1") == []
