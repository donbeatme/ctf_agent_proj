"""main.py CLI 子命令注册测试(子进程 UTF-8 模式,仿 test_local_challenge_workflow)。"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable

COMMANDS = [
    "challenge-fetch",
    "challenge-sync",
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
