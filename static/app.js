"use strict";

const $ = (id) => document.getElementById(id);
const state = { identity: null, results: new Map(), scope: null, busy: false, sequence: 0,
  checked: 0, total: 0, done: false, filter: "全部", expanded: false, uncertainExpanded: false,
  downloading: false, download: null, controller: null, downloadEvents: null };
const groups = { PDF: ["pdf"], 表格: ["xls", "xlsx", "csv"], 文档: ["doc", "docx", "ppt", "pptx"], 图片: ["jpg", "jpeg", "tif", "tiff", "png", "gif"], 视频: ["mp4", "mov"] };
const defaultTypes = ["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "csv", "zip", "mp4", "mov", "jpg", "tif", "png", "gif"];
const types = [...defaultTypes, "jpeg", "tiff"];
const numberOf = (row) => Number(row.key.split(".")[0]);
const extensionOf = (row) => row.key.split(".")[1];
const groupOf = (row) => Object.keys(groups).find((key) => groups[key].includes(extensionOf(row))) || "其他";
const formatBytes = (value) => value >= 1073741824 ? `${(value / 1073741824).toFixed(2)} GB` : value >= 1048576 ? `${(value / 1048576).toFixed(1)} MB` : value > 0 ? `${Math.ceil(value / 1024)} KB` : "";

function element(tag, className = "", text = "") {
  const node = document.createElement(tag); node.className = className; node.textContent = text; return node;
}
function icon(name, small = false) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", `icon${small ? " small" : ""}`); svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(svg.namespaceURI, "use"); use.setAttribute("href", `#${name}`); svg.append(use); return svg;
}
function safeLink(url, text, className = "") {
  const link = element("a", className, text); link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer"; return link;
}
function preferences() {
  return { maximum: Number($("maximum").value), concurrency: Number($("concurrency").value),
    extensions: [...$("extensions").querySelectorAll("input:checked")].map((input) => input.value) };
}
function updateSettings() {
  const value = preferences(); $("maximum-label").textContent = value.maximum || "—";
  $("candidate-count").textContent = `共 ${value.maximum * value.extensions.length || 0} 个候选`;
  try { localStorage.setItem("supplement-local-settings", JSON.stringify(value)); } catch { /* Private windows can disable storage. */ }
}
function initialize() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem("supplement-local-settings") || "{}"); } catch { /* Use built-in defaults. */ }
  if (!saved || typeof saved !== "object") saved = {};
  $("maximum").value = Number.isInteger(saved.maximum) && saved.maximum >= 1 && saved.maximum <= 100 ? saved.maximum : 11;
  $("concurrency").value = Number.isInteger(saved.concurrency) && saved.concurrency >= 1 && saved.concurrency <= 88 ? saved.concurrency : 32;
  const selected = Array.isArray(saved.extensions) ? saved.extensions.filter((ext) => types.includes(ext)) : defaultTypes;
  for (const type of types) {
    const label = element("label"), input = document.createElement("input"); input.type = "checkbox"; input.value = type; input.checked = selected.includes(type);
    label.append(input, document.createTextNode(type.toUpperCase())); $("extensions").append(label);
  }
  $("query").value = "";
  $("settings-fields").addEventListener("change", updateSettings); $("maximum").addEventListener("input", updateSettings); updateSettings();
  try { if (localStorage.getItem("supplement-local-theme") === "dark") document.documentElement.dataset.theme = "dark"; } catch { /* Keep light theme. */ }
  $("theme").addEventListener("click", () => {
    const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark"; document.documentElement.dataset.theme = theme;
    try { localStorage.setItem("supplement-local-theme", theme); } catch { /* Theme still works without persistence. */ }
  });
}

async function request(path, body) {
  const response = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Local-Request": "1" }, body: JSON.stringify(body) });
  const data = await response.json(); if (!response.ok) throw Error(data.error || "操作未完成，请重试"); return data;
}

async function readEvents(response, onEvent) {
  if (!response.ok) { const data = await response.json(); throw Error(data.error || "查询未完成"); }
  if (!response.headers.get("Content-Type")?.includes("text/event-stream")) throw Error("查询响应无效，请重试");
  const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = "";
  try {
    for (;;) {
      const { value, done } = await reader.read(); buffer += decoder.decode(value, { stream: !done });
      let index;
      while ((index = buffer.indexOf("\n\n")) !== -1) {
        const block = buffer.slice(0, index); buffer = buffer.slice(index + 2);
        const name = block.split("\n").find((line) => line.startsWith("event: "))?.slice(7);
        const data = block.split("\n").filter((line) => line.startsWith("data: ")).map((line) => line.slice(6)).join("\n");
        if (name && data) onEvent(name, JSON.parse(data));
      }
      if (done) break;
    }
  } finally { reader.releaseLock(); }
}

