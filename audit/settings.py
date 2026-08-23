"""环境配置；统一走 model_config(环境变量优先,model_config.json 兜底),不再自读 .env。"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from model_config import get as _cfg


def _cfg_bool(name: str, default: bool = False) -> bool:
    value = _cfg(name)
    if value is None or value == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    mode: str
    data_dir: Path
    langsmith_enabled: bool
    llm_api_key: Optional[str]      # LLM_* 优先,DEEPSEEK_* 兜底(兼容旧命名)
    llm_base_url: str
    llm_model: str
    ragflow_enabled: bool
    ragflow_api_key: Optional[str]
    ragflow_base_url: str
    ragflow_dataset_name: str
    ragflow_timeout_seconds: float = 30.0
    experience_search_limit: int = 5
    ragflow_observability: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        mode = str(_cfg("CTF_AUDIT_MODE", "offline")).strip().lower()
        if mode not in {"offline", "online"}:
            raise ValueError("CTF_AUDIT_MODE must be offline or online, got: %s" % mode)
        return cls(
            mode=mode,
            data_dir=Path(str(_cfg("CTF_AUDIT_DATA_DIR", "./data"))),
            langsmith_enabled=_cfg_bool("LANGSMITH_TRACING") and bool(_cfg("LANGSMITH_API_KEY")),
            llm_api_key=_cfg("LLM_API_KEY") or _cfg("DEEPSEEK_API_KEY") or None,
            llm_base_url=_cfg("LLM_BASE_URL") or _cfg("DEEPSEEK_BASE_URL") or "https://api.deepseek.com",
            llm_model=_cfg("LLM_MODEL") or _cfg("DEEPSEEK_MODEL") or "deepseek-v4-flash",
            ragflow_enabled=_cfg_bool("RAGFLOW_ENABLED"),
            ragflow_api_key=_cfg("RAGFLOW_API_KEY") or None,
            ragflow_base_url=_cfg("RAGFLOW_BASE_URL") or "http://127.0.0.1:9380",
            ragflow_dataset_name=_cfg("RAGFLOW_DATASET_NAME") or "ctf-agent-audit",
            ragflow_timeout_seconds=max(
                1.0, float(str(_cfg("RAGFLOW_TIMEOUT_SECONDS") or 30))
            ),
            experience_search_limit=max(
                1, int(str(_cfg("EXPERIENCE_SEARCH_LIMIT") or 5))
            ),
            ragflow_observability=_cfg_bool("RAGFLOW_OBSERVABILITY", True),
        )
