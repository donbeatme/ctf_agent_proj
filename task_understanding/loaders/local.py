"""本地 challenge 加载器。

支持两种输入：

- {"challenge_dir": "/path/to/challenge"}
- {"metadata_path": "/path/to/metadata.yml", "files_dir": "/path/to/distfiles"}
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class LocalChallengeLoader:
    """读取本地 challenge 的 metadata.yml 与 distfiles 路径。"""

    def resolve_paths(self, raw: dict) -> tuple[Path, Path]:
        if not isinstance(raw, dict):
            raise TypeError("raw challenge input must be a dict")

        if raw.get("metadata_path"):
            metadata_path = Path(raw["metadata_path"])
        elif raw.get("challenge_dir"):
            metadata_path = Path(raw["challenge_dir"]) / "metadata.yml"
        else:
            raise ValueError("raw must provide challenge_dir or metadata_path")

        if raw.get("files_dir"):
            files_dir = Path(raw["files_dir"])
        elif raw.get("challenge_dir"):
            files_dir = Path(raw["challenge_dir"]) / "distfiles"
        else:
            files_dir = metadata_path.parent / "distfiles"

        return metadata_path, files_dir

    def load_metadata(self, metadata_path: Path) -> dict[str, Any]:
        if not metadata_path.is_file():
            raise FileNotFoundError(f"metadata.yml not found: {metadata_path}")
        with metadata_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise ValueError(f"metadata.yml must contain a mapping: {metadata_path}")
        return data
