from __future__ import annotations

from email.message import Message
import json
from urllib.error import HTTPError

import pytest

from oraja_training.tables.fetch import TableFetchError, fetch_table
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


def test_ambiguous_md5_is_not_guessed():
    md5 = "d" * 32
    charts = [
        {"sha256": "1" * 64, "md5": md5},
        {"sha256": "2" * 64, "md5": md5},
    ]
    report = resolve([{"table_id": "x", "level": "1", "md5": md5}], charts)
    assert report.matched == 0
    assert report.unmatched_entries[0].reason == "ambiguous_local_hash"
