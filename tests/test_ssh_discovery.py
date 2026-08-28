"""SshBackend 密钥登录 + host key 局域网自动发现(不连真实 SSH)。

覆盖:
- ssh_key 设置时 connect 传 client_keys,password 仍传
- 首次 connect 网络失败 + host_key 钉住 → 发现新 IP、写回 config、重连成功
- 未配置 host_key / 认证失败(PermissionDenied)→ 不发现、原样抛
- discover_vm:keyscan 命中期望 key → 返回该 IP;不匹配 → None
- config_sandbox.set 读-改-写保留其它键;SandboxSettings.from_env 读 CTF_SSH_KEY/CTF_SSH_HOST_KEY
"""

import asyncio
import json
import os

import asyncssh
import pytest

from agent.ssh import SshBackend
from sandbox_env.config import SandboxSettings
from sandbox_env.discovery import _candidates, discover_vm, parse_key

import config_sandbox
import sandbox_env.discovery as discovery_module


class _FakeClient:
    pass


# ===== Part A:密钥登录 =====

async def test_connect_passes_client_keys(monkeypatch):
    captured = {}

    async def fake_connect(**kwargs):
        captured.update(kwargs)
        return _FakeClient()

    monkeypatch.setattr(asyncssh, "connect", fake_connect)
    sb = SshBackend(host="h", user="root", password="pw",
                    ssh_key="~/.ssh/id_ed25519")
    await sb._connect()
    assert captured["client_keys"] == [os.path.expanduser("~/.ssh/id_ed25519")]
    assert captured["password"] == "pw"


async def test_connect_without_key_omits_client_keys(monkeypatch):
    captured = {}

    async def fake_connect(**kwargs):
        captured.update(kwargs)
        return _FakeClient()

    monkeypatch.setattr(asyncssh, "connect", fake_connect)
    sb = SshBackend(host="h", user="root", password="pw")
    await sb._connect()
    assert "client_keys" not in captured


# ===== Part C:发现回退 =====

async def test_connect_discovery_fallback(monkeypatch):
    calls = []

    async def fake_connect(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise asyncio.TimeoutError("connect timeout")
        return _FakeClient()

    async def fake_discover(*a, **k):
        return "192.168.3.50"

    recorded = []
    monkeypatch.setattr(asyncssh, "connect", fake_connect)
    monkeypatch.setattr(discovery_module, "discover_vm", fake_discover)
    monkeypatch.setattr("config_sandbox.set",
                        lambda name, value: recorded.append((name, value)))

    sb = SshBackend(host="192.168.3.36", user="root", password="p",
                    ssh_key="k", host_key="ssh-ed25519 AAAA")
    client = await sb._connect()
    assert client is not None
    assert sb.host == "192.168.3.50"
    assert len(calls) == 2
    assert calls[1]["host"] == "192.168.3.50"
    assert recorded == [("CTF_SSH_HOST", "192.168.3.50")]


async def test_no_discovery_without_host_key(monkeypatch):
    called = []

    async def fake_connect(**kwargs):
        raise asyncio.TimeoutError("timeout")

    async def fake_discover(*a, **k):
        called.append(1)
        return "1.2.3.4"

    monkeypatch.setattr(asyncssh, "connect", fake_connect)
    monkeypatch.setattr(discovery_module, "discover_vm", fake_discover)
    sb = SshBackend(host="h", user="root", password="p")  # host_key 未配置
    with pytest.raises(asyncio.TimeoutError):
        await sb._connect()
    assert called == []


async def test_no_discovery_on_permission_denied(monkeypatch):
    called = []

    async def fake_connect(**kwargs):
        raise asyncssh.PermissionDenied("bad credentials")

    async def fake_discover(*a, **k):
        called.append(1)
        return "1.2.3.4"

    monkeypatch.setattr(asyncssh, "connect", fake_connect)
    monkeypatch.setattr(discovery_module, "discover_vm", fake_discover)
    sb = SshBackend(host="h", user="root", password="p",
                    host_key="ssh-ed25519 AAAA")
    with pytest.raises(asyncssh.PermissionDenied):
        await sb._connect()
    assert called == []


async def test_discovery_not_found_reraises(monkeypatch):
    async def fake_connect(**kwargs):
        raise asyncio.TimeoutError("timeout")

    async def fake_discover(*a, **k):
        return None

    monkeypatch.setattr(asyncssh, "connect", fake_connect)
    monkeypatch.setattr(discovery_module, "discover_vm", fake_discover)
    sb = SshBackend(host="h", user="root", password="p",
                    host_key="ssh-ed25519 AAAA")
    with pytest.raises(asyncio.TimeoutError):
        await sb._connect()


# ===== Part B:discover_vm =====

TARGET_KEY = "ssh-ed25519 AAAABADKEY"


async def _keyscan_hitting(hit_ip: str):
    async def fake_keyscan(ip, port, timeout):
        return f"{ip} ssh-ed25519 AAAABADKEY\n" if ip == hit_ip else ""
    return fake_keyscan


async def test_discover_vm_matches_host_key():
    hit = "192.168.3.36"
    fake = await _keyscan_hitting(hit)
    found = await discover_vm(TARGET_KEY, subnet="192.168.3.10",
                              concurrency=8, keyscan=fake)
    assert found == hit


async def test_discover_vm_no_match_returns_none():
    async def fake_keyscan(ip, port, timeout):
        return f"{ip} ssh-ed25519 AAAADIFFERENT\n"

    found = await discover_vm(TARGET_KEY, subnet="192.168.3.10",
                              concurrency=8, keyscan=fake_keyscan)
    assert found is None


def test_parse_key():
    out = "# 192.168.3.36:22 SSH-2.0-OpenSSH_9.3\n192.168.3.36 ssh-ed25519 AAAABADKEY\n"
    assert parse_key(out) == "ssh-ed25519 AAAABADKEY"
    assert parse_key("nothing here\n") is None
    assert parse_key("") is None


def test_candidates_skip_local_and_edge():
    hosts = _candidates("192.168.3.10")
    assert len(hosts) == 253
    assert "192.168.3.10" not in hosts
    assert "192.168.3.0" not in hosts and "192.168.3.255" not in hosts
    assert "192.168.3.36" in hosts


# ===== config 写回 / from_env =====

def test_config_sandbox_set_preserves_other_keys(tmp_path, monkeypatch):
    f = tmp_path / "config_sandbox.json"
    f.write_text('{"CTF_SSH_HOST": "192.168.3.36", "CTF_SSH_PASSWORD": "p"}',
                 encoding="utf-8")
    monkeypatch.setattr(config_sandbox, "_CONFIG_FILE", f)
    config_sandbox.set("CTF_SSH_HOST", "192.168.3.50")
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data["CTF_SSH_HOST"] == "192.168.3.50"
    assert data["CTF_SSH_PASSWORD"] == "p"


def test_settings_from_env_reads_key(monkeypatch):
    monkeypatch.setenv("CTF_SSH_KEY", "C:/k/id_ed25519")
    monkeypatch.setenv("CTF_SSH_HOST_KEY", "ssh-ed25519 AAAA")
    s = SandboxSettings.from_env()
    assert s.ssh_key == "C:/k/id_ed25519"
    assert s.ssh_host_key == "ssh-ed25519 AAAA"
