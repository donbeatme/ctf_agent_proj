"""环境检查钩子:SkillEnvProbe 探测 + apply_tool 返回 probe + engine ENV_CHECK 打点 + log。

覆盖:
- probe_tool 四态(available/missing/manual/unknown)
- probe_sandbox(needed + 运行时探测)与 probe_category 透传
- probe_manifest 全量计数(真 catalog 总和恒等式)
- apply_tool 返回带 probe(向后兼容 added/unknown)
- engine 在 run_start / step 触发 ENV_CHECK
- logger 写 check[run_start] / check[step] 与 run-end 汇总
"""

from agent.checks import SkillEnvProbe
from agent.ctf_skill_tools import TOOL_MANIFEST, CtfSkillToolCatalog
from agent.engine import Engine
from agent.evaluator import EvalResult, MockEvaluator, Verdict
from agent.executor import MockExecutor
from agent.schema import PlannerMode
from agent.tools import ToolRegistry
from agent.workspace import MockWorkspace
from tests.mock_data import MOCK_TASK


class StubCatalog:
    """只含 get_tool / compatibility / allowed_tools / install_commands 的最小目录。"""

    def __init__(self, manifest, categories=None):
        self.manifest = list(manifest)
        self._by_id = {e["tool_id"]: e for e in self.manifest}
        self._cat = categories or {}

    def get_tool(self, tool_id):
        return self._by_id.get(tool_id)

    def compatibility(self, category):
        return self._cat.get(category, {}).get("compatibility", "")

    def allowed_tools(self, category):
        return self._cat.get(category, {}).get("allowed_tools", [])

    def install_commands(self, category):
        return self._cat.get(category, {}).get("install_cmds", [])


# ===== probe_tool 四态 =====


def test_probe_tool_states():
    stub = StubCatalog([
        {"tool_id": "a", "verify_check": "import json"},              # stdlib 存在
        {"tool_id": "b", "verify_check": "import no_such_mod_xyz"},   # 必不存在
        {"tool_id": "c", "verify_check": ""},                         # manual
    ])
    probe = SkillEnvProbe(stub)
    assert probe.probe_tool("a")["status"] == "available"
    assert probe.probe_tool("b")["status"] == "missing"
    assert probe.probe_tool("c")["status"] == "manual"
    assert probe.probe_tool("no.such")["status"] == "unknown"  # 不在清单


def test_probe_tool_returns_check():
    stub = StubCatalog([{"tool_id": "a", "verify_check": "import json"}])
    p = SkillEnvProbe(stub).probe_tool("a")
    assert p["tool_id"] == "a" and p["check"] == "import json"


# ===== 沙箱判定 =====


def test_probe_sandbox():
    probe = SkillEnvProbe(StubCatalog([]), sandbox_probe=lambda cat: False)
    pwn = probe.probe_sandbox("ctf-pwn")
    assert pwn["needed"] is True and pwn["available"] is False
    web = probe.probe_sandbox("ctf-web")
    assert web["needed"] is False and web["available"] is None  # 不需要时不探测


def test_probe_sandbox_available_true():
    probe = SkillEnvProbe(StubCatalog([]), sandbox_probe=lambda cat: True)
    assert probe.probe_sandbox("ctf-pwn")["available"] is True


def test_sandbox_categories_override():
    probe = SkillEnvProbe(StubCatalog([]), sandbox_probe=lambda cat: False,
                          sandbox_categories={"ctf-web"})
    assert probe.probe_sandbox("ctf-web")["needed"] is True
    assert probe.probe_sandbox("ctf-pwn")["needed"] is False


# ===== 分类就绪度 =====


def test_probe_category():
    stub = StubCatalog([], {
        "ctf-web": {"compatibility": "Requires network",
                    "allowed_tools": ["Bash", "Read"],
                    "install_cmds": ["apt install nmap", "pip install requests"]},
    })
    probe = SkillEnvProbe(stub, sandbox_probe=lambda cat: True)
    rep = probe.probe_category("ctf-web")
    assert rep["exists"] is True
    assert rep["compatibility"] == "Requires network"
    assert rep["allowed_tools"] == ["Bash", "Read"]
    assert rep["install_cmds"] == ["apt install nmap", "pip install requests"]
    assert rep["sandbox"]["needed"] is False
    # 未知分类:exists=False,透传空
    rep2 = probe.probe_category("no.such")
    assert rep2["exists"] is False
    assert rep2["compatibility"] == "" and rep2["allowed_tools"] == []


# ===== 全量清单快照 =====


def test_probe_manifest_counts():
    stub = StubCatalog([
        {"tool_id": "a", "verify_check": "import json"},
        {"tool_id": "b", "verify_check": "import no_such_mod_xyz"},
        {"tool_id": "c", "verify_check": ""},
    ])
    probe = SkillEnvProbe(stub, sandbox_probe=lambda cat: False)
    rep = probe.probe_manifest()
    assert rep["total"] == 3
    assert rep["available"] == 1
    assert rep["missing"] == 1
    assert rep["manual"] == 1
    assert rep["unknown"] == 0
    assert rep["missing_list"] == ["b(import no_such_mod_xyz)"]
    assert rep["sandbox"]["needed"] is True   # 以需隔离分类(ctf-pwn)代表运行时
    assert rep["sandbox"]["available"] is False


