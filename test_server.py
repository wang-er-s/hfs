from __future__ import annotations

import http.client
import io
import json
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from errno import EADDRINUSE
from http.client import HTTPMessage
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import patch
from zipfile import ZipFile

import server
from hfs_runtime import RuntimeState


class ServerControllerTests(unittest.TestCase):
    def test_port_switch_rejects_existing_loopback_listener(self):
        controller = server.ServerController("0.0.0.0", 0)
        controller.start()
        self.addCleanup(controller.stop)
        original_server = controller.http_server
        assert original_server is not None
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            with self.assertRaises(OSError) as raised:
                controller.change_port(listener.getsockname()[1])

        self.assertEqual(raised.exception.errno, EADDRINUSE)
        self.assertIs(controller.http_server, original_server)
        connection = http.client.HTTPConnection(
            "127.0.0.1", original_server.server_port, timeout=3
        )
        self.addCleanup(connection.close)
        connection.request("GET", "/api/health")
        with connection.getresponse() as response:
            self.assertEqual(response.status, 200)


class DatabaseConnectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / "test.sqlite3"
        database_patch = patch.object(server, "DATABASE_PATH", self.database)
        database_patch.start()
        self.addCleanup(database_patch.stop)

    def test_connection_closes_and_commits(self):
        with server.db_connection() as connection:
            connection.execute("CREATE TABLE entries (name TEXT)")
            connection.execute("INSERT INTO entries VALUES ('video')")
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        with server.db_connection() as reopened:
            self.assertEqual(reopened.execute("SELECT name FROM entries").fetchone()[0], "video")

    def test_connection_closes_and_rolls_back_on_error(self):
        with server.db_connection() as connection:
            connection.execute("CREATE TABLE entries (name TEXT)")
        with (
            self.assertRaisesRegex(RuntimeError, "interrupted"),
            server.db_connection() as connection,
        ):
            connection.execute("INSERT INTO entries VALUES ('video')")
            raise RuntimeError("interrupted")
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        with server.db_connection() as reopened:
            self.assertEqual(reopened.execute("SELECT count(*) FROM entries").fetchone()[0], 0)


