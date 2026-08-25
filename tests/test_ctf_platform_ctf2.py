"""Ctf2Adapter 单测:parse 字段映射 / 下载 URL 模板回退 / 提交 / 鉴权 / 同步。"""

import json
from pathlib import Path

import pytest
import yaml

from ctf_platform.config import StoreSettings
from ctf_platform.ctf2 import Ctf2Adapter
from ctf_platform.errors import AuthError, DownloadError
from ctf_platform.storage import ChallengeMeta, FileRecord

REAL = Path(__file__).resolve().parent / "fixtures" / "real"
BASE = "https://ctf2.dasctf.com/api/v1"


@pytest.fixture(autouse=True)
def _isolate_adaptor_config(monkeypatch, tmp_path):
    """隔离 config_adaptor:不读仓库根真实 JSON / CTF2_CONFIG_JSON 外部文件,
    仅由测试 env 驱动(防真实凭据/URL 污染 StoreSettings.from_env)。"""
    import config_adaptor

    monkeypatch.setattr(config_adaptor, "_CONFIG_FILE", tmp_path / "config_adaptor.json")
    monkeypatch.setattr(config_adaptor, "_external_config", lambda: {})


class FakeResponse:
    def __init__(self, status_code=200, content=b"", json_data=None, text=None):
        self.status_code = status_code
        self.content = content
        self._json = json_data
        self.text = text if text is not None else content.decode("utf-8", "replace")

    def json(self):
        if isinstance(self._json, Exception):
            raise self._json
        return self._json


class FakeSession:
    def __init__(self):
        self.routes = {}
        self._seq = {}
        self.requests = []

    def add(self, url, status=200, content=b"", json_data=None):
        self.routes[url] = FakeResponse(status, content, json_data)

    def seq(self, url, responses):
        """同一 URL 按顺序返回的响应队列(最后一个无限复用)。"""
        self._seq[url] = list(responses)

    def _resp(self, url):
        queued = self._seq.get(url)
        if queued:
            if len(queued) > 1:
                return queued.pop(0)
            return queued[0]
        return self.routes.get(url, FakeResponse(404))

    def get(self, url, **kw):
        self.requests.append(("GET", url, kw))
        return self._resp(url)

    def post(self, url, **kw):
        self.requests.append(("POST", url, kw))
        return self._resp(url)

    def delete(self, url, **kw):
        self.requests.append(("DELETE", url, kw))
        return self._resp(url)


