// siphon serve — the phone UI. Plain JS, no build step, nothing loaded from outside the box.
"use strict";

const $ = (id) => document.getElementById(id);
const jobs = new Map();
const PULL_KEY = "siphon.pull";  // jobs this tab asked to save to "this device"

function pending() {
  try { return new Set(JSON.parse(sessionStorage.getItem(PULL_KEY) || "[]")); } catch { return new Set(); }
}
function remember(ids) {
  try { sessionStorage.setItem(PULL_KEY, JSON.stringify([...ids])); } catch { /* private mode: fine */ }
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method, credentials: "same-origin",
    headers: body ? { "Content-Type": "application/json", "X-Siphon": "1" } : { "X-Siphon": "1" },
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  return { ok: res.ok, status: res.status, data };
}

// ------------------------------------------------------------------ pairing
function showPair(message) {
  $("app").hidden = true;
  $("pair").hidden = false;
  $("pair-msg").textContent = message || "";
  $("code").focus();
}

$("pair-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const r = await api("POST", "/api/pair", { code: $("code").value });
  if (r.ok) return boot();
  $("pair-msg").textContent = (r.data && r.data.error) || "Pairing failed.";
});

// ------------------------------------------------------------------ rendering
function label(job) {
  const s = job.status, st = job.stage;
  if (s === "done") return ["Saved", "done"];
  if (s === "error") return ["Failed", "error"];
  if (s === "pending") return ["Queued", ""];
  if (st === "downloading" && job.progress && job.progress.percent != null) return [`${Math.round(job.progress.percent)}%`, "active"];
  if (st === "downloading") return ["Downloading", "active"];
  if (st === "uploading") return job.progress && job.progress.percent != null ? [`Uploading ${Math.round(job.progress.percent)}%`, "active"] : ["Uploading", "active"];
  if (st === "saved") return ["Finishing", "active"];
  return ["Finding the video", "active"];
}

function stateArt(job) {
  if (job.status === "done") return "/states/done.svg";
  if (job.status === "error") return "/states/error.svg";
  if (job.status === "pending") return "/mark.svg";
  if (job.stage === "downloading" || job.stage === "saved" || job.stage === "uploading") return "/states/downloading.svg";
  return "/states/resolving.svg";
}

function title(url) {
  try {
    const u = new URL(url);
    return u.hostname.replace(/^www\./, "") + (u.pathname.length > 1 ? u.pathname : "");
  } catch { return url; }
}

