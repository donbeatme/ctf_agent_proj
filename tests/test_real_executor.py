"""RealExecutor:LLM 工具循环 → 命令路由 → ExecResult。

覆盖:
- trace 归一为 {tool, args, result} + result 提取 flag/answer
- 复合 tool_exec:run_command/run_python/submit_flag 走执行侧,其余委托引擎注入
- 分类从 step.skill_id / workspace.challenge_type 解析并传入 runner
- LLM 异常 → 失败 observation,不崩
- engine 集成:RealExecutor 接 Executor 契约跑通到 DONE,submitted_flag 落账
"""

from agent.blueprint import Blueprint, Step
from agent.engine import Engine, EngineState
from agent.evaluator import EvalResult, MockEvaluator, Verdict
from agent.executor import ExecResult, RealExecutor
from agent.llm_api import ToolResult
from agent.schema import PlannerMode, Role, parse_plan
from agent.workspace import MockWorkspace
from tests.mock_data import MOCK_TASK


class _FakeRunner:
    """记录 runner 调用,固定返回成功 RunOutcome(不真执行)。"""

    def __init__(self):
        self.calls = []
        self.python_calls = []

    def run(self, cmd, **kw):
        self.calls.append((cmd, kw))
        return _outcome("ssh", cmd)

    def run_python(self, code, **kw):
        self.python_calls.append((code, kw))
        return _outcome("ssh", ["python", "script"])


def _outcome(target, cmd):
    from agent.runner import RunOutcome

    return RunOutcome(ok=True, returncode=0, stdout="ok", stderr="", cmd=cmd, target=target)


def _tool_result(*trace, content="完成", rounds=2):
    return ToolResult(content=content, trace=list(trace), rounds=rounds,
                      total_usage={"prompt_tokens": 1, "completion_tokens": 1,
                                   "total_tokens": 2})


def _tr(name, arguments, result):
    return {"name": name, "arguments": arguments, "result": result}


# ===== trace 归一与 result 提取 =====


def test_run_normalizes_trace_and_extracts_flag():
    def llm(*, system, prompt, tools, tool_exec, **kw):
        assert "run_command" in [s["function"]["name"] for s in tools]
        return _tool_result(
            _tr("run_command", '{"command": "echo hi"}', {"ok": True, "stdout": "hi"}),
            _tr("submit_flag", '{"flag": "CTF{x}"}', {"submitted": True}),
        )

    ex = RealExecutor(llm_fn=llm, runner=_FakeRunner())
    res = ex.run(None, "ctx")
    assert isinstance(res, ExecResult)
    assert res.tool_calls == [
        {"tool": "run_command", "args": {"command": "echo hi"}, "result": {"ok": True, "stdout": "hi"}},
        {"tool": "submit_flag", "args": {"flag": "CTF{x}"}, "result": {"submitted": True}},
    ]
    assert res.result == {"flag": "CTF{x}"}
    assert "2 轮" in res.observation
    assert res.total_usage["total_tokens"] == 2


def test_run_extracts_answer_when_no_flag():
    def llm(*, system, prompt, tools, tool_exec, **kw):
        return _tool_result(_tr("answer", '{"text": "结论: 明文为 hello"}', {"answer": "ok"}))

    res = RealExecutor(llm_fn=llm, runner=_FakeRunner()).run(None, "ctx")
    assert res.result == {"answer": "结论: 明文为 hello"}
    assert res.tool_calls[0]["tool"] == "answer"


# ===== 复合 tool_exec =====


