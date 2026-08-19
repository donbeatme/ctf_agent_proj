import json
import re
from pathlib import Path

import pytest

from agent.schema import TaskInput
from task_understanding.artifact_adapter import artifact_to_json_safe, contains_binary
from task_understanding.real_understander import RealTaskUnderstander


SAMPLE = Path(__file__).resolve().parent / "fixtures" / "challenges" / "sample_challenge"


def _sources(task: TaskInput) -> set[str]:
    return {
        (artifact.get("meta") or {}).get("source")
        for artifact in task.raw_content.get("artifacts", [])
    }


def _sample_image_artifact(task: TaskInput) -> dict | None:
    for artifact in task.raw_content["artifacts"]:
        if (artifact.get("meta") or {}).get("source") == "screenshot.png":
            return artifact
    return None


class FakeImageUnderstander:
    def analyze(self, path: Path) -> dict:
        return {
            "status": "ok",
            "summary": f"Example screenshot from {path.name}",
            "visible_text": "Example",
            "model": "fake-vlm",
        }


class RaisingImageUnderstander:
    def analyze(self, path: Path) -> dict:
        raise RuntimeError("vlm unavailable")


def test_real_understander_challenge_dir_mode():
    task = RealTaskUnderstander().understand({"challenge_dir": str(SAMPLE)})

    assert isinstance(task, TaskInput)
    assert task.raw_content["name"] == "Sample Web Challenge"
    assert task.raw_content["category"] == "web"
    assert task.raw_content["description"]
    assert task.raw_content["target"] == "http://127.0.0.1:8080"
    assert task.raw_content["target_info"] == {
        "raw": "http://127.0.0.1:8080",
        "kind": "url",
        "scheme": "http",
        "host": "127.0.0.1",
        "port": 8080,
        "source": "target",
    }
    assert task.raw_content["task_goal"]["id"] == "obtain_flag"
    assert "Sample Web Challenge" in task.raw_content["task_goal"]["description"]
    assert task.raw_content["task_goal"]["flag_format"] == "flag{...}"
    assert "flag_format" in task.raw_content["task_goal"]["source"]
    constraint_types = {c["type"] for c in task.raw_content["constraints"]}
    assert {"flag_format", "hint", "target", "category"} <= constraint_types
    assert task.raw_content["flag_format"] == "flag{...}"
    assert task.raw_content["hints"] == ["Inspect the provided challenge materials."]
    assert len(task.goal_list) == 1
    assert task.goal_list[0].id == "obtain_flag"

    sources = _sources(task)
    assert len(task.raw_content["artifacts"]) >= 1
    assert "README.txt" in sources

    dumped = task.model_dump()
    assert not contains_binary(dumped)
    json.dumps(dumped, ensure_ascii=False)


def test_real_understander_explicit_paths_mode_matches_core_data():
    by_dir = RealTaskUnderstander().understand({"challenge_dir": str(SAMPLE)})
    explicit = RealTaskUnderstander().understand(
        {
            "metadata_path": str(SAMPLE / "metadata.yml"),
            "files_dir": str(SAMPLE / "distfiles"),
        }
    )

    assert explicit.raw_content["name"] == by_dir.raw_content["name"]
    assert explicit.raw_content["category"] == by_dir.raw_content["category"]
    assert explicit.raw_content["target"] == by_dir.raw_content["target"]
    assert _sources(explicit) == _sources(by_dir)
    assert explicit.goal_list[0].id == "obtain_flag"


def test_real_understander_no_distfiles(tmp_path):
    metadata = tmp_path / "metadata.yml"
    metadata.write_text("name: No Files\ncategory: misc\n", encoding="utf-8")

    task = RealTaskUnderstander().understand({"challenge_dir": str(tmp_path)})

    assert isinstance(task, TaskInput)
    assert task.raw_content["name"] == "No Files"
    assert task.raw_content["artifacts"] == []
    assert task.goal_list[0].id == "obtain_flag"


def test_real_understander_missing_metadata_fails():
    with pytest.raises(FileNotFoundError):
        RealTaskUnderstander().understand({"challenge_dir": "/definitely/not/exist"})


