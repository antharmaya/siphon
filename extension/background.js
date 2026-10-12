// siphon background service worker.
//
// Two jobs:
//   1. Watch network traffic (observational webRequest, all MV3 allows) for media URLs the
//      page's player fetched — HLS/DASH manifests and progressive files a content script
//      can't see, because they never touch the DOM. Keep a per-tab list.
//   2. On a download request from the content script or popup, hand the job to the siphon
//      CLI through the native messaging host and relay progress back.
//
// The service worker is event-driven and may be killed between events (MV3). Per-tab state
// lives in chrome.storage.session so it survives a restart within the browsing session.

const HOST = "com.antharmaya.siphon";

// Distribution policy, set at build time (policy.js; scripts/build-extension.py). The GitHub build
// blocks nothing; the Chrome Web Store build lists hosts it must not download from. Chrome loads it
// here; Firefox lists policy.js before this file in background.scripts, so it's already defined.
if (typeof self.SIPHON_POLICY === "undefined" && typeof importScripts === "function") {
  try { importScripts("policy.js"); } catch {}
}
const POLICY = self.SIPHON_POLICY || { edition: "github", blockedHosts: [], blockedMessage: "" };

// True when url's host is one of the blocked hosts or a subdomain of one.
function isBlocked(url) {
  if (!url || !POLICY.blockedHosts.length) return false;
  let host;
  try { host = new URL(url).hostname.toLowerCase(); } catch { return false; }
  return POLICY.blockedHosts.some((b) => host === b || host.endsWith("." + b));
}