def _adapter(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("CTF_STORE_DIR", str(tmp_path))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Ctf2Adapter(StoreSettings.from_env(), session=FakeSession())


def _load(fname):
    return json.loads((REAL / fname).read_text(encoding="utf-8"))


# ===== parse =====


def test_parse_fixture_json_maps_fields(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    meta = a.parse(_load("ctf2_pwn_PCHAL-2026-0063.json"))
    assert meta.friendly_id == "PCHAL-2026-0063"
    assert meta.name == "rip"
    assert meta.category == "PWN"
    assert meta.challenge_type == "ctf-pwn"
    assert meta.practice_ground_id == "b9bbb32f-f186-458f-b90b-12440c0f6aea"
    assert len(meta.files) == 1
    f = meta.files[0]
    assert f.file_id and f.file_name


def test_parse_json_file_count_matches_manifest(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    manifest = json.loads((REAL / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest:
        raw = _load(entry["file"])
        meta = a.parse(raw)
        if entry["has_files"]:
            assert len(meta.files) == entry["file_count"], entry["friendly_id"]
        else:
            assert len(meta.files) == 0, entry["friendly_id"]


def test_parse_http_url_extracts_uuid_and_fetches(tmp_path, monkeypatch):
    s = FakeSession()
    monkeypatch.setenv("CTF_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("CTF2_PRACTICE_GROUND_ID", "pg1")
    a = Ctf2Adapter(StoreSettings.from_env(), session=s)
    cid = "54fd6f56-4458-4b07-8729-ea429c289cd1"
    url = f"{BASE}/practice/pg1/challenges/{cid}/"
    s.add(url, status=200, json_data=_load("ctf2_pwn_PCHAL-2026-0062.json"))
    meta = a.parse(f"https://ctf2.dasctf.com/practice/pg1/challenges/{cid}")
    assert meta.challenge_id == cid
    assert s.requests[0][0] == "GET" and s.requests[0][1] == url


def test_parse_unknown_id_without_gid_raises(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)  # 无 CTF2_PRACTICE_GROUND_ID
    with pytest.raises(Exception, match="CTF2_PRACTICE_GROUND_ID"):
        a.parse("PCHAL-9999")


def test_parse_friendly_id_missing_index_hints_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("CTF_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("CTF2_PRACTICE_GROUND_ID", "pg1")
    a = Ctf2Adapter(StoreSettings.from_env(), session=FakeSession())
    with pytest.raises(Exception, match="challenge-sync"):
        a.parse("PCHAL-9999")  # 有 gid 但索引无此 friendly_id → 提示先建索引


# ===== download =====

DETAIL_URL = f"{BASE}/practice/pg1/challenges/c1/"
CDN = "https://ctf2-files.dasctf.com/ctf-files/uploads/f1.bin"


def _detail(file_id="f1", url=CDN):
    return {"data": {"files": [{"file_id": file_id, "file_name": "pwn1", "download_url": url}]}}


def test_download_uses_detail_download_url(tmp_path, monkeypatch):
    s = FakeSession()
    monkeypatch.setenv("CTF_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("CTF2_PRACTICE_GROUND_ID", "pg1")
    monkeypatch.setenv("CTF2_TOKEN", "tok")
    a = Ctf2Adapter(StoreSettings.from_env(), session=s)
    s.add(DETAIL_URL, status=200, json_data=_detail())
    s.add(CDN, status=200, content=b"DATA")
    assert a.download("f1", "c1") == b"DATA"
    # 详情带鉴权 + 浏览器头;CDN 直下不带任何凭证头(防 token 外泄给第三方域名)
    assert s.requests[0][2]["headers"]["Authorization"] == "Bearer tok"
    assert s.requests[0][2]["headers"].get("Origin")
    assert "Authorization" not in s.requests[1][2].get("headers", {})
    assert "Cookie" not in s.requests[1][2].get("headers", {})


def test_download_success_and_failure_record_events(tmp_path, monkeypatch):
    """download 成功/失败都进审计线(补缺口:附件下载曾是静默操作)。"""
    from opslog import attach, detach

    def _events(a, fn):
        seen = []
        sink = lambda kind, detail: seen.append((kind, detail))
        attach(sink)
        try:
            fn()
        finally:
            detach(sink)
        return seen

    s = FakeSession()
    monkeypatch.setenv("CTF_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("CTF2_PRACTICE_GROUND_ID", "pg1")
    a = Ctf2Adapter(StoreSettings.from_env(), session=s)
    s.add(DETAIL_URL, status=200, json_data=_detail())
    s.add(CDN, status=200, content=b"DATA")
    seen = _events(a, lambda: a.download("f1", "c1"))
    ok_ev = [d for k, d in seen if k == "adapter.download"]
    assert len(ok_ev) == 1 and ok_ev[0]["size"] == 4

    s2 = FakeSession()
    a2 = Ctf2Adapter(StoreSettings.from_env(), session=s2)
    s2.add(DETAIL_URL, status=401)
    seen2 = []
    sink2 = lambda kind, detail: seen2.append((kind, detail))
    attach(sink2)
    try:
        with pytest.raises(AuthError):
            a2.download("f1", "c1")
    finally:
        detach(sink2)
    fail_ev = [d for k, d in seen2 if k == "adapter.download_failed"]
    assert len(fail_ev) == 1
    assert "AuthError" in fail_ev[0]["error"]


def test_parse_not_found_records_event(tmp_path, monkeypatch):
    """parse 失败(索引无此 id)→ adapter.parse_failed 进审计线。"""
    from opslog import attach, detach

    s = FakeSession()
    monkeypatch.setenv("CTF_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("CTF2_PRACTICE_GROUND_ID", "pg1")
    a = Ctf2Adapter(StoreSettings.from_env(), session=s)
    seen = []
    sink = lambda kind, detail: seen.append((kind, detail))
    attach(sink)
    try:
        with pytest.raises(Exception, match="索引中无"):
            a.parse("nope")
    finally:
        detach(sink)
    fail_ev = [d for k, d in seen if k == "adapter.parse_failed"]
    assert len(fail_ev) == 1
    assert "索引中无" in fail_ev[0]["error"]


def test_download_session_relative_url_prefixes_origin_and_auths(tmp_path, monkeypatch):
    s = FakeSession()
    monkeypatch.setenv("CTF_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("CTF2_PRACTICE_GROUND_ID", "pg1")
    monkeypatch.setenv("CTF2_SESSION_TOKEN", "tok")
    a = Ctf2Adapter(StoreSettings.from_env(), session=s)
    origin = "https://ctf2.dasctf.com"
    session_url = f"{BASE}/practice/pg1/challenges/c1/files/f1/"
    s.add(DETAIL_URL, status=200, json_data={"data": {"files": [
        {"file_id": "f1", "file_name": "pwn1", "download_url": session_url[len(origin):]}]}})
    s.add(session_url, status=200, content=b"DATA")
    assert a.download("f1", "c1") == b"DATA"
    req = s.requests[-1]
    assert req[1] == session_url  # 相对 URL 已前缀 origin
    assert req[2]["headers"]["Authorization"] == "Bearer tok"  # 同源会话端点带凭证


def test_download_detail_401_raises_auth_error_no_cdn(tmp_path, monkeypatch):
    s = FakeSession()
    monkeypatch.setenv("CTF_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("CTF2_PRACTICE_GROUND_ID", "pg1")
    a = Ctf2Adapter(StoreSettings.from_env(), session=s)
    s.add(DETAIL_URL, status=401)
    with pytest.raises(AuthError):
        a.download("f1", "c1")
    assert len(s.requests) == 1  # 无 token 不触发续期,401 立即抛


def test_detail_401_refreshes_token_and_retries(tmp_path, monkeypatch):
    s = FakeSession()
    monkeypatch.setenv("CTF_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("CTF2_PRACTICE_GROUND_ID", "pg1")
    monkeypatch.setenv("CTF2_SESSION_TOKEN", "tok")
    a = Ctf2Adapter(StoreSettings.from_env(), session=s)
    refresh_url = f"{BASE}/auth/refresh/"
    s.seq(DETAIL_URL, [
        FakeResponse(401),
        FakeResponse(200, json_data=_detail()),
    ])
    s.add(refresh_url, status=200, json_data={"data": {"token": "new-token"}})
    s.add(CDN, status=200, content=b"DATA")
    assert a.download("f1", "c1") == b"DATA"
    # 401 → 续期 → 重试成功;内存与新 token 落回 config_adaptor.json
    assert a.settings.ctf2_session_token == "new-token"
    auth_get = [r for r in s.requests if r[0] == "GET" and r[1] == DETAIL_URL]
    assert auth_get[1][2]["headers"]["Authorization"] == "Bearer new-token"
    assert "Authorization" not in s.requests[-1][2].get("headers", {})
    import config_adaptor
    saved = json.loads((tmp_path / "config_adaptor.json").read_text(encoding="utf-8"))
    assert saved["CTF2_SESSION_TOKEN"] == "new-token"


def test_download_file_missing_in_detail_raises(tmp_path, monkeypatch):
    s = FakeSession()
    monkeypatch.setenv("CTF_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("CTF2_PRACTICE_GROUND_ID", "pg1")
    a = Ctf2Adapter(StoreSettings.from_env(), session=s)
    s.add(DETAIL_URL, status=200, json_data={"data": {"files": [{"file_id": "other", "download_url": CDN}]}})
    with pytest.raises(DownloadError):
        a.download("f1", "c1")  # 详情无 f1 且无 env 模板


def test_download_detail_404_falls_back_custom_template(tmp_path, monkeypatch):
    s = FakeSession()
    monkeypatch.setenv("CTF_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("CTF2_PRACTICE_GROUND_ID", "pg1")
    monkeypatch.setenv("CTF2_DOWNLOAD_URL_TEMPLATE", "{base_url}/files/{file_id}/download/")
    a = Ctf2Adapter(StoreSettings.from_env(), session=s)
    s.add(DETAIL_URL, status=404)
    s.add(f"{BASE}/files/f1/download/", status=200, content=b"T")
    assert a.download("f1", "c1") == b"T"
    assert s.requests[0][1] == DETAIL_URL
    assert s.requests[1][1] == f"{BASE}/files/f1/download/"


def test_download_no_gid_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("CTF_STORE_DIR", str(tmp_path))
    a = Ctf2Adapter(StoreSettings.from_env(), session=FakeSession())
    with pytest.raises(Exception, match="CTF2_PRACTICE_GROUND_ID"):
        a.download("f1", "c1")


def test_download_cookie_only_on_api_call(tmp_path, monkeypatch):
    s = FakeSession()
    monkeypatch.setenv("CTF_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("CTF2_PRACTICE_GROUND_ID", "pg1")
    monkeypatch.setenv("CTF2_COOKIE", "session=abc")
    a = Ctf2Adapter(StoreSettings.from_env(), session=s)
    s.add(DETAIL_URL, status=200, json_data=_detail())
    s.add(CDN, status=200, content=b"G")
    a.download("f1", "c1")
    assert s.requests[0][2]["headers"].get("Cookie") == "session=abc"
    assert "Cookie" not in s.requests[1][2].get("headers", {})


# ===== submit =====

SUBMIT_URL = f"{BASE}/practice/pg1/challenges/c1/submit/"


def _submit_adapter(tmp_path, monkeypatch, s):
    monkeypatch.setenv("CTF_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("CTF2_PRACTICE_GROUND_ID", "pg1")
    return Ctf2Adapter(StoreSettings.from_env(), session=s)


def _seed_challenge(a, cid="c1"):
    """FK:persist_flag 需 challenges 有行(真实流程先 ingest/sync)。"""
    a.store.upsert_challenge(ChallengeMeta(
        challenge_id=cid, platform="ctf2", friendly_id="F-" + cid,
        name="t", category="PWN", description="d",
        files=[FileRecord("f1", "pwn1")],
    ))


def test_submit_correct_persists_locally(tmp_path, monkeypatch):
    s = FakeSession()
    a = _submit_adapter(tmp_path, monkeypatch, s)
    _seed_challenge(a)
    s.add(SUBMIT_URL, status=200, json_data={"success": True, "data": {"is_correct": True}})
    res = a.submit("c1", "flag{x}")
    assert res.ok and res.correct is True
    assert s.requests[0][0] == "POST"
    assert s.requests[0][2]["json"] == {"flag": "flag{x}"}
    # 正确 → 落本地答案库 + 提交日志
    assert a.get_flag("c1")["flag"] == "flag{x}"
    logs = a.store.recent_submissions("c1")
    assert len(logs) == 1 and logs[0]["correct"] == 1


def test_submit_incorrect_flag_shape(tmp_path, monkeypatch):
    s = FakeSession()
    a = _submit_adapter(tmp_path, monkeypatch, s)
    s.add(SUBMIT_URL, status=200, json_data={
        "success": False, "error": {"code": "INCORRECT_FLAG"},
        "data": {"attempt": 2, "is_correct": False},
    })
    res = a.submit("c1", "flag{wrong}")
    assert res.ok and res.correct is False
    assert "剩余次数" in res.message
    assert a.store.recent_submissions("c1")[0]["verdict"] == "INCORRECT_FLAG"
    assert a.get_flag("c1") is None  # 错误不写答案库


def test_submit_verified_local_flag_shortcircuits(tmp_path, monkeypatch):
    """本地答案库已有已验证正确 flag → 直接本地比对,不再请求平台。"""
    s = FakeSession()
    a = _submit_adapter(tmp_path, monkeypatch, s)
    _seed_challenge(a)
    s.add(SUBMIT_URL, status=400, json_data={"success": False, "error": {"code": "ALREADY_SOLVED"}})
    a.persist_flag("c1", "flag{saved}", verified=True)
    ok = a.submit("c1", "flag{saved}")
    assert ok.ok and ok.correct is True and "本地比对:答案正确" in ok.message
    bad = a.submit("c1", "flag{other}")
    assert bad.ok and bad.correct is False and "本地比对:答案错误" in bad.message
    assert not s.requests  # 平台往返被跳过
    logs = a.store.recent_submissions("c1")
    assert len(logs) == 2 and all(r["verdict"] == "LOCAL_VERIFIED" for r in logs)


def test_submit_unverified_local_flag_still_hits_platform(tmp_path, monkeypatch):
    """未验证(verified=0)的本地 flag 不作权威,仍走平台提交。"""
    s = FakeSession()
    a = _submit_adapter(tmp_path, monkeypatch, s)
    _seed_challenge(a)
    a.store.upsert_flag("c1", "flag{rule}", source="flag_rules", verified=False)
    s.add(SUBMIT_URL, status=200, json_data={"success": True, "data": {"is_correct": True}})
    res = a.submit("c1", "flag{rule}")
    assert res.ok and res.correct is True
    assert len(s.requests) == 1
    assert a.store.recent_submissions("c1")[0]["verdict"] == "success"


def test_submit_already_solved_without_local_flag(tmp_path, monkeypatch):
    s = FakeSession()
    a = _submit_adapter(tmp_path, monkeypatch, s)
    s.add(SUBMIT_URL, status=400, json_data={"success": False, "error": {"code": "ALREADY_SOLVED"}})
    res = a.submit("c1", "flag{?}")
    assert res.ok and res.correct is None
    assert "无法比对" in res.message


def test_submit_logs_every_attempt(tmp_path, monkeypatch):
    s = FakeSession()
    a = _submit_adapter(tmp_path, monkeypatch, s)
    s.add(SUBMIT_URL, status=200, json_data={"success": False, "error": {"code": "INCORRECT_FLAG"}})
    a.submit("c1", "flag{1}")
    a.submit("c1", "flag{2}")
    a.submit("c1", "flag{1}")
    assert len(a.store.recent_submissions("c1")) == 3


def test_submit_401_raises_auth_error(tmp_path, monkeypatch):
    s = FakeSession()
    a = _submit_adapter(tmp_path, monkeypatch, s)
    s.add(SUBMIT_URL, status=401)
    with pytest.raises(AuthError):
        a.submit("c1", "flag{x}")


def test_submit_ambiguous_correctness_falls_through(tmp_path, monkeypatch):
    s = FakeSession()
    a = _submit_adapter(tmp_path, monkeypatch, s)
    s.add(SUBMIT_URL, status=200, json_data={"success": "false"})
    res = a.submit("c1", "flag{wrong}")
    assert res.ok and res.correct is False


def test_submit_risk_action_captcha_not_judged(tmp_path, monkeypatch):
    """风控验证码:本次提交未判定,不算错,也不落答案库。"""
    s = FakeSession()
    a = _submit_adapter(tmp_path, monkeypatch, s)
    _seed_challenge(a)
    s.add(SUBMIT_URL, status=400, json_data={
        "success": False, "error": {"code": "SUBMISSION_RISK_CHALLENGE_REQUIRED"},
        "data": {"risk_action": "challenge", "risk_challenge": {"id": "x", "image": "data:image/png;base64,.."}},
    })
    res = a.submit("c1", "flag{x}")
    assert res.ok is False and res.correct is None
    assert "验证码" in res.message
    log = a.store.recent_submissions("c1")[0]
    assert log["verdict"] == "SUBMISSION_RISK_CHALLENGE_REQUIRED" and log["correct"] is None
    assert a.get_flag("c1") is None  # 未判定,不写答案库
    # 提交请求带浏览器特征头(风控引擎按 UA/Accept 判定非浏览器流量)
    assert s.requests[0][2]["headers"]["User-Agent"].startswith("Mozilla/5.0")
    assert s.requests[0][2]["headers"]["Accept"] == "application/json"
    assert s.requests[0][2]["headers"].get("X-Skip-Global-Error-Message") == "true"


# ===== 靶机开/关(environment/start + DELETE target) =====

OPEN_START_URL = (
    "https://ctf2.dasctf.com/api/open/v1/user/practice/pg1/challenges/c1/environment/start/"
)
SESS_TARGET_URL = f"{BASE}/practice/pg1/challenges/c1/target/"


def _target_adapter(tmp_path, monkeypatch, s):
    monkeypatch.setenv("CTF_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("CTF2_PRACTICE_GROUND_ID", "pg1")
    monkeypatch.setenv("CTF2_API_KEY", "pat-xxx")
    return Ctf2Adapter(StoreSettings.from_env(), session=s)


def _ready_env():
    return {"success": True, "data": {
        "environment_id": "env-1", "status": "running", "access_ready": True,
        "access_type": "tcp", "access_url": "abc.tcp-ctf2.dasctf.com:9999",
        "access_urls": [{"type": "tcp", "url": "abc.tcp-ctf2.dasctf.com:9999", "nc_ssl": True}],
        "expires_at": "2026-08-18T22:00:00+08:00",
    }}


def test_parse_target_extracts_host_port_from_access_url():
    a = Ctf2Adapter.__new__(Ctf2Adapter)
    info = a._parse_target(_ready_env()["data"])
    assert info["host"] == "abc.tcp-ctf2.dasctf.com"
    assert info["port"] == 9999
    assert info["access_url"] == "abc.tcp-ctf2.dasctf.com:9999"
    assert info["environment_id"] == "env-1"
    assert info["access_urls"][0]["type"] == "tcp"


def test_start_target_polls_until_ready_writes_target(tmp_path, monkeypatch):
    s = FakeSession()
    a = _target_adapter(tmp_path, monkeypatch, s)
    _seed_challenge(a)
    s.add(OPEN_START_URL, status=200, json_data=_ready_env())
    info = a.start_target("c1")
    assert info["access_url"] == "abc.tcp-ctf2.dasctf.com:9999"
    assert info["host"] == "abc.tcp-ctf2.dasctf.com" and info["port"] == 9999
    # 走 open API + Bearer PAT 鉴权
    method, url, kw = s.requests[0]
    assert method == "POST" and url == OPEN_START_URL
    assert kw["headers"]["Authorization"] == "Bearer pat-xxx"
    # host:port 写回 challenges.target 供执行层读取
    assert a.store.get_challenge("c1")["target"] == "abc.tcp-ctf2.dasctf.com:9999"


def _container_meta():
    return ChallengeMeta(
        challenge_id="c1", platform="ctf2", friendly_id="F-c1",
        name="pwn", category="PWN", description="d",
        has_container=True, files=[],
    )


def test_materialize_auto_starts_target_for_container(tmp_path, monkeypatch):
    s = FakeSession()
    a = _target_adapter(tmp_path, monkeypatch, s)
    meta = _container_meta()
    a.store.upsert_challenge(meta)   # 真实 ingest 先落索引再物化
    s.add(OPEN_START_URL, status=200, json_data=_ready_env())
    dest = a._materialize(meta, tmp_path / "challenges" / "c1")
    # metadata.yml 写入 target(host:port) 供理解层/执行层读取
    md = yaml.safe_load((dest / "metadata.yml").read_text(encoding="utf-8"))
    assert md["target"] == "abc.tcp-ctf2.dasctf.com:9999"
    assert md["has_container"] is True
    assert md["access"]["nc_ssl"] is True   # 访问方式落盘,供理解层/执行层选连接方式
    # host:port 写回库
    assert a.store.get_challenge("c1")["target"] == "abc.tcp-ctf2.dasctf.com:9999"
    # 触发过 open API environment/start
    assert any(m == "POST" and u == OPEN_START_URL for m, u, _ in s.requests)


def test_materialize_skips_requery_when_meta_has_target_and_access(tmp_path, monkeypatch):
    """target 与 access 都就绪 → 不再请求开靶接口,access 原样写 metadata.yml。"""
    s = FakeSession()
    a = _target_adapter(tmp_path, monkeypatch, s)
    meta = _container_meta()
    meta.target = "static.example.com:31337"
    meta.access = {"access_type": "tcp", "nc_ssl": True}
    dest = a._materialize(meta, tmp_path / "challenges" / "c1")
    md = yaml.safe_load((dest / "metadata.yml").read_text(encoding="utf-8"))
    assert md["target"] == "static.example.com:31337"
    assert md["access"] == {"access_type": "tcp", "nc_ssl": True}
    assert s.requests == []


def test_materialize_backfills_access_when_target_present(tmp_path, monkeypatch):
    """已有 target 但缺 access(连接方式) → 幂等补查开靶接口,access 落 metadata.yml。

    平台把挑战端口用 TLS 转发器包裹(nc_ssl=true),access 是正确连接的必需信息,
    不能因为 target 已就绪就跳过。
    """
    s = FakeSession()
    a = _target_adapter(tmp_path, monkeypatch, s)
    meta = _container_meta()
    meta.target = "abc.tcp-ctf2.dasctf.com:9999"   # 已有 target → 不重复开靶
    s.add(OPEN_START_URL, status=200, json_data=_ready_env())
    dest = a._materialize(meta, tmp_path / "challenges" / "c1")
    md = yaml.safe_load((dest / "metadata.yml").read_text(encoding="utf-8"))
    assert md["target"] == "abc.tcp-ctf2.dasctf.com:9999"
    assert md["access"]["nc_ssl"] is True
    assert md["access"]["access_type"] == "tcp"
    assert md["access"]["access_urls"][0]["nc_ssl"] is True
    # 确有过一次开靶查询(幂等补 access)
    assert any(m == "POST" and u == OPEN_START_URL for m, u, _ in s.requests)


def test_materialize_auto_start_respects_config_flag(tmp_path, monkeypatch):
    s = FakeSession()
    a = _target_adapter(tmp_path, monkeypatch, s)
    a.settings.ctf2_auto_start_target = False   # CTF2_AUTO_START_TARGET=0
    dest = a._materialize(_container_meta(), tmp_path / "challenges" / "c1")
    md = yaml.safe_load((dest / "metadata.yml").read_text(encoding="utf-8"))
    assert "target" not in md
    assert s.requests == []


def test_start_target_polls_starting_then_ready(tmp_path, monkeypatch):
    s = FakeSession()
    a = _target_adapter(tmp_path, monkeypatch, s)
    s.add(OPEN_START_URL, status=200, json_data={
        "success": True, "data": {"environment_id": "env-1", "status": "starting", "access_ready": False},
    })
    # 从不 ready → 超时返回未就绪 dict,不抛
    info = a.start_target("c1", timeout=0.01)
    assert info.get("status") == "starting"
    assert info.get("access_url") is None


def test_start_target_without_pat_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("CTF_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("CTF2_PRACTICE_GROUND_ID", "pg1")
    a = Ctf2Adapter(StoreSettings.from_env(), session=FakeSession())
    with pytest.raises(AuthError, match="CTF2_API_KEY"):
        a.start_target("c1")


def test_stop_target_deletes_and_clears_target(tmp_path, monkeypatch):
    s = FakeSession()
    a = _target_adapter(tmp_path, monkeypatch, s)
    _seed_challenge(a)
    a.store.set_challenge_target("c1", "abc.tcp-ctf2.dasctf.com:9999")
    s.add(SESS_TARGET_URL, status=200, json_data={"success": True})
    r = a.stop_target("c1")
    assert r["ok"] is True
    assert s.requests[0][0] == "DELETE" and s.requests[0][1] == SESS_TARGET_URL
    assert a.store.get_challenge("c1")["target"] is None  # 关闭后清除


def test_stop_target_requires_confirmation(tmp_path, monkeypatch):
    s = FakeSession()
    a = _target_adapter(tmp_path, monkeypatch, s)
    r = a.stop_target("c1", confirmation=False)
    assert r["ok"] is False and "CONFIRMATION_REQUIRED" in r["message"]
    assert len(s.requests) == 0  # 未确认不发 DELETE


# ===== sync =====


def test_sync_paginates_and_upserts(tmp_path, monkeypatch):
    s = FakeSession()
    monkeypatch.setenv("CTF_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("CTF2_PRACTICE_GROUND_ID", "pg1")
    a = Ctf2Adapter(StoreSettings.from_env(), session=s)
    page1 = [_load("ctf2_pwn_PCHAL-2026-0062.json"), _load("ctf2_pwn_PCHAL-2026-0063.json")]
    page2 = [_load("ctf2_web_PCHAL-2026-0024.json")]
    # 真实响应形态: {data: {categories, data: [...], pagination}}
    s.add(f"{BASE}/practice/pg1/challenges/?page=1&page_size=100",
          status=200, json_data={"success": True, "data": {"data": page1, "pagination": {"total": 3}}})
    s.add(f"{BASE}/practice/pg1/challenges/?page=2&page_size=100",
          status=200, json_data={"success": True, "data": {"data": page2, "pagination": {"total": 3}}})
    r = a.sync_challenges()
    assert r["total"] == 3 and r["inserted"] == 3 and r["updated"] == 0
    assert a.store.get_challenge("PCHAL-2026-0063") is not None


def test_parse_friendly_id_resolves_from_index(tmp_path, monkeypatch):
    """sync 建索引后,friendly_id 走本地索引,不再拉网络(详情端点只收 UUID)。"""
    s = FakeSession()
    monkeypatch.setenv("CTF_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("CTF2_PRACTICE_GROUND_ID", "pg1")
    a = Ctf2Adapter(StoreSettings.from_env(), session=s)
    s.add(f"{BASE}/practice/pg1/challenges/?page=1&page_size=100",
          status=200, json_data={"data": {"data": [_load("ctf2_pwn_PCHAL-2026-0063.json")]}})
    a.sync_challenges()
    meta = a.parse("PCHAL-2026-0063")
    assert meta.challenge_id == "250f010a-fbfc-462a-aee0-4ab862ae735d"
    assert len(s.requests) == 1  # 只有 sync 的 list 请求,parse 未再拉网络


def test_sync_401_raises(tmp_path, monkeypatch):
    s = FakeSession()
    monkeypatch.setenv("CTF_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("CTF2_PRACTICE_GROUND_ID", "pg1")
    a = Ctf2Adapter(StoreSettings.from_env(), session=s)
    s.add(f"{BASE}/practice/pg1/challenges/?page=1&page_size=100", status=401)
    with pytest.raises(AuthError):
        a.sync_challenges()