def test_error_artifact_is_json_safe():
    error_artifact = {
        "text": "",
        "images": [],
        "audio": [],
        "video": [],
        "meta": {
            "source": "missing.bin",
            "error": {"code": "unpack-error", "message": "missing"},
        },
    }

    safe = artifact_to_json_safe(error_artifact)

    assert safe["meta"]["error"]["code"] == "unpack-error"
    assert not contains_binary(safe)
    json.dumps(safe, ensure_ascii=False)


def test_unknown_metadata_fields_are_preserved(tmp_path):
    metadata = tmp_path / "metadata.yml"
    metadata.write_text(
        "\n".join(
            [
                "name: Extra Metadata",
                "category: misc",
                "author: test-author",
                "difficulty: easy",
            ]
        ),
        encoding="utf-8",
    )

    task = RealTaskUnderstander().understand({"challenge_dir": str(tmp_path)})

    assert task.raw_content["author"] == "test-author"
    assert task.raw_content["difficulty"] == "easy"
    assert task.raw_content["artifacts"] == []


def test_image_understander_adds_semantics_to_image_artifact():
    # Fixture 没有图片时跳过；有图片时验证语义注入路径。
    task = RealTaskUnderstander(image_understander=FakeImageUnderstander()).understand(
        {"challenge_dir": str(SAMPLE)}
    )

    image = _sample_image_artifact(task)
    if image is None:
        pytest.skip("fixture has no image artifact")
    semantics = image["meta"]["extra"]["image_semantics"]
    assert semantics["status"] == "ok"
    assert semantics["summary"].startswith("Example screenshot")
    assert semantics["visible_text"] == "Example"
    assert semantics["model"] == "fake-vlm"
    assert not contains_binary(task.model_dump())
    json.dumps(task.model_dump(), ensure_ascii=False)


def test_image_understander_failure_degrades_to_error_metadata():
    task = RealTaskUnderstander(image_understander=RaisingImageUnderstander()).understand(
        {"challenge_dir": str(SAMPLE)}
    )

    image = _sample_image_artifact(task)
    if image is None:
        pytest.skip("fixture has no image artifact")
    semantics = image["meta"]["extra"]["image_semantics"]
    assert semantics["status"] == "error"
    assert semantics["error_type"] == "RuntimeError"
    assert "vlm unavailable" in semantics["message"]
    assert not contains_binary(task.model_dump())
    json.dumps(task.model_dump(), ensure_ascii=False)


def test_structured_goal_without_flag_format(tmp_path):
    metadata = tmp_path / "metadata.yml"
    metadata.write_text("name: Plain Goal\ncategory: misc\n", encoding="utf-8")

    task = RealTaskUnderstander().understand({"challenge_dir": str(tmp_path)})

    assert task.goal_list[0].id == "obtain_flag"
    assert task.raw_content["task_goal"] == {
        "id": "obtain_flag",
        "description": "Solve challenge 'Plain Goal' and obtain the requested flag.",
        "source": ["name"],
    }


def test_constraints_from_hints_files_category_and_target(tmp_path):
    metadata = tmp_path / "metadata.yml"
    metadata.write_text(
        "\n".join(
            [
                "name: Constraints",
                "category: crypto",
                "flag_format: csaw{...}",
                "target: 192.0.2.10",
                "hints:",
                "- Look at the modulus.",
                "files:",
                "- params.txt",
            ]
        ),
        encoding="utf-8",
    )

    task = RealTaskUnderstander().understand({"challenge_dir": str(tmp_path)})

    assert task.raw_content["target_info"]["kind"] == "ip"
    assert task.raw_content["target_info"]["host"] == "192.0.2.10"
    constraints = task.raw_content["constraints"]
    assert ("flag_format", "csaw{...}") in {
        (c["type"], c["value"]) for c in constraints if c["type"] == "flag_format"
    }
    assert any(c["type"] == "hint" for c in constraints)
    assert any(c["type"] == "provided_files" and c["value"] == ["params.txt"] for c in constraints)
    assert any(c["type"] == "category" and c["value"] == "crypto" for c in constraints)
    assert any(c["type"] == "target" for c in constraints)


