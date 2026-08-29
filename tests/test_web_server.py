from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.executor import MockExecutor, RealExecutor
from agent.schema import Signal
from agent.workspace import Workspace
from sandbox_env.config import SandboxSettings
import web_server
from web_server import (
    _capabilities,
    _ensure_vm_runtime,
    _make_agents,
    _reserved,
    _validate_real_task,
    jsonable,
)


def test_jsonable_signal_enum():
    assert jsonable({"s": Signal.RUN_STARTED, "n": 1}) == {"s": "run_started", "n": 1}


def test_jsonable_truncates_long_text():
    out = jsonable("x" * 50, limit=10)
    assert out.endswith("…")
    assert len(out) == 11


def test_capabilities_has_layers():
    caps = _capabilities()
    ids = {L["id"] for L in caps["layers"]}
    assert "planner" in ids
    assert "experience" in ids
    assert "flag_verify" in ids
    assert "audit_report" in ids
    statuses = {L["id"]: L["status"] for L in caps["layers"]}
    assert statuses["planner"] == "wired"
    assert statuses["executor"] == "wired"
    assert statuses["experience"] == "reserved"
    assert statuses["flag_verify"] == "wired"


def test_reserved_payload_shape():
    r = _reserved("/api/x", "contract", items=[])
    assert r["wired"] is False
    assert r["reserved"] is True
    assert r["endpoint"] == "/api/x"
    assert r["items"] == []


def test_make_agents_demo_uses_mock_executor(tmp_path):
    ws = Workspace.create("web-demo", {"title": "demo"}, root=tmp_path)
    stack = _make_agents(ws, execution_mode="demo")

    assert isinstance(stack["executor"], MockExecutor)
    assert stack["scheduler"] is None


def test_make_agents_real_uses_real_executor_and_scheduler(tmp_path, monkeypatch):
    ws = Workspace.create(
        "web-real",
        {"challenge_dir": str(tmp_path)},
        root=tmp_path,
    )
    settings = SandboxSettings(ssh_host="sandbox.example", ssh_user="ctf")
    monkeypatch.setattr(web_server, "_platform_adapter", lambda: None)

    stack = _make_agents(ws, execution_mode="real", actors=2, settings=settings)

    assert isinstance(stack["executor"], RealExecutor)
    assert stack["scheduler"] is not None
    assert stack["understander"].__class__.__name__ == "RealTaskUnderstander"


def test_validate_real_task_accepts_only_match_scoped_directory(tmp_path, monkeypatch):
    match_root = tmp_path / "match"
    challenge = match_root / "data" / "challenge"
    challenge.mkdir(parents=True)
    outside = tmp_path / "Developer" / "repo"
    outside.mkdir(parents=True)
    monkeypatch.setattr(web_server, "ROOT", match_root)
    monkeypatch.setattr("agent.llm_api.resolve_key", lambda: "test-key")
    settings = SandboxSettings(ssh_host="127.0.0.1")

    assert _validate_real_task(
        {"challenge_dir": str(challenge)}, settings=settings
    ) == challenge.resolve()
    with pytest.raises(ValueError, match="Match 项目内"):
        _validate_real_task({"challenge_dir": str(outside)}, settings=settings)


def test_ensure_vm_runtime_runs_only_match_vm_script_for_local_ssh(tmp_path, monkeypatch):
    script = tmp_path / "match_vm.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(web_server, "VM_SCRIPT", script)
    monkeypatch.setattr(web_server, "ROOT", tmp_path)
    monkeypatch.setattr(
        web_server.subprocess,
        "run",
        lambda cmd, **kwargs: calls.append((cmd, kwargs)) or SimpleNamespace(
            returncode=0, stdout="already running", stderr=""
        ),
    )
    monkeypatch.setattr(
        web_server,
        "_probe_ssh_runtime",
        lambda settings: {
            "ready": True,
            "host": settings.ssh_host,
            "port": settings.ssh_port,
            "user": settings.ssh_user,
            "image": settings.image,
        },
    )

    runtime = _ensure_vm_runtime(
        "run-local",
        settings=SandboxSettings(ssh_host="127.0.0.1", ssh_port=60022),
    )

    assert calls[0][0] == [str(script), "start"]
    assert calls[0][1]["cwd"] == tmp_path
    assert runtime["ready"] is True
    assert runtime["auto_started"] is True


def test_ensure_vm_runtime_does_not_start_local_vm_for_remote_ssh(monkeypatch):
    monkeypatch.setattr(
        web_server.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("remote SSH must not start local Lima VM"),
    )
    monkeypatch.setattr(
        web_server,
        "_probe_ssh_runtime",
        lambda settings: {
            "ready": True,
            "host": settings.ssh_host,
            "port": settings.ssh_port,
            "user": settings.ssh_user,
            "image": settings.image,
        },
    )

    runtime = _ensure_vm_runtime(
        "run-remote",
        settings=SandboxSettings(ssh_host="sandbox.example"),
    )

    assert runtime["auto_started"] is False


def test_frontend_posts_execution_mode_and_renders_execution_stream():
    root = Path(web_server.__file__).resolve().parent
    html = (root / "web" / "index.html").read_text(encoding="utf-8")
    js = (root / "web" / "app.js").read_text(encoding="utf-8")

    assert 'id="task-execution-mode"' in html
    assert 'value="real" selected' in html
    assert 'id="tool-stream"' in html
    assert "execution_mode: executionMode" in js
    assert '$("tool-stream")' in js
