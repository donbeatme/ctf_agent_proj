"""局域网发现沙箱 VM:DCHP 下 VM 的 IP 会漂移,但 ed25519 host key 稳定。

按钉住的服务器公钥(`CTF_SSH_HOST_KEY`)在宿主 /24 子网内 ssh-keyscan 匹配,
命中即返回 VM 当前 IP。host key 由服务器私钥签发、LAN 上伪造不出,命中即真身
(TOFU+pin 模型);未配置 host key 时不启用本发现(上层 `SshBackend._connect` 判定)。

`keyscan` 参数是测试缝:单测注入假扫描器,不跑真实 ssh-keyscan。
"""

from __future__ import annotations

import asyncio
import socket

_KEYS_MAXLEN = 80


def _local_ipv4() -> str:
    """默认路由出口 IP(跨平台,免 ipconfig/hostname 解析)。失败回环地址。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _candidates(local_ip: str, exclude: set[str] | None = None) -> list[str]:
    """local_ip 的 /24 内 1..254,跳过 .0/.255 与本机(exclude 可加,如网关)。"""
    parts = local_ip.split(".")
    if len(parts) != 4:
        return []
    prefix = ".".join(parts[:3])
    skip = {int(parts[3]), 0, 255}
    if exclude:
        for ip in exclude:
            seg = ip.split(".")
            if len(seg) == 4 and seg[0] == parts[0] and seg[1] == parts[1] and seg[2] == parts[2]:
                skip.add(int(seg[3]))
    return [f"{prefix}.{i}" for i in range(1, 255) if i not in skip]


async def _keyscan(ip: str, port: int, timeout: float) -> str:
    """ssh-keyscan 取 ed25519 key,失败/超时返回 ""。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh-keyscan", "-T", str(timeout), "-t", "ed25519",
            "-p", str(port), ip,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout + 5)
        return out.decode("utf-8", "replace")
    except (OSError, asyncio.TimeoutError):
        return ""


def parse_key(keyscan_out: str) -> str | None:
    """从 keyscan 输出提取 `<algo> <base64>`,如 "ssh-ed25519 AAAA..."。"""
    for line in keyscan_out.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[-2].endswith("ed25519"):
            return f"{fields[-2]} {fields[-1]}"
    return None


async def discover_vm(expected_key: str, subnet: str | None = None, *,
                      port: int = 22, timeout: float = 1.2,
                      concurrency: int = 32, keyscan=_keyscan) -> str | None:
    """按期望 host key 在局域网找 VM。subnet 给 IP(取其 /24)或 None(取本机路由)。"""
    local = subnet if subnet else _local_ipv4()
    hosts = _candidates(local)
    if not hosts:
        return None
    sem = asyncio.Semaphore(concurrency)

    async def _probe(ip: str):
        async with sem:
            out = await keyscan(ip, port, timeout)
            key = parse_key(out)
            return ip if key is not None and key == expected_key else None

    # 分批并发:命中当前批即返回,避免等完剩余慢探针
    for i in range(0, len(hosts), concurrency):
        batch = hosts[i:i + concurrency]
        for r in await asyncio.gather(*[_probe(ip) for ip in batch]):
            if r is not None:
                return r
    return None


__all__ = ["discover_vm", "parse_key", "_candidates", "_keyscan", "_local_ipv4"]
