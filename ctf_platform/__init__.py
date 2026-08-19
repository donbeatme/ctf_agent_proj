"""真实平台适配器:ChallengeAdapter 基类 + Ctf2Adapter 实现。

换平台/靶场 = 新增 ChallengeAdapter 子类,主架构只依赖基类接口。
"""

from .base import (
    AdapterError,
    ChallengeAdapter,
    ChallengeMeta,
    FileRecord,
    SubmitResult,
)
from .config import StoreSettings
from .ctf2 import Ctf2Adapter
from .errors import AuthError, CacheIntegrityError, DownloadError, ParseError
from .storage import AttachmentCache, ChallengeStore, connect

__all__ = [
    "AdapterError",
    "AttachmentCache",
    "AuthError",
    "CacheIntegrityError",
    "ChallengeAdapter",
    "ChallengeMeta",
    "ChallengeStore",
    "Ctf2Adapter",
    "DownloadError",
    "FileRecord",
    "ParseError",
    "StoreSettings",
    "SubmitResult",
    "connect",
]
