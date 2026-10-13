# Chrome Web Store submission checklist: siphon

Everything to paste or upload is in this folder. Field text lives in `listing.md` and
`PERMISSIONS.md`, and the policy in `PRIVACY.md`. Work top to bottom.

## 0. Before you open the dashboard

- [ ] **Build the package:** `python3 scripts/build-extension.py`. Upload
      `dist/siphon-extension-0.2.0-store.zip`, **not** the `-github` zip (that one carries the
      `key` field and `host/`). The build is deterministic: the same tree gives the same sha256.
- [ ] **Make the privacy policy reachable.** The dashboard needs a public URL, and
      `https://github.com/antharmaya/siphon/blob/master/store/PRIVACY.md` exists only once
      `store/` is committed and pushed. Push is your call. Any public URL with the same text works.
- [ ] **YouTube decision is built in.** The store build has no button on youtube.com / youtu.be /
      youtube-nocookie.com / youtubekids.com, and the background refuses those hosts plus
      googlevideo.com with a message. Verified in headless Brave (`store/src/capture.py`, 14/14).

## 1. Account settings (one time, dashboard → Account)

- [ ] **Publisher display name:** `varbees` (already set).
- [ ] **Contact email:** enter it, then click the link in the verification email. You can't
      publish until it's verified.
- [ ] **Trader / non-trader declaration (EU DSA):** see founder decision. A trader declaration
      publishes an address and phone number on the listing.
- [ ] **2-Step Verification** must be on for the Google account that publishes.
- [ ] Optional: **Trusted testers** emails, if you want a Private test release first.
- Note: new publishers can have at most **2 published extensions** until the limit is raised (store docs).

## 2. Upload (dashboard → Add new item)

