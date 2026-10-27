"""Optional FRPC companion process for the desktop HFS server."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path


def start_frpc() -> subprocess.Popen | None:
    if os.environ.get("HFS_FRPC", "1") == "0":
        return None
    binary = shutil.which("frpc")
    if binary is None and Path("/opt/homebrew/bin/frpc").is_file():
        binary = "/opt/homebrew/bin/frpc"
    config = Path(os.environ.get("HFS_FRPC_CONFIG", "/opt/homebrew/etc/frp/frpc.toml")).expanduser()
    if binary is None or not config.is_file():
        print("FRPC 未启动：未找到程序或配置，本地 HFS 继续运行。")
        return None
    # Do not create a competing proxy or take ownership of an external service.
    try:
        existing = subprocess.run(["pgrep", "-x", "frpc"], capture_output=True, check=False)
        if existing.returncode == 0:
            print("检测到已有 FRPC，保留现有进程，本次不重复启动；请确认它使用正确配置。")
            return None
        process = subprocess.Popen([binary, "-c", str(config)])
    except OSError as error:
        print(f"FRPC 启动失败：{error}；本地 HFS 继续运行。")
        return None
    print(f"已启动 FRPC（PID {process.pid}），连接日志显示在当前终端。")

    def report_exit() -> None:
        code = process.wait()
        if code:
            print(f"FRPC 已退出（状态 {code}），请检查连接日志；本地 HFS 仍可使用。")

    threading.Thread(target=report_exit, daemon=True).start()
    return process


def stop_frpc(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
