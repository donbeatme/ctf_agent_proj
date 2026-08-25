"""CLI 命令:适配器能力经命令行暴露。main.py 薄接线。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .base import AdapterError
from .config import StoreSettings
from .ctf2 import Ctf2Adapter
from .errors import AuthError


def register(sub):
    p = sub.add_parser(
        "challenge-fetch",
        help="拉取题目并物化到本地 challenge 目录(URL/JSON 路径/friendly_id)",
    )
    p.add_argument("source", help="ctf2 题目 URL / JSON 文件路径 / friendly_id")
    p.add_argument(
        "--dest", default=None, help="物化目录(缺省 {CTF_STORE_DIR}/challenges/{friendly_id})"
    )
    p.set_defaults(func=cmd_challenge_fetch)

    p = sub.add_parser("challenge-sync", help="拉取全量题目索引落库")
    p.add_argument("--practice-ground-id", default=None)
    p.set_defaults(func=cmd_challenge_sync)

    p = sub.add_parser(
        "challenge-list", help="查看本地题目单(读 data/ctf_platform.db,需先 sync/fetch 落库)"
    )
    p.add_argument("--category", default=None, help="按分类过滤(MISC/REVERSE/CRYPTO/...)")
    p.add_argument("--difficulty", default=None, help="按难度过滤(Easy/Medium/Hard)")
    p.add_argument("--platform", default=None, help="按平台过滤(默认 ctf2)")
    p.add_argument("--limit", type=int, default=200, help="显示条数上限,0 = 全部(默认 200)")
    p.set_defaults(func=cmd_challenge_list)

    p = sub.add_parser(
        "flag-submit", help="向平台提交 flag;正确则写入本地答案库"
    )
    p.add_argument("id", help="challenge_id 或 friendly_id")
    p.add_argument("flag", help="flag 内容")
    p.set_defaults(func=cmd_flag_submit)

    p = sub.add_parser(
        "challenge-target", help="启动/关闭题目靶机容器(start=开, stop=关)"
    )
    p.add_argument("action", choices=("start", "stop"))
    p.add_argument("id", help="challenge_id 或 friendly_id")
    p.add_argument(
        "--yes", action="store_true",
        help="stop 需确认(平台 confirmation 语义,缺省拒绝)",
    )
    p.set_defaults(func=cmd_challenge_target)

    p = sub.add_parser(
        "flags-import", help="导入 flag 规则 JSON → 本地答案库(source=flag_rules)"
    )
    p.add_argument("rules", help="--flag-rules 格式 JSON 文件路径")
    p.set_defaults(func=cmd_flags_import)

    p = sub.add_parser("cache-stats", help="附件缓存统计")
    p.set_defaults(func=cmd_cache_stats)

    p = sub.add_parser("cache-purge", help="清空附件缓存")
    p.set_defaults(func=cmd_cache_purge)


def _adapter() -> Ctf2Adapter:
    return Ctf2Adapter(StoreSettings.from_env())


def cmd_challenge_fetch(args) -> None:
    source = args.source
    if isinstance(source, str) and not source.startswith(("http", "PCHAL")):
        path = Path(source)
        if path.is_file():
            try:
                source = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                print(f"无法解析 JSON 输入: {e}", file=sys.stderr)
                sys.exit(1)
    try:
        dest = _adapter().ingest(source, dest_dir=args.dest)
    except (AuthError, AdapterError) as e:
        print(f"challenge-fetch 失败: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"materialized: {dest}")


def cmd_challenge_sync(args) -> None:
    try:
        r = _adapter().sync_challenges(args.practice_ground_id)
    except (AuthError, AdapterError) as e:
        print(f"challenge-sync 失败: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"challenges: total={r['total']} inserted={r['inserted']} updated={r['updated']}")


def cmd_challenge_list(args) -> None:
    """查看本地题目单(读 data/ctf_platform.db)。需先 challenge-sync / challenge-fetch 落库。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    from .storage import ChallengeStore, connect

    settings = StoreSettings.from_env()
    if not settings.db_path.exists():
        print(f"本地库不存在: {settings.db_path}", file=sys.stderr)
        print("请先运行 challenge-sync 拉取全量,或 challenge-fetch 拉取单题", file=sys.stderr)
        sys.exit(1)
    active = [
        (col, val)
        for col, val in (
            ("platform", args.platform),
            ("category", args.category),
            ("difficulty", args.difficulty),
        )
        if val
    ]
    st = ChallengeStore(connect(settings.store_dir))
    rows = st.query_challenges(
        **{col: val for col, val in active},
        limit=args.limit if args.limit > 0 else 10**9,
    )
    if not rows:
        print("本地库中无匹配题目(可调 --category/--difficulty/--limit 放宽,或先 challenge-sync/fetch)")
        return
    total_sql = "SELECT COUNT(*) FROM challenges WHERE 1=1" + "".join(
        f" AND {col}=?" for col, _ in active
    )
    total = st.conn.execute(total_sql, [val for _, val in active]).fetchone()[0]
    width = max(len(str(r["friendly_id"])) for r in rows)
    print(f"total={total}  shown={len(rows)}")
    for r in rows:
        print(
            f"{str(r['friendly_id']).ljust(width)}  {r['name']}  "
            f"[{r['category']} / {r['difficulty']}]"
        )
    if len(rows) < total:
        print(f"… 还有 {total - len(rows)} 条未显示(调大 --limit)")


