// logout search — local: relays search.logout.cloud's requests to the native host
// "cloud.logout.search" (the search engine on this computer) over native messaging.
//
// One long-lived port: the host loads its models once, and an open native port keeps this
// service worker alive while the page is open. Replies are matched by id.
"use strict";
const api = globalThis.browser || globalThis.chrome;
const HOST = "cloud.logout.search";
let port = null;
const waiting = new Map();   // id -> sendResponse

function connect() {
  if (port) return port;
  port = api.runtime.connectNative(HOST);
  port.onMessage.addListener((msg) => {
    const done = msg && waiting.get(msg.id);
    if (done) { waiting.delete(msg.id); done(msg); }
  });
  port.onDisconnect.addListener(() => {
    // host missing, crashed, or not installed: fail everything in flight
    const why = (api.runtime.lastError && api.runtime.lastError.message) || "host disconnected";
    for (const done of waiting.values()) done({ error: why });
    waiting.clear();
    port = null;
  });
  return port;
}

let seq = 0;
api.runtime.onMessage.addListener((req, sender, sendResponse) => {
  // only the content script on search.logout.cloud talks to us
  if (!sender.url || !sender.url.startsWith("https://search.logout.cloud/")) return false;
  const id = "m" + (++seq);
  const msg = req && req.ping ? { id, ping: true }
            : req && req.info ? { id, info: true }
            : req && req.details ? { id, details: true }
            : req && req.op === "get_settings" ? { id, get_settings: true }
            : req && req.op === "set_settings" ? { id, set_settings: req.settings || {} }
            : req && req.op === "news_more" ? { id, news_more: true, q: String(req.q || "").slice(0, 200),
                                                offset: Math.min(Math.max(parseInt(req.offset, 10) || 0, 0), 100) }
            : req && req.op === "explain" ? { id, explain: true, q: String(req.q || "").slice(0, 200), near: String(req.near || "").slice(0, 40) }
            : req && req.op === "radar" ? Object.assign(
                { id, radar: /^[A-Z]{4}$/.test(String(req.station || "")) ? String(req.station) : "" },
                Number.isInteger(req.t) && req.t > 0 ? { t: req.t } : {})
            : { id, q: String((req && req.q) || "").slice(0, 200), results: !!(req && req.results), full: !!(req && req.full),
                 near: String((req && req.near) || "").slice(0, 40) };
  waiting.set(id, sendResponse);
  try {
    connect().postMessage(msg);
  } catch (e) {
    waiting.delete(id);
    sendResponse({ error: String(e && e.message || e) });
    return false;
  }
  setTimeout(() => { const d = waiting.get(id); if (d) { waiting.delete(id); d({ error: "timeout" }); } }, 10000);
  return true;   // answer asynchronously
});
