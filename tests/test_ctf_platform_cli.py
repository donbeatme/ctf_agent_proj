"""main.py CLI 子命令注册测试(子进程 UTF-8 模式,仿 test_local_challenge_workflow)。"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable

COMMANDS = [
    "challenge-fetch",
    "challenge-sync",
    "challenge-list",
    "flag-submit",
    "flags-import",
    "cache-stats",
    "cache-purge",
]


def test_main_help_lists_ctf_platform_commands():
    # 子进程也要 UTF-8 模式:否则中文帮助文本按 locale(GBK) 写出,
    # 而父进程在 -X utf8 下按 utf-8 解码会炸(Windows 编码错位)。
    result = subprocess.run(
        [PYTHON, "-X", "utf8", "main.py", "--help"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    for cmd in COMMANDS:
        assert cmd in result.stdout, f"缺少子命令 {cmd}"


def test_subcommand_help_lists_options():
    result = subprocess.run(
        [PYTHON, "-X", "utf8", "main.py", "challenge-fetch", "--help"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert "source" in result.stdout
    assert "--dest" in result.stdout


def _run_cli(args, env_extra=None, cwd=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [PYTHON, "-X", "utf8", "main.py", *args],
        cwd=cwd or REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def test_challenge_list_reads_local_store(tmp_path):
    from ctf_platform.storage import ChallengeMeta, ChallengeStore, connect

    meta1 = ChallengeMeta(
        challenge_id="c-1", platform="ctf2", friendly_id="PCHAL-2026-0001",
        name="测试题一", category="MISC", difficulty="Easy",
    )
    meta2 = ChallengeMeta(
        challenge_id="c-2", platform="ctf2", friendly_id="PCHAL-2026-0002",
        name="测试题二", category="REVERSE", difficulty="Hard",
    )
    st = ChallengeStore(connect(tmp_path))
    assert st.upsert_challenge(meta1) == "insert"
    assert st.upsert_challenge(meta2) == "insert"

    result = _run_cli(
        ["challenge-list"],
        env_extra={"CTF_STORE_DIR": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    assert "PCHAL-2026-0001" in result.stdout
    assert "PCHAL-2026-0002" in result.stdout
    assert "total=2" in result.stdout

    filtered = _run_cli(
        ["challenge-list", "--category", "MISC"],
        env_extra={"CTF_STORE_DIR": str(tmp_path)},
    )
    assert "PCHAL-2026-0001" in filtered.stdout
    assert "PCHAL-2026-0002" not in filtered.stdout


def test_challenge_list_empty_store_prints_hint(tmp_path):
    from ctf_platform.storage import connect

    connect(tmp_path)  # 建库但不插数据
    result = _run_cli(
        ["challenge-list"],
        env_extra={"CTF_STORE_DIR": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    assert "无匹配题目" in result.stdout


def test_challenge_list_missing_db_errors(tmp_path):
    result = _run_cli(
        ["challenge-list"],
        env_extra={"CTF_STORE_DIR": str(tmp_path)},
    )
    assert result.returncode != 0
    assert "本地库不存在" in result.stderr
