# Privacy practices tab: what to paste

Every statement below was checked against `extension/*.js` on 2026-10-12. The store build
(`dist/siphon-extension-<version>-store.zip`, made by `scripts/build-extension.py`) declares
exactly these permissions:

```
permissions:       webRequest, storage, nativeMessaging, downloads, webNavigation
host_permissions:  <all_urls>
```

## Removed from the store build (and proposed for removal from the source manifest)

| permission | why it goes | evidence |
|---|---|---|
| `scripting` | Never used. Nothing calls `chrome.scripting`; the content script is declared in the manifest. Requesting an unused permission is a listed rejection reason. | `grep -n "chrome.scripting" extension/*.js` returns nothing |
| `tabs` | Not needed. The code uses `chrome.tabs.query`, `sendMessage` and `onRemoved`, none of which require it. The one thing `tabs` would add, reading `tab.url`, is already granted by the `<all_urls>` host permission. | Verified in headless Brave: with `tabs` removed, the popup still reads the page's URL (`store/src/capture.py`, check "popup reads the tab URL without the `tabs` permission") |

Both are still in `extension/manifest.json`, which the GitHub build ships unchanged. Removing them
from the source is a one-line change. I left it alone because the brief said to derive the store
manifest instead of editing the source.

## Single purpose (paste)

> siphon saves the video or image you're looking at. It puts a download button on web videos (and
> on photos on X), lists the media streams the current page loaded, and hands the one you pick to
> the browser's own downloader, to the free siphon app on your computer, or to a siphon server you
> run yourself.

## Permission justifications (paste one per field)

**webRequest**
> siphon watches network responses, read-only, to find the media streams a page's video player
> loads: HLS (.m3u8) and DASH (.mpd) manifests and plain video/audio files. Players fetch these
> over the network and never put them in the page, so this is the only way to list them in the
> toolbar popup. The listener reads each response's URL, Content-Type and Content-Length, ignores
> anything that isn't media, and never blocks, changes or redirects a request. Matching URLs are
> kept in memory and in session storage for that tab only. They are cleared when the tab navigates
> or closes, and they never leave the device unless the user clicks Get on one.

**storage**
> chrome.storage.session holds each tab's list of detected media streams, so the list survives
> the service worker being suspended. chrome.storage.local holds three settings the user enters in
> the popup: where downloads go (this computer, or their own siphon server), that server's URL, and
> its access token. Nothing is synced or sent anywhere by the storage API.

**nativeMessaging**
> Browsers can't stitch HLS/DASH streams or save blob: video, so for those siphon hands the job to
> the open-source siphon app the user installs on their own computer
> (github.com/antharmaya/siphon). Native messaging is how the extension sends that app the page
> URL, the chosen stream and a quality preset, and gets progress back to show on the button. It
> connects only to the host "com.antharmaya.siphon", and only after the user clicks a download.

**downloads**
> When the user clicks the button on a plain video file or a photo, siphon saves it with
> chrome.downloads, so the file goes through the browser's own downloader and the browser's own
> session for that site. It also watches its own downloads' state (complete / interrupted) to show
> "Saved" or an error on the button, and to look up the saved file's path for the copy-path action.

**webNavigation**
> siphon listens for top-level navigations (onCommitted, frameId 0) only to clear the previous
> page's list of detected media and reset the toolbar badge, so the popup never offers streams
> from a page the user has left. No navigation data is stored or sent.

**Host permission: `<all_urls>`**
> The download button is for videos on any site, so the content script that places it, and the
> read-only webRequest listener that finds a player's streams, must run on the sites the user
> visits. It also lets the extension reach the siphon server URL the user types in (their own
> machine, any host name). The content script only looks for <video> elements, and on X for
> photos. It reads their source URLs and the page title, and does nothing on pages without them.
> No page content is collected or sent anywhere unless the user clicks a download.

**Remote code**
> No, I am not using remote code. All JavaScript ships in the package. The extension loads only
> its own policy.js (importScripts, packaged), and uses no eval or new Function.

## Data usage checkboxes

The Chrome Web Store asks which user data the extension *handles*, which includes data that never
leaves the device (User Data FAQ, Q3). Declare these:

| category | tick? | what siphon handles |
|---|---|---|
| Personally identifiable information | no | none |
| Health information | no | none |
| Financial and payment information | no | none |
| **Authentication information** | **yes** | the access token for the user's own siphon server, typed in by the user, stored in chrome.storage.local, sent only to that server as a Bearer header |
| Personal communications | no | none |
| Location | no | none |
| **Web history** | **yes** | URLs of media requests on the current tab (session only); the page URL and stream URL of a download the user starts, sent to the user's own siphon app or server |
| User activity | no | no click, keystroke or scroll monitoring. Only clicks on siphon's own button are handled |
| **Website content** | **yes** | `<video>` source URLs, the page title, and X photo URLs, read on the page to place the button and name the download |

Certify all three statements. Each one is true:
- I do not sell or transfer user data to third parties, outside of the approved use cases
- I do not use or transfer user data for purposes that are unrelated to my item's single purpose
- I do not use or transfer user data to determine creditworthiness or for lending purposes

Why the Limited Use and encryption rules are met: the only off-device destinations are the user's
own siphon app (a local process) and a server the user names. The User Data FAQ (Q15) puts
user-specified servers outside Limited Use. siphon has no developer-operated server, no analytics
and no telemetry. Recommend HTTPS for the box URL anyway: Tailscale's `https://*.ts.net` URLs, or
`siphon serve --cloudflare`. A plain `http://` box URL would send the token unencrypted, and that
is the user's own choice.

## Privacy policy URL

`https://github.com/antharmaya/siphon/blob/master/store/PRIVACY.md`. This only resolves after
`store/` is committed and pushed.
