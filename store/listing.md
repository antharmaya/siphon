# Store listing copy

The **name** and **summary** shown in the store come from the package's manifest, not from the
dashboard. `scripts/build-extension.py` sets them for the store build (`STORE_NAME`,
`STORE_DESCRIPTION`) and refuses to build if either is too long. Only change them there.

## Name (37 / 75 chars, from the manifest)

```
siphon: download button for web video
```

## Summary (127 / 132 chars, from the manifest)

```
A download button on web videos and X photos. Streams go to the free siphon app on your computer, or to your own siphon server.
```

## Category

**Productivity → Tools**. I couldn't confirm the current category list from public docs. If
"Tools" isn't in the dropdown, use **Productivity → Workflow & Planning**.

## Language

**English** (English (United States) if the dropdown asks for a locale).

## Description (plain text, paste as is)

```
siphon puts a download button on the video you're watching.

Hover a video on any page with a standard HTML5 player and a Download button sits in its corner. Click it and the button shows the progress, then "Saved". One more click copies the saved file's path.

WHAT IT DOES
• A button on web videos. Plain video files (MP4, WebM and similar) save straight through Chrome's own downloader.
• On X, a button on photos: save this photo at full resolution, or every photo in the post.
• A toolbar list of the media streams the page loaded (HLS and DASH playlists and plain files), each with its own Get button, plus "Download this page".
• Format presets: best quality, compatible (H.264/MP4), small (720p or less), archive (with metadata).
• Progress on the button itself, and copy-path when the file lands.

STREAMS USE THE FREE SIPHON APP
Browsers can't join HLS/DASH streams or save blob: video by themselves. For those, the extension hands the page to siphon, a free, open-source (MIT) app you install on your computer: github.com/antharmaya/siphon. They talk through Chrome's native messaging. Without the app, plain video files and X photos still download.

OR SEND IT TO YOUR OWN SERVER
Run "siphon serve" on a home machine or a small server, paste its address and token into the popup, and videos download there instead, with live progress on the same button.

PRIVATE BY DESIGN
• No account, no analytics, no telemetry.
• The extension talks only to your browser, the siphon app on your computer and, if you set one up, your own siphon server. Nothing is sent to us or to anyone else.
• Open source: every line is on GitHub.

LIMITS
• DRM-protected video isn't supported. siphon doesn't break copy protection.
• This edition doesn't download from YouTube.
• Images get a button on X only. Elsewhere the button is for video.

Please download only what you have the right to save, and respect the terms of the sites you use and the people who made the work.

Privacy policy: https://github.com/antharmaya/siphon/blob/master/store/PRIVACY.md
Made by Antharmaya Labs: https://antharmaya.com
```

## Graphics (all in `store/`)

| field | file | size |
|---|---|---|
| Store icon | `icon-128.png` | 128×128, 96 px art inside 16 px of transparent padding, per the store guideline |
| Screenshot 1 | `screenshot-1-button.png` | 1280×800 |
| Screenshot 2 | `screenshot-2-progress.png` | 1280×800 |
| Screenshot 3 | `screenshot-3-streams.png` | 1280×800 |
| Screenshot 4 | `screenshot-4-box.png` | 1280×800 |
| Small promo tile | `promo-small-440x280.png` | 440×280 |
| Marquee promo tile (optional) | `promo-marquee-1400x560.png` | 1400×560 |

The screenshots are the real extension, captured in headless Brave on a made-up demo page
(`field-notes.example`, served locally) and framed in brand colours. Regenerate them with
`.venv/bin/python store/src/capture.py`. Only one state is staged: in screenshot 2, after a real
click, a progress event at 42% was sent through the extension's own `relay()` so the button
would hold that frame. Every other state on screen is real. The popup's
`host v0.1.0` and `✓ box · siphon 0.2.1` come from a real native host and a real throwaway
`siphon serve`.

## Optional fields

- **Promo video**: none yet.
- **Homepage URL**: `https://github.com/antharmaya/siphon`
- **Support URL**: `https://github.com/antharmaya/siphon/issues`
- **Official URL**: needs a site verified in Google Search Console (e.g. antharmaya.com). Optional.
- **Mature content**: off.
