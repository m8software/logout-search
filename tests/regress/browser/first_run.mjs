// First-run case, in a real (headless) browser: right after setup, the search page shows the engine pill and a search is
// answered by the engine over the link, with ZERO requests to the public site's /api/instant.
//   node first_run.mjs [--page https://search.logout.cloud/] [--query gmail] [--expect-card Gmail] [--wait 45] [--chrome PATH]
// Needs playwright-core (npm i playwright-core) and a Chrome: the hosted runners have one (channel "chrome"); elsewhere
// --chrome PATH. Exit 0 pass, 1 fail, 77 skipped (no playwright or no browser). Prints one JSON line with the evidence.
// What it checks, in order: 1 the pill (#mode) appears within --wait s and does not say public; 2 the search box answers: the
// results area shows the card for --expect-card; 3 no request went to /api/instant (nor /api/more) on the page's own site.
import { createRequire } from "node:module";
const arg = (k, d) => { const i = process.argv.indexOf("--" + k); return i > 0 ? process.argv[i + 1] : d; };
const page_url = arg("page", "https://search.logout.cloud/"), query = arg("query", "gmail"), expect = arg("expect-card", "Gmail");
const wait = Number(arg("wait", "45")) * 1000, chrome = arg("chrome", "");
const out = { page: page_url, query };
let chromium;
try { ({ chromium } = createRequire(import.meta.url)("playwright-core")); } catch (e) { console.log(JSON.stringify({ ...out, skip: "playwright-core is not installed" })); process.exit(77); }
let browser;
try {
  browser = await chromium.launch(chrome ? { executablePath: chrome, headless: true, args: ["--no-sandbox"] } : { channel: "chrome", headless: true });
} catch (e) { console.log(JSON.stringify({ ...out, skip: "no browser: " + String(e).slice(0, 160) })); process.exit(77); }
const fails = [];
const ctx = await browser.newContext();
const pg = await ctx.newPage();
const toPublic = [];
const origin = new URL(page_url).origin;
pg.on("request", (r) => { const u = new URL(r.url()); if (u.origin === origin && /^\/api\/(instant|more)/.test(u.pathname)) toPublic.push(u.pathname + u.search.slice(0, 60)); });
const t0 = Date.now();
await pg.goto(page_url, { waitUntil: "domcontentloaded" });
try {
  await pg.waitForSelector("#mode:not([hidden])", { timeout: wait });
  out.pill = (await pg.textContent("#mode") || "").trim();
  out.pill_ms = Date.now() - t0;
  if (/public/i.test(out.pill)) fails.push(`the pill says public (${out.pill})`);
} catch (e) { fails.push(`no engine pill within ${wait / 1000} s`); }
await pg.fill("#q", query);
await pg.press("#q", "Enter");
const t1 = Date.now();
try {
  await pg.waitForSelector("#out .card-head h2", { timeout: 60000 });
  out.card = (await pg.textContent("#out .card-head h2") || "").trim();
  out.answer_ms = Date.now() - t1;
  if (!out.card.toLowerCase().includes(expect.toLowerCase())) fails.push(`card ${JSON.stringify(out.card)} is not ${JSON.stringify(expect)}`);
} catch (e) { fails.push("no card in the results area within 60 s"); out.results_text = ((await pg.textContent("#out").catch(() => "")) || "").slice(0, 160); }
out.requests_to_public_api = toPublic;
if (toPublic.length) fails.push(`${toPublic.length} request(s) to ${origin}/api/...: ${toPublic.slice(0, 3).join(" ")}`);
out.fails = fails;
console.log(JSON.stringify(out));
await browser.close();
process.exit(fails.length ? 1 : 0);
