"""Hack World 动态 flag 验证的 engine 端到端重跑(产出 runs/<run_id>/run.log)。

流程:开一个真实新实例 → 真实 Engine 跑一圈 →
  真实 planner 规划详细 DAG → 真实 executor(ReAct)经 Experience 组件看到已验证
  procedure,对当前实例重跑 solve_extract.py 推导 flag → submit_flag 附 provenance →
  adapter.submit 走 _local_verify(T1 procedure 重跑本地判 LOCAL_PROCEDURE) →
  真实 ee 见 submission correct=true 判 pass+is_completed → et mock DONE → DONE。

评估器分角色开关(env,经 build_evaluator 生效):
  EVALUATOR_PLAN=mock  EVALUATOR_STEP=real  EVALUATOR_TASK=mock
planner / executor 全真实。
"""
import json
import os
import sys
import time

sys.path.insert(0, r"D:/pythonProject/ctf_agent_proj")
os.environ.setdefault("CTF2_CONFIG_JSON", r"D:/pythonProject/ctf2/config.json")
os.environ.setdefault("CTF_STORE_DIR", "data")
# 分角色评估器开关:ep/et mock,ee real;其余(planner/executor)全真实
os.environ["EVALUATOR_PLAN"] = "mock"
os.environ["EVALUATOR_STEP"] = "real"
os.environ["EVALUATOR_TASK"] = "mock"

from agent.engine import Engine
from agent.evaluator import build_evaluator
from agent.executor import RealExecutor
from agent.planner import Planner
from agent.runner import CommandRunner
from agent.skills import CtfSkillsDocStore
from agent.understander import MockTaskUnderstander
from agent.workspace import Workspace
from ctf_platform.config import StoreSettings
from ctf_platform.ctf2 import Ctf2Adapter

CID = "e9baf08f-5f6e-40b8-953f-2c30689f6c05"
GID = "b9bbb32f-f186-458f-b90b-12440c0f6aea"
CHALLENGE_DIR = r"D:/pythonProject/ctf_agent_proj/data/challenges/PCHAL-2026-1223"

adapter = Ctf2Adapter(StoreSettings.from_env())
adapter.practice_ground_id = GID

# 1) 开一个真实新实例,本 run 内复用(memoize start_target → _local_verify 同靶机)
t1 = adapter.start_target(CID)
host, port = t1.get("host") or "", t1.get("port")
target = f"{host}:{port}" if host and port else None
print(f"[driver] fresh target: {target}  status={t1.get('status')}")
adapter.start_target = lambda cid, timeout=120: t1
# 环境打开:清理挑战目录里上次 run 遗留的非依赖/非附件文件(防解题方法泄漏)
_removed = adapter.clean_challenge_dir(CID)
if _removed:
    print(f"[driver] 清理挑战目录遗留产物: {_removed}")

run_id = "run-" + time.strftime("%Y%m%d-%H%M%S") + "-hackworld-rerun"
raw_content = {
    "challenge_id": CID,
    "friendly_id": "PCHAL-2026-1223",
    "name": "Hack World",
    "category": "WEB",
    "challenge_dir": CHALLENGE_DIR,
    "has_container": True,
    "target": target,
    "description": "Web 题目:启动靶机获取目标地址,分析页面漏洞,提取 flag(格式 CTF2{uuid})并提交,完成目标 g1。",
    "goals": [{"id": "g1"}],
}
ws = Workspace.create(run_id, raw_content)

runner = CommandRunner()
# 真实 executor:默认 llm_fn = 真实 LLM ReAct(读 Experience → 重跑 verifier → 提交)
executor = RealExecutor(runner=runner, workspace=ws, adapter=adapter)
# 真实 planner
planner = Planner(workspace=ws, docs=CtfSkillsDocStore())
# 分角色评估器:ep=mock et=mock ee=real(env 已设)
evaluator = build_evaluator(ws)

engine = Engine(planner, executor, evaluator, workspace=ws,
                understander=MockTaskUnderstander())
engine.run(raw_content)

print("== 终态:", engine.scheduler.state.value, "fail_reason:", engine.fail_reason)
print("== submitted_flag:", engine.submitted_flag)
print("== submission:", ws.meta.get("submission"))
print("== run.log: ", ws.root / "run.log")

try:
    stop = getattr(adapter, "stop_target", None)
    if stop:
        stop(CID)
        print("[driver] target stopped")
except Exception as e:
    print("[driver] stop_target 异常:", e)
