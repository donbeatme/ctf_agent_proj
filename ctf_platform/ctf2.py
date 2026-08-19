"""ctf2 平台适配器(Ctf2Adapter):ChallengeAdapter 的实现子类。

换平台/靶场 = 写新子类,主架构只依赖基类接口。
- parse: ctf2 题目 JSON / 题目 URL / friendly_id|challenge_id(索引或拉取)
- download: 真实流程 = 详情 GET → files[].download_url;同源会话端点带 Bearer(自动续期),
  CDN(第三方域名)直下不带凭证头;相对 URL 前缀 origin;详情缺失时回退 env
  CTF2_DOWNLOAD_URL_TEMPLATE 模板
- submit: POST /practice/{gid}/challenges/{cid}/submit/,body {"flag": ...}
- sync_challenges: 分页拉全量索引
- 鉴权: CTF2_SESSION_TOKEN(Bearer JWT)/CTF2_COOKIE;凭证可经 CTF2_CONFIG_JSON 读取
"""

from __future__ import annotations

import base64
import json
import re
import time
from typing import Any

import config_adaptor

from opslog import emit

from .base import ChallengeAdapter, ChallengeMeta, SubmitResult
from .config import StoreSettings
from .errors import AdapterError, AuthError, DownloadError, ParseError
from .storage import FileRecord

# 与平台风控引擎对齐:无浏览器特征头会被判定为非浏览器流量并弹验证码(见 api.py 同款 UA)
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)

# token 剩余有效期小于该值时主动刷新(约 6 天,对齐 ctf2/api.py REFRESH_BEFORE)
_REFRESH_BEFORE = 6 * 24 * 3600


def _jwt_exp(token: str) -> int:
    """解析 JWT exp(unix 秒);解析失败返回 0(视为不主动刷新)。"""
    try:
        seg = token.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        return int(json.loads(base64.urlsafe_b64decode(seg)).get("exp") or 0)
    except Exception:
        return 0


