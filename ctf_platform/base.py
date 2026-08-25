"""真实平台适配器基类(ChallengeAdapter)。

能力(换平台/靶场只换子类):
  1. ingest(source)   —— 任务理解层输入 → 本地物化目录(parse 抽象 hook + 共享物化)
  2. download(...)    —— 下载附件(抽象, 走共享 LRU 缓存)
  3. submit(...)      —— 提交 flag / target() 预留
  4. persist_flag(...)—— 正确 flag 落本地 SQLite; cache_stats/purge

与主架构解耦:engine/understander 只消费 ingest() 物化的 challenge_dir,
经注入的适配器实例提交/持久化。本模块不存提交历史(归主架构 history)。
"""

from __future__ import annotations

import shutil
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import yaml

from opslog import emit

from .config import StoreSettings
from .errors import AdapterError
from .storage import AttachmentCache, ChallengeMeta, ChallengeStore, FileRecord, connect

__all__ = [
    "AdapterError",
    "ChallengeAdapter",
    "ChallengeMeta",
    "FileRecord",
    "SubmitResult",
    "verify_challenge_dir",
]


@dataclass
class SubmitResult:
    ok: bool                    # 请求是否成功(平台可达)
    correct: bool | None = None  # None=平台未给出明确正确性
    message: str = ""


_CHALLENGE_DIR_KEEP = ("metadata.yml", "distfiles")


def clean_challenge_dir(
    challenge_dir: str | Path, keep: tuple[str, ...] = _CHALLENGE_DIR_KEEP
) -> list[str]:
    """清理 challenge 目录下的解题过程遗留产物(环境打开/物化时调用)。

    目录根下除 metadata.yml(题目元数据)与 distfiles/(附件)外的顶层文件/目录,
    视为上次 run 写入的脚本/临时文件(如 solve_extract.py/_ctf_exec.py),一并删除,
    防止泄漏到下一次运行的执行上下文。返回被删除的路径列表;目录不存在返回 []。
    """
    root = Path(challenge_dir)
    if not root.is_dir():
        return []
    removed: list[str] = []
    for p in root.iterdir():
        if p.name in keep:
            continue
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            removed.append(str(p))
        except OSError:
            pass
    return removed


def verify_challenge_dir(
    challenge_dir: str | Path, files: list[str] | None = None
) -> list[str]:
    """物化完整性守卫:返回问题列表(空 = 就绪)。

    检查目录存在、metadata.yml 可解析且含 id/name、声明的附件已落盘。
    缺任一项即返回具体错误;调用方应 fail-fast,避免 run 已启动后沙箱
    /work 里缺附件才暴露(附件同步失败仅记 recoverable,不阻断命令)。
    """
    root = Path(challenge_dir)
    problems: list[str] = []
    if not root.is_dir():
        return [f"挑战目录不存在或不是目录: {root}"]
    meta = root / "metadata.yml"
    if not meta.is_file():
        problems.append(f"缺少 metadata.yml: {meta}")
    else:
        try:
            data = yaml.safe_load(meta.read_text(encoding="utf-8"))
        except Exception as exc:
            problems.append(f"metadata.yml 解析失败: {exc}")
            data = None
        if not isinstance(data, dict) or not data.get("id") or not data.get("name"):
            problems.append(f"metadata.yml 缺少 id/name 字段: {meta}")
    for rel in files or []:
        p = root / "distfiles" / rel
        if not p.is_file():
            problems.append(f"附件缺失: {p}")
    return problems


def build_summary(
    name: str,
    category: str | None,
    difficulty: str | None,
    description: str | None,
    file_names: list[str],
    max_files: int = 8,
    max_desc: int = 200,
) -> str:
    """纯字符串摘要:标题行 + 附件名(≤8) + 描述摘录(首 200 字符单行)。"""
    title = f"[{category}] {name}" if category else name
    if difficulty:
        title = f"{title} ({difficulty})"
    parts = [title]
    if file_names:
        names = file_names[:max_files]
        tail = "…" if len(file_names) > max_files else ""
        parts.append("附件: " + ", ".join(names) + tail)
    if description:
        desc = " ".join(str(description).split())
        parts.append(desc[:max_desc] + ("…" if len(desc) > max_desc else ""))
    return "\n".join(parts)


