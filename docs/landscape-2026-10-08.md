# Video-download landscape: research notes, 2026-10-08

Personal tool, not a product. This is market research to find **techniques** siphon lacks,
plus what moves in the industry would break it.

## Who does what

| Tool | Model | Core technique | What siphon took / should take |
|---|---|---|---|
| yt-dlp | OSS CLI | ~1,750 dedicated extractors + generic | is siphon's first resolver |
| 4K Video Downloader, SnapDownloader | paid GUI | site extractors behind a GUI | nothing new |
| JDownloader 2 | OSS GUI | LinkGrabber "deep decrypt", file hosts, clipboard watching | **gap:** file hosts (Drive/Dropbox/OneDrive/MEGA), clipboard watch |
| cobalt.tools | OSS web service | per-service API extractors (~20 services) | nothing yt-dlp lacks |
| gallery-dl | OSS CLI | image/gallery extractors (Pinterest, Tumblr, Bluesky, Imgur…), delegates video to yt-dlp | **gap:** add as a resolver for image+video posts |
| Video DownloadHelper | extension | network sniff + "capture while playing" (no CoApp since Nov 2025) | ✓ both |
| CocoCut, M3U8 Detector, StreamGrabber | extension / userscript | m3u8 detect + download *inside the browser* ("resolves IP-locked segments locally") | **gap:** in-browser fetch for session/IP-bound media that 403s outside |
| Soul Browser, 1DM | Android browser | sniff any page while you browse | ✓ `--browse` |
| N_m3u8DL-RE | OSS CLI | HLS/DASH/MSS, live, key-based decryption | MSS support; decryption is out of scope (DRM) |
| streamlink | OSS CLI | live resolver, `--retry-streams` waits for a broadcast | **gap:** expose yt-dlp `wait_for_video` |

Sources: alternativeto.net, videohelp.com, github.com/yt-dlp/yt-dlp issues, addons.mozilla.org listings,
NAB/IBC 2026 coverage. Full links are in the session report.

## Industry moves that matter

1. **YouTube SABR + PO tokens (2025→).** Session-bound proof-of-origin tokens; the yt-dlp answer
   is a sidecar provider (`bgutil-ytdlp-pot-provider`), which itself sees intermittent 403s.
   YouTube worked on 2026-10-08 without it. **siphon's edge:** real Chrome generates valid
   tokens, and MSE capture records what the player decodes, so it's a fallback when extraction breaks.
2. **Chrome native HLS (141+).** Chrome 152 here answers `canPlayType(mpegurl) = "maybe"`.
   Players then skip MediaSource, which blinds capture. **Fixed:** siphon's browser denies
   native HLS (tested, mutation-checked).
3. **YouTube serves different encodes under one itag.** itag 398 (AV1 720p): 374 kbps in
   3 of 4 responses, 711 kbps in 1, 351 kbps in another run. **Done:** `--preset archive`
   samples 4 responses and keeps the richest.
4. **Media over QUIC (MoQ) / WebCodecs players.** draft-18, 11 vendors at NAB 2026,
   production "2027 territory". Traffic rides WebTransport (invisible to network sniffing)
   and decodes via WebCodecs (no MSE). **Future:** hook `VideoDecoder.decode` /
   `EncodedVideoChunk` the way `appendBuffer` is hooked today.

## Formats: the expert defaults

- **Never re-encode.** Choose among served streams and stream-copy. Re-encoding loses
  quality and burns CPU for a stream the server already offers.
- **Codec vs this machine.** Intel UHD 630 + GTX 1650 Mobile decode H.264/HEVC/VP9 in hardware,
  **not AV1** (AV1 decode starts at Intel 11th gen / RTX 30 / RX 6000). yt-dlp's default picks
  AV1, which is fine at 1080p on CPU but 4K AV1 is software-decoded here. → `--preset compat`.
- **Container.** MP4 for compatibility (H.264/AAC). MKV for archiving (any codec, many
  subtitle tracks, attachments like thumbnails and info.json). → `--preset archive` uses MKV.

## Speed: measured, not assumed

| Change | Before | After |
|---|---|---|
| unknown site (no extractor), end to end | 35–42s (and an indefinite hang before the bounded-call fix) | 13–14s |
| sniffer after first find | +15–20s (serial DOM pass over stalled frames) | +~3s |
| YouTube, aria2c ×8 vs native | 4.7–5.4 vs 5.3–6.0 MB/s | no change: the line is the limit, not YouTube |
| progressive file, 16 fixed connections vs 1 | 28s vs 6.3s | adaptive: starts at 1, grows only on measured gain |
| startup | 0.52s (0.45s imports, yt-dlp 0.29s) | left as is: only direct-file paths would gain |

## Open gaps, ranked by "any link" coverage

1. In-browser fetch fallback for 403/session-bound media (CocoCut/M3U8-Detector class).
2. gallery-dl as a resolver (Pinterest, Tumblr, Bluesky, Imgur, image+video posts).
3. File hosts: Google Drive, Dropbox (`?dl=1`), OneDrive share API, MEGA (AES-CTR from URL key).
4. `--wait` for scheduled live streams (yt-dlp `wait_for_video`).
5. Clipboard watch / browser hand-off (JDownloader/IDM convenience).
6. WebCodecs capture for MoQ-era players.
