# logout search — default search engine extension

One job: add `search.logout.cloud` to the browser's search engines and offer to make it the
default. A manifest and icons — no script, no permission, no host access. Separate from
`apps/search/extension/` (native messaging to the local engine) because both stores want an
extension that changes the search engine to do only that.

| Browser | What installing does |
|---|---|
| Firefox (desktop) | adds "logout", asks whether to make it the default |
| Chrome / Edge / Brave on Windows and macOS | adds "logout", makes it the default, asks to keep it |
| Chrome-family on Linux | **nothing** — `chrome_settings_overrides` is not honoured there; use the steps on `/default.html` |
| Firefox for Android, Safari | not supported |

The values match `infra/search-public/public/opensearch.xml` — change the two together:
search `https://search.logout.cloud/#q={searchTerms}` (after `#`, so the search is not in the
address the browser requests), suggestions `/api/suggest` (gateway: phrases only, not recorded).

## Package

    cd apps/search/extension-default && zip -r -X ../logout-search-default.zip manifest.json icons

The same zip goes to both stores.

## Publish (not done — needs the owner's accounts)

- **addons.mozilla.org**: free. Submit the zip as a listed add-on. `data_collection_permissions`
  says `none`: the extension itself sends nothing; the browser sends searches to the site once
  the user picks it. If the reviewer reads that differently, the answer is `searchTerms`.
- **Chrome Web Store**: a developer account costs a one-time US$5 registration fee — ask before
  paying. Listing needs a privacy policy link (the Privacy note on the site) and the
  single-purpose statement ("sets the default search engine to search.logout.cloud").
- **Edge Add-ons**: free, takes the same zip.

After a listing is live, put its address in `STORE` at the top of
`infra/search-public/public/default.js`; the install button on `/default.html` stays hidden
until then.

## Not tested in a browser yet

Loading it unpacked (`about:debugging` → This Firefox → Load Temporary Add-on;
`chrome://extensions` → Developer mode → Load unpacked) and searching from the address bar: the
search must open the results page, and typing must show phrases from `/api/suggest`.

Icons: the site's mark (`public/favicon.svg`) on a white tile, drawn with the site's Outfit font.
