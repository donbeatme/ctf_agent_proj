"""环境配置；不依赖 python-dotenv。"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def load_env_file(path: Path) -> None:
    """读取简单 KEY=VALUE 文件；已有环境变量优先。"""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    mode: str
    data_dir: Path
    langsmith_enabled: bool
    deepseek_api_key: Optional[str]
    deepseek_base_url: str
    deepseek_model: str
    ragflow_enabled: bool
    ragflow_api_key: Optional[str]
    ragflow_base_url: str
    ragflow_dataset_name: str
    ragflow_timeout_seconds: float = 30.0
    experience_search_limit: int = 5
    ragflow_observability: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        load_env_file(Path(__file__).resolve().parents[1] / ".env")
        mode = os.getenv("CTF_AUDIT_MODE", "offline").strip().lower()
        return cls(
            mode=mode,
            data_dir=Path(os.getenv("CTF_AUDIT_DATA_DIR", "./data")),
            langsmith_enabled=env_bool("LANGSMITH_TRACING") and bool(os.getenv("LANGSMITH_API_KEY")),
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY") or None,
            deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            ragflow_enabled=env_bool("RAGFLOW_ENABLED"),
            ragflow_api_key=os.getenv("RAGFLOW_API_KEY") or None,
            ragflow_base_url=os.getenv("RAGFLOW_BASE_URL", "http://127.0.0.1:9380"),
            ragflow_dataset_name=os.getenv("RAGFLOW_DATASET_NAME", "ctf-agent-audit"),
            ragflow_timeout_seconds=max(
                1.0, float(os.getenv("RAGFLOW_TIMEOUT_SECONDS", "30"))
            ),
            experience_search_limit=max(
                1, int(os.getenv("EXPERIENCE_SEARCH_LIMIT", "5"))
            ),
            ragflow_observability=env_bool("RAGFLOW_OBSERVABILITY", True),
        )
