"""截断续跑验证:从 flag 已提取的断点轻量收敛(不重跑执行/提取)。

断点来源:rerun_hackworld.py 某次运行在 s1(实际已提取并提交 flag,ee 软鉴定
is_completed=true → task_completed=true)后的终局修订(REFLECTING→PLANNING)被截断,
state.json 已持久化 submission(flag + correct=None)与 s1 step_record(is_completed=true)。

resume 只跑终局尾部,不再触发 executor:
  PLANNING(bp 已存在)→ PLAN_REVIEW(ep mock)→ SCHEDULING 见 task_completed
  → REFLECTING(et mock DONE)→ 终局修订(planner 真实一次)→ DONE

用法:python -X utf8 -u scripts/resume_hackworld.py [run_id]
默认 resume run-20260819-181701-hackworld-rerun(已含 submission 证据)。
"""
import os
import shutil
import sys
import time

sys.path.insert(0, r"D:/pythonProject/ctf_agent_proj")
os.environ.setdefault("CTF2_CONFIG_JSON", r"D:/pythonProject/ctf2/config.json")
os.environ.setdefault("CTF_STORE_DIR", "data")
# 分角色评估器:ep/et mock,ee real(与 rerun_hackworld.py 一致)
os.environ["EVALUATOR_PLAN"] = "mock"
os.environ["EVALUATOR_STEP"] = "real"
os.environ["EVALUATOR_TASK"] = "mock"

from agent.engine import Engine
from agent.evaluator import build_evaluator
from agent.executor import RealExecutor
from agent.planner import Planner
from agent.runner import CommandRunner
from agent.skills import CtfSkillsDocStore
from agent.workspace import Workspace
from ctf_platform.config import StoreSettings
from ctf_platform.ctf2 import Ctf2Adapter

RUN_ID = sys.argv[1] if len(sys.argv) > 1 else "run-20260819-181701-hackworld-rerun"

# 先载入工作区供 executor/planner 引用同一份持久化状态(Engine.resume 内部会再 load 一次)
ws = Workspace.load(RUN_ID)
runner = CommandRunner()
adapter = Ctf2Adapter(StoreSettings.from_env())
executor = RealExecutor(runner=runner, workspace=ws, adapter=adapter)
planner = Planner(workspace=ws, docs=CtfSkillsDocStore())
evaluator = build_evaluator(ws)

# resume 的 logger 以 "w" 重开 run.log,先备份断点前的完整轨迹
log_path = ws.root / "run.log"
if log_path.exists():
    shutil.copy(log_path, ws.root / f"run.log.pre-{time.strftime('%Y%m%d-%H%M%S')}")

t0 = time.time()
engine = Engine.resume(RUN_ID, planner, executor, evaluator)
print(f"== resume 耗时: {time.time() - t0:.0f}s")
print("== 终态:", engine.scheduler.state.value, "fail_reason:", engine.fail_reason)
print("== task_completed:", engine.task_completed)
print("== submission:", ws.meta.get("submission"))
print("== 是否重跑执行(executor 无调用则未重跑提取): 见上方 run.log 的 EXECUTING 段")
print("== run.log: ", ws.root / "run.log")
