"""Offline tests for tools/import_x_baseline.py (no network, no browser)."""

from __future__ import annotations

import codecs
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import import_x_baseline as baseline  # noqa: E402

TWEET_ID = "123456789012345678"


def _write(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _write_json(path: Path, payload: object) -> Path:
    return _write(path, json.dumps(payload).encode("utf-8"))


def _make_baseline_db(path: Path, ids: tuple[str, ...] = ()) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute("CREATE TABLE x_download_baseline(source_id TEXT PRIMARY KEY)")
            conn.executemany(
                "INSERT INTO x_download_baseline(source_id) VALUES (?)",
                [(tweet_id,) for tweet_id in ids],
            )
    finally:
        conn.close()
    return path


def _baseline_rows(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [
            row[0]
            for row in conn.execute(
                "SELECT source_id FROM x_download_baseline ORDER BY source_id"
            )
        ]
    finally:
        conn.close()


def _make_legacy_db(path: Path, rows: list[tuple[object, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute("CREATE TABLE tweets(tweet_id TEXT, status TEXT)")
            conn.executemany("INSERT INTO tweets(tweet_id, status) VALUES (?, ?)", rows)
    finally:
        conn.close()
    return path


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("123456789012345.jpg", "123456789012345"),
        ("123456789012345678.jpg", TWEET_ID),
        ("12345678901234567890.mp4", "12345678901234567890"),
        ("123456789012345678_1.jpg", TWEET_ID),
        ("123456789012345678-1.mp4", TWEET_ID),
        ("123456789012345678 1.jpg", TWEET_ID),
        ("prefix_123456789012345678.jpg", TWEET_ID),
        ("prefix-123456789012345678-extra.webp", TWEET_ID),
        ("  123456789012345678.jpg  ", TWEET_ID),
        ("123456789012345678", TWEET_ID),
        ("x_111111111111111111_222222222222222222.jpg", "111111111111111111"),
    ],
)
def test_extract_tweet_id_from_filename_accepts_snowflake_boundaries(
    name: str, expected: str
) -> None:
    assert baseline.extract_tweet_id_from_filename(name) == expected


@pytest.mark.parametrize(
    "name",
    [
        "12345678901234.jpg",
        "123456789012345678901.jpg",
        "abc123456789012345678.jpg",
        "123456789012345678abc.jpg",
        "prefix123456789012345678.jpg",
        "abc_12345678901234.jpg",
        "",
        "   ",
        None,
    ],
)
def test_extract_tweet_id_from_filename_rejects_non_snowflake_input(
    name: str | None,
) -> None:
    assert baseline.extract_tweet_id_from_filename(name) == ""


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"tweet_id": "111111111111111111", "id": "222222222222222222"},
            "111111111111111111",
        ),
        (
            {"tweet_id": "not-a-snowflake", "id": "222222222222222222"},
            "222222222222222222",
        ),
        ({"id": "bad", "display_id": "333333333333333333"}, "333333333333333333"),
        ({"rest_id": "444444444444444444"}, "444444444444444444"),
        ({"id": " 555555555555555555 "}, "555555555555555555"),
        ({"id": 666666666666666666}, "666666666666666666"),
    ],
)
def test_extract_tweet_id_from_json_uses_supported_keys_in_order(
    tmp_path: Path, payload: dict[str, object], expected: str
) -> None:
    path = _write_json(tmp_path / "meta.json", payload)
    assert baseline.extract_tweet_id_from_json(path) == expected


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b'{"tweet_id": "12345678901234"}',
        b'{"tweet_id": "123456789012345678901"}',
        b'{"tweet_id": "not-a-snowflake"}',
        b"[]",
        b'"123456789012345678"',
        b"{",
        b"",
        b'{"tweet_id": null}',
        b'{"tweet_id": "   "}',
    ],
)
def test_extract_tweet_id_from_json_rejects_invalid_payloads(
    tmp_path: Path, raw: bytes
) -> None:
    path = _write(tmp_path / "meta.json", raw)
    assert baseline.extract_tweet_id_from_json(path) == ""


def test_extract_tweet_id_from_json_accepts_utf8_bom(tmp_path: Path) -> None:
    raw = codecs.BOM_UTF8 + json.dumps({"tweet_id": TWEET_ID}).encode("utf-8")
    path = _write(tmp_path / "bom.json", raw)
    assert baseline.extract_tweet_id_from_json(path) == TWEET_ID


def test_extract_tweet_id_from_json_skips_missing_and_directory_paths(
    tmp_path: Path,
) -> None:
    assert baseline.extract_tweet_id_from_json(tmp_path / "missing.json") == ""
    assert baseline.extract_tweet_id_from_json(tmp_path) == ""


