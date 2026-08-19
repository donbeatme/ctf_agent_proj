"""单题真跑:物化(下载附件/开靶机) + 真实 planner + 真实 executor + 真实 ee。

供 run_six_categories.py 以子进程方式逐题调用(超时看门狗隔离)。
结果写一行 RESULT <json> 到 stdout 与 runs/<label>_result.json;运行日志落在
runs/real-<label>-<ts>/run.log(engine 自己的 workspace 日志)。

评估器分角色开关(env,对齐 Hack World 基线):
  EVALUATOR_PLAN=mock  EVALUATOR_STEP=real  EVALUATOR_TASK=mock
planner / executor 全真实;understander 用 RealTaskUnderstander(摄入本地物化目录,
行使题型判定 → Forensics 路由)。
"""

import json
import os
import sys
import time

os.environ.setdefault("CTF2_CONFIG_JSON", r"D:/pythonProject/ctf2/config.json")
os.environ.setdefault("CTF_STORE_DIR", "data")
os.environ["EVALUATOR_PLAN"] = "mock"
os.environ["EVALUATOR_STEP"] = "real"
os.environ["EVALUATOR_TASK"] = "mock"
sys.path.insert(0, r"D:/pythonProject/ctf_agent_proj")

# 命令只经沙箱执行:CommandRunner 懒建 SandboxManager(config_sandbox 提供凭据)
GID = "b9bbb32f-f186-458f-b90b-12440c0f6aea"
RESULTS_DIR = r"D:/pythonProject/ctf_agent_proj/runs"


def _write_result(label: str, data: dict) -> None:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(os.path.join(RESULTS_DIR, f"{label}_result.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main() -> int:
    fid, label = sys.argv[1], sys.argv[2]
    started = time.time()
    result = {
        "friendly_id": fid, "label": label, "state": None, "fail_reason": None,
        "submitted_flag": None, "submission": None, "replans": None,
        "steps": None, "run_id": None, "seconds": None, "error": None,
    }
    try:
        from agent.engine import Engine
        from agent.evaluator import build_evaluator
        from agent.executor import RealExecutor
        from agent.planner import Planner
        from agent.runner import CommandRunner
        from agent.skills import CtfSkillsDocStore
        from agent.workspace import Workspace
        from ctf_platform.config import StoreSettings
        from ctf_platform.ctf2 import Ctf2Adapter
        from task_understanding.real_understander import RealTaskUnderstander

        adapter = Ctf2Adapter(StoreSettings.from_env())
        adapter.practice_ground_id = GID
        # 物化:解析本地索引 → 下载附件(distfiles/) → 含容器自动开靶机 → metadata.yml 带 target。
        # 平台开靶机有限流(429),退避重试几次。
        dest = None
        for attempt in range(4):
            try:
                dest = adapter.ingest(fid)
                break
            except Exception as exc:  # noqa: BLE001 — 限流/临时 429 时退避重试
                if "429" in str(exc) or "RATE_LIMIT" in str(exc).upper():
                    time.sleep(45 * (attempt + 1))
                    continue
                raise
        if dest is None:
            raise RuntimeError("物化失败:平台开靶机限流重试耗尽")
        raw = {"challenge_dir": str(dest)}
        run_id = f"real-{label}-{time.strftime('%Y%m%d-%H%M%S')}"
        ws = Workspace.create(run_id, raw)
        runner = CommandRunner()
        executor = RealExecutor(runner=runner, workspace=ws, workdir=str(dest), adapter=adapter)
        planner = Planner(workspace=ws, docs=CtfSkillsDocStore())
        evaluator = build_evaluator(ws)
        engine = Engine(planner, executor, evaluator, workspace=ws,
                        understander=RealTaskUnderstander())
        engine.run(raw)
        result.update(
            state=engine.scheduler.state.value,
            fail_reason=engine.fail_reason,
            submitted_flag=engine.submitted_flag,
            submission=(ws.meta or {}).get("submission"),
            replans=engine.replans,
            steps=len(engine.bp.steps) if engine.bp else 0,
            run_id=run_id,
        )
    except Exception as exc:  # noqa: BLE001 — 单题异常记入结果,不中断批次
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["seconds"] = round(time.time() - started, 1)
    _write_result(label, result)
    print("RESULT " + json.dumps(result, ensure_ascii=False))
    return 0 if result["state"] == "DONE" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        fatal = {"fatal": f"{type(exc).__name__}: {exc}"}
        _write_result(sys.argv[2] if len(sys.argv) > 2 else "unknown", fatal)
        print("RESULT " + json.dumps(fatal, ensure_ascii=False))
        sys.exit(2)
