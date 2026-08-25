"""opslog 统一操作日志:落盘 JSONL + attach 转发。"""

from __future__ import annotations

import json

import pytest

from opslog import ErrorLevel, attach, detach, emit, record_error, reset, set_log_path


@pytest.fixture(autouse=True)
def log(tmp_path):
    """每测隔离:独立日志文件路径 + 清空转发器。"""
    set_log_path(tmp_path / "ops.log")
    reset()
    yield tmp_path / "ops.log"


def test_emit_writes_jsonl_line(log):
    emit("adapter", "submit", challenge_id="c1", correct=True)
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["domain"] == "adapter"
    assert rec["event"] == "submit"
    assert rec["challenge_id"] == "c1"
    assert rec["correct"] is True
    assert "ts" in rec


def test_emit_appends_multiple_lines(log):
    emit("sandbox", "exec", tool_id="ROPgadget")
    emit("sandbox", "install", tool_id="ropper", result="installed")
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_attach_forwards_kind_and_detail(log):
    seen = []
    attach(lambda kind, detail: seen.append((kind, detail)))
    emit("adapter", "ingest", challenge_id="c1", name="demo")
    emit("sandbox", "container_created", session_key="abc", container="ctf-abc")
    assert len(seen) == 2
    kind0, det0 = seen[0]
    assert kind0 == "adapter.ingest"
    assert det0["domain"] == "adapter" and det0["event"] == "ingest"
    assert det0["challenge_id"] == "c1" and det0["name"] == "demo"
    kind1, det1 = seen[1]
    assert kind1 == "sandbox.container_created"
    assert det1["session_key"] == "abc" and det1["container"] == "ctf-abc"


def test_detach_stops_forwarding(log):
    seen = []
    sink = lambda kind, detail: seen.append(kind)
    attach(sink)
    emit("adapter", "sync", total=1)
    detach(sink)
    emit("adapter", "sync", total=2)
    assert seen == ["adapter.sync"]


def test_emit_never_raises_on_bad_path(log):
    set_log_path("/nonexistent-dir/sub/ops.log")  # 不存在的父目录 → 不抛异常
    emit("adapter", "submit", challenge_id="c1")


# ===== record_error:统一错误事件(失败必须进事件,不静默) =====


def test_record_error_writes_error_event(log):
    record_error("sandbox", "init", exc=RuntimeError("boom"),
                 level=ErrorLevel.FATAL, challenge_id="c1")
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["domain"] == "sandbox"
    assert rec["event"] == "init_failed"
    assert rec["level"] == "fatal"
    assert rec["error"] == "RuntimeError: boom"
    assert rec["challenge_id"] == "c1"


def test_record_error_default_recoverable(log):
    record_error("executor", "experience_match", exc=ValueError("nope"))
    rec = json.loads(log.read_text(encoding="utf-8").strip().splitlines()[0])
    assert rec["level"] == "recoverable"


def test_record_error_without_exc(log):
    record_error("adapter", "target_stop", level=ErrorLevel.CLEANUP,
                 challenge_id="c1", status_code=500)
    rec = json.loads(log.read_text(encoding="utf-8").strip().splitlines()[0])
    assert rec["level"] == "cleanup"
    assert rec["error"] == ""
