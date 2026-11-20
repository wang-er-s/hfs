# HFS 局域网文件

这是一个“引用本地文件”的局域网文件服务器：Tkinter 客户端登记任意位置的文件或文件夹，而不复制内容。单个文件直接提供下载；文件夹只登记根路径，网页在用户进入某个目录时读取该目录当前一层内容。

## 运行程序

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python server.py
```

`server.py` 会同时启动局域网网页服务和 Tkinter 拖拽窗口。

如果当前 Python 报 `No module named '_tkinter'`，改用 macOS 自带的 Python：

```bash
/usr/bin/python3 -m pip install --user tkinterdnd2
/usr/bin/python3 server.py
```

服务器默认监听 `0.0.0.0:8765`。在服务器电脑上查看局域网 IP，然后让其他设备访问：

```text
http://服务器局域网IP:8765
```

例如：`http://192.168.1.20:8765`。

把任意位置的文件或文件夹拖进启动后出现的窗口。单个文件会直接登记；文件夹只会登记其根路径，不会递归扫描。网页打开目录时才读取该目录当前一层的文件和子目录。网页文件列表会自动定时刷新，拖入新项目后无需手动刷新浏览器。

根级登记项可通过其名称直接访问。例如，登记 `CDN` 文件夹后可访问 `http://服务器IP:8765/CDN`；登记 `report.pdf` 后访问 `http://服务器IP:8765/report.pdf` 会直接下载。目录内路径也沿用文件夹层级，例如 `/CDN/assets/logo.png`。名称包含空格或特殊字符时，浏览器会自动使用 URL 编码。

客户端会展示所有已登记项目。每一项右侧的“删除”按钮可取消该项共享；“清空登记”会删除全部登记记录。这两种操作都不会删除磁盘上的原文件或文件夹。

视频支持 HTTP Range 分段读取。每个传输的读取缓冲区最多 64 KB；单次网络读写阻塞超过 30 秒会关闭连接并释放线程、文件句柄和下载记录。这个超时不限制视频总播放时长；长时间暂停导致连接关闭后，播放器需要重新请求剩余片段。

## 桌面控制台

桌面窗口按运行状态组织：顶部工具栏可设置端口和下载限速，浏览器地址支持复制或直接打开；速率图显示最近的总下载速率。主区域左侧是可拖拽的共享项目列表，右侧显示最近访问和下载事件；底部表格显示进行中的下载 IP、文件名、进度与速度。

访问日志、下载任务和累计输出量只保存在内存中，服务重启后清空。状态接口仅允许服务器本机访问；配置了 `HFS_REGISTER_TOKEN` 时，客户端会携带该令牌访问。

## 工作限制

- 原文件或共享目录必须持续存在；移动或删除后相应链接会失效。
- 目录内容在打开或刷新该目录时动态读取；新增和删除的项目会反映出来，但不会递归预加载。
- 服务器电脑必须保持开机并运行 `server.py`。
- 网页没有上传入口，上传只通过本机 Tkinter 客户端登记。
- 网页目录行右侧可点“压缩下载”，递归打包成 ZIP，保留空目录，跳过符号链接及特殊文件。ZIP 按需生成并在传输结束后删除；一次处理一个压缩下载，大目录会占用临时磁盘空间。单个文件仍从原路径直接下载。
- `tkinterdnd2` 需要系统提供 Tk。macOS 通常可直接使用 Python.org 的 Python；Linux 可能需要安装 `python3-tk`。

## 代码结构

- `server.py`：HTTP 路由、SQLite 登记数据和应用启动。默认登记数据库为项目目录下的 `hfs.sqlite3`，从其他工作目录启动时也会使用同一文件。
- `hfs_runtime.py`：带类型数据模型的访问事件、活动下载与状态快照。
- `hfs_settings.py`：端口与限速的验证、持久化和下载节流。
- `client.py`：Tkinter 管理控制台与本机登记客户端。

## 可选配置

```bash
python server.py --port 8875
```

`--port` 适用于桌面和 `--headless` 模式，接受 `1` 到 `65535` 的整数，只影响本次启动。端口优先级：`--port`、`HFS_PORT` 环境变量、`hfs-settings.json` 保存值、默认 `8765`。可用 `HFS_HOST` 环境变量指定监听地址。

启动后的客户端可在“监听端口”中输入新端口并点击“应用端口”。服务会立即切换到新端口，并把选择保存到 `hfs-settings.json`，供下次启动使用。

客户端的“下载限速”以每个下载连接的 `KB/s` 上限工作。输入 `0` 会立即取消限速；输入其他非负整数会立即按该值限速，无需点击应用按钮。设置保存到 `hfs-settings.json`；可用 `HFS_DOWNLOAD_LIMIT_KBPS` 环境变量在启动时覆盖该设置。

如果希望给本机登记接口加一个共享令牌，在启动前设置环境变量：

```bash
export HFS_REGISTER_TOKEN='replace-with-a-local-secret'
```


## 与 FRPC 一起启动

运行 `python server.py` 会自动查找 PATH 中的 `frpc`（或 `/opt/homebrew/bin/frpc`），使用 `/opt/homebrew/etc/frp/frpc.toml` 启动隧道。连接日志输出到当前终端；重试行为遵循 FRPC 配置（需要初次连接失败也重试时，在 TOML 顶层设置 `loginFailExit = false`）；本地 HFS 可继续使用。关闭 HFS 窗口会停止本次启动的 FRPC。

若已有 FRPC 进程，HFS 不重复启动，也不停止外部进程。需要统一启停时，先退出手动运行的 FRPC；若启用了 Homebrew 服务，先执行 `brew services stop frpc`。

设置 `HFS_FRPC=0` 可禁用自动启动；`HFS_FRPC_CONFIG` 可指定配置路径。FRPC 的 `localPort` 必须与 HFS 端口一致，修改端口后需同步配置并重启。

## 无头模式

不打开桌面窗口，只运行文件服务和 FRPC：

```bash
python3 server.py --headless
```

无头模式不需要 Tk 或 tkinterdnd2，继续使用 `hfs.sqlite3` 中已登记的共享文件及目录。首次可通过 GUI 登记文件后退出，再切换到无头模式。按 `Ctrl+C` 或发送 `SIGTERM` 会停止 HTTP 服务和本次启动的 FRPC；已有外部 FRPC 不受影响。

只运行 HFS、不启动 FRPC：

```bash
HFS_FRPC=0 python3 server.py --headless
```

无头模式仍在前台运行，并非自动后台化；终端日志继续只显示最近 20 行，重定向输出时保留完整日志。可用 `python3 server.py --help` 查看参数。

网页每两秒请求 `/api/files` 刷新列表，其成功响应不再打印日志；错误及文件传输日志保留。

## 回归检查

运行 `python3 -W error::ResourceWarning -m unittest -v test_server test_headless`，检查数据库、视频 Range 传输、断连和超时资源释放，以及无 Tk 依赖启动、SIGINT/SIGTERM 正常退出。测试使用临时数据库及随机本机端口，不启动实际 FRPC 隧道。
