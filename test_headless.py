"""Subprocess coverage for startup without Tk and graceful signal shutdown."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


class HeadlessTests(unittest.TestCase):
    def test_headless_without_tk_and_graceful_shutdown(self):
        for stop_signal in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=stop_signal), tempfile.TemporaryDirectory() as directory:
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    port = listener.getsockname()[1]
                environment = dict(os.environ, HFS_HOST="127.0.0.1", HFS_PORT=str(port),
                                   HFS_DATABASE=str(Path(directory) / "test.sqlite3"), HFS_FRPC="0")
                script = '''
import importlib.abc
import runpy
import sys
class NoTk(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'tkinter', '_tkinter', 'tkinterdnd2', 'client'}:
            raise AssertionError('Headless imported GUI: ' + fullname)
sys.meta_path.insert(0, NoTk())
sys.argv = ['server.py', '--headless']
runpy.run_path('server.py', run_name='__main__')
'''
                process = subprocess.Popen([sys.executable, "-u", "-c", script], cwd=BASE_DIR,
                                           env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           text=True)
                output = ""
                try:
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        if process.poll() is not None:
                            output, _ = process.communicate()
                            self.fail(output)
                        try:
                            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=.5) as response:
                                self.assertEqual(json.load(response), {"ok": True})
                            break
                        except (urllib.error.URLError, TimeoutError):
                            time.sleep(.05)
                    else:
                        self.fail("Headless server did not become ready")
                    process.send_signal(stop_signal)
                    output, _ = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 0, output)
                    self.assertNotIn("Traceback", output)
                    with socket.socket() as probe:
                        self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)), 0)
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.communicate()


if __name__ == "__main__":
    unittest.main()
