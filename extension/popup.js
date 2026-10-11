// Toolbar popup: lists every media stream found on the active tab and lets you download
// the whole page or one specific stream, at a chosen preset.

const $ = (id) => document.getElementById(id);
let tabId = null;
let pageUrl = "";

function tag(kind) {
  const el = document.createElement("span");
  el.className = "tag " + kind;
  el.textContent = kind;
  return el;
}

function humanSize(n) {
  if (!n) return "";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(1) : n.toFixed(0)) + u[i];
}

function render(items) {
  const list = $("list");
  list.innerHTML = "";
  if (!items.length) {
    list.innerHTML = '<li class="empty">No separate streams detected yet.<br>Use “Download this page”.</li>';
    return;
  }
  // HLS master playlists first, then by size.
  items.sort((a, b) => {
    const rank = (x) => (x.kind === "hls" ? 3 : x.kind === "dash" ? 2 : 1);
    return rank(b) - rank(a) || (b.size || 0) - (a.size || 0);
  });
  for (const it of items) {
    const li = document.createElement("li");
    li.appendChild(tag(it.kind));
    const meta = document.createElement("div");
    meta.className = "meta";
    const u = document.createElement("div");
    u.className = "u";
    u.textContent = (humanSize(it.size) ? humanSize(it.size) + " · " : "") + prettyUrl(it.url);
    u.title = it.url;
    meta.appendChild(u);
    li.appendChild(meta);
    const dl = document.createElement("button");
    dl.className = "dl";
    dl.textContent = "Get";
    dl.addEventListener("click", () => {
      // A plain file the browser can save itself; a stream goes through siphon.
      if (it.kind === "file") download({ mode: "browser", url: it.url, filename: fileName(it.url), page: pageUrl });
      else download({ mode: "siphon", page: pageUrl, media: it.url, kind: it.kind });
    });
    li.appendChild(dl);
    list.appendChild(li);
  }
}

function prettyUrl(url) {
  try {
    const u = new URL(url);
    const tail = u.pathname.split("/").filter(Boolean).pop() || u.hostname;
    return decodeURIComponent(tail).slice(0, 48);
  } catch { return url.slice(0, 48); }
}

let popupJob = 0;
function fileName(url) {
  try { return decodeURIComponent(new URL(url).pathname.split("/").filter(Boolean).pop() || ""); } catch { return ""; }
}
function download(req) {
  req.preset = $("preset").value;
  const jobId = "p" + Date.now() + "-" + popupJob++;
  setStatus("starting…");
  chrome.runtime.sendMessage({ type: "download", tabId, jobId, ...req }, (res) => {
    if (chrome.runtime.lastError || !res?.started) {
      setStatus("could not start: " + (chrome.runtime.lastError?.message || "native host?"));
    }
  });
}

function setStatus(t) { $("status").textContent = t; $("status").classList.remove("saved"); }

function setSaved(file) {
  const s = $("status");
  s.classList.add("saved");
  s.innerHTML = "";
  const txt = document.createElement("span");
  txt.textContent = "✓ saved: " + (file || "");
  txt.title = file || "";
  const copy = document.createElement("button");
  copy.className = "copy-btn";
  copy.title = "copy path";
  copy.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
  copy.addEventListener("click", () => {
    navigator.clipboard.writeText(file || "").then(() => { copy.textContent = "✓"; setTimeout(() => (copy.innerHTML = copy.dataset.svg), 1200); }, () => {});
  });
  copy.dataset.svg = copy.innerHTML;
  s.append(txt, copy);
}

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type !== "progress") return;
  if (msg.event === "resolving") setStatus(msg.message || "finding the stream…");
  else if (msg.event === "progress" && msg.percent != null) setStatus("downloading " + Math.round(msg.percent) + "%" + (msg.speed ? " · " + msg.speed : ""));
  else if (msg.event === "done") setSaved(msg.files && msg.files.length > 1 ? `${msg.files.length} files · ${msg.file}` : msg.file);
  else if (msg.event === "error") setStatus("✗ " + (msg.message || "failed"));
});

async function init() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  tabId = tab.id;
  pageUrl = tab.url;
  $("grab").addEventListener("click", () => download({ mode: "siphon", page: pageUrl }));

  chrome.runtime.sendMessage({ type: "ping" }, (res) => {
    const host = $("host");
    if (res?.ok) { host.textContent = "host v" + (res.version || "?"); host.className = "host ok"; }
    else { host.textContent = "host missing"; host.className = "host bad"; $("install").style.display = "block"; }
  });

  chrome.runtime.sendMessage({ type: "list", tabId }, (res) => render(res?.items || []));
  initTarget();
}

// Where downloads go: this computer (the native host) or your siphon box (siphon serve).
async function initTarget() {
  const s = await chrome.storage.local.get(["target", "boxUrl", "boxToken"]);
  const target = s.target === "box" ? "box" : "local";
  document.querySelector(`input[name=target][value=${target}]`).checked = true;
  $("boxset").hidden = target !== "box";
  $("boxUrl").value = s.boxUrl || "";
  $("boxToken").value = s.boxToken || "";
  document.querySelectorAll("input[name=target]").forEach((r) => r.addEventListener("change", () => {
    const t = document.querySelector("input[name=target]:checked").value;
    $("boxset").hidden = t !== "box";
    chrome.storage.local.set({ target: t });
  }));
  for (const id of ["boxUrl", "boxToken"]) {
    $(id).addEventListener("change", () => chrome.storage.local.set({ [id]: $(id).value.trim() }));
  }
  $("boxTest").addEventListener("click", () => {
    const res = $("boxResult");
    res.textContent = "checking…"; res.className = "result";
    chrome.runtime.sendMessage({ type: "boxTest", boxUrl: $("boxUrl").value.trim(), boxToken: $("boxToken").value.trim() }, (r) => {
      if (r?.ok) { res.textContent = `✓ ${r.host} · siphon ${r.version}`; res.className = "result ok"; }
      else { res.textContent = "✗ " + (r?.error || "no answer"); res.className = "result bad"; }
    });
  });
}

init();
