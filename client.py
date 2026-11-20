from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
import webbrowser
from collections import deque
from pathlib import Path
from tkinter import (
    BOTH,
    HORIZONTAL,
    LEFT,
    RIGHT,
    X,
    Y,
    Canvas,
    Frame,
    Label,
    PanedWindow,
    PhotoImage,
    StringVar,
    Text,
    messagebox,
)
from tkinter.ttk import Button, Entry, Scrollbar, Style, Treeview
from urllib.parse import urlparse

from tkinterdnd2 import DND_FILES, TkinterDnD


DEFAULT_SERVER = os.environ.get("HFS_SERVER", "http://127.0.0.1:8765")
REGISTER_TOKEN = os.environ.get("HFS_REGISTER_TOKEN", "")


def format_bytes(value: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    amount = max(0, value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return "0 B"


def format_rate(value: int) -> str:
    return f"{format_bytes(value)}/s"


def collect_files(dropped_paths: list[str]) -> list[dict]:
    entries = []
    for raw_path in dropped_paths:
        path = Path(raw_path).expanduser()
        if path.is_file():
            entries.append(
                {"kind": "file", "source_path": str(path), "display_path": path.name}
            )
        elif path.is_dir():
            entries.append(
                {
                    "kind": "directory",
                    "source_path": str(path),
                    "display_path": path.name,
                }
            )
    return entries


class UploadClient:
    def __init__(
        self,
        root: TkinterDnD.Tk,
        server_url: str = DEFAULT_SERVER,
        lan_urls: list[str] | None = None,
        change_port=None,
        set_download_limit=None,
        download_limit_kbps: int = 0,
    ):
        self.root = root
        self.colors = {
            "app": "#e9edf1",
            "toolbar": "#d7e1ea",
            "address": "#eef3f7",
            "shared": "#f4f8fb",
            "shared_header": "#d9e7f2",
            "log": "#f3f7f5",
            "log_header": "#dce9e2",
            "transfer": "#e6ebf0",
            "status": "#d5dde5",
            "text": "#1f2933",
            "button": "#f8fafc",
        }
        root.configure(bg=self.colors["app"])
        style = Style(root)
        style.theme_use("clam")
        style.configure(
            "Hfs.TButton",
            background="#f8fafc",
            foreground="#1f2933",
            bordercolor="#c6d1dc",
            lightcolor="#f8fafc",
            darkcolor="#c6d1dc",
            padding=(8, 4),
        )
        style.map(
            "Hfs.TButton",
            background=[("active", "#edf3f8"), ("pressed", "#dbe7f1")],
            bordercolor=[("active", "#8ea9c1"), ("focus", "#8ea9c1")],
        )
        style.configure(
            "Hfs.TEntry",
            fieldbackground="#ffffff",
            foreground=self.colors["text"],
            bordercolor="#c6d1dc",
            lightcolor="#ffffff",
            darkcolor="#c6d1dc",
            padding=(5, 3),
        )
        style.map(
            "Hfs.TEntry",
            bordercolor=[("focus", "#7da2c4")],
            lightcolor=[("focus", "#ffffff")],
        )
        style.configure(
            "Hfs.Treeview",
            background="#fbfcfd",
            foreground=self.colors["text"],
            fieldbackground="#fbfcfd",
            rowheight=24,
        )
        style.configure(
            "Hfs.Treeview.Heading",
            background="#cfd9e2",
            foreground=self.colors["text"],
        )
        style.map(
            "Hfs.Treeview",
            background=[("selected", "#b9d5ea")],
            foreground=[("selected", self.colors["text"])],
        )
        self.server_url = StringVar(value=server_url)
        self.public_url = StringVar(value=server_url)
        self.port = StringVar(value=str(urlparse(server_url).port or 8765))
        self.download_limit = StringVar(value=str(download_limit_kbps))
        self.status = StringVar(value="把文件或文件夹拖到共享项目区域")
        self.summary = StringVar(value="连接: 0 | 输出: 0 B/s | 总输出: 0 B | 已登记: 0")
        self.lan_hosts = [urlparse(url).hostname for url in lan_urls or []]
        self.lan_hosts = [host for host in self.lan_hosts if host]
        self.lan_address = StringVar()
        self.change_port = change_port
        self.set_download_limit = set_download_limit
        self._download_limit_kbps = download_limit_kbps
        self._registered_count = 0
        self._runtime_request_pending = False
        self._speed_history: deque[int] = deque(maxlen=90)
        self.download_limit.trace_add("write", self.on_download_limit_change)
        self.update_lan_address(int(self.port.get()))

        root.title("HFS 局域网文件服务器")
        # Keep the PhotoImage alive for Tk's window/application icon.
        self.app_icon = PhotoImage(file=str(Path(__file__).resolve().parent / "assets" / "hfs.gif"))
        root.iconphoto(True, self.app_icon)
        self.toolbar_icon = self.app_icon.subsample(4, 4)
        root.geometry("1080x720")
        root.minsize(800, 560)

        toolbar = Frame(root, padx=12, pady=8, bg=self.colors["toolbar"])
        toolbar.pack(fill=X)
        Label(toolbar, image=self.toolbar_icon, bg=self.colors["toolbar"]).pack(side=LEFT, padx=(0, 10))
        Button(toolbar, text="打开网页", command=self.open_public_url, style="Hfs.TButton").pack(side=LEFT)
        Button(toolbar, text="测试连接", command=self.test_connection, style="Hfs.TButton").pack(side=LEFT, padx=(6, 18))
        Label(toolbar, text="监听端口", bg=self.colors["toolbar"], fg=self.colors["text"]).pack(side=LEFT)
        Entry(toolbar, textvariable=self.port, width=7, style="Hfs.TEntry").pack(side=LEFT, padx=(6, 5))
        Button(toolbar, text="应用端口", command=self.apply_port, style="Hfs.TButton").pack(side=LEFT, padx=(0, 18))
        Label(toolbar, text="下载限速", bg=self.colors["toolbar"], fg=self.colors["text"]).pack(side=LEFT)
        Entry(toolbar, textvariable=self.download_limit, width=7, style="Hfs.TEntry").pack(side=LEFT, padx=(6, 4))
        Label(toolbar, text="KB/s（0 不限速）", bg=self.colors["toolbar"], fg=self.colors["text"]).pack(side=LEFT)
        Label(toolbar, text="运行中", fg="#13795b", bg=self.colors["toolbar"]).pack(side=RIGHT)

        address_row = Frame(root, padx=12, pady=7, bg=self.colors["address"])
        address_row.pack(fill=X, pady=(0, 8))
        Label(address_row, text="浏览器地址", bg=self.colors["address"], fg=self.colors["text"]).pack(side=LEFT)
        Entry(address_row, textvariable=self.public_url, style="Hfs.TEntry").pack(
            side=LEFT, fill=X, expand=True, padx=(8, 6)
        )
        Button(address_row, text="复制", command=self.copy_public_url, style="Hfs.TButton").pack(side=LEFT)

        self.speed_graph = Canvas(root, height=54, bg="#10161b", highlightthickness=0)
        self.speed_graph.pack(fill=X, padx=12, pady=(0, 8))
        self.runtime_text = StringVar(value="当前输出: 0 B/s    活跃下载: 0    限速: 不限速")

        workspace = PanedWindow(root, orient=HORIZONTAL, sashrelief="raised", showhandle=True, bg="#bcc8d3")
        workspace.pack(fill=BOTH, expand=True, padx=12, pady=(0, 8))
        shared_panel = Frame(workspace, bg=self.colors["shared"])
        log_panel = Frame(workspace, bg=self.colors["log"])
        workspace.add(shared_panel, minsize=300)
        workspace.add(log_panel, minsize=360)

        shared_header = Frame(shared_panel, pady=5, bg=self.colors["shared_header"])
        shared_header.pack(fill=X)
        Label(shared_header, text="共享项目", anchor="w", bg=self.colors["shared_header"], fg=self.colors["text"]).pack(side=LEFT, fill=X, expand=True)
        Button(shared_header, text="刷新", command=self.refresh_registered, style="Hfs.TButton").pack(side=LEFT, padx=(0, 5))
        Button(shared_header, text="清空登记", command=self.clear_registrations, style="Hfs.TButton").pack(side=LEFT)
        shared_list = Frame(shared_panel, bg=self.colors["shared"])
        shared_list.pack(fill=BOTH, expand=True)
        self.entry_canvas = Canvas(shared_list, highlightthickness=0, bg=self.colors["shared"])
        scrollbar = Scrollbar(shared_list, orient="vertical", command=self.entry_canvas.yview)
        self.entry_canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=RIGHT, fill=Y)
        self.entry_canvas.pack(side=LEFT, fill=BOTH, expand=True)
        self.entry_list = Frame(self.entry_canvas, bg=self.colors["shared"])
        self.entry_window = self.entry_canvas.create_window(
            (0, 0), window=self.entry_list, anchor="nw"
        )
        self.entry_list.bind("<Configure>", self.update_scroll_region)
        self.entry_canvas.bind("<Configure>", self.resize_entry_list)

        log_header = Frame(log_panel, pady=5, bg=self.colors["log_header"])
        log_header.pack(fill=X)
        Label(log_header, text="访问日志 · 最近 20 条", anchor="w", bg=self.colors["log_header"], fg=self.colors["text"]).pack(side=LEFT, fill=X, expand=True)
        self.log_view = Text(
            log_panel,
            wrap="none",
            height=12,
            state="disabled",
            bg=self.colors["log"],
            fg=self.colors["text"],
            insertbackground=self.colors["text"],
            selectbackground="#b8d6c5",
            borderwidth=0,
            relief="flat",
            highlightthickness=0,
        )
        log_scrollbar = Scrollbar(log_panel, orient="vertical", command=self.log_view.yview)
        self.log_view.configure(yscrollcommand=log_scrollbar.set)
        log_scrollbar.pack(side=RIGHT, fill=Y)
        self.log_view.pack(side=LEFT, fill=BOTH, expand=True)
        self.log_view.tag_configure("time", foreground="#d4493e")
        self.log_view.tag_configure("request", foreground="#8c5a00")
        self.log_view.tag_configure("completed", foreground="#13795b")
        self.log_view.tag_configure("cancelled", foreground="#6b7280")
        self.log_view.tag_configure("failed", foreground="#b42318")

        transfer_panel = Frame(root, padx=12, pady=5, bg=self.colors["transfer"])
        transfer_panel.pack(fill=X, pady=(0, 4))
        Label(transfer_panel, text="当前下载", anchor="w", bg=self.colors["transfer"], fg=self.colors["text"]).pack(fill=X)
        columns = ("address", "filename", "progress", "speed", "status")
        self.download_table = Treeview(transfer_panel, style="Hfs.Treeview", columns=columns, show="headings", height=4)
        for column, title, width in (
            ("address", "IP", 150),
            ("filename", "文件名", 310),
            ("progress", "进度", 190),
            ("speed", "速度", 130),
            ("status", "状态", 100),
        ):
            self.download_table.heading(column, text=title)
            self.download_table.column(column, width=width, anchor="w", stretch=column == "filename")
        self.download_table.pack(fill=X)

        root.drop_target_register(DND_FILES)
        root.dnd_bind("<<Drop>>", self.on_drop)

        bottom = Frame(root, padx=12, pady=7, relief="sunken", borderwidth=1, bg=self.colors["status"])
        bottom.pack(fill=X)
        Label(bottom, textvariable=self.summary, anchor="w", bg=self.colors["status"], fg=self.colors["text"]).pack(side=LEFT, fill=X, expand=True)
        Label(bottom, textvariable=self.status, anchor="e", fg="#45515c", bg=self.colors["status"]).pack(side=RIGHT)
        self.refresh_registered()
        self.refresh_runtime_status()

    def update_scroll_region(self, event=None) -> None:
        self.entry_canvas.configure(scrollregion=self.entry_canvas.bbox("all"))

    def resize_entry_list(self, event) -> None:
        self.entry_canvas.itemconfigure(self.entry_window, width=event.width)

    def clear_entry_rows(self) -> None:
        for row in self.entry_list.winfo_children():
            row.destroy()

    def add_entry_row(self, item: dict, entry_id: str | None = None) -> None:
        row = Frame(self.entry_list, padx=10, pady=6, bg=self.colors["shared"])
        row.pack(fill=X)
        kind = "目录" if item.get("kind") == "directory" else "文件"
        availability = "" if item.get("available", True) else " (原路径不可用)"
        text = f"[{kind}] {item.get('path', item.get('display_path', ''))}{availability}"
        Label(row, text=text, anchor="w", bg=self.colors["shared"], fg=self.colors["text"]).pack(side=LEFT, fill=X, expand=True)
        if entry_id:
            Button(row, text="删除", command=lambda: self.delete_entry(entry_id), style="Hfs.TButton").pack(side=RIGHT)

    def add_message_row(self, text: str) -> None:
        row = Frame(self.entry_list, padx=10, pady=8, bg=self.colors["shared"])
        row.pack(fill=X)
        Label(row, text=text, anchor="w", fg="#68757d", bg=self.colors["shared"]).pack(fill=X)

    def endpoint(self, path: str) -> str:
        return f"{self.server_url.get().rstrip('/')}{path}"

    def headers(self) -> dict[str, str]:
        return {"X-Register-Token": REGISTER_TOKEN} if REGISTER_TOKEN else {}

    def update_lan_address(self, port: int) -> None:
        lan_urls = [f"http://{host}:{port}" for host in self.lan_hosts]
        lan_text = "、".join(lan_urls) or "未探测到"
        self.lan_address.set(f"局域网网页地址: {lan_text}")
        self.public_url.set(lan_urls[0] if lan_urls else self.server_url.get())

    def copy_public_url(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.public_url.get())
        self.status.set("浏览器地址已复制")

    def open_public_url(self) -> None:
        webbrowser.open(self.public_url.get())

    def refresh_runtime_status(self) -> None:
        if not self._runtime_request_pending:
            self._runtime_request_pending = True
            threading.Thread(target=self.load_runtime_status, daemon=True).start()
        self.root.after(1000, self.refresh_runtime_status)

    def load_runtime_status(self) -> None:
        try:
            data = self.get_json(self.endpoint("/api/status"), self.headers())
        except (OSError, ValueError, KeyError, urllib.error.HTTPError):
            data = None
        self.root.after(0, lambda: self.show_runtime_status(data))

    def show_runtime_status(self, data: dict | None) -> None:
        self._runtime_request_pending = False
        if data is None:
            return
        downloads = data.get("active_downloads", [])
        speed = int(data.get("current_speed", 0))
        total = int(data.get("total_bytes_sent", 0))
        limit = self._download_limit_kbps
        limit_text = "不限速" if limit == 0 else f"{limit} KB/s"
        self.runtime_text.set(
            f"当前输出: {format_rate(speed)}    活跃下载: {len(downloads)}    限速: {limit_text}"
        )
        self.summary.set(
            f"连接: {len(downloads)} | 输出: {format_rate(speed)} | "
            f"总输出: {format_bytes(total)} | 已登记: {self._registered_count}"
        )
        self.draw_speed_graph(speed)
        self.show_events(data.get("events", []))
        self.show_downloads(downloads)

    def draw_speed_graph(self, speed: int) -> None:
        self._speed_history.append(speed)
        graph = self.speed_graph
        width = max(graph.winfo_width(), 1)
        height = max(graph.winfo_height(), 1)
        graph.delete("all")
        for x in range(0, width, 24):
            graph.create_line(x, 0, x, height, fill="#1f2d3a")
        for y in range(0, height, 16):
            graph.create_line(0, y, width, y, fill="#1f2d3a")
        history = list(self._speed_history)
        if len(history) > 1:
            ceiling = max(max(history), 1)
            coordinates = []
            for index, value in enumerate(history):
                x = index * width / max(len(history) - 1, 1)
                y = height - 5 - (value / ceiling) * max(height - 16, 1)
                coordinates.extend((x, y))
            graph.create_line(*coordinates, fill="#e737e9", width=2)
        graph.create_text(
            width - 8,
            8,
            anchor="ne",
            fill="#d7e5eb",
            text=self.runtime_text.get(),
        )

    def show_events(self, events: list[dict]) -> None:
        self.log_view.configure(state="normal")
        self.log_view.delete("1.0", "end")
        for event in events[:20]:
            level = event.get("level", "info")
            self.log_view.insert("end", f"{event.get('time', '--:--:--')} ", "time")
            self.log_view.insert("end", f"{event.get('message', '')}\n", level)
        self.log_view.configure(state="disabled")

    def show_downloads(self, downloads: list[dict]) -> None:
        for item_id in self.download_table.get_children():
            self.download_table.delete(item_id)
        for download in downloads:
            sent = int(download.get("sent", 0))
            size = int(download.get("size", 0))
            percent = sent / size * 100 if size else 0
            progress = f"{format_bytes(sent)} / {format_bytes(size)} ({percent:.0f}%)"
            self.download_table.insert(
                "",
                "end",
                values=(
                    download.get("address", ""),
                    download.get("filename", ""),
                    progress,
                    format_rate(int(download.get("speed", 0))),
                    "下载中",
                ),
            )

    def apply_port(self) -> None:
        try:
            port = int(self.port.get())
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            messagebox.showerror("端口无效", "请输入 1 到 65535 之间的整数")
            return

        try:
            if self.change_port is not None:
                port = self.change_port(port)
        except OSError as error:
            messagebox.showerror("端口切换失败", str(error))
            return

        parsed = urlparse(self.server_url.get())
        scheme = parsed.scheme or "http"
        host = parsed.hostname or "127.0.0.1"
        self.port.set(str(port))
        self.server_url.set(f"{scheme}://{host}:{port}")
        self.update_lan_address(port)
        self.status.set(f"已切换到端口 {port}")
        self.refresh_registered()

    def on_download_limit_change(self, *_args) -> None:
        value = self.download_limit.get().strip()
        if not value:
            return
        try:
            limit_kbps = int(value)
            if limit_kbps < 0:
                raise ValueError
        except ValueError:
            return
        if limit_kbps == self._download_limit_kbps:
            return
        if self.set_download_limit is None:
            self.status.set("请通过 server.py 启动，以修改当前服务的下载限速")
            return
        try:
            limit_kbps = self.set_download_limit(limit_kbps)
        except (OSError, ValueError) as error:
            self.status.set(f"限速设置失败: {error}")
            return
        self._download_limit_kbps = limit_kbps
        self.download_limit.set(str(limit_kbps))
        text = "已取消下载限速" if limit_kbps == 0 else f"下载限速已设为 {limit_kbps} KB/s"
        self.status.set(text)

    @staticmethod
    def open_request(request: urllib.request.Request, timeout: int):
        if urlparse(request.full_url).hostname in {"127.0.0.1", "localhost", "::1"}:
            return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                request, timeout=timeout
            )
        return urllib.request.urlopen(request, timeout=timeout)

    def post_json(self, path: str, payload: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint(path),
            data=body,
            headers={"Content-Type": "application/json", **self.headers()},
            method="POST",
        )
        with self.open_request(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    def get_json(self, url: str, headers: dict[str, str] | None = None) -> dict:
        request = urllib.request.Request(url, headers=headers or {})
        with self.open_request(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def delete_url(self, url: str, headers: dict[str, str]) -> dict:
        request = urllib.request.Request(url, headers=headers, method="DELETE")
        with self.open_request(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def delete_entry(self, entry_id: str) -> None:
        endpoint = self.endpoint(f"/api/entries/{entry_id}")
        headers = self.headers()
        self.status.set("正在删除登记项目…")
        threading.Thread(
            target=self.delete_registered, args=(endpoint, headers), daemon=True
        ).start()

    def delete_registered(self, endpoint: str, headers: dict[str, str]) -> None:
        try:
            self.delete_url(endpoint, headers)
        except (OSError, ValueError, KeyError, urllib.error.HTTPError) as error:
            self.root.after(0, lambda: self.status.set("删除登记失败"))
            self.root.after(0, messagebox.showerror, "删除登记失败", str(error))
            return
        self.root.after(0, self.refresh_registered)

    def clear_registrations(self) -> None:
        if not messagebox.askyesno(
            "清空登记", "将删除全部共享登记记录，不会删除磁盘上的原文件。继续？"
        ):
            return
        endpoint = self.endpoint("/api/entries")
        headers = self.headers()
        self.status.set("正在清空登记…")
        threading.Thread(
            target=self.clear_registered, args=(endpoint, headers), daemon=True
        ).start()

    def clear_registered(self, endpoint: str, headers: dict[str, str]) -> None:
        try:
            self.delete_url(endpoint, headers)
        except (OSError, ValueError, KeyError, urllib.error.HTTPError) as error:
            self.root.after(0, lambda: self.status.set("清空登记失败"))
            self.root.after(0, messagebox.showerror, "清空登记失败", str(error))
            return
        self.root.after(0, self.refresh_registered)

    def refresh_registered(self) -> None:
        endpoint = self.endpoint("/api/files")
        self.status.set("正在读取已登记项目…")
        threading.Thread(
            target=self.load_registered, args=(endpoint,), daemon=True
        ).start()

    def load_registered(self, endpoint: str) -> None:
        try:
            data = self.get_json(endpoint)
        except (OSError, ValueError, KeyError, urllib.error.HTTPError) as error:
            self.root.after(0, self.status.set, f"读取已登记项目失败: {error}")
            return
        self.root.after(0, lambda: self.show_registered(data))

    def show_registered(self, data: dict) -> None:
        self.clear_entry_rows()
        count = 0
        for batch in data.get("batches", []):
            for item in batch.get("files", []):
                self.add_entry_row(item, item.get("id"))
                count += 1
        if not count:
            self.add_message_row("暂无已登记项目，可以拖入文件或文件夹")
        self._registered_count = count
        self.status.set(f"已加载 {count} 个登记项目")

    def test_connection(self) -> None:
        try:
            request = urllib.request.Request(self.endpoint("/api/health"))
            with self.open_request(request, timeout=3) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")
            self.status.set("服务器连接正常")
        except (OSError, ValueError, RuntimeError) as error:
            self.status.set("连接失败")
            messagebox.showerror("连接失败", str(error))

    def on_drop(self, event) -> None:
        paths = list(self.root.tk.splitlist(event.data))
        files = collect_files(paths)
        if not files:
            self.status.set("没有找到可登记的文件")
            return
        self.status.set(f"正在登记 {len(files)} 个项目…")
        threading.Thread(target=self.register_files, args=(paths, files), daemon=True).start()

    def register_files(self, dropped_paths: list[str], files: list[dict]) -> None:
        names = [Path(path).name for path in dropped_paths if Path(path).name]
        batch_name = names[0] if len(names) == 1 else f"{len(names)} 个拖拽项目"
        try:
            self.post_json(
                "/api/register",
                {"batch_name": batch_name, "files": files},
            )
        except (OSError, ValueError, KeyError, urllib.error.HTTPError) as error:
            self.root.after(0, lambda: self.status.set("登记失败"))
            self.root.after(0, messagebox.showerror, "登记失败", str(error))
            return
        self.root.after(0, self.refresh_registered)


if __name__ == "__main__":
    app = TkinterDnD.Tk()
    UploadClient(app)
    app.mainloop()
