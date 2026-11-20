const batchesElement = document.querySelector("#batches");
const emptyElement = document.querySelector("#empty");
const statusElement = document.querySelector("#status");
const refreshButton = document.querySelector("#refresh");
let loading = false;
const zipStatus = document.querySelector("#zip-status");
let activeZip = null;
let zipFrame = null;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function copyAddress(address) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(address);
    return;
  }
  // HTTP 分享页面无法使用 Clipboard API，保留传统复制方式。
  const input = element("textarea");
  input.value = address;
  input.style.cssText = "position:fixed;left:-9999px;top:0";
  const focused = document.activeElement;
  document.body.append(input);
  try {
    input.select();
    if (!document.execCommand("copy")) throw new Error("浏览器未允许复制");
  } finally {
    input.remove();
    if (focused) focused.focus();
  }
}

function updateZipButton(button) {
  const pending = button.dataset.url === activeZip;
  button.disabled = activeZip !== null;
  button.textContent = pending ? "正在压缩…" : "压缩下载";
  button.setAttribute("aria-label", `${button.textContent} ${button.dataset.name}`);
}

function startZip(button) {
  if (activeZip !== null) return;
  const url = new URL(button.dataset.url, window.location.href);
  const ticket = Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, "0")).join("");
  const cookie = `hfs_zip_${ticket}`;
  url.searchParams.set("ticket", ticket);
  activeZip = button.dataset.url;
  zipStatus.textContent = `正在压缩：${button.dataset.name}`;
  batchesElement.querySelectorAll(".zip-download").forEach(updateZipButton);

  if (zipFrame) zipFrame.remove();
  const frame = zipFrame = element("iframe");
  frame.hidden = true;
  frame.title = "压缩下载";
  frame.setAttribute("sandbox", "allow-downloads allow-same-origin");
  function finish(message, keepFrame = false) {
    clearInterval(poll);
    clearTimeout(timeout);
    if (!keepFrame) frame.remove();
    activeZip = null;
    zipStatus.textContent = message;
    batchesElement.querySelectorAll(".zip-download").forEach(updateZipButton);
  }

  frame.addEventListener("load", () => {
    let message = "压缩下载失败，请重试";
    try {
      const error = JSON.parse(frame.contentDocument.body.textContent).error;
      if (error) message = `压缩下载失败：${error}`;
    } catch {
      // 网络错误页面不一定可读，也不一定返回 JSON。
    }
    finish(message);
  }, { once: true });
  // 附件响应不会触发 iframe load；用短期 Cookie 确认下载已交给浏览器。
  const poll = setInterval(() => {
    if (!document.cookie.split(/;\s*/).includes(`${cookie}=1`)) return;
    document.cookie = `${cookie}=; Path=/; Max-Age=0`;
    finish("压缩完成，已交给浏览器下载", true);
  }, 250);
  const timeout = setTimeout(() => finish("未确认下载状态，请查看浏览器下载列表后重试"), 30 * 60 * 1000);
  frame.src = url.href;
  document.body.append(frame);
}

function itemLink(item) {
  const isDirectory = item.kind === "directory";
  const row = element("div", "file-row");
  const link = element("a", "file-link");
  link.href = item.url || (isDirectory ? item.browse_url : item.download_url);
  if (!isDirectory) link.setAttribute("download", "");
  const icon = element("span", "file-icon", isDirectory ? "📁" : "📄");
  const label = element("span", "file-name", item.path || item.name);
  const kind = element("span", "file-kind", isDirectory ? "目录" : "文件");
  const info = element("div", "file-info");
  info.append(icon, label, kind);
  link.append(info);
  const copy = element("button", "button copy-address", "复制地址");
  copy.type = "button";
  copy.setAttribute("aria-label", `复制 ${item.path || item.name} 的地址`);
  copy.addEventListener("click", async () => {
    try {
      await copyAddress(link.href);
      statusElement.textContent = "地址已复制";
    } catch (error) {
      statusElement.textContent = `复制失败：${error.message}`;
    }
  });
  const actions = element("div", "file-actions");
  actions.append(copy);
  if (isDirectory && item.zip_url) {
    const zip = element("button", "button zip-download");
    zip.type = "button";
    zip.dataset.url = item.zip_url;
    zip.dataset.name = item.path || item.name;
    updateZipButton(zip);
    zip.addEventListener("click", () => startZip(zip));
    actions.append(zip);
  }
  row.append(link, actions);
  return row;
}

function renderRows(items) {
  const list = element("div", "file-list");
  for (const item of items) list.append(itemLink(item));
  return list;
}

function renderHome(batches) {
  const items = batches.flatMap((batch) => batch.files);
  batchesElement.replaceChildren();
  emptyElement.hidden = items.length !== 0;
  if (items.length) batchesElement.append(renderRows(items));
  return items.length;
}

function currentDirectory() {
  return window.location.pathname.replace(/^\/+/, "");
}

function renderDirectory(directory) {
  batchesElement.replaceChildren();
  emptyElement.hidden = directory.entries.length !== 0;
  const header = element("div", "directory-header");
  header.append(element("strong", "directory-name", directory.name));
  const actions = element("div", "directory-actions");
  const home = element("a", "back-link", "主页");
  home.href = "/";
  const back = element("a", "back-link", "返回");
  back.href = directory.parent_url;
  actions.append(home, back);
  header.append(actions);
  batchesElement.append(header);
  if (directory.entries.length) batchesElement.append(renderRows(directory.entries));
}

async function loadFiles() {
  if (loading) return;
  loading = true;
  refreshButton.disabled = true;
  statusElement.textContent = "正在读取…";
  try {
    const directoryId = currentDirectory();
    const endpoint = directoryId
      ? `/api/public-directory?path=${encodeURIComponent(directoryId)}`
      : "/api/files";
    const response = await fetch(endpoint, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (directoryId) {
      renderDirectory(data);
      statusElement.textContent = `${data.entries.length} 个项目`;
    } else {
      statusElement.textContent = `${renderHome(data.batches || [])} 个共享项目`;
    }
  } catch (error) {
    statusElement.textContent = `读取失败：${error.message}`;
  } finally {
    loading = false;
    refreshButton.disabled = false;
  }
}

refreshButton.addEventListener("click", loadFiles);
loadFiles();
setInterval(loadFiles, 2000);
