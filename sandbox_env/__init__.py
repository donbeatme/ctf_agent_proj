"""沙箱环境管理器:类适配器的沙箱抽象(SandboxBackend)+ 门面(SandboxManager)。

主架构只依赖 SandboxManager;换沙箱后端 = 写新子类。凭证/配置走 env(CTF_SSH_*)。
"""

from .base import (
    SandboxBackend,
    SandboxManager,
    session_key_for,
    container_name_for,
    ExecOutcome,
)
from .config import SandboxSettings
from .errors import (
    SandboxError,
    SandboxUnavailableError,
    SandboxExecError,
    ToolInstallError,
)

__all__ = [
    "SandboxBackend",
    "SandboxManager",
    "SandboxSettings",
    "SandboxError",
    "SandboxUnavailableError",
    "SandboxExecError",
    "ToolInstallError",
    "session_key_for",
    "container_name_for",
    "ExecOutcome",
]
