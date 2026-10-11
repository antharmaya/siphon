# Contributing to siphon

Thanks for wanting to make siphon better. The first thing to know is where a change belongs,
because the fastest way to help is often a pull request to a different project.

## Where your change goes

**A site doesn't work, or you want a new one** → almost always **upstream**.
siphon recognises sites through [yt-dlp](https://github.com/yt-dlp/yt-dlp) (video) and
[gallery-dl](https://github.com/mikf/gallery-dl) (image posts). Their communities keep 1,700+
extractors working as sites change, and siphon picks up every fix on update. Report or fix it there:

- video site: yt-dlp's [issue tracker](https://github.com/yt-dlp/yt-dlp/issues) or an extractor PR.
  A site too niche for yt-dlp can ship as a [yt-dlp plugin](https://github.com/yt-dlp/yt-dlp#plugins)
  package; siphon loads plugins like yt-dlp does.
- image site: gallery-dl's [issue tracker](https://github.com/mikf/gallery-dl/issues).

Before filing, run `siphon -v URL` and check which resolver handled it. If yt-dlp has an
extractor for the site and it fails there too, it's an upstream bug.

**Everything siphon does on top of them** → **here**. That's the browser sniffer, blob/MediaSource
capture, the downloader, the queue, setup, block diagnosis and the browser extension.

## The seams

siphon has one central type, `Candidate` (`src/siphon/models.py`): a piece of media something
found. Resolvers produce candidates; downloaders consume them. Most changes plug into one end.

| You want to… | Look at |
|---|---|
| find media a new way | `src/siphon/resolve/`: a resolver returns `Candidate`s. `direct.py` is the smallest example; `sniff.py` drives Chrome over CDP |
| catch media inside the page (players, MSE, blobs) | `INIT_JS` in `resolve/sniff.py`, the script injected into every frame before the page runs |
| save a kind of media | `src/siphon/download/`: `download()` dispatches on `Candidate.kind` |
| make siphon fetch another tool | `FETCHABLE` in `src/siphon/tools.py`: a name, its release URL per platform, its checksum file |
| add a capability check | `capabilities()` in `src/siphon/setup.py` (shared by `doctor`, `setup` and any UI) |
| change the button in the browser | `extension/content.js` (`attach()` places a button) and `extension/background.js` |

## Getting set up

```bash
git clone https://github.com/antharmaya/siphon && cd siphon
uv sync
uv run siphon doctor          # what this machine can do; `uv run siphon setup` fills the gaps
uv run pytest                 # full suite; the e2e tests need Chrome and ffmpeg
uv run pytest --ignore=tests/test_e2e.py   # fast unit tests only (~10 s)
```

The end-to-end tests serve a local fixture site (`tests/conftest.py`) built with ffmpeg, so
they never touch the network. If you add a mechanism, add a fixture page that only that
mechanism can solve.

## House rules

- **No telemetry, no accounts, nothing phones home.** That's the reason people choose siphon.
- **Never decrypt DRM.** Detect it and say so. Plain AES-128 HLS is not DRM and is fine.
- **Never re-encode by default.** Choose among the streams a site serves and stream-copy them.
- **A test has to fail when the thing it covers breaks.** Before you open the PR, break your
  mechanism on purpose and watch the test go red. A green suite on its own proves nothing.
- **Verify against a real download**, not just a passing config. Say in the PR what you ran.
- Commits follow [Conventional Commits](https://www.conventionalcommits.org/) (`feat(setup): …`,
  `fix(extension): …`), one coherent change per commit.

## Good first contributions

- **Windows and macOS testing.** `install.sh` and `siphon setup` are verified on Linux;
  the Windows/macOS download tables in `tools.py` are written but not yet run on those systems.
- **macOS ffmpeg fetch.** `siphon setup` defers to Homebrew there; a checksummed source would remove that step.
- **An Android share target.** Sharing a link from any app straight to a running siphon.

## Responsible use

siphon is for research, archiving and personal use. Contributions that exist to defeat paywalls,
DRM or access controls won't be merged.