def cmd_flag_submit(args) -> None:
    adapter = _adapter()
    row = adapter.store.get_challenge(args.id)
    cid = row["challenge_id"] if row else args.id
    try:
        res = adapter.submit(cid, args.flag)
    except (AuthError, AdapterError) as e:
        print(f"flag-submit 失败: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"submit: ok={res.ok} correct={res.correct}")
    if res.message:
        print("message:", res.message)
    if res.correct is True:
        print("已写入本地答案库")  # 实际落库在 adapter.submit 内完成(persist_flag + submissions 日志)


def cmd_challenge_target(args) -> None:
    adapter = _adapter()
    row = adapter.store.get_challenge(args.id)
    cid = row["challenge_id"] if row else args.id
    try:
        if args.action == "start":
            info = adapter.start_target(cid)
            url = info.get("access_url")
            if url:
                print(f"target started: {url}")
            elif info.get("host") and info.get("port"):
                print(f"target started: {info['host']}:{info['port']}")
            else:
                print(f"target start: status={info.get('status')} (未就绪: {info.get('raw', info)})")
            if info.get("environment_id"):
                print(f"environment_id: {info['environment_id']}  过期: {info.get('expires_at')}")
            print(f"(已写回 challenges.target 供执行层读取: {adapter.store.get_challenge(cid)['target']})")
        else:
            if not args.yes:
                print("stop 需确认,请加 --yes 才能关闭靶机", file=sys.stderr)
                sys.exit(1)
            r = adapter.stop_target(cid)
            print(f"stop: ok={r.get('ok')} message={r.get('message', '')}")
    except (AuthError, AdapterError) as e:
        print(f"challenge-target 失败: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_flags_import(args) -> None:
    try:
        rules = json.loads(Path(args.rules).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"无法读取规则文件: {e}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(rules, dict):
        print("规则文件必须是 JSON object", file=sys.stderr)
        sys.exit(1)
    adapter = _adapter()
    imported = skipped = 0
    for key, rule in rules.items():
        if not isinstance(rule, dict) or str(rule.get("mode", "exact")) != "exact":
            skipped += 1  # sha256/regex 无法还原明文 flag,跳过
            continue
        flag = str(rule.get("value", "")).strip()
        if not flag:
            skipped += 1
            continue
        row = adapter.store.get_challenge(str(key))
        cid = row["challenge_id"] if row else str(key)
        try:
            adapter.persist_flag(cid, flag, verified=True, source="flag_rules")
            imported += 1
        except Exception:
            skipped += 1
    print(f"flags-import: imported={imported} skipped={skipped}")


def cmd_cache_stats(args) -> None:
    s = _adapter().cache_stats()
    print(f"file_count={s['file_count']} total_bytes={s['total_bytes']} capacity_bytes={s['capacity_bytes']}")


def cmd_cache_purge(args) -> None:
    freed = _adapter().cache_purge()
    print(f"cache-purge: freed {freed} bytes")