class ZipDownloadTests(unittest.TestCase):
    def test_public_zip_download(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        database = patch.object(server, "DATABASE_PATH", Path(self.directory.name) / "db.sqlite3")
        database.start()
        self.addCleanup(database.stop)
        root = Path(self.directory.name) / "共享 %23"
        nested = root / "子目录 #"
        (nested / "empty").mkdir(parents=True)
        (nested / "file.txt").write_bytes(b"archive contents")
        outside = root.with_name("private.txt")
        outside.write_bytes(b"not shared")
        try:
            (nested / "link").symlink_to(outside)
        except (NotImplementedError, OSError):
            pass
        server.initialize_database()
        controller = server.ServerController("127.0.0.1", 0)
        controller.start()
        self.addCleanup(controller.stop)
        connection = http.client.HTTPConnection("127.0.0.1", controller.http_server.server_port, timeout=3)
        self.addCleanup(connection.close)

        def request(path, payload=None):
            connection.request("POST" if payload else "GET", path, json.dumps(payload) if payload else None)
            with connection.getresponse() as response:
                return response.status, response.headers, response.read()

        with patch.object(server, "REGISTER_TOKEN", ""):
            status, _, _ = request("/api/register", {"files": [{"source_path": str(root), "kind": "directory"}]})
        self.assertEqual(status, 200)
        shared = json.loads(request("/api/files")[2])["batches"][0]["files"][0]
        status, headers, body = request(shared["zip_url"])
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/zip")
        with ZipFile(io.BytesIO(body)) as archive:
            self.assertEqual(set(archive.namelist()), {"子目录 #/", "子目录 #/empty/", "子目录 #/file.txt"})
            self.assertEqual(archive.read("子目录 #/file.txt"), b"archive contents")
        query = urlencode({"path": shared["browse_url"]})
        entries = json.loads(request(f"/api/public-directory?{query}")[2])["entries"]
        self.assertEqual(request(entries[0]["zip_url"])[0], 200)
        self.assertEqual(request(entries[0]["zip_url"] + "%2Ffile.txt")[0], 404)
        self.assertEqual(request(entries[0]["zip_url"] + "%2F..%2Fprivate.txt")[0], 400)


class VideoTransferTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.media = Path(self.directory.name) / "video.mp4"
        self.content = bytes(range(256)) * 1024
        self.media.write_bytes(self.content)
        self.state = RuntimeState()
        state_patch = patch.object(server, "RUNTIME_STATE", self.state)
        state_patch.start()
        self.addCleanup(state_patch.stop)
        limiter_patch = patch.object(server, "DOWNLOAD_LIMITER", server.DOWNLOAD_LIMITER.__class__())
        limiter_patch.start()
        self.addCleanup(limiter_patch.stop)

        media = self.media
        request_workers = self.request_workers = []

        class Handler(server.HfsHandler):
            timeout = 0.2

            def log_message(self, format: str, *args):
                pass

            def do_GET(self):
                request_workers.append(threading.current_thread())
                self.send_source_file(media)

        self.http = server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.worker = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.http.shutdown()
        self.http.server_close()
        self.worker.join(timeout=2)

    def wait_until(self, predicate, message):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail(message)

    def request(self, byte_range=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.http.server_port, timeout=3)
        try:
            headers = {"Range": byte_range} if byte_range else {}
            connection.request("GET", "/video.mp4", headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_full_file_and_byte_ranges(self):
        for byte_range, expected_status, expected_body, expected_range in [
            (None, 200, self.content, None),
            ("bytes=100-199", 206, self.content[100:200], f"bytes 100-199/{len(self.content)}"),
            ("bytes=262100-", 206, self.content[262100:], "bytes 262100-262143/262144"),
            ("bytes=-123", 206, self.content[-123:], "bytes 262021-262143/262144"),
            ("bytes=999999-", 416, b"", f"bytes */{len(self.content)}"),
        ]:
            with self.subTest(byte_range=byte_range):
                status, headers, body = self.request(byte_range)
                self.assertEqual(status, expected_status)
                self.assertEqual(body, expected_body)
                self.assertEqual(int(headers["Content-Length"]), len(body))
                self.assertEqual(headers.get("Content-Range"), expected_range)
        self.wait_until(lambda: self.state.snapshot().active_count == 0, "Completed transfers retained")

    def stalled_reader(self):
        with self.media.open("wb") as file:
            file.truncate(64 * 1024 * 1024)
        client = socket.socket()
        self.addCleanup(client.close)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        client.settimeout(3)
        client.connect(self.http.server_address)
        client.sendall(b"GET /video.mp4 HTTP/1.0\r\nRange: bytes=0-\r\n\r\n")
        self.wait_until(lambda: self.state.snapshot().active_count == 1, "Transfer did not start")
        return client

    def test_stalled_reader_releases_worker_and_file(self):
        opened_files = []
        original_open = Path.open

        def track_open(path, *args, **kwargs):
            file = original_open(path, *args, **kwargs)
            if path == self.media and args and args[0] == "rb":
                opened_files.append(file)
            return file

        with patch.object(Path, "open", track_open):
            self.stalled_reader()
            self.wait_until(lambda: self.state.snapshot().active_count == 0, "Stalled transfer retained")
            self.wait_until(lambda: not any(t.is_alive() for t in self.request_workers), "Worker retained")
        self.assertTrue(opened_files)
        self.assertTrue(all(file.closed for file in opened_files))
        self.assertEqual(self.state.snapshot().events[0].level, "cancelled")

    def test_disconnected_reader_releases_transfer(self):
        client = self.stalled_reader()
        client.close()
        self.wait_until(lambda: self.state.snapshot().active_count == 0, "Disconnected transfer retained")
        self.assertEqual(self.state.snapshot().events[0].level, "cancelled")

    def test_unexpected_error_still_releases_transfer(self):
        handler = server.HfsHandler.__new__(server.HfsHandler)
        handler.headers = HTTPMessage()
        handler.client_address = ("127.0.0.1", 1234)
        handler.command = "GET"
        handler.path = "/video.mp4"

        class BrokenWriter:
            def write(self, data):
                raise RuntimeError("unexpected failure")

        with (
            patch.object(handler, "wfile", BrokenWriter(), create=True),
            patch.object(handler, "send_response"),
            patch.object(handler, "send_header"),
            patch.object(handler, "end_headers"),
            self.assertRaisesRegex(RuntimeError, "unexpected failure"),
        ):
            handler.send_source_file(self.media)
        self.assertEqual(self.state.snapshot().active_count, 0)
        self.assertEqual(self.state.snapshot().events[0].level, "failed")


if __name__ == "__main__":
    unittest.main()