def test_exec_tool_delegates_builtins_to_engine():
    calls = []

    def llm(*, system, prompt, tools, tool_exec, **kw):
        # 执行侧工具本地处理;其余委托引擎注入的 tool_exec
        r1 = tool_exec("run_command", {"command": "ls"})
        r2 = tool_exec("get_doc", {"doc_id": "ctf-pwn"})
        r3 = tool_exec("submit_flag", {"flag": "CTF{flag}"})
        calls.append((r1, r2, r3))
        # 真实 chat_with_tools 会把每次 tool_exec 结果追加进 trace
        return _tool_result(_tr("submit_flag", '{"flag": "CTF{flag}"}',
                                {"submitted": True}))

    def engine_tool_exec(name, args):
        if name == "get_doc":
            return {"doc_id": "ctf-pwn", "content": "doc"}
        return {"error": f"unknown: {name}"}

    ws = MockWorkspace()
    ws.meta["task"] = {"challenge_dir": "D:/chal"}
    ex = RealExecutor(llm_fn=llm, runner=_FakeRunner(), workspace=ws)
    res = ex.run(None, "ctx", tool_exec=engine_tool_exec)
    r1, r2, r3 = calls[0]
    assert "stdout" in r1                      # run_command 走 runner
    assert r2 == {"doc_id": "ctf-pwn", "content": "doc"}  # get_doc 委托引擎
    assert r3 == {"submitted": True, "flag": "CTF{flag}"}  # submit_flag 本地
    assert res.result == {"flag": "CTF{flag}"}


def test_run_command_forwards_category_and_tool_id():
    captured = {}

    def llm(*, system, prompt, tools, tool_exec, **kw):
        captured["ret"] = tool_exec("run_command",
                                    {"command": "gdb -q ./pwn1", "tool_id": "gdb"})
        return _tool_result(_tr("run_command", '{"command": "gdb -q ./pwn1"}', "ok"))

    runner = _FakeRunner()
    ws = MockWorkspace()
    ws.meta["task"] = {"challenge_dir": "D:/chal"}
    ex = RealExecutor(llm_fn=llm, runner=runner, workspace=ws)
    step = Step(id="s1", instruction="调试", criterion="拿到泄漏", skill_id="ctf-pwn.exploit")
    ex.run(step, "ctx")
    cmd, kw = runner.calls[0]
    assert cmd == "gdb -q ./pwn1"
    assert kw["category"] == "ctf-pwn"        # 从 skill_id 前缀解析
    assert kw["tool_id"] == "gdb"


def test_run_python_forwards_tool_id():
    def llm(*, system, prompt, tools, tool_exec, **kw):
        tool_exec("run_python", {"code": "from pwn import *", "tool_id": "pwntools"})
        return _tool_result(_tr("run_python", '{"code": "x"}', "ok"))

    runner = _FakeRunner()
    ws = MockWorkspace()
    ws.meta["task"] = {"challenge_dir": "D:/chal"}
    RealExecutor(llm_fn=llm, runner=runner, workspace=ws).run(None, "ctx")
    code, kw = runner.python_calls[0]
    assert code == "from pwn import *"
    assert kw["tool_id"] == "pwntools"


def test_category_falls_back_to_workspace_challenge_type():
    captured = {}

    def llm(*, system, prompt, tools, tool_exec, **kw):
        captured["ret"] = tool_exec("run_command", {"command": "strings a.bin"})
        return _tool_result(_tr("run_command", '{"command": "strings a.bin"}', "ok"))

    ws = MockWorkspace()
    ws.meta["task"] = {"challenge_type": "ctf-reverse", "challenge_dir": "D:/chal"}
    runner = _FakeRunner()
    RealExecutor(llm_fn=llm, runner=runner, workspace=ws).run(None, "ctx")
    assert runner.calls[0][1]["category"] == "ctf-reverse"


# ===== submit_flag → adapter.submit 提交回环 =====


class _FakeAdapter:
    """记录 submit 调用,固定返回 SubmitResult(证明 executor→adapter 接线)。"""

    def __init__(self, ok=True, correct=True, message="ok"):
        self.calls = []
        self._ok, self._correct, self._message = ok, correct, message

    def submit(self, challenge_id, flag):
        from ctf_platform.base import SubmitResult

        self.calls.append((challenge_id, flag))
        return SubmitResult(ok=self._ok, correct=self._correct, message=self._message)


