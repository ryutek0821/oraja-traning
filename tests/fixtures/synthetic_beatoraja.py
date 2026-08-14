"""Build public-safe, deterministic beatoraja source and catalog fixtures.

The source databases are generated into pytest's temporary directory.  No
``player-file`` snapshot, BMS body, personal name, or machine path is needed
to run the collection, backfill, snapshot, and golden tests.

The default builder creates a coherent static five-DB snapshot.  Variant
builders intentionally create separate directories for failure-path tests;
the session fixture never points at one of those mutable variants.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


FIXTURE_VERSION = 2
DATABASE_NAMES = (
    "score.db",
    "scoredatalog.db",
    "scorelog.db",
    "songdata.db",
    "songinfo.db",
)
CATALOG_FILENAME = "catalog.json"
BASE_DATE = 1_700_000_000
MODEL_VERSION = 3

# These names are deliberately semantic rather than tied to a real player's
# row counts.  They are the review checklist for every fixture change.
REQUIRED_CASES = (
    "normal_play",
    "non_pb_play",
    "ir_import",
    "course_play",
    "partial_session",
    "missing_chart",
    "duplicate_chart_path",
    "time_boundary",
    "wal_sidecar",
    "shm_sidecar",
    "journal_sidecar",
    "partial_update",
    "corrupt_sqlite",
    "counter_rollback",
    "sp7_owned",
    "sp7_unowned",
    "hash_collision",
    "hash_missing",
    "model_state",
    "normalized_play",
    "recommend_candidates",
    "daily_menu",
    "runtime_optional_columns_absent",
)
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")

_SCORE_COLUMNS = (
    "sha256", "mode", "clear", "epg", "lpg", "egr", "lgr", "egd", "lgd",
    "ebd", "lbd", "epr", "lpr", "ems", "lms", "notes", "combo", "minbp",
    "avgjudge", "playcount", "clearcount", "trophy", "ghost", "option", "seed",
    "random", "date", "state", "scorehash",
)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _score_row(
    chart: int,
    *,
    clear: int,
    notes: int,
    judged: int,
    empty_poor: int,
    playcount: int,
    date: int,
    minbp: int,
) -> dict[str, Any]:
    sha256 = str(chart) * 64
    return {
        "sha256": sha256,
        "mode": 0,
        "clear": clear,
        "epg": judged,
        "lpg": 0,
        "egr": 0,
        "lgr": 0,
        "egd": 0,
        "lgd": 0,
        "ebd": 0,
        "lbd": 0,
        "epr": 0,
        "lpr": 0,
        "ems": empty_poor,
        "lms": 0,
        "notes": notes,
        "combo": notes if judged == notes else judged,
        "minbp": minbp,
        "avgjudge": 0,
        "playcount": playcount,
        "clearcount": playcount if clear else 0,
        "trophy": "synthetic",
        "ghost": "",
        "option": 1,
        "seed": 2,
        "random": 0,
        "date": date,
        "state": 0,
        "scorehash": "",
    }


def _create_score_database(
    path: Path,
    table: str,
    rows: list[dict[str, Any]],
    player_rows: list[dict[str, Any]] | None = None,
) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            f"""
            CREATE TABLE {table} (
              sha256 TEXT NOT NULL,
              mode INTEGER NOT NULL,
              clear INTEGER,
              epg INTEGER, lpg INTEGER, egr INTEGER, lgr INTEGER,
              egd INTEGER, lgd INTEGER, ebd INTEGER, lbd INTEGER,
              epr INTEGER, lpr INTEGER, ems INTEGER, lms INTEGER,
              notes INTEGER, combo INTEGER, minbp INTEGER, avgjudge INTEGER,
              playcount INTEGER, clearcount INTEGER, trophy TEXT, ghost TEXT,
              option INTEGER, seed INTEGER, random INTEGER, date INTEGER,
              state INTEGER, scorehash TEXT,
              PRIMARY KEY(sha256, mode)
            )
            """
        )
        placeholders = ", ".join("?" for _ in _SCORE_COLUMNS)
        columns = ", ".join(_SCORE_COLUMNS)
        conn.executemany(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
            [tuple(row[column] for column in _SCORE_COLUMNS) for row in rows],
        )
        if player_rows is not None:
            conn.execute(
                """
                CREATE TABLE player (
                  date INTEGER PRIMARY KEY,
                  playcount INTEGER NOT NULL,
                  playtime INTEGER NOT NULL,
                  epg INTEGER, lpg INTEGER, egr INTEGER, lgr INTEGER,
                  egd INTEGER, lgd INTEGER, ebd INTEGER, lbd INTEGER,
                  epr INTEGER, lpr INTEGER, ems INTEGER, lms INTEGER
                )
                """
            )
            player_columns = tuple(player_rows[0])
            player_placeholders = ", ".join("?" for _ in player_columns)
            conn.executemany(
                f"INSERT INTO player ({', '.join(player_columns)}) VALUES ({player_placeholders})",
                [tuple(row[column] for column in player_columns) for row in player_rows],
            )
        conn.commit()
    finally:
        conn.close()


def _create_scorelog(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        # Deliberately omit newer avgjudge columns: readers must use runtime
        # column discovery rather than assume one beatoraja schema revision.
        conn.execute(
            """
            CREATE TABLE scorelog (
              sha256 TEXT NOT NULL,
              mode INTEGER NOT NULL,
              clear INTEGER,
              oldclear INTEGER,
              score INTEGER,
              oldscore INTEGER,
              combo INTEGER,
              oldcombo INTEGER,
              minbp INTEGER,
              oldminbp INTEGER,
              date INTEGER
            )
            """
        )
        conn.execute(
            "INSERT INTO scorelog VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("1" * 64, 0, 6, 5, 190, 180, 100, 90, 5, 8, BASE_DATE + 60),
        )
        conn.commit()
    finally:
        conn.close()


def _create_songdata(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE song (
              sha256 TEXT NOT NULL,
              md5 TEXT,
              title TEXT,
              artist TEXT,
              notes INTEGER,
              mode INTEGER,
              path TEXT
            )
            """
        )
        rows = [
            ("1" * 64, "1" * 32, "Synthetic Chart 1", "Synthetic Artist", 100, 0, "synthetic/chart-1.bms"),
            ("2" * 64, "2" * 32, "Synthetic Chart 2", "Synthetic Artist", 80, 0, "synthetic/chart-2.bms"),
            ("3" * 64, "3" * 32, "Synthetic Chart 3", "Synthetic Artist", 120, 0, "synthetic/chart-3.bms"),
            ("4" * 64, "4" * 32, "Synthetic Chart 4", "Synthetic Artist", 50, 0, "synthetic/chart-4.bms"),
            # Duplicate path identity is intentionally represented by the
            # same content hash with a different relative song path.
            ("1" * 64, "1" * 32, "Synthetic Chart 1", "Synthetic Artist", 100, 0, "synthetic/alternate/chart-1.bms"),
        ]
        conn.executemany("INSERT INTO song VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        conn.commit()
    finally:
        conn.close()


def _create_songinfo(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE information (
              sha256 TEXT NOT NULL PRIMARY KEY,
              distribution TEXT,
              speedchange TEXT,
              lanenotes TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO information VALUES (?, ?, ?, ?)",
            [
                (str(chart) * 64, "#00000000000500", "120,0,120,1000", "5,0,0")
                for chart in range(1, 5)
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _catalog_chart(index: int, *, owned: bool = True) -> dict[str, Any]:
    clear = 6 if index % 13 <= 3 else 4 if index % 13 <= 6 else 1
    return {
        "sha256": f"{index:064x}",
        "md5": f"{index:032x}",
        "title": f"Synthetic Catalog Chart {index:03d}",
        "artist": "Synthetic Catalog Artist",
        "notes": 1_800 + index % 900,
        "game_mode": "SP7",
        "owned": owned,
        "table_id": "satellite" if index % 2 else "genocide",
        "level": str(index % 13),
        "clear": clear,
        "playcount": 4 if clear >= 4 else index % 3,
        "last_played": BASE_DATE - (index % 20) * 86_400,
        "density": 8.0 + index % 8,
        "scratch": 0.12 + (index % 10) / 100,
        "model_scratch": (index % 12) / 20,
        "ln": 0.10 + (index % 7) / 100,
        "soflan": index % 5,
    }


def _build_catalog() -> dict[str, Any]:
    owned = [_catalog_chart(index) for index in range(1, 181)]
    collision_a = _catalog_chart(181)
    collision_b = _catalog_chart(182)
    collision_a["md5"] = collision_b["md5"] = "f" * 32
    owned.extend((collision_a, collision_b))
    unowned = [_catalog_chart(900, owned=False)]
    unowned[0]["table_id"] = "satellite"
    table_entries = [
        {
            "table_id": row["table_id"],
            "level": row["level"],
            "sha256": row["sha256"],
            "md5": row["md5"],
            "title": row["title"],
            "game_mode": row["game_mode"],
        }
        for row in owned
    ]
    table_entries.extend(
        {
            "table_id": row["table_id"],
            "level": row["level"],
            "sha256": row["sha256"],
            "md5": row["md5"],
            "title": row["title"],
            "game_mode": row["game_mode"],
        }
        for row in unowned
    )
    # A duplicate table row exercises deduplication by content hash.
    table_entries.append({**table_entries[0], "table_id": "genocide"})
    # A remote entry with only MD5 must never become a guessed ownership hit.
    table_entries.append(
        {
            "table_id": "satellite",
            "level": "sl-missing",
            "sha256": None,
            "md5": "e" * 32,
            "title": "Synthetic Missing Hash Entry",
            "game_mode": "SP7",
        }
    )
    return {
        "catalog_version": 1,
        "source": "synthetic",
        "game_mode": "SP7",
        "owned": owned,
        "unowned": unowned,
        "table_entries": table_entries,
        "hash_cases": {
            "collision_md5": "f" * 32,
            "collision_sha256": [collision_a["sha256"], collision_b["sha256"]],
            "missing_sha256_entries": 1,
        },
        "model": {
            "target": "observed_completion",
            "version": MODEL_VERSION,
            "trained_at": BASE_DATE,
            "n_train": 240,
            "weights": [0.0, 0.0, 0.0, 0.0],
            "means": [0.0, 0.0, 0.0],
            "scales": [1.0, 1.0, 1.0],
            "feature_names": ["level", "density_p99", "scratch_rate"],
            "metrics": {"logloss": 0.4, "brier": 0.2, "baseline_logloss": 0.5},
            "description": "Synthetic validated model; no player data.",
        },
    }


def _manifest() -> dict[str, Any]:
    return {
        "fixture_version": FIXTURE_VERSION,
        "source": "synthetic",
        "databases": list(DATABASE_NAMES),
        "rows": {
            "score.score": 4,
            "score.player": 2,
            "scoredatalog.scoredatalog": 4,
            "scorelog.scorelog": 1,
            "songdata.song": 5,
            "songinfo.information": 4,
        },
        "cases": list(REQUIRED_CASES),
        "scenario_expectations": {
            "normal_play": "latest scoredatalog row has a positive date",
            "non_pb_play": "a non-perfect scored row retains minbp and empty-poor",
            "ir_import": "date=0 aggregate rows are imported without play derivations",
            "course_play": "clear=0 legacy rows are marked as course-like",
            "partial_session": "clear=0/playcount=0 rows are not IR plays",
            "missing_chart": "catalog has an unowned SP7 entry",
            "duplicate_chart_path": "same sha256 appears at two relative paths",
            "time_boundary": "player rows straddle exactly one UTC day",
            "wal_sidecar": "static snapshot rejects score.db-wal",
            "shm_sidecar": "static snapshot rejects score.db-shm",
            "journal_sidecar": "static snapshot rejects score.db-journal",
            "partial_update": "scoredatalog can be newer than score",
            "corrupt_sqlite": "invalid SQLite bytes are isolated in a variant",
            "counter_rollback": "cumulative player counters move backwards",
            "sp7_owned": "catalog contains owned SP7 charts",
            "sp7_unowned": "catalog contains an unowned SP7 chart",
            "hash_collision": "two local charts share one MD5",
            "hash_missing": "one table entry has no SHA-256",
            "model_state": "catalog model is a fixed validated snapshot",
            "normalized_play": "backfill golden stores normalized play columns",
            "recommend_candidates": "catalog owns enough candidates for the daily budget",
            "daily_menu": "golden covers 100k core plus 10k reserve",
            "runtime_optional_columns_absent": "scorelog omits avgjudge columns",
        },
        "catalog": {
            "file": CATALOG_FILENAME,
            "game_mode": "SP7",
            "model_version": MODEL_VERSION,
        },
        "privacy": {
            "path_prefix": "synthetic/",
            "forbidden_categories": ["absolute_path", "personal_identifier", "bms_body"],
            "contains_bms_body": False,
        },
    }


def _build_base(root: str | Path) -> Path:
    root = Path(root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    scoredatalog_rows = [
        _score_row(1, clear=6, notes=100, judged=95, empty_poor=3, playcount=3, date=BASE_DATE + 100, minbp=5),
        _score_row(2, clear=0, notes=80, judged=76, empty_poor=1, playcount=2, date=BASE_DATE + 200, minbp=8),
        _score_row(3, clear=4, notes=120, judged=120, empty_poor=0, playcount=1, date=BASE_DATE + 300, minbp=2),
        _score_row(4, clear=0, notes=50, judged=0, empty_poor=0, playcount=0, date=0, minbp=0),
    ]
    score_rows = [dict(row) for row in scoredatalog_rows]
    # The aggregate score for chart 1 is lower than the latest snapshot, so
    # the backfill golden covers the exceeded-aggregate branch.
    score_rows[0]["epg"] = 85
    for row in score_rows:
        row["date"] = 0

    player_rows = [
        {
            "date": BASE_DATE,
            "playcount": 10,
            "playtime": 3_000,
            "epg": 80, "lpg": 20, "egr": 5, "lgr": 0,
            "egd": 3, "lgd": 0, "ebd": 1, "lbd": 0,
            "epr": 1, "lpr": 0, "ems": 3, "lms": 1,
        },
        {
            "date": BASE_DATE + 86_400,
            "playcount": 12,
            "playtime": 4_000,
            "epg": 100, "lpg": 25, "egr": 8, "lgr": 0,
            "egd": 4, "lgd": 0, "ebd": 2, "lbd": 0,
            "epr": 1, "lpr": 0, "ems": 4, "lms": 1,
        },
    ]

    _create_score_database(root / "score.db", "score", score_rows, player_rows)
    _create_score_database(root / "scoredatalog.db", "scoredatalog", scoredatalog_rows)
    _create_scorelog(root / "scorelog.db")
    _create_songdata(root / "songdata.db")
    _create_songinfo(root / "songinfo.db")
    _write_json(root / CATALOG_FILENAME, _build_catalog())
    _write_json(root / "manifest.json", _manifest())
    return root


def build_synthetic_fixture(root: str | Path) -> Path:
    """Create and return a deterministic, coherent five-DB source set."""

    return _build_base(root)


def _set_variant(root: Path, name: str, **details: Any) -> Path:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["variant"] = {"name": name, **details}
    _write_json(root / "manifest.json", manifest)
    return root


def build_sidecar_fixture(root: str | Path, suffix: str) -> Path:
    """Create a static snapshot plus one named SQLite sidecar marker."""

    if suffix not in SIDECAR_SUFFIXES:
        raise ValueError(f"unsupported sidecar suffix: {suffix}")
    fixture = _build_base(root)
    (fixture / f"score.db{suffix}").write_bytes(b"synthetic sidecar marker\n")
    return _set_variant(fixture, "sidecar", suffix=suffix)


def build_corrupt_sqlite_fixture(root: str | Path) -> Path:
    """Create a variant whose score DB is deliberately not SQLite."""

    fixture = _build_base(root)
    (fixture / "score.db").write_bytes(b"not a sqlite database\n")
    return _set_variant(fixture, "corrupt_sqlite", file="score.db")


def build_counter_rollback_fixture(root: str | Path) -> Path:
    """Create a score DB whose cumulative player counters move backwards."""

    fixture = _build_base(root)
    conn = sqlite3.connect(fixture / "score.db")
    try:
        conn.execute(
            """
            UPDATE player SET
              playcount = 9, playtime = 2000,
              epg = 50, lpg = 10, egr = 1, lgr = 0,
              egd = 1, lgd = 0, ebd = 0, lbd = 0,
              epr = 0, lpr = 0, ems = 1, lms = 0
            WHERE date = ?
            """,
            (BASE_DATE + 86_400,),
        )
        conn.commit()
    finally:
        conn.close()
    return _set_variant(fixture, "counter_rollback", expected="SnapshotPairMismatch")


def build_partial_update_fixture(root: str | Path) -> Path:
    """Create a pair where scoredatalog advanced before score was copied."""

    fixture = _build_base(root)
    conn = sqlite3.connect(fixture / "scoredatalog.db")
    try:
        conn.execute(
            "UPDATE scoredatalog SET playcount = playcount + 3, date = date + 60 "
            "WHERE sha256 = ? AND mode = 0",
            ("1" * 64,),
        )
        conn.commit()
    finally:
        conn.close()
    return _set_variant(fixture, "partial_update", expected="SnapshotPairMismatch")


def apply_coherent_update(root: str | Path) -> Path:
    """Advance both score DBs and the cumulative row for transition tests."""

    fixture = Path(root).expanduser().resolve(strict=True)
    for filename, table in (("score.db", "score"), ("scoredatalog.db", "scoredatalog")):
        conn = sqlite3.connect(fixture / filename)
        try:
            conn.execute(
                f"UPDATE {table} SET playcount = playcount + 3, date = date + 60 "
                "WHERE sha256 = ? AND mode = 0",
                ("1" * 64,),
            )
            conn.commit()
        finally:
            conn.close()
    conn = sqlite3.connect(fixture / "score.db")
    try:
        conn.execute(
            "UPDATE player SET playcount = playcount + 3, epg = epg + 300 "
            "WHERE date = (SELECT max(date) FROM player)"
        )
        conn.commit()
    finally:
        conn.close()
    return _set_variant(fixture, "coherent_update", expected="accepted")


def load_catalog(root: str | Path) -> dict[str, Any]:
    """Load the public-safe recommendation catalog from a built fixture."""

    path = Path(root).expanduser().resolve(strict=True) / CATALOG_FILENAME
    return json.loads(path.read_text(encoding="utf-8"))