- [ ] Choose `dist/siphon-extension-0.2.0-store.zip` → Upload.
- [ ] **Copy the item ID right away.** It's the 32-letter ID in the item URL / Package tab. The store
      assigns its own ID, and it will **not** be `pbeimeldfnbenghllloohnnofbfaicfe` (the GitHub
      build's ID).

## 3. Before you submit: let the native host accept the store ID (blocking for streams)

**Done 2026-10-12** for item `dkicnkfpoodpflagdafjoobaoclfndlp` (siphon 0.3.0). Kept below for Edge's ID.

`extension/host/install.sh` writes the host manifest with
`"allowed_origins": ["chrome-extension://pbeimeldfnbenghllloohnnofbfaicfe/"]` only. A store
install has a different ID, so Chrome refuses the connection. I reproduced this: the store build
in Brave got *"Access to the specified native messaging host is forbidden."* Box mode, plain
files and X photos still work, but every stream download fails until this is fixed.

- [ ] Edit `extension/host/install.sh` so it allows both IDs (and Edge's later):
  ```bash
  EXT_ID="pbeimeldfnbenghllloohnnofbfaicfe"   # GitHub / unpacked build (key in manifest.json)
  STORE_ID="<paste the Chrome Web Store item ID>"
  # in the MANIFEST heredoc:
  "allowed_origins": ["chrome-extension://$EXT_ID/", "chrome-extension://$STORE_ID/"]
  ```
- [ ] Commit and push that before the listing goes live, so a store user who follows the
      popup's "github.com/antharmaya/siphon" pointer gets a host that accepts them. Don't put the
      store key into `extension/manifest.json`: it would change the GitHub build's ID and break
      existing installs.
- [ ] Re-run `./extension/host/install.sh` on your own machine, then test once with the
      store-installed extension.

## 4. Package tab

Read-only. Check it shows **siphon: download button for web video**, version **0.2.0**, MV3.

## 5. Store listing tab (in the dashboard's order)

| field | value |
|---|---|
| Description | paste the block from `listing.md` → "Description" |
| Category | Productivity → Tools (fallback: Workflow & Planning) |
| Language | English |
| Store icon | `store/icon-128.png` |
| Global promo video | leave empty |
| Screenshots | `store/screenshot-1-button.png`, `-2-progress`, `-3-streams`, `-4-box` (in that order) |
| Small promo tile | `store/promo-small-440x280.png` |
| Marquee promo tile | `store/promo-marquee-1400x560.png` |
| Official URL | optional (needs a site verified in Search Console) |
| Homepage URL | `https://github.com/antharmaya/siphon` |
| Support URL | `https://github.com/antharmaya/siphon/issues` |
| Mature content | off |

(Name and summary come from the manifest. Change them in `scripts/build-extension.py`.)

## 6. Privacy practices tab

All text is in `PERMISSIONS.md`.

- [ ] **Single purpose:** paste.
- [ ] **Permission justifications:** one field each for `webRequest`, `storage`,
      `nativeMessaging`, `downloads`, `webNavigation`, and the host permission. If the dashboard
      lists `tabs` or `scripting`, you uploaded the wrong zip.
- [ ] **Remote code:** "No, I am not using remote code".
- [ ] **Data usage:** tick **Authentication information**, **Web history**, **Website content**.
      Leave the rest unticked.
- [ ] Tick all three **certifications**.
- [ ] **Privacy policy URL:** `https://github.com/antharmaya/siphon/blob/master/store/PRIVACY.md`

## 7. Distribution tab

- [ ] **Payments:** Free.
- [ ] **Visibility:** Public, or Unlisted if you want a soft launch. Both get the same review.
- [ ] **Regions:** All regions.

## 8. Test instructions tab (for the reviewer)

```
No account or login is needed.

1. Open https://www.w3schools.com/html/html5_video.asp, scroll to the first video and hover it.
   A "Download" button appears in its top-right corner. Click it: the file (mov_bbb.mp4) is saved
   by Chrome's downloader and the button shows "Saved · copy".
   (Verified with this exact build on 2026-10-12.)
2. Click the toolbar icon: the popup lists the media the page loaded.
3. HLS/DASH streams and "Download this page" need the free companion app (native messaging host
   com.antharmaya.siphon), installed from https://github.com/antharmaya/siphon. Without it the popup
   says the app isn't installed and points there. Plain files work without it.
4. This edition does not run on YouTube: there is no button on youtube.com, and a download request
   for a YouTube URL is refused with a message.
```

## 9. Submit

- [ ] **Submit for review.** The confirm dialog has a "publish automatically after review" box.
      Untick it if you want to choose the launch moment. You then have 30 days to publish.
- Expect a slower, in-depth review. `<all_urls>`, `webRequest` and `nativeMessaging` together
  are the kind of combination that gets a closer look. The store also says video downloaders
  aren't featured, though they can be listed (program policies, "Product ranking").
- Every later upload needs a higher `version` in `extension/manifest.json`. It's 0.2.0 now, while
  the Python package is 0.2.1, and the two are versioned separately.

---

## Microsoft Edge Add-ons

**Does the same zip work?** Yes. Upload `siphon-extension-<version>-store.zip` to Partner Center
→ Edge Add-ons. It's the same Chromium MV3 package, and `background.js` already detects Edge (UA
`Edg/`) so the app uses Edge's cookies.

- **Policy:** Edge's developer policy has the same rule as Chrome's:
  *"Your extension must not provide, encourage, or enable the unauthorized access, download, or
  streaming of copyrighted content or media."*
  ([learn.microsoft.com/…/developer-policies](https://learn.microsoft.com/en-us/legal/microsoft-edge/extensions/developer-policies)).
  Use the store build, with YouTube excluded, here too.
- **ID:** Edge assigns its own ID. Add it to `allowed_origins` in `host/install.sh` (step 3) as
  a third ID.
- **Native host manifest location:** Linux `~/.config/microsoft-edge/NativeMessagingHosts/`
  (install.sh already writes there). macOS `~/Library/Application Support/Microsoft
  Edge/NativeMessagingHosts/`. Windows registry key
  `HKCU\Software\Microsoft\Edge\NativeMessagingHosts\com.antharmaya.siphon`. install.sh is Linux-only.
- The privacy policy, listing text and graphics carry over. Edge asks for its own screenshot set.
  The 1280×800 PNGs fit.

## Firefox (addons.mozilla.org)

**Built and verified 2026-10-13.** `python3 scripts/build-extension.py` writes
`dist/siphon-extension-<version>-firefox.zip`: the full edition (nothing excluded; Mozilla's add-on
policies have no YouTube or download rule). `web-ext lint` reports 0 errors and one warning (no
Firefox-for-Android support declared, which is intended). `store/src/firefox_check.py` runs it in a
real Firefox and passes 12 of 12: install, site access granted at install, the button, the stream
list, the native host, a browser download, an HLS download through the host, and the localhost
route for sandboxed browsers.

What the Firefox manifest changes, and why:

| change | why |
|---|---|
| `browser_specific_settings.gecko.id` = `siphon@antharmaya.com` | the native host allows add-on IDs, so the ID must be fixed |
| `background.scripts` = `["policy.js", "background.js"]` | Firefox MV3 runs an event page, not a service worker, and an event page has no `importScripts` |
| `data_collection_permissions.required` = `authenticationInfo`, `browsingActivity`, `websiteContent` | required for new AMO add-ons since 2025-11-03. Page and stream URLs, media links and the user's own box token leave the browser (to the local app, or to the server the user names). Same three as the Chrome listing |
| `strict_min_version` = `140.0` | the built-in data-consent prompt ships in 140 |
| no `key`, no `minimum_chrome_version` | Chrome-only |

Native host: `siphon setup browser` writes `~/.mozilla/native-messaging-hosts/` (also for a Firefox
whose profile is under `~/.config/mozilla`: a real Firefox 157 did not read the XDG dir), plus
`~/.zen`, `~/.librewolf`, `~/.floorp`, `~/.waterfox` when they exist, keyed by the add-on ID.

**Flatpak and Snap browsers can't start a native host** (no WebExtensions portal on a stock
desktop). There the user runs `siphon serve` on the same computer and picks "my siphon box" with
`http://127.0.0.1:8765` and the token from `siphon serve token`. The popup and
`siphon setup browser` both say so. Gap: that route doesn't carry the browser's logins yet.

### Submitting (needs a Firefox account; free, no fee)

- [ ] https://addons.mozilla.org/developers/ → sign in → **Submit a New Add-on** → **On this site**.
- [ ] Upload `dist/siphon-extension-<version>-firefox.zip`. Compatibility: Firefox (desktop) only.
- [ ] "Do you need to submit source code?" **No**: nothing is minified or generated except
      `manifest.json` and `policy.js`, which `scripts/build-extension.py` writes (say so in the notes).
- [ ] Name, summary, description, categories, licence, privacy policy, notes for reviewers: paste
      from `store/amo.md`.
- [ ] Screenshots: the same four PNGs in `store/`.
- [ ] Submit. Listed add-ons are checked automatically first; a human review can follow later.

To install in your own Firefox or Zen before it is listed: `about:debugging` → This Firefox →
**Load Temporary Add-on** → pick `dist/extension-firefox/manifest.json` (it stays until the browser
restarts). A permanent install needs Mozilla's signature, which the AMO submission gives.