def _submit_exe(llm, *, adapter=None, task=None):
    ws = MockWorkspace()
    if task:
        ws.meta["task"] = task
    return RealExecutor(llm_fn=llm, runner=_FakeRunner(), workspace=ws, adapter=adapter)


def test_submit_flag_calls_adapter_and_returns_verdict():
    seen = {}

    def llm(*, system, prompt, tools, tool_exec, **kw):
        seen["r"] = tool_exec("submit_flag", {"flag": "CTF{x}"})
        return _tool_result(_tr("submit_flag", '{"flag": "CTF{x}"}', seen["r"]))

    adapter = _FakeAdapter()
    ex = _submit_exe(llm, adapter=adapter, task={"challenge_id": "c-1"})
    res = ex.run(None, "ctx")
    assert adapter.calls == [("c-1", "CTF{x}")]          # 真调 adapter
    assert seen["r"] == {"submitted": True, "flag": "CTF{x}",
                         "ok": True, "correct": True, "message": "ok"}
    assert res.result == {"flag": "CTF{x}"}              # flag 仍进 result → engine 提取


def test_submit_flag_record_only_when_no_adapter():
    seen = {}

    def llm(*, system, prompt, tools, tool_exec, **kw):
        seen["r"] = tool_exec("submit_flag", {"flag": "CTF{x}"})
        return _tool_result(_tr("submit_flag", '{"flag": "CTF{x}"}', seen["r"]))

    ex = _submit_exe(llm, task={"challenge_id": "c-1"})   # 无 adapter
    ex.run(None, "ctx")
    assert seen["r"] == {"submitted": True, "flag": "CTF{x}"}   # 历史行为:仅记录


def test_submit_flag_record_only_when_no_challenge_id():
    seen = {}

    def llm(*, system, prompt, tools, tool_exec, **kw):
        seen["r"] = tool_exec("submit_flag", {"flag": "CTF{x}"})
        return _tool_result(_tr("submit_flag", '{"flag": "CTF{x}"}', seen["r"]))

    adapter = _FakeAdapter()
    ex = _submit_exe(llm, adapter=adapter)                 # task 无 challenge_id
    ex.run(None, "ctx")
    assert adapter.calls == []                            # 无 id 不提交
    assert seen["r"]["correct"] is None
    assert "缺少 challenge_id" in seen["r"]["message"]


def test_submit_flag_adapter_exception_returns_failure():
    seen = {}

    def llm(*, system, prompt, tools, tool_exec, **kw):
        seen["r"] = tool_exec("submit_flag", {"flag": "CTF{x}"})
        return _tool_result(_tr("submit_flag", '{"flag": "CTF{x}"}', seen["r"]))

    class _Boom:
        def submit(self, challenge_id, flag):
            raise RuntimeError("平台挂了")

    ex = _submit_exe(llm, adapter=_Boom(), task={"id": "c-9"})
    ex.run(None, "ctx")
    assert seen["r"]["ok"] is False                      # 失败降级,不崩
    assert seen["r"]["correct"] is None
    assert "提交异常" in seen["r"]["message"]


# ===== 目标地址(靶机)注入 =====


class _TargetAdapter:
    """记录 start_target 调用,固定返回已就绪的靶机信息。"""

    def __init__(self, host="tcp.example.com", port=9999):
        self.starts = []
        self._host, self._port = host, port

    def start_target(self, challenge_id):
        self.starts.append(challenge_id)
        return {"host": self._host, "port": self._port,
                "access_url": f"{self._host}:{self._port}", "status": "running"}


def _capture_prompt(task=None, adapter=None):
    seen = {}

    def llm(*, system, prompt, tools, tool_exec, **kw):
        seen["prompt"] = prompt
        return _tool_result(_tr("answer", '{"text": "ok"}', {"answer": "ok"}))

    ws = MockWorkspace()
    if task:
        ws.meta["task"] = task
    ex = RealExecutor(llm_fn=llm, runner=_FakeRunner(), workspace=ws, adapter=adapter)
    ex.run(None, "ctx")
    return seen["prompt"]


