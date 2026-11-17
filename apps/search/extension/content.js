// logout search — local: runs only on https://search.logout.cloud/*.
// Pings the native host; if it answers, marks the page (<html data-logout-local="1">) so the
// page sends its searches here instead of to the site, and relays them:
//   page -> window.postMessage({type: "logout-search", id, q, results})
//   page <- window.postMessage({type: "logout-search-reply", id, body})
"use strict";
(() => {
  const api = globalThis.browser || globalThis.chrome;
  const send = (m) => new Promise((resolve) => {
    try { api.runtime.sendMessage(m, (r) => resolve(r || { error: (api.runtime.lastError || {}).message || "no reply" })); }
    catch (e) { resolve({ error: String(e) }); }
  });

  window.addEventListener("message", async (ev) => {
    if (ev.source !== window || ev.origin !== location.origin) return;
    const d = ev.data;
    if (!d || d.type !== "logout-search" || typeof d.id !== "string") return;
    if (document.documentElement.dataset.logoutLocal !== "1") return;   // host not confirmed
    if (d.op === "get_settings" || d.op === "set_settings") {
      const r = await send({ op: d.op, settings: d.settings });
      window.postMessage({ type: "logout-search-reply", id: d.id, body: r }, location.origin);
      return;
    }
    if (d.op === "details") {                         // the person clicked the badge: name, LAN address, index size
      let r = await send({ details: true });
      if (!r.details) r = await send({ info: true }); // a host from before has them under "info"
      window.postMessage({ type: "logout-search-reply", id: d.id, body: { details: r.details || r.info || null } }, location.origin);
      return;
    }
    if (d.op === "news_more") {                       // "More news": the next page of headlines
      const r = await send({ op: "news_more", q: String(d.q || "").slice(0, 200), offset: d.offset });
      window.postMessage({ type: "logout-search-reply", id: d.id, body: r.body || { news: [], more: false, error: r.error || "local engine error" } },
                         location.origin);
      return;
    }
    if (d.op === "explain") {                         // "How this was ranked": the steps of one search
      const r = await send({ op: "explain", q: String(d.q || "").slice(0, 200), near: typeof d.near === "string" ? d.near.slice(0, 40) : "" });
      window.postMessage({ type: "logout-search-reply", id: d.id, body: r.body || { steps: [], error: r.error || "local engine error" } },
                         location.origin);
      return;
    }
    if (d.op === "radar") {                           //: local radar manifest or frame from the box
      const r = await send({ op: "radar", station: d.station, t: d.t });
      window.postMessage({ type: "logout-search-reply", id: d.id, body: r.body !== undefined ? r.body : null },
                         location.origin);
      return;
    }
    const r = await send({ q: String(d.q || ""), results: !!d.results, full: !!d.full, near: typeof d.near === "string" ? d.near.slice(0, 40) : "" });
    window.postMessage({ type: "logout-search-reply", id: d.id, body: r.body || { suggest: [], results: [], error: r.error || "local engine error" } },
                       location.origin);
  });

  // Synchronously at document_start, before the page's own script runs. The last verdict is kept in this site's
  // storage: a computer whose host answered last time is marked "1" at once, so the page's first search goes to
  // the host with no wait; the ping below corrects it. Anything else is "pending": the page waits for the verdict
  // instead of sending early keystrokes to the site. A stale "1" never reaches the site either: the page then asks
  // the host, which answers with an error, and the mark is removed when the ping fails.
  const LAST = "logout-local-last";
  const last = (() => { try { return localStorage.getItem(LAST); } catch (_) { return null; } })();
  const remember = (v) => { try { if (v) localStorage.setItem(LAST, v); else localStorage.removeItem(LAST); } catch (_) { /* no storage */ } };
  document.documentElement.dataset.logoutLocal = last === "1" ? "1" : "pending";
  send({ ping: true }).then((r) => {
    if (r && r.pong) {
      document.documentElement.dataset.logoutLocal = "1";
      remember("1");
      // how the engine is reached (the badge reads "relay" in it) and nothing else: a host from before
      // answers with its name, LAN address and index size too, and those stay out of the page until asked for
      send({ info: true }).then((i) => {
        if (i && i.info) document.documentElement.dataset.logoutInfo = JSON.stringify(
          { via: typeof i.info.via === "string" ? i.info.via : "", host_version: i.info.host_version });
      });
    } else {                                                           // no host: public mode
      delete document.documentElement.dataset.logoutLocal;
      remember(null);
    }
  });
})();
