# siphon browser extension

A download button next to every video and image, wired to the `siphon` CLI. Works in Vivaldi,
Chrome, Chromium, Brave and Edge, and in Firefox, Zen and LibreWolf (140 or later).

```
          ┌──────────────┐  click   ┌────────────┐  native   ┌──────────────┐  runs  ┌────────┐
 page ──► │ content.js    │ ───────► │ background │ messaging │ siphon_host  │ ─────► │ siphon │ ──► file
 video    │ (the button)  │          │ (worker)   │ ────────► │ .py (bridge) │        │  CLI   │
          └──────────────┘          └────────────┘           └──────────────┘        └────────┘
                 ▲ progress events flow back the same way ▲
```

An extension cannot run a program directly, so the button talks to a tiny **native
messaging host** (`host/siphon_host.py`) that runs `siphon` and streams progress back.
This is the same mechanism Video DownloadHelper's "CoApp" used.

## Install (one time)

1. **Install the native host.** With siphon installed, from anywhere:
   ```bash
   siphon setup browser
   ```
   It writes a manifest into every installed browser's `NativeMessagingHosts` dir, locked to
   this extension's two IDs: `pbeimeldfnbenghllloohnnofbfaicfe` (this folder, the GitHub zip)
   and `dkicnkfpoodpflagdafjoobaoclfndlp` (the Chrome Web Store build). `--remove` undoes it.
   Working on the host itself? `./host/install.sh` from this folder points the browsers at
   `host/siphon_host.py` in the checkout instead; re-run it if you move the repo.

2. **Load the extension** — in `vivaldi://extensions` (or `chrome://extensions`):
   enable **Developer mode**, click **Load unpacked**, and pick this `extension/` folder.
   The ID is fixed by the `key` in `manifest.json`, so the host manifest keeps matching.

3. `siphon` must be on your `PATH`: the one-line installer in the main README does that, or
   `uv tool install --editable ..` from the repo root, then `siphon setup` for ffmpeg and
   friends. Or set `SIPHON_BIN`. The host also finds it via `uv run` in a dev checkout.

**Firefox, Zen, LibreWolf.** Build the Firefox edition (`python3 scripts/build-extension.py
--unpacked`), then in `about:debugging` → This Firefox → **Load Temporary Add-on**, pick
`dist/extension-firefox/manifest.json`. `siphon setup browser` registers the host for them too.
A Flatpak or Snap browser can't start the host: run `siphon serve` and use **my siphon box** with
`http://127.0.0.1:8765` (see below). `store/src/firefox_check.py` runs the whole flow in a real Firefox.

The popup's top-right shows **host v…** when the bridge is reachable, or a red
**host missing** with install instructions if not.

## Use

- **Button on the video.** Hover a video; a *Download* button sits in its top-right. Click it:
  siphon re-opens the page, finds the real stream and saves it. The button shows
  `Finding… → 42% → Saved ✓`.
- **Button on Twitter/X images.** On x.com, each photo gets the same button; it grabs the
  whole tweet's images (full resolution). Logged-in content works automatically — the
  extension tells the host which browser it is, and the host reads that browser's cookies.
- **Toolbar popup.** Click the siphon icon for everything found on the page — each HLS/DASH
  manifest and progressive file the player fetched, newest and highest-quality first, each
  with its own *Get*. Plus **Download this page** and a format preset (best / compatible /
  small / archive).

Files go to `~/Downloads` (override with `SIPHON_DOWNLOAD_DIR`).

While it works, the button shows the siphon mark doing what the download is doing: finding the
stream, swallowing it down, a gulp when it's saved, a recoil if it failed.

## Send it to your siphon box instead

If siphon runs on another machine (`siphon serve`, see the main README), open the popup, choose
**Send downloads to → my siphon box**, and paste the box's URL (e.g. your Tailscale
`https://box.tailnet.ts.net`) and its token (`siphon serve token` on the box). Press **Test**.
From then on videos go to the box, with live progress on the same button. Images still save
on this computer instantly. The native host isn't needed for box mode.

## Why it sends the page URL, not the `<video>` src

Most players use `blob:` / MediaSource sources that only exist inside the tab — useless to
any downloader. siphon's strength is re-opening the page and watching what it fetches, so the
button hands it the **page** URL. A specific stream picked from the popup is passed as well,
and siphon takes it directly when it can.

## Config

| env (read by the host) | meaning |
|---|---|
| `SIPHON_BIN` | path to the siphon executable |
| `SIPHON_COOKIES_BROWSER` | override which browser's cookies to use (`none` disables); default: the browser the extension runs in |
| `SIPHON_DOWNLOAD_DIR` | where files are saved (default `~/Downloads`) |
| `SIPHON_HOST_LOG` | host log file (default `~/.local/share/siphon/host.log`) |

Uninstall the host with `./host/uninstall.sh`.

## Files

| file | role |
|---|---|
| `manifest.json` | MV3 manifest; `key` fixes the extension ID |
| `background.js` | service worker: observes network for media (webRequest), runs the native-messaging port |
| `content.js` / `content.css` | injects the button over each `<video>`, relays progress |
| `popup.html` / `popup.js` | per-page media list + presets + host status |
| `host/siphon_host.py` | native-messaging bridge to the CLI |
| `host/install.sh` / `uninstall.sh` | register/remove the host for each browser |