def test_probe_manifest_real_catalog_sums():
    """真 catalog:总数等于 TOOL_MANIFEST,各状态之和等于总数(可用/缺失随机器变)。"""
    rep = SkillEnvProbe().probe_manifest()
    assert rep["total"] == len(TOOL_MANIFEST)
    assert (rep["available"] + rep["missing"] + rep["manual"]
            + rep["unknown"]) == rep["total"]
    assert len(rep["missing_list"]) == rep["missing"]


def test_probe_manifest_sandbox_uses_configured_rep():
    """probe_manifest 的 sandbox 代表分类从 sandbox_categories 挑(不硬编码 ctf-pwn)。"""
    stub = StubCatalog([{"tool_id": "a", "verify_check": "import json"}])
    probe = SkillEnvProbe(stub, sandbox_probe=lambda cat: True,
                          sandbox_categories={"ctf-web"})
    rep = probe.probe_manifest()
    assert rep["sandbox"]["category"] == "ctf-web"
    assert rep["sandbox"]["needed"] is True
    assert rep["sandbox"]["available"] is True


# ===== apply_tool 返回带 probe =====


def test_apply_tool_returns_probe():
    ws = MockWorkspace()
    ws.tool_catalog = CtfSkillToolCatalog()
    reg = ToolRegistry()
    reg.set_workspace(ws)
    res = reg.call_tool("apply_tool", {"tool_ids": ["sqlmap", "no.such"]})
    assert res["added"] == ["sqlmap"]          # 向后兼容
    assert res["unknown"] == ["no.such"]       # 向后兼容
    assert "probe" in res                      # 新增 key
    assert "sqlmap" in res["probe"]
    assert res["probe"]["sqlmap"]["status"] in \
        {"available", "missing", "manual", "unknown"}


# ===== engine ENV_CHECK 打点 =====


class StubChecker:
    """固定 report 的环境检查桩(不依赖真实环境)。"""

    def probe_manifest(self):
        return {"total": 3, "available": 1, "missing": 1, "manual": 1, "unknown": 0,
                "missing_list": ["gdb(gdb)"],
                "sandbox": {"category": "ctf-pwn", "needed": True, "available": False}}

    def probe_tools(self, tool_ids):
        return [{"tool_id": t, "status": "missing", "check": t} for t in tool_ids]

    def probe_category(self, cat):
        return {"category": cat, "exists": True, "compatibility": "Requires X",
                "allowed_tools": ["Bash"], "install_cmds": ["apt install x"],
                "sandbox": {"category": cat, "needed": True, "available": False}}


class _StepPlanner:
    """单步(skill_id 绑定)planner:initial 产出带 skill_id 的步骤,revise 原样返回。"""

    def __init__(self):
        self.calls = 0
        self.bp = None

    def plan(self, pin):
        self.calls += 1
        from agent.blueprint import Blueprint, Step
        if pin.mode == PlannerMode.INITIAL:
            self.bp = Blueprint(meta={"task": MOCK_TASK})
            self.bp.add_step(Step(id="s1", instruction="读题", criterion="拿到文本",
                                  skill_id="ctf-pwn.exploit"))
        return self.bp


def _env_run(checker):
    """跑一个带 checker 的完整 run,返回 ENV_CHECK 事件列表 [(scope, step_id), ...]。"""
    evaluator = MockEvaluator({
        "evaluator_plan": EvalResult(Verdict.PASS, "计划可执行"),
        "evaluator_step": EvalResult(Verdict.PASS, "s1: 完成"),
        "evaluator_task": EvalResult(Verdict.DONE, "反思: 无问题"),
    })
    engine = Engine(_StepPlanner(), MockExecutor(observation="执行完成"), evaluator,
                    workspace=MockWorkspace(), checker=checker)
    events = []
    engine.signals.subscribe(_Collector(events))
    engine.run(MOCK_TASK)
    return events


class _Collector:
    def __init__(self, events):
        self._events = events

    def on_env_check(self, scope="", step_id=None, report=None, **kw):
        self._events.append((scope, step_id))


def test_engine_emits_env_check():
    events = _env_run(StubChecker())
    assert ("run_start", None) in events
    assert ("step", "s1") in events          # step 作用域探测的是当前活动集 + 分类


def test_engine_no_checker_no_env_check():
    """无 catalog / 未显式传 checker → 不发 ENV_CHECK(现有测试不受影响)。"""
    events = _env_run(None)
    assert events == []


# ===== logger 写 run.log =====


