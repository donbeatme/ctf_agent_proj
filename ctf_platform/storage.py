"""本地存储:ChallengeStore(SQLite 索引+flag) + AttachmentCache(LRU 附件缓存)。

- challenges / challenge_files / challenge_flags / attachment_cache 四张表。
- 提交/验证历史不在此库(归主架构 history),这里只存"正确 flag"。
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from .errors import CacheIntegrityError

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS challenges (
  challenge_id   TEXT PRIMARY KEY,
  platform       TEXT NOT NULL,
  friendly_id    TEXT NOT NULL,
  practice_ground_id TEXT,
  name           TEXT NOT NULL,
  category       TEXT,
  difficulty     TEXT,
  description    TEXT,
  summary        TEXT,
  challenge_type TEXT,
  points         INTEGER,
  has_container  INTEGER DEFAULT 0,
  target         TEXT,
  solve_count    INTEGER DEFAULT 0,
  is_solved      INTEGER DEFAULT 0,
  extra_json     TEXT,
  last_synced_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_challenges_platform_friendly
  ON challenges(platform, friendly_id);
CREATE TABLE IF NOT EXISTS challenge_files (
  file_id     TEXT PRIMARY KEY,
  challenge_id TEXT NOT NULL REFERENCES challenges(challenge_id) ON DELETE CASCADE,
  file_name   TEXT NOT NULL,
  file_size   INTEGER,
  file_md5    TEXT,
  file_type   TEXT,
  path        TEXT,
  updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS ix_challenge_files_challenge ON challenge_files(challenge_id);
CREATE TABLE IF NOT EXISTS challenge_flags (
  challenge_id TEXT PRIMARY KEY REFERENCES challenges(challenge_id) ON DELETE CASCADE,
  flag         TEXT NOT NULL,
  flag_format  TEXT,
  source       TEXT NOT NULL,          -- 'flag_rules' | 'verified_submission' | 'manual'
  verified     INTEGER DEFAULT 0,
  updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submissions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  challenge_id TEXT NOT NULL,
  flag         TEXT NOT NULL,
  verdict      TEXT,                   -- success / INCORRECT_FLAG / ALREADY_SOLVED / 其它平台 code
  correct      INTEGER,                -- 1 / 0 / NULL
  message      TEXT,
  submitted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_submissions_challenge ON submissions(challenge_id);
CREATE TABLE IF NOT EXISTS challenge_procedures (
  procedure_id      TEXT PRIMARY KEY,
  challenge_id      TEXT NOT NULL REFERENCES challenges(challenge_id) ON DELETE CASCADE,
  friendly_id       TEXT,              -- 精确匹配键(denormalize 自 challenges)
  template_id       TEXT,              -- 精确匹配键(跨场地同题;来自 extra_json)
  method            TEXT NOT NULL,     -- 'literal' | 'procedure'
  flag              TEXT,              -- literal: 权威串; procedure: 上次推导值(仅 hint)
  flag_format       TEXT,
  verifier_path     TEXT,              -- procedure: 相对 challenge 目录的提取脚本
  target            TEXT,              -- 验证时实例
  trace_json        TEXT,              -- provenance: 逐字符断言 / 脚本 hash+stdout
  platform_verified INTEGER DEFAULT 0, -- T0/T1 闸门: 输出曾被平台接受过一次
  last_ok_at        TEXT,
  used_count        INTEGER DEFAULT 0,
  created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_proc_challenge ON challenge_procedures(challenge_id);
CREATE INDEX IF NOT EXISTS ix_proc_friendly  ON challenge_procedures(friendly_id);
CREATE INDEX IF NOT EXISTS ix_proc_template  ON challenge_procedures(template_id);
CREATE TABLE IF NOT EXISTS attachment_cache (
  file_id      TEXT PRIMARY KEY REFERENCES challenge_files(file_id) ON DELETE CASCADE,
  challenge_id TEXT NOT NULL,
  rel_path     TEXT NOT NULL,
  size_bytes   INTEGER NOT NULL,
  md5          TEXT,
  last_access  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_attachment_cache_last_access ON attachment_cache(last_access);
"""


