from __future__ import annotations

import sqlite3

import pytest

from oraja_training import cli
from oraja_training.db import store
from oraja_training.db.classification_adapter import (
    load_chart_classifications,
    record_classification_error,
    replace_classification_source,
)
from oraja_training.db.recommendation_adapter import SQLiteRecommendationRepository
from oraja_training.domain import ProfileContext
from oraja_training.tables.classification import (
    DEFAULT_CLASSIFICATION_SOURCES,
    ClassificationError,
    ClassificationSourceSpec,
    build_classification_batch,
)
from oraja_training.tables.fetch import TableData, TableEntry


def _source(source_id: str = "slst-delay") -> ClassificationSourceSpec:
    return next(
        source
        for source in DEFAULT_CLASSIFICATION_SOURCES
        if source.source_id == source_id
    )


def _table(
    source: ClassificationSourceSpec,
    entries: tuple[TableEntry, ...],
    *,
    stale: bool = False,
    fetched_at: int = 123,
) -> TableData:
    return TableData(
        table_id=source.source_id,
        source_url=source.url,
        header_url="https://example.test/header.json",
        data_url="https://example.test/data.json",
        header={"name": "Classification", "data_url": "data.json"},
        entries=entries,
        fetched_at=fetched_at,
        from_cache=stale,
        stale=stale,
    )


def _entry(
    source: ClassificationSourceSpec,
    level: str,
    *,
    sha256: str | None = None,
    md5: str | None = None,
    title: str = "Chart",
) -> TableEntry:
    data = {"level": level, "title": title}
    if sha256 is not None:
        data["sha256"] = sha256
    if md5 is not None:
        data["md5"] = md5
    return TableEntry(source.source_id, level, sha256, md5, title, data)


def test_default_sources_cover_four_explicit_pattern_families() -> None:
    assert {
        source.source_id: (source.family, source.scales)
        for source in DEFAULT_CLASSIFICATION_SOURCES
    } == {
        "slst-code-stream": ("code_stream", ("乱打", "重発狂")),
        "slst-mini-jack": ("mini_jack", ("微縦連", "連打複合")),
        "slst-arm": ("arm", ("Ude", "腕")),
        "slst-delay": ("delay", ("///", "dl")),
    }


def test_multi_axis_delay_and_negative_level_are_preserved() -> None:
    source = _source()
    sha256, md5 = "a" * 64, "1" * 32
    table = _table(
        source,
        (_entry(source, "sl10,dl-2,///10", sha256=sha256, md5=md5),),
    )

    batch = build_classification_batch(source, table, ({"sha256": sha256, "md5": md5},))

    assert batch.entry_count == 1
    assert batch.classification_count == 2
    assert batch.matched_count == 1
    assert {
        (
            row.base_scale,
            row.base_level,
            row.classification_scale,
            row.classification_level,
        )
        for row in batch.rows
    } == {("sl", 10, "dl", -2), ("sl", 10, "///", 10)}
    assert {row.raw_level for row in batch.rows} == {"sl10,dl-2,///10"}
    assert {row.match_status for row in batch.rows} == {"sha256"}


def test_unknown_or_repeated_classification_axis_is_rejected() -> None:
    source = _source()
    for raw_level in ("sl3,unknown5", "sl3,dl1,dl2", "3,dl1", "sl3"):
        table = _table(
            source,
            (_entry(source, raw_level, sha256="a" * 64, md5="1" * 32),),
        )
        with pytest.raises(ClassificationError):
            build_classification_batch(source, table, ())


def test_present_malformed_hash_is_not_silently_discarded() -> None:
    source = _source("slst-arm")
    entry = TableEntry(
        source.source_id,
        "sl2,Ude3",
        None,
        "1" * 32,
        "Chart",
        {
            "level": "sl2,Ude3",
            "sha256": "not-a-sha256",
            "md5": "1" * 32,
            "title": "Chart",
        },
    )

    with pytest.raises(ClassificationError, match="invalid sha256"):
        build_classification_batch(source, _table(source, (entry,)), ())


def test_duplicate_source_chart_hash_is_rejected_before_persistence() -> None:
    source = _source("slst-delay")
    sha256 = "a" * 64
    entries = (
        _entry(source, "sl3,dl1", sha256=sha256, md5="1" * 32),
        _entry(source, "sl3,///2", sha256=sha256, md5="2" * 32),
    )

    with pytest.raises(ClassificationError, match="repeats sha256"):
        build_classification_batch(source, _table(source, entries), ())


def test_md5_fallback_and_ambiguous_match_status_are_visible() -> None:
    source = _source("slst-arm")
    unique_md5, ambiguous_md5 = "1" * 32, "2" * 32
    entries = (
        _entry(source, "sl2,Ude3", md5=unique_md5),
        _entry(source, "st1,腕2", md5=ambiguous_md5),
    )
    charts = (
        {"sha256": "a" * 64, "md5": unique_md5},
        {"sha256": "b" * 64, "md5": ambiguous_md5},
        {"sha256": "c" * 64, "md5": ambiguous_md5},
    )

    batch = build_classification_batch(source, _table(source, entries), charts)

    assert batch.matched_count == 1
    by_md5 = {row.md5: row for row in batch.rows}
    assert by_md5[unique_md5].local_sha256 == "a" * 64
    assert by_md5[unique_md5].match_status == "md5"
    assert by_md5[ambiguous_md5].local_sha256 is None
    assert by_md5[ambiguous_md5].match_status == "ambiguous_local_hash"