def test_logger_writes_env_check(tmp_path):
    from agent.logging import EngineLogger

    log = EngineLogger(tmp_path)
    log.on_run_started(task={"description": "t"})
    log.on_env_check(scope="run_start", report={
        "total": 70, "available": 60, "missing": 5, "manual": 4, "unknown": 1,
        "missing_list": ["gdb(gdb)", "radare2(r2)"],
        "sandbox": {"category": "ctf-pwn", "needed": True, "available": False},
    })
    log.on_env_check(scope="step", step_id="s1", report={
        "tools": [{"tool_id": "gdb", "status": "missing", "check": "gdb"}],
        "category": {"category": "ctf-pwn", "exists": True, "compatibility": "Requires bash",
                     "allowed_tools": ["Bash"], "install_cmds": ["apt install gdb"],
                     "sandbox": {"category": "ctf-pwn", "needed": True, "available": False}},
    })
    log.on_run_end(state="DONE", fail_reason=None, total_cycles=3)
    text = (tmp_path / "run.log").read_text(encoding="utf-8")

    assert "check[run_start] 环境快照 工具可用 60/70 缺失 5" in text
    assert "沙箱运行时: docker/podman(无)" in text
    assert "check[run_start] 缺工具: gdb(gdb), radare2(r2)" in text
    assert "check[step s1]" in text
    assert "category=ctf-pwn" in text
    assert "沙箱 needed=True available=False" in text
    assert "工具缺失 1: gdb(gdb)" in text
    assert "环境检查: 缺工具 5/70" in text
    assert "manual 4" in text
    assert "sandbox=无" in text


def test_logger_env_check_step_skips_no_tools(tmp_path):
    """step 作用域无缺工具/无分类 → 不写行(静默)。"""
    from agent.logging import EngineLogger

    log = EngineLogger(tmp_path)
    log.on_run_started(task={})
    log.on_env_check(scope="step", step_id="s1", report={"tools": [], "category": {}})
    log.on_run_end(state="DONE", fail_reason=None, total_cycles=1)
    text = (tmp_path / "run.log").read_text(encoding="utf-8")
    assert "check[step s1]" not in text


# ===== 接线:task_understanding challenge_type 接入 run_start 分类探测 =====


class _ReportCollector:
    def __init__(self):
        self.start_reports = []

    def on_env_check(self, scope="", step_id=None, report=None, **kw):
        if scope == "run_start":
            self.start_reports.append(report)


def test_engine_run_start_probes_challenge_type_category():
    """raw 带 challenge_type 时,run_start 快照追加该分类就绪度探测。"""
    collector = _ReportCollector()
    evaluator = MockEvaluator({
        "evaluator_plan": EvalResult(Verdict.PASS, "计划可执行"),
        "evaluator_step": EvalResult(Verdict.PASS, "s1: 完成"),
        "evaluator_task": EvalResult(Verdict.DONE, "反思: 无问题"),
    })
    engine = Engine(_StepPlanner(), MockExecutor(observation="执行完成"), evaluator,
                    workspace=MockWorkspace(), checker=StubChecker())
    engine.signals.subscribe(collector)
    engine.run({**MOCK_TASK, "challenge_type": "ctf-pwn"})

    assert len(collector.start_reports) == 1
    rep = collector.start_reports[0]
    assert rep["category"]["category"] == "ctf-pwn"     # 接线:题型分类探测进了 run_start
    assert rep["category"]["compatibility"] == "Requires X"


def test_engine_run_start_no_challenge_type_no_category_probe():
    """raw 无 challenge_type → 不追加分类探测(纯全量快照)。"""
    collector = _ReportCollector()
    evaluator = MockEvaluator({
        "evaluator_plan": EvalResult(Verdict.PASS, "计划可执行"),
        "evaluator_step": EvalResult(Verdict.PASS, "s1: 完成"),
        "evaluator_task": EvalResult(Verdict.DONE, "反思: 无问题"),
    })
    engine = Engine(_StepPlanner(), MockExecutor(observation="执行完成"), evaluator,
                    workspace=MockWorkspace(), checker=StubChecker())
    engine.signals.subscribe(collector)
    engine.run(dict(MOCK_TASK))

    assert len(collector.start_reports) == 1
    assert "category" not in collector.start_reports[0]


def test_logger_run_start_writes_challenge_type_category(tmp_path):
    """run_start 带 category → log 写题型分类就绪度行。"""
    from agent.logging import EngineLogger

    log = EngineLogger(tmp_path)
    log.on_run_started(task={})
    log.on_env_check(scope="run_start", report={
        "total": 70, "available": 60, "missing": 5, "manual": 4, "unknown": 1,
        "missing_list": [],
        "sandbox": {"category": "ctf-pwn", "needed": True, "available": False},
        "category": {"category": "ctf-pwn", "exists": True,
                     "compatibility": "Requires X", "allowed_tools": ["Bash"],
                     "install_cmds": ["apt install gdb"],
                     "sandbox": {"category": "ctf-pwn", "needed": True, "available": False}},
    })
    log.on_run_end(state="DONE", fail_reason=None, total_cycles=1)
    text = (tmp_path / "run.log").read_text(encoding="utf-8")

    assert "check[run_start] 题型分类就绪度: category=ctf-pwn" in text
    assert 'compat="Requires X"' in text
    assert "沙箱 needed=True available=False" in text
