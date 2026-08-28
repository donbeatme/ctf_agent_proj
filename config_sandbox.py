"""沙箱环境配置:与主 config(model_config)分开,存放沙箱级敏感配置。

配对: SandboxManager/SshSandboxBackend 经 SandboxSettings.from_env 消费本模块。
取值优先级: 环境变量 → config_sandbox.json。
"""

import json
import os
from pathlib import Path

_CONFIG_FILE = Path(__file__).resolve().parent / "config_sandbox.json"


def get(name: str, default=None):
    """env 优先 → config_sandbox.json 兜底。"""
    value = os.environ.get(name)
    if value:
        return value
    try:
        data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    value = data.get(name)
    return value if value is not None else default


def set(name: str, value) -> None:
    """写回 config_sandbox.json(读-改-写,保留其它键)。env 不改——只落文件。"""
    try:
        data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    data[name] = value
    _CONFIG_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
