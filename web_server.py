"""CTF Agent 前端 HTTP 服务:包装现有 Engine / Workspace / Skills / 配置。

对接点:
- 启动:演示模式用 MockExecutor;真实模式自动启动本地 VM 并接 RealExecutor + SSH 沙箱
- 信号: Engine(subscribers=[LiveBridge]) → SignalBus
- 停跑: engine.request_stop()
- 续跑: Engine.resume(..., subscribers=[...])
- 历史: runs/<run_id>/{state.json,events.jsonl,run.log}
- 技能: SkillLibrary / CtfSkillToolCatalog / SkillEnvProbe
- 配置: model_config.get/set/reload
"""

from __future__ import annotations

import asyncio
import json
import shlex
import shutil
import subprocess
import threading
import time
import traceback
from collections import deque
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
RUNS_DIR = ROOT / "runs"
UPLOAD_DIR = ROOT / "downloads" / "uploads"
VM_SCRIPT = ROOT / "scripts" / "match_vm.sh"

_live: dict[str, dict] = {}
_live_lock = threading.Lock()
_lib = None
_catalog = None


def _skills():
    global _lib
    if _lib is None:
        from agent.skills import SkillLibrary
        _lib = SkillLibrary()
    return _lib


def _tools():
    global _catalog
    if _catalog is None:
        from agent.ctf_skill_tools import CtfSkillToolCatalog
        _catalog = CtfSkillToolCatalog()
    return _catalog


def _platform_adapter():
    from ctf_platform.config import StoreSettings
    from ctf_platform.ctf2 import Ctf2Adapter

    return Ctf2Adapter(StoreSettings.from_env())


def _challenge_count(store) -> int:
    row = store.conn.execute("SELECT COUNT(*) AS n FROM challenges").fetchone()
    return int(row["n"] if row and "n" in row.keys() else 0)


def _platform_status() -> dict:
    from ctf_platform.config import StoreSettings

    settings = StoreSettings.from_env()
    adapter = _platform_adapter()
    recent = adapter.store.query_challenges(limit=12)
    return {
        "configured": bool(settings.ctf2_session_token or settings.ctf2_cookie),
        "api_key_set": bool(settings.ctf2_api_key),
        "base_url": settings.ctf2_base_url,
        "origin": settings.ctf2_origin,
        "practice_ground_id": settings.ctf2_practice_ground_id,
        "auto_start_target": settings.ctf2_auto_start_target,
        "store_dir": str(settings.store_dir),
        "challenges_dir": str(settings.challenges_dir),
        "challenge_count": _challenge_count(adapter.store),
        "cache": adapter.cache_stats(),
        "recent": recent,
    }


def _understand_payload(raw: dict) -> dict:
    from task_understanding.real_understander import RealTaskUnderstander

    task_input = RealTaskUnderstander().understand(raw)
    task = jsonable(task_input.raw_content)
    goals = jsonable(task_input.goal_list)
    return {
        "task": task,
        "goal_list": goals,
        "goals_preview": [
            (g.get("id") if isinstance(g, dict) else getattr(g, "id", str(g)))
            for g in goals
        ],
        "classification": {
            "primary": task.get("challenge_type"),
            "label": task.get("challenge_type_label"),
            "confidence": task.get("type_confidence"),
            "ranked": [
                {
                    "category": s.get("category"),
                    "label": s.get("label"),
                    "score": s.get("score"),
                    "evidence": s.get("evidence") or [],
                }
                for s in (task.get("type_scores") or [])
            ],
        },
        "source": "RealTaskUnderstander",
    }


def _platform_fetch(body: dict) -> dict:
    adapter = _platform_adapter()
    source = body.get("source")
    if source is None:
        source = body.get("url") or body.get("challenge_id") or body.get("friendly_id")
    if isinstance(source, str):
        text = source.strip()
        if text.startswith("{") or text.startswith("["):
            source = json.loads(text)
        elif Path(text).exists():
            source = json.loads(Path(text).read_text(encoding="utf-8"))
    dest = body.get("dest_dir") or body.get("dest")
    if dest:
        resolved_dest = Path(str(dest)).resolve()
        try:
            resolved_dest.relative_to(ROOT)
        except ValueError as exc:
            raise ValueError("题目下载目录必须位于 Match 项目内") from exc
        dest = str(resolved_dest)
    challenge_dir = adapter.ingest(source, dest_dir=dest)
    try:
        Path(challenge_dir).resolve().relative_to(ROOT)
    except ValueError as exc:
        raise RuntimeError("平台题目目录不在 Match 项目内，已拒绝继续处理") from exc
    understood = _understand_payload({"challenge_dir": str(challenge_dir)})
    return {
        "ok": True,
        "challenge_dir": str(challenge_dir),
        "platform": adapter.platform,
        "understood": understood,
        "status": _platform_status(),
    }


def _probe_ssh_runtime(settings) -> dict:
    """Verify the configured SSH endpoint, Docker daemon, and sandbox image."""
    from agent.ssh import SshBackend

    async def probe():
        ssh = SshBackend(
            host=settings.ssh_host,
            port=settings.ssh_port,
            user=settings.ssh_user,
            password=settings.ssh_password,
            workdir=settings.ssh_workdir,
            ssh_key=settings.ssh_key,
            host_key=settings.ssh_host_key,
        )
        try:
            image = shlex.quote(settings.image)
            result = await ssh.exec(
                f"docker info >/dev/null 2>&1 && "
                f"docker image inspect {image} >/dev/null 2>&1",
                timeout=30,
            )
        finally:
            await ssh.close()
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace").strip()[:300]
            raise RuntimeError(
                f"SSH/Docker 探测失败 rc={result.returncode}"
                + (f": {detail}" if detail else "")
            )
        return {
            "ready": True,
            "host": settings.ssh_host,
            "port": settings.ssh_port,
            "user": settings.ssh_user,
            "image": settings.image,
        }

    return asyncio.run(probe())


