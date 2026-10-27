from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from threading import Lock
from time import monotonic
from typing import Literal
from uuid import uuid4


EventLevel = Literal["request", "completed", "cancelled", "failed", "info"]
DownloadOutcome = Literal["completed", "cancelled", "failed"]


def format_rate(bytes_per_second: int) -> str:
    amount = max(0, bytes_per_second)
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if amount < 1024 or unit == "GB/s":
            return f"{amount} {unit}" if unit == "B/s" else f"{amount:.1f} {unit}"
        amount /= 1024
    return "0 B/s"


@dataclass(frozen=True)
class RuntimeEvent:
    time: str
    level: EventLevel
    message: str


@dataclass
class ActiveDownload:
    id: str
    address: str
    filename: str
    size: int
    sent: int
    speed: int
    started_at: float
    updated_at: float

    def public_data(self, now: float) -> dict[str, int | str]:
        speed = self.speed if now - self.updated_at <= 2 else 0
        return {
            "id": self.id,
            "address": self.address,
            "filename": self.filename,
            "size": self.size,
            "sent": self.sent,
            "speed": speed,
        }


@dataclass(frozen=True)
class RuntimeSnapshot:
    active_downloads: list[dict[str, int | str]]
    active_count: int
    current_speed: int
    total_bytes_sent: int
    events: list[RuntimeEvent]

    def public_data(self) -> dict[str, object]:
        return {
            "active_downloads": self.active_downloads,
            "active_count": self.active_count,
            "current_speed": self.current_speed,
            "total_bytes_sent": self.total_bytes_sent,
            "events": [asdict(event) for event in self.events],
        }


class RuntimeState:
    def __init__(self, event_limit: int = 200) -> None:
        self._lock = Lock()
        self._downloads: dict[str, ActiveDownload] = {}
        self._events: deque[RuntimeEvent] = deque(maxlen=event_limit)
        self._total_bytes_sent = 0

    def record_event(self, level: EventLevel, message: str) -> None:
        with self._lock:
            self._events.append(self._event(level, message))

    def start_download(self, address: str, filename: str, size: int, path: str) -> str:
        download_id = uuid4().hex
        now = monotonic()
        download = ActiveDownload(
            id=download_id,
            address=address,
            filename=filename,
            size=size,
            sent=0,
            speed=0,
            started_at=now,
            updated_at=now,
        )
        with self._lock:
            self._downloads[download_id] = download
            self._events.append(self._event("request", f"{address} 请求下载 {path}"))
        return download_id

    def advance_download(self, download_id: str, sent: int) -> None:
        now = monotonic()
        with self._lock:
            download = self._downloads.get(download_id)
            if download is None:
                return
            transferred = sent - download.sent
            elapsed = max(now - download.updated_at, 0.001)
            download.sent = sent
            download.speed = max(0, int(transferred / elapsed))
            download.updated_at = now
            self._total_bytes_sent += max(0, transferred)

    def finish_download(self, download_id: str, outcome: DownloadOutcome) -> None:
        labels: dict[DownloadOutcome, str] = {
            "completed": "下载完成",
            "cancelled": "下载已取消",
            "failed": "下载失败",
        }
        with self._lock:
            download = self._downloads.pop(download_id, None)
            if download is None:
                return
            elapsed = max(monotonic() - download.started_at, 0.001)
            average_speed = int(download.sent / elapsed)
            self._events.append(
                self._event(
                    outcome,
                    (
                        f"{download.address} {labels[outcome]} {download.filename} "
                        f"({download.sent} / {download.size} 字节，平均速度 {format_rate(average_speed)})"
                    ),
                )
            )

    def snapshot(self) -> RuntimeSnapshot:
        now = monotonic()
        with self._lock:
            downloads = [download.public_data(now) for download in self._downloads.values()]
            return RuntimeSnapshot(
                active_downloads=downloads,
                active_count=len(downloads),
                current_speed=sum(int(download["speed"]) for download in downloads),
                total_bytes_sent=self._total_bytes_sent,
                events=list(reversed(self._events)),
            )

    @staticmethod
    def _event(level: EventLevel, message: str) -> RuntimeEvent:
        return RuntimeEvent(
            time=datetime.now().strftime("%H:%M:%S"),
            level=level,
            message=message,
        )


RUNTIME_STATE = RuntimeState()
