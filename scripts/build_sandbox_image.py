"""在远程沙箱 VM 上构建/重建 ctf-sandbox:latest。

流程:SSH(CTF_SSH_* 凭据) → 确保 docker daemon → 上传最小构建上下文
(/root/ctf-build: Dockerfile + install_ctf_tools.sh) → docker build(流式输出)
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
USER = _cfg("CTF_SSH_USER") or "root"
PASS = _cfg("CTF_SSH_PASSWORD") or ""
MIRROR = _cfg("CTF_APT_MIRROR") or "mirrors.aliyun.com"
SKIP_BUILD = "--skip-build" in sys.argv

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DF = os.path.join(HERE, "Dockerfile.ctf-sandbox")
SCRIPT = os.path.join(ROOT, "skills", "ctf-skills", "scripts", "install_ctf_tools.sh")
# ghidra 官方 zip(经本机代理下载到 downloads/;VM 直连 GitHub 不通,构建改 COPY 烘焙)
GHIDRA_ZIP = os.path.join(ROOT, "downloads", "ghidra_12.1.3_PUBLIC_20260817.zip")
REMOTE = "/root/ctf-build"


def _lf(path: str) -> bytes:
    return open(path, "rb").read().replace(b"\r\n", b"\n")


def _exec(client, cmd: str, timeout: float | None = None, echo=True):
    if echo:
        print(f"\n$ {cmd}", flush=True)
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    if out:
        print(out, flush=True)
    if err:
        print(err, flush=True)
    return out, err, stdout.channel.recv_exit_status()


def main() -> None:
    import paramiko

    print(f"[connect] {USER}@{HOST}")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, port=22, username=USER, password=PASS, timeout=20)
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
        if not os.path.exists(GHIDRA_ZIP):
            sys.exit(
                f"缺少 ghidra zip: {GHIDRA_ZIP}\n"
                "请先经代理下载:D:\\pythonProject\\ctf_agent_proj 下\n"
                "curl -fSL -x http://127.0.0.1:7897 -o downloads/ghidra_12.1.3_PUBLIC_20260817.zip "
                "https://github.com/NationalSecurityAgency/ghidra/releases/download/"
                "Ghidra_12.1.3_build/ghidra_12.1.3_PUBLIC_20260817.zip"
            )
        print(f"[upload] {DF} + install_ctf_tools.sh + ghidra.zip -> {REMOTE}")
        _exec(client, f"rm -rf {REMOTE} && mkdir -p {REMOTE}/skills/ctf-skills/scripts")
        sftp = client.open_sftp()
        with sftp.open(f"{REMOTE}/Dockerfile.ctf-sandbox", "wb") as f:
            f.write(_lf(DF))
        with sftp.open(f"{REMOTE}/skills/ctf-skills/scripts/install_ctf_tools.sh", "wb") as f:
            f.write(_lf(SCRIPT))
        print(f"[upload] ghidra.zip({os.path.getsize(GHIDRA_ZIP)//1024//1024} MB)...", flush=True)
        with sftp.open(f"{REMOTE}/ghidra.zip", "wb") as f:
            with open(GHIDRA_ZIP, "rb") as src:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
        sftp.close()
        print("[upload] ok")

        t0 = time.time()
        print("[build] 开始(装 70 个 ctf-skill 工具,可能 10-30 分钟)...", flush=True)
        build_cmd = f"cd {REMOTE} && docker build -t ctf-sandbox:latest --build-arg APT_MIRROR={MIRROR} -f Dockerfile.ctf-sandbox ."
        _out, _err, build_rc = _exec(client, build_cmd, timeout=None)
        print(f"[build] 结束,用时 {int((time.time()-t0)/60)} 分钟,rc={build_rc}", flush=True)

    # 构建后回收 build cache(成功/失败都做;失败时磁盘可能已满,先腾出空间再复查)
    if not SKIP_BUILD:
        print("\n[prune] 回收 build cache...")
        _exec(client, "docker builder prune -f -a 2>&1 | tail -3")
        _exec(client, "df -h / | tail -1")

    print("\n===== 构建后复查:安装脚本 --verify =====")
    _exec(client, "docker run --rm ctf-sandbox:latest bash /opt/install_ctf_tools.sh --verify")

    print("\n===== 工具点检 =====")
    _exec(client, "docker run --rm ctf-sandbox:latest bash -lc 'for c in tshark capinfos exiftool binwalk foremost steghide 7z zsteg r2 objdump vol file gdb analyzeHeadless; do printf \"%-14s %s\\n\" \"$c\" \"$(command -v $c || echo MISSING)\"; done'")

    client.close()
    if build_rc != 0:
        sys.exit(f"[failed] docker build rc={build_rc}(镜像未更新,请见上方输出)")
    print("\n[done] 镜像已就绪")


if __name__ == "__main__":
    main()
