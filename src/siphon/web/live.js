// Live job updates: Server-Sent Events, falling back to polling when the stream can't get through.
//
// Cloudflare quick tunnels buffer an event stream until it closes ("Quick Tunnels do not support
// Server-Sent Events"; measured: six events, one a second, all delivered at once at the end), so
// through `siphon serve --cloudflare` the stream opens and then says nothing. The server's first
// message is always a snapshot, so silence past `firstWait` means buffering or a dead stream: close
// it and poll /api/jobs. Closing matters: a buffered stream that is later flushed would replay a
// stale snapshot over newer state. SSE is retried now and then (one request, dropped after
// `firstWait` if silent) because over Tailscale or plain HTTP it works, and it is instant.
// No DOM in here, so tests/test_serve.py can drive it under node with fake timers.
"use strict";

function liveUpdates({ connect, fetchJobs, onSnapshot, onJob, busy, timers = globalThis,
                       firstWait = 3000, fast = 1500, slow = 10000, retry = 60000 }) {
  let es = null, waitT = null, pollT = null, retryT = null, polling = false, inflight = false, stopped = false;

  function sse() {
    if (stopped) return;
    if (es) es.close();
    timers.clearTimeout(waitT);
    const s = es = connect();
    let heard = false;
    const heardFrom = () => {
      if (heard) return;
      heard = true;
      timers.clearTimeout(waitT);
      stopPolling();
    };
    s.addEventListener("snapshot", (e) => { heardFrom(); onSnapshot(JSON.parse(e.data)); });
    s.addEventListener("job", (e) => { heardFrom(); onJob(JSON.parse(e.data)); });
    s.onerror = () => { if (s === es) fallBack(); };  // a closed or refused stream; EventSource would retry blind
    waitT = timers.setTimeout(() => { if (!heard && s === es) fallBack(); }, firstWait);
  }

  function fallBack() {
    if (es) { es.close(); es = null; }
    timers.clearTimeout(waitT);
    timers.clearTimeout(retryT);
    if (stopped) return;
    retryT = timers.setTimeout(sse, retry);
    if (!polling) { polling = true; tick(); }
  }

  function stopPolling() {
    polling = false;
    timers.clearTimeout(pollT);
    timers.clearTimeout(retryT);
  }

  async function tick() {
    timers.clearTimeout(pollT);
    if (!polling || inflight) return;
    inflight = true;
    let list = null;
    try { list = await fetchJobs(); } catch { /* the network blinked: next tick tries again */ }
    inflight = false;
    if (!polling) return;  // SSE came back while this was in flight; its snapshot is newer
    if (list) onSnapshot(list);
    pollT = timers.setTimeout(tick, busy() ? fast : slow);
  }

  return {
    start: sse,
    poke() { if (polling) tick(); },  // something just changed here (a job was added): don't wait out a slow tick
    stop() { stopped = true; stopPolling(); if (es) { es.close(); es = null; } timers.clearTimeout(waitT); },
    get mode() { return stopped ? "stopped" : polling ? "polling" : "sse"; },
  };
}
