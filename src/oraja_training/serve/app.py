"""Read-only local HTTP server for the beatoraja training cockpit."""

from __future__ import annotations

from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import ipaddress
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any
from urllib.parse import unquote, urlsplit


JUDGEMENT_COLUMNS = (
    "epg", "lpg", "egr", "lgr", "egd", "lgd", "ebd", "lbd", "epr", "lpr"
)

_EXPORT_ROUTES = {
    "/table/recommend/header.json": ("table", "recommend", "header.json"),
    "/table/recommend/score.json": ("table", "recommend", "score.json"),
    "/table/today/header.json": ("table", "today", "header.json"),
    "/table/today/score.json": ("table", "today", "score.json"),
    "/api/session": ("session.json",),
    "/api/review/latest": ("review", "latest.json"),
}

_MAX_PROGRESS_BODY = 4096
_TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as source:
        return json.load(source)


def _read_latest_judged(score_db: Path) -> int:
    uri = f"{score_db.expanduser().resolve(strict=True).as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=0.1)
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 100")
        columns = ", ".join(f'"{name}"' for name in JUDGEMENT_COLUMNS)
        row = connection.execute(
            f"SELECT {columns} FROM player ORDER BY date DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("player table is empty")
        return sum(max(0, int(value or 0)) for value in row)
    finally:
        connection.close()


def _validate_progress_bind(host: str, progress_token: str | None) -> None:
    """Keep the authenticated progress receiver off LAN/wildcard interfaces."""

    if progress_token is None:
        return
    if host == "localhost":
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise ValueError(
            "progress receiver host must be a loopback or exact Tailscale IP"
        ) from error
    if not (
        address.is_loopback
        or address in _TAILSCALE_V4
        or address in _TAILSCALE_V6
    ):
        raise ValueError(
            "progress receiver host must be a loopback or exact Tailscale IP"
        )


class TrainingHTTPServer(ThreadingHTTPServer):
    """HTTP server carrying immutable export configuration and status cache."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        export_dir: str | Path,
        score_db: str | Path | None = None,
        *,
        target_judged: int = 100_000,
        progress_state: str | Path | None = None,
        progress_token: str | None = None,
        progress_source_id: str = "RYU-DESKTOP2",
        progress_stale_after: int = 90,
    ) -> None:
        self.export_dir = Path(export_dir).expanduser().resolve()
        self.score_db = (
            Path(score_db).expanduser().resolve() if score_db is not None else None
        )
        self.target_judged = max(1, int(target_judged))
        self.last_current_judged: int | None = None
        self.progress_state = (
            Path(progress_state).expanduser().resolve()
            if progress_state is not None
            else None
        )
        self.progress_token = progress_token
        self.progress_source_id = progress_source_id
        self.progress_stale_after = max(1, int(progress_stale_after))
        self.progress_lock = threading.Lock()
        super().__init__(server_address, TrainingRequestHandler)

    def receive_progress(self, payload: Any) -> tuple[HTTPStatus, dict[str, Any]]:
        if not isinstance(payload, dict):
            return HTTPStatus.BAD_REQUEST, {"error": "JSON object required"}
        if set(payload) != {"source_id", "current_judged", "observed_at"}:
            return HTTPStatus.BAD_REQUEST, {"error": "invalid progress payload"}
        source_id = payload["source_id"]
        current = payload["current_judged"]
        observed_at = payload["observed_at"]
        now = int(time.time())
        if (
            not isinstance(source_id, str)
            or not source_id
            or len(source_id) > 64
            or not all(character.isalnum() or character in "-_" for character in source_id)
            or source_id != self.progress_source_id
            or type(current) is not int
            or type(observed_at) is not int
            or current < 0
            or observed_at < 0
            or observed_at > now + 300
        ):
            return HTTPStatus.BAD_REQUEST, {"error": "invalid progress payload"}
        if self.progress_state is None:
            return HTTPStatus.SERVICE_UNAVAILABLE, {"error": "progress receiver disabled"}

        with self.progress_lock:
            previous: dict[str, Any] | None = None
            try:
                loaded = _read_json(self.progress_state)
                if isinstance(loaded, dict):
                    previous = loaded
            except FileNotFoundError:
                pass
            except (OSError, ValueError, json.JSONDecodeError):
                return HTTPStatus.SERVICE_UNAVAILABLE, {"error": "progress state unavailable"}
            if previous is not None:
                if (
                    previous.get("source_id") != source_id
                    or type(previous.get("observed_at")) is not int
                    or type(previous.get("current_judged")) is not int
                    or type(previous.get("received_at")) is not int
                ):
                    return HTTPStatus.SERVICE_UNAVAILABLE, {
                        "error": "progress state unavailable"
                    }
                old_time = previous["observed_at"]
                old_current = previous["current_judged"]
                if observed_at < old_time or current < old_current:
                    return HTTPStatus.CONFLICT, {"error": "progress moved backwards"}
                if observed_at == old_time and current != old_current:
                    return HTTPStatus.CONFLICT, {"error": "conflicting progress snapshot"}
                if observed_at == old_time and current == old_current:
                    return HTTPStatus.OK, {"status": "accepted", **previous}

            state = {
                "source_id": source_id,
                "current_judged": current,
                "observed_at": observed_at,
                "received_at": now,
            }
            self.progress_state.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.progress_state.with_name(
                f".{self.progress_state.name}.{os.getpid()}.tmp"
            )
            try:
                temporary.write_text(
                    json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8",
                )
                os.replace(temporary, self.progress_state)
            finally:
                temporary.unlink(missing_ok=True)
        return HTTPStatus.OK, {"status": "accepted", **state}

    def status(self) -> dict[str, Any]:
        stale = False
        errors: list[str] = []
        baseline = 0
        try:
            manifest = _read_json(self.export_dir / "manifest.json")
            baseline = max(0, int(manifest["baseline_judged"]))
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            stale = True
            errors.append("manifestを読み取れません")

        current: int | None = None
        source = "baseline"
        remote_observed_at: int | None = None
        remote_received_at: int | None = None
        if self.progress_state is not None:
            try:
                progress = _read_json(self.progress_state)
                current = max(0, int(progress["current_judged"]))
                remote_observed_at = int(progress["observed_at"])
                source = str(progress["source_id"])
                remote_received_at = int(
                    progress.get("received_at", remote_observed_at)
                )
                freshness_at = min(remote_observed_at, remote_received_at)
                if int(time.time()) - freshness_at > self.progress_stale_after:
                    stale = True
                    errors.append(f"{source}からの進捗が停止しています")
            except FileNotFoundError:
                pass
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                stale = True
                errors.append("受信進捗を読み取れません")
        if (
            current is None
            and self.score_db is None
            and self.progress_state is not None
        ):
            stale = True
            errors.append(f"{self.progress_source_id}からの進捗をまだ受信していません")
        elif current is None and self.score_db is None:
            stale = True
            errors.append("score.dbが設定されていません")
        elif current is None:
            try:
                current = _read_latest_judged(self.score_db)
                self.last_current_judged = current
                source = "local-score-db"
            except (OSError, ValueError, RuntimeError, sqlite3.Error):
                stale = True
                errors.append("score.dbを読み取れません")
                current = self.last_current_judged

        if current is None:
            current = baseline
        live = max(0, current - baseline)
        target = self.target_judged
        return {
            "target_judged": target,
            "baseline_judged": baseline,
            "current_judged": current,
            "progress_source": source,
            "progress_observed_at": remote_observed_at,
            "progress_received_at": remote_received_at,
            "live_judged": live,
            "remaining_judged": max(0, target - live),
            "progress": min(1.0, live / target),
            "complete": live >= target,
            "stale": stale,
            "message": " / ".join(errors) if errors else None,
            "checked_at": _utc_now(),
        }


class TrainingRequestHandler(BaseHTTPRequestHandler):
    """Serve only explicitly allowed exports; arbitrary files are unreachable."""

    server: TrainingHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        # Keep the local command quiet; callers can subclass when access logs matter.
        return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch(send_body=True)

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch(send_body=False)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            path = unquote(urlsplit(self.path).path, errors="strict")
        except (UnicodeDecodeError, ValueError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid path"}, True)
            return
        if path != "/api/progress":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"}, True)
            return
        expected = self.server.progress_token
        supplied = self.headers.get("Authorization", "")
        if not expected or not hmac.compare_digest(supplied, f"Bearer {expected}"):
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"}, True)
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = -1
        if length < 0 or length > _MAX_PROGRESS_BODY:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "invalid body size"}, True)
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"}, True)
            return
        status, response = self.server.receive_progress(payload)
        self._send_json(status, response, True)

    def _dispatch(self, *, send_body: bool) -> None:
        try:
            path = unquote(urlsplit(self.path).path, errors="strict")
        except (UnicodeDecodeError, ValueError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid path"}, send_body)
            return

        if path == "/":
            self._send_bytes(
                HTTPStatus.OK,
                COCKPIT_HTML.encode("utf-8"),
                "text/html; charset=utf-8",
                send_body,
                cache="no-store",
            )
            return
        if path == "/healthz":
            self._send_json(
                HTTPStatus.OK, {"status": "ok", "time": _utc_now()}, send_body
            )
            return
        if path == "/api/status":
            self._send_json(HTTPStatus.OK, self.server.status(), send_body)
            return
        relative = _EXPORT_ROUTES.get(path)
        if relative is not None:
            self._serve_export(relative, send_body)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"}, send_body)

    def _serve_export(self, relative: tuple[str, ...], send_body: bool) -> None:
        # ``relative`` comes only from the constant allow-list above.  The resolve
        # check remains defence in depth if routes are extended later.
        root = self.server.export_dir
        target = root.joinpath(*relative).resolve()
        if target != root and root not in target.parents:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"}, send_body)
            return
        try:
            payload = target.read_bytes()
        except FileNotFoundError:
            self._send_json(
                HTTPStatus.NOT_FOUND, {"error": "export not found"}, send_body
            )
            return
        except OSError:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "export unavailable"},
                send_body,
            )
            return
        self._send_bytes(
            HTTPStatus.OK,
            payload,
            "application/json; charset=utf-8",
            send_body,
            cache="no-cache",
        )

    def _send_json(self, status: HTTPStatus, value: Any, send_body: bool) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self._send_bytes(
            status,
            payload,
            "application/json; charset=utf-8",
            send_body,
            cache="no-store",
        )

    def _send_bytes(
        self,
        status: HTTPStatus,
        payload: bytes,
        content_type: str,
        send_body: bool,
        *,
        cache: str,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        if send_body:
            self.wfile.write(payload)


def make_server(
    export_dir: str | Path,
    score_db: str | Path | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    target_judged: int = 100_000,
    progress_state: str | Path | None = None,
    progress_token: str | None = None,
    progress_source_id: str = "RYU-DESKTOP2",
    progress_stale_after: int = 90,
) -> TrainingHTTPServer:
    """Create, but do not start, a local training HTTP server."""

    if (progress_state is None) != (progress_token is None):
        raise ValueError("progress_state and progress_token must be configured together")
    _validate_progress_bind(host, progress_token)

    return TrainingHTTPServer(
        (host, int(port)), export_dir, score_db, target_judged=target_judged,
        progress_state=progress_state, progress_token=progress_token,
        progress_source_id=progress_source_id,
        progress_stale_after=progress_stale_after,
    )


def serve(
    export_dir: str | Path,
    score_db: str | Path | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    target_judged: int = 100_000,
    progress_state: str | Path | None = None,
    progress_token: str | None = None,
    progress_source_id: str = "RYU-DESKTOP2",
    progress_stale_after: int = 90,
) -> None:
    """Serve the cockpit until interrupted."""

    server = make_server(
        export_dir, score_db, host=host, port=port, target_judged=target_judged,
        progress_state=progress_state, progress_token=progress_token,
        progress_source_id=progress_source_id,
        progress_stale_after=progress_stale_after,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


COCKPIT_HTML = r'''<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>10万打鍵 Training Cockpit</title>
<style>
:root{--bg:#0B1020;--panel:#151D32;--text:#E7EDF7;--muted:#91A0B8;--cyan:#6EE7F2;--gold:#F2C14E;--red:#FF6B6B;--line:#2A3653}
*{box-sizing:border-box}html{background:var(--bg);color:var(--text);font-family:"Yu Gothic UI","Yu Gothic",sans-serif}body{margin:0;min-height:100vh;background:linear-gradient(90deg,transparent 49.9%,rgba(110,231,242,.025) 50%,transparent 50.1%);padding:24px}
.shell{max-width:1180px;margin:auto}.top{display:flex;align-items:end;justify-content:space-between;border-bottom:1px solid var(--line);padding:0 0 15px}.brand{display:flex;gap:14px;align-items:center}.mark{display:grid;grid-template-columns:repeat(7,5px);gap:3px;height:32px;align-items:end}.mark i{display:block;background:var(--cyan);height:var(--h);box-shadow:0 0 12px rgba(110,231,242,.28)}h1{font:700 20px Bahnschrift,"Arial Narrow",sans-serif;letter-spacing:.12em;margin:0}.eyebrow,.label{font:600 11px Bahnschrift,"Arial Narrow",sans-serif;letter-spacing:.16em;text-transform:uppercase;color:var(--muted)}.sync{display:flex;gap:9px;align-items:center;font-size:12px;color:var(--muted)}.dot{width:7px;height:7px;border-radius:50%;background:var(--cyan);box-shadow:0 0 10px var(--cyan)}.dot.stale{background:var(--gold);box-shadow:none}
.grid{display:grid;grid-template-columns:minmax(0,1.65fr) minmax(280px,.8fr);gap:18px;margin-top:18px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:5px}.hero{padding:28px;min-height:337px;position:relative;overflow:hidden}.hero:after{content:"7K";position:absolute;right:-10px;bottom:-38px;font:800 150px Bahnschrift,sans-serif;color:rgba(231,237,247,.025);pointer-events:none}.hero-head{display:flex;justify-content:space-between;gap:20px}.slot{color:var(--cyan)}.tag{border:1px solid rgba(110,231,242,.45);padding:5px 9px;border-radius:3px;color:var(--cyan);font:600 11px Bahnschrift,sans-serif;letter-spacing:.08em}h2{font:700 clamp(25px,4vw,44px) Bahnschrift,"Yu Gothic UI",sans-serif;margin:35px 0 7px;line-height:1.1;max-width:760px}.artist{color:var(--muted);margin:0;font-size:15px}.meta{display:flex;gap:24px;margin:34px 0 0;padding:17px 0 0;border-top:1px solid var(--line)}.meta b{display:block;font:700 19px Bahnschrift,sans-serif;margin-top:5px}.keys{display:grid;grid-template-columns:repeat(7,1fr);gap:5px;height:32px;margin-top:25px}.keys i{background:#202B46;border-bottom:3px solid var(--cyan)}.keys i:nth-child(even){background:#0F1526;border-bottom-color:var(--gold);transform:translateY(-8px)}
.side{padding:22px}.stats{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:18px}.stat{border-left:2px solid var(--line);padding-left:12px}.stat strong{display:block;font:700 28px Bahnschrift,sans-serif}.stat span{color:var(--muted);font-size:11px}.remaining strong{color:var(--gold)}.rail-panel{grid-column:1/-1;padding:22px}.rail-head{display:flex;justify-content:space-between;align-items:end}.count{font:700 clamp(30px,5vw,54px) Bahnschrift,sans-serif;line-height:.9}.count small{font-size:14px;color:var(--muted)}.rail{height:58px;display:grid;grid-template-columns:repeat(10,1fr);gap:4px;margin-top:22px}.segment{background:#202A43;position:relative;overflow:hidden;border-bottom:2px solid #354260}.segment:before{content:"";position:absolute;inset:auto 0 0;height:var(--fill,0%);background:var(--cyan);transition:height .45s ease}.segment:nth-child(even):before{background:#54C9D5}.ticks{display:flex;justify-content:space-between;color:var(--muted);font:10px Bahnschrift,sans-serif;margin-top:7px}.queue{grid-column:1/-1;padding:22px}.queue-list{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:16px}.queue-item{border:1px solid var(--line);padding:13px 14px;min-width:0}.queue-item b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:13px;margin-top:4px}.queue-item span{color:var(--cyan);font:600 10px Bahnschrift,sans-serif;letter-spacing:.08em}.empty{color:var(--muted);font-size:13px}.error{color:var(--red)}
:focus-visible{outline:3px solid var(--gold);outline-offset:3px}@media(max-width:720px){body{padding:13px}.top{align-items:center}.sync span:last-child{display:none}.grid{grid-template-columns:1fr}.hero{min-height:315px;padding:20px}.side,.rail-panel,.queue{padding:18px}.queue-list{grid-template-columns:1fr}.rail{height:45px}.meta{gap:14px;flex-wrap:wrap}}
@media(prefers-reduced-motion:reduce){*,*:before{scroll-behavior:auto!important;transition:none!important;animation:none!important}}
</style>
</head>
<body><main class="shell">
<header class="top"><div class="brand"><span class="mark" aria-hidden="true"><i style="--h:40%"></i><i style="--h:70%"></i><i style="--h:52%"></i><i style="--h:100%"></i><i style="--h:62%"></i><i style="--h:82%"></i><i style="--h:47%"></i></span><div><div class="eyebrow">Personal recommend</div><h1>10万打鍵 COCKPIT</h1></div></div><div class="sync"><i class="dot" id="dot"></i><span id="sync">LIVE</span><span id="checked">接続中</span></div></header>
<div class="grid">
<section class="panel hero" aria-labelledby="next-title"><div class="hero-head"><div class="label"><span class="slot">NEXT / </span><span id="slot">WARMUP</span></div><span class="tag" id="band">R1</span></div><h2 id="next-title">メニューを読み込み中</h2><p class="artist" id="artist">—</p><div class="meta"><div><span class="label">NOTES</span><b id="notes">—</b></div><div><span class="label">LEVEL</span><b id="level">—</b></div><div><span class="label">TARGET</span><b id="target">—</b></div></div><div class="keys" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div></section>
<aside class="panel side"><div class="label">TODAY'S RUN</div><div class="stats"><div class="stat"><strong id="played">0</strong><span>打鍵済み</span></div><div class="stat remaining"><strong id="remaining">100,000</strong><span>残り打鍵</span></div><div class="stat"><strong id="rate">0%</strong><span>達成率</span></div><div class="stat"><strong id="queue-count">0</strong><span>残り譜面</span></div></div></aside>
<section class="panel rail-panel"><div class="rail-head"><div><div class="label">100,000 JUDGEMENTS</div><div class="count"><span id="big-count">0</span> <small>/ 100,000</small></div></div><div class="label" id="state">READY</div></div><div class="rail" id="rail" aria-label="10万打鍵の進捗"></div><div class="ticks"><span>START</span><span>50K</span><span>GOAL</span></div></section>
<section class="panel queue"><div class="label">FLIGHT QUEUE</div><div class="queue-list" id="queue"><div class="empty">メニューの生成後、ここに次の譜面が表示されます。</div></div></section>
</div></main>
<script>
const fmt=n=>new Intl.NumberFormat('ja-JP').format(Number(n)||0);
const pick=(o,...ks)=>{for(const k of ks)if(o&&o[k]!=null)return o[k];return null};
function charts(s){const q=pick(s,'queue','items','charts','menu');return Array.isArray(q)?q:[]}
function showSession(s){const q=charts(s),next=pick(s,'next')||q[0]||{};document.querySelector('#next-title').textContent=pick(next,'title','name')||'次の譜面はありません';document.querySelector('#artist').textContent=pick(next,'artist','subtitle')||'—';document.querySelector('#notes').textContent=fmt(pick(next,'notes','judged')||0);document.querySelector('#level').textContent=pick(next,'level','difficulty')||'—';document.querySelector('#target').textContent=pick(next,'target','lamp','goal')||'—';document.querySelector('#slot').textContent=pick(next,'slot','category','phase')||'WARMUP';document.querySelector('#band').textContent=pick(next,'band','recommend_band')||'R1';document.querySelector('#queue-count').textContent=q.length;const box=document.querySelector('#queue');box.innerHTML=q.slice(1,7).map((x,i)=>`<div class="queue-item"><span>${String(i+2).padStart(2,'0')} / ${esc(pick(x,'slot','category','phase')||'NEXT')}</span><b>${esc(pick(x,'title','name')||'名称未設定')}</b></div>`).join('')||'<div class="empty">後続の譜面はありません。</div>'}
function esc(v){const d=document.createElement('div');d.textContent=String(v);return d.innerHTML}
function showStatus(s){const live=Number(s.live_judged)||0,target=Number(s.target_judged)||100000,p=Math.min(1,live/target),received=Number(s.progress_received_at)||0,stamp=received?'最終受信 '+new Date(received*1000).toLocaleTimeString('ja-JP',{hour:'2-digit',minute:'2-digit',second:'2-digit'}):'更新 '+new Date(s.checked_at).toLocaleTimeString('ja-JP',{hour:'2-digit',minute:'2-digit'});document.querySelector('#played').textContent=fmt(live);document.querySelector('#remaining').textContent=fmt(s.remaining_judged);document.querySelector('#rate').textContent=Math.floor(p*100)+'%';document.querySelector('#big-count').textContent=fmt(live);document.querySelector('#state').textContent=s.complete?'GOAL':'IN PROGRESS';document.querySelector('#dot').classList.toggle('stale',!!s.stale);document.querySelector('#sync').textContent=s.stale?'STALE':'LIVE';document.querySelector('#checked').textContent=s.stale?((s.message||'前回値を表示中')+' / '+stamp):stamp;[...document.querySelectorAll('.segment')].forEach((el,i)=>el.style.setProperty('--fill',Math.max(0,Math.min(1,p*10-i))*100+'%'))}
const rail=document.querySelector('#rail');for(let i=0;i<10;i++){const e=document.createElement('i');e.className='segment';rail.appendChild(e)}
async function load(){try{const [a,b]=await Promise.all([fetch('/api/session',{cache:'no-store'}),fetch('/api/status',{cache:'no-store'})]);if(a.ok)showSession(await a.json());else document.querySelector('#next-title').textContent='今日のメニューを生成してください';if(b.ok)showStatus(await b.json())}catch(e){document.querySelector('#sync').textContent='OFFLINE';document.querySelector('#dot').classList.add('stale');document.querySelector('#checked').textContent='サーバーへ接続できません'}}load();setInterval(async()=>{try{const r=await fetch('/api/status',{cache:'no-store'});if(r.ok)showStatus(await r.json())}catch(e){}},5000);
</script></body></html>'''