def test_target_nc_ssl_flows_into_target_info_and_constraint(tmp_path):
    """平台标注 access.nc_ssl=true(端口被 TLS 转发器包裹)→ 透传 target_info + 加约束提示。"""
    metadata = tmp_path / "metadata.yml"
    metadata.write_text(
        "\n".join(
            [
                "name: TLS Target",
                "category: pwn",
                "target: abc.tcp-ctf2.dasctf.com:9999",
                "access:",
                "  access_type: tcp",
                "  nc_ssl: true",
                "  access_urls:",
                "  - type: tcp",
                "    url: abc.tcp-ctf2.dasctf.com:9999",
                "    nc_ssl: true",
            ]
        ),
        encoding="utf-8",
    )

    task = RealTaskUnderstander().understand({"challenge_dir": str(tmp_path)})
    rc = task.raw_content
    assert rc["access"]["nc_ssl"] is True            # 原始 access 原样透传
    assert rc["target_info"]["nc_ssl"] is True       # 并语义化挂到 target 上
    assert rc["target_info"]["access"]["access_type"] == "tcp"
    assert any(
        c["type"] == "target_access" and "TLS" in c["value"]
        for c in rc["constraints"]
    )
    # 无 nc_ssl 标注时不加 target_access 约束
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    (plain_dir / "metadata.yml").write_text(
        "name: Plain\ncategory: pwn\ntarget: pwn.example.test:5000\n",
        encoding="utf-8",
    )
    task2 = RealTaskUnderstander().understand({"challenge_dir": str(plain_dir)})
    assert all(c["type"] != "target_access" for c in task2.raw_content["constraints"])


def test_target_info_from_box_and_internal_port(tmp_path):
    metadata = tmp_path / "metadata.yml"
    metadata.write_text(
        "name: Host Port\ncategory: pwn\nbox: pwn.example.test\ninternal_port: 5000\n",
        encoding="utf-8",
    )

    task = RealTaskUnderstander().understand({"challenge_dir": str(tmp_path)})

    assert task.raw_content["target_info"] == {
        "raw": "pwn.example.test",
        "kind": "host_port",
        "host": "pwn.example.test",
        "source": "box/internal_port",
        "port": 5000,
    }


def test_target_info_malformed_target_does_not_crash(tmp_path):
    metadata = tmp_path / "metadata.yml"
    metadata.write_text(
        "name: Bad Target\ncategory: misc\ntarget: 'not a valid <target>'\n",
        encoding="utf-8",
    )

    task = RealTaskUnderstander().understand({"challenge_dir": str(tmp_path)})

    assert task.raw_content["target_info"] == {
        "raw": "not a valid <target>",
        "kind": "unknown",
        "source": "target",
    }
    json.dumps(task.model_dump(), ensure_ascii=False)


def test_unknown_direct_file_gets_binary_metadata_fallback(tmp_path):
    (tmp_path / "metadata.yml").write_text(
        "name: Tiny Binary\ncategory: rev\n",
        encoding="utf-8",
    )
    distfiles = tmp_path / "distfiles"
    distfiles.mkdir()
    binary = distfiles / "tiny.bin"
    binary.write_bytes(b"\x00\x01\x02\x03not-text\x00")

    task = RealTaskUnderstander().understand({"challenge_dir": str(tmp_path)})

    artifact = task.raw_content["artifacts"][0]
    assert artifact["meta"]["source"] == "tiny.bin"
    assert artifact["meta"]["kind"] == "binary"
    assert artifact["meta"]["extra"]["binary_format"] == "generic"
    assert artifact["meta"]["extra"]["size_bytes"] == binary.stat().st_size
    assert re.fullmatch(r"[0-9a-f]{64}", artifact["meta"]["extra"]["sha256"])
    assert not contains_binary(task.model_dump())
    json.dumps(task.model_dump(), ensure_ascii=False)


def test_binary_metadata_fallback_ignores_nested_sources(tmp_path):
    (tmp_path / "metadata.yml").write_text(
        "name: Nested Archive\ncategory: misc\n",
        encoding="utf-8",
    )
    distfiles = tmp_path / "distfiles"
    distfiles.mkdir()
    (distfiles / "source.zip").write_bytes(b"not a zip")

    understander = RealTaskUnderstander()
    artifacts = [
        {
            "text": "",
            "images": [],
            "audio": [],
            "video": [],
            "meta": {"source": "source.zip/nested.bin", "kind": None},
        }
    ]

    enriched = understander._add_binary_metadata_fallback(artifacts, distfiles)

    assert enriched[0]["meta"]["kind"] is None
    assert "extra" not in enriched[0]["meta"]