def test_target_injected_from_task_target_info():
    task = {"challenge_id": "c-1",
            "target_info": {"kind": "host_port", "host": "abc.tcp-ctf2.dasctf.com",
                            "port": 9999, "source": "target"}}
    prompt = _capture_prompt(task=task)
    assert "# 目标地址(靶机)" in prompt
    assert "abc.tcp-ctf2.dasctf.com:9999" in prompt


def test_target_injected_from_task_target_string():
    prompt = _capture_prompt(task={"target": "1.2.3.4:31337"})
    assert "# 目标地址(靶机)" in prompt
    assert "1.2.3.4:31337" in prompt


def test_target_fmt_url_kind():
    task = {"target_info": {"kind": "url", "scheme": "http", "host": "10.0.0.5", "port": 80}}
    assert "http://10.0.0.5" in _capture_prompt(task=task)


def test_target_lazy_start_when_container_and_adapter():
    adapter = _TargetAdapter()
    task = {"challenge_id": "c-77", "has_container": 1}   # 无 target → 惰性开靶
    prompt = _capture_prompt(task=task, adapter=adapter)
    assert adapter.starts == ["c-77"]
    assert "tcp.example.com:9999" in prompt


def test_target_resolved_once_per_executor():
    """单 Executor 多步只开一次靶(缓存),不重复消耗靶机配额。"""
    seen = {}

    def llm(*, system, prompt, tools, tool_exec, **kw):
        seen["prompt"] = prompt
        return _tool_result(_tr("answer", '{"text": "ok"}', {"answer": "ok"}))

    adapter = _TargetAdapter()
    ws = MockWorkspace()
    ws.meta["task"] = {"challenge_id": "c-1", "has_container": 1}
    ex = RealExecutor(llm_fn=llm, runner=_FakeRunner(), workspace=ws, adapter=adapter)
    ex.run(None, "ctx")
    ex.run(None, "ctx")   # 第二次 run 不再 start_target
    assert adapter.starts == ["c-1"]


def test_target_none_without_container_no_target():
    prompt = _capture_prompt(task={"challenge_id": "c-1"})   # 非容器、无 target
    assert "# 目标地址(靶机)" not in prompt


# ===== cwd 收口:只能指向题目附件目录 =====


def _cwd_exe(ws=None, workdir=None):
    return RealExecutor(llm_fn=lambda **kw: None, runner=_FakeRunner(),
                        workspace=ws or MockWorkspace(), workdir=workdir)


def test_cwd_defaults_to_workdir(tmp_path):
    ex = _cwd_exe(workdir=str(tmp_path))
    assert ex._cwd({}) == str(tmp_path.resolve())


def test_cwd_defaults_to_meta_challenge_dir(tmp_path):
    ws = MockWorkspace()
    ws.meta["task"] = {"challenge_dir": str(tmp_path / "chal")}
    ex = _cwd_exe(ws=ws)
    assert ex._cwd({}) == str((tmp_path / "chal").resolve())


def test_cwd_accepts_subdir_inside_allowed(tmp_path):
    (tmp_path / "sub").mkdir()
    ex = _cwd_exe(workdir=str(tmp_path))
    assert ex._cwd({"cwd": str(tmp_path / "sub")}) == str((tmp_path / "sub").resolve())


def test_cwd_rejects_outside_allowed(tmp_path):
    ex = _cwd_exe(workdir=str(tmp_path))
    try:
        ex._cwd({"cwd": "C:/Windows"})
        raise AssertionError("目录外 cwd 应当被拒绝")
    except ValueError as exc:
        assert "题目附件目录" in str(exc)


def test_cwd_unknown_root_raises():
    ex = _cwd_exe()  # 无 workdir、meta 也无 challenge_dir
    try:
        ex._cwd({})
        raise AssertionError("无法确定允许根应当抛错")
    except ValueError as exc:
        assert "无法确定题目附件目录" in str(exc)


