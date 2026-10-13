<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/lockup-animated-dark.svg">
    <img src="assets/lockup-animated-light.svg" alt="siphon" width="420">
  </picture>
</p>

<p align="center"><b>One sink for any download.</b><br>
Video from 1,700+ sites, image posts, and the <code>blob:</code> video other tools can't reach.<br>
A CLI, a button in your browser, and a queue. No account, no telemetry, your storage.</p>

```bash
curl -fsSL https://raw.githubusercontent.com/antharmaya/siphon/master/install.sh | sh
```

No sudo. It installs [uv](https://docs.astral.sh/uv/) if you don't have it, installs siphon as an
isolated tool, and fetches ffmpeg and a JavaScript runtime only if this machine lacks them.

```bash
siphon 'https://www.youtube.com/watch?v=jNQXAC9IVRw'   # any page with a video
siphon 'https://x.com/user/status/123'                 # a post: its video, or every image
siphon 'blob:https://site.example/uuid'                # tab-only video: opens the page, records it
siphon queue add URL URL URL && siphon queue run -N 3  # line them up, three at a time
siphon doctor                                          # what works here, and the fix for what doesn't
siphon serve --tailscale                               # use it from your phone, anywhere
```

## What siphon is

siphon stands on two excellent projects and adds the parts they don't cover:

- **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** ships 1,700+ site extractors. siphon uses it as a library
  for every site it recognises.
- **[gallery-dl](https://github.com/mikf/gallery-dl)** knows image posts: X/Twitter, Instagram,
  Reddit and more. siphon uses it for photos, at full resolution.
- **Everything else is siphon's own:** a real Chrome that watches the network on pages no
  extractor knows; an in-page recorder for `blob:` and MediaSource video; a browser button that
  downloads what you're looking at; an adaptive segmented downloader with resume; a persistent
  queue; a diagnosis that tells an ISP block from a geofence in seconds instead of hanging; and
  `siphon setup`, which fetches its own dependencies.

New sites come from yt-dlp and gallery-dl, whose communities keep them working as sites change;
siphon inherits each fix the moment you update. See [CONTRIBUTING.md](CONTRIBUTING.md) for where a
contribution belongs.

## Install, other ways

```bash
uv tool install "antharmaya-siphon @ git+https://github.com/antharmaya/siphon"   # then: siphon setup
uv tool install "antharmaya-siphon[impersonate] @ git+https://github.com/antharmaya/siphon"  # + TLS impersonation (+38 MB)
```

The package is `antharmaya-siphon` (PyPI's plain `siphon` is an unrelated weather library);
the command is `siphon`. On Windows, install uv from PowerShell
(`irm https://astral.sh/uv/install.ps1 | iex`), then run the same `uv tool install` and
`siphon setup -y`.

`siphon setup` downloads ffmpeg, deno, rclone or cloudflared from their official releases into
`~/.local/share/siphon/bin`, checks the published SHA-256 where one exists, and runs each binary
once before installing it. `siphon doctor` lists every capability, what it needs, and the
command that fixes it.

## Usage

```bash
siphon URL                          # best stream, any page
siphon -F URL                       # list everything found, download nothing
siphon -j URL                       # same, as JSON (cookies redacted)
siphon -p URL                       # choose which candidate
siphon --all URL                    # every distinct stream on the page
siphon -B URL                       # browse mode: you drive Chrome, siphon catches what plays
siphon -a urls.txt -o ~/research    # batch
siphon --cookies-from-browser chrome URL   # your login, this site's cookies only
siphon -f 'bv*[height<=1080]+ba' URL       # yt-dlp format selection passes through
siphon -x URL                       # audio only
siphon -P compat URL                # H.264+AAC MP4: plays on any phone/TV/editor/WhatsApp
siphon -P archive URL               # best + metadata, chapters, subs, thumbnail, info.json (MKV)
siphon --live-duration 600 URL      # record 10 minutes of a live stream
siphon --sponsorblock URL           # cut sponsor segments (comma list to add intro,outro,...)
siphon --section 1:30-2:45 URL      # download only that span
siphon -r 2M URL                    # cap speed
siphon --archive seen.txt -a list.txt   # batch, skip anything already downloaded
siphon --proxy socks5h://127.0.0.1:1080 URL   # route through a tunnel (env: SIPHON_PROXY)
siphon queue add URL... [flags]     # queue jobs (flags stored per job); queue list / run -N 3 / clear / rm ID
```

Blocked sites are diagnosed in about 4 s instead of hanging: siphon tells an ISP DNS sinkhole
(fixable with any proxy) apart from a platform geofence (needs an exit in another country).
`SIPHON_PROXY_DOMAINS=tiktok.com,streamable.com` routes only those sites through the tunnel.

Every download is appended to `<output>/siphon.jsonl` with the page URL, the media URL, a
timestamp and the SHA-256: provenance for research use. `--no-log` turns it off.

## Your phone, anywhere: `siphon serve`

Run siphon on your computer (or a small server) and use it from your phone. Paste a link, choose
**this device** (the file lands in the phone's Downloads) or **the box** (it stays on the computer),
and watch it live.

```bash
siphon serve --tailscale     # private: only your own devices, through your tailnet (recommended)
siphon serve                 # this machine only: http://127.0.0.1:8765
siphon serve --cloudflare    # a public https link with no account; pairing is still required
siphon serve pair            # a new pairing code (and QR) for another device
```

Scan the QR code it prints, or type the 8-digit code, once per device. To run an always-on box
(an old laptop, a Pi, a small VPS) with downloads landing in your own R2 bucket, see
[docs/self-host.md](docs/self-host.md). On the phone, *Add to Home
Screen* gives it an icon. None of these opens a port on your router, so it works behind CGNAT,
and the tunnel closes when siphon stops.

How it stays private: it listens on 127.0.0.1 unless you choose otherwise, answers only to
hostnames it knows (no DNS rebinding), pairs each device with a one-time code (10 minutes, five
tries), and accepts *what* to download (a link, a quality, a destination), never command-line
flags. It only fetches http(s) links, never this machine's own addresses or cloud-metadata ones.
It serves back only the files a job saved, from inside the download folder.

## Straight to your own cloud

```bash
siphon setup rclone                 # once: fetches rclone (no sudo)
siphon cloud add gdrive             # signs in to your Google Drive in the browser
siphon URL --to gdrive:siphon       # download, upload, remove the local copy (--keep keeps it)
siphon cloud default gdrive:siphon  # then: --to cloud, and the phone shows it under "Save to"
```

Anything [rclone](https://rclone.org/overview/) speaks works: Google Drive, Dropbox, OneDrive, MEGA,
pCloud, Cloudflare R2 (`siphon cloud add r2 --type r2 access_key_id=… secret_access_key=… endpoint=…`),
S3, B2, WebDAV, SFTP. Your sign-in stays in rclone's own config; siphon never sees or sends it.
The provenance record keeps the SHA-256 of what was uploaded and where it went.

## The browser button

[`extension/`](extension/) adds a download button beside every video and image in any
Chromium browser (Chrome, Vivaldi, Brave, Edge) and in Firefox, Zen and LibreWolf. Images and plain files download instantly
through the browser; streams go to the CLI through a native-messaging bridge, with live progress
on the button and a copy-path icon when the file lands. Multi-image posts offer *this one*,
*all*, or *cancel*.

```bash
siphon setup browser    # once: lets the extension hand streams to this siphon
```

Then reload the extension. That works for the Chrome Web Store build, the Firefox build and the
GitHub zip alike; loading the extension from source is in [extension/README.md](extension/README.md).

A Flatpak or Snap browser is sandboxed and can't start a program on your computer. There, run
`siphon serve`, and in the extension's popup choose **my siphon box** with `http://127.0.0.1:8765`
and the token from `siphon serve token`. `siphon setup browser` tells you when that applies.

## How it resolves a link

```
URL ─► direct (0.1s) ──► ytdlp (1-5s) ──► sniff: real Chrome (5-30s)
        is the URL         a dedicated       network: every request in every frame
        itself media?      extractor?        capture: blob:/MediaSource bytes in-page
                                             dom:     <video src>, og:video, URLs in HTML
                    │
                    ▼
              [Candidate]  ── the one seam: resolvers produce it, downloaders consume it
                    │
   site → yt-dlp   hls/dash → yt-dlp fragments + page headers
   file → segmented downloader (adaptive, resumable)   capture → ffmpeg remux
```

- **System Chrome over raw CDP, no Playwright.** siphon speaks the Chrome DevTools Protocol
  itself (`resolve/cdp.py`, ~300 lines over one WebSocket) to the Chrome you already have.
  That saved 137 MB (the venv went 239 → 61 MB), drops a Node driver process per run, and
  removes a version pin. Chrome rather than Chromium because Chromium lacks H.264/AAC and
  many players fail silently on it before requesting any media. `SIPHON_CHROME` overrides.
- **HLS master over renditions.** The master lists every quality, including ones the
  player never requested.
- **Ads are blocked at the route layer** (host list + IAB banner sizes), and related-video
  previews scraped from HTML are dropped once the network has found the real stream.

## blob: video

A `blob:` URL is a handle into one tab's memory, so nothing can fetch it from outside.
siphon goes inside the tab instead:

- **MediaSource** (most streaming players): hooks `SourceBuffer.appendBuffer`, records
  every byte the player decodes, plays the video through at 16×, and remuxes the tracks.
  This kicks in automatically when the network pass found nothing fetchable
  (`--capture force` always records; `--capture off` never does).
- **Plain Blob**: grabbed at `URL.createObjectURL`, before the page can revoke it.

Limits: seeking or quality switches during a recording can leave gaps; MSE inside a
Web Worker isn't hooked.

## Segmented downloader

IDM-style dynamic segmentation: when a connection goes idle, it steals the back half of
the largest range still in flight. Resume is checkpointed to `<file>.part.json` and
validated against size + ETag.

Parallelism is **adaptive**. It starts with 1 connection and adds one at a time only
while total throughput rises by at least 15%. Measured on a 40 Mbit line, one connection
already saturates the link: fixed 16 connections took 28 s against 6.3 s for one, and
aria2c degrades the same way. More connections only help when each one is capped
(throttling CDNs, file hosts). `-N` sets the ceiling (default 8).

## Formats

siphon never re-encodes. It chooses among the streams a site serves and stream-copies them.
`-P best` (default) takes the highest quality, usually AV1 on YouTube. `-P compat` prefers
H.264 + AAC in MP4, which is hardware-decoded on GPUs without AV1 support (anything before
Intel 11th gen / NVIDIA RTX 30). `-P archive` keeps everything in MKV, and for YouTube it
samples several responses, because the same format ID is served at different bitrates
(measured: 374 vs 711 kbps).

See `docs/landscape-2026-10-08.md` for the competitor/technique research and measured speed numbers,
and [docs/design.md](docs/design.md) for the decisions behind the server, the clouds and the mark.

## Not supported

**DRM** (Widevine / PlayReady / FairPlay). It's detected when a license is actually
requested, and when a manifest carries it, and reported. siphon does not decrypt it.
Plain AES-128 HLS is not DRM and works.

## Use it responsibly

siphon is for research, archiving and personal use. It downloads what your browser can already
play; respect the terms of the sites you use and the rights of the people who made the work.
It never decrypts DRM (Widevine, PlayReady, FairPlay are detected and reported, not broken),
never re-encodes unless you ask, and sends nothing anywhere: no account, no telemetry.

## Tests

`uv run pytest` runs the suite (67 tests). The end-to-end tests drive real Chrome against a local
fixture site built with ffmpeg: a late-loaded `<video>`, an XHR-loaded HLS master, an MSE player
fed from obscured URLs (the network pass can't win there, so only capture can), and a revoked
Blob. Every output file is checked with ffprobe for its streams and duration. Each core
mechanism (work-stealing, resume, adaptive growth, the appendBuffer hook, the Blob grab, the
checksum and path guards in `siphon setup`) has been broken on purpose to confirm its test fails.

## License

MIT, see [LICENSE](LICENSE). siphon depends on yt-dlp (Unlicense) and gallery-dl (GPL-2.0), and
`siphon setup` fetches ffmpeg builds that are GPL; each keeps its own license. The logo
wordmark is set in Bricolage Grotesque (SIL Open Font License).

Made at [Antharmaya Labs](https://antharmaya.com).
