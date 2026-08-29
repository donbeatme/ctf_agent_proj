"""在远程沙箱 VM 上构建/重建 ctf-sandbox:latest。

流程:SSH(CTF_SSH_* 凭据) → 确保 docker daemon → 上传最小构建上下文
({CTF_SSH_WORKDIR}-build: Dockerfile + install_ctf_tools.sh) → docker build(流式输出)
→ 构建后 --verify 复查 + 几个工具点检。

用法:python scripts/build_sandbox_image.py [--skip-build]
凭据:CTF_SSH_HOST/USER/PASSWORD env 或 config_sandbox.json(config_sandbox 模块)。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config_sandbox import get as _cfg

HOST = _cfg("CTF_SSH_HOST") or sys.exit("CTF_SSH_HOST 未配置")
PORT = int(_cfg("CTF_SSH_PORT") or 22)
USER = _cfg("CTF_SSH_USER") or "root"
PASS = _cfg("CTF_SSH_PASSWORD") or ""
KEY = _cfg("CTF_SSH_KEY") or ""
WORKDIR = str(_cfg("CTF_SSH_WORKDIR") or "/root/ctf").rstrip("/")
MIRROR = _cfg("CTF_APT_MIRROR") or "mirrors.aliyun.com"
SKIP_BUILD = "--skip-build" in sys.argv

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DF = os.path.join(HERE, "Dockerfile.ctf-sandbox")
SCRIPT = os.path.join(ROOT, "skills", "ctf-skills", "scripts", "install_ctf_tools.sh")
# ghidra 官方 zip + git 方法源码包(经本机代理下载到 downloads/;VM 直连 GitHub 不通,
# 构建改 COPY 烘焙)。pycdc/RsaCtfTool 与 ghidra 同模式,Dockerfile 里 COPY 解包。
GHIDRA_ZIP = os.path.join(ROOT, "downloads", "ghidra_12.1.3_PUBLIC_20260817.zip")
PYCDC_TGZ = os.path.join(ROOT, "downloads", "pycdc.tar.gz")
RSACTFTOOL_TGZ = os.path.join(ROOT, "downloads", "RsaCtfTool.tar.gz")
REMOTE = f"{WORKDIR}-build"


def _lf(path: str) -> bytes:
    return open(path, "rb").read().replace(b"\r\n", b"\n")


def _exec(client, cmd: str, timeout: float | None = None, echo=True):
    if echo:
        print(f"\n$ {cmd}", flush=True)
    _stdin, stdout, _stderr = client.exec_command(cmd, timeout=timeout)
    channel = stdout.channel
    out_parts: list[str] = []
    err_parts: list[str] = []
    while True:
        if channel.recv_ready():
            chunk = channel.recv(32768).decode("utf-8", "replace")
            out_parts.append(chunk)
            print(chunk, end="", flush=True)
        if channel.recv_stderr_ready():
            chunk = channel.recv_stderr(32768).decode("utf-8", "replace")
            err_parts.append(chunk)
            print(chunk, end="", file=sys.stderr, flush=True)
        if (channel.exit_status_ready() and not channel.recv_ready()
                and not channel.recv_stderr_ready()):
            break
        time.sleep(0.05)
    return "".join(out_parts), "".join(err_parts), channel.recv_exit_status()


def main() -> None:
    import paramiko

    print(f"[connect] {USER}@{HOST}:{PORT}")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs = {
        "hostname": HOST,
        "port": PORT,
        "username": USER,
        "password": PASS or None,
        "timeout": 20,
    }
    if KEY:
        connect_kwargs["key_filename"] = os.path.expanduser(KEY)
    client.connect(**connect_kwargs)
    print("[connect] ok")

    _exec(client, "docker info >/dev/null 2>&1 || (rc-service docker start >/dev/null 2>&1; sleep 3)")
    out, err, rc = _exec(client, "docker version --format '{{.Server.Version}}'")
    if rc != 0:
        sys.exit("docker daemon 不可用,终止")

    build_rc = 0
    if SKIP_BUILD:
        print("[skip-build] 仅复查,不重新构建")
        _exec(client, f"docker run --rm {REMOTE.replace('/ctf-build', '/ctf/scripts')} 2>/dev/null || true")
    else:
        missing = [p for p in (GHIDRA_ZIP, PYCDC_TGZ, RSACTFTOOL_TGZ) if not os.path.exists(p)]
        if missing:
            sys.exit(
                "缺少构建上下文文件:\n" + "\n".join(f"  {p}" for p in missing) + "\n"
                "请先经代理下载到 downloads/(curl -fSL -x http://127.0.0.1:7897 -o <path> <url>):\n"
                "  ghidra_12.1.3_PUBLIC_20260817.zip  https://github.com/NationalSecurityAgency/"
                "ghidra/releases/download/Ghidra_12.1.3_build/ghidra_12.1.3_PUBLIC_20260817.zip\n"
                "  pycdc.tar.gz        https://github.com/zrax/pycdc (git clone 后 tar czf)\n"
                "  RsaCtfTool.tar.gz   https://github.com/RsaCtfTool/RsaCtfTool (git clone 后 tar czf)"
            )

        def _upload(sftp_, local: str, remote: str) -> None:
            print(f"[upload] {os.path.basename(local)} "
                  f"({os.path.getsize(local)//1024//1024} MB)...", flush=True)
            with sftp_.open(remote, "wb") as f, open(local, "rb") as src:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)

        print(f"[upload] {DF} + install_ctf_tools.sh + 3 个源码包 -> {REMOTE}")
        _exec(client, f"rm -rf {REMOTE} && mkdir -p {REMOTE}/skills/ctf-skills/scripts")
        sftp = client.open_sftp()
        with sftp.open(f"{REMOTE}/Dockerfile.ctf-sandbox", "wb") as f:
            f.write(_lf(DF))
        with sftp.open(f"{REMOTE}/skills/ctf-skills/scripts/install_ctf_tools.sh", "wb") as f:
            f.write(_lf(SCRIPT))
        _upload(sftp, GHIDRA_ZIP, f"{REMOTE}/ghidra.zip")
        _upload(sftp, PYCDC_TGZ, f"{REMOTE}/pycdc.tar.gz")
        _upload(sftp, RSACTFTOOL_TGZ, f"{REMOTE}/RsaCtfTool.tar.gz")
        sftp.close()
        print("[upload] ok")

        t0 = time.time()
        print("[build] 开始(装 70 个 ctf-skill 工具,可能 10-30 分钟)...", flush=True)
        build_cmd = f"cd {REMOTE} && docker build -t ctf-sandbox:latest --build-arg APT_MIRROR={MIRROR} -f Dockerfile.ctf-sandbox ."
        _out, _err, build_rc = _exec(client, build_cmd, timeout=None)
        print(f"[build] 结束,用时 {int((time.time()-t0)/60)} 分钟,rc={build_rc}", flush=True)

    if build_rc != 0:
        client.close()
        sys.exit(f"[failed] docker build rc={build_rc}(保留构建缓存以便修复后重试)")

    # 构建成功后回收 build cache;最终镜像不会被删除。
    if not SKIP_BUILD:
        print("\n[prune] 回收 build cache...")
        _exec(client, "docker builder prune -f -a 2>&1 | tail -3")
        _exec(client, "df -h / | tail -1")

    print("\n===== 构建后复查:安装脚本 --verify =====")
    _exec(client, "docker run --rm ctf-sandbox:latest bash /opt/install_ctf_tools.sh --verify")

    print("\n===== 工具点检 =====")
    _exec(client, "docker run --rm ctf-sandbox:latest bash -lc 'for c in tshark capinfos exiftool binwalk foremost steghide 7z zsteg r2 objdump vol file gdb analyzeHeadless; do printf \"%-14s %s\\n\" \"$c\" \"$(command -v $c || echo MISSING)\"; done'")

    client.close()
    print("\n[done] 镜像已就绪")


if __name__ == "__main__":
    main()