function error(message = "") { $("error").textContent = message; $("error").hidden = !message; }
function busy(value) {
  state.busy = value; $("submit").disabled = value; $("submit").querySelector("span").textContent = value ? "查找中" : "查找";
  $("settings-fields").disabled = value; $("search-status").hidden = !value; $("recheck").disabled = value;
  $("continue").hidden = value || state.done || !state.identity?.pii;
}
function renderIdentity() {
  const identity = state.identity; $("identity").hidden = !identity?.pii;
  if (!identity?.pii) return;
  $("article-title").textContent = identity.title || "ScienceDirect 文献"; $("article-link").href = `https://www.sciencedirect.com/science/article/pii/${identity.pii}`;
  $("identifiers").replaceChildren();
  for (const [label, value] of [["DOI", identity.doi], ["PII", identity.pii]]) {
    if (!value) continue;
    const item = element("span", "identifier"); item.append(document.createTextNode(label), element("code", "", value)); $("identifiers").append(item);
  }
}
function rowsWith(status) {
  return [...state.results.values()].filter((row) => row.status === status && state.scope && numberOf(row) <= state.scope.maximum && state.scope.extensions.includes(extensionOf(row)))
    .sort((a, b) => numberOf(a) - numberOf(b) || types.indexOf(extensionOf(a)) - types.indexOf(extensionOf(b)));
}
function renderResults() {
  $("materials").hidden = !state.identity?.pii; if (!state.identity?.pii) return;
  const found = rowsWith("found"), uncertain = rowsWith("uncertain");
  $("found-count").textContent = `${found.length} 个文件`;
  const cached = [...state.results.values()].filter((row) => row.cached && numberOf(row) <= state.scope.maximum && state.scope.extensions.includes(extensionOf(row))).length;
  $("progress-label").textContent = `${state.busy ? "正在检查" : state.done ? "检查完成" : "已停止"} ${state.checked} / ${state.total}${cached ? ` · 复用 ${cached} 项` : ""}`;
  $("check-progress").value = state.total ? state.checked / state.total * 100 : 0;
  $("archive").hidden = found.length < 2 || found.length > 100; $("archive").disabled = state.downloading;
  $("archive").querySelector("span").textContent = `一键打包 ${found.length} 份`;
  $("continue").hidden = state.busy || state.done;
  const counts = new Map(); found.forEach((row) => counts.set(groupOf(row), (counts.get(groupOf(row)) || 0) + 1));
  if (!counts.has(state.filter)) state.filter = "全部";
  $("filters").hidden = found.length <= 12; $("filters").replaceChildren();
  if (found.length > 12) for (const label of ["全部", ...Object.keys(groups), "其他"]) {
    if (label !== "全部" && !counts.has(label)) continue;
    const button = element("button", "filter-button", `${label} ${label === "全部" ? found.length : counts.get(label)}`);
    button.type = "button"; button.setAttribute("aria-pressed", String(state.filter === label));
    button.addEventListener("click", () => { state.filter = label; state.expanded = false; renderResults(); }); $("filters").append(button);
  }
  const filtered = state.filter === "全部" ? found : found.filter((row) => groupOf(row) === state.filter);
  const visible = state.expanded ? filtered : filtered.slice(0, 6);
  const existing = new Map([...$("file-list").children].map((node) => [node.dataset.filename, node]));
  const wanted = new Set(visible.map((row) => row.filename));
  for (const [name, node] of existing) if (!wanted.has(name)) node.remove();
  for (const [index, row] of visible.entries()) {
    let li = existing.get(row.filename);
    if (li) {
      li.querySelector("button").disabled = state.downloading; li.querySelector(".file-size").textContent = formatBytes(row.size);
      if ($("file-list").children[index] !== li) $("file-list").insertBefore(li, $("file-list").children[index] || null);
      continue;
    }
    li = element("li", "file-row"); li.dataset.filename = row.filename;
    const link = safeLink(row.url, "", "file-link"); link.title = row.filename;
    const illustration = element("span", "file-icon"); illustration.append(icon("file"));
    link.append(illustration, element("span", "file-name", `材料 ${numberOf(row)}`), element("span", "file-format", extensionOf(row).toUpperCase()), icon("external", true));
    const button = element("button", "button compact"); button.type = "button"; button.disabled = state.downloading; button.setAttribute("aria-label", `下载材料 ${numberOf(row)} ${extensionOf(row).toUpperCase()}`);
    button.append(icon("download", true), document.createTextNode("下载")); button.addEventListener("click", () => startDownload([row.key]));
    li.append(link, element("span", "file-size", formatBytes(row.size)), button); $("file-list").insertBefore(li, $("file-list").children[index] || null);
  }
  $("expand").hidden = filtered.length <= 6; $("expand").textContent = state.expanded ? "收起材料列表" : `展开其余 ${filtered.length - 6} 份`;
  $("expand").setAttribute("aria-expanded", String(state.expanded));
  $("empty").hidden = found.length > 0; $("empty").textContent = state.busy ? "正在寻找补充材料，找到后会陆续显示。" : uncertain.length ? "暂未确认可用材料，可展开下方候选链接或重新检查。" : "当前编号和格式范围内未找到材料，可扩大检索范围后再试。";
  $("uncertain").hidden = !uncertain.length; $("uncertain-count").textContent = `${uncertain.length} 项`;
  $("uncertain-list").replaceChildren();
  for (const row of (state.uncertainExpanded ? uncertain : uncertain.slice(0, 12))) {
    const li = element("li"); li.append(safeLink(row.url, `材料 ${numberOf(row)} · ${extensionOf(row).toUpperCase()}`), element("span", "", row.reason || "网络未确认")); $("uncertain-list").append(li);
  }
  $("expand-uncertain").hidden = uncertain.length <= 12; $("expand-uncertain").textContent = state.uncertainExpanded ? "收起候选链接" : `显示其余 ${uncertain.length - 12} 个候选`;
}

