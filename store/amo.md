# addons.mozilla.org listing copy

The data-collection declaration is in the manifest (`scripts/build-extension.py`, `FIREFOX_DATA`),
not in this form. Firefox shows it in the install prompt.

## Name

```
siphon: video download button
```

## Summary (max 250 characters)

```
A download button on web videos and X photos. Plain files save through Firefox itself; streams go to the free, open-source siphon app on your computer, or to your own siphon server. No account, no analytics.
```

## Description

```
siphon puts a download button on the video you're watching.

Hover a video on any page with a standard HTML5 player and a Download button sits in its corner. Click it and the button shows the progress, then "Saved". One more click copies the saved file's path.

WHAT IT DOES
• A button on web videos. Plain video files (MP4, WebM and similar) save straight through Firefox's own downloader.
• On X, a button on photos: save this photo at full resolution, or every photo in the post.
• A toolbar list of the media streams the page loaded (HLS and DASH playlists and plain files), each with its own Get button, plus "Download this page".
• Format presets: best quality, compatible (H.264/MP4), small (720p or less), archive (with metadata).

STREAMS USE THE FREE SIPHON APP
Browsers can't join HLS/DASH streams or save blob: video by themselves. For those, the extension hands the page to siphon, a free, open-source (MIT) app you install on your computer: github.com/antharmaya/siphon. After installing it, run "siphon setup browser" once. Without the app, plain video files and X photos still download.

FLATPAK OR SNAP FIREFOX, ZEN AND OTHERS
A sandboxed browser can't start an app on your computer. Run "siphon serve" instead and choose "my siphon box" in the popup, with the address http://127.0.0.1:8765 and the token from "siphon serve token".

OR SEND IT TO YOUR OWN SERVER
Run "siphon serve" on a home machine or a small server, paste its address and token into the popup, and videos download there instead, with live progress on the same button.

PRIVATE BY DESIGN
• No account, no analytics, no telemetry.
• The extension talks only to your browser, the siphon app on your computer and, if you set one up, your own siphon server. Nothing is sent to us or to anyone else.
• Open source: every line is on GitHub.

LIMITS
• DRM-protected video isn't supported. siphon doesn't break copy protection.
• Images get a button on X only. Elsewhere the button is for video.

Please download only what you have the right to save, and respect the terms of the sites you use and the people who made the work.
```

## Categories

**Download Management** (and **Photos, Music & Videos** as the second).

## Licence

MIT License.

## Privacy policy

Tick "This add-on has a privacy policy" and paste the text of `store/PRIVACY.md`.

## Support

- Support site: `https://github.com/antharmaya/siphon/issues`
- Homepage: `https://github.com/antharmaya/siphon`

## Notes for reviewers (paste)

```
Source: https://github.com/antharmaya/siphon (MIT). The package is built from extension/ by scripts/build-extension.py with no bundler or minifier; the script only rewrites manifest.json and policy.js for this edition.

No account is needed. To test without the optional desktop app:
1. Open https://www.w3schools.com/html/html5_video.asp and hover the video: a Download button appears in its corner. Click it and the MP4 saves through Firefox's downloader.
2. Click the toolbar icon: the popup lists the media the page loaded, each with a Get button.

Native messaging: streams (HLS/DASH) are handed to the open-source siphon command-line app, if the user installed it and ran "siphon setup browser". The host is com.antharmaya.siphon; its source is extension/host/siphon_host.py in the same repository. Without it the popup says so and plain files still download.

Host permission <all_urls>: the button is for videos on any site, and the read-only webRequest listener that lists a player's streams must see the sites the user visits. Nothing is blocked or modified.

Data: page and stream URLs go only to that local app, or to a server address the user typed in themselves (their own "siphon serve"), with the token they typed in. There is no developer-operated server, no analytics and no telemetry.
```
