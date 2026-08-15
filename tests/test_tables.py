from __future__ import annotations

from email.message import Message
import json
import sqlite3
from urllib.error import HTTPError

import pytest

from oraja_training import cli
from oraja_training.cli import DEFAULT_TABLES
from oraja_training.db import store
from oraja_training.tables.fetch import (
    TableData,
    TableEntry,
    TableFetchError,
    fetch_table,
)
from oraja_training.tables.match import resolve


class Response:
    def __init__(self, value, *, etag=None, last_modified=None):
        self.value = json.dumps(value).encode() if not isinstance(value, bytes) else value
        self.headers = Message()
        if etag:
            self.headers["ETag"] = etag
        if last_modified:
            self.headers["Last-Modified"] = last_modified

    def read(self):
        return self.value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def test_default_sources_cover_four_independent_difficulty_tables():
    assert dict(DEFAULT_TABLES) == {
        "genocide": "https://nekokan.dyndns.info/~lobsak/genocide/insane.html",
        "overjoy": "https://lr2.sakura.ne.jp/data/header.json",
        "satellite": "https://stellabms.xyz/sl/table.html",
        "stella": "https://stellabms.xyz/st/table.html",
    }


def test_fetches_official_genocide_legacy_table_without_evaluating_javascript(
    tmp_path,
):
    source = """
    <html><meta charset="SHIFT_JIS"><script>
    var mname = [
      [1, "★1", "Ａ Chart", "15", "<a href=''>Artist</a>", "", "",],
      [2, "★2", "日本語 Chart", "22", "<a href=''>Artist</a>", "", "",],
    ];
    </script></html>
    """.encode("cp932")

    table = fetch_table(
        "genocide",
        "https://example.test/insane.html",
        cache_dir=tmp_path,
        opener=lambda _request, _timeout: Response(source),
    )

    assert table.header["format"] == "genocide-legacy"
    assert [entry.level for entry in table.entries] == ["★1", "★2"]
    assert table.entries[0].sha256 is None
    assert table.entries[0].data["_match"] == "unique_title"

    stale = fetch_table(
        "genocide",
        "https://example.test/insane.html",
        cache_dir=tmp_path,
        opener=lambda _request, _timeout: (_ for _ in ()).throw(OSError("offline")),
    )
    assert stale.stale is True
    assert stale.entries[0].data["_match"] == "unique_title"


def test_legacy_title_parser_is_not_enabled_for_other_tables():
    source = b'<script>var mname = [\n[1, "1", "Chart", "15", "", "", "",],\n];</script>'

    with pytest.raises(TableFetchError):
        fetch_table(
            "untrusted",
            "https://example.test/table.html",
            opener=lambda _request, _timeout: Response(source),
        )


def test_https_header_upgrades_same_host_http_data_url():
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        if request.full_url.endswith("header.json"):
            return Response(
                {"data_url": "http://example.test/data.json"}
            )
        return Response([{"level": 1, "sha256": "f" * 64}])

    table = fetch_table(
        "one", "https://example.test/header.json", opener=opener
    )

    assert calls[-1] == "https://example.test/data.json"
    assert table.data_url == "https://example.test/data.json"


def test_fetches_html_meta_and_resolves_relative_urls(tmp_path):
    calls = []
    sha = "a" * 64

    def opener(request, timeout):
        calls.append(request.full_url)
        values = {
            "https://example.test/list/index.html": Response(
                b'<HTML><meta CONTENT="headers/header.json" NAME="BMSTABLE"></HTML>'
            ),
            "https://example.test/list/headers/header.json": Response(
                {"name": "Table", "symbol": "x", "data_url": "../data.json"}
            ),
            "https://example.test/list/data.json": Response(
                [{"level": "1", "sha256": sha, "title": "Chart"}]
            ),
        }
        return values[request.full_url]

    table = fetch_table(
        "one", "https://example.test/list/index.html", cache_dir=tmp_path, opener=opener
    )

    assert calls == [
        "https://example.test/list/index.html",
        "https://example.test/list/headers/header.json",
        "https://example.test/list/data.json",
    ]
    assert table.header_url.endswith("/list/headers/header.json")
    assert table.data_url.endswith("/list/data.json")
    assert table.entries[0].sha256 == sha


def test_conditional_cache_uses_etag_and_last_modified(tmp_path):
    sha = "b" * 64
    seen_headers = []
    round_number = 0

    def opener(request, timeout):
        nonlocal round_number
        seen_headers.append(dict(request.header_items()))
        if round_number >= 2:
            raise HTTPError(request.full_url, 304, "Not Modified", Message(), None)
        round_number += 1
        if request.full_url.endswith("header.json"):
            return Response(
                {"data_url": "score.json"},
                etag='"header-1"',
                last_modified="Tue, 01 Jan 2030 00:00:00 GMT",
            )
        return Response([{"level": 1, "sha256": sha}], etag='"data-1"')

    url = "https://example.test/header.json"
    first = fetch_table("one", url, cache_dir=tmp_path, opener=opener)
    second = fetch_table("one", url, cache_dir=tmp_path, opener=opener)

    assert not first.from_cache
    assert second.from_cache
    assert seen_headers[2]["If-none-match"] == '"header-1"'
    assert seen_headers[2]["If-modified-since"] == "Tue, 01 Jan 2030 00:00:00 GMT"
    assert seen_headers[3]["If-none-match"] == '"data-1"'


def test_invalid_json_schema_is_rejected_without_cache():
    def opener(request, timeout):
        return Response({"name": "missing data URL"})

    with pytest.raises(TableFetchError, match="data_url"):
        fetch_table("bad", "https://example.test/header.json", opener=opener)


