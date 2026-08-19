"""6 类题真跑驱动:每类一道,逐题在子进程里跑(超时看门狗隔离)。

每题的完整运行日志在 runs/real-<label>-<ts>/run.log;
每题结果 JSON 在 runs/<label>_result.json;进度追写 runs/six_categories_progress.log。
末尾打印 6 类汇总表。
"""

import json
import os
import subprocess
import sys
import time

HERE = r"D:/pythonProject/ctf_agent_proj"
sys.path.insert(0, HERE)

CHALLENGES = [
    ("PCHAL-2026-0024", "WEB"),
    ("PCHAL-2026-0062", "PWN"),
    ("PCHAL-2026-0016", "CRYPTO"),
    ("PCHAL-2026-0013", "REVERSE"),
    ("PCHAL-2026-0462", "FORENSICS"),
    ("PCHAL-2026-0007", "MISC"),
]
PER_CHALLENGE_TIMEOUT = 1800  # 30 分钟/题
PROGRESS = os.path.join(HERE, "runs", "six_categories_progress.log")
RUNS_DIR = os.path.join(HERE, "runs")


def _log(line: str) -> None:
    os.makedirs(RUNS_DIR, exist_ok=True)
    with open(PROGRESS, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


def _read_result(label: str) -> dict:
    path = os.path.join(RUNS_DIR, f"{label}_result.json")
    if not os.path.isfile(path):
        return {}
    try:
        return json.loads(open(path, encoding="utf-8").read())
    except (OSError, json.JSONDecodeError):
        return {}


def main() -> None:
    results = []
    for fid, label in CHALLENGES:
        _log(f"[{time.strftime('%H:%M:%S')}] {label} {fid} starting")
        t0 = time.time()
        with open(os.path.join(RUNS_DIR, f"{label}_subprocess.log"), "w", encoding="utf-8") as out:
            try:
                subprocess.run(
                    [sys.executable, "-X", "utf8",
                     os.path.join(HERE, "scripts", "run_one_challenge.py"), fid, label],
                    cwd=HERE, stdout=out, stderr=out, timeout=PER_CHALLENGE_TIMEOUT,
                )
            except subprocess.TimeoutExpired:
                _log(f"[{time.strftime('%H:%M:%S')}] {label} {fid} TIMEOUT ({PER_CHALLENGE_TIMEOUT}s)")
        summary = _read_result(label)
        if summary:
            summary.setdefault("elapsed", round(time.time() - t0, 1))
        else:
            summary = {"friendly_id": fid, "label": label, "state": "TIMEOUT",
                       "elapsed": round(time.time() - t0, 1), "error": "看门狗超时/无结果"}
        summary["elapsed"] = round(time.time() - t0, 1)
        results.append(summary)
        _log(f"[{time.strftime('%H:%M:%S')}] {label} {fid} done state={summary.get('state')} err={summary.get('error')}")

    print("\n===== 6 类真跑汇总 =====")
    for r in results:
        sub = r.get("submission") or {}
        print(
            f"  {str(r.get('label')):<9} {r.get('friendly_id')}  "
            f"state={r.get('state')}  flag={str(r.get('submitted_flag'))[:42]}  "
            f"correct={sub.get('correct')} ok={sub.get('ok')}  "
            f"secs={r.get('elapsed')}  err={r.get('error')}"
        )
    with open(os.path.join(RUNS_DIR, "six_categories_summary.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