async function startSearch(keep = false, refresh = false) {
  const query = keep ? state.identity?.pii : $("query").value.trim(); if (!query || state.busy) return;
  const settings = preferences();
  if (!Number.isInteger(settings.maximum) || settings.maximum < 1 || settings.maximum > 100 || !Number.isInteger(settings.concurrency) || settings.concurrency < 1 || settings.concurrency > 88 || !settings.extensions.length) { error("请检查附件编号、并发，并至少选择一种文件类型。"); return; }
  state.controller?.abort(); state.controller = new AbortController(); const sequence = ++state.sequence;
  error(); state.scope = settings; state.checked = 0; state.total = settings.maximum * settings.extensions.length; state.done = false;
  if (!keep) { state.identity = null; state.results.clear(); state.filter = "全部"; state.expanded = false; state.uncertainExpanded = false; }
  renderIdentity(); busy(true); renderResults(); $("stage").textContent = "正在识别文献…";
  let ended = false;
  try {
    const response = await fetch("/api/search", { method: "POST", headers: { "Content-Type": "application/json", "X-Local-Request": "1" }, body: JSON.stringify({ query, ...settings, refresh }), signal: state.controller.signal });
    await readEvents(response, (name, data) => {
      if (sequence !== state.sequence) return;
      if (name === "stage") $("stage").textContent = data.message;
      if (name === "identity") {
        state.identity = keep && state.identity?.pii === data.pii ? { ...data, title: state.identity.title, doi: state.identity.doi } : data;
        renderIdentity(); if (!data.pii) error(data.message || "未找到对应文献，请补充 DOI、ScienceDirect 链接或 PII。");
      }
      if (name === "progress" || name === "batch") {
        for (const row of data.results || []) state.results.set(row.key, row);
        state.checked = data.checked; state.total = data.total; $("stage").textContent = `正在检查材料 ${state.checked} / ${state.total}`;
        scheduleResults();
      }
      if (name === "done") { state.checked = data.checked; state.total = data.total; state.done = true; ended = true; }
      if (name === "error") { ended = true; error(data.message); }
    });
    if (!ended) error("检查连接已中断，找到的材料仍保留，可继续检查。");
  } catch (failure) { if (sequence === state.sequence && failure.name !== "AbortError") error(failure.message || "查询未完成，请重试"); }
  finally { if (sequence === state.sequence) { busy(false); renderResults(); } }
}
let renderTimer = null;
function scheduleResults() { if (renderTimer === null) renderTimer = setTimeout(() => { renderTimer = null; renderResults(); }, 120); }

