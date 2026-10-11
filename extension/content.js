// Puts a siphon download button over every <video> on the page, and reports DOM-visible
// media URLs to the background worker so the popup can list them too.
//
// The button sends the PAGE url (not the <video> src) to siphon by default: siphon re-opens
// the page itself and finds the real stream, which is far more robust than a blob:/MSE src
// that only lives in this tab. If the video has a plain http(s) src, that's passed as a hint.

(() => {
  if (window.__siphonContentLoaded) return;
  window.__siphonContentLoaded = true;

  const BTN_CLASS = "siphon-dl-btn";
  const tracked = new WeakSet();

  function usableSrc(v) {
    const s = v.currentSrc || v.src || "";
    if (s && /^https?:/i.test(s)) return s;
    const source = v.querySelector("source[src]");
    const ss = source?.src || "";
    return /^https?:/i.test(ss) ? ss : null;
  }

  function report(v) {
    if (!extAlive()) return;
    const url = usableSrc(v);
    if (url) {
      try { chrome.runtime.sendMessage({ type: "domVideo", url, title: document.title }); } catch {}
    }
  }

  // minWidth/minHeight: images can be smaller than video players. target() returns the
  // {page, media} to download on click; onClick(btn, el) overrides that to do something else
  // (e.g. open a menu for a multi-image post).
  function attach(el, { minW, minH, target, onClick }) {
    if (tracked.has(el)) return;
    const r = el.getBoundingClientRect();
    if (r.width < minW || r.height < minH) return; // re-checked by the observer as it grows/loads
    tracked.add(el);

    const btn = document.createElement("button");
    btn.className = BTN_CLASS;
    btn.title = "Download with siphon";
    setState(btn, "idle", "Download");
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      // After a save, the button becomes a "copy path" button so the file is easy to find.
      if (btn.classList.contains("siphon-done") && btn._savedFile) { copyPath(btn); return; }
      if (onClick) onClick(btn, el);
      else trigger(btn, target());
    });

    const holder = document.createElement("div");
    holder.className = "siphon-dl-holder";
    holder.appendChild(btn);
    document.body.appendChild(holder);
    place(holder, el, minW, minH);

    const reposition = () => place(holder, el, minW, minH);
    window.addEventListener("scroll", reposition, { passive: true, capture: true });
    window.addEventListener("resize", reposition, { passive: true });
    const io = new ResizeObserver(reposition);
    io.observe(el);
    holder._cleanup = () => { io.disconnect(); window.removeEventListener("scroll", reposition, true); window.removeEventListener("resize", reposition); };
    holder._el = el;
  }

  function place(holder, el, minW, minH) {
    const r = el.getBoundingClientRect();
    if (r.width < minW || r.height < minH || !document.contains(el)) {
      holder.style.display = "none";
      return;
    }
    holder.style.display = "block";
    holder.style.top = Math.max(8, window.scrollY + r.top + 10) + "px";
    holder.style.left = window.scrollX + r.right - 150 + "px";
  }

  let jobSeq = 0;

  function trigger(btn, req) {
    if (!extAlive()) { fail(btn, "extension was reloaded — refresh this page"); teardown(); return; }
    const jobId = "c" + Date.now() + "-" + jobSeq++;
    btn._jobId = jobId; // only progress events for THIS id touch THIS button
    btn.classList.remove("siphon-error", "siphon-done");
    btn.classList.add("siphon-working");
    setState(btn, "resolving", "Starting…");
    try {
      chrome.runtime.sendMessage(
        { type: "download", jobId, preset: "best", ...req, page: req.page || location.href },
        (res) => {
          if (chrome.runtime.lastError || !res?.started) {
            fail(btn, chrome.runtime.lastError?.message || "couldn't start");
          }
        }
      );
    } catch (e) {
      fail(btn, "extension was reloaded — refresh this page");
      teardown();
    }
  }

  function fail(btn, why) {
    btn.classList.remove("siphon-working");
    btn.classList.add("siphon-error");
    setState(btn, "error", "Failed");
    btn.title = "siphon: " + why;
    setTimeout(() => reset(btn), 4000);
  }

  function reset(btn) {
    btn.classList.remove("siphon-working", "siphon-error", "siphon-done");
    btn._savedFile = null;
    setState(btn, "idle", "Download");
    btn.title = "Download with siphon";
  }

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type !== "progress" || !msg.jobId) return;
    // Exactly the button that started this job — not every button on the page.
    let btn = null;
    for (const h of document.querySelectorAll(".siphon-dl-holder")) {
      const b = h.querySelector("." + BTN_CLASS);
      if (b && b._jobId === msg.jobId) { btn = b; break; }
    }
    const label = btn?.querySelector(".siphon-dl-label");
    if (!label) return;
    if (msg.event === "progress" && msg.percent != null) {
      btn.classList.add("siphon-working");
      setState(btn, "downloading", Math.round(msg.percent) + "%");
    } else if (msg.event === "resolving") {
      btn.classList.add("siphon-working");
      setState(btn, "resolving", msg.message || "Finding…");
    } else if (msg.event === "done") {
      btn.classList.remove("siphon-working");
      btn.classList.add("siphon-done");
      btn._savedFile = msg.file || "";
      const n = Array.isArray(msg.files) ? msg.files.length : 0;
      setState(btn, "done", n > 1 ? "Saved " + n + " · copy" : "Saved · copy");
      btn.title = "Saved: " + btn._savedFile + "  ·  click to copy path";
      // Linger longer than a normal reset so the copy affordance is actually usable.
      clearTimeout(btn._resetT);
      btn._resetT = setTimeout(() => reset(btn), 12000);
    } else if (msg.event === "error") {
      fail(btn, msg.message || "error");
    }
  });

  function copyPath(btn) {
    const path = btn._savedFile || "";
    const label = btn.querySelector(".siphon-dl-label");
    const done = () => { if (label) { label.textContent = "Copied ✓"; setTimeout(() => reset(btn), 1500); } };
    try {
      navigator.clipboard.writeText(path).then(done, () => fallbackCopy(path, done));
    } catch { fallbackCopy(path, done); }
  }

  function fallbackCopy(text, cb) {
    // clipboard API can be blocked without a recent gesture; a hidden textarea + execCommand still works.
    try {
      const ta = document.createElement("textarea");
      ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove(); cb();
    } catch {}
  }

  const isTwitter = /(^|\.)(twitter|x)\.com$/.test(location.hostname);

  function tweetUrlFor(el) {
    // The permalink lives on the <time>'s parent <a> inside the tweet's <article>.
    const article = el.closest("article");
    const a = article?.querySelector('a[href*="/status/"] time')?.closest("a")
           || article?.querySelector('a[href*="/status/"]');
    // Keep only up to the tweet id — drop /photo/1, /video/1, /analytics, /likes, ?query, #hash.
    if (a?.href) return a.href.replace(/(\/status\/\d+).*$/, "$1");
    const m = location.pathname.match(/\/status\/\d+/); // already on a status page
    return m ? location.origin + m[0] : location.href;
  }

  const PROGRESSIVE = /\.(mp4|m4v|webm|mov|mkv|m4a|mp3|ogg|opus|aac|wav|flac)(\?|#|$)/i;

  function fileNameFrom(url) {
    try {
      const p = new URL(url).pathname.split("/").filter(Boolean).pop();
      return p ? decodeURIComponent(p) : null;
    } catch { return null; }
  }

  // pbs.twimg.com/media/XYZ?format=jpg&name=small  ->  the original, full-resolution image.
  function fullResTwitterImage(img) {
    const u = new URL(img.currentSrc || img.src);
    const fmt = u.searchParams.get("format") || "jpg";
    u.searchParams.set("format", fmt);
    u.searchParams.set("name", "orig");
    const id = u.pathname.split("/").pop();
    return { url: u.toString(), filename: `${id}.${fmt}` };
  }

  function scanVideos() {
    document.querySelectorAll("video").forEach((v) => {
      report(v);
      attach(v, {
        minW: 120, minH: 80,
        target: () => {
          // On X, the tweet permalink lets yt-dlp fetch exactly this tweet's video — no re-sniff.
          if (isTwitter) return { mode: "siphon", page: tweetUrlFor(v) };
          const src = usableSrc(v);
          // A plain progressive file the browser can just save itself: instant, no host.
          if (src && PROGRESSIVE.test(src)) return { mode: "browser", url: src, filename: fileNameFrom(src), page: location.href };
          // blob:/MSE or unknown — siphon finds the real stream; pass any http(s) src as a hint.
          return { mode: "siphon", page: location.href, media: src || null };
        },
      });
    });
  }

  // In a carousel the slides overlap, so the clicked button may belong to an off-screen image.
  // Pick the pbs image with the largest on-screen area in this post — the slide you're looking at.
  function currentImage(img) {
    const scope = img.closest("article") || document;
    let best = img, area = -1;
    scope.querySelectorAll('img[src*="pbs.twimg.com/media/"]').forEach((i) => {
      const r = i.getBoundingClientRect();
      const vw = Math.max(0, Math.min(r.right, innerWidth) - Math.max(r.left, 0));
      const vh = Math.max(0, Math.min(r.bottom, innerHeight) - Math.max(r.top, 0));
      if (vw * vh > area) { area = vw * vh; best = i; }
    });
    return best;
  }

  function imagesInPost(img) {
    const article = img.closest("article");
    if (!article) return 1;
    const n = article.querySelectorAll('[data-testid="tweetPhoto"], img[src*="pbs.twimg.com/media/"]').length;
    // Carousels lazy-load, so also trust X's "Image 2 of 4" aria-label if present.
    const label = article.querySelector('[aria-label*=" of "]')?.getAttribute("aria-label") || "";
    const m = label.match(/of\s+(\d+)/i);
    return Math.max(n, m ? parseInt(m[1], 10) : 1);
  }

  let openMenu = null;
  function closeMenu() { openMenu?.remove(); openMenu = null; }

  function showImageMenu(btn, img) {
    closeMenu();
    const menu = document.createElement("div");
    menu.className = "siphon-menu";
    const add = (label, fn) => {
      const b = document.createElement("button");
      b.className = "siphon-menu-item";
      b.textContent = label;
      b.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); closeMenu(); fn(); });
      menu.appendChild(b);
    };
    add("Download this image", () => { const cur = currentImage(img); trigger(btn, { mode: "browser", isImage: true, ...fullResTwitterImage(cur), page: tweetUrlFor(cur) }); });
    add("Download all images", () => trigger(btn, { mode: "siphon", page: tweetUrlFor(img) }));
    const cancel = document.createElement("button");
    cancel.className = "siphon-menu-item siphon-menu-cancel";
    cancel.textContent = "Cancel";
    cancel.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); closeMenu(); });
    menu.appendChild(cancel);

    document.body.appendChild(menu);
    const r = btn.getBoundingClientRect();
    menu.style.top = window.scrollY + r.bottom + 6 + "px";
    menu.style.left = window.scrollX + Math.max(8, r.right - menu.offsetWidth) + "px";
    openMenu = menu;
  }

  document.addEventListener("click", (e) => { if (openMenu && !openMenu.contains(e.target) && !e.target.closest("." + BTN_CLASS)) closeMenu(); }, true);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });
  window.addEventListener("scroll", closeMenu, { passive: true, capture: true });

  function scanTwitterImages() {
    if (!isTwitter) return;
    document.querySelectorAll('img[src*="pbs.twimg.com/media/"]').forEach((img) => {
      if (img.closest('[data-testid="videoComponent"], [data-testid="videoPlayer"]')) return; // video poster
      attach(img, {
        minW: 120, minH: 120,
        // One image: save it directly (instant). Many images: let the user choose which / all.
        target: () => ({ mode: "browser", isImage: true, ...fullResTwitterImage(img), page: tweetUrlFor(img) }),
        onClick: (btn, el) => {
          if (imagesInPost(el) > 1) showImageMenu(btn, el);
          else { const cur = currentImage(el); trigger(btn, { mode: "browser", isImage: true, ...fullResTwitterImage(cur), page: tweetUrlFor(cur) }); }
        },
      });
    });
  }

  function scan() {
    if (!extAlive()) { teardown(); return; } // extension was reloaded; stop the old instance
    scanVideos();
    scanTwitterImages();
    document.querySelectorAll(".siphon-dl-holder").forEach((h) => {
      if (!document.contains(h._el)) { h._cleanup?.(); h.remove(); }
    });
  }

  // When the extension is reloaded/updated, content scripts already running on open tabs lose
  // their connection — any chrome.runtime.* call then throws "Extension context invalidated".
  // Detect that and quietly tear this old instance down instead of throwing on every scan tick.
  let scanTimer = null;
  function extAlive() {
    try { return !!(chrome.runtime && chrome.runtime.id); } catch { return false; }
  }
  function teardown() {
    try { mo.disconnect(); } catch {}
    if (scanTimer) { clearInterval(scanTimer); scanTimer = null; }
    try { closeMenu(); } catch {}
    document.querySelectorAll(".siphon-dl-holder").forEach((h) => { try { h._cleanup?.(); } catch {} h.remove(); });
  }

  const mo = new MutationObserver(() => scan());
  mo.observe(document.documentElement, { childList: true, subtree: true });
  scan();
  scanTimer = setInterval(scan, 2000); // SPAs swap media nodes as you scroll (YouTube, X, ...)

  // The living mark: the same body shows what the download is doing. Idle sits beside the label;
  // any working state stacks above it. Only swap the art on a state change, so a progress tick
  // never restarts the animation.
  function art(name) {
    try { return '<img class="siphon-state" alt="" src="' + chrome.runtime.getURL("states/" + name + ".svg") + '">'; }
    catch { return ""; } // extension reloaded: no art, the label still works
  }

  function setState(btn, name, text) {
    if (btn.dataset.state !== name) {
      btn.dataset.state = name;
      btn.innerHTML = art(name) + '<span class="siphon-dl-label"></span>';
      btn.classList.toggle("siphon-stack", name !== "idle");
    }
    btn.querySelector(".siphon-dl-label").textContent = text;
  }
})();