function size(n) {
  if (!n) return "";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 && i ? 1 : 0)} ${units[i]}`;
}

function base(path) { return path.split(/[\\/]/).pop(); }

// R2/S3/B2 files can be opened from the cloud directly (a private link that expires); others can't.
let presignable = new Set();
function linkable(remotePath) { return presignable.has(remotePath.split(":")[0]); }

function render(job) {
  let li = document.querySelector(`li[data-id="${job.id}"]`);
  if (!li) {
    li = document.createElement("li");
    li.className = "job";
    li.dataset.id = job.id;
    li.innerHTML = `<img class="state" alt="" width="44" height="44">
      <div class="body"><div class="job-top"><span class="job-title"></span><span class="pill"></span></div>
      <div class="bar"><i></i></div><div class="meta"></div><div class="actions"></div></div>`;
    $("jobs").prepend(li);
  }
  const [text, cls] = label(job);
  // The mark is alive: the same body shows what the job is doing. Only swap on a state change,
  // so a progress tick doesn't restart the animation.
  const art = stateArt(job);
  const img = li.querySelector("img.state");
  if (img.dataset.art !== art) { img.dataset.art = art; img.src = art; }
  li.querySelector(".job-title").textContent = title(job.url);
  li.querySelector(".job-title").title = job.url;
  const pill = li.querySelector(".pill");
  pill.textContent = text;
  pill.className = "pill " + cls;

  const bar = li.querySelector(".bar");
  const active = job.status === "active";
  bar.hidden = !active;
  const pct = job.progress && job.progress.percent;
  bar.classList.toggle("indeterminate", active && pct == null);
  bar.firstElementChild.style.width = pct != null ? `${pct}%` : "";

  const meta = [];
  if (active && job.progress && job.progress.speed) meta.push(`${size(job.progress.speed)}/s`);
  if (job.dest === "device") meta.push("to this device");
  if ((job.dest || "").startsWith("cloud")) meta.push(job.status === "done" && job.remote && job.remote.length ? `in ${job.remote.join(", ")}` : `to ${job.dest.replace(/^cloud:?/, "") || "your cloud"}`);
  if (job.status === "error" && job.error) meta.push(job.error);
  li.querySelector(".meta").textContent = meta.join(" · ");

  const actions = li.querySelector(".actions");
  actions.replaceChildren();
  (job.files || []).forEach((f, n) => {
    if (job.status !== "done") return;
    const a = document.createElement("a");
    a.className = "quiet";
    a.href = `/api/jobs/${job.id}/files/${n}`;
    a.textContent = `Save ${base(f)}`;
    actions.append(a);
  });
  if (job.status === "done") (job.remote || []).forEach((r, n) => {
    if (!linkable(r)) return;
    actions.append(button(`Open ${base(r)}`, async () => {
      const res = await api("GET", `/api/jobs/${job.id}/link/${n}`);
      if (res.ok) location.href = res.data.url; else alert((res.data && res.data.error) || "No link");
    }));
  });
  if (job.status === "error") actions.append(button("Retry", () => api("POST", `/api/jobs/${job.id}/retry`)));
  actions.append(button(active ? "Cancel" : "Remove", () => api("DELETE", `/api/jobs/${job.id}`)));
  $("empty").hidden = jobs.size > 0;
}

function button(text, onClick) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "quiet";
  b.textContent = text;
  b.addEventListener("click", onClick);
  return b;
}

// "This device": once the box has the file, hand it to this browser as a normal download.
function maybePull(job) {
  const want = pending();
  if (job.status !== "done" || !want.has(job.id)) return;
  want.delete(job.id);
  remember(want);
  (job.files || []).forEach((_, n) => {
    const a = document.createElement("a");
    a.href = `/api/jobs/${job.id}/files/${n}`;
    a.download = "";
    document.body.append(a);
    a.click();
    a.remove();
  });
}

function upsert(job) {
  if (job.removed) {
    jobs.delete(job.id);
    const li = document.querySelector(`li[data-id="${job.id}"]`);
    if (li) li.remove();
    $("empty").hidden = jobs.size > 0;
    return;
  }
  jobs.set(job.id, job);
  render(job);
  maybePull(job);
}

// ------------------------------------------------------------------ adding
$("add").addEventListener("submit", async (e) => {
  e.preventDefault();
  const go = $("go");
  go.disabled = true;
  $("add-msg").textContent = "";
  const dest = document.querySelector("input[name=dest]:checked").value;
  const body = { url: $("url").value.trim(), dest, preset: $("preset").value };
  if ($("audio").checked) body.audio = true;
  const r = await api("POST", "/api/jobs", body);
  go.disabled = false;
  if (r.status === 401) return showPair();
  if (!r.ok) { $("add-msg").textContent = (r.data && r.data.error) || "Couldn't add that link."; return; }
  if (dest === "device") { const w = pending(); w.add(r.data.id); remember(w); }
  $("url").value = "";
  upsert(r.data);
  if (live) live.poke();
});

// ------------------------------------------------------------------ live updates
// The whole list, from the SSE snapshot or a poll (live.js). Reconciled in place rather than
// rebuilt, so a poll every 1.5 s doesn't restart every job's animation.
function sync(list) {
  const seen = new Set(list.map((j) => j.id));
  [...jobs.keys()].filter((id) => !seen.has(id)).forEach((id) => upsert({ id, removed: true }));
  list.slice().reverse().forEach(upsert);  // oldest first: render() prepends new rows
  $("empty").hidden = jobs.size > 0;
}

let live = null;
function listen() {
  if (live) live.stop();
  const me = live = liveUpdates({
    connect: () => new EventSource("/api/events"),
    fetchJobs: async () => {
      const r = await api("GET", "/api/jobs");
      if (r.status === 401) { me.stop(); showPair(); return null; }
      return r.ok && Array.isArray(r.data) ? r.data : null;
    },
    onSnapshot: sync,
    onJob: upsert,
    busy: () => [...jobs.values()].some((j) => j.status === "active" || j.status === "pending"),
  });
  me.start();
}
document.addEventListener("visibilitychange", () => { if (!document.hidden && live) live.poke(); });

// ------------------------------------------------------------------ start
async function boot() {
  const params = new URLSearchParams(location.search);
  const shared = params.get("url") || (params.get("text") || "").match(/https?:\/\/\S+/)?.[0];
  const r = await api("GET", "/api/status");
  if (r.status === 401) return showPair(params.get("pair") === "failed" ? "That code is wrong or expired." : "");
  if (!r.ok) return showPair("The box didn't answer. Is siphon serve running?");
  $("pair").hidden = true;
  $("app").hidden = false;
  $("host").textContent = `on ${r.data.host}`;
  $("box-name").textContent = r.data.host;
  const dests = document.querySelector(".dest");
  dests.querySelectorAll("label.cloud").forEach((l) => l.remove());
  presignable = new Set((r.data.clouds || []).filter((c) => c.type === "s3" || c.type === "b2").map((c) => c.name));
  (r.data.clouds || []).forEach((c) => {
    const label = document.createElement("label");
    label.className = "cloud";
    const input = document.createElement("input");
    input.type = "radio"; input.name = "dest"; input.value = c.id;
    label.append(input, document.createTextNode(" " + c.name));
    label.title = c.type;
    dests.append(label);
  });
  if (shared) $("url").value = shared;
  if (params.toString()) history.replaceState(null, "", "/");
  $("api-url").textContent = location.origin + "/api/jobs";
  listen();
}

$("show-token").addEventListener("click", async () => {
  const r = await api("GET", "/api/token");
  if (!r.ok) return;
  $("token").textContent = r.data.token;
  $("token").hidden = false;
  $("copy-token").hidden = false;
});
$("copy-token").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("token").textContent); $("copy-token").textContent = "Copied"; }
  catch { getSelection().selectAllChildren($("token")); }
});

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});

boot();