def test_extract_tweet_id_from_json_accepts_file_at_exact_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = json.dumps({"tweet_id": TWEET_ID}).encode("utf-8")
    exact = _write(tmp_path / "exact.json", payload)
    over = _write(tmp_path / "over.json", payload + b" ")
    monkeypatch.setattr(baseline, "_MAX_JSON_BYTES", len(payload))
    assert baseline.extract_tweet_id_from_json(exact) == TWEET_ID
    assert baseline.extract_tweet_id_from_json(over) == ""


def test_extract_tweet_id_from_json_skips_file_above_real_size_cap(
    tmp_path: Path,
) -> None:
    path = tmp_path / "huge.json"
    with path.open("wb") as handle:
        handle.seek(baseline._MAX_JSON_BYTES)
        handle.write(b" ")
    assert path.stat().st_size == baseline._MAX_JSON_BYTES + 1
    assert baseline.extract_tweet_id_from_json(path) == ""


def _build_archive(root: Path) -> None:
    _write(root / "111111111111111111.jpg")
    _write(root / "nested" / "222222222222222222_1.PNG")
    _write(root / "333333333333333333.jpg.part")
    _write(root / "notes.txt")
    _write(
        root / "meta" / "note.json",
        json.dumps({"tweet_id": "555555555555555555"}).encode(),
    )
    _write(root / "meta" / "444444444444444444.json", b"{}")
    _write(root / "666666666666666666.mp4")
    _write(root / "thumb.webp")