class Ctf2Adapter(ChallengeAdapter):
    platform = "ctf2"

    def __init__(self, settings: StoreSettings, session=None):
        import requests  # lazy:缺 requests 时仅构造失败,不影响其它适配器

        super().__init__(settings)
        # 会话客户端不预设全局头:下载走 CDN download_url 时绝不能带上 Bearer/凭证
        self._session = session if session is not None else requests.Session()
        self.base_url = settings.ctf2_base_url.rstrip("/")
        self.practice_ground_id = settings.ctf2_practice_ground_id
        self.origin = settings.ctf2_origin

    # ---- 会话 token 自动续期 ----

    def _refresh_token(self) -> str:
        """POST /auth/refresh/ 换新 token,更新内存 settings 并写回 config_adaptor.json。"""
        resp = self._session.post(
            f"{self.base_url}/auth/refresh/",
            headers={"Authorization": f"Bearer {self.settings.ctf2_session_token}"},
            timeout=30,
        )
        if resp.status_code != 200:
            raise AuthError(f"会话 token 刷新失败 HTTP {resp.status_code}")
        data = resp.json()
        payload = data.get("data") if isinstance(data, dict) else {}
        new_token = payload.get("token") if isinstance(payload, dict) else None
        if not new_token:
            raise AuthError("会话 token 刷新失败:响应中无 token")
        self.settings.ctf2_session_token = str(new_token)
        config_adaptor.set("CTF2_SESSION_TOKEN", str(new_token))
        emit("adapter", "token_refreshed", status_code=resp.status_code)
        return str(new_token)

    def _auth_request(self, method: str, url: str, timeout: int = 30,
                      browser: bool = True, **kw):
        """会话 API 请求:token 临近到期主动刷新;401/403 刷新一次重试。

        刷新仍失败时返回原响应,由调用方既有的 401/403 → AuthError 判定兜底。
        """

        def _send():
            headers = self._auth_headers(browser=browser)
            if method == "GET":
                return self._session.get(url, headers=headers, timeout=timeout, **kw)
            if method == "POST":
                return self._session.post(url, headers=headers, timeout=timeout, **kw)
            if method == "DELETE":
                return self._session.delete(url, headers=headers, timeout=timeout, **kw)
            return self._session.request(method, url, headers=headers, timeout=timeout, **kw)

        token = self.settings.ctf2_session_token
        if token:
            exp = _jwt_exp(token)
            if exp and exp - time.time() < _REFRESH_BEFORE:
                try:
                    self._refresh_token()
                except AuthError:
                    pass  # 刷新失败继续用旧 token,请求层再 401 判定
        resp = _send()
        if resp.status_code in (401, 403) and self.settings.ctf2_session_token:
            try:
                self._refresh_token()
            except AuthError:
                return resp  # 刷新失败,交回调用方报 401
            resp = _send()
        return resp

    # ---- 鉴权 ----

    def _auth_headers(self, browser: bool = False) -> dict:
        """会话 API 请求头。browser=True 追加浏览器特征头,规避平台风控验证码。"""
        h: dict = {}
        if self.settings.ctf2_session_token:
            h["Authorization"] = f"Bearer {self.settings.ctf2_session_token}"
        elif self.settings.ctf2_cookie:
            h["Cookie"] = self.settings.ctf2_cookie
        if browser:
            h["User-Agent"] = _UA
            h["Accept"] = "application/json"
            h["Origin"] = self.origin
            h["Referer"] = self.origin + "/dashboard/practice"
            h["X-Skip-Global-Error-Message"] = "true"
        return h

    # ---- 能力 1: parse ----

    def parse(self, source) -> ChallengeMeta:
        if isinstance(source, dict):
            return self._parse_json(source)
        s = str(source).strip()
        if s.startswith("http"):
            ident = self._extract_id(s)
        else:
            ident = s
        row = self.store.get_challenge(ident)
        if row:
            return self._meta_from_row(row)
        if not _UUID.match(ident) and self.practice_ground_id:
            raise AdapterError(
                f"索引中无 '{ident}'——friendly_id 需先运行 challenge-sync 建索引,"
                "或改用题目 URL/UUID"
            )
        return self._fetch_challenge(ident)

    def _extract_id(self, url: str) -> str:
        m = _UUID.search(url)
        if m:
            return m.group(0)
        segs = [x for x in url.rstrip("/").split("/") if x]
        return segs[-1] if segs else url

    def _meta_from_row(self, row: dict) -> ChallengeMeta:
        files = [
            FileRecord(
                file_id=f["file_id"], file_name=f["file_name"], file_size=f["file_size"],
                file_md5=f["file_md5"], file_type=f["file_type"], path=f["path"],
            )
            for f in self.store.files_for(row["challenge_id"])
        ]
        return ChallengeMeta(
            challenge_id=row["challenge_id"], platform=self.platform,
            friendly_id=row["friendly_id"], practice_ground_id=row["practice_ground_id"],
            name=row["name"], category=row["category"], difficulty=row["difficulty"],
            description=row["description"], points=row["points"],
            has_container=bool(row["has_container"]), target=row["target"],
            solve_count=row["solve_count"] or 0, is_solved=bool(row["is_solved"]),
            summary=row["summary"], challenge_type=row["challenge_type"], files=files,
        )

    def _fetch_challenge_detail(self, ident: str) -> dict:
        """GET 题目详情,解包 {data: {...}} 后返回原始 dict(files 含 download_url)。"""
        gid = self.practice_ground_id
        if not gid:
            raise AdapterError("CTF2_PRACTICE_GROUND_ID 未配置,无法从平台拉取题目")
        url = f"{self.base_url}/practice/{gid}/challenges/{ident}/"
        resp = self._auth_request("GET", url)
        if resp.status_code in (401, 403):
            raise AuthError(
                f"鉴权失败(HTTP {resp.status_code})——需要 CTF2_TOKEN 或 CTF2_COOKIE"
            )
        if resp.status_code != 200:
            raise DownloadError(f"拉取题目失败 HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        if not isinstance(data, dict):
            raise ParseError(f"拉取题目响应结构异常: {str(data)[:200]}")
        return data

    def _fetch_challenge(self, ident: str) -> ChallengeMeta:
        return self._parse_json(self._fetch_challenge_detail(ident))

    def _parse_json(self, d: dict) -> ChallengeMeta:
        cid = str(d.get("id") or d.get("challenge_id") or "").strip()
        friendly = str(d.get("friendly_id") or cid).strip()
        cat = d.get("category")
        files: list[FileRecord] = []
        for f in d.get("files") or []:
            if not isinstance(f, dict):
                continue
            fid = f.get("file_id")
            if not fid:
                continue
            name = str(f.get("file_name") or f.get("path") or fid)
            files.append(
                FileRecord(
                    file_id=str(fid), file_name=name, file_size=f.get("file_size"),
                    file_md5=f.get("file_md5"), file_type=f.get("file_type"),
                    path=f.get("path") or f.get("file_name"),
                )
            )
        return ChallengeMeta(
            challenge_id=cid, platform=self.platform, friendly_id=friendly,
            practice_ground_id=d.get("practice_ground_id") or self.practice_ground_id,
            name=str(d.get("name") or cid), category=cat, difficulty=d.get("difficulty"),
            description=d.get("description"), points=d.get("points"),
            has_container=bool(d.get("has_container")), target=d.get("target"),
            solve_count=d.get("solve_count") or 0, is_solved=bool(d.get("is_solved")),
            summary=None, challenge_type=("ctf-" + str(cat).strip().lower()) if cat else None,
            files=files, extra=d,
        )

    # ---- 能力 2: download(真实流程: 详情 → download_url CDN 直下,无需鉴权) ----

    def download(self, file_id: str, challenge_id: str) -> bytes:
        """详情 → download_url 直下。相对 URL 前缀 origin;同源会话端点经 _auth_request
        带 Bearer(可自动续期);CDN(第三方域名)绝不带凭证头,防 token 外泄。"""
        url = self._resolve_download_url(file_id, challenge_id)
        if url.startswith("/"):
            url = self.origin.rstrip("/") + url
        if self._same_origin(url, self.origin):
            resp = self._auth_request("GET", url, timeout=120)
        else:
            resp = self._session.get(url, timeout=120)  # CDN:无凭证头
        if resp.status_code in (401, 403):
            raise AuthError(
                f"鉴权失败(HTTP {resp.status_code})——需要 CTF2_TOKEN 或 CTF2_COOKIE。URL: {url}"
            )
        if resp.status_code != 200:
            raise DownloadError(f"附件下载失败 HTTP {resp.status_code}: {url}")
        return resp.content

    @staticmethod
    def _same_origin(url: str, base: str) -> bool:
        """URL 与 base 同 host(判定会话端点 vs CDN)。"""
        from urllib.parse import urlparse

        return urlparse(url).netloc == urlparse(base).netloc

    def _resolve_download_url(self, file_id: str, challenge_id: str) -> str:
        """优先详情 download_url;详情 404 或缺失该附件时回退自定义 URL 模板。"""
        try:
            detail = self._fetch_challenge_detail(challenge_id)
            for f in (detail or {}).get("files") or []:
                if isinstance(f, dict) and f.get("file_id") == file_id and f.get("download_url"):
                    return f["download_url"]
        except DownloadError:
            pass  # 详情拉取失败 → 模板兜底
        urls = self._download_urls(file_id, challenge_id)
        if urls:
            return urls[0]
        raise DownloadError(
            f"challenge {challenge_id} 未提供附件 {file_id} 的 download_url"
        )

    def _download_urls(self, file_id: str, challenge_id: str) -> list[str]:
        """自定义模板兜底(仅 env CTF2_DOWNLOAD_URL_TEMPLATE 显式配置)。"""
        base = self.base_url
        gid = self.practice_ground_id
        out = []
        for t in self.settings.ctf2_download_url_templates:
            if "{practice_ground_id}" in t and not gid:
                continue
            out.append(
                t.format(
                    base_url=base, file_id=file_id, challenge_id=challenge_id,
                    practice_ground_id=gid or "", is_private="false",
                )
            )
        return out

    # ---- 能力 3: submit ----

    def submit(self, challenge_id: str, flag: str,
               provenance: dict | None = None) -> SubmitResult:
        """提交验证,两档:

        1. 本地判定(见 base._local_verify):静态题走答案库串比对;动态题(has_container)
           走已验证 procedure 对当前实例重跑推导——不请求平台(平台对已解题不再判分)。
        2. 否则走平台真实语义:success→正确;动态题落已验证 procedure(T1,provenance 给出
           则带 verifier/trace,否则占位),静态题落本地答案库;INCORRECT_FLAG→错误;
           ALREADY_SOLVED→平台不再判分,静态题用本地 flag 比对,动态题不信任过期存串。
        每次提交都落本地 submissions 日志。"""
        local = self._local_verify(challenge_id, flag)
        if local is not None:
            return local
        gid = self.practice_ground_id
        if not gid:
            raise AdapterError("CTF2_PRACTICE_GROUND_ID 未配置,无法提交")
        if self.settings.ctf2_submit_url_template:
            url = self.settings.ctf2_submit_url_template.format(
                base_url=self.base_url, challenge_id=challenge_id, practice_ground_id=gid
            )
        else:
            url = f"{self.base_url}/practice/{gid}/challenges/{challenge_id}/submit/"
        try:
            resp = self._auth_request("POST", url, json={"flag": flag})
        except Exception as e:  # 网络/超时等 → 可重试
            emit("adapter", "submit", challenge_id=challenge_id, verdict="request_error",
                 correct=None, ok=False)
            return SubmitResult(ok=False, correct=None, message=f"请求异常: {e}")
        if resp.status_code in (401, 403):
            raise AuthError(
                f"鉴权失败(HTTP {resp.status_code})——需要 CTF2_TOKEN 或 CTF2_COOKIE"
            )
        data: Any = {}
        try:
            data = resp.json()
        except Exception:
            data = {}
        code = (
            str(((data or {}).get("error") or {}).get("code") or "")
            if isinstance(data, dict)
            else ""
        )

        # 已解决: 平台不再判分 → 本地基准比对
        if code == "ALREADY_SOLVED":
            verdict = "ALREADY_SOLVED"
            ch = self.store.get_challenge(challenge_id) or {}
            if ch.get("has_container"):
                # 动态题:存串是旧实例的,不可信;已验证过程在 _local_verify 已试过 → 无法判定
                self.store.log_submission(challenge_id, flag, verdict=verdict,
                                          correct=None,
                                          message="该题已解决且为动态 flag,无已验证过程可推导,无法本地判定")
                emit("adapter", "submit", challenge_id=challenge_id, verdict=verdict,
                     correct=None, ok=True)
                return SubmitResult(
                    ok=True, correct=None,
                    message="该题已解决且为动态 flag,无已验证过程可推导,无法本地判定",
                )
            row = self.store.get_flag(challenge_id)
            if row:
                ok = row["flag"] == flag
                self.store.log_submission(
                    challenge_id, flag, verdict=verdict, correct=ok,
                    message=("该题已解决,平台不再判分;本地比对:正确" if ok else "该题已解决,本地比对:答案错误"),
                )
                emit("adapter", "submit", challenge_id=challenge_id, verdict=verdict,
                     correct=ok, ok=True)
                return SubmitResult(
                    ok=True, correct=ok,
                    message="该题已解决,平台不再判分;本地比对:答案正确"
                    if ok else "该题已解决,平台不再判分;本地比对:答案错误",
                )
            self.store.log_submission(challenge_id, flag, verdict=verdict,
                                     correct=None, message="本地无此题的 flag,无法比对")
            emit("adapter", "submit", challenge_id=challenge_id, verdict=verdict,
                 correct=None, ok=True)
            return SubmitResult(
                ok=True, correct=None,
                message="该题已解决,平台不再判分;本地答案库无此题 flag,无法比对",
            )

        # 正确: 落本地答案库(源=verified_submission)
        sv = data.get("success") if isinstance(data, dict) else None
        success = sv if isinstance(sv, bool) else (
            str(sv).strip().lower() in ("1", "true", "yes") if sv is not None else None
        )
        if success is True:
            ch = self.store.get_challenge(challenge_id) or {}
            if ch.get("has_container"):
                # 动态题: 落已验证 procedure(T1);有 provenance 带 verifier/trace,否则占位
                if provenance:
                    self.record_procedure(
                        challenge_id, method="procedure",
                        verifier_path=provenance.get("verifier"),
                        trace=provenance.get("trace"),
                        flag=flag,
                        flag_format=provenance.get("flag_format"),
                        platform_verified=True,
                    )
                else:
                    self.record_procedure(challenge_id, method="procedure", flag=flag,
                                          platform_verified=True)
            else:
                self.persist_flag(challenge_id, flag, verified=True, source="verified_submission")
            self.store.log_submission(challenge_id, flag, verdict="success", correct=True,
                                      message="提交成功,答案正确,已写入本地答案库")
            emit("adapter", "submit", challenge_id=challenge_id, verdict="success",
                 correct=True, ok=True)
            return SubmitResult(ok=True, correct=True,
                                message="提交成功,答案正确,已写入本地答案库")

        # 错误
        if code == "INCORRECT_FLAG":
            attempt = int((data.get("data") or {}).get("attempt") or 0)
            max_attempts = self._max_attempts(challenge_id)
            remaining = "不限" if max_attempts in (0, None) else max(max_attempts - attempt, 0)
            message = f"答案错误,已尝试第 {attempt} 次,剩余次数 {remaining}"
            self.store.log_submission(challenge_id, flag, verdict="INCORRECT_FLAG",
                                      correct=False, message=message)
            emit("adapter", "submit", challenge_id=challenge_id, verdict="INCORRECT_FLAG",
                 correct=False, ok=True)
            return SubmitResult(ok=True, correct=False, message=message)

        # 风控验证码:提交未判定,不能当成答案错误
        if code == "SUBMISSION_RISK_CHALLENGE_REQUIRED":
            message = "平台风控验证码(SUBMISSION_RISK_CHALLENGE_REQUIRED),本次提交未判定,请稍后重试或人工过验证"
            self.store.log_submission(challenge_id, flag, verdict=code,
                                      correct=None, message=message)
            emit("adapter", "submit", challenge_id=challenge_id, verdict=code,
                 correct=None, ok=False)
            return SubmitResult(ok=False, correct=None, message=message)

        # 未知结构(其它平台兼容): 宽泛解析
        correct: bool | None = None
        if isinstance(data, dict):
            for key in ("correct", "is_correct", "success", "accepted", "result"):
                v = data.get(key)
                if v is None:
                    continue
                if isinstance(v, bool):
                    correct = v
                elif str(v).strip().lower() in ("1", "true", "yes", "pass", "accepted"):
                    correct = True
                elif str(v).strip().lower() in ("0", "false", "no", "fail", "rejected"):
                    correct = False
                else:
                    continue
                break
        message = str(((data or {}).get("message") or (data or {}).get("detail") or ""))
        if not message:
            message = resp.text[:200]
        self.store.log_submission(challenge_id, flag, verdict=code or None,
                                  correct=correct, message=message[:200])
        emit("adapter", "submit", challenge_id=challenge_id, verdict=code or None,
             correct=correct, ok=resp.status_code == 200)
        return SubmitResult(ok=resp.status_code == 200, correct=correct, message=message)

    # ---- 能力: 靶机开/关(动态容器) ----

    def _open_api_base(self) -> str:
        """open API 基址:派生自 origin(/api/open/v1),独立于会话 /api/v1。"""
        return f"{self.origin.rstrip('/')}/api/open/v1"

    def _open_headers(self) -> dict:
        """open API 鉴权头:Bearer PAT(个人访问令牌),缺则抛 AuthError。"""
        key = self.settings.ctf2_api_key
        if not key:
            raise AuthError(
                "开靶机需要 open API 令牌:CTF2_API_KEY(PAT,见平台 开发者→Open API 页签发)"
            )
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def start_target(self, challenge_id: str, timeout: float = 120) -> dict:
        """开靶机:POST open API environment/start → 轮询到 access_ready 返回访问地址。

        平台容器异步启动:每次 POST 幂等返回当前状态(starting/running/...);
        轮询直至 access_url 就绪。返回 {host, port, access_url, access_urls,
        environment_id, status, expires_at};host:port 写回 challenges.target。
        """
        gid = self.practice_ground_id
        if not gid:
            raise AdapterError("CTF2_PRACTICE_GROUND_ID 未配置,无法开靶机")
        url = (
            f"{self._open_api_base()}/user/practice/{gid}"
            f"/challenges/{challenge_id}/environment/start/"
        )
        deadline = time.time() + timeout
        data: dict = {}
        while time.time() < deadline:
            resp = self._session.post(url, json={}, headers=self._open_headers(), timeout=60)
            if resp.status_code in (401, 403):
                raise AuthError(
                    f"开靶机鉴权失败(HTTP {resp.status_code})——PAT 无效或缺 environment:write 权限"
                )
            if resp.status_code != 200:
                raise DownloadError(f"开靶机失败 HTTP {resp.status_code}: {resp.text[:200]}")
            payload = resp.json()
            body = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(body, dict):
                raise DownloadError(f"开靶机响应结构异常: {str(payload)[:200]}")
            data = body
            if body.get("access_ready"):
                break
            time.sleep(3)
        info = self._parse_target(data)
        host, port = info.get("host"), info.get("port")
        self.store.set_challenge_target(
            challenge_id, f"{host}:{port}" if host and port else None
        )
        emit("adapter", "target_started", challenge_id=challenge_id,
             status=data.get("status"), host=host, port=port,
             environment_id=data.get("environment_id"),
             ok=bool(info.get("access_url")))
        return info

    def stop_target(self, challenge_id: str, confirmation: bool = True) -> dict:
        """关靶机:DELETE 会话 API /practice/{gid}/challenges/{cid}/target/。

        confirmation 语义同平台:必须为 true(CLI 侧 --yes 确认)才发删除。
        """
        if confirmation is not True:
            return {"ok": False, "message": "CONFIRMATION_REQUIRED: 关靶机需确认"}
        gid = self.practice_ground_id
        if not gid:
            raise AdapterError("CTF2_PRACTICE_GROUND_ID 未配置,无法关靶机")
        url = f"{self.base_url}/practice/{gid}/challenges/{challenge_id}/target/"
        resp = self._auth_request("DELETE", url, timeout=60)
        if resp.status_code in (401, 403):
            raise AuthError(
                f"鉴权失败(HTTP {resp.status_code})——需要 CTF2_TOKEN 或 CTF2_COOKIE"
            )
        self.store.set_challenge_target(challenge_id, None)
        emit("adapter", "target_stopped", challenge_id=challenge_id,
             status_code=resp.status_code)
        if resp.status_code == 200:
            return {"ok": True, "message": "靶机已关闭"}
        return {"ok": False, "status_code": resp.status_code,
                "message": resp.text[:200]}

    def _materialize(self, meta: ChallengeMeta, dest_dir=None) -> Path:
        """含容器题自动开靶机:host:port 写 metadata.yml target,访问方式(含 nc_ssl)写 access。

        target 已就绪但缺 access 时也幂等补查(开靶接口返回当前环境状态),
        nc_ssl=true 表示端口被平台 TLS 转发器包裹,供理解层/执行层选对连接方式。
        """
        if meta.has_container and self.settings.ctf2_auto_start_target:
            try:
                if not meta.access or not meta.target:
                    info = self.start_target(meta.challenge_id)
                    host = info.get("host") or ""
                    port = info.get("port")
                    if host and port:
                        meta.target = f"{host}:{port}"
                        self.store.set_challenge_target(meta.challenge_id, meta.target)
                    meta.access = {
                        k: info[k] for k in (
                            "access_type", "access_url", "access_urls", "nc_ssl",
                        ) if info.get(k) is not None
                    } or None
            except (AdapterError, AuthError) as e:
                emit("adapter", "target_auto_start_failed", challenge_id=meta.challenge_id,
                     error=f"{type(e).__name__}: {e}")
        return super()._materialize(meta, dest_dir)

    @staticmethod
    def _parse_target(data: Any) -> dict:
        """把平台 target/environment 响应归一为 {host, port, access_url, ...}。

        兼容三种形态:access_url(h:port / https://...)、{host|ip|hostname|domain, port}、
        address("h:port")。access_urls 原样透传供执行层选 nc/ssh。
        """
        if not isinstance(data, dict):
            return {"raw": data}
        url = data.get("access_url") or data.get("target")
        host = data.get("host") or data.get("ip") or data.get("hostname") or data.get("domain")
        port = data.get("port")
        if url and not host:
            # access_url 可能是 host:port 或完整 URL
            try:
                from urllib.parse import urlparse

                parsed = urlparse(str(url))
                if parsed.hostname:
                    host = parsed.hostname
                if parsed.port:
                    port = parsed.port
            except (ValueError, AttributeError):
                pass
            if not host:
                h, _, p = str(url).rpartition(":")
                if p.isdigit():
                    host, port = h or None, int(p)
        info: dict = {"raw": data}
        if host is not None:
            info["host"] = str(host)
        if port is not None:
            try:
                info["port"] = int(port)
            except (TypeError, ValueError):
                pass
        for k in ("access_url", "access_urls", "access_type", "access_ready",
                  "environment_id", "status", "expires_at", "created_at", "nc_ssl"):
            if data.get(k) is not None:
                info[k] = data[k]
        if info.get("nc_ssl") is None and isinstance(info.get("access_urls"), list):
            # nc_ssl 也可能只出现在 access_urls 条目里(平台两种形态都返)
            info["nc_ssl"] = any(
                isinstance(u, dict) and bool(u.get("nc_ssl"))
                for u in info["access_urls"]
            )
        return info

    def _max_attempts(self, challenge_id: str) -> int:
        """读本地索引的 extra_json.max_attempts(缺省 0=不限),不额外发请求。"""
        row = self.store.get_challenge(challenge_id)
        if not row:
            return 0
        try:
            extra = json.loads(row.get("extra_json") or "{}")
        except (TypeError, ValueError):
            return 0
        try:
            return int(extra.get("max_attempts") or 0)
        except (TypeError, ValueError):
            return 0

    # ---- 能力: 同步索引 ----

    def sync_challenges(self, practice_ground_id: str | None = None) -> dict:
        gid = practice_ground_id or self.practice_ground_id
        if not gid:
            raise AdapterError("CTF2_PRACTICE_GROUND_ID 未配置")
        page = 1
        items_all: list[dict] = []
        while True:
            url = (
                f"{self.base_url}/practice/{gid}/challenges/"
                f"?page={page}&page_size={self.settings.ctf2_list_page_size}"
            )
            resp = self._auth_request("GET", url)
            if resp.status_code in (401, 403):
                raise AuthError(
                    f"鉴权失败(HTTP {resp.status_code})——需要 CTF2_TOKEN 或 CTF2_COOKIE"
                )
            if resp.status_code != 200:
                raise DownloadError(f"列表拉取失败 HTTP {resp.status_code}: {resp.text[:200]}")
            payload = resp.json()
            items = self._extract_items(payload)
            if not items:
                break
            items_all.extend(items)
            total = self._extract_total(payload)
            if total is not None:
                if len(items_all) >= total:
                    break
            elif len(items) < self.settings.ctf2_list_page_size:
                break
            page += 1
        inserted = updated = 0
        for item in items_all:
            if not isinstance(item, dict):
                continue
            meta = self._parse_json(item)
            if not meta.challenge_id:
                continue
            r = self.store.upsert_challenge(meta)
            self.store.upsert_challenge_files(meta.challenge_id, meta.files)
            inserted += r == "insert"
            updated += r == "update"
        emit("adapter", "sync", practice_ground_id=gid, total=len(items_all),
             inserted=inserted, updated=updated)
        return {"total": len(items_all), "inserted": inserted, "updated": updated}

    @staticmethod
    def _extract_items(data: Any) -> list:
        """兼容多种分页容器:data/items/results/challenges/records/list,
        以及真实 ctf2 形态 {data: {categories, data: [...], pagination}}。"""
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for k in ("data", "items", "results", "challenges", "records"):
                if isinstance(data.get(k), list):
                    return data[k]
            inner = data.get("data")
            if isinstance(inner, dict):
                for k in ("data", "list", "items", "results"):
                    if isinstance(inner.get(k), list):
                        return inner[k]
            if isinstance(data.get("list"), list):
                return data["list"]
        return []

    @staticmethod
    def _extract_total(data: Any) -> int | None:
        """分页总数:顶层 count/total,或真实形态 data.pagination.total。"""
        if not isinstance(data, dict):
            return None
        for k in ("count", "total"):
            v = data.get(k)
            if isinstance(v, int):
                return v
        inner = data.get("data")
        if isinstance(inner, dict):
            pg = inner.get("pagination")
            if isinstance(pg, dict) and isinstance(pg.get("total"), int):
                return pg["total"]
        return None