def test_external_json_cannot_request_legacy_title_only_matching():
    def opener(request, timeout):
        if request.full_url.endswith("header.json"):
            return Response({"data_url": "data.json"})
        return Response(
            [{"level": "1", "title": "Chart", "_match": "unique_title"}]
        )

    with pytest.raises(TableFetchError, match="no valid sha256 or md5"):
        fetch_table(
            "untrusted",
            "https://example.test/header.json",
            opener=opener,
        )


def test_last_good_cache_is_used_when_refresh_fails(tmp_path):
    sha = "c" * 64

    def good(request, timeout):
        if request.full_url.endswith("header.json"):
            return Response({"data_url": "data.json"})
        return Response([{"level": "2", "sha256": sha}])

    fetch_table(
        "one", "https://example.test/header.json", cache_dir=tmp_path, opener=good
    )

    def broken(request, timeout):
        raise OSError("offline")

    stale = fetch_table(
        "one", "https://example.test/header.json", cache_dir=tmp_path, opener=broken
    )
    assert stale.stale and stale.from_cache
    assert stale.entries[0].sha256 == sha


def test_stale_table_cache_is_persisted_as_a_visible_warning(
    tmp_path, monkeypatch
):
    assistant = tmp_path / "assistant.db"
    sha = "d" * 64
    conn = store.init(assistant)
    with conn:
        conn.execute(
            "INSERT INTO charts VALUES (?, NULL, 'Chart', 'Artist', 1000, 7, NULL, 1)",
            (sha,),
        )
    conn.close()
    table = TableData(
        table_id="satellite",
        source_url="https://example.test/table.html",
        header_url="https://example.test/header.json",
        data_url="https://example.test/data.json",
        header={"data_url": "data.json"},
        entries=(TableEntry("satellite", "sl1", sha, None, "Chart", {}),),
        fetched_at=123,
        from_cache=True,
        stale=True,
    )
    monkeypatch.setattr(cli, "fetch_table", lambda *args, **kwargs: table)

    result = cli._refresh_tables(
        assistant,
        tmp_path / "cache",
        (("satellite", "https://example.test/table.html"),),
    )

    conn = sqlite3.connect(assistant)
    try:
        error, entry_count, matched_count = conn.execute(
            "SELECT last_error, entry_count, matched_count "
            "FROM table_sources WHERE table_id='satellite'"
        ).fetchone()
    finally:
        conn.close()
    assert result["tables"][0]["stale"] is True
    assert error == "stale cache used after refresh failure"
    assert (entry_count, matched_count) == (1, 1)


def test_match_prefers_sha_then_falls_back_to_md5_and_separates_tables():
    sha_a, sha_b = "a" * 64, "b" * 64
    md5_a, md5_b = "1" * 32, "2" * 32
    charts = [
        {"sha256": sha_a.upper(), "md5": md5_a, "title": "A"},
        {"sha256": sha_b, "md5": md5_b, "title": "B"},
    ]
    entries = [
        {
            "table_id": "genocide",
            "level": "1",
            "sha256": sha_a,
            "md5": md5_b,
        },
        {"table_id": "satellite", "level": "sl1", "md5": md5_b},
        {"table_id": "satellite", "level": "sl2", "md5": "f" * 32},
    ]

    report = resolve(entries, charts)

    assert [match.matched_by for match in report.matches] == ["sha256", "md5"]
    assert report.matches[0].chart["title"] == "A"
    assert report.for_table("genocide").match_rate == 1.0
    assert report.for_table("satellite").match_rate == 0.5
    assert report.unmatched_entries[0].reason == "not_owned"


def test_match_preserves_one_chart_in_all_four_tables():
    sha = "e" * 64
    entries = [
        {"table_id": table_id, "level": level, "sha256": sha}
        for table_id, level in (
            ("genocide", "★4"),
            ("overjoy", "★★1"),
            ("satellite", "sl3"),
            ("stella", "st0"),
        )
    ]

    report = resolve(entries, [{"sha256": sha, "md5": None}])

    assert report.matched == 4
    assert {match.entry.table_id for match in report.matches} == {
        "genocide", "overjoy", "satellite", "stella",
    }


def test_ambiguous_md5_is_not_guessed():
    md5 = "d" * 32
    charts = [
        {"sha256": "1" * 64, "md5": md5},
        {"sha256": "2" * 64, "md5": md5},
    ]
    report = resolve([{"table_id": "x", "level": "1", "md5": md5}], charts)
    assert report.matched == 0
    assert report.unmatched_entries[0].reason == "ambiguous_local_hash"


def test_legacy_genocide_title_fallback_requires_one_unique_local_chart():
    entry = {
        "table_id": "genocide",
        "level": "★3",
        "title": "Ａ  Chart",
        "_match": "unique_title",
    }
    unique = resolve(
        [entry],
        [{"sha256": "1" * 64, "md5": None, "title": "A Chart"}],
    )
    assert unique.matched == 1
    assert unique.matches[0].matched_by == "title"
    assert unique.for_table("genocide").title_matches == 1

    ambiguous = resolve(
        [entry],
        [
            {"sha256": "1" * 64, "md5": None, "title": "A Chart"},
            {"sha256": "2" * 64, "md5": None, "title": "Ａ  Chart"},
        ],
    )
    assert ambiguous.matched == 0
    assert ambiguous.unmatched_entries[0].reason == "ambiguous_local_title"

    duplicate_table_titles = resolve(
        [entry, {**entry, "level": "★4"}],
        [{"sha256": "1" * 64, "md5": None, "title": "A Chart"}],
    )
    assert duplicate_table_titles.matched == 0
    assert {
        miss.reason for miss in duplicate_table_titles.unmatched_entries
    } == {"ambiguous_table_title"}