async function startDownload(keys, reuse = null) {
  if (state.downloading) return;
  const body = reuse || { pii: state.identity.pii, files: keys }; state.download = body; state.downloadTask = null; state.downloading = true; renderResults();
  $("download-panel").hidden = false; $("download-status").textContent = "准备下载…"; $("download-metrics").textContent = "";
  $("download-error").hidden = true; $("save-again").hidden = true; $("retry-download").hidden = true; $("cancel-download").hidden = true;
  $("download-progress").removeAttribute("value");
  try {
    const task = await request("/api/downloads", body); watchDownload(task.id);
  } catch (failure) {
    state.downloading = false; $("cancel-download").hidden = true; $("retry-download").hidden = false;
    $("download-status").textContent = "下载未开始"; $("download-error").textContent = failure.message; $("download-error").hidden = false; renderResults();
  }
}

function watchDownload(taskId) {
  state.downloadEvents?.close(); state.downloading = true; renderResults();
  $("download-error").hidden = true; $("cancel-download").hidden = false;
  const events = new EventSource(`/api/downloads/${taskId}/events`); state.downloadEvents = events; state.downloadTask = taskId;
    events.addEventListener("progress", (event) => {
      const value = JSON.parse(event.data), labels = { queued: "等待下载", downloading: "正在下载", packing: "正在本地打包", complete: "已准备好", error: "下载未完成", cancelled: "已取消下载" };
      $("download-status").textContent = `${labels[value.status]} · ${value.completed} / ${value.count} 份`;
      $("download-metrics").textContent = `${formatBytes(value.bytes)}${value.total ? ` / ${formatBytes(value.total)}` : ""}${value.status === "downloading" ? ` · ${formatBytes(value.speed) || "0 KB"}/s` : ""}`;
      if (value.rows.every((row) => row.total > 0)) $("download-progress").value = Math.min(100, value.bytes / value.total * 100);
      if (["complete", "error", "cancelled"].includes(value.status)) {
        events.close(); state.downloading = false; $("cancel-download").hidden = true; renderResults();
        if (value.status === "complete") {
          $("download-progress").value = 100; $("save-again").href = value.url; $("save-again").hidden = false;
          const link = element("a"); link.href = value.url; link.download = ""; document.body.append(link); link.click(); link.remove();
        } else { $("download-progress").value = 0; $("retry-download").hidden = false; $("download-error").textContent = value.error; $("download-error").hidden = !value.error; }
      }
    });
    events.onerror = () => {
      events.close(); state.downloading = false; $("cancel-download").hidden = true;
      $("download-error").hidden = false; $("download-error").textContent = "下载进度连接已中断，可点击重新连接查看当前任务。";
      // Reconnect to the same task rather than start a duplicate download.
      $("retry-download").hidden = true; $("download-status").textContent = "连接已中断";
      const reconnect = element("button", "text-button", "重新连接"); reconnect.type = "button";
      reconnect.addEventListener("click", () => watchDownload(taskId));
      $("download-bottom-placeholder")?.remove(); reconnect.id = "download-bottom-placeholder"; $("download-error").append(" ", reconnect); renderResults();
    };
}

initialize();
let composing = false;
$("query").addEventListener("compositionstart", () => { composing = true; });
$("query").addEventListener("compositionend", () => { composing = false; });
$("query").addEventListener("keydown", (event) => { if (event.key === "Enter" && (event.isComposing || composing)) event.preventDefault(); });
$("query-form").addEventListener("submit", (event) => { event.preventDefault(); if (!composing) void startSearch(); });
$("pause").addEventListener("click", () => state.controller?.abort());
$("continue").addEventListener("click", () => startSearch(true)); $("recheck").addEventListener("click", () => startSearch(true, true));
$("expand").addEventListener("click", () => { state.expanded = !state.expanded; renderResults(); });
$("expand-uncertain").addEventListener("click", () => { state.uncertainExpanded = !state.uncertainExpanded; renderResults(); });
$("archive").addEventListener("click", () => startDownload(rowsWith("found").map((row) => row.key)));
$("retry-download").addEventListener("click", () => startDownload([], state.download));
$("cancel-download").addEventListener("click", async () => {
  try { await request(`/api/downloads/${state.downloadTask}/cancel`, {}); $("download-status").textContent = "正在取消…"; $("cancel-download").hidden = true; }
  catch (failure) { $("download-error").textContent = failure.message; $("download-error").hidden = false; }
});
document.addEventListener("click", (event) => { document.querySelectorAll(".nav-menu[open]").forEach((menu) => { if (!menu.contains(event.target)) menu.open = false; }); });
document.addEventListener("keydown", (event) => { if (event.key === "Escape") document.querySelectorAll(".nav-menu[open]").forEach((menu) => { menu.open = false; menu.querySelector("summary").focus(); }); });
