from __future__ import annotations

import hmac
import json
import mimetypes
import os
import re
import shutil
import signal
import sqlite3
import socket
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from errno import EADDRINUSE
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
from zipfile import ZIP_DEFLATED, ZipFile

from hfs_runtime import RUNTIME_STATE
from hfs_settings import (
    DOWNLOAD_LIMITER,
    configured_download_limit_kbps,
    configured_port,
    parse_port,
    save_port,
    set_download_limit_kbps,
)


BASE_DIR = Path(__file__).resolve().parent
configured_database_path = Path(os.environ.get("HFS_DATABASE", "hfs.sqlite3")).expanduser()
DATABASE_PATH = (
    configured_database_path
    if configured_database_path.is_absolute()
    else BASE_DIR / configured_database_path
)
WEB_ROOT = BASE_DIR / "web"
REGISTER_TOKEN = os.environ.get("HFS_REGISTER_TOKEN", "").strip()
FILE_ID_RE = re.compile(r"^[a-f0-9]{32}$")
DB_LOCK = threading.Lock()
ZIP_DOWNLOAD_SLOT = threading.BoundedSemaphore()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()




@contextmanager
def db_connection() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DATABASE_PATH)
    try:
        connection.row_factory = sqlite3.Row
        # SQLite's transaction context commits/rolls back, but does not close.
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db_connection() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS batches (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS files (
                id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL REFERENCES batches(id),
                source_path TEXT NOT NULL,
                display_path TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'file',
                size INTEGER NOT NULL,
                modified REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS files_batch_index ON files(batch_id);
            """
        )
        try:
            db.execute("ALTER TABLE files ADD COLUMN kind TEXT NOT NULL DEFAULT 'file'")
        except sqlite3.OperationalError:
            pass


def clean_text(value: object, fallback: str, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] or fallback


def safe_relative_path(value: str) -> Path:
    if "\x00" in value:
        raise ValueError("invalid directory path")
    parts = []
    for part in value.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError("invalid directory path")
        parts.append(part)
    return Path(*parts) if parts else Path()


def inside_directory(root: Path, relative: Path) -> Path:
    root = root.expanduser().resolve()
    target = (root / relative).resolve()
    if target != root and root not in target.parents:
        raise ValueError("directory path escapes shared root")
    return target


def browse_url(root_id: str, relative: Path = Path()) -> str:
    query = urlencode({"path": relative.as_posix()}) if relative.parts else ""
    return f"/browse/{root_id}{('?' + query) if query else ''}"


def public_url(relative: Path) -> str:
    return "/" + "/".join(quote(part, safe="") for part in relative.parts)


def zip_url(relative: Path) -> str:
    return f"/api/download-zip?{urlencode({'path': public_url(relative)})}"


def lan_addresses() -> list[str]:
    addresses = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = str(info[4][0])
            if not address.startswith("127."):
                addresses.add(address)
    except OSError:
        pass
    if addresses:
        return sorted(addresses)
    for target in ("192.0.2.1", "10.255.255.1"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((target, 1))
            address = sock.getsockname()[0]
            if not address.startswith("127."):
                addresses.add(address)
        except OSError:
            continue
        finally:
            sock.close()
    return sorted(addresses)


class ServerController:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.http_server: ThreadingHTTPServer | None = None
        self.http_thread: threading.Thread | None = None
        self.lock = threading.Lock()

    def start(self) -> None:
        self._replace_server(self.port)

    def change_port(self, port: int) -> int:
        port = parse_port(port)
        if port == self.port:
            save_port(port)
            return port
        self._replace_server(port)
        save_port(port)
        return port

    def _replace_server(self, port: int) -> None:
        if port and self.host in {"", "0.0.0.0"}:
            # shortcut: catches existing loopback listeners, use exclusive binding if concurrent binds matter.
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    pass
            except ConnectionRefusedError:
                pass
            else:
                raise OSError(EADDRINUSE, f"本机端口 {port} 已被占用，请更换端口")
        new_server = ThreadingHTTPServer((self.host, port), HfsHandler)
        new_thread = threading.Thread(target=new_server.serve_forever, daemon=True)
        with self.lock:
            old_server = self.http_server
            self.http_server = new_server
            self.http_thread = new_thread
            self.port = port
        if old_server is not None:
            old_server.shutdown()
            old_server.server_close()
        new_thread.start()

    def stop(self) -> None:
        with self.lock:
            http_server = self.http_server
            self.http_server = None
        if http_server is not None:
            http_server.shutdown()
            http_server.server_close()


def read_json_body(handler: BaseHTTPRequestHandler) -> dict:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
        payload = json.loads(handler.rfile.read(length) or b"{}")
    except (ValueError, json.JSONDecodeError):
        raise ValueError("invalid JSON body")
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    return payload


_BYTE_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")


def _parse_byte_range(header: str, size: int) -> tuple[int, int] | None:
    if size <= 0:
        return None
    match = _BYTE_RANGE_RE.fullmatch(header.strip())
    if match is None:
        return None
    start_text, end_text = match.group(1), match.group(2)
    if not start_text and not end_text:
        return None
    if not start_text:
        suffix = int(end_text)
        if suffix <= 0:
            return None
        return max(size - suffix, 0), size - 1
    start = int(start_text)
    end = int(end_text) if end_text else size - 1
    if start >= size or start > end:
        return None
    return start, min(end, size - 1)

class HfsHandler(BaseHTTPRequestHandler):
    server_version = "HFS/0.1"
    # Bound stalled reads/writes, not total playback time. Players can reconnect
    # with Range after a long pause without retaining a worker indefinitely.
    timeout = 30

    def log_message(self, format: str, *args) -> None:
        if (
            urlparse(self.path).path in {"/api/status", "/api/files"}
            and len(args) > 1
            and args[1] == "200"
        ):
            return
        print(f"{self.address_string()} - {format % args}")

    def end_headers(self) -> None:
        # Unity WebGL opened from a local file fetches YooAsset bundles from this server.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Range")
        self.send_header("Access-Control-Expose-Headers", "Accept-Ranges, Content-Length, Content-Range")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status: int, message: str) -> None:
        self.send_json({"error": message}, status)

    def registration_allowed(self) -> bool:
        if REGISTER_TOKEN:
            supplied = self.headers.get("X-Register-Token", "")
            return hmac.compare_digest(supplied, REGISTER_TOKEN)
        return self.client_address[0] in {"127.0.0.1", "::1"}

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/":
                self.serve_static("index.html", "text/html; charset=utf-8")
            elif path.startswith("/static/"):
                name = path.removeprefix("/static/")
                content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
                self.serve_static(name, f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type)
            elif path == "/api/health":
                self.send_json({"ok": True})
            elif path == "/api/status":
                if not self.registration_allowed():
                    self.send_error_json(
                        HTTPStatus.FORBIDDEN,
                        "runtime status is limited to the server computer",
                    )
                    return
                self.send_json(RUNTIME_STATE.snapshot().public_data())
            elif path == "/api/files":
                self.list_files()
            elif path == "/api/public-directory":
                self.list_public_directory(parse_qs(parsed.query).get("path", [""])[0])
            elif path == "/api/download-zip":
                query = parse_qs(parsed.query)
                self.download_zip(query.get("path", [""])[0], query.get("ticket", [""])[0])
            elif path.startswith("/api/directory/"):
                self.list_directory(path.removeprefix("/api/directory/"), parsed.query)
            elif path.startswith("/browse/"):
                self.serve_static("index.html", "text/html; charset=utf-8")
            elif path.startswith("/download-directory/"):
                self.download_directory_file(path.removeprefix("/download-directory/"), parsed.query)
            elif path.startswith("/download/"):
                self.download_file(path.removeprefix("/download/"))
            else:
                self.serve_public_path(path)
        except FileNotFoundError:
            self.send_error_json(HTTPStatus.NOT_FOUND, "source file is no longer available")
        except Exception as error:
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

    def do_POST(self) -> None:
        if self.path != "/api/register":
            self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
            return
        if not self.registration_allowed():
            self.send_error_json(HTTPStatus.FORBIDDEN, "registration is limited to the server computer")
            return
        try:
            self.register_files(read_json_body(self))
        except ValueError as error:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(error))
        except FileNotFoundError as error:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(error))
        except Exception as error:
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if not self.registration_allowed():
            self.send_error_json(HTTPStatus.FORBIDDEN, "registration is limited to the server computer")
            return
        try:
            if path == "/api/entries":
                self.clear_registrations()
            elif path.startswith("/api/entries/"):
                self.delete_registration(path.removeprefix("/api/entries/"))
            else:
                self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
        except Exception as error:
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

    def serve_static(self, name: str, content_type: str) -> None:
        if Path(name).name != name or name not in {"index.html", "app.js", "styles.css"}:
            self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
            return
        file_path = WEB_ROOT / name if name == "index.html" else WEB_ROOT / name
        body = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def public_resource(self, raw_path: str) -> tuple[sqlite3.Row, Path, Path] | None:
        try:
            relative = safe_relative_path(unquote(raw_path).lstrip("/"))
        except ValueError as error:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(error))
            return None
        if not relative.parts:
            self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
            return None

        root_name = relative.parts[0]
        with DB_LOCK, db_connection() as db:
            row = db.execute(
                """
                SELECT files.id, files.source_path, files.kind
                FROM files JOIN batches ON batches.id = files.batch_id
                WHERE files.display_path = ?
                ORDER BY batches.created_at DESC
                LIMIT 1
                """,
                (root_name,),
            ).fetchone()
        if row is None:
            self.send_error_json(HTTPStatus.NOT_FOUND, "shared item not found")
            return None

        source = Path(row["source_path"]).expanduser()
        if row["kind"] == "file":
            if len(relative.parts) != 1:
                self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
                return None
            return row, source, Path()
        try:
            target = inside_directory(source, Path(*relative.parts[1:]))
        except ValueError as error:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(error))
            return None
        return row, target, Path(*relative.parts[1:])

    def serve_public_path(self, path: str) -> None:
        resource = self.public_resource(path)
        if resource is None:
            return
        row, target, _ = resource
        if row["kind"] == "directory" and target.is_dir():
            RUNTIME_STATE.record_event(
                "info", f"{self.client_address[0]} 打开目录 {path}"
            )
            self.serve_static("index.html", "text/html; charset=utf-8")
            return
        self.send_source_file(target)

    def list_public_directory(self, raw_path: str) -> None:
        resource = self.public_resource(raw_path)
        if resource is None:
            return
        row, directory, relative = resource
        if row["kind"] != "directory" or not directory.is_dir():
            self.send_error_json(HTTPStatus.NOT_FOUND, "directory not found")
            return

        entries = []
        try:
            children = sorted(
                directory.iterdir(),
                key=lambda child: (not child.is_dir(), child.name.casefold()),
            )
        except OSError as error:
            self.send_error_json(HTTPStatus.FORBIDDEN, str(error))
            return
        root_name = safe_relative_path(unquote(raw_path).lstrip("/")).parts[0]
        for child in children:
            if child.is_symlink() or not (child.is_dir() or child.is_file()):
                continue
            child_route = Path(root_name) / relative / child.name
            is_directory = child.is_dir()
            entry = {
                "name": child.name,
                "kind": "directory" if is_directory else "file",
                "url": public_url(child_route),
            }
            if is_directory:
                entry["zip_url"] = zip_url(child_route)
            entries.append(entry)

        current_route = Path(root_name) / relative
        parent_url = public_url(current_route.parent) if relative.parts else "/"
        self.send_json(
            {
                "name": directory.name,
                "parent_url": parent_url,
                "entries": entries,
            }
        )

    def list_files(self) -> None:
        with DB_LOCK, db_connection() as db:
            rows = db.execute(
                """
                SELECT batches.id AS batch_id, batches.name AS batch_name,
                       batches.created_at, files.id AS file_id,
                       files.source_path, files.display_path, files.kind,
                       files.size, files.modified
                FROM batches JOIN files ON files.batch_id = batches.id
                ORDER BY batches.created_at DESC, files.display_path COLLATE NOCASE
                """
            ).fetchall()

        batches = {}
        for row in rows:
            batch = batches.setdefault(
                row["batch_id"],
                {
                    "id": row["batch_id"],
                    "name": row["batch_name"],
                    "created_at": row["created_at"],
                    "files": [],
                },
            )
            source = Path(row["source_path"])
            item = {
                "id": row["file_id"],
                "name": Path(row["display_path"]).name,
                "path": row["display_path"],
                "kind": row["kind"],
                "available": source.is_dir() if row["kind"] == "directory" else source.is_file(),
            }
            is_root_item = len(safe_relative_path(row["display_path"]).parts) == 1
            if row["kind"] == "directory":
                item["browse_url"] = (
                    public_url(Path(row["display_path"]))
                    if is_root_item
                    else f"/browse/{row['file_id']}"
                )
                if is_root_item and item["available"]:
                    item["zip_url"] = zip_url(Path(row["display_path"]))
            else:
                try:
                    item["size"] = source.stat().st_size
                except OSError:
                    item["size"] = row["size"]
                item["download_url"] = (
                    public_url(Path(row["display_path"]))
                    if is_root_item
                    else f"/download/{row['file_id']}"
                )
            batch["files"].append(item)
        self.send_json({"batches": list(batches.values())})

    def register_files(self, payload: dict) -> None:
        batch_name = clean_text(payload.get("batch_name"), "拖拽上传")
        raw_files = payload.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            raise ValueError("files must be a non-empty list")

        batch_id = uuid.uuid4().hex
        records = []
        for raw_file in raw_files:
            kind = raw_file.get("kind", "file")
            if kind not in {"file", "directory"}:
                raise ValueError("invalid entry kind")
            source_path = Path(str(raw_file.get("source_path", ""))).expanduser()
            if kind == "directory" and not source_path.is_dir():
                raise FileNotFoundError(f"source directory is unavailable: {source_path}")
            if kind == "file" and not source_path.is_file():
                raise FileNotFoundError(f"source file is unavailable: {source_path}")
            stat = source_path.stat()
            display_path = str(raw_file.get("display_path") or source_path.name)
            display_path = display_path.replace("\x00", "")[:500] or source_path.name
            records.append(
                (
                    uuid.uuid4().hex,
                    batch_id,
                    str(source_path),
                    display_path,
                    kind,
                    stat.st_size if kind == "file" else 0,
                    stat.st_mtime,
                )
            )

        with DB_LOCK, db_connection() as db:
            db.execute(
                "INSERT INTO batches(id, name, created_at) VALUES (?, ?, ?)",
                (batch_id, batch_name, utc_now()),
            )
            db.executemany(
                """
                INSERT INTO files(id, batch_id, source_path, display_path, kind, size, modified)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                records,
            )
            db.commit()
        self.send_json({"ok": True, "batch_id": batch_id, "file_count": len(records)})

    def delete_registration(self, file_id: str) -> None:
        if not FILE_ID_RE.fullmatch(file_id):
            self.send_error_json(HTTPStatus.NOT_FOUND, "registration not found")
            return
        with DB_LOCK, db_connection() as db:
            row = db.execute(
                "SELECT batch_id FROM files WHERE id = ?", (file_id,)
            ).fetchone()
            if row is None:
                self.send_error_json(HTTPStatus.NOT_FOUND, "registration not found")
                return
            db.execute("DELETE FROM files WHERE id = ?", (file_id,))
            db.execute(
                "DELETE FROM batches WHERE id = ? AND NOT EXISTS "
                "(SELECT 1 FROM files WHERE batch_id = ?)",
                (row["batch_id"], row["batch_id"]),
            )
            db.commit()
        self.send_json({"ok": True})

    def clear_registrations(self) -> None:
        with DB_LOCK, db_connection() as db:
            db.execute("DELETE FROM batches")
            db.commit()
        self.send_json({"ok": True})

    def shared_directory_root(self, root_id: str) -> Path | None:
        if not FILE_ID_RE.fullmatch(root_id):
            self.send_error_json(HTTPStatus.NOT_FOUND, "directory not found")
            return None
        with DB_LOCK, db_connection() as db:
            row = db.execute(
                "SELECT source_path, kind FROM files WHERE id = ?", (root_id,)
            ).fetchone()
        if row is None or row["kind"] != "directory":
            self.send_error_json(HTTPStatus.NOT_FOUND, "directory not found")
            return None
        root = Path(row["source_path"]).expanduser()
        if not root.is_dir():
            self.send_error_json(HTTPStatus.NOT_FOUND, "source directory is no longer available")
            return None
        return root

    def list_directory(self, root_id: str, query: str) -> None:
        root = self.shared_directory_root(root_id)
        if root is None:
            return
        try:
            relative = safe_relative_path(parse_qs(query).get("path", [""])[0])
            directory = inside_directory(root, relative)
        except ValueError as error:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(error))
            return
        if not directory.is_dir():
            self.send_error_json(HTTPStatus.NOT_FOUND, "directory not found")
            return

        entries = []
        try:
            children = sorted(
                directory.iterdir(),
                key=lambda child: (not child.is_dir(), child.name.casefold()),
            )
        except OSError as error:
            self.send_error_json(HTTPStatus.FORBIDDEN, str(error))
            return
        for child in children:
            if child.is_symlink() or not (child.is_dir() or child.is_file()):
                continue
            child_relative = relative / child.name
            item: dict[str, str | int] = {"name": child.name, "kind": "directory" if child.is_dir() else "file"}
            if child.is_dir():
                item["browse_url"] = browse_url(root_id, child_relative)
            else:
                try:
                    item["size"] = child.stat().st_size
                except OSError:
                    continue
                item["download_url"] = (
                    f"/download-directory/{root_id}?{urlencode({'path': child_relative.as_posix()})}"
                )
            entries.append(item)

        parent_url = browse_url(root_id, relative.parent) if relative.parts else "/"
        self.send_json(
            {
                "root_id": root_id,
                "name": root.name if not relative.parts else directory.name,
                "path": relative.as_posix(),
                "parent_url": parent_url,
                "entries": entries,
            }
        )

    def download_zip(self, raw_path: str, ticket: str) -> None:
        resource = self.public_resource(raw_path)
        if resource is None:
            return
        row, directory, relative = resource
        root = Path(row["source_path"]).expanduser().resolve()
        if row["kind"] != "directory" or directory != root / relative or not directory.is_dir():
            self.send_error_json(HTTPStatus.NOT_FOUND, "directory not found")
            return
        if not ZIP_DOWNLOAD_SLOT.acquire(blocking=False):
            self.send_error_json(HTTPStatus.SERVICE_UNAVAILABLE, "已有压缩下载进行中，请稍后重试")
            return
        try:
            with tempfile.TemporaryFile() as output:
                try:
                    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
                        self.archive_directory(directory, archive)
                except ValueError:
                    self.send_error_json(HTTPStatus.BAD_REQUEST, "目录包含不支持的文件名")
                    return
                except OSError:
                    self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "无法读取目录或创建 ZIP")
                    return
                self.send_open_file(
                    output, f"{directory.name or 'folder'}.zip", output.tell(), zip_ticket=ticket
                )
        except (ConnectionError, TimeoutError):
            self.close_connection = True
        finally:
            ZIP_DOWNLOAD_SLOT.release()

    @staticmethod
    def archive_directory(directory: Path, archive: ZipFile) -> None:
        def raise_walk_error(error: OSError) -> None:
            raise error

        flags = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        for current, dirs, files in os.walk(directory, onerror=raise_walk_error):
            kept_dirs = []
            for name in dirs + files:
                child = Path(current) / name
                metadata = child.lstat()
                if stat.S_ISLNK(metadata.st_mode) or (
                    getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    continue
                if not child.resolve().is_relative_to(directory):
                    raise OSError("directory changed during compression")
                if "\\" in name or ":" in name or name.rstrip(" .") != name:
                    raise ValueError("unsafe ZIP entry name")
                relative = child.relative_to(directory).as_posix()
                if stat.S_ISDIR(metadata.st_mode):
                    kept_dirs.append(name)
                    archive.writestr(f"{relative}/", b"")
                elif stat.S_ISREG(metadata.st_mode):
                    with open(child, "rb", opener=lambda path, mode: os.open(path, mode | flags)) as source:
                        if not os.path.samestat(metadata, os.fstat(source.fileno())):
                            raise OSError("file changed during compression")
                        with archive.open(relative, "w", force_zip64=True) as target:
                            shutil.copyfileobj(source, target)
            dirs[:] = kept_dirs

    def send_source_file(self, source: Path) -> None:
        if not source.is_file():
            self.send_error_json(HTTPStatus.NOT_FOUND, "source file is no longer available")
            return
        with source.open("rb") as file_handle:
            self.send_open_file(file_handle, source.name, os.fstat(file_handle.fileno()).st_size)

    def send_open_file(
        self, file_handle: BinaryIO, name: str, size: int, *, zip_ticket: str = ""
    ) -> None:
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        start, end = 0, max(size - 1, 0)
        status = HTTPStatus.OK
        range_header = self.headers.get("Range")
        if range_header:
            parsed_range = _parse_byte_range(range_header, size)
            if parsed_range is None:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start, end = parsed_range
            status = HTTPStatus.PARTIAL_CONTENT
        length = 0 if size == 0 else end - start + 1
        self.send_response(status)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header(
            "Content-Disposition",
            f"attachment; filename*=UTF-8''{quote(name)}",
        )
        if FILE_ID_RE.fullmatch(zip_ticket):
            self.send_header("Set-Cookie", f"hfs_zip_{zip_ticket}=1; Path=/; Max-Age=120; SameSite=Lax")
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command == "HEAD" or length == 0:
            return
        bytes_sent = 0
        started_at = time.monotonic()
        chunk_size = min(DOWNLOAD_LIMITER.chunk_size(), 64 * 1024)
        download_id = RUNTIME_STATE.start_download(
            self.client_address[0], name, length, self.path
        )
        outcome = "failed"
        try:
            file_handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = file_handle.read(min(chunk_size, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
                bytes_sent += len(chunk)
                RUNTIME_STATE.advance_download(download_id, bytes_sent)
                DOWNLOAD_LIMITER.throttle(bytes_sent, started_at)
            if remaining == 0:
                outcome = "completed"
        except (ConnectionError, TimeoutError):
            outcome = "cancelled"
        except OSError:
            # Headers have already been sent; record failure without writing
            # a second HTTP response into the video stream.
            outcome = "failed"
        finally:
            RUNTIME_STATE.finish_download(download_id, outcome)

    def download_file(self, file_id: str) -> None:
        if not FILE_ID_RE.fullmatch(file_id):
            self.send_error_json(HTTPStatus.NOT_FOUND, "file not found")
            return
        with DB_LOCK, db_connection() as db:
            row = db.execute(
                "SELECT source_path, kind FROM files WHERE id = ?", (file_id,)
            ).fetchone()
        if row is None or row["kind"] != "file":
            self.send_error_json(HTTPStatus.NOT_FOUND, "file not found")
            return
        self.send_source_file(Path(row["source_path"]).expanduser())

    def download_directory_file(self, root_id: str, query: str) -> None:
        root = self.shared_directory_root(root_id)
        if root is None:
            return
        try:
            relative = safe_relative_path(parse_qs(query).get("path", [""])[0])
            source = inside_directory(root, relative)
        except ValueError as error:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(error))
            return
        self.send_source_file(source)


def _process_command(pid: int) -> str:
    completed = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _process_cwd(pid: int) -> Path | None:
    completed = subprocess.run(
        ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
        check=False,
        capture_output=True,
        text=True,
    )
    for line in completed.stdout.splitlines():
        if line.startswith("n"):
            return Path(line[1:])
    return None


def _is_hfs_server(pid: int) -> bool:
    command = _process_command(pid)
    if "server.py" not in command:
        return False
    if str(BASE_DIR / "server.py") in command or "hfs/server.py" in command:
        return True
    cwd = _process_cwd(pid)
    return cwd is not None and cwd.resolve() == BASE_DIR.resolve()


def _listening_pids(port: int) -> list[int]:
    completed = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        check=False,
        capture_output=True,
        text=True,
    )
    pids: list[int] = []
    for line in completed.stdout.split():
        try:
            pid = int(line)
        except ValueError:
            continue
        if pid != os.getpid():
            pids.append(pid)
    return pids


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def close_existing_hfs(port: int) -> None:
    """启动前关掉已经打开的 HFS，避免端口占用导致这次打不开。"""
    pids = [pid for pid in _listening_pids(port) if _is_hfs_server(pid)]
    if not pids:
        return
    print("关闭已打开的 HFS: " + ", ".join(str(pid) for pid in pids))
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and any(_process_alive(pid) for pid in pids):
        time.sleep(0.05)
    for pid in pids:
        if not _process_alive(pid):
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and _listening_pids(port):
        time.sleep(0.05)


def main() -> None:
    import argparse

    from hfs_frpc import start_frpc, stop_frpc
    from hfs_terminal import install_terminal_log

    parser = argparse.ArgumentParser(description="HFS 本地文件共享与视频服务")
    parser.add_argument("--headless", action="store_true", help="无头模式，不打开桌面窗口，也不需要 Tk")
    parser.add_argument("--port", metavar="PORT", help="监听端口（1–65535），优先于 HFS_PORT 和保存的设置")
    args = parser.parse_args()
    try:
        port = parse_port(args.port) if args.port is not None else configured_port()
    except ValueError as error:
        source = "--port" if args.port is not None else "HFS_PORT"
        parser.error(f"{source}: {error}")

    TkinterDnD = None
    UploadClient = None
    if not args.headless:
        try:
            from tkinterdnd2 import TkinterDnD
            from client import UploadClient
        except ModuleNotFoundError as error:
            if error.name == "_tkinter":
                raise SystemExit(
                    "当前 Python 没有 Tk 支持，请使用 /usr/bin/python3 server.py，或添加 --headless"
                ) from error
            raise SystemExit(
                "缺少 tkinterdnd2，请先安装该依赖，或使用 --headless"
            ) from error

    install_terminal_log()
    initialize_database()
    host = os.environ.get("HFS_HOST", "0.0.0.0")
    close_existing_hfs(port)
    server_controller = ServerController(host, port)
    frpc_process = None
    stop_event = threading.Event()
    previous_handlers = {}

    def request_stop(signum, frame) -> None:
        stop_event.set()

    if args.headless:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, request_stop)

    try:
        try:
            server_controller.start()
        except OSError as error:
            raise SystemExit(f"HFS 无法监听 {host}:{port}：{error}") from error
        print(f"HFS listening on http://{host}:{port}")
        print(f"登记数据库: {DATABASE_PATH}")
        print(f"本机访问: http://127.0.0.1:{port}")
        addresses = lan_addresses()
        for address in addresses:
            print(f"局域网访问: http://{address}:{port}")
        if not addresses:
            print("局域网访问地址未能自动探测，请查看本机网络设置")

        if args.headless:
            print("无头模式：使用已有共享登记；按 Ctrl+C 停止 HFS 和本次启动的 FRPC。")
            frpc_process = start_frpc()
            stop_event.wait()
        else:
            assert TkinterDnD is not None and UploadClient is not None
            root = TkinterDnD.Tk()
            frpc_process = start_frpc()
            UploadClient(
                root,
                f"http://127.0.0.1:{port}",
                [f"http://{address}:{port}" for address in addresses],
                server_controller.change_port,
                set_download_limit_kbps,
                configured_download_limit_kbps(),
            )
            root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server_controller.stop()
        finally:
            stop_frpc(frpc_process)
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)


if __name__ == "__main__":
    main()
