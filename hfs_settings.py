from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = BASE_DIR / "hfs-settings.json"


def parse_port(value: object) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("端口必须是 1 到 65535 之间的整数") from error
    if not 1 <= port <= 65535:
        raise ValueError("端口必须是 1 到 65535 之间的整数")
    return port


def parse_download_limit_kbps(value: object) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("限速必须是大于或等于 0 的整数 KB/s") from error
    if limit < 0:
        raise ValueError("限速必须是大于或等于 0 的整数 KB/s")
    return limit


class SettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def read(self) -> dict[str, int]:
        try:
            settings = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(settings, dict):
            return {}
        return {name: value for name, value in settings.items() if isinstance(value, int)}

    def save(self, name: str, value: int) -> None:
        with self._lock:
            settings = self.read()
            settings[name] = value
            self.path.write_text(
                json.dumps(settings, ensure_ascii=False) + "\n", encoding="utf-8"
            )


SETTINGS = SettingsStore(SETTINGS_PATH)


def configured_port() -> int:
    environment_port = os.environ.get("HFS_PORT")
    if environment_port is not None:
        return parse_port(environment_port)
    try:
        return parse_port(SETTINGS.read().get("port"))
    except ValueError:
        return 8765


def save_port(port: int) -> None:
    SETTINGS.save("port", port)


def configured_download_limit_kbps() -> int:
    environment_limit = os.environ.get("HFS_DOWNLOAD_LIMIT_KBPS")
    if environment_limit is not None:
        return parse_download_limit_kbps(environment_limit)
    try:
        return parse_download_limit_kbps(SETTINGS.read().get("download_limit_kbps", 0))
    except ValueError:
        return 0


class DownloadLimiter:
    def __init__(self, limit_kbps: int = 0) -> None:
        self._limit_bytes_per_second = limit_kbps * 1024
        self._lock = threading.Lock()

    def set_limit_kbps(self, limit_kbps: int) -> None:
        with self._lock:
            self._limit_bytes_per_second = limit_kbps * 1024

    def chunk_size(self) -> int:
        with self._lock:
            limit = self._limit_bytes_per_second
        if limit <= 0:
            return 1024 * 1024
        return min(64 * 1024, max(1024, limit // 10))

    def throttle(self, bytes_sent: int, started_at: float) -> None:
        with self._lock:
            limit = self._limit_bytes_per_second
        if limit <= 0:
            return
        delay = bytes_sent / limit - (time.monotonic() - started_at)
        if delay > 0:
            time.sleep(delay)


DOWNLOAD_LIMITER = DownloadLimiter(configured_download_limit_kbps())


def set_download_limit_kbps(limit_kbps: int) -> int:
    limit_kbps = parse_download_limit_kbps(limit_kbps)
    DOWNLOAD_LIMITER.set_limit_kbps(limit_kbps)
    SETTINGS.save("download_limit_kbps", limit_kbps)
    return limit_kbps
