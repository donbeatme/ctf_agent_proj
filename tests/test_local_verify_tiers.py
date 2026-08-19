"""_local_verify 分层判定(T0 literal / T1 procedure 重跑 / 回退平台)。"""

from ctf_platform.base import ChallengeAdapter, SubmitResult
from ctf_platform.config import StoreSettings
from ctf_platform.storage import ChallengeMeta, ChallengeStore, connect


class _TierAdapter(ChallengeAdapter):
    """最小可用适配器:存真 store(tmp),start_target 固定返回 tgt:80。"""

    def __init__(self, settings):
        super().__init__(settings)

    def parse(self, source):
        return ChallengeMeta(challenge_id="x", platform="t", friendly_id="F",
                             name="x", category="WEB")

    def download(self, file_id, challenge_id):
        return b""

    def submit(self, challenge_id, flag):
        return SubmitResult(ok=True)

    def start_target(self, challenge_id):
        return {"host": "tgt", "port": 80}


def _adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("CTF_STORE_DIR", str(tmp_path))
    return _TierAdapter(StoreSettings.from_env())


def _meta(cid, friendly, has_container, tpl=None):
    return ChallengeMeta(
        challenge_id=cid, platform="t", friendly_id=friendly, name="题", category="WEB",
        has_container=has_container, extra={"template_id": tpl} if tpl else None,
    )


def _dynamic_with_procedure(a, runner, verifier="extract.py"):
    """动态题 + 一条已验证 procedure + 注入 runner。"""
    a.store.upsert_challenge(_meta("c-dyn", "F-DYN", True, "tpl-D"))
    a.store.upsert_procedure(
        "p-dyn", "c-dyn", method="procedure", flag="CTF2{old}",
        flag_format="CTF2{}", verifier_path=verifier, platform_verified=True,
    )
    a.set_procedure_runner(runner)


def test_static_literal_unchanged(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    a.store.upsert_challenge(_meta("c-static", "F-STATIC", False))
    a.store.upsert_flag("c-static", "flag{exact}", source="verified_submission", verified=True)
    ok = a._local_verify("c-static", "flag{exact}")
    assert ok and ok.ok and ok.correct is True
    bad = a._local_verify("c-static", "flag{wrong}")
    assert bad and bad.ok and bad.correct is False
    # 静态题不因 runner 存在而改变(仍走 literal 比对)
    a.set_procedure_runner(lambda vp, t: "whatever")
    assert a._local_verify("c-static", "flag{exact}").correct is True
    # 未验证字面量 → 交回平台
    a.store.upsert_challenge(_meta("c-static2", "F-S2", False))
    a.store.upsert_flag("c-static2", "flag{x}", source="guess", verified=False)
    assert a._local_verify("c-static2", "flag{x}") is None


def test_dynamic_derived_match_local_procedure(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    _dynamic_with_procedure(a, lambda vp, t: "CTF2{fresh}")
    res = a._local_verify("c-dyn", "CTF2{fresh}")
    assert res.ok and res.correct is True
    assert res.message == "已验证过程重跑推导:答案正确"
    # 命中 → 累加 used_count
    p = next(x for x in a.store.get_procedures("c-dyn") if x["procedure_id"] == "p-dyn")
    assert p["used_count"] == 1


def test_dynamic_derived_mismatch_correct_false(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    _dynamic_with_procedure(a, lambda vp, t: "CTF2{other}")
    res = a._local_verify("c-dyn", "CTF2{fresh}")
    assert res.ok and res.correct is False
    # 未命中不累加
    p = next(x for x in a.store.get_procedures("c-dyn") if x["procedure_id"] == "p-dyn")
    assert p["used_count"] == 0


def test_dynamic_no_procedure_returns_none(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    a.store.upsert_challenge(_meta("c-dyn", "F-DYN", True, "tpl-D"))  # 无 procedure
    a.set_procedure_runner(lambda vp, t: "CTF2{fresh}")
    assert a._local_verify("c-dyn", "CTF2{fresh}") is None


def test_dynamic_no_runner_returns_none(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    a.store.upsert_challenge(_meta("c-dyn", "F-DYN", True, "tpl-D"))
    a.store.upsert_procedure(
        "p-dyn", "c-dyn", method="procedure", verifier_path="extract.py",
        platform_verified=True,
    )  # 不注入 runner
    assert a._local_verify("c-dyn", "CTF2{fresh}") is None


def test_dynamic_runner_derivation_fails_returns_none(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    _dynamic_with_procedure(a, lambda vp, t: None)   # 推导失败
    assert a._local_verify("c-dyn", "CTF2{fresh}") is None
    # runner 抛异常同样回退
    def boom(vp, t):
        raise RuntimeError("sandbox down")
    _dynamic_with_procedure(a, boom)
    assert a._local_verify("c-dyn", "CTF2{fresh}") is None


def test_dynamic_skips_procedure_without_verifier_path(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    a.store.upsert_challenge(_meta("c-dyn", "F-DYN", True, "tpl-D"))
    a.store.upsert_procedure(
        "p-nopath", "c-dyn", method="procedure", verifier_path=None, platform_verified=True,
    )
    a.set_procedure_runner(lambda vp, t: "CTF2{fresh}")
    assert a._local_verify("c-dyn", "CTF2{fresh}") is None
