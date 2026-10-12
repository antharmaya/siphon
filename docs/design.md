# How siphon is put together, and why

Notes for contributors on the decisions that are expensive to undo. Code-level detail lives in
each module's docstring.

## One type in the middle

Everything that finds media produces a `Candidate` (`models.py`); everything that saves media
consumes one. Resolvers (`resolve/`: direct URL, yt-dlp, gallery-dl, the Chrome sniffer, in-page
capture) never know how files are written; downloaders (`download/`) never know how media was
found. A new way to find video is a new resolver, nothing else.

## Site support belongs upstream

yt-dlp and gallery-dl maintain 1,700+ extractors as sites change. siphon uses them as libraries
and never forks an extractor, so it inherits every fix on update and its own surface stays small.
siphon's own work is what they don't do: the browser sniffer, blob/MediaSource capture, the
segmented downloader, the queue, setup, block diagnosis, the server and the extension.

## Machine-readable progress

`siphon --progress-json` writes one JSON event per stdout line. Everything that runs siphon as a
subprocess (the queue, `siphon serve`, the extension's host) reads that channel instead of parsing
console text. yt-dlp, gallery-dl and ffmpeg all print to stdout themselves, so enabling the
channel duplicates fd 1 for the events and points fd 1 at stderr: nothing else can reach it.

## siphon serve

**Each job is a subprocess.** A stuck page or a crash can't take the server down, and the job
runs the exact same pipeline as the CLI. The queue file is shared, so `siphon queue list` and the
phone see the same jobs.

**The API takes intent, not flags.** A client says *this URL, this quality, this destination*.
`jobs.build_args` maps that to CLI flags from an allowlist. Raw flags would let a client point
`-o` anywhere or make siphon read local files (`--cookies`, `-a`). URLs must be http(s), and
loopback and link-local addresses (including cloud metadata at 169.254.169.254) are refused.
The media a page points at is the page author's choice, not the client's, so it is held to a
second line (`netcheck.inward`): never on a more local network than the page itself. A public page
can't get siphon to fetch 127.0.0.1, a router or the metadata service and save the reply where the
phone can download it; a NAS page can still serve its own LAN media.

**Defence in depth on the HTTP side.** It binds to 127.0.0.1 by default. A Host-header allowlist
blocks DNS rebinding (a web page pointing its own hostname at your machine). Devices pair with a
one-time 8-digit code (10 minutes, five tries) for an HttpOnly, SameSite=Strict session cookie;
scripts use a bearer token. Cookie-authenticated writes need an `X-Siphon` header, which a
cross-site page can't send without a CORS preflight the server never grants. Request bodies are
read before any decision, so a refused request can't leave bytes that get parsed as the next one.
Files are served only from a finished job's own list, inside the download folder.

**Reaching it from elsewhere composes with existing tools instead of running a relay.** Tailscale
(private) and Cloudflare tunnels (public) are outbound connections, so they work behind CGNAT,
and their security is someone else's audited job. siphon runs them in the foreground as child
processes: when siphon stops, the exposure stops. A relay operated by siphon would mean accounts,
a server to run, and every user's traffic passing through it.

## Clouds through rclone

rclone supports 70+ storage services and handles each one's sign-in, so siphon shells out to it
rather than implementing OAuth per provider. Tokens stay in rclone's config. Private expiring
links are offered only for S3-type storage (R2, S3, B2): for Drive or Dropbox, `rclone link`
changes the file's sharing settings, which siphon won't do on its own.

## The mark

The logo is a golden spiral that sharpens to a point; every motion state is the same outline
recomputed per frame (`assets/make.py`) and morphed with SVG `<animate>`, which works in every
browser and inside `<img>` (CSS path morphing doesn't work in Safari). Each state has a still copy
for reduced motion.
