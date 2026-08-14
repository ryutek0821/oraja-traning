"""Fetch bmstable difficulty tables with conditional, last-good caching."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen as _urlopen


_HEX_32 = re.compile(r"^[0-9a-fA-F]{32}$")
_HEX_64 = re.compile(r"^[0-9a-fA-F]{64}$")


class TableFetchError(RuntimeError):
    """Raised when a table cannot be fetched or fails schema validation."""


@dataclass(frozen=True, slots=True)
class TableEntry:
    """One validated bmstable data entry."""

    table_id: str
    level: str
    sha256: str | None
    md5: str | None
    title: str | None
    data: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class TableData:
    """A complete, validated difficulty table."""

    table_id: str
    source_url: str
    header_url: str
    data_url: str
    header: Mapping[str, Any]
    entries: tuple[TableEntry, ...]
    fetched_at: int
    from_cache: bool = False
    stale: bool = False


class _BmstableMetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.header_url: str | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.casefold() != "meta" or self.header_url is not None:
            return
        values = {key.casefold(): value for key, value in attrs}
        if (values.get("name") or "").casefold() == "bmstable":
            content = values.get("content")
            if content and content.strip():
                self.header_url = content.strip()


@dataclass(frozen=True, slots=True)
class _Response:
    body: bytes | None
    etag: str | None
    last_modified: str | None
    not_modified: bool


def _default_open(request: Request, timeout: float) -> Any:
    return _urlopen(request, timeout=timeout)


def _request(
    url: str,
    *,
    timeout: float,
    validators: Mapping[str, Any] | None,
    opener: Callable[[Request, float], Any],
) -> _Response:
    headers = {"Accept": "application/json, text/html;q=0.9, */*;q=0.1"}
    if validators:
        if validators.get("etag"):
            headers["If-None-Match"] = str(validators["etag"])
        if validators.get("last_modified"):
            headers["If-Modified-Since"] = str(validators["last_modified"])
    request = Request(url, headers=headers)
    try:
        response = opener(request, timeout)
        with response:
            body = response.read()
            response_headers = response.headers
        return _Response(
            body=body,
            etag=response_headers.get("ETag"),
            last_modified=response_headers.get("Last-Modified"),
            not_modified=False,
        )
    except HTTPError as exc:
        if exc.code == 304:
            return _Response(None, None, None, True)
        raise TableFetchError(f"HTTP {exc.code} while fetching {url}") from exc
    except (OSError, TimeoutError) as exc:
        raise TableFetchError(f"failed to fetch {url}: {exc}") from exc


def _decode_json(body: bytes, url: str) -> Any:
    try:
        return json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TableFetchError(f"invalid JSON from {url}: {exc}") from exc


def _header_url_from_html(body: bytes, source_url: str) -> str:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = body.decode("cp932", errors="replace")
    parser = _BmstableMetaParser()
    parser.feed(text)
    if parser.header_url is None:
        raise TableFetchError(f"no bmstable meta tag found at {source_url}")
    return urljoin(source_url, parser.header_url)


def _secure_same_host_url(base_url: str, resolved_url: str) -> str:
    """Avoid mixed-content HTTP when an HTTPS table points at the same host."""

    base = urlsplit(base_url)
    resolved = urlsplit(resolved_url)
    if (
        base.scheme == "https"
        and resolved.scheme == "http"
        and base.hostname == resolved.hostname
    ):
        return urlunsplit(resolved._replace(scheme="https"))
    return resolved_url


def _validate_header(value: Any, url: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TableFetchError(f"header from {url} must be a JSON object")
    data_url = value.get("data_url")
    if not isinstance(data_url, str) or not data_url.strip():
        raise TableFetchError(f"header from {url} has no valid data_url")
    for key in ("name", "symbol"):
        if key in value and not isinstance(value[key], str):
            raise TableFetchError(f"header field {key!r} must be a string")
    if "level_order" in value and not isinstance(value["level_order"], list):
        raise TableFetchError("header field 'level_order' must be an array")
    return value


def _normal_hash(value: Any, pattern: re.Pattern[str]) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or pattern.fullmatch(value.strip()) is None:
        return None
    return value.strip().lower()


def _validate_entries(value: Any, table_id: str, url: str) -> tuple[TableEntry, ...]:
    if not isinstance(value, list):
        raise TableFetchError(f"table data from {url} must be a JSON array")
    entries: list[TableEntry] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise TableFetchError(f"entry {index} from {url} must be an object")
        level = raw.get("level")
        if not isinstance(level, (str, int, float)) or isinstance(level, bool):
            raise TableFetchError(f"entry {index} has no valid level")
        sha256 = _normal_hash(raw.get("sha256"), _HEX_64)
        md5 = _normal_hash(raw.get("md5"), _HEX_32)
        if sha256 is None and md5 is None:
            raise TableFetchError(f"entry {index} has no valid sha256 or md5")
        title = raw.get("title")
        if title is not None and not isinstance(title, str):
            raise TableFetchError(f"entry {index} title must be a string")
        entries.append(
            TableEntry(
                table_id=table_id,
                level=str(level),
                sha256=sha256,
                md5=md5,
                title=title,
                data=dict(raw),
            )
        )
    return tuple(entries)


def _cache_path(cache_dir: Path, table_id: str, source_url: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", table_id).strip("._") or "table"
    suffix = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:12]
    return cache_dir / f"{safe_id}-{suffix}.json"


def _load_cache(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _save_cache(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(path)


def _cached_table(cache: Mapping[str, Any], *, stale: bool) -> TableData:
    header = _validate_header(cache.get("header"), str(cache.get("header_url", "cache")))
    entries = _validate_entries(
        cache.get("data"), str(cache["table_id"]), str(cache.get("data_url", "cache"))
    )
    return TableData(
        table_id=str(cache["table_id"]),
        source_url=str(cache["source_url"]),
        header_url=str(cache["header_url"]),
        data_url=str(cache["data_url"]),
        header=header,
        entries=entries,
        fetched_at=int(cache["fetched_at"]),
        from_cache=True,
        stale=stale,
    )


def fetch_table(
    table_id: str,
    header_url: str,
    *,
    cache_dir: str | Path | None = None,
    timeout: float = 15.0,
    allow_stale_on_error: bool = True,
    opener: Callable[[Request, float], Any] | None = None,
) -> TableData:
    """Fetch and validate a bmstable header and data document.

    ``header_url`` may point directly to a JSON header or to an HTML page with
    ``<meta name="bmstable" content="...">``.  Relative header and data URLs
    are resolved against the document that contains them.  When ``cache_dir``
    is provided, ETag and Last-Modified validators are sent on later requests.
    """

    if not table_id or not isinstance(table_id, str):
        raise ValueError("table_id must be a non-empty string")
    if not isinstance(header_url, str) or not header_url.strip():
        raise ValueError("header_url must be a non-empty URL")
    source_url = header_url.strip()
    cache_path = (
        _cache_path(Path(cache_dir), table_id, source_url) if cache_dir is not None else None
    )
    cache = _load_cache(cache_path) if cache_path is not None else None
    open_resource = opener or _default_open

    try:
        source_response = _request(
            source_url,
            timeout=timeout,
            validators=(cache or {}).get("source_validators"),
            opener=open_resource,
        )
        source_kind = str((cache or {}).get("source_kind", ""))
        if source_response.not_modified:
            if cache is None:
                raise TableFetchError("received 304 without a cached source")
            resolved_header_url = str(cache["header_url"])
            header = _validate_header(cache.get("header"), resolved_header_url)
        else:
            assert source_response.body is not None
            try:
                source_json = _decode_json(source_response.body, source_url)
            except TableFetchError:
                resolved_header_url = _header_url_from_html(source_response.body, source_url)
                source_kind = "html"
            else:
                header = _validate_header(source_json, source_url)
                resolved_header_url = source_url
                source_kind = "json"

        header_response: _Response | None = None
        if source_kind == "html":
            header_response = _request(
                resolved_header_url,
                timeout=timeout,
                validators=(cache or {}).get("header_validators"),
                opener=open_resource,
            )
            if header_response.not_modified:
                if cache is None:
                    raise TableFetchError("received 304 without a cached header")
                header = _validate_header(cache.get("header"), resolved_header_url)
            else:
                assert header_response.body is not None
                header = _validate_header(
                    _decode_json(header_response.body, resolved_header_url),
                    resolved_header_url,
                )

        data_url = _secure_same_host_url(
            resolved_header_url,
            urljoin(resolved_header_url, str(header["data_url"]).strip()),
        )
        old_data_url = str((cache or {}).get("data_url", ""))
        data_response = _request(
            data_url,
            timeout=timeout,
            validators=(cache or {}).get("data_validators") if data_url == old_data_url else None,
            opener=open_resource,
        )
        if data_response.not_modified:
            if cache is None:
                raise TableFetchError("received 304 without cached table data")
            raw_data = cache.get("data")
        else:
            assert data_response.body is not None
            raw_data = _decode_json(data_response.body, data_url)
        entries = _validate_entries(raw_data, table_id, data_url)
        fetched_at = int(time.time())
        result = TableData(
            table_id=table_id,
            source_url=source_url,
            header_url=resolved_header_url,
            data_url=data_url,
            header=header,
            entries=entries,
            fetched_at=fetched_at,
            from_cache=source_response.not_modified and data_response.not_modified,
        )
        if cache_path is not None:
            def validators(response: _Response | None, old: Any) -> dict[str, Any]:
                if response is None or response.not_modified:
                    return dict(old or {})
                return {"etag": response.etag, "last_modified": response.last_modified}

            _save_cache(
                cache_path,
                {
                    "table_id": table_id,
                    "source_url": source_url,
                    "source_kind": source_kind,
                    "header_url": resolved_header_url,
                    "data_url": data_url,
                    "header": header,
                    "data": raw_data,
                    "fetched_at": fetched_at,
                    "source_validators": validators(
                        source_response, (cache or {}).get("source_validators")
                    ),
                    "header_validators": validators(
                        header_response, (cache or {}).get("header_validators")
                    ),
                    "data_validators": validators(
                        data_response, (cache or {}).get("data_validators")
                    ),
                },
            )
        return result
    except (TableFetchError, KeyError, TypeError, ValueError) as exc:
        if allow_stale_on_error and cache is not None:
            try:
                return _cached_table(cache, stale=True)
            except (TableFetchError, KeyError, TypeError, ValueError):
                pass
        if isinstance(exc, TableFetchError):
            raise
        raise TableFetchError(f"invalid cache or table data for {table_id}: {exc}") from exc
