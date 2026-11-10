// The one reader for every poll. It keeps each URL's last answer and ETag in memory only (never the browser's disk cache), sends the
// ETag back, and on a 304 reuses the answer with the server's own top-level stamps from their headers: not the time of the request,
// since a state's as_of is herdr's last good read.
const POLL = (() => {
  const STAMPS = { as_of: "X-Atlas-As-Of", last_look: "X-Atlas-Last-Look", read_at: "X-Atlas-Read-At" }; // web.py STAMPS
  const seen = new Map();
  // deadline: ms for this read; signal: optional, to cancel it sooner. Throws on any status but 200 and 304; the error has .status.
  async function get(url, deadline, signal) {
    const old = seen.get(url);
    const ctl = new AbortController(), forward = () => ctl.abort(signal.reason);
    // The deadline's reason is the TimeoutError a fetch given AbortSignal.timeout would throw.
    const timer = setTimeout(() => ctl.abort(new DOMException("signal timed out", "TimeoutError")), deadline);
    if (signal) { if (signal.aborted) forward(); else signal.addEventListener("abort", forward, { once: true }); }
    try {
      const res = await fetch(url, { cache: "no-store", signal: ctl.signal, headers: old ? { "If-None-Match": old.etag } : {} });
      if (res.status === 304 && old) {
        if (!old.body || Array.isArray(old.body)) return old.body;
        const out = { ...old.body };
        for (const [k, h] of Object.entries(STAMPS)) { const v = res.headers.get(h); if (v) out[k] = v; }
        return out;
      }
      if (!res.ok) throw Object.assign(new Error(`${url}: ${res.status}`), { status: res.status });
      const body = await res.json(), etag = res.headers.get("ETag");
      if (etag) seen.set(url, { etag, body });
      else seen.delete(url);
      return body;
    } finally { // the deadline covers the body too, as AbortSignal.timeout did
      clearTimeout(timer);
      if (signal) signal.removeEventListener("abort", forward);
    }
  }
  return { get };
})();
