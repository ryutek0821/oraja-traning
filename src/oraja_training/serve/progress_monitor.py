"""Small Tkinter monitor for the Windows-to-Mac progress sender."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import threading
import time
from typing import Callable
from urllib.parse import urlsplit

from .progress_sender import (
    ProgressCounts,
    read_progress_counts,
    send_progress_snapshot,
)


_SOURCE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_DEFAULT_SERVER_STALE_AFTER = 90.0


@dataclass(frozen=True, slots=True)
class MonitorSettings:
    """Non-secret sender configuration persisted beside the Windows app data."""

    score_db: str = ""
    server_url: str = ""
    token_file: str = ""
    source_id: str = "RYU-DESKTOP2"
    poll_interval: float = 5.0
    heartbeat: float = 30.0
    send_enabled: bool = True

    def normalized(self) -> MonitorSettings:
        if not isinstance(self.send_enabled, bool):
            raise ValueError("送信ON/OFFはtrueまたはfalseで指定してください")
        score_db = Path(self.score_db).expanduser().resolve(strict=True)
        token_file = Path(self.token_file).expanduser().resolve(strict=True)
        if not score_db.is_file():
            raise ValueError(f"score.dbが見つかりません: {score_db}")
        if not token_file.is_file():
            raise ValueError(f"tokenファイルが見つかりません: {token_file}")

        url = self.server_url.strip().rstrip("/")
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("送信先は http(s)://host:port 形式で指定してください")
        if not _SOURCE_ID.fullmatch(self.source_id):
            raise ValueError("送信元IDは英数字・ハイフン・下線の1〜64文字です")
        poll_interval = float(self.poll_interval)
        heartbeat = float(self.heartbeat)
        if poll_interval < 0.5:
            raise ValueError("確認間隔は0.5秒以上にしてください")
        if heartbeat < 1.0:
            raise ValueError("heartbeatは1秒以上にしてください")

        token = token_file.read_text(encoding="utf-8").strip()
        if not token or len(token) > 4096:
            raise ValueError("tokenファイルが空または大きすぎます")
        return replace(
            self,
            score_db=str(score_db),
            server_url=url,
            token_file=str(token_file),
            source_id=self.source_id,
            poll_interval=poll_interval,
            heartbeat=heartbeat,
        )

    def for_storage(self) -> MonitorSettings:
        """Normalize saved values without requiring send-ready paths while OFF."""

        if self.send_enabled:
            return self.normalized()
        if not isinstance(self.send_enabled, bool):
            raise ValueError("送信ON/OFFはtrueまたはfalseで指定してください")

        score_db = self.score_db.strip()
        token_file = self.token_file.strip()
        return replace(
            self,
            score_db=(
                str(Path(score_db).expanduser().resolve()) if score_db else ""
            ),
            server_url=self.server_url.strip().rstrip("/"),
            token_file=(
                str(Path(token_file).expanduser().resolve()) if token_file else ""
            ),
            source_id=self.source_id.strip(),
            poll_interval=float(self.poll_interval),
            heartbeat=float(self.heartbeat),
        )

    def read_token(self) -> str:
        token = Path(self.token_file).read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError("progress token file is empty")
        return token


@dataclass(frozen=True, slots=True)
class MonitorSnapshot:
    status: str
    current_judged: int | None
    today_judged: int | None
    last_sent_at: float | None
    next_send_at: float | None
    server_url: str
    error: str | None = None


def default_monitor_config_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / ".local" / "share"
    return base / "oraja-training" / "progress-monitor.json"


def load_monitor_settings(path: str | Path) -> MonitorSettings:
    target = Path(path).expanduser()
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return MonitorSettings()
    if not isinstance(value, dict):
        raise ValueError("進捗モニター設定はJSON objectである必要があります")
    allowed = {field for field in MonitorSettings.__dataclass_fields__}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"進捗モニター設定に不明な項目があります: {sorted(unknown)}")
    if "send_enabled" in value and not isinstance(value["send_enabled"], bool):
        raise ValueError("送信ON/OFFはtrueまたはfalseで指定してください")
    try:
        return MonitorSettings(**value)
    except TypeError as error:
        raise ValueError("進捗モニター設定を読み取れません") from error


def save_monitor_settings(path: str | Path, settings: MonitorSettings) -> Path:
    """Atomically save settings; token material is never persisted."""

    normalized = settings.for_storage()
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(asdict(normalized), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


class ProgressMonitorWorker:
    """Testable background loop; Tk widgets stay on the GUI thread."""

    def __init__(
        self,
        settings: MonitorSettings,
        on_snapshot: Callable[[MonitorSnapshot], None],
        *,
        reader: Callable[[str | Path], ProgressCounts] = read_progress_counts,
        sender: Callable[..., dict[str, object]] = send_progress_snapshot,
        wall_clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings.normalized()
        self._token = self.settings.read_token()
        self._on_snapshot = on_snapshot
        self._reader = reader
        self._sender = sender
        self._wall_clock = wall_clock
        self._monotonic = monotonic
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._send_now = threading.Event()
        self._last_sent_monotonic: float | None = None
        self._last_sent_at: float | None = None
        self._last_sent_count: int | None = None
        self._last_counts: ProgressCounts | None = None

    def poll_once(self, *, force: bool = False) -> MonitorSnapshot:
        now = self._wall_clock()
        monotonic_now = self._monotonic()
        try:
            counts = self._reader(self.settings.score_db)
            self._last_counts = counts
            due = (
                force
                or self._last_sent_monotonic is None
                or counts.current_judged != self._last_sent_count
                or monotonic_now - self._last_sent_monotonic
                >= self.settings.heartbeat
            )
            if due:
                self._sender(
                    self.settings.server_url,
                    self._token,
                    counts.current_judged,
                    source_id=self.settings.source_id,
                    observed_at=int(now),
                )
                self._last_sent_monotonic = monotonic_now
                self._last_sent_at = now
                self._last_sent_count = counts.current_judged
            remaining = (
                self.settings.heartbeat
                if self._last_sent_monotonic is None
                else max(
                    0.0,
                    self.settings.heartbeat
                    - (monotonic_now - self._last_sent_monotonic),
                )
            )
            sent_age = (
                float("inf")
                if self._last_sent_monotonic is None
                else monotonic_now - self._last_sent_monotonic
            )
            return MonitorSnapshot(
                status=(
                    "LIVE"
                    if sent_age <= _DEFAULT_SERVER_STALE_AFTER
                    else "STALE"
                ),
                current_judged=counts.current_judged,
                today_judged=counts.today_judged,
                last_sent_at=self._last_sent_at,
                next_send_at=now + remaining,
                server_url=self.settings.server_url,
            )
        except Exception as error:  # keep the monitor alive across DB/network failures
            return MonitorSnapshot(
                status="ERROR",
                current_judged=(
                    None if self._last_counts is None
                    else self._last_counts.current_judged
                ),
                today_judged=(
                    None if self._last_counts is None
                    else self._last_counts.today_judged
                ),
                last_sent_at=self._last_sent_at,
                next_send_at=now + self.settings.poll_interval,
                server_url=self.settings.server_url,
                error=str(error) or type(error).__name__,
            )

    def run(self) -> None:
        while not self._stop.is_set():
            force = self._send_now.is_set()
            self._send_now.clear()
            self._on_snapshot(self.poll_once(force=force))
            self._wake.wait(self.settings.poll_interval)
            self._wake.clear()

    def send_now(self) -> None:
        self._send_now.set()
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()


class ProgressMonitorController:
    """Own exactly one sender worker and expose safe lifecycle transitions."""

    def __init__(
        self,
        *,
        worker_factory: Callable[..., ProgressMonitorWorker] = ProgressMonitorWorker,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
    ) -> None:
        self._worker_factory = worker_factory
        self._thread_factory = thread_factory
        self._worker: ProgressMonitorWorker | None = None
        self._thread: threading.Thread | None = None

    @property
    def active(self) -> bool:
        return self._worker is not None

    def start(
        self,
        settings: MonitorSettings,
        on_snapshot: Callable[[MonitorSnapshot], None],
    ) -> MonitorSettings:
        normalized = settings.normalized()
        replacement = self._worker_factory(normalized, on_snapshot)
        self.stop()
        replacement_thread = self._thread_factory(
            target=replacement.run,
            daemon=True,
        )
        self._worker = replacement
        self._thread = replacement_thread
        try:
            replacement_thread.start()
        except Exception:
            replacement.stop()
            self._worker = None
            self._thread = None
            raise
        return normalized

    def send_now(self) -> bool:
        if self._worker is None:
            return False
        self._worker.send_now()
        return True

    def stop(self) -> None:
        if self._worker is not None:
            self._worker.stop()
        if self._thread is not None:
            self._thread.join(timeout=6.0)
            if self._thread.is_alive():
                raise RuntimeError("旧送信処理の停止を確認できません")
        self._worker = None
        self._thread = None


def run_progress_monitor(
    *,
    config_path: str | Path | None = None,
    score_db: str | Path | None = None,
    server_url: str | None = None,
    token_file: str | Path | None = None,
    source_id: str | None = None,
    poll_interval: float | None = None,
    heartbeat: float | None = None,
) -> int:
    """Open the standard-library monitor and optionally seed its saved settings."""

    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    config = Path(config_path) if config_path else default_monitor_config_path()
    try:
        settings = load_monitor_settings(config)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        settings = MonitorSettings()
        startup_error = str(error)
    else:
        startup_error = None
    overlays = {
        "score_db": str(score_db) if score_db is not None else None,
        "server_url": server_url,
        "token_file": str(token_file) if token_file is not None else None,
        "source_id": source_id,
        "poll_interval": poll_interval,
        "heartbeat": heartbeat,
    }
    settings = replace(
        settings,
        **{key: value for key, value in overlays.items() if value is not None},
    )
    auto_start = bool(
        settings.send_enabled
        and settings.score_db
        and settings.server_url
        and settings.token_file
    )

    root = tk.Tk()
    root.title("Oraja Training 進捗モニター")
    root.geometry("620x520")
    root.minsize(560, 490)

    style = ttk.Style(root)
    style.configure("Count.TLabel", font=("Segoe UI", 18, "bold"))
    style.configure("Status.TLabel", font=("Segoe UI", 16, "bold"))
    style.configure("SendState.TLabel", font=("Segoe UI", 11, "bold"))

    score_var = tk.StringVar(value=settings.score_db)
    url_var = tk.StringVar(value=settings.server_url)
    token_var = tk.StringVar(value=settings.token_file)
    source_var = tk.StringVar(value=settings.source_id)
    status_var = tk.StringVar(value="ERROR" if startup_error else "STALE")
    current_var = tk.StringVar(value="—")
    today_var = tk.StringVar(value="—")
    last_var = tk.StringVar(value="未送信")
    next_var = tk.StringVar(value="—")
    error_var = tk.StringVar(value=startup_error or "設定を確認しています")
    destination_var = tk.StringVar(value=settings.server_url or "未設定")
    send_enabled_var = tk.BooleanVar(value=auto_start)
    send_state_var = tk.StringVar(value="ON" if auto_start else "OFF")

    outer = ttk.Frame(root, padding=16)
    outer.pack(fill="both", expand=True)
    header = ttk.Frame(outer)
    header.pack(fill="x")
    status_label = ttk.Label(header, textvariable=status_var, style="Status.TLabel")
    status_label.configure(foreground="#C62828" if startup_error else "#A56A00")
    status_label.pack(side="left")

    sender_summary = ttk.Frame(outer, padding=(0, 8, 0, 0))
    sender_summary.pack(fill="x")
    ttk.Label(sender_summary, text="送信状態").grid(row=0, column=0, sticky="w")
    send_state_label = ttk.Label(
        sender_summary,
        textvariable=send_state_var,
        style="SendState.TLabel",
    )
    send_state_label.grid(row=0, column=1, sticky="w", padx=(8, 24))
    ttk.Label(sender_summary, text="送信先 URL").grid(row=0, column=2, sticky="w")
    ttk.Label(sender_summary, textvariable=destination_var).grid(
        row=0,
        column=3,
        sticky="w",
        padx=(8, 0),
    )
    sender_summary.columnconfigure(3, weight=1)

    counts = ttk.Frame(outer, padding=(0, 14))
    counts.pack(fill="x")
    for column in range(2):
        counts.columnconfigure(column, weight=1)
    ttk.Label(counts, text="累積打鍵数").grid(row=0, column=0, sticky="w")
    ttk.Label(counts, text="本日の打鍵数").grid(row=0, column=1, sticky="w")
    ttk.Label(counts, textvariable=current_var, style="Count.TLabel").grid(
        row=1, column=0, sticky="w"
    )
    ttk.Label(counts, textvariable=today_var, style="Count.TLabel").grid(
        row=1, column=1, sticky="w"
    )
    ttk.Label(counts, text="最終送信").grid(row=2, column=0, sticky="w", pady=(12, 0))
    ttk.Label(counts, text="次回heartbeat").grid(
        row=2, column=1, sticky="w", pady=(12, 0)
    )
    ttk.Label(counts, textvariable=last_var).grid(row=3, column=0, sticky="w")
    ttk.Label(counts, textvariable=next_var).grid(row=3, column=1, sticky="w")

    config_frame = ttk.LabelFrame(outer, text="送信設定", padding=10)
    config_frame.pack(fill="x")
    config_frame.columnconfigure(1, weight=1)

    def choose_score_db() -> None:
        selected = filedialog.askopenfilename(
            title="beatorajaのscore.dbを選択",
            initialfile="score.db",
            filetypes=(("beatoraja score.db", "score.db"), ("SQLite DB", "*.db")),
        )
        if selected:
            score_var.set(selected)

    def choose_token_file() -> None:
        selected = filedialog.askopenfilename(title="progress tokenファイルを選択")
        if selected:
            token_var.set(selected)

    ttk.Label(config_frame, text="score.db").grid(row=0, column=0, sticky="w")
    ttk.Entry(config_frame, textvariable=score_var).grid(
        row=0, column=1, sticky="ew", padx=8
    )
    ttk.Button(config_frame, text="参照", command=choose_score_db).grid(row=0, column=2)
    ttk.Label(config_frame, text="送信先 URL").grid(
        row=1, column=0, sticky="w", pady=6
    )
    ttk.Entry(config_frame, textvariable=url_var).grid(
        row=1, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=6
    )
    ttk.Label(config_frame, text="token file").grid(row=2, column=0, sticky="w")
    ttk.Entry(config_frame, textvariable=token_var).grid(
        row=2, column=1, sticky="ew", padx=8
    )
    ttk.Button(config_frame, text="参照", command=choose_token_file).grid(row=2, column=2)
    ttk.Label(config_frame, text="送信元ID").grid(row=3, column=0, sticky="w", pady=(6, 0))
    ttk.Entry(config_frame, textvariable=source_var).grid(
        row=3, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=(6, 0)
    )

    ttk.Label(outer, textvariable=error_var, wraplength=570).pack(
        fill="x", pady=(10, 4)
    )
    buttons = ttk.Frame(outer)
    buttons.pack(fill="x", side="bottom")

    snapshots: Queue[tuple[int, MonitorSnapshot]] = Queue()
    controller = ProgressMonitorController()
    worker_generation = 0
    latest: MonitorSnapshot | None = None

    def settings_from_form(*, send_enabled: bool) -> MonitorSettings:
        return MonitorSettings(
            score_db=score_var.get().strip(),
            server_url=url_var.get().strip(),
            token_file=token_var.get().strip(),
            source_id=source_var.get().strip(),
            poll_interval=settings.poll_interval,
            heartbeat=settings.heartbeat,
            send_enabled=send_enabled,
        )

    def update_send_controls(enabled: bool) -> None:
        send_enabled_var.set(enabled)
        send_state_var.set("ON" if enabled else "OFF")
        send_state_label.configure(foreground="#148A43" if enabled else "#6B7280")
        if enabled and controller.active:
            send_now_button.state(["!disabled"])
        else:
            send_now_button.state(["disabled"])

    def show_start_error(error: Exception) -> None:
        status_var.set("ERROR")
        status_label.configure(foreground="#C62828")
        error_var.set(str(error))
        messagebox.showerror("進捗モニター設定", str(error), parent=root)

    def start_worker() -> None:
        nonlocal worker_generation, settings, latest
        # Invalidate queued snapshots before validating replacement settings.
        worker_generation += 1
        generation = worker_generation
        latest = None
        status_var.set("STALE")
        status_label.configure(foreground="#A56A00")
        try:
            candidate = settings_from_form(send_enabled=True).normalized()
            controller.stop()
            save_monitor_settings(config, candidate)
            candidate = controller.start(
                candidate,
                lambda snapshot, generation=generation: snapshots.put((generation, snapshot)),
            )
        except Exception as error:
            try:
                controller.stop()
                disabled = settings_from_form(send_enabled=False)
                save_monitor_settings(config, disabled)
                settings = disabled
            except Exception as stop_error:
                error = RuntimeError(f"{error} / {stop_error}")
            update_send_controls(False)
            destination_var.set(url_var.get().strip().rstrip("/") or "未設定")
            next_var.set("—")
            show_start_error(error)
            return
        settings = candidate
        destination_var.set(candidate.server_url)
        error_var.set("送信を開始しています")
        update_send_controls(True)

    def stop_sending() -> None:
        nonlocal worker_generation, settings, latest
        worker_generation += 1
        latest = None
        disabled = settings_from_form(send_enabled=False)
        try:
            controller.stop()
            save_monitor_settings(config, disabled)
        except Exception as error:
            update_send_controls(controller.active)
            show_start_error(error)
            return
        settings = disabled
        destination_var.set(disabled.server_url.strip().rstrip("/") or "未設定")
        next_var.set("—")
        error_var.set("送信は停止しています")
        update_send_controls(False)

    def toggle_sending() -> None:
        if send_enabled_var.get():
            start_worker()
        else:
            stop_sending()

    def send_now() -> None:
        if not send_enabled_var.get():
            return
        controller.send_now()

    def close() -> None:
        try:
            controller.stop()
        except RuntimeError:
            pass
        root.destroy()

    ttk.Checkbutton(
        buttons,
        text="送信を有効にする",
        variable=send_enabled_var,
        command=toggle_sending,
    ).pack(side="left")
    ttk.Button(buttons, text="保存して開始", command=start_worker).pack(
        side="left", padx=(8, 0)
    )
    send_now_button = ttk.Button(buttons, text="今すぐ送信", command=send_now)
    send_now_button.pack(side="left", padx=8)
    send_now_button.state(["disabled"])
    ttk.Button(buttons, text="終了", command=close).pack(side="right")

    def refresh() -> None:
        nonlocal latest
        try:
            while True:
                generation, snapshot = snapshots.get_nowait()
                if generation == worker_generation:
                    latest = snapshot
        except Empty:
            pass
        if latest is not None:
            status_var.set(latest.status)
            status_label.configure(
                foreground={"LIVE": "#148A43", "STALE": "#A56A00"}.get(
                    latest.status, "#C62828"
                )
            )
            current_var.set(
                "—" if latest.current_judged is None else f"{latest.current_judged:,}"
            )
            today_var.set(
                "—" if latest.today_judged is None else f"{latest.today_judged:,}"
            )
            last_var.set(
                "未送信"
                if latest.last_sent_at is None
                else time.strftime("%H:%M:%S", time.localtime(latest.last_sent_at))
            )
            if latest.next_send_at is None:
                next_var.set("—")
            else:
                next_var.set(f"{max(0, int(latest.next_send_at - time.time()))} 秒")
            error_var.set(latest.error or "正常に送信しています")
        root.after(250, refresh)

    root.protocol("WM_DELETE_WINDOW", close)
    root.after(100, refresh)
    if auto_start:
        root.after(150, start_worker)
    elif startup_error is None:
        error_var.set("送信は停止しています")
    root.mainloop()
    return 0
