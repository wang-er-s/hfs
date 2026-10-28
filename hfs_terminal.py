"""Bounded live terminal log; redirected output remains unchanged."""

from __future__ import annotations

import atexit
import re
import shutil
import sys
import threading
import unicodedata
from collections import deque

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class TerminalLog:
    def __init__(self, stream, limit: int = 20):
        self.stream = stream
        self.lines: deque[str] = deque(maxlen=limit)
        self.pending: dict[int, str] = {}
        self.lock = threading.RLock()
        self.closed = False
        self.encoding = stream.encoding
        stream.write("\x1b[?1049h")
        stream.flush()

    def isatty(self):
        return True

    def fileno(self):
        return self.stream.fileno()

    def write(self, text):
        with self.lock:
            if self.closed:
                return self.stream.write(text)
            key = threading.get_ident()
            parts = (self.pending.get(key, "") + text).split("\n")
            self.pending[key] = parts.pop()
            for line in parts:
                clean = ANSI.sub("", line)
                self.lines.append("".join(c for c in clean if c == "\t" or ord(c) >= 32).expandtabs(4))
            if parts:
                self.render()
        return len(text)

    def render(self):
        columns, rows = shutil.get_terminal_size()
        visible = list(self.lines)[-max(1, rows - 1):]
        clipped = []
        for line in visible:
            width = 0
            chars = []
            for char in line:
                size = 0 if unicodedata.combining(char) else (2 if unicodedata.east_asian_width(char) in "WF" else 1)
                if width + size > max(1, columns - 1):
                    break
                chars.append(char)
                width += size
            clipped.append("".join(chars))
        self.stream.write("\x1b[H\x1b[2J" + "\n".join(clipped) + "\n")
        self.stream.flush()

    def flush(self):
        with self.lock:
            self.stream.flush()

    def finish(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.stream.write("\x1b[?1049l")
            for line in self.lines:
                self.stream.write(line + "\n")
            for text in self.pending.values():
                if text:
                    self.stream.write(text + "\n")
            self.stream.flush()


def install_terminal_log():
    if sys.stdout.isatty() and sys.stderr.isatty():
        log = TerminalLog(sys.stdout)
        sys.stdout = log
        sys.stderr = log
        atexit.register(log.finish)
