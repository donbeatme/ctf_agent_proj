"""沙箱环境配置。敏感凭据在 config_sandbox(env 优先,config_sandbox.json 兜底),
与主 config(model_config)分开。"""

from dataclasses import dataclass
from typing import Optional

from config_sandbox import get as _cfg


def _bool(name: str, default: bool) -> bool:
    value = _cfg(name)
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class SandboxSettings:
    backend: str = "ssh"                       # 本期只实现 ssh;预留 shared|ephemeral
    container_model: str = "per_challenge"     # 一题一个持久容器
    image: str = "ctf-sandbox:latest"
    ssh_host: Optional[str] = None
    ssh_user: str = "root"
    ssh_password: str = ""
    ssh_workdir: str = "/root/ctf"
    install_auto: bool = True                  # exec 前自动安装缺失依赖(进会话容器,持久)
    keep_container: bool = True                # 解完是否保留容器(便于复查)

    @classmethod
    def from_env(cls) -> "SandboxSettings":
        return cls(
            backend=str(_cfg("CTF_SANDBOX_BACKEND") or "ssh"),
            container_model=str(_cfg("CTF_SANDBOX_CONTAINER_MODEL") or "per_challenge"),
            image=str(_cfg("CTF_SANDBOX_IMAGE") or "ctf-sandbox:latest"),
            ssh_host=_cfg("CTF_SSH_HOST") or None,
            ssh_user=str(_cfg("CTF_SSH_USER") or "root"),
            ssh_password=str(_cfg("CTF_SSH_PASSWORD") or ""),
            ssh_workdir=str(_cfg("CTF_SSH_WORKDIR") or "/root/ctf"),
            install_auto=_bool("CTF_SANDBOX_INSTALL_AUTO", True),
            keep_container=_bool("CTF_SANDBOX_KEEP_CONTAINER", True),
        )

    @property
    def ssh_configured(self) -> bool:
        return bool(self.ssh_host)
