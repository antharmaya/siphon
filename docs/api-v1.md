# siphon engine API, v1

The one interface between the siphon engine (the daemon, today `siphon serve`) and everything
that drives it: the desktop app, the CLI, the browser extension's host, the phone page, scripts.
Clients never touch the job database or spawn downloads themselves. This file is the contract;
`tests/test_api_contract.py` checks the engine against it.

Status: **draft**, being built 2026-10. Items marked *(planned)* are not served yet.

## Rules that never change within v1

1. **Additive only.** v1 may gain endpoints, fields, event kinds and job states. It never
   removes or renames one, or changes a field's type or meaning. Clients ignore what they don't
   know. Anything that breaks this is v2, served alongside v1 until old clients are gone.
2. **Ask, don't assume.** A client reads `GET /api/v1/status` first and uses `features` to decide
   what to offer. It never infers support from `version`. (A host that assumed its CLI knew a flag
   broke every browser click on 2026-10-17.)
3. **Intent, not flags.** Clients describe what they want (URL, preset, destination). The engine
   turns that into its own arguments from an allowlist. No endpoint accepts raw CLI flags or paths.
4. **The engine owns state.** Every change goes through the API, so every change produces an event.

## Transport and access

- HTTP/1.1 on `127.0.0.1:8765` by default. Remote reach (Tailscale, a tunnel) is opt-in and
  requires the same token or a paired session.
- **Token** (`Authorization: Bearer <token>`): local clients read it from
  `$SIPHON_STATE/serve.json` (mode 0600, owner only); a browser extension or phone gets it once
  from `siphon serve token`.
- **Paired session cookie**: the phone page after pairing. Non-GET requests with a cookie must send
  `X-Siphon: 1` and a same-origin `Origin` (blocks cross-site requests).
- **Host check**: requests whose `Host` isn't loopback or an `--allow-host` name are refused
  (blocks DNS rebinding).
- `/api/*` (unversioned) stays as an alias of `/api/v1/*` for clients already in the stores.

## Resources

### Status
`GET /api/v1/status` →
```json
{"version": "0.4.0", "api": 1, "host": "fed", "downloads": "/home/u/Downloads",
 "features": ["jobs", "events.resume", "jobs.priority", "jobs.category", "jobs.tags", "jobs.pause", "jobs.edit"],
 "destinations": ["device", "box"], "clouds": [...], "capabilities": [...]}
```

### Jobs
A job is one thing the user asked for (a URL); it may produce many files.

| field | type | notes |
|---|---|---|
| `id` | string | 8 hex chars, stable forever |
| `url` | string | what was asked for |
| `status` | string | `pending`, `active`, `paused`, `done`, `error` (more may be added) |
| `stage` | string | finer detail while active: `queued`, `resolving`, `downloading`, `uploading`, `saved` |
| `priority` | int | higher runs first; default 0 |
| `category` | string or null | user's label, e.g. `movies` |
| `tags` | string[] | |
| `added`, `started`, `finished` | string | local ISO time, `YYYY-MM-DDTHH:MM:SS` |
| `files` | string[] | saved paths on the engine's machine |
| `size` | int or null | total bytes of `files`, once done |
| `remote` | string[] | cloud copies |
| `error` | string or null | last error, human-readable |
| `progress` | object or null | live only: `percent`, `speed`, `done`, `total` |

- `GET /api/v1/jobs` → jobs, newest first.
- `POST /api/v1/jobs` `{"url", "preset"?, "audio"?, "format"?, "section"?, "sponsorblock"?,
  "playlist"?, "dest"?, "priority"?, "category"?, "tags"?, "video"?}` → 201 job.
  `video: true` means the user picked a video (not the post's images), so the engine skips the
  image-post check (feature `jobs.video`).
- `GET /api/v1/jobs/{id}` → job.
- `PATCH /api/v1/jobs/{id}` `{"priority"?, "category"?, "tags"?}` → job. Labels only; `category: null` clears it.
- `POST /api/v1/jobs/{id}/pause` → job. A pending job leaves the queue; a running one is stopped.
  Plain-file (segmented) downloads continue from their checkpoint on resume. Whether a yt-dlp
  download continues or restarts is not yet verified; an HLS/DASH recording through ffmpeg starts
  over. 409 if the job is done or errored.
- `POST /api/v1/jobs/{id}/resume` → job, back to `pending`. 409 unless paused.
- `POST /api/v1/jobs/{id}/retry` → job.
- `DELETE /api/v1/jobs/{id}` → `{"ok": true}`. Removes the job; files stay on disk.
- `GET /api/v1/jobs/{id}/files/{n}` → the file's bytes.

### Events
`GET /api/v1/events` is a server-sent event stream.

- Each **stored** change is an event with an `id:` (a number that only grows, kept in the job
  database, so it survives a restart):
  - `job` — a job was added or changed; data is the full job.
  - `removed` — `{"id": ...}`.
- **Live progress** (`progress` events) has no `id`: it is too frequent to store, and a fresh
  `snapshot` already carries current progress.
- On connect the engine sends `snapshot` (all jobs, with `id:` set to the latest event number).
- A reconnecting client sends `Last-Event-ID: <n>` (browsers do this automatically). The engine
  replays every stored event after `n`, or sends a fresh `snapshot` if `n` is too old to replay.
- A `: ping` comment every 15 s keeps proxies from closing the stream.

## Errors
Non-2xx responses are `{"error": "<human-readable message>"}` with a fitting status: 400 bad input,
401 not authorised, 403 refused (host, origin), 404 no such thing, 409 conflict, 413 too large.
