"""配置拆分:config_adaptor / config_sandbox 与主 config(model_config) 分离。

覆盖:
- config_adaptor.get:env 优先 → 自有 JSON → CTF2_CONFIG_JSON 外部文件兜底
- config_sandbox.get:env 优先 → 自有 JSON
- StoreSettings.from_env 从 config_adaptor 取值(不再读 model_config)
- SandboxSettings.from_env 从 config_sandbox 取值
- model_config 不再承载 CTF_SSH_* / CTF2_*(敏感配置已迁移)
"""

import json

import config_adaptor
import config_sandbox
from ctf_platform.config import StoreSettings
from sandbox_env.config import SandboxSettings


def _write(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


# ===== config_adaptor =====


def test_adaptor_reads_own_json(monkeypatch, tmp_path):
    p = _write(tmp_path, "adaptor.json", {"CTF2_API_KEY": "ak-1"})
    monkeypatch.setattr(config_adaptor, "_CONFIG_FILE", p)
    monkeypatch.delenv("CTF2_API_KEY", raising=False)
    assert config_adaptor.get("CTF2_API_KEY") == "ak-1"


def test_adaptor_env_overrides_json(monkeypatch, tmp_path):
    p = _write(tmp_path, "adaptor.json", {"CTF2_API_KEY": "ak-json"})
    monkeypatch.setattr(config_adaptor, "_CONFIG_FILE", p)
    monkeypatch.setenv("CTF2_API_KEY", "ak-env")
    assert config_adaptor.get("CTF2_API_KEY") == "ak-env"


def test_adaptor_falls_back_to_external_config_json(monkeypatch, tmp_path):
    p = _write(tmp_path, "adaptor.json", {})
    external = _write(tmp_path, "external.json", {"CTF2_SESSION_TOKEN": "tok-1"})
    monkeypatch.setattr(config_adaptor, "_CONFIG_FILE", p)
    monkeypatch.setenv("CTF2_CONFIG_JSON", str(external))
    monkeypatch.delenv("CTF2_SESSION_TOKEN", raising=False)
    assert config_adaptor.get("CTF2_SESSION_TOKEN") == "tok-1"


# ===== config_sandbox =====


def test_sandbox_reads_own_json(monkeypatch, tmp_path):
    p = _write(tmp_path, "sandbox.json", {"CTF_SSH_PASSWORD": "pw-1"})
    monkeypatch.setattr(config_sandbox, "_CONFIG_FILE", p)
    monkeypatch.delenv("CTF_SSH_PASSWORD", raising=False)
    assert config_sandbox.get("CTF_SSH_PASSWORD") == "pw-1"


def test_sandbox_env_overrides_json(monkeypatch, tmp_path):
    p = _write(tmp_path, "sandbox.json", {"CTF_SSH_PASSWORD": "pw-json"})
    monkeypatch.setattr(config_sandbox, "_CONFIG_FILE", p)
    monkeypatch.setenv("CTF_SSH_PASSWORD", "pw-env")
    assert config_sandbox.get("CTF_SSH_PASSWORD") == "pw-env"


# ===== Settings 从新模块取值 =====


def test_store_settings_sources_from_config_adaptor(monkeypatch, tmp_path):
    p = _write(tmp_path, "adaptor.json", {
        "CTF2_BASE_URL": "https://api.example.test",
        "CTF2_SESSION_TOKEN": "tok",
        "CTF2_API_KEY": "ak",
        "CTF2_PRACTICE_GROUND_ID": "pg-1",
    })
    monkeypatch.setattr(config_adaptor, "_CONFIG_FILE", p)
    monkeypatch.setenv("CTF_STORE_DIR", str(tmp_path))
    for k in ("CTF2_BASE_URL", "CTF2_SESSION_TOKEN", "CTF2_API_KEY",
              "CTF2_PRACTICE_GROUND_ID"):
        monkeypatch.delenv(k, raising=False)
    s = StoreSettings.from_env()
    assert s.ctf2_base_url == "https://api.example.test"
    assert s.ctf2_session_token == "tok"
    assert s.ctf2_api_key == "ak"
    assert s.ctf2_practice_ground_id == "pg-1"


def test_sandbox_settings_sources_from_config_sandbox(monkeypatch, tmp_path):
    p = _write(tmp_path, "sandbox.json", {
        "CTF_SSH_HOST": "10.0.0.9",
        "CTF_SSH_USER": "alpine",
        "CTF_SSH_PASSWORD": "pw",
        "CTF_SANDBOX_BACKEND": "ssh",
    })
    monkeypatch.setattr(config_sandbox, "_CONFIG_FILE", p)
    for k in ("CTF_SSH_HOST", "CTF_SSH_USER", "CTF_SSH_PASSWORD",
              "CTF_SANDBOX_BACKEND"):
        monkeypatch.delenv(k, raising=False)
    s = SandboxSettings.from_env()
    assert s.ssh_host == "10.0.0.9"
    assert s.ssh_user == "alpine"
    assert s.ssh_password == "pw"
    assert s.ssh_configured is True


# ===== model_config 不再承载敏感键 =====


def test_model_config_no_longer_holds_sensitive_keys():
    import model_config

    data = model_config._config
    for k in ("CTF_SSH_HOST", "CTF_SSH_USER", "CTF_SSH_PASSWORD",
              "CTF2_SESSION_TOKEN", "CTF2_API_KEY", "CTF2_BASE_URL",
              "CTF2_COOKIE"):
        assert k not in data, f"{k} 不应留在 model_config"
