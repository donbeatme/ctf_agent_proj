from agent.schema import Signal
from web_server import _capabilities, _reserved, jsonable


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
    assert statuses["executor"] == "wired_declare"
    assert statuses["experience"] == "reserved"
    assert statuses["flag_verify"] == "wired"


def test_reserved_payload_shape():
    r = _reserved("/api/x", "contract", items=[])
    assert r["wired"] is False
    assert r["reserved"] is True
    assert r["endpoint"] == "/api/x"
    assert r["items"] == []