class ChallengeAdapter(ABC):
    platform: str = "unknown"

    def __init__(self, settings: StoreSettings):
        self.settings = settings
        self.store = ChallengeStore(connect(settings.store_dir))
        self.cache = AttachmentCache(
            self.store, settings.cache_dir, settings.cache_bytes, downloader=self.download
        )
        # 动态 flag 本地判定的执行钩子:fn(verifier_path, target) -> derived_flag|None,
        # 由 executor 注入(内部走沙箱跑提取脚本);None = 动态题不本地判定,交回平台。
        self._procedure_runner = None

    def set_procedure_runner(self, runner) -> None:
        """注入动态题本地判定的 verifier 执行钩子(EE 把关: 重跑已验证脚本推导当前实例 flag)。"""
        self._procedure_runner = runner

    # ---- 能力 1: 输入 → 本地物化(模板方法) ----

    def ingest(self, source, dest_dir: str | Path | None = None) -> Path:
        """解析 source → 索引落库 → 附件缓存 → 物化 challenge 目录。返回目录路径。"""
        meta = self.parse(source)
        if not meta.challenge_id or not meta.name:
            raise AdapterError("parse 结果缺少 challenge_id/name")
        if meta.summary is None:
            meta.summary = build_summary(
                meta.name, meta.category, meta.difficulty,
                meta.description, [f.file_name for f in meta.files],
            )
        if meta.challenge_type is None and meta.category:
            meta.challenge_type = _default_type(meta.category)
        self.store.upsert_challenge(meta)
        self.store.upsert_challenge_files(meta.challenge_id, meta.files)
        dest = self._materialize(meta, dest_dir)
        emit("adapter", "ingest", challenge_id=meta.challenge_id, name=meta.name,
             category=meta.category, dest=str(dest))
        return dest

    def _materialize(self, meta: ChallengeMeta, dest_dir: str | Path | None = None) -> Path:
        dest = (
            Path(dest_dir)
            if dest_dir
            else self.settings.challenges_dir / (meta.friendly_id or meta.challenge_id)
        )
        # 物化即"环境准备":先清上次 run 遗留产物,再落附件/元数据(仅留 distfiles+metadata)
        clean_challenge_dir(dest)
        for f in meta.files:
            self.cache.ensure(
                f.file_id, meta.challenge_id, f.rel_path, f.file_md5
            )
        self.cache.materialize(meta.challenge_id, dest / "distfiles")
        self._write_metadata(meta, dest)
        problems = verify_challenge_dir(dest, [f.rel_path for f in meta.files])
        if problems:
            raise AdapterError("物化结果不完整: " + "; ".join(problems))
        return dest

    def _write_metadata(self, meta: ChallengeMeta, dest: Path) -> None:
        data = {
            "id": meta.challenge_id,
            "platform": meta.platform,
            "friendly_id": meta.friendly_id,
            "name": meta.name,
            "category": meta.category,
            "difficulty": meta.difficulty,
            "description": meta.description,
            "points": meta.points,
            "has_container": meta.has_container,
            "files": [f.rel_path for f in meta.files],
        }
        if meta.target:
            data["target"] = meta.target
        if meta.access:
            data["access"] = meta.access
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "metadata.yml").write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )

    @abstractmethod
    def parse(self, source) -> ChallengeMeta:
        """把输入(URL/JSON/friendly_id...)解析成 ChallengeMeta。"""

    # ---- 能力 2: 下载(抽象, 走共享 LRU 缓存) ----

    @abstractmethod
    def download(self, file_id: str, challenge_id: str) -> bytes:
        """按 file_id 取附件字节(带会话鉴权)。由 cache.ensure 调度。"""

    # ---- 能力 3: 提交/交互 ----

    @abstractmethod
    def submit(self, challenge_id: str, flag: str) -> SubmitResult:
        """向平台提交 flag,返回正确性。"""

    def start_target(self, challenge_id: str) -> dict:
        """启动/获取该题靶机容器,返回 {host, port, ...};不支持的平台返回空 dict。"""
        return {}

    def stop_target(self, challenge_id: str) -> dict:
        """关闭该题靶机容器;不支持的平台返回空 dict。"""
        return {}

    def clean_challenge_dir(self, challenge_id: str) -> list[str]:
        """按 challenge_id 解析物化目录并清理遗留产物(环境打开时调用)。

        只保留 metadata.yml 与 distfiles/,删除其余顶层文件/目录(上次 run 写入的
        脚本/临时文件)。返回被删除的路径列表;目录不存在返回 []。
        """
        ch = self.store.get_challenge(challenge_id) or {}
        root = self.settings.challenges_dir / (ch.get("friendly_id") or challenge_id)
        removed = clean_challenge_dir(root)
        if removed:
            emit("adapter", "challenge_dir_cleaned", challenge_id=challenge_id,
                 removed=removed)
        return removed

    # ---- 能力 4: 持久化(共享) ----

    def persist_flag(
        self,
        challenge_id: str,
        flag: str,
        verified: bool = False,
        source: str = "verified_submission",
        flag_format: str | None = None,
    ) -> None:
        """把正确 flag 存入本地答案库(提交/验证历史归主架构,不在此)。"""
        self.store.upsert_flag(
            challenge_id, flag, source=source, verified=verified, flag_format=flag_format
        )
        emit("adapter", "flag_persisted", challenge_id=challenge_id,
             verified=verified, source=source)

    def get_flag(self, challenge_id: str) -> dict | None:
        return self.store.get_flag(challenge_id)

    # ===== 解题过程(动态 flag 的可重跑验证,EE 把关) =====

    def record_procedure(
        self,
        challenge_id: str,
        *,
        method: str = "procedure",
        verifier_path: str | None = None,
        trace: dict | None = None,
        flag: str | None = None,
        flag_format: str | None = None,
        platform_verified: bool = False,
        procedure_id: str | None = None,
    ) -> str:
        """写一条解题过程记录;返回 procedure_id。platform_verified=True 表示曾被平台接受过。"""
        pid = procedure_id or uuid.uuid4().hex
        self.store.upsert_procedure(
            pid, challenge_id, method=method, flag=flag, flag_format=flag_format,
            verifier_path=verifier_path, target=self._stored_target(challenge_id),
            trace=trace, platform_verified=platform_verified,
        )
        emit("adapter", "procedure_recorded", challenge_id=challenge_id,
             procedure_id=pid, method=method, platform_verified=platform_verified)
        return pid

    def match_procedures(self, challenge_id: str) -> list[dict]:
        """当前挑战精确匹配到的已验证解题经验(friendly_id / template_id 完全一致)。"""
        ch = self.store.get_challenge(challenge_id) or {}
        if not ch:
            return []
        return self.store.match_procedures(
            ch.get("friendly_id"), self.store._template_id(ch.get("extra_json"))
        )

    def _stored_target(self, challenge_id: str) -> str | None:
        ch = self.store.get_challenge(challenge_id) or {}
        return ch.get("target")

    def _current_target(self, challenge_id: str) -> str | None:
        """当前实例靶机 host:port(动态题):优先 start_target 复用/刷新,回退存库 target。"""
        try:
            r = self.start_target(challenge_id)
            host, port = r.get("host") or "", r.get("port")
            if host and port:
                return f"{host}:{port}"
        except Exception:
            pass
        return self._stored_target(challenge_id)

    def _local_verify(self, challenge_id: str, flag: str) -> SubmitResult | None:
        """本地判定,按题分层:

        - **动态题**(has_container):不信任 challenge_flags 存串(那是上一次实例的,已过期)。
          有已验证 procedure(platform_verified=1)+ 注入 runner → 对当前实例重跑 verifier
          推导 → 本地比对(LOCAL_PROCEDURE);否则返回 None 交回平台。
        - **静态题**:本地答案库 verified 串即权威答案,串比对(LOCAL_VERIFIED,原行为)。

        无 verified 记录/无可用 procedure → 返回 None 交回平台。
        """
        ch = self.store.get_challenge(challenge_id) or {}
        if ch.get("has_container"):
            if self._procedure_runner is not None:
                target = self._current_target(challenge_id)
                for proc in self.store.get_validated_procedures(challenge_id):
                    vp = proc.get("verifier_path")
                    if not vp:
                        continue
                    try:
                        derived = self._procedure_runner(vp, target)
                    except Exception:
                        derived = None
                    if not derived:
                        continue
                    ok = derived == flag
                    if ok:
                        self.store.mark_procedure_ok(proc["procedure_id"])
                    message = "已验证过程重跑推导:答案正确" if ok else "已验证过程重跑推导:答案错误"
                    self.store.log_submission(challenge_id, flag, verdict="LOCAL_PROCEDURE",
                                              correct=ok, message=message)
                    emit("adapter", "submit", challenge_id=challenge_id,
                         verdict="LOCAL_PROCEDURE", correct=ok, ok=True)
                    return SubmitResult(ok=True, correct=ok, message=message)
            return None  # 动态题无已验证 procedure/runner → 交回平台
        row = self.store.get_flag(challenge_id)
        if not row or not row.get("verified"):
            return None
        ok = row["flag"] == flag
        message = (
            "该题本地答案库已有正确记录,本地比对:答案正确"
            if ok else "该题本地答案库已有正确记录,本地比对:答案错误"
        )
        self.store.log_submission(challenge_id, flag, verdict="LOCAL_VERIFIED",
                                  correct=ok, message=message)
        emit("adapter", "submit", challenge_id=challenge_id, verdict="LOCAL_VERIFIED",
             correct=ok, ok=True)
        return SubmitResult(ok=True, correct=ok, message=message)

    def cache_stats(self) -> dict:
        return self.cache.stats()

    def cache_purge(self) -> int:
        n = self.cache.purge()
        emit("adapter", "cache_purge", count=n)
        return n

    # ---- 帮助方法 ----

    def touch(self) -> None:
        """占位:子类可覆写做连接保活。"""
        return None

    def _now(self) -> float:
        return time.time()


def _default_type(category: str) -> str:
    return "ctf-" + str(category).strip().lower()