def test_run_command_outside_cwd_returns_error():
    """LLM 传目录外 cwd → _run_command 捕获 ValueError 返回 {"error":...},不执行。"""
    seen = {}

    def llm(*, system, prompt, tools, tool_exec, **kw):
        seen["r"] = tool_exec("run_command", {"command": "cat token.txt", "cwd": "C:/Users"})
        return _tool_result(_tr("run_command", '{"command": "cat token.txt"}', seen["r"]))

    ws = MockWorkspace()
    ws.meta["task"] = {"challenge_dir": "D:/chal"}
    runner = _FakeRunner()
    ex = RealExecutor(llm_fn=llm, runner=runner, workspace=ws)
    ex.run(None, "ctx")
    assert "error" in seen["r"]
    assert "题目附件目录" in seen["r"]["error"]
    assert runner.calls == []  # 被拒,未进 runner


def test_run_python_outside_cwd_returns_error():
    seen = {}

    def llm(*, system, prompt, tools, tool_exec, **kw):
        seen["r"] = tool_exec("run_python", {"code": "print(1)", "cwd": "C:/"})
        return _tool_result(_tr("run_python", '{"code": "print(1)"}', seen["r"]))

    ws = MockWorkspace()
    ws.meta["task"] = {"challenge_dir": "D:/chal"}
    runner = _FakeRunner()
    ex = RealExecutor(llm_fn=llm, runner=runner, workspace=ws)
    ex.run(None, "ctx")
    assert "error" in seen["r"]
    assert runner.python_calls == []


# ===== 异常保护 =====


def test_llm_exception_returns_error_observation():
    def llm(**kw):
        raise RuntimeError("boom")

    res = RealExecutor(llm_fn=llm, runner=_FakeRunner()).run(None, "ctx")
    assert isinstance(res, ExecResult)
    assert "异常" in res.observation
    assert res.tool_calls is None


def test_tool_loop_error_preserves_partial_trace():
    """工具循环超上限:部分轨迹保留进 ExecResult.tool_calls(喂 run.log/events/flag 提取)。"""
    from agent.llm_api import ToolLoopError

    def llm(*, system, prompt, tools, tool_exec, **kw):
        tool_exec("run_command", {"command": "strings a.bin"})
        raise ToolLoopError("工具循环超过上限 8 轮", trace=[
            {"name": "run_command", "arguments": '{"command": "strings a.bin"}',
             "result": {"ok": True, "stdout": "CTF{partial}"}},
            {"name": "submit_flag", "arguments": '{"flag": "CTF{partial}"}',
             "result": {"submitted": True}},
        ])

    res = RealExecutor(llm_fn=llm, runner=_FakeRunner()).run(None, "ctx")
    assert isinstance(res, ExecResult)
    assert res.tool_calls == [
        {"tool": "run_command", "args": {"command": "strings a.bin"},
         "result": {"ok": True, "stdout": "CTF{partial}"}},
        {"tool": "submit_flag", "args": {"flag": "CTF{partial}"},
         "result": {"submitted": True}},
    ]
    assert res.result == {"flag": "CTF{partial}"}   # flag 仍进 result → engine 提取
    assert "超上限" in res.observation


def test_llm_missing_trace_returns_empty():
    def llm(*, system, prompt, tools, tool_exec, **kw):
        return ToolResult(content="直接给出结论", trace=[], rounds=1, total_usage=None)

    res = RealExecutor(llm_fn=llm, runner=_FakeRunner()).run(None, "ctx")
    assert res.tool_calls is None
    assert "直接给出结论" in res.observation


# ===== engine 集成 =====