def test_replace_is_idempotent_and_error_keeps_last_good_rows(tmp_path) -> None:
    path = tmp_path / "assistant.db"
    conn = store.init(path)
    source = _source("slst-code-stream")
    sha256, md5 = "a" * 64, "1" * 32
    with conn:
        conn.execute(
            "INSERT INTO charts VALUES (?, ?, 'Chart', NULL, 100, 7, NULL, 1)",
            (sha256, md5),
        )
    batch = build_classification_batch(
        source,
        _table(
            source,
            (_entry(source, "sl4,乱打5", sha256=sha256, md5=md5),),
        ),
        ({"sha256": sha256, "md5": md5},),
    )

    with conn:
        replace_classification_source(conn, batch)
        replace_classification_source(conn, batch)
    assert conn.execute("SELECT count(*) FROM chart_classifications").fetchone()[0] == 1
    assert load_chart_classifications(conn)[0]["classification_level"] == 5

    with conn:
        record_classification_error(conn, source, "TableFetchError: offline")
    assert conn.execute("SELECT count(*) FROM chart_classifications").fetchone()[0] == 1
    assert (
        "offline"
        in conn.execute(
            "SELECT last_error FROM classification_sources WHERE source_id = ?",
            (source.source_id,),
        ).fetchone()[0]
    )
    assert conn.execute(
        "SELECT stale FROM classification_sources WHERE source_id = ?",
        (source.source_id,),
    ).fetchone()[0] == 1
    conn.close()


def test_stale_batch_is_persisted_with_digest_and_warning(tmp_path) -> None:
    conn = store.init(tmp_path / "assistant.db")
    source = _source("slst-mini-jack")
    sha256, md5 = "d" * 64, "4" * 32
    with conn:
        conn.execute(
            "INSERT INTO charts VALUES (?, ?, 'Chart', NULL, 100, 7, NULL, 1)",
            (sha256, md5),
        )
    batch = build_classification_batch(
        source,
        _table(
            source,
            (_entry(source, "st2,連打複合3", sha256=sha256, md5=md5),),
            stale=True,
        ),
        ({"sha256": sha256, "md5": md5},),
    )
    with conn:
        replace_classification_source(conn, batch)

    digest, stale, error, entries, classifications, matched = conn.execute(
        "SELECT content_digest, stale, last_error, entry_count, "
        "classification_count, matched_count FROM classification_sources"
    ).fetchone()
    assert len(digest) == 64
    assert (stale, entries, classifications, matched) == (1, 1, 1, 1)
    assert error == "stale cache used after refresh failure"
    conn.close()


def test_recommendation_input_exposes_classifications_without_candidate_join(
    tmp_path,
) -> None:
    conn = store.init(tmp_path / "assistant.db")
    source = _source("slst-arm")
    sha256, md5 = "e" * 64, "5" * 32
    with conn:
        conn.execute(
            "INSERT INTO charts VALUES (?, ?, 'Chart', NULL, 100, 7, NULL, 1)",
            (sha256, md5),
        )
        replace_classification_source(
            conn,
            build_classification_batch(
                source,
                _table(
                    source,
                    (_entry(source, "sl6,Ude7", sha256=sha256, md5=md5),),
                ),
                ({"sha256": sha256, "md5": md5},),
            ),
        )

    recommendation_input = SQLiteRecommendationRepository(conn).load_input(
        ProfileContext()
    )

    assert recommendation_input.candidates == ()
    assert recommendation_input.classifications == (
        {
            "sha256": sha256,
            "source_id": "slst-arm",
            "family": "arm",
            "base_scale": "sl",
            "base_level": 6,
            "classification_scale": "Ude",
            "classification_level": 7,
            "raw_level": "sl6,Ude7",
            "match_status": "sha256",
        },
    )
    conn.close()


def test_cli_source_override_rejects_unknown_and_duplicate_ids() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        cli._classification_specs(("unknown=https://example.test/header.json",))
    with pytest.raises(ValueError, match="duplicate"):
        cli._classification_specs(
            (
                "slst-arm=https://one.test/header.json",
                "slst-arm=https://two.test/header.json",
            )
        )


def test_cli_refresh_commits_each_source_and_reports_partial_failure(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "assistant.db"
    conn = store.init(path)
    sha256, md5 = "f" * 64, "6" * 32
    with conn:
        conn.execute(
            "INSERT INTO charts VALUES (?, ?, 'Chart', NULL, 100, 7, NULL, 1)",
            (sha256, md5),
        )
    conn.close()
    good, bad = _source("slst-arm"), _source("slst-delay")

    def fake_fetch(source_id, url, *, cache_dir):
        del url, cache_dir
        if source_id == bad.source_id:
            raise OSError("offline")
        return _table(
            good,
            (_entry(good, "sl2,Ude3", sha256=sha256, md5=md5),),
        )

    monkeypatch.setattr(cli, "fetch_table", fake_fetch)
    result = cli._refresh_classifications(path, tmp_path / "cache", (good, bad))

    assert result["classifications"][0]["matched"] == 1
    assert result["errors"] == [{"source_id": "slst-delay", "error": "offline"}]
    conn = sqlite3.connect(path)
    try:
        assert (
            conn.execute("SELECT count(*) FROM chart_classifications").fetchone()[0]
            == 1
        )
        assert (
            "offline"
            in conn.execute(
                "SELECT last_error FROM classification_sources WHERE source_id = ?",
                (bad.source_id,),
            ).fetchone()[0]
        )
    finally:
        conn.close()
