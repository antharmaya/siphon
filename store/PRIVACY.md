# siphon browser extension: privacy policy

*Last updated 2026-10-13. Applies to the siphon extension for Chrome, Edge and other Chromium
browsers and for Firefox and Firefox-based browsers (Chrome Web Store, Firefox and GitHub
editions), version 0.2.0 and later.*

**Short version:** siphon has no account, no analytics and no telemetry. It doesn't send
anything to Antharmaya Labs or anyone else. The only places it sends data are your browser's own
downloader, the siphon app on your computer, and a siphon server you set up yourself.

siphon is open source (MIT). Everything below can be checked in the code at
[github.com/antharmaya/siphon](https://github.com/antharmaya/siphon/tree/master/extension).

## What the extension reads, and why

| what | where it comes from | what it's for |
|---|---|---|
| URLs of media the page loads (video/audio files, HLS and DASH playlists), with their size and type | the browser's network events for the current tab (read-only) | listing the streams in the toolbar popup |
| `<video>` source URLs and the page title | the page you're on | placing the download button and naming the download |
| photo URLs and post links on X (twitter.com / x.com) | the page you're on | the button on X photos, at full resolution |
| when a tab navigates or closes | the browser | clearing that tab's list |

The extension reads nothing else. It doesn't read form fields, messages, passwords or cookies,
and it doesn't track which sites you visit. The list of media found on a tab is kept in memory
and in the browser's session storage. It is deleted when you leave the page, close the tab or
quit the browser.

## What it stores

In the browser's local extension storage, on your device only:

- where downloads go: "this computer" or "my siphon box"
- your siphon box's URL and access token, if you enter them

Remove the extension and they're gone.

## Where data goes

Nothing leaves your device until you click a download. Then, depending on what you clicked:

1. **Photos, and plain video files when box mode is off,** are saved by your browser's own
   downloader, straight from the site, the same as right-click → Save. If the site refuses that
   download, siphon retries it through 2 or 3 below.
2. **Streams** (HLS, DASH, `blob:` video, or "Download this page") go to the **siphon app on your
   computer** through the browser's native messaging. The extension sends the page URL, the
   stream URL if you picked one, the quality preset, and the name of your browser. That message
   stays on your machine.
3. **If you turned on "send to my siphon box"**, video downloads go to **the server you entered**,
   with your token, instead of the app. The extension sends the page or stream URL and the
   preset. It then checks that server for progress about once a second until the job finishes. Use
   an `https://` address (for example your Tailscale `*.ts.net` URL) so the token is encrypted in
   transit.

siphon doesn't sell, share, rent or transfer any data to anyone, doesn't use it for advertising or
profiling, and has no server of its own for data to reach.

## The siphon app (a separate program you install)

When a stream is sent to it, the siphon app on your computer:

- downloads the media from the site and saves it to your Downloads folder (or `SIPHON_DOWNLOAD_DIR`)
- appends one line per saved file to `siphon.jsonl` in that folder: the page URL, media URL,
  time, and the file's SHA-256 (a provenance record; turn it off with `--no-log`)
- writes a log of what it ran, including those URLs, to `~/.local/share/siphon/host.log`
  (`SIPHON_HOST_LOG` moves it)
- reads your browser's cookies **on your computer** so a site you're logged into treats the
  download as you. They are sent only to the sites they belong to, by the normal cookie rules.
  Set `SIPHON_COOKIES_BROWSER=none` to turn this off.

All of these files stay on your computer, and you can delete them at any time.

## Children

siphon isn't directed at children and doesn't knowingly collect data from anyone.

## Changes

Changes to this policy are made in this file, and its history is public in the repository.

## Contact

Open an issue at [github.com/antharmaya/siphon/issues](https://github.com/antharmaya/siphon/issues).
Antharmaya Labs, [antharmaya.com](https://antharmaya.com).