def test_collect_media_files_by_default(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    _build_archive(root)
    ids, stats = baseline.collect_tweet_ids_from_dirs([root])
    assert ids == {"111111111111111111", "222222222222222222", "666666666666666666"}
    assert stats == {
        "files_scanned": 4,
        "from_filenames": 3,
        "from_json": 0,
        "from_legacy_db": 0,
        "unique_ids": 3,
    }


def test_collect_metadata_json_when_enabled(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    _build_archive(root)
    ids, stats = baseline.collect_tweet_ids_from_dirs([root], include_metadata=True)
    assert ids == {
        "111111111111111111",
        "222222222222222222",
        "444444444444444444",
        "555555555555555555",
        "666666666666666666",
    }
    assert stats == {
        "files_scanned": 6,
        "from_filenames": 4,
        "from_json": 1,
        "from_legacy_db": 0,
        "unique_ids": 5,
    }


def test_collect_counts_duplicate_files_but_dedupes_ids(tmp_path: Path) -> None:
    root = tmp_path / "dup"
    _write(root / "111111111111111111.jpg")
    _write(root / "111111111111111111_2.jpg")
    _write(root / "111111111111111111.jpg.part")
    _write(root / "222222222222222222.mp4")
    ids, stats = baseline.collect_tweet_ids_from_dirs([root])
    assert ids == {"111111111111111111", "222222222222222222"}
    assert stats == {
        "files_scanned": 3,
        "from_filenames": 3,
        "from_json": 0,
        "from_legacy_db": 0,
        "unique_ids": 2,
    }


def test_collect_ignores_missing_or_non_directory_roots(tmp_path: Path) -> None:
    _write(tmp_path / "plain-file")
    ids, stats = baseline.collect_tweet_ids_from_dirs(
        [tmp_path / "missing", tmp_path / "plain-file"]
    )
    assert ids == set()
    assert stats == {
        "files_scanned": 0,
        "from_filenames": 0,
        "from_json": 0,
        "from_legacy_db": 0,
        "unique_ids": 0,
    }


def test_collect_legacy_db_filters_status_and_id_shape(tmp_path: Path) -> None:
    legacy = _make_legacy_db(
        tmp_path / "x_auto.sqlite3",
        [
            ("123456789012345678", "done"),
            ("223456789012345678", "skipped"),
            ("323456789012345678", "pending"),
            ("123", "done"),
            ("9" * 30, "done"),
            ("9" * 31, "done"),
            ("not-a-number", "done"),
            (None, "done"),
            (" 423456789012345678 ", "done"),
        ],
    )
    ids, stats = baseline.collect_tweet_ids_from_dirs([], legacy_db=legacy)
    assert ids == {
        "123456789012345678",
        "223456789012345678",
        "123",
        "9" * 30,
        "423456789012345678",
    }
    assert stats == {
        "files_scanned": 0,
        "from_filenames": 0,
        "from_json": 0,
        "from_legacy_db": 5,
        "unique_ids": 5,
    }


def test_collect_ignores_missing_malformed_and_non_sqlite_legacy_dbs(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.sqlite3"
    malformed = tmp_path / "malformed.sqlite3"
    conn = sqlite3.connect(malformed)
    try:
        with conn:
            conn.execute("CREATE TABLE other(value TEXT)")
    finally:
        conn.close()
    garbage = _write(tmp_path / "garbage.sqlite3", b"not a sqlite database")

    for legacy in (missing, malformed, garbage):
        ids, stats = baseline.collect_tweet_ids_from_dirs([], legacy_db=legacy)
        assert ids == set()
        assert stats["from_legacy_db"] == 0
        assert stats["unique_ids"] == 0


def test_import_filters_and_dedupes_ids(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "state.sqlite3"
    stats = baseline.import_baseline_to_db(
        db,
        [" 123 ", "456", "456", "abc", "", "   ", "9" * 31, "9" * 30, "7" * 19],
    )
    assert stats == {"submitted": 4, "added": 4, "existing": 0, "total": 4}
    assert _baseline_rows(db) == ["123", "456", "7" * 19, "9" * 30]


def test_import_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite3"
    ids = {"123", "456", "789"}
    first = baseline.import_baseline_to_db(db, ids)
    second = baseline.import_baseline_to_db(db, ids)
    assert first == {"submitted": 3, "added": 3, "existing": 0, "total": 3}
    assert second == {"submitted": 3, "added": 0, "existing": 3, "total": 3}
    assert _baseline_rows(db) == ["123", "456", "789"]


def test_import_preserves_existing_rows(tmp_path: Path) -> None:
    db = _make_baseline_db(tmp_path / "state.sqlite3", ("123",))
    stats = baseline.import_baseline_to_db(db, {"123", "456"})
    assert stats == {"submitted": 2, "added": 1, "existing": 1, "total": 2}
    assert _baseline_rows(db) == ["123", "456"]


def test_dry_run_on_missing_db_creates_nothing(tmp_path: Path) -> None:
    db = tmp_path / "state" / "state.sqlite3"
    stats = baseline.import_baseline_to_db(db, {"123", "456"}, dry_run=True)
    assert stats == {"submitted": 2, "added": 2, "existing": 0, "total": 2}
    assert not db.exists()


def test_dry_run_reports_overlap_without_writing(tmp_path: Path) -> None:
    db = _make_baseline_db(tmp_path / "state.sqlite3", ("123", "456"))
    stats = baseline.import_baseline_to_db(db, {"123", "789"}, dry_run=True)
    assert stats == {"submitted": 2, "added": 1, "existing": 1, "total": 3}
    assert _baseline_rows(db) == ["123", "456"]


def test_dry_run_without_baseline_table_does_not_create_it(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite3"
    conn = sqlite3.connect(db)
    try:
        with conn:
            conn.execute("CREATE TABLE unrelated(value TEXT)")
    finally:
        conn.close()
    stats = baseline.import_baseline_to_db(db, {"123"}, dry_run=True)
    assert stats == {"submitted": 1, "added": 1, "existing": 0, "total": 1}
    conn = sqlite3.connect(db)
    try:
        found = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='x_download_baseline'"
        ).fetchone()
    finally:
        conn.close()
    assert found is None


def test_dry_run_counts_existing_ids_across_query_chunks(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite3"
    all_ids = [f"{index:019d}" for index in range(1001)]
    _make_baseline_db(db, tuple(all_ids[:500]))
    stats = baseline.import_baseline_to_db(db, all_ids, dry_run=True)
    assert stats == {"submitted": 1001, "added": 501, "existing": 500, "total": 1001}
    assert len(_baseline_rows(db)) == 500


def test_main_dry_run_emits_stats_and_creates_no_db(tmp_path: Path, capsys) -> None:
    root = tmp_path / "archive"
    _write(root / "111111111111111111.jpg")
    _write(
        root / "meta" / "note.json",
        json.dumps({"tweet_id": "555555555555555555"}).encode(),
    )
    db = tmp_path / "state" / "state.sqlite3"

    code = baseline.main(
        [
            "--db",
            str(db),
            "--dirs",
            str(root),
            "--legacy-db",
            str(tmp_path / "missing.sqlite3"),
            "--dry-run",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "scan": {
            "files_scanned": 1,
            "from_filenames": 1,
            "from_json": 0,
            "from_legacy_db": 0,
            "unique_ids": 1,
        },
        "import": {"submitted": 1, "added": 1, "existing": 0, "total": 1},
        "dry_run": True,
    }
    assert not db.exists()


def test_main_import_is_idempotent_and_respects_metadata_flag(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "archive"
    _write(root / "111111111111111111.jpg")
    _write(
        root / "meta" / "note.json",
        json.dumps({"tweet_id": "555555555555555555"}).encode(),
    )
    _write(root / "ignored.txt")
    db = tmp_path / "state.sqlite3"
    args = [
        "--db",
        str(db),
        "--dirs",
        str(root),
        "--legacy-db",
        str(tmp_path / "missing.sqlite3"),
        "--include-metadata",
    ]

    assert baseline.main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["scan"] == {
        "files_scanned": 2,
        "from_filenames": 1,
        "from_json": 1,
        "from_legacy_db": 0,
        "unique_ids": 2,
    }
    assert first["import"] == {"submitted": 2, "added": 2, "existing": 0, "total": 2}
    assert first["dry_run"] is False

    assert baseline.main(args) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["import"] == {"submitted": 2, "added": 0, "existing": 2, "total": 2}
    assert _baseline_rows(db) == ["111111111111111111", "555555555555555555"]