@dataclass
class FileRecord:
    file_id: str
    file_name: str
    file_size: int | None = None
    file_md5: str | None = None
    file_type: str | None = None
    path: str | None = None

    @property
    def rel_path(self) -> str:
        return self.path or self.file_name


@dataclass
class ChallengeMeta:
    challenge_id: str
    platform: str
    friendly_id: str
    name: str
    category: str | None = None
    difficulty: str | None = None
    description: str | None = None
    points: int | None = None
    has_container: bool = False
    target: str | None = None
    access: dict | None = None  # 平台开靶返回的访问方式(access_type/access_urls/nc_ssl)
    practice_ground_id: str | None = None
    solve_count: int = 0
    is_solved: bool = False
    summary: str | None = None
    challenge_type: str | None = None
    extra: dict | None = None
    files: list[FileRecord] = field(default_factory=list)


def connect(store_dir: str | Path) -> sqlite3.Connection:
    store_dir = Path(store_dir)
    store_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(store_dir / "ctf_platform.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class ChallengeStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---- challenges ----

    def upsert_challenge(self, meta: ChallengeMeta) -> str:
        """返回 'insert' 或 'update'。以 challenge_id 为主键 upsert。"""
        existing = self.get_challenge(meta.challenge_id)
        now = _now_iso()
        extra = json.dumps(meta.extra, ensure_ascii=False) if meta.extra else None
        self.conn.execute(
            """INSERT INTO challenges(
                 challenge_id, platform, friendly_id, practice_ground_id, name, category,
                 difficulty, description, summary, challenge_type, points, has_container,
                 target, solve_count, is_solved, extra_json, last_synced_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(challenge_id) DO UPDATE SET
                 platform=excluded.platform, friendly_id=excluded.friendly_id,
                 practice_ground_id=excluded.practice_ground_id, name=excluded.name,
                 category=excluded.category, difficulty=excluded.difficulty,
                 description=excluded.description, summary=excluded.summary,
                 challenge_type=excluded.challenge_type, points=excluded.points,
                 has_container=excluded.has_container, target=excluded.target,
                 solve_count=excluded.solve_count, is_solved=excluded.is_solved,
                 extra_json=excluded.extra_json, last_synced_at=excluded.last_synced_at""",
            (
                meta.challenge_id, meta.platform, meta.friendly_id,
                meta.practice_ground_id, meta.name, meta.category, meta.difficulty,
                meta.description, meta.summary, meta.challenge_type, meta.points,
                int(bool(meta.has_container)), meta.target, meta.solve_count,
                int(bool(meta.is_solved)), extra, now,
            ),
        )
        self.conn.commit()
        return "update" if existing else "insert"

    def get_challenge(self, ident: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM challenges WHERE challenge_id=? OR friendly_id=?",
            (ident, ident),
        ).fetchone()
        return dict(row) if row else None

    def set_challenge_target(self, challenge_id: str, target: str | None) -> None:
        """把已启动靶机的 host:port 写回 challenges.target(None=关闭清除)。"""
        self.conn.execute(
            "UPDATE challenges SET target=? WHERE challenge_id=?",
            (target, challenge_id),
        )
        self.conn.commit()

    def query_challenges(
        self,
        *,
        platform: str | None = None,
        category: str | None = None,
        difficulty: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        sql = "SELECT * FROM challenges WHERE 1=1"
        args: list = []
        if platform:
            sql += " AND platform=?"
            args.append(platform)
        if category:
            sql += " AND category=?"
            args.append(category)
        if difficulty:
            sql += " AND difficulty=?"
            args.append(difficulty)
        sql += " ORDER BY friendly_id LIMIT ?"
        args.append(limit)
        rows = self.conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    # ---- files ----

    def upsert_challenge_files(self, challenge_id: str, files: list[FileRecord]) -> None:
        new_ids = {f.file_id for f in files}
        now = _now_iso()
        with self.conn:
            old = self.conn.execute(
                "SELECT file_id FROM challenge_files WHERE challenge_id=?", (challenge_id,)
            ).fetchall()
            for stale in {r["file_id"] for r in old} - new_ids:
                self.conn.execute("DELETE FROM challenge_files WHERE file_id=?", (stale,))
            for f in files:
                self.conn.execute(
                    """INSERT INTO challenge_files(
                         file_id, challenge_id, file_name, file_size, file_md5, file_type, path, updated_at)
                       VALUES(?,?,?,?,?,?,?,?)
                       ON CONFLICT(file_id) DO UPDATE SET
                         challenge_id=excluded.challenge_id, file_name=excluded.file_name,
                         file_size=excluded.file_size, file_md5=excluded.file_md5,
                         file_type=excluded.file_type, path=excluded.path,
                         updated_at=excluded.updated_at""",
                    (f.file_id, challenge_id, f.file_name, f.file_size, f.file_md5,
                     f.file_type, f.path, now),
                )

    def files_for(self, challenge_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM challenge_files WHERE challenge_id=?", (challenge_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def file(self, file_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM challenge_files WHERE file_id=?", (file_id,)
        ).fetchone()
        return dict(row) if row else None

    # ---- flags ----

    def upsert_flag(
        self,
        challenge_id: str,
        flag: str,
        source: str = "manual",
        verified: bool = False,
        flag_format: str | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO challenge_flags(challenge_id, flag, flag_format, source, verified, updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(challenge_id) DO UPDATE SET
                 flag=excluded.flag, flag_format=excluded.flag_format,
                 source=excluded.source, verified=excluded.verified,
                 updated_at=excluded.updated_at""",
            (challenge_id, flag, flag_format, source, int(bool(verified)), _now_iso()),
        )
        self.conn.commit()

    def get_flag(self, challenge_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM challenge_flags WHERE challenge_id=?", (challenge_id,)
        ).fetchone()
        return dict(row) if row else None

    # ---- procedures(动态 flag: 已验证解题过程,可对当前实例重跑推导) ----

    @staticmethod
    def _template_id(extra_json: str | None) -> str | None:
        if not extra_json:
            return None
        try:
            extra = json.loads(extra_json)
        except (ValueError, TypeError):
            return None
        return extra.get("template_id") if isinstance(extra, dict) else None

    def upsert_procedure(
        self,
        procedure_id: str,
        challenge_id: str,
        *,
        method: str = "procedure",
        flag: str | None = None,
        flag_format: str | None = None,
        verifier_path: str | None = None,
        target: str | None = None,
        trace: dict | None = None,
        platform_verified: bool = False,
    ) -> None:
        """写入/更新一条解题过程记录。friendly_id/template_id 自动取自 challenges 行。"""
        ch = self.get_challenge(challenge_id) or {}
        now = _now_iso()
        self.conn.execute(
            """INSERT INTO challenge_procedures(
                 procedure_id, challenge_id, friendly_id, template_id, method, flag,
                 flag_format, verifier_path, target, trace_json, platform_verified,
                 last_ok_at, used_count, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(procedure_id) DO UPDATE SET
                 method=excluded.method, flag=excluded.flag,
                 flag_format=excluded.flag_format, verifier_path=excluded.verifier_path,
                 target=excluded.target, trace_json=excluded.trace_json,
                 platform_verified=excluded.platform_verified,
                 last_ok_at=excluded.last_ok_at, used_count=excluded.used_count""",
            (
                procedure_id, challenge_id, ch.get("friendly_id"),
                self._template_id(ch.get("extra_json")), method, flag, flag_format,
                verifier_path, target,
                json.dumps(trace, ensure_ascii=False) if trace else None,
                int(bool(platform_verified)),
                now if platform_verified else None,
                0, now,
            ),
        )
        self.conn.commit()

    def get_procedures(self, challenge_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM challenge_procedures WHERE challenge_id=? "
            "ORDER BY last_ok_at IS NULL, last_ok_at DESC",
            (challenge_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_validated_procedures(self, challenge_id: str) -> list[dict]:
        """已通过平台验证的 procedure(method='procedure', platform_verified=1),按最近成功排序。"""
        rows = self.conn.execute(
            "SELECT * FROM challenge_procedures WHERE challenge_id=? AND method='procedure' "
            "AND platform_verified=1 ORDER BY last_ok_at IS NULL, last_ok_at DESC",
            (challenge_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def match_procedures(
        self, friendly_id: str | None, template_id: str | None, limit: int = 10
    ) -> list[dict]:
        """仅精确匹配:friendly_id 或 template_id 完全一致、且已被平台验证的 procedure。"""
        conds, args = [], []
        if friendly_id:
            conds.append("friendly_id=?")
            args.append(friendly_id)
        if template_id:
            conds.append("template_id=?")
            args.append(template_id)
        if not conds:
            return []
        args.append(limit)
        rows = self.conn.execute(
            "SELECT * FROM challenge_procedures WHERE platform_verified=1 "
            "AND method='procedure' AND ("
            + " OR ".join(conds) + ") ORDER BY last_ok_at IS NULL, last_ok_at DESC LIMIT ?",
            args,
        ).fetchall()
        return [dict(r) for r in rows]

    def promote_procedure(self, procedure_id: str) -> None:
        """把一条 procedure 标记为已被平台验证(platform_verified=1)。"""
        self.conn.execute(
            "UPDATE challenge_procedures SET platform_verified=1, last_ok_at=? "
            "WHERE procedure_id=?",
            (_now_iso(), procedure_id),
        )
        self.conn.commit()

    def mark_procedure_ok(self, procedure_id: str) -> None:
        """本地判定命中:累加 used_count 并刷新 last_ok_at。"""
        self.conn.execute(
            "UPDATE challenge_procedures SET used_count=used_count+1, last_ok_at=? "
            "WHERE procedure_id=?",
            (_now_iso(), procedure_id),
        )
        self.conn.commit()

    # ---- submissions 日志(每次提交本地保存,支撑 ALREADY_SOLVED 本地比对) ----

    def log_submission(
        self,
        challenge_id: str,
        flag: str,
        verdict: str | None = None,
        correct: bool | None = None,
        message: str = "",
    ) -> None:
        self.conn.execute(
            """INSERT INTO submissions(challenge_id, flag, verdict, correct, message, submitted_at)
               VALUES(?,?,?,?,?,?)""",
            (challenge_id, flag, verdict, (None if correct is None else int(correct)),
             message or "", _now_iso()),
        )
        self.conn.commit()

    def recent_submissions(self, challenge_id: str, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM submissions WHERE challenge_id=? "
            "ORDER BY id DESC LIMIT ?",
            (challenge_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- raw sql access (cache 复用) ----

    def execute(self, sql: str, args=()):
        return self.conn.execute(sql, args)

    def commit(self) -> None:
        self.conn.commit()


class AttachmentCache:
    """LRU 附件缓存:blob 扁平两级分片,DB 记录 last_access。

    downloader 为 callable(file_id, challenge_id) -> bytes(生产传适配器.download,
    测试传 FakeDownloader)。md5 校验在落盘前。
    """

    _SHRINK_RATIO = 0.8

    def __init__(
        self,
        store: ChallengeStore,
        blob_root: str | Path,
        capacity_bytes: int,
        downloader,
    ):
        self.store = store
        self.blob_root = Path(blob_root)
        self.capacity = int(capacity_bytes)
        self.downloader = downloader

    def _blob_path(self, file_id: str) -> Path:
        return self.blob_root / file_id[:2] / file_id

    def ensure(
        self, file_id: str, challenge_id: str, rel_path: str, expected_md5: str | None = None
    ) -> Path:
        """返回本地 blob 路径;命中 touch last_access,未命中下载落盘并 LRU 淘汰。"""
        row = self.store.execute(
            "SELECT * FROM attachment_cache WHERE file_id=?", (file_id,)
        ).fetchone()
        path = self._blob_path(file_id)
        if row is not None and path.is_file():
            self.store.execute(
                "UPDATE attachment_cache SET last_access=? WHERE file_id=?",
                (time.time(), file_id),
            )
            self.store.commit()
            return path
        content = self.downloader(file_id, challenge_id)
        self._store_blob(file_id, challenge_id, rel_path, content, expected_md5)
        return path

    def _store_blob(
        self,
        file_id: str,
        challenge_id: str,
        rel_path: str,
        content: bytes,
        expected_md5: str | None,
    ) -> None:
        if expected_md5:
            actual = hashlib.md5(content).hexdigest()
            if actual != str(expected_md5).lower():
                raise CacheIntegrityError(
                    f"md5 不符 file_id={file_id}: expected {expected_md5}, got {actual}"
                )
        if len(content) > self.capacity:
            log.warning(
                "附件 %s 大小 %d 超过缓存容量 %d,保留但将无法满足阈值",
                file_id, len(content), self.capacity,
            )
        path = self._blob_path(file_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        self.store.execute(
            """INSERT INTO attachment_cache(file_id, challenge_id, rel_path, size_bytes, md5, last_access)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(file_id) DO UPDATE SET
                 challenge_id=excluded.challenge_id, rel_path=excluded.rel_path,
                 size_bytes=excluded.size_bytes, md5=excluded.md5,
                 last_access=excluded.last_access""",
            (file_id, challenge_id, rel_path, len(content),
             hashlib.md5(content).hexdigest(), time.time()),
        )
        self.store.commit()
        self._evict_to_capacity()

    def _evict_to_capacity(self) -> None:
        if self.total_bytes() <= self.capacity:
            return
        rows = self.store.execute(
            "SELECT file_id FROM attachment_cache ORDER BY last_access ASC"
        ).fetchall()
        # 不淘汰最后一件(刚写入):单件超容量时无法压到阈值内,保留兜底(见 _store_blob warning)
        for r in rows[:-1]:
            if self.total_bytes() <= self.capacity * self._SHRINK_RATIO:
                break
            self._remove(r["file_id"])

    def _remove(self, file_id: str) -> None:
        path = self._blob_path(file_id)
        try:
            path.unlink(missing_ok=True)
        except OSError as e:  # pragma: no cover - 平台差异
            log.warning("删除 blob %s 失败: %s", file_id, e)
        self.store.execute("DELETE FROM attachment_cache WHERE file_id=?", (file_id,))
        self.store.commit()

    def materialize(self, challenge_id: str, dest_distfiles: str | Path) -> int:
        """把某题已缓存的附件 copy 到 dest_distfiles/<rel_path>(支持子目录)。"""
        dest = Path(dest_distfiles)
        dest.mkdir(parents=True, exist_ok=True)
        rows = self.store.execute(
            "SELECT file_id, rel_path FROM attachment_cache WHERE challenge_id=?",
            (challenge_id,),
        ).fetchall()
        copied = 0
        now = time.time()
        for r in rows:
            src = self._blob_path(r["file_id"])
            if not src.is_file():
                continue
            target = dest / r["rel_path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
            self.store.execute(
                "UPDATE attachment_cache SET last_access=? WHERE file_id=?",
                (now, r["file_id"]),
            )
            copied += 1
        self.store.commit()
        return copied

    def total_bytes(self) -> int:
        row = self.store.execute(
            "SELECT COALESCE(SUM(size_bytes),0) AS s FROM attachment_cache"
        ).fetchone()
        return int(row["s"])

    def stats(self) -> dict:
        row = self.store.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(size_bytes),0) AS s FROM attachment_cache"
        ).fetchone()
        return {
            "file_count": int(row["c"]),
            "total_bytes": int(row["s"]),
            "capacity_bytes": self.capacity,
        }

    def purge(self) -> int:
        """清空缓存:删除所有 DB 行 + blob(含无引用孤儿)。返回释放字节。"""
        paths = {self._blob_path(r["file_id"]) for r in self.store.execute(
            "SELECT file_id FROM attachment_cache"
        ).fetchall()}
        if self.blob_root.exists():
            for p in self.blob_root.rglob("*"):
                if p.is_file():
                    paths.add(p)
        freed = 0
        for p in paths:
            try:
                freed += p.stat().st_size
                p.unlink()
            except OSError:
                pass
        self.store.execute("DELETE FROM attachment_cache")
        self.store.commit()
        return freed
