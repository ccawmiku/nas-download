"""一次性离线扫描已下载的 X 本地文件与历史库，批量导入 x_download_baseline 基线表。"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path

_TWEET_ID_PREFIX_RE = re.compile(r"^(\d{15,20})(?:[_.\-\s]|$)")
_TWEET_ID_TOKEN_RE = re.compile(r"(?:^|[_-])(\d{15,20})(?:[_.\-\s]|$)")
_STRICT_TWEET_ID_RE = re.compile(r"^\d{1,30}$")
_SNOWFLAKE_ID_RE = re.compile(r"^\d{15,20}$")
_MAX_JSON_BYTES = 5 * 1024 * 1024

DEFAULT_DIRS = [
    "/downloads/x/images",
    "/downloads/x/videos",
]
MEDIA_EXTENSIONS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".gif",
        ".heic",
        ".avif",
        ".mp4",
        ".mkv",
        ".webm",
        ".mov",
        ".m4v",
    }
)


def extract_tweet_id_from_filename(name: str) -> str:
    """从文件名中提取 15-20 位推文雪花 ID。"""
    text = str(name or "").strip()
    if not text:
        return ""
    match = _TWEET_ID_PREFIX_RE.match(text)
    if match:
        return match.group(1)
    match = _TWEET_ID_TOKEN_RE.search(text)
    if match:
        return match.group(1)
    return ""


def extract_tweet_id_from_json(path: Path) -> str:
    """从体积受限的元数据 JSON 文件中提取推文 ID。"""
    try:
        if not path.is_file() or path.stat().st_size > _MAX_JSON_BYTES:
            return ""
        data = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    for key in ("tweet_id", "id", "display_id", "rest_id"):
        raw = str(data.get(key) or "").strip()
        if _SNOWFLAKE_ID_RE.match(raw):
            return raw
    return ""


def collect_tweet_ids_from_dirs(
    dirs: list[Path], legacy_db: Path | None = None, *, include_metadata: bool = False
) -> tuple[set[str], dict[str, int]]:
    """扫描本地归档目录与可选旧版 sqlite，收集已下载推文 ID。"""
    ids: set[str] = set()
    stats = {
        "files_scanned": 0,
        "from_filenames": 0,
        "from_json": 0,
        "from_legacy_db": 0,
        "unique_ids": 0,
    }
    for root in dirs:
        if not root.exists() or not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.name.endswith(".part"):
                continue
            suffix = path.suffix.lower()
            is_media = suffix in MEDIA_EXTENSIONS
            is_json = suffix == ".json"
            if not is_media and not (include_metadata and is_json):
                continue
            stats["files_scanned"] += 1
            tid = extract_tweet_id_from_filename(path.name)
            if tid:
                ids.add(tid)
                stats["from_filenames"] += 1
                continue
            if include_metadata and is_json:
                tid = extract_tweet_id_from_json(path)
                if tid:
                    ids.add(tid)
                    stats["from_json"] += 1

    if legacy_db and legacy_db.is_file():
        conn = sqlite3.connect(legacy_db.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            for (raw_id,) in conn.execute(
                "SELECT tweet_id FROM tweets WHERE status IN ('done','skipped')"
            ):
                tid = str(raw_id or "").strip()
                if _STRICT_TWEET_ID_RE.match(tid):
                    ids.add(tid)
                    stats["from_legacy_db"] += 1
        except sqlite3.Error:
            pass
        finally:
            conn.close()

    stats["unique_ids"] = len(ids)
    return ids, stats


def import_baseline_to_db(
    db_path: Path, tweet_ids: set[str] | list[str], dry_run: bool = False
) -> dict[str, int]:
    """将收集到的推文 ID 幂等写入核心数据库的 x_download_baseline 表。"""
    clean = sorted(
        {str(tid).strip() for tid in tweet_ids if _STRICT_TWEET_ID_RE.match(str(tid).strip())}
    )
    if dry_run:
        existing = 0
        current_total = 0
        if db_path.is_file():
            conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                has_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='x_download_baseline'"
                ).fetchone()
                if has_table:
                    current_total = int(
                        conn.execute("SELECT count(*) FROM x_download_baseline").fetchone()[0] or 0
                    )
                    for i in range(0, len(clean), 500):
                        chunk = clean[i : i + 500]
                        slots = ",".join("?" for _ in chunk)
                        existing += int(
                            conn.execute(
                                f"SELECT count(*) FROM x_download_baseline WHERE source_id IN ({slots})",
                                chunk,
                            ).fetchone()[0]
                            or 0
                        )
            finally:
                conn.close()
        added = len(clean) - existing
        return {
            "submitted": len(clean),
            "added": added,
            "existing": existing,
            "total": current_total + added,
        }

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("CREATE TABLE IF NOT EXISTS x_download_baseline(source_id TEXT PRIMARY KEY)")
        added = 0
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            for tid in clean:
                added += conn.execute(
                    "INSERT OR IGNORE INTO x_download_baseline(source_id) VALUES (?)",
                    (tid,),
                ).rowcount
        total = int(conn.execute("SELECT count(*) FROM x_download_baseline").fetchone()[0] or 0)
    finally:
        conn.close()
    return {
        "submitted": len(clean),
        "added": added,
        "existing": len(clean) - added,
        "total": total,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="离线扫描 X 已下载目录与旧库，导入 x_download_baseline 基线表（零 Cookie、零外网请求）。"
    )
    parser.add_argument(
        "--db",
        default="/state/core/state.sqlite3",
        help="核心 SQLite 数据库路径（默认 /state/core/state.sqlite3）",
    )
    parser.add_argument(
        "--dirs",
        nargs="+",
        default=DEFAULT_DIRS,
        help="要扫描的 X 下载与元数据目录列表",
    )
    parser.add_argument(
        "--legacy-db",
        default="/state/x/x_auto.sqlite3",
        help="可选旧版 x_auto.sqlite3 路径（默认 /state/x/x_auto.sqlite3）",
    )
    parser.add_argument(
        "--include-metadata",
        action="store_true",
        help="同时从 .json 元数据文件中提取推文 ID（默认仅扫描实际媒体文件，避免失败元数据误入基线）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅统计将导入数量，不实际写入数据库",
    )
    args = parser.parse_args(argv)

    legacy = Path(args.legacy_db) if args.legacy_db else None
    ids, scan_stats = collect_tweet_ids_from_dirs(
        [Path(d) for d in args.dirs],
        legacy_db=legacy,
        include_metadata=args.include_metadata,
    )
    db_stats = import_baseline_to_db(Path(args.db), ids, dry_run=args.dry_run)
    print(json.dumps({"scan": scan_stats, "import": db_stats, "dry_run": args.dry_run}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
