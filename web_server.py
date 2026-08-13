"""CTF Agent 前端 HTTP 服务:只包装现有 Engine / Workspace / Skills / 配置,不另造状态机。

对接点:
- 启动: Engine(Planner + CtfSkillsDocStore, MockExecutor, SmokeEvaluator, tool_catalog)
- 信号: Engine(subscribers=[LiveBridge]) → SignalBus
- 停跑: engine.request_stop()
- 续跑: Engine.resume(..., subscribers=[...])
- 历史: runs/<run_id>/{state.json,events.jsonl,run.log}
- 技能: SkillLibrary / CtfSkillToolCatalog / SkillEnvProbe
- 配置: model_config.get/set/reload
"""

from __future__ import annotations

import json
import shutil
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


def _make_agents(ws):
    from agent.challenge_intake import ChallengeUnderstander
    from agent.ctf_skill_tools import CtfSkillToolCatalog
    from agent.executor import MockExecutor
    from agent.planner import Planner
    from agent.skills import CtfSkillsDocStore
    from main import SmokeEvaluator

    planner = Planner(workspace=ws, docs=CtfSkillsDocStore())
    executor = MockExecutor(observation="(mock) 执行完成")
    evaluator = SmokeEvaluator(ws)
    understander = ChallengeUnderstander()
    return planner, executor, evaluator, CtfSkillToolCatalog(), understander


def _start_thread(run_id: str, fn):
    bridge = LiveBridge()
    slot = {"alive": True, "engine": None, "bridge": bridge, "error": None, "replans": 0}
    with _live_lock:
        _live[run_id] = slot

    def worker():
        try:
            fn(slot, bridge)
        except Exception as exc:
            slot["error"] = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        finally:
            slot["alive"] = False
            eng = slot.get("engine")
            if eng is not None:
                slot["replans"] = getattr(eng, "replans", 0)

    threading.Thread(target=worker, name=f"ctf-run-{run_id}", daemon=True).start()
    return slot


def start_run(task: dict, run_id: str | None = None) -> str:
    from agent.engine import Engine
    from agent.workspace import Workspace

    run_id = run_id or f"run-{time.strftime('%Y%m%d-%H%M%S')}"
    ws = Workspace.create(run_id, task)

    def fn(slot, bridge):
        planner, executor, evaluator, catalog, understander = _make_agents(ws)
        engine = Engine(
            planner, executor, evaluator,
            workspace=ws, tool_catalog=catalog, understander=understander,
            subscribers=[bridge],
        )
        slot["engine"] = engine
        engine.run(task)
        slot["replans"] = engine.replans

    _start_thread(run_id, fn)
    return run_id


def resume_run(run_id: str) -> str:
    from agent.engine import Engine
    from agent.workspace import Workspace

    ws = Workspace.load(run_id)
    status = (ws.meta or {}).get("run_status")
    if status in ("DONE", "FAILED"):
        raise RuntimeError(f"终态 {status} 不能续跑")

    def fn(slot, bridge):
        planner, executor, evaluator, _catalog, _u = _make_agents(ws)
        engine = Engine.resume(
            run_id, planner, executor, evaluator, subscribers=[bridge],
        )
        slot["engine"] = engine
        slot["replans"] = getattr(engine, "replans", 0)

    _start_thread(run_id, fn)
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
                "impl": "ChallengeUnderstander / MockTaskUnderstander",
                "note": "多源摄入+题型判定已落地;完整多模态(图片OCR等)仍可扩展",
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
                "status": "stub",
                "impl": "MockExecutor",
                "note": "第二组② 真实执行/Docker 沙箱未接入;接口已收口 tool_exec",
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
                "status": "stub",
                "impl": "SmokeEvaluator / MockEvaluator",
                "note": "真实 Flag 校验逻辑未实现",
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
                "contract": "SkillEnvProbe + Signal.ENV_CHECK",
                "status": "wired",
                "impl": "agent/checks.py",
                "note": "只读 which/find_spec + docker/podman 探测",
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
                "contract": "未在仓库声明独立 API",
                "status": "frontend_reserved",
                "impl": None,
                "note": "技术路线要求;前端预留 /api/flag/verify",
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
        lines.append("- （无；当前多为 MockExecutor）")
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
            run_id = start_run(task, body.get("run_id"))
            return self._json({
                "run_id": run_id,
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
            return self._json(_reserved(
                "POST /api/flag/verify",
                "Flag 验证(未声明契约)",
                flag=body.get("flag"),
                run_id=body.get("run_id"),
                valid=None,
            ))
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