// Which browser are we in? The native host uses this to read THIS browser's login cookies,
// without which X/Instagram image posts come back empty. UA tells Vivaldi/Edge/Opera apart;
// Chrome vs Brave is ambiguous (Brave hides itself), so Brave is detected via navigator.brave.
async function detectBrowser() {
  const ua = navigator.userAgent;
  if (/Vivaldi/.test(ua)) return "vivaldi";
  if (/Edg\//.test(ua)) return "edge";
  if (/OPR\//.test(ua)) return "opera";
  try { if (navigator.brave && (await navigator.brave.isBrave())) return "brave"; } catch {}
  return "chrome";
}

const MEDIA_RE = /\.(m3u8|mpd|mp4|webm|m4v|mov|mkv|flv|m4a|mp3)(\?|#|$)/i;
const SEGMENT_RE = /(seg(ment)?[-_]?\d+|frag(ment)?[-_]?\d+|chunk[-_]?\d+|\/range\/|init\.mp4|\.ts(\?|$)|\.m4s(\?|$))/i;
const AD_RE = /doubleclick|googlesyndication|googleadservices|adservice|moatads|adnxs|criteo|taboola|outbrain|amazon-adsystem|adsrvr|pubmatic|rubiconproject|openx|smartadserver|imasdk|\/ads?\/|\/vast\//i;

function kindOf(url, type) {
  if (/\.m3u8(\?|#|$)/i.test(url) || type === "application/vnd.apple.mpegurl") return "hls";
  if (/\.mpd(\?|#|$)/i.test(url)) return "dash";
  if (SEGMENT_RE.test(url)) return null; // a piece of a stream, not the stream
  if (MEDIA_RE.test(url)) return "file";
  return null;
}

// tabId -> Map(canonicalUrl -> {url, kind, size, title, ts})
const perTab = new Map();

function canonical(url) {
  try {
    const u = new URL(url);
    for (const p of ["bytestart", "byteend", "range", "_nc_rid"]) u.searchParams.delete(p);
    return u.origin + u.pathname + (u.search || "");
  } catch { return url; }
}

async function remember(tabId, item) {
  if (tabId < 0) return;
  let m = perTab.get(tabId);
  if (!m) { m = new Map(); perTab.set(tabId, m); }
  const key = canonical(item.url);
  const prev = m.get(key);
  if (prev && (prev.size || 0) >= (item.size || 0)) return;
  m.set(key, { ...prev, ...item, ts: Date.now() });
  updateBadge(tabId);
  // Persist a trimmed copy; storage can't hold Maps.
  try {
    await chrome.storage.session.set({ ["media_" + tabId]: [...m.values()].slice(-40) });
  } catch {}
}

function updateBadge(tabId) {
  const n = perTab.get(tabId)?.size || 0;
  chrome.action.setBadgeText({ tabId, text: n ? String(n) : "" }).catch(() => {});
  chrome.action.setBadgeBackgroundColor({ tabId, color: "#1f6feb" }).catch(() => {});
}

chrome.webRequest.onResponseStarted.addListener(
  (d) => {
    if (d.tabId < 0 || AD_RE.test(d.url) || d.url.startsWith("blob:") || d.url.startsWith("data:")) return;
    if (isBlocked(d.url) || isBlocked(d.initiator) || isBlocked(d.documentUrl)) return; // store build: not listed
    const ct = (d.responseHeaders || []).find((h) => h.name.toLowerCase() === "content-type")?.value;
    const kind = kindOf(d.url, (ct || "").split(";")[0].trim());
    if (!kind) return;
    const clen = (d.responseHeaders || []).find((h) => h.name.toLowerCase() === "content-length")?.value;
    remember(d.tabId, { url: d.url, kind, size: clen ? parseInt(clen, 10) : null, source: "network" });
  },
  { urls: ["<all_urls>"] },
  ["responseHeaders"]
);

chrome.tabs.onRemoved.addListener((tabId) => {
  perTab.delete(tabId);
  chrome.storage.session.remove("media_" + tabId).catch(() => {});
});

chrome.webNavigation?.onCommitted?.addListener((d) => {
  if (d.frameId === 0) { // top-level navigation: the old page's media is stale
    perTab.delete(d.tabId);
    chrome.storage.session.remove("media_" + d.tabId).catch(() => {});
    updateBadge(d.tabId);
  }
});

// ----- download dispatch -------------------------------------------------------------------
//
// Two paths, picked by the caller's `mode`:
//   browser  the browser saves the URL itself (chrome.downloads) — instant, uses this tab's
//            own session and cookies, NO native host and NO headless re-sniff. For images and
//            plain progressive files, which is the common case and what should feel immediate.
//   siphon   HLS/DASH/blob streams and extractor sites (YouTube, X video) go to the siphon CLI
//            through the native host, which stitches/merges what the browser can't save directly.
//
// Every job carries a jobId (from the content script) so progress updates only the one button
// that was clicked — never every button on the page.

const ports = new Map(); // jobId -> native port
const dlJobs = new Map(); // chrome.downloads id -> {jobId, tabId, req}

function relay(tabId, jobId, ev) {
  const out = { type: "progress", tabId, jobId, ...ev };
  chrome.runtime.sendMessage(out).catch(() => {});
  if (tabId >= 0) chrome.tabs.sendMessage(tabId, out).catch(() => {});
}

function browserDownload(req, tabId, jobId) {
  const opts = { url: req.url, saveAs: false };
  if (req.filename) opts.filename = req.filename.replace(/[\\/:*?"<>|\x00-\x1f]+/g, "_").replace(/^\.+/, "").slice(0, 200);
  relay(tabId, jobId, { event: "resolving", message: "saving…" });
  try {
    chrome.downloads.download(opts, (id) => {
      if (chrome.runtime.lastError || id == null) {
        // The browser refused (bad URL, blocked). If there's a page, let siphon try instead.
        if (req.page) { viaSiphon(req, tabId, jobId); return; }
        relay(tabId, jobId, { event: "error", message: chrome.runtime.lastError?.message || "download failed" });
        return;
      }
      dlJobs.set(id, { jobId, tabId, req });
    });
  } catch (e) {
    if (req.page) viaSiphon(req, tabId, jobId);
    else relay(tabId, jobId, { event: "error", message: e.message });
  }
}

// siphon proper: your box when "send to my siphon box" is on, else the CLI through the native host.
function viaSiphon(req, tabId, jobId) {
  boxSettings().then((box) => (box ? boxDownload(req, tabId, jobId, box) : siphonDownload(req, tabId, jobId)));
}

chrome.downloads?.onChanged.addListener((delta) => {
  const j = dlJobs.get(delta.id);
  if (!j) return;
  const state = delta.state?.current;
  if (state === "complete") {
    dlJobs.delete(delta.id);
    chrome.downloads.search({ id: delta.id }, (r) => relay(j.tabId, j.jobId, { event: "done", file: r?.[0]?.filename || "" }));
  } else if (state === "interrupted") {
    dlJobs.delete(delta.id);
    // A hotlink-protected CDN can 403 a bare browser download; siphon replays with page context.
    if (j.req?.page) viaSiphon(j.req, j.tabId, j.jobId);
    else relay(j.tabId, j.jobId, { event: "error", message: delta.error?.current || "interrupted" });
  }
});

function siphonDownload(req, tabId, jobId) {
  let port;
  let finished = false; // the host sent done/error already
  try {
    port = chrome.runtime.connectNative(HOST);
  } catch (e) {
    relay(tabId, jobId, { event: "error", message: "native host not reachable: " + e.message });
    return;
  }
  ports.set(jobId, port);
  port.onMessage.addListener((msg) => {
    if (msg.event === "done" || msg.event === "error") finished = true;
    relay(tabId, jobId, msg);
  });
  port.onDisconnect.addListener(() => {
    const err = chrome.runtime.lastError;
    ports.delete(jobId);
    // The host exits right after sending "done" — that disconnect is normal, not a failure.
    // Only a disconnect BEFORE any result means the host never ran (not installed / rejected).
    if (finished) return;
    relay(tabId, jobId, { event: "error", message: (err?.message || "host disconnected") + " — " + (POLICY.hostHelp || "native host not installed? run host/install.sh, then reload the extension") });
  });
  detectBrowser().then((browser) => {
    port.postMessage({
      action: "download", page: req.page, media: req.media || null, kind: req.kind || null,
      preset: req.preset || "best", audioOnly: !!req.audioOnly, browser,
    });
  }).catch(() => {
    port.postMessage({ action: "download", page: req.page, media: req.media || null,
      kind: req.kind || null, preset: req.preset || "best", audioOnly: !!req.audioOnly });
  });
}

// ---- "send to my siphon box": the job runs on your `siphon serve`, progress comes back here ----
async function boxSettings() {
  const s = await chrome.storage.local.get(["target", "boxUrl", "boxToken"]);
  return s.target === "box" && s.boxUrl && s.boxToken ? s : null;
}

async function boxDownload(req, tabId, jobId, box) {
  const base = box.boxUrl.replace(/\/+$/, "");
  const auth = { Authorization: "Bearer " + box.boxToken };
  // The box re-resolves the page itself; a blob: or page-local stream URL means nothing over there.
  const direct = req.media || (req.mode === "browser" ? req.url : null);  // a plain file's URL rides in `url`
  const media = direct && /^https?:/.test(direct) ? direct : null;
  const body = { url: media || req.page, preset: req.preset || "best", dest: "box" };
  if (req.audioOnly) body.audio = true;
  relay(tabId, jobId, { event: "resolving", message: "sending to your box…" });
  let job;
  try {
    const r = await fetch(base + "/api/jobs", { method: "POST", headers: { ...auth, "Content-Type": "application/json" }, body: JSON.stringify(body) });
    job = await r.json();
    if (!r.ok) throw new Error(job.error || "HTTP " + r.status);
  } catch (e) {
    relay(tabId, jobId, { event: "error", message: "box: " + e.message });
    return;
  }
  const end = Date.now() + 2 * 3600 * 1000;
  while (Date.now() < end) {
    await new Promise((ok) => setTimeout(ok, 1000));
    let j;
    try {
      const r = await fetch(base + "/api/jobs", { headers: auth });
      j = (await r.json()).find((x) => x.id === job.id);
    } catch { continue; } // a dropped poll on a flaky network is not a failed download
    if (!j) { relay(tabId, jobId, { event: "error", message: "the job disappeared from the box" }); return; }
    // Relaying each tick also keeps this service worker awake (extension API calls reset its idle timer).
    if (j.status === "done") {
      const files = [...(j.remote || []), ...(j.files || [])];
      relay(tabId, jobId, { event: "done", file: files[0] ? "on your box: " + files[0] : "on your box", files });
      return;
    }
    if (j.status === "error") { relay(tabId, jobId, { event: "error", message: j.error || "failed on the box" }); return; }
    const pct = j.progress && j.progress.percent;
    if ((j.stage === "downloading" || j.stage === "uploading") && pct != null) relay(tabId, jobId, { event: "progress", percent: pct });
    else relay(tabId, jobId, { event: "resolving", message: j.status === "pending" ? "queued on your box…" : "finding…" });
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  const tabId = sender.tab?.id ?? msg.tabId ?? -1;

  if (msg.type === "list") {
    const m = perTab.get(tabId);
    sendResponse({ items: m ? [...m.values()] : [] });
    return true;
  }

  if (msg.type === "domVideo") {
    if (msg.url && !msg.url.startsWith("blob:") && !isBlocked(msg.url) && !isBlocked(sender.tab?.url)) {
      const kind = kindOf(msg.url) || "file";
      remember(tabId, { url: msg.url, kind, title: msg.title, source: "dom" });
    }
    sendResponse({ ok: true });
    return true;
  }

  if (msg.type === "ping") {
    try {
      const port = chrome.runtime.connectNative(HOST);
      let answered = false;
      port.onMessage.addListener((m) => { answered = true; sendResponse({ ok: true, version: m.version }); port.disconnect(); });
      port.onDisconnect.addListener(() => { if (!answered) sendResponse({ ok: false, error: chrome.runtime.lastError?.message }); });
      port.postMessage({ action: "hello" });
    } catch (e) { sendResponse({ ok: false, error: e.message }); }
    return true;
  }

  if (msg.type === "download") {
    const jobId = msg.jobId || Math.random().toString(36).slice(2);
    // Store build: refuse blocked hosts here, the one place every download passes through
    // (button, popup, box and native host alike). The button and popup show the message.
    if ([msg.page, msg.url, msg.media, sender.tab?.url, sender.url].some(isBlocked)) {
      sendResponse({ started: true, jobId, refused: true });
      relay(tabId, jobId, { event: "error", message: POLICY.blockedMessage || "not available in this edition" });
      return true;
    }
    boxSettings().then((box) => {
      // Images stay instant and local. With a box, every video goes to the box (even a plain file:
      // that's the point of sending it there); without one, plain files the browser can save stay local.
      if (msg.mode === "browser" && msg.url && (msg.isImage || !box)) browserDownload(msg, tabId, jobId);
      else if (box) boxDownload(msg, tabId, jobId, box);
      else siphonDownload(msg, tabId, jobId);
    });
    sendResponse({ started: true, jobId });
    return true;
  }

  if (msg.type === "boxTest") {
    const base = (msg.boxUrl || "").replace(/\/+$/, "");
    fetch(base + "/api/status", { headers: { Authorization: "Bearer " + (msg.boxToken || "") } })
      .then(async (r) => sendResponse(r.ok ? { ok: true, ...(await r.json()) } : { ok: false, error: r.status === 401 ? "token rejected" : "HTTP " + r.status }))
      .catch((e) => sendResponse({ ok: false, error: e.message }));
    return true;
  }
});