class _Plan:
    """按序消费预置 PlanPatch JSON 的 planner。"""

    def __init__(self, *responses):
        self._r = list(responses)
        self.calls = 0

    def plan(self, pin):
        bp = Blueprint.from_dict(pin.feedback.dag) if pin.mode == PlannerMode.REVISE \
            else Blueprint(meta={"task": MOCK_TASK})
        resp = self._r[min(self.calls, len(self._r) - 1)]
        self.calls += 1
        bp.apply_patch(parse_plan(resp).to_patch())
        return bp


def _seq(results):
    state = {"i": 0}

    def fn(ctx):
        r = results[min(state["i"], len(results) - 1)]
        state["i"] += 1
        return r

    return fn


def _resp(*bodies):
    """把 add 数组包成 PlanPatch JSON(与 test_engine._plan_responses 一致)。"""
    return ['{"add":' + bodies[0] + ',"reason":"initial"}'] + list(bodies[1:])


def test_real_executor_engine_integration_done():
    def llm(*, system, prompt, tools, tool_exec, **kw):
        tool_exec("run_command", {"command": "echo hi"})
        tool_exec("submit_flag", {"flag": "CTF{real}"})
        return _tool_result(
            _tr("run_command", '{"command": "echo hi"}', "ok"),
            _tr("submit_flag", '{"flag": "CTF{real}"}', {"submitted": True}),
        )

    planner = _Plan(*_resp(
        '[{"id":"s1","instruction":"读题","criterion":"c","depends_on":[]}]',
        '{}',
    ))
    evaluator = MockEvaluator({
        "evaluator_plan": _seq([EvalResult(Verdict.PASS, "计划可执行")]),
        "evaluator_step": _seq([EvalResult(Verdict.PASS, "s1: 完成")]),
        "evaluator_task": _seq([EvalResult(Verdict.DONE, "反思: 无问题")]),
    })
    engine = Engine(planner, RealExecutor(llm_fn=llm, runner=_FakeRunner()),
                    evaluator, workspace=MockWorkspace())
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    assert engine.submitted_flag == "CTF{real}"
    assert engine.current is None


def test_engine_records_submission_verdict_to_ee():
    """executor 提交 flag(adapter 判定 correct=True)→ engine 落 ws.meta["submission"],
    ee 上下文可见提交判定(ee 判 is_completed/DONE 的核心证据)。"""

    def llm(*, system, prompt, tools, tool_exec, **kw):
        tool_exec("submit_flag", {"flag": "CTF{ok}"})
        return _tool_result(
            _tr("submit_flag", '{"flag": "CTF{ok}"}',
                {"submitted": True, "ok": True, "correct": True, "message": "success"}),
        )

    planner = _Plan(*_resp(
        '[{"id":"s1","instruction":"读题","criterion":"c","depends_on":[]}]',
        '{}',
    ))
    evaluator = MockEvaluator({
        "evaluator_plan": _seq([EvalResult(Verdict.PASS, "计划可执行")]),
        "evaluator_step": _seq([EvalResult(Verdict.PASS, "s1: 提交被平台确认,完成",
                                           is_completed=True)]),
        "evaluator_task": _seq([EvalResult(Verdict.DONE, "反思: 无问题")]),
    })
    ws = MockWorkspace()
    ws.meta["task"] = {"challenge_id": "c-1"}
    adapter = _FakeAdapter(correct=True, message="success")
    engine = Engine(
        planner,
        RealExecutor(llm_fn=llm, runner=_FakeRunner(), workspace=ws, adapter=adapter),
        evaluator,
        workspace=ws,
    )
    engine.run(MOCK_TASK)
    assert engine.scheduler.state == EngineState.DONE
    sub = ws.meta.get("submission")
    assert sub is not None
    assert sub["flag"] == "CTF{ok}"
    assert sub["correct"] is True
    assert sub["message"] == "success"
    # ee 上下文经 SubmissionComponent 投影出提交判定
    ctx, _, _ = ws.assembler.assemble(Role.EVALUATOR_STEP)
    assert "# 已提交 flag" in ctx
    assert "正确(平台确认)" in ctx
