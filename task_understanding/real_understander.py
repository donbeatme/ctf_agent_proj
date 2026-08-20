"""Real task understander for local CTF challenge inputs.

This first implementation only normalizes local metadata and local attachments.
It does not execute attachments, connect to targets, or call any LLM.
"""

from __future__ import annotations

import hashlib
import ipaddress
import mimetypes
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from attachments import att
except ModuleNotFoundError:
    def att(path: str):
        root = Path(path)
        artifacts = []
        if not root.exists():
            return artifacts
        for item in root.rglob("*"):
            if item.is_file():
                artifacts.append({
                    "text": "",
                    "images": [],
                    "audio": [],
                    "video": [],
                    "meta": {
                        "source": str(item.relative_to(root)),
                        "mime": mimetypes.guess_type(str(item))[0],
                        "size": item.stat().st_size,
                        "loader": "fallback",
                    },
                })
        return artifacts

from agent.schema import Goal, TaskInput
from agent.understander import TaskUnderstander
from task_understanding.artifact_adapter import artifacts_to_json_safe
from task_understanding.classify import classify_challenge
from task_understanding.goals import default_goals
from task_understanding.image_understanding import ImageUnderstander
from task_understanding.loaders.local import LocalChallengeLoader


class RealTaskUnderstander(TaskUnderstander):
    """Read local challenge metadata/files and return ctf_agent_proj TaskInput."""

    def __init__(
        self,
        image_understander: ImageUnderstander | None = None,
        loader: LocalChallengeLoader | None = None,
    ):
        self.image_understander = image_understander
        self.loader = loader or LocalChallengeLoader()

    def understand(self, raw: dict) -> TaskInput:
        if raw.get("challenge_dir") or raw.get("metadata_path"):
            return self._understand_local(raw)
        return self._understand_multi_source(raw)

    def _understand_local(self, raw: dict) -> TaskInput:
        metadata_path, files_dir = self.loader.resolve_paths(raw)
        metadata = self.loader.load_metadata(metadata_path)
        raw_content = dict(metadata)
        # 物化挑战目录随 raw_content 走,executor 据此锁定 cwd(题目附件目录)
        raw_content["challenge_dir"] = str(metadata_path.parent)
        target_info = self._target_info(metadata)
        raw_content["task_goal"] = self._task_goal(metadata)
        raw_content["constraints"] = self._constraints(metadata, target_info)
        if target_info is not None:
            raw_content["target_info"] = target_info
        raw_content["artifacts"] = self._load_artifacts(files_dir)
        raw_content = self._classify_and_tag(raw_content)
        goals = default_goals(raw_content.get("challenge_type"), raw_content)
        raw_content.pop("goals", None)
        return TaskInput(raw_content=raw_content, goal_list=goals)

    def _understand_multi_source(self, raw: dict) -> TaskInput:
        raw_content = dict(raw or {})
        if not raw_content.get("challenge_type"):
            raw_content = self._classify_and_tag(raw_content)
        goals = default_goals(raw_content.get("challenge_type"), raw_content)
        raw_content.pop("goals", None)
        if "attachments" in raw_content:
            raw_content["attachments"] = [
                {k: a.get(k) for k in ("name", "path", "size", "mime") if a.get(k) is not None}
                for a in raw_content["attachments"]
            ]
        return TaskInput(raw_content=raw_content, goal_list=goals)

    def _classify_and_tag(self, raw_content: dict) -> dict:
        attachments = list(raw_content.get("attachments") or [])
        attachments.extend(
            {"name": f} for f in (raw_content.get("files") or []) if isinstance(f, str)
        )
        for artifact in raw_content.get("artifacts") or []:
            source = (artifact.get("meta") or {}).get("source")
            if source:
                attachments.append({"name": source})
        result = classify_challenge(raw_content, attachments=attachments)
        raw_content["challenge_type"] = result.primary
        raw_content["challenge_type_label"] = result.ranked[0].label if result.ranked else result.primary
        raw_content["type_confidence"] = result.confidence
        raw_content["type_scores"] = [
            {
                "category": h.category,
                "label": h.label,
                "score": h.score,
                "evidence": h.evidence,
            }
            for h in result.ranked
        ]
        label = raw_content["challenge_type_label"]
        tip = f"[题型判定] {label} ({result.primary}, confidence={result.confidence})"
        desc = raw_content.get("description") or ""
        if tip not in desc:
            raw_content["description"] = f"{tip}\n{desc}".strip()
        return raw_content

    def _load_artifacts(self, files_dir: Path) -> list[dict]:
        if not files_dir.exists():
            return []
        artifacts = att(str(files_dir))
        safe_artifacts = artifacts_to_json_safe(artifacts)
        safe_artifacts = self._add_binary_metadata_fallback(safe_artifacts, files_dir)
        return self._add_image_semantics(safe_artifacts, files_dir)

    def _task_goal(self, metadata: dict[str, Any]) -> dict[str, Any]:
        name = str(metadata.get("name") or "CTF challenge")
        flag_format = metadata.get("flag_format")
        if flag_format:
            description = (
                f"Solve challenge '{name}' and obtain a flag matching "
                f"'{flag_format}'."
            )
            source = ["name", "flag_format"]
        else:
            description = f"Solve challenge '{name}' and obtain the requested flag."
            source = ["name"]
        return {
            "id": "obtain_flag",
            "description": description,
            "source": source,
            **({"flag_format": flag_format} if flag_format else {}),
        }

    def _constraints(
        self, metadata: dict[str, Any], target_info: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        constraints: list[dict[str, Any]] = []
        if metadata.get("flag_format"):
            constraints.append(
                {
                    "type": "flag_format",
                    "value": metadata["flag_format"],
                    "source": "flag_format",
                }
            )
        hints = metadata.get("hints")
        if isinstance(hints, list):
            for hint in hints:
                constraints.append({"type": "hint", "value": hint, "source": "hints"})
        elif isinstance(hints, str):
            constraints.append({"type": "hint", "value": hints, "source": "hints"})
        if isinstance(metadata.get("files"), list):
            constraints.append(
                {
                    "type": "provided_files",
                    "value": list(metadata["files"]),
                    "source": "files",
                }
            )
        if metadata.get("category"):
            constraints.append(
                {
                    "type": "category",
                    "value": metadata["category"],
                    "source": "category",
                }
            )
        if target_info is not None:
            constraints.append(
                {
                    "type": "target",
                    "value": target_info,
                    "source": target_info.get("source", "target"),
                }
            )
        if target_info and target_info.get("nc_ssl"):
            constraints.append(
                {
                    "type": "target_access",
                    "value": "靶机端口被平台 TLS 转发器包裹(nc_ssl=true):"
                             "必须用 TLS/SSL 连接(SNI + 关闭证书校验),裸 TCP 只会收到 0 字节。",
                    "source": "access.nc_ssl",
                }
            )
        return constraints

    def _target_info(self, metadata: dict[str, Any]) -> dict[str, Any] | None:
        target = metadata.get("target")
        if isinstance(target, str) and target.strip():
            info = self._parse_target(target.strip(), source="target")
        else:
            box = metadata.get("box")
            port = metadata.get("internal_port")
            if not (isinstance(box, str) and box.strip()):
                return None
            info = {
                "raw": box.strip(),
                "kind": "host_port" if port is not None else "host",
                "host": box.strip(),
                "source": "box/internal_port" if port is not None else "box",
            }
            parsed_port = self._parse_port(port)
            if parsed_port is not None:
                info["port"] = parsed_port
        access = metadata.get("access")
        if isinstance(access, dict) and access.get("nc_ssl") is not None:
            # 平台标注 nc_ssl=true:挑战端口被 TLS 转发器包裹,需 SSL 连接而非裸 TCP
            info["nc_ssl"] = bool(access.get("nc_ssl"))
            info["access"] = {
                k: access[k] for k in ("access_type", "access_url", "access_urls")
                if access.get(k) is not None
            }
        return info

    def _parse_target(self, raw: str, *, source: str) -> dict[str, Any]:
        parsed = urlparse(raw)
        if parsed.scheme and parsed.netloc:
            info: dict[str, Any] = {
                "raw": raw,
                "kind": "url",
                "scheme": parsed.scheme,
                "host": parsed.hostname,
                "source": source,
            }
            try:
                port = parsed.port
            except ValueError:
                port = None
                info["error"] = "invalid port"
            if port is not None:
                info["port"] = port
            if parsed.path and parsed.path != "/":
                info["path"] = parsed.path
            return info

        if " " in raw or any(ch in raw for ch in "<>"):
            return {"raw": raw, "kind": "unknown", "source": source}

        host, sep, port_text = raw.rpartition(":")
        if sep and host and port_text.isdigit():
            return {
                "raw": raw,
                "kind": "host_port",
                "host": host,
                "port": int(port_text),
                "source": source,
            }

        try:
            ipaddress.ip_address(raw)
            return {"raw": raw, "kind": "ip", "host": raw, "source": source}
        except ValueError:
            return {"raw": raw, "kind": "host", "host": raw, "source": source}

    def _parse_port(self, port: Any) -> int | None:
        if isinstance(port, int):
            return port
        if isinstance(port, str) and port.isdigit():
            return int(port)
        return None

    def _add_binary_metadata_fallback(
        self, artifacts: list[dict], files_dir: Path
    ) -> list[dict]:
        for artifact in artifacts:
            if not self._needs_binary_metadata_fallback(artifact):
                continue

            path = self._direct_artifact_path(files_dir, artifact)
            if path is None:
                continue

            meta = artifact.setdefault("meta", {})
            meta["kind"] = "binary"
            extra = dict(meta.get("extra") or {})
            extra.update(self._file_metadata(path))
            meta["extra"] = extra

        return artifacts

    def _needs_binary_metadata_fallback(self, artifact: dict) -> bool:
        meta = artifact.get("meta") or {}
        return (
            meta.get("kind") is None
            and not (artifact.get("text") or "")
            and not (artifact.get("images") or [])
            and meta.get("error") is None
        )

    def _direct_artifact_path(self, files_dir: Path, artifact: dict) -> Path | None:
        source = (artifact.get("meta") or {}).get("source")
        if not source or not isinstance(source, str):
            return None

        source_path = Path(source)
        if source_path.is_absolute() or len(source_path.parts) != 1:
            return None

        root = files_dir.resolve()
        candidate = (root / source_path).resolve()
        if candidate.parent != root or not candidate.is_file():
            return None
        return candidate

    def _file_metadata(self, path: Path) -> dict[str, Any]:
        header = self._read_header(path)
        metadata: dict[str, Any] = {
            "size_bytes": path.stat().st_size,
            "sha256": self._sha256_file(path),
            "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        }
        metadata.update(self._binary_format_metadata(header))
        return metadata

    def _read_header(self, path: Path, size: int = 64) -> bytes:
        with path.open("rb") as f:
            return f.read(size)

    def _sha256_file(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _binary_format_metadata(self, header: bytes) -> dict[str, str]:
        if header.startswith(b"\x7fELF"):
            metadata = {"binary_format": "ELF"}
            if len(header) >= 20:
                elf_class = {1: "ELF32", 2: "ELF64"}.get(header[4])
                endianness = {1: "little", 2: "big"}.get(header[5])
                if elf_class:
                    metadata["elf_class"] = elf_class
                if endianness:
                    metadata["endianness"] = endianness
                    machine = int.from_bytes(header[18:20], endianness)
                    architecture = {
                        0x03: "x86",
                        0x28: "ARM",
                        0x3E: "x86-64",
                        0xB7: "AArch64",
                        0xF3: "RISC-V",
                    }.get(machine)
                    if architecture:
                        metadata["architecture"] = architecture
            return metadata

        if header.startswith(b"MZ"):
            return {"binary_format": "PE/MZ"}

        return {"binary_format": "generic"}

    def _add_image_semantics(
        self, artifacts: list[dict], files_dir: Path
    ) -> list[dict]:
        if self.image_understander is None:
            return artifacts

        for artifact in artifacts:
            if not self._is_image_artifact(artifact):
                continue
            meta = artifact.setdefault("meta", {})
            extra = dict(meta.get("extra") or {})
            path = self._direct_artifact_path(files_dir, artifact)
            if path is None:
                extra["image_semantics"] = {
                    "status": "unavailable",
                    "reason": "image source is not a direct local file",
                }
            else:
                try:
                    extra["image_semantics"] = self.image_understander.analyze(path)
                except Exception as exc:
                    extra["image_semantics"] = {
                        "status": "error",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    }
            meta["extra"] = extra
        return artifacts

    def _is_image_artifact(self, artifact: dict) -> bool:
        meta = artifact.get("meta") or {}
        if meta.get("kind") == "image":
            return True
        return bool(artifact.get("images") or [])