def _ensure_vm_runtime(run_id: str, settings=None) -> dict:
    """Start the isolated local Lima VM when applicable, then probe SSH/Docker."""
    from opslog import emit
    from sandbox_env.config import SandboxSettings

    settings = settings or SandboxSettings.from_env()
    if not settings.ssh_configured:
        raise RuntimeError("真实执行需要先配置 CTF_SSH_HOST")

    local_vm = settings.ssh_host in {"127.0.0.1", "localhost", "::1"}
    if local_vm:
        if not VM_SCRIPT.is_file():
            raise RuntimeError(f"VM 启动脚本不存在: {VM_SCRIPT}")
        emit("vm", "start_requested", run_id=run_id, script=str(VM_SCRIPT))
        result = subprocess.run(
            [str(VM_SCRIPT), "start"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=30 * 60,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-500:]
            raise RuntimeError(f"Lima VM 启动失败 rc={result.returncode}: {detail}")
        emit("vm", "started", run_id=run_id, host=settings.ssh_host,
             port=settings.ssh_port)

    runtime = _probe_ssh_runtime(settings)
    runtime["auto_started"] = local_vm
    emit("vm", "ready", run_id=run_id, **runtime)
    return runtime


def _sandbox_runtime(probe: bool = False) -> dict:
    from sandbox_env.config import SandboxSettings
    from sandbox_env.tools import ToolManager

    settings = SandboxSettings.from_env()
    out = {
        "configured": settings.ssh_configured,
        "backend": "ssh" if settings.ssh_configured else "local/unconfigured",
        "host": settings.ssh_host,
        "port": settings.ssh_port,
        "user": settings.ssh_user,
        "image": settings.image,
        "workdir": settings.ssh_workdir,
        "container_model": settings.container_model,
        "install_auto": settings.install_auto,
        "keep_container": settings.keep_container,
        "conflicts": ToolManager().tool_conflicts(),
    }
    if probe:
        try:
            out.update(_probe_ssh_runtime(settings))
        except Exception as exc:
            out["ready"] = False
            out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def jsonable(obj, *, depth=0, limit=8000):
    if depth > 8:
        return str(obj)[:limit]
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        return obj if len(obj) <= limit else obj[:limit] + "…"
    if isinstance(obj, dict):
        return {str(k): jsonable(v, depth=depth + 1, limit=limit) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(x, depth=depth + 1, limit=limit) for x in obj]
    if hasattr(obj, "model_dump"):
        return jsonable(obj.model_dump(), depth=depth + 1, limit=limit)
    if hasattr(obj, "value") and not isinstance(obj, type):
        try:
            return obj.value
        except Exception:
            pass
    if hasattr(obj, "__dict__") and not isinstance(obj, type):
        return jsonable(vars(obj), depth=depth + 1, limit=limit)
    return str(obj)[:limit]


class LiveBridge:
    """SignalBus 订阅者:on_<signal> 经 __getattr__ 接到环形缓冲,供前端轮询。"""

    def __init__(self, maxlen=400):
        self.buf = deque(maxlen=maxlen)
        self.lock = threading.Lock()
        self.seq = 0

    def __getattr__(self, name):
        if name.startswith("on_"):
            sig = name[3:]

            def handler(**kw):
                rec = {
                    "seq": 0,
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "signal": sig,
                    "data": jsonable(kw, limit=4000),
                }
                with self.lock:
                    self.seq += 1
                    rec["seq"] = self.seq
                    self.buf.append(rec)

            return handler
        raise AttributeError(name)

    def since(self, after: int) -> list[dict]:
        with self.lock:
            return [r for r in self.buf if r["seq"] > after]


def _snapshot_from_disk(run_id: str) -> dict | None:
    st_path = RUNS_DIR / run_id / "state.json"
    if not st_path.exists():
        return None
    try:
        st = json.loads(st_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    meta = st.get("meta") or {}
    bp = st.get("blueprint") or {}
    steps = (bp.get("steps") if isinstance(bp, dict) else None) or {}
    live = None
    with _live_lock:
        live = _live.get(run_id)
    status = meta.get("run_status", "PLANNING")
    if live and live.get("alive"):
        eng = live.get("engine")
        if eng is not None and getattr(eng, "scheduler", None):
            status = eng.scheduler.state.value
    return {
        "run_id": run_id,
        "status": status,
        "alive": bool(live and live.get("alive")),
        "error": (live or {}).get("error"),
        "fail_reason": meta.get("fail_reason"),
        "task": meta.get("task") or {},
        "goal_list": meta.get("goal_list") or [],
        "current_step": meta.get("current_step"),
        "run_tokens": meta.get("run_tokens", 0),
        "created_at": meta.get("created_at"),
        "blueprint": bp,
        "steps": st.get("steps") or {},
        "tools": st.get("tools") or {},
        "docs": list((st.get("docs") or {}).keys()),
        "step_count": len(steps),
        "replans": (live or {}).get("replans"),
        "execution_mode": (live or {}).get("execution_mode") or meta.get("execution_mode") or "demo",
        "actors": (live or {}).get("actors") or meta.get("actors") or 1,
        "phase": (live or {}).get("phase") or ("finished" if status in ("DONE", "FAILED") else "idle"),
        "runtime": (live or {}).get("runtime"),
    }


def _list_runs() -> list[dict]:
    if not RUNS_DIR.exists():
        return []
    items = []
    for p in sorted(RUNS_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not p.is_dir() or not (p / "state.json").exists():
            continue
        snap = _snapshot_from_disk(p.name)
        if snap:
            items.append({
                "run_id": snap["run_id"],
                "status": snap["status"],
                "alive": snap["alive"],
                "fail_reason": snap["fail_reason"],
                "task": snap["task"],
                "created_at": snap["created_at"],
                "step_count": snap["step_count"],
                "run_tokens": snap["run_tokens"],
                "execution_mode": snap["execution_mode"],
                "actors": snap["actors"],
                "phase": snap["phase"],
            })
    return items


def _read_events(run_id: str, after: int = 0, limit: int = 300) -> list[dict]:
    path = RUNS_DIR / run_id / "events.jsonl"
    if not path.exists():
        return []
    out = []
    idx = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        idx += 1
        if idx <= after:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        rec["_i"] = idx
        out.append(rec)
        if len(out) >= limit:
            break
    return out


def _read_log(run_id: str, tail: int = 200) -> str:
    path = RUNS_DIR / run_id / "run.log"
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(lines[-tail:])


def _validate_real_task(task: dict, settings=None) -> Path:
    from agent.llm_api import resolve_key
    from sandbox_env.config import SandboxSettings

    try:
        resolve_key()
    except Exception as exc:
        raise ValueError(
            "真实执行需要先在模型页面配置 API Key、Base URL 和模型名称"
        ) from exc

    settings = settings or SandboxSettings.from_env()
    if not settings.ssh_configured:
        raise ValueError("真实执行需要先配置 SSH 沙箱")

    workdir = task.get("challenge_dir") or task.get("workdir") or task.get("cwd")
    if not workdir:
        raise ValueError("真实执行需要先通过平台拉取题目，得到本地 challenge_dir")
    path = Path(str(workdir)).resolve()
    if not path.is_dir():
        raise ValueError(f"题目目录不存在: {path}")
    try:
        path.relative_to(ROOT)
    except ValueError as exc:
        raise ValueError("真实执行目录必须位于 Match 项目内，禁止同步外部仓库") from exc
    return path


def _build_real_scheduler(actors: int, settings=None):
    from agent.env_providers import SandboxProvider, SshProvider
    from agent.scheduler import ExecutionScheduler
    from sandbox_env.config import SandboxSettings

    actors = int(actors or 1)
    if actors < 1 or actors > 8:
        raise ValueError("并行 Agent 数必须在 1 到 8 之间")
    settings = settings or SandboxSettings.from_env()
    ssh = SshProvider(settings=settings, max_connections=actors)
    return ExecutionScheduler(providers=[SandboxProvider(ssh, settings=settings)])


def _close_scheduler(scheduler) -> None:
    if scheduler is None:
        return
    try:
        asyncio.run(scheduler.close())
    except Exception:
        pass


def _ops_sink(ws, engine, run_id):
    """Project this run's VM, SSH, sandbox, and adapter events into its ledger."""
    def sink(kind: str, detail: dict) -> None:
        event_run_id = detail.get("run_id")
        if event_run_id and event_run_id != run_id:
            return
        domain = detail.get("domain")
        if domain == "ws":
            return
        rec = dict(detail)
        rec.setdefault("run_id", run_id)
        if not (domain == "engine" and kind not in ("engine.run_started", "engine.run_ended")):
            try:
                ws.ingest_external(kind, rec)
            except Exception:
                pass
        active_engine = engine() if callable(engine) else engine
        if domain not in ("engine", "ws") and active_engine is not None:
            try:
                fields = "  ".join(
                    f"{key}={value}" for key, value in rec.items()
                    if key not in (
                        "ts", "domain", "event", "run_id", "seq",
                        "node_id", "round", "_uuid",
                    )
                )
                active_engine._log.engine_action(
                    f"ops[{kind}] run_id={run_id}  {fields}"
                )
            except Exception:
                pass

    return sink


def _make_agents(ws, execution_mode="demo", actors=1, settings=None):
    from agent.challenge_intake import ChallengeUnderstander
    from agent.ctf_skill_tools import CtfSkillToolCatalog
    from agent.evaluator import SmokeEvaluator, build_evaluator
    from agent.executor import MockExecutor, RealExecutor
    from agent.planner import Planner
    from agent.runner import CommandRunner
    from agent.skills import CtfSkillsDocStore

    planner = Planner(workspace=ws, docs=CtfSkillsDocStore())
    catalog = CtfSkillToolCatalog()
    if execution_mode == "demo":
        return {
            "planner": planner,
            "executor": MockExecutor(observation="(mock) 执行完成"),
            "evaluator": SmokeEvaluator(ws),
            "catalog": catalog,
            "understander": ChallengeUnderstander(),
            "scheduler": None,
            "compress": None,
        }
    if execution_mode != "real":
        raise ValueError(f"未知执行模式: {execution_mode}")

    from agent.llm_api import make_compress
    from task_understanding.real_understander import RealTaskUnderstander

    return {
        "planner": planner,
        "executor": RealExecutor(
            runner=CommandRunner(), workspace=ws, adapter=_platform_adapter()
        ),
        "evaluator": build_evaluator(ws),
        "catalog": catalog,
        "understander": RealTaskUnderstander(),
        "scheduler": _build_real_scheduler(actors, settings=settings),
        "compress": make_compress(),
    }


def _start_thread(run_id: str, fn, workspace=None, metadata=None):
    bridge = LiveBridge()
    slot = {
        "alive": True,
        "engine": None,
        "bridge": bridge,
        "error": None,
        "replans": 0,
        "phase": "queued",
        "runtime": None,
        **(metadata or {}),
    }
    with _live_lock:
        _live[run_id] = slot

    def worker():
        try:
            fn(slot, bridge)
        except Exception as exc:
            slot["error"] = f"{type(exc).__name__}: {exc}"
            slot["phase"] = "failed"
            if workspace is not None:
                try:
                    from agent.schema import Role

                    workspace.meta["run_status"] = "FAILED"
                    workspace.meta["fail_reason"] = slot["error"]
                    workspace.add_event(
                        Role.SYSTEM, "runtime_failed", error=slot["error"]
                    )
                    workspace.sync()
                except Exception:
                    pass
            traceback.print_exc()
        finally:
            slot["alive"] = False
            eng = slot.get("engine")
            if eng is not None:
                slot["replans"] = getattr(eng, "replans", 0)

    threading.Thread(target=worker, name=f"ctf-run-{run_id}", daemon=True).start()
    return slot


def start_run(task: dict, run_id: str | None = None, *, execution_mode="real",
              actors=1) -> str:
    from agent.engine import Engine
    from agent.workspace import Workspace
    from opslog import attach, detach

    execution_mode = str(execution_mode or "real").strip().lower()
    actors = int(actors or 1)
    if execution_mode == "real":
        _validate_real_task(task)
    elif execution_mode != "demo":
        raise ValueError("execution_mode 必须是 real 或 demo")
    run_id = run_id or f"run-{time.strftime('%Y%m%d-%H%M%S')}"
    ws = Workspace.create(
        run_id,
        task,
        meta={"execution_mode": execution_mode, "actors": actors},
    )

    def fn(slot, bridge):
        stack = _make_agents(ws, execution_mode=execution_mode, actors=actors)
        engine = Engine(
            stack["planner"], stack["executor"], stack["evaluator"],
            workspace=ws,
            tool_catalog=stack["catalog"],
            understander=stack["understander"],
            subscribers=[bridge],
            scheduler=stack["scheduler"],
            max_concurrency=actors,
            compress=stack["compress"],
        )
        slot["engine"] = engine
        sink = _ops_sink(ws, engine, run_id)
        attach(sink)
        try:
            if execution_mode == "real":
                slot["phase"] = "starting_vm"
                slot["runtime"] = _ensure_vm_runtime(run_id)
                slot["phase"] = "running"
            else:
                slot["phase"] = "demo"
            engine.run(task)
            slot["replans"] = engine.replans
            slot["phase"] = "finished"
        finally:
            _close_scheduler(stack["scheduler"])
            detach(sink)

    _start_thread(
        run_id,
        fn,
        workspace=ws,
        metadata={"execution_mode": execution_mode, "actors": actors},
    )
    return run_id


def resume_run(run_id: str) -> str:
    from agent.engine import Engine
    from agent.workspace import Workspace
    from opslog import attach, detach

    ws = Workspace.load(run_id)
    status = (ws.meta or {}).get("run_status")
    if status in ("DONE", "FAILED"):
        raise RuntimeError(f"终态 {status} 不能续跑")

    execution_mode = str((ws.meta or {}).get("execution_mode") or "demo")
    actors = int((ws.meta or {}).get("actors") or 1)
    task = (ws.meta or {}).get("task") or {}
    if execution_mode == "real":
        _validate_real_task(task)

    def fn(slot, bridge):
        stack = _make_agents(ws, execution_mode=execution_mode, actors=actors)
        engine_ref = {}
        sink = _ops_sink(ws, lambda: engine_ref.get("engine"), run_id)
        attach(sink)
        try:
            if execution_mode == "real":
                slot["phase"] = "starting_vm"
                slot["runtime"] = _ensure_vm_runtime(run_id)
                slot["phase"] = "running"
            else:
                slot["phase"] = "demo"

            def on_ready(engine):
                engine_ref["engine"] = engine
                slot["engine"] = engine

            engine = Engine.resume(
                run_id,
                stack["planner"],
                stack["executor"],
                stack["evaluator"],
                subscribers=[bridge],
                scheduler=stack["scheduler"],
                tool_catalog=stack["catalog"],
                compress=stack["compress"],
                max_concurrency=actors,
                on_ready=on_ready,
            )
            slot["replans"] = getattr(engine, "replans", 0)
            slot["phase"] = "finished"
        finally:
            _close_scheduler(stack["scheduler"])
            detach(sink)

    _start_thread(
        run_id,
        fn,
        workspace=ws,
        metadata={"execution_mode": execution_mode, "actors": actors},
    )
    return run_id


def stop_run(run_id: str) -> bool:
    with _live_lock:
        slot = _live.get(run_id)
    if not slot or not slot.get("alive") or not slot.get("engine"):
        return False
    slot["engine"].request_stop("用户停止")
    return True


def _config_public():
    import model_config
    from agent.llm_api import current_base_url, current_model, resolve_key

    model_config.reload()
    key_set = False
    try:
        resolve_key()
        key_set = True
    except Exception:
        pass
    return {
        "LLM_BASE_URL": current_base_url(),
        "LLM_MODEL": current_model(),
        "LLM_MODEL_PLANNER": model_config.get("LLM_MODEL_PLANNER") or current_model(),
        "LLM_ENABLE_TOOLS": model_config.get("LLM_ENABLE_TOOLS", True),
        "key_set": key_set,
        "engine": model_config.get_engine_config(),
    }


def _capabilities() -> dict:
    """能力地图:对接设计/contracts 的已接线 vs 桩 vs 前端预留。"""
    return {
        "layers": [
            {
                "id": "understand",
                "name": "任务理解层",
                "contract": "TaskUnderstander.understand(raw)→TaskInput",
                "status": "wired",
                "impl": "ChallengeUnderstander + RealTaskUnderstander",
                "note": "文本/JSON/附件启发识别与本地 challenge_dir 真实元数据解析均已接入前端",
            },
            {
                "id": "planner",
                "name": "规划 Agent",
                "contract": "Planner.plan(PlannerInput)→Blueprint",
                "status": "wired",
                "impl": "agent/planner.py + CtfSkillsDocStore",
                "note": "真 LLM;工具调用可经 LLM_ENABLE_TOOLS 关闭",
            },
            {
                "id": "executor",
                "name": "执行 Agent",
                "contract": "Executor.run(step, ctx, tool_exec=None)→ExecResult",
                "status": "wired",
                "impl": "MockExecutor / RealExecutor + CommandRunner",
                "note": "Web 默认真实执行:自动启动本地 VM，经 SSH 在任务独立 Docker 容器中运行；可显式切换演示模式",
            },
            {
                "id": "evaluator_plan",
                "name": "计划评审 ep",
                "contract": "Evaluator.review(ctx)→EvalResult",
                "status": "stub",
                "impl": "SmokeEvaluator / MockEvaluator",
                "note": "冒烟固定 PASS(空计划 FAIL)",
            },
            {
                "id": "evaluator_step",
                "name": "步骤验收 ee",
                "contract": "Evaluator.step_eval / eval_goals",
                "status": "wired_declare",
                "impl": "SmokeEvaluator + ctf_platform submit/local_check",
                "note": "前端 Flag 审核已对接本地答案库与平台 submit;Engine 内 ee 仍可继续深化",
            },
            {
                "id": "evaluator_task",
                "name": "任务反思 et",
                "contract": "Evaluator.reflect(ctx)→EvalResult",
                "status": "stub",
                "impl": "SmokeEvaluator / MockEvaluator",
                "note": "终局 DONE/REPLAN",
            },
            {
                "id": "platform",
                "name": "CTF 平台适配",
                "contract": "ChallengeAdapter.ingest/sync/submit/start_target",
                "status": "wired",
                "impl": "ctf_platform.Ctf2Adapter + ChallengeStore",
                "note": "前端已接 /api/platform/status、fetch、sync、target 与 /api/flag/verify",
            },
            {
                "id": "skills",
                "name": "技能库",
                "contract": "DocStore.search / load_doc",
                "status": "wired",
                "impl": "skills/ctf-skills + agent/skills.py",
                "note": "",
            },
            {
                "id": "tools",
                "name": "工具编排",
                "contract": "CtfSkillToolCatalog + apply_tool/remove_tool",
                "status": "wired_declare",
                "impl": "agent/ctf_skill_tools.py / tools.py",
                "note": "声明+探测已接线;真实调用属执行层②",
            },
            {
                "id": "env_check",
                "name": "环境/沙箱探测",
                "contract": "SkillEnvProbe + sandbox_env.SandboxManager",
                "status": "wired",
                "impl": "agent/checks.py + sandbox_env/*",
                "note": "只读工具探测、SSH/Pi 沙箱配置状态、工具冲突检测已接入",
            },
            {
                "id": "experience",
                "name": "经验沉淀 RAG",
                "contract": "ExperienceStore.query/record (contracts §6)",
                "status": "reserved",
                "impl": None,
                "note": "设计已声明;Engine 尚未接 experience_store 参数",
            },
            {
                "id": "flag_verify",
                "name": "Flag 验证",
                "contract": "POST /api/flag/verify → local_check | platform_submit",
                "status": "wired",
                "impl": "ChallengeAdapter.get_flag/submit",
                "note": "默认本地核验,勾选真实提交后才向平台提交",
            },
            {
                "id": "hitl",
                "name": "人机协同 / 升级接管",
                "contract": "未声明(仅 escalate 判定)",
                "status": "frontend_reserved",
                "impl": None,
                "note": "ee escalate 存在;人工审批通道前端预留",
            },
            {
                "id": "audit_report",
                "name": "审计报告交付",
                "contract": "events.jsonl + run.log → 报告",
                "status": "wired",
                "impl": "GET /api/runs/:id/report",
                "note": "由事件流组装;非独立 Agent",
            },
        ]
    }


def _build_report(run_id: str) -> dict | None:
    snap = _snapshot_from_disk(run_id)
    if snap is None:
        return None
    events = _read_events(run_id, after=0, limit=5000)
    steps = (snap.get("blueprint") or {}).get("steps") or {}
    product = {}
    for sid, sr in (snap.get("steps") or {}).items():
        if str(sr.get("verdict") or "").lower() == "pass":
            product[sid] = sr.get("result")
    verdicts = [e for e in events if e.get("kind") in (
        "plan_review", "step_eval", "reflect", "goal_eval")]
    tools = [e for e in events if e.get("kind") in ("use_tool", "tool_result")]
    lines = [
        f"# CTF Agent 审计报告",
        f"",
        f"- run_id: `{run_id}`",
        f"- 状态: **{snap.get('status')}**",
        f"- 题型: {((snap.get('task') or {}).get('challenge_type_label') or (snap.get('task') or {}).get('challenge_type') or '-')}",
        f"- tokens: {snap.get('run_tokens', 0)}",
        f"- fail_reason: {snap.get('fail_reason') or '无'}",
        f"",
        f"## 目标 goal_list",
    ]
    for g in snap.get("goal_list") or []:
        gid = g.get("id") if isinstance(g, dict) else g
        lines.append(f"- `{gid}`")
    if not snap.get("goal_list"):
        lines.append("- （空）")
    lines += ["", "## 计划 DAG"]
    for sid, s in steps.items():
        lines.append(
            f"- `{sid}` [{s.get('status')}] {s.get('instruction')} "
            f"(criterion={s.get('criterion')}; skill={s.get('skill_id')})"
        )
    if not steps:
        lines.append("- （无步骤）")
    lines += ["", "## 评估意见摘要"]
    for e in verdicts[-30:]:
        d = e.get("detail") or {}
        op = d.get("opinion") if isinstance(d, dict) else getattr(d, "opinion", "")
        lines.append(
            f"- {e.get('ts')} `{e.get('kind')}` {e.get('step_id') or ''} "
            f"→ {e.get('verdict') or ''} {op or ''}"
        )
    if not verdicts:
        lines.append("- （无）")
    lines += ["", "## 工具轨迹"]
    for e in tools[-40:]:
        d = e.get("detail") or {}
        tool = d.get("tool") if isinstance(d, dict) else ""
        lines.append(f"- {e.get('ts')} `{e.get('kind')}` {tool}")
    if not tools:
        lines.append("- （无工具调用记录）")
    lines += ["", "## 交付产物 product", f"```json", json.dumps(product, ensure_ascii=False, indent=2), "```"]
    return {
        "run_id": run_id,
        "status": snap.get("status"),
        "markdown": "\n".join(lines),
        "product": product,
        "goal_list": snap.get("goal_list") or [],
        "challenge_type": (snap.get("task") or {}).get("challenge_type"),
        "event_count": len(events),
        "step_count": len(steps),
    }


def _sandbox_status() -> dict:
    from agent.checks import SANDBOX_CATEGORIES, SkillEnvProbe
    probe = SkillEnvProbe(_tools())
    cats = {}
    for c in sorted(SANDBOX_CATEGORIES):
        cats[c] = probe.probe_sandbox(c)
    # 代表探测
    sample = next(iter(sorted(SANDBOX_CATEGORIES)), "ctf-pwn")
    return {
        "sandbox_categories": sorted(SANDBOX_CATEGORIES),
        "categories": cats,
        "runtime": probe.probe_sandbox(sample),
        "note": "只读探测 docker/podman CLI;创建沙箱属执行层②",
    }


def _reserved(name: str, contract: str, **extra) -> dict:
    return {
        "wired": False,
        "reserved": True,
        "endpoint": name,
        "contract": contract,
        "message": "接口未在引擎接线;前端已预留,待第二组交付后替换实现",
        **extra,
    }


def _save_config(body: dict):
    import model_config

    model_config.reload()
    mapping = {
        "LLM_BASE_URL": ("LLM_BASE_URL", "DEEPSEEK_BASE_URL"),
        "LLM_MODEL": ("LLM_MODEL", "DEEPSEEK_MODEL", "LLM_MODEL_PLANNER"),
        "LLM_MODEL_PLANNER": ("LLM_MODEL_PLANNER",),
        "LLM_ENABLE_TOOLS": ("LLM_ENABLE_TOOLS",),
    }
    for src, keys in mapping.items():
        if src in body and body[src] is not None and body[src] != "":
            for k in keys:
                model_config.set(k, body[src])
    if body.get("LLM_API_KEY"):
        model_config.set("LLM_API_KEY", body["LLM_API_KEY"])
        model_config.set("DEEPSEEK_API_KEY", body["LLM_API_KEY"])
    if isinstance(body.get("engine"), dict):
        model_config.reload()
        cfg = dict(model_config._config)
        eng = dict(cfg.get("engine") or {})
        eng.update(body["engine"])
        cfg["engine"] = eng
        model_config._config = cfg
        model_config.set("engine", eng)
    model_config.reload()
    return _config_public()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def log_message(self, fmt, *args):
        if self.path.startswith("/api/"):
            return
        super().log_message(fmt, *args)

    def _json(self, data, code=200):
        raw = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8") or "{}")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        q = parse_qs(parsed.query)
        if path == "/api/config":
            return self._json(_config_public())
        if path == "/api/skills":
            lib = _skills()
            qtext = (q.get("q") or [""])[0].lower()
            cat = (q.get("category") or [""])[0]
            items = []
            for meta in lib.catalog.values():
                if cat and meta.category != cat:
                    continue
                blob = f"{meta.doc_id} {meta.description}".lower()
                if qtext and qtext not in blob:
                    continue
                items.append({
                    "doc_id": meta.doc_id,
                    "description": meta.description,
                    "category": meta.category,
                    "kind": meta.kind,
                })
            return self._json({"categories": lib.categories(), "items": items})
        if path.startswith("/api/skills/") and path != "/api/skills/":
            doc_id = path.split("/api/skills/", 1)[1]
            text = _skills().load_doc(doc_id)
            if text is None:
                return self._json({"error": "未找到文档"}, 404)
            meta = _skills().catalog.get(doc_id)
            return self._json({
                "doc_id": doc_id,
                "description": meta.description if meta else "",
                "category": meta.category if meta else "",
                "kind": meta.kind if meta else "",
                "text": text,
            })
        if path == "/api/tools":
            cat = _tools()
            return self._json({
                "installer_path": str(cat.installer_path),
                "manifest": cat.manifest,
            })
        if path == "/api/env-check":
            from agent.checks import SkillEnvProbe
            return self._json(SkillEnvProbe(_tools()).probe_manifest())
        if path == "/api/capabilities":
            return self._json(_capabilities())
        if path == "/api/sandbox":
            return self._json(_sandbox_status())
        if path == "/api/sandbox/runtime":
            probe = (q.get("probe") or ["0"])[0] in ("1", "true", "yes")
            try:
                return self._json(_sandbox_runtime(probe=probe))
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        if path == "/api/platform/status":
            try:
                return self._json(_platform_status())
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        if path == "/api/experience":
            return self._json(_reserved(
                "/api/experience",
                "ExperienceStore.query(topics, role) / record(event) — design/contracts.md §6",
                items=[],
            ))
        if path == "/api/flag/verify":
            return self._json(_reserved(
                "/api/flag/verify",
                "技术路线 Flag 验证(未在仓库声明独立契约)",
            ))
        if path == "/api/hitl/pending":
            return self._json(_reserved(
                "/api/hitl/*",
                "人机协同审批(未声明;仅有 ee escalate 判定)",
                pending=[],
            ))
        if path == "/api/runs":
            return self._json({"runs": _list_runs()})
        if path.startswith("/api/runs/"):
            rest = path[len("/api/runs/"):]
            parts = rest.split("/")
            run_id = parts[0]
            extra = parts[1] if len(parts) > 1 else ""
            if extra == "log":
                tail = int((q.get("tail") or ["220"])[0])
                return self._json({"log": _read_log(run_id, tail=tail)})
            if extra == "events":
                after = int((q.get("after") or ["0"])[0])
                return self._json({"events": _read_events(run_id, after=after)})
            if extra == "signals":
                after = int((q.get("after") or ["0"])[0])
                with _live_lock:
                    slot = _live.get(run_id)
                sigs = slot["bridge"].since(after) if slot else []
                return self._json({"signals": sigs})
            if extra == "report":
                rep = _build_report(run_id)
                if rep is None:
                    return self._json({"error": "run 不存在"}, 404)
                return self._json(rep)
            if extra == "product":
                snap = _snapshot_from_disk(run_id)
                if snap is None:
                    return self._json({"error": "run 不存在"}, 404)
                product = {}
                for sid, sr in (snap.get("steps") or {}).items():
                    if str(sr.get("verdict") or "").lower() == "pass":
                        product[sid] = sr.get("result")
                return self._json({
                    "run_id": run_id,
                    "product": product,
                    "goal_list": snap.get("goal_list") or [],
                    "status": snap.get("status"),
                })
            if extra:
                return self._json({"error": "unknown"}, 404)
            snap = _snapshot_from_disk(run_id)
            if snap is None:
                return self._json({"error": "run 不存在"}, 404)
            return self._json(snap)
        if path.startswith("/api/"):
            return self._json({"error": "not found"}, 404)
        if parsed.path in ("/", ""):
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            body = self._read_json()
        except json.JSONDecodeError:
            return self._json({"error": "JSON 无效"}, 400)
        if path == "/api/config":
            return self._json(_save_config(body))
        if path == "/api/challenge/parse":
            from agent.challenge_intake import normalize_sources, parse_challenge
            try:
                if body.get("challenge_dir") or body.get("metadata_path"):
                    return self._json(_understand_payload(body))
                raw = normalize_sources(
                    title=body.get("title") or "",
                    description=body.get("description") or "",
                    challenge_id=body.get("challenge_id") or "",
                    task_id=body.get("task_id") or "",
                    goals=body.get("goals"),
                    target_url=body.get("target_url") or body.get("url") or "",
                    json_blob=body.get("json") or body.get("json_blob"),
                    attachments=body.get("attachments"),
                    category_override=body.get("category_override") or body.get("challenge_type"),
                )
                return self._json(parse_challenge(
                    raw,
                    category_override=body.get("category_override") or body.get("challenge_type"),
                ))
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
        if path == "/api/challenge/understand":
            try:
                return self._json(_understand_payload(body))
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
        if path == "/api/platform/fetch":
            try:
                return self._json(_platform_fetch(body))
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
        if path == "/api/platform/sync":
            try:
                adapter = _platform_adapter()
                summary = adapter.sync_challenges(body.get("practice_ground_id"))
                return self._json({"ok": True, "summary": summary, "status": _platform_status()})
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
        if path == "/api/platform/target":
            try:
                adapter = _platform_adapter()
                challenge_id = body.get("challenge_id") or body.get("id")
                if not challenge_id:
                    return self._json({"error": "缺少 challenge_id"}, 400)
                action = body.get("action") or "start"
                if action == "stop":
                    data = adapter.stop_target(challenge_id)
                else:
                    data = adapter.start_target(challenge_id)
                return self._json({"ok": True, "challenge_id": challenge_id, "action": action, "target": data})
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
        if path == "/api/challenge/upload":
            # JSON: {files:[{name, content_b64, mime?}]} → downloads/uploads/
            import base64
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            saved = []
            for f in body.get("files") or []:
                name = Path(f.get("name") or "upload.bin").name
                if not name or name in (".", ".."):
                    continue
                stamp = time.strftime("%Y%m%d-%H%M%S")
                dest = UPLOAD_DIR / f"{stamp}_{name}"
                try:
                    dest.write_bytes(base64.b64decode(f.get("content_b64") or ""))
                except Exception as exc:
                    return self._json({"error": f"解码失败 {name}: {exc}"}, 400)
                saved.append({
                    "name": name,
                    "path": str(dest),
                    "size": dest.stat().st_size,
                    "mime": f.get("mime"),
                })
            return self._json({"attachments": saved})
        if path == "/api/runs":
            from agent.challenge_intake import normalize_sources, parse_challenge
            # 允许直接传已 parse 的 task,或现场多源归一+判定
            if body.get("task") and isinstance(body["task"], dict):
                task = body["task"]
                if not task.get("challenge_type"):
                    task = parse_challenge(task).get("task", task)
            else:
                raw = normalize_sources(
                    title=body.get("title") or "",
                    description=body.get("description") or "",
                    challenge_id=body.get("challenge_id") or "c-ui",
                    task_id=body.get("task_id") or f"ui-{int(time.time())}",
                    goals=body.get("goals"),
                    target_url=body.get("target_url") or body.get("url") or "",
                    json_blob=body.get("json") or body.get("json_blob"),
                    attachments=body.get("attachments"),
                    category_override=body.get("category_override") or body.get("challenge_type"),
                )
                task = parse_challenge(
                    raw,
                    category_override=body.get("category_override") or body.get("challenge_type"),
                )["task"]
            execution_mode = str(body.get("execution_mode") or "real").strip().lower()
            try:
                actors = int(body.get("actors") or 1)
                run_id = start_run(
                    task,
                    body.get("run_id"),
                    execution_mode=execution_mode,
                    actors=actors,
                )
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
            return self._json({
                "run_id": run_id,
                "execution_mode": execution_mode,
                "actors": actors,
                "challenge_type": task.get("challenge_type"),
                "challenge_type_label": task.get("challenge_type_label"),
                "type_confidence": task.get("type_confidence"),
            })
        if path.startswith("/api/runs/") and path.endswith("/stop"):
            run_id = path[len("/api/runs/"):-len("/stop")]
            ok = stop_run(run_id)
            return self._json({"ok": ok})
        if path.startswith("/api/runs/") and path.endswith("/resume"):
            run_id = path[len("/api/runs/"):-len("/resume")]
            try:
                resume_run(run_id)
            except Exception as exc:
                return self._json({"error": str(exc)}, 400)
            return self._json({"run_id": run_id})
        if path == "/api/experience/query":
            return self._json(_reserved(
                "POST /api/experience/query",
                "ExperienceStore.query(topics, role)",
                topics=body.get("topics") or [],
                role=body.get("role") or "evaluator_plan",
                items=[],
            ))
        if path == "/api/experience/record":
            return self._json(_reserved(
                "POST /api/experience/record",
                "ExperienceStore.record(event)",
                accepted=False,
                event=body.get("event") or body,
            ))
        if path == "/api/flag/verify":
            try:
                adapter = _platform_adapter()
                challenge_id = body.get("challenge_id") or body.get("id")
                flag = (body.get("flag") or "").strip()
                if not flag:
                    return self._json({"error": "缺少 flag"}, 400)
                if not challenge_id:
                    run_id = body.get("run_id")
                    snap = _snapshot_from_disk(run_id) if run_id else None
                    task = (snap or {}).get("task") or {}
                    challenge_id = task.get("id") or task.get("challenge_id") or task.get("task_id")
                if not challenge_id:
                    return self._json({"error": "缺少 challenge_id；可填写平台题目 ID 或关联 run_id"}, 400)
                known = adapter.get_flag(challenge_id)
                real_submit = bool(body.get("real_submit"))
                if real_submit:
                    result = adapter.submit(challenge_id, flag)
                    return self._json({
                        "wired": True,
                        "mode": "platform_submit",
                        "challenge_id": challenge_id,
                        "flag": flag,
                        "ok": result.ok,
                        "correct": result.correct,
                        "message": result.message,
                        "known_local_flag": bool(known),
                    })
                correct = None
                if known and known.get("flag"):
                    correct = known["flag"] == flag
                return self._json({
                    "wired": True,
                    "mode": "local_check",
                    "challenge_id": challenge_id,
                    "flag": flag,
                    "correct": correct,
                    "known_local_flag": bool(known),
                    "message": "默认未提交到平台；勾选真实提交后才调用 CTF 平台 submit。",
                    "local_record": known,
                })
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
        if path == "/api/hitl/decide":
            return self._json(_reserved(
                "POST /api/hitl/decide",
                "人机协同审批(未声明;escalate 后无人工闸门)",
                decision=body.get("decision"),
                run_id=body.get("run_id"),
            ))
        return self._json({"error": "not found"}, 404)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if not path.startswith("/api/runs/"):
            return self._json({"error": "not found"}, 404)
        run_id = path[len("/api/runs/"):]
        if "/" in run_id or not run_id:
            return self._json({"error": "bad id"}, 400)
        with _live_lock:
            slot = _live.get(run_id)
        if slot and slot.get("alive"):
            return self._json({"error": "运行中不能删除,请先停止"}, 409)
        target = RUNS_DIR / run_id
        if not target.exists():
            return self._json({"error": "不存在"}, 404)
        shutil.rmtree(target)
        with _live_lock:
            _live.pop(run_id, None)
        return self._json({"ok": True})


def serve(host="127.0.0.1", port=8765):
    WEB_DIR.mkdir(exist_ok=True)
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"CTF Agent 指令台  http://{host}:{port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    args = p.parse_args()
    serve(args.host, args.port)
