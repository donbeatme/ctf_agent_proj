"""平台适配器配置:与主 config(model_config)分开,存放适配器级敏感配置。

配对: Ctf2Adapter(及其子类)经 StoreSettings.from_env 消费本模块。
取值优先级: 环境变量 → config_adaptor.json → CTF2_CONFIG_JSON 指向的外部文件(兼容旧布局)。
"""

import json
import os
from pathlib import Path

_CONFIG_FILE = Path(__file__).resolve().parent / "config_adaptor.json"


def _external_config() -> dict:
    """CTF2_CONFIG_JSON 指向的外部配置文件(兼容旧布局)。"""
    path = os.environ.get("CTF2_CONFIG_JSON")
    if not path:
        return {}
    try:
        data = json.loads(Path(str(path)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def get(name: str, default=None):
    """env 优先 → config_adaptor.json → CTF2_CONFIG_JSON 外部文件兜底。"""
    value = os.environ.get(name)
    if value:
        return value
    try:
        data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    value = data.get(name)
    if value is not None:
        return value
    value = _external_config().get(name)
    return value if value is not None else default


def set(name: str, value) -> None:
    """写回 config_adaptor.json(供会话 token 自动续期持久化,跨进程复用)。

    不覆盖 env(env 仍优先);落盘失败静默跳过,不阻塞请求。
    """
    try:
        data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    data[name] = value
    try:
        _CONFIG_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass
