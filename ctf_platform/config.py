"""适配器配置。敏感凭证在 config_adaptor(env 优先,config_adaptor.json 兜底,
CTF2_CONFIG_JSON 外部文件兼容),与主 config(model_config)分开。"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from config_adaptor import get as _cfg

_GIB = 1024 * 1024 * 1024


def _int(name: str, default: int) -> int:
    value = _cfg(name)
    if value is None or value == "":
        return default
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _csv(name: str) -> list[str]:
    value = _cfg(name)
    if not value:
        return []
    return [t.strip() for t in str(value).split(",") if t.strip()]


def _bool(name: str, default: bool) -> bool:
    value = _cfg(name)
    if value is None or value == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class StoreSettings:
    store_dir: Path
    db_path: Path
    cache_dir: Path          # 附件 blob 根目录(扁平两级分片)
    challenges_dir: Path     # 物化 challenge 目录根
    cache_bytes: int
    ctf2_base_url: str
    ctf2_session_token: Optional[str]   # 网页登录态 JWT(Bearer)
    ctf2_cookie: Optional[str]
    ctf2_api_key: Optional[str]         # 个人访问令牌 PAT(open API,环境开/关用)
    ctf2_origin: str
    ctf2_practice_ground_id: Optional[str]
    ctf2_download_url_templates: list[str]
    ctf2_submit_url_template: Optional[str]
    ctf2_list_page_size: int
    ctf2_auto_start_target: bool   # 物化含容器题时自动开靶机(metadata.yml 写 target)

    @classmethod
    def from_env(cls) -> "StoreSettings":
        store_dir = Path(str(_cfg("CTF_STORE_DIR", "./data")))
        # 适配器全部走会话 API(/api/v1)。源项目惯例:CTF2_SESSION_BASE 是会话 base,
        # CTF2_BASE_URL 可能是 open API(/api/open/v1);故会话 base 优先取 SESSION_BASE,
        # 旧布局只有 CTF2_BASE_URL(=会话 base)时兜底用它。
        base_url = (
            _cfg("CTF2_SESSION_BASE")
            or _cfg("CTF2_BASE_URL")
            or "https://ctf2.dasctf.com/api/v1"
        )
        return cls(
            store_dir=store_dir,
            db_path=store_dir / "ctf_platform.db",
            cache_dir=store_dir / "cache" / "blobs",
            challenges_dir=store_dir / "challenges",
            cache_bytes=_int("CTF_ATTACHMENT_CACHE_BYTES", 2 * _GIB),
            ctf2_base_url=str(base_url).rstrip("/"),
            ctf2_session_token=(
                _cfg("CTF2_SESSION_TOKEN")
                or _cfg("CTF2_TOKEN")
                or None
            ),
            ctf2_cookie=_cfg("CTF2_COOKIE") or None,
            ctf2_api_key=_cfg("CTF2_API_KEY") or None,
            ctf2_origin=(
                _cfg("CTF2_ORIGIN") or "https://ctf2.dasctf.com"
            ),
            ctf2_practice_ground_id=_cfg("CTF2_PRACTICE_GROUND_ID") or None,
            ctf2_download_url_templates=_csv("CTF2_DOWNLOAD_URL_TEMPLATE"),
            ctf2_submit_url_template=_cfg("CTF2_SUBMIT_URL_TEMPLATE") or None,
            ctf2_list_page_size=max(1, _int("CTF2_LIST_PAGE_SIZE", 100)),
            ctf2_auto_start_target=_bool("CTF2_AUTO_START_TARGET", True),
        )
