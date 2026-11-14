"""Intent slots and site-search bridges for "name + intent words" searches (2026-11-10).

"nike returns", "chase login", "github status": the search names a thing and what the person wants there. A slot
table holds, per site, the address of each common intent (built offline by tools/site-slots.py from the site's own
home-page links, same registered domain only). A second table holds, per site, the address of the site's own search
(tools/site-nav.py search: schema.org SearchAction / OpenSearch), for "sony wh-1000xm5" -> "Search sony.com for ...".

This module is pure (no database, no network, no psycopg), so the tools and the unit tests import the very code the
engine runs. search_api.py calls apply_slots() / apply_bridge() on the shown list after the Wikipedia mix, behind
SEARCH_SITE_SLOTS and SEARCH_SITE_BRIDGE (both off by default).

Rules that hold everywhere:
  * an exact title for the WHOLE query wins ("apple pay", "amazon music", "target field" are never split);
  * a slot or bridge URL is always on the entity's own registered domain (checked again at load, never trusted);
  * nothing left over after the name = no row; the leftover words are URL-encoded and put in one placeholder.
"""
from __future__ import annotations

import json
import re
import unicodedata
from urllib.parse import quote, urlsplit

_WORD = re.compile(r"[a-z0-9]+")
_FILLER_LEAD = frozenset(("my", "the", "your", "customer", "official"))
_FILLER_TAIL = frozenset(("page", "online", "website", "site", "portal", "link", "number"))


def _plain(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def words(s: str) -> list[str]:
    return _WORD.findall(_plain(s))


def norm_key(s: str) -> str:
    """Letters and digits only, lowercase - search_api.norm_key."""
    return "".join(_WORD.findall((s or "").lower()))


# ---- the vocabulary ----------------------------------------------------------------------------------------------
# id | label shown | what a person types after/before the name (empty: the slot is nav-only and never answers a search)
# | the link texts that fill it (whole text, lower case; English first, then the commonest other languages).
# Counted over 15,732 fetched home pages (docs in tools/site-slots.py report); the grouping is by hand from that table.
# A text belongs to ONE slot (first listed wins); product aisles ("shoes", "jeans"), languages and numbers are no slot.
_SLOT_DEF = [
    ("login", "Log in", "login|log in|sign in|signin|sign-in|member login|customer login|client login|portal login|staff login",
     "log in|login|sign in|signin|member login|customer login|client login|portal login|staff login|website sign in|"
     "online banking|my login|anmelden|einloggen|kundenportal|connexion|se connecter|iniciar sesion|iniciar sesión|inloggen|accedi|mijn account inloggen"),
    ("account", "My account", "account|my account|manage account|manage my account",
     "my account|account|your account|manage account|mon compte|mein konto|mi cuenta|mijn account|mon espace|min side|accounts|profile"),
    ("signup", "Sign up", "sign up|signup|register|create account|join|open an account",
     "sign up|register|create an account|create account|join now|join|open an account|become a member|registrieren|jetzt anmelden|"
     "create a website account|registration|registro|inscription|mitglied werden"),
    ("contact", "Contact us", "contact|contact us|contact info|phone number|customer service number|email",
     "contact us|contact|kontakt|contactez-nous|nous contacter|contacto|contáctanos|contactos|contatti|contattaci|contacts|get in touch|"
     "contact information|kontaktformular|kontakty|kontakt oss|お問い合わせ|contacte-nos"),
    ("support", "Support", "support|help|help center|customer service|customer support|customer care|help desk",
     "support|help|help center|help centre|customer service|customer support|customer care|service|klantenservice|kundenservice|aide|ayuda|hilfe|"
     "help & support|assistance|servicio al cliente|centre d'aide|hilfe & kontakt"),
    ("faq", "FAQ", "faq|faqs|frequently asked questions",
     "faq|faqs|faq's|frequently asked questions|preguntas frecuentes|veelgestelde vragen|häufige fragen|foire aux questions|domande frequenti|how it works"),
    ("returns", "Returns", "returns|return policy|refund policy|refunds|exchanges|returns and exchanges|return",
     "returns|return policy|refund policy|returns & exchanges|returns and exchanges|exchanges|refunds|refund|retours|devoluciones|widerrufsrecht|"
     "vertrag widerrufen|shipping & returns|return an item|start a return"),
    ("shipping", "Shipping", "shipping|delivery|shipping policy|shipping info|delivery info|shipping rates",
     "shipping|delivery|shipping policy|shipping info|shipping information|delivery information|shipping & delivery|livraison|versand|envíos|envios|"
     "shipping rates"),
    ("tracking", "Track an order", "track order|track my order|order status|order tracking|track package|track a package|where is my order",
     "track order|track my order|track your order|order status|order tracking|track a package|track package|track shipment|suivre ma commande|sendungsverfolgung|"
     "orders|my orders"),
    ("pricing", "Pricing", "pricing|plans|prices|plans and pricing|rates|fees|price list|cost",
     "pricing|plans|prices|plans & pricing|plans and pricing|rates|fees|price list|tarifs|preise|precios|prijzen|tariffe"),
    ("careers", "Careers", "careers|jobs|career|employment|job openings|work with us|vacancies|hiring",
     "careers|career|jobs|job openings|job opportunities|employment|employment opportunities|work with us|join our team|join us|join the team|vacancies|"
     "current vacancies|career opportunities|karriere|stellenangebote|offene stellen|jobs & karriere|emplois|carrières|recrutement|nous rejoindre|"
     "trabaja con nosotros|lavora con noi|werken bij|vacatures|ledige stillinger|kariera|kariéra|internships|human resources|recruitment|"
     "jobs & salary info|work for us"),
    ("docs", "Documentation", "docs|documentation|api|api docs|developer|developers|developer docs|guides|api reference|sdk",
     "docs|documentation|developers|developer|developer portal|api|api documentation|api reference|guides|developer docs|sdk|dokumentation"),
    ("status", "Status", "status|system status|service status|uptime|outage|outages|is it down",
     "status|system status|service status|uptime|outages|report an outage"),
    ("download", "Download", "download|downloads|get the app|app download|install",
     "download|downloads|download the app|get the app|download app|télécharger|herunterladen|descargas|descargar"),
    ("app", "Mobile app", "app|mobile app|apps",
     "mobile app|app|apps|mobile apps|mobile|nos applications"),
    ("locations", "Store locator", "store locator|find a store|locations|stores|find us|near me|branches|find a location|location|dealer locator",
     "locations|location|find a store|store locator|stores|find us|find a location|our locations|store locations|store finder|branch locator|"
     "find a branch|all locations|branches|filialen|standorte|find your store|nos magasins|nos boutiques|boutiques|winkels|filiales|"
     "view all locations|our stores|ubicaciones|sedi|dealer locator|find a dealer"),
    ("hours", "Hours", "hours|opening hours|store hours|business hours|open hours",
     "hours|opening hours|store hours|hours & locations|öffnungszeiten|horaires|horarios|business hours"),
    ("investors", "Investors", "investors|investor relations|ir|shareholders|stock|annual report|sec filings",
     "investors|investor relations|investor|financial information|annual reports|annual report|financial reports|shareholders|financials|"
     "investoren|investisseurs|inversores|aandeelhouders"),
    ("press", "Press", "press|newsroom|press releases|media|media kit|press kit|press room|media center",
     "press|newsroom|press releases|press release|press room|media|media center|media centre|media kit|presse|pressemitteilungen|espace presse|"
     "news releases|media releases|news room|sala de prensa|stampa|in the news|media contacts|media resources|news & media"),
    ("cancel", "Cancel", "cancel|cancellation|cancel subscription|cancel membership|cancellation policy|unsubscribe",
     "cancel|cancellation|cancel subscription|cancellation policy|cancel membership|vertrag kündigen|resilier|résilier|cancelar"),
    ("paybill", "Pay a bill", "pay bill|pay my bill|pay a bill|make a payment|pay online|bill pay|payments|billing|pay now|pay your bill",
     "pay bill|pay my bill|pay your bill|make a payment|pay online|bill pay|payments|billing|pay now|pay|online payments|payment options|pay a bill|"
     "understanding your bill|rechnung|paiement|pagos"),
    ("giftcards", "Gift cards", "gift cards|gift card|giftcard|gift card balance|check gift card balance",
     "gift cards|gift card|giftcards|gift certificates|e-gift cards|egift cards|cartes cadeaux|gutscheine|tarjetas regalo"),
    ("menu", "Menu", "menu|menus|food menu|drink menu|drinks menu|dinner menu|lunch menu",
     "menu|menus|our menu|food menu|view menu|view full menu|food & drink|speisekarte|carte|carta|menü"),
    ("order", "Order online", "order online|order now|online ordering|order food|order delivery|order",
     "order online|order now|order|order catering|online ordering|commander|bestellen|pedir online"),
    ("reservations", "Reservations", "reservations|reservation|book a table|make a reservation|booking|book now|appointments|book appointment|book an appointment",
     "reservations|reservation|book a table|book now|book|booking|bookings|book an appointment|schedule an appointment|make an appointment|appointments|"
     "termin vereinbaren|reserver|réserver|reservar|prenota|termine"),
    ("tickets", "Tickets", "tickets|buy tickets|ticket|book tickets|ticket prices",
     "tickets|buy tickets|book tickets|ticket|billets|entradas|karten|biglietti|tickets & hospitality"),
    ("events", "Events", "events|event calendar|calendar|upcoming events|whats on|what's on",
     "events|event calendar|events calendar|calendar|upcoming events|view full calendar|view all events|see all events|all events|what's on|agenda|"
     "veranstaltungen|veranstaltungskalender|eventi|eventos|community events|special events|calendars|district calendar|termine"),
    ("schedule", "Schedule", "schedule|fixtures|game schedule|matches|results|timetable",
     "schedule|fixtures|matches|fixtures & results|results|timetable|game schedule|spielplan|calendrier|horario|calendario"),
    ("shop", "Shop", "shop|online store|merch|merchandise|official store|shop online|webshop",
     "shop|shop now|shop all|online store|shop online|store|merch|merchandise|official store|official shop|shop merch|webshop|e-shop|eshop|online shop|"
     "boutique|tienda|shop our products|shop all products|all products|products"),
    ("about", "About", "about|about us|our story|who we are|history|our history",
     "about us|about|who we are|our story|our company|company|our history|history|our mission|what we do|over ons|über uns|unternehmen|qui sommes-nous ?|"
     "quiénes somos|nosotros|chi siamo|notre histoire|om oss|o nás|o nas|o nama|nous connaître|historie|geschichte|mission|our values|our vision|organisation"),
    ("team", "Leadership", "leadership|executives|management|board of directors|our team|executive team|meet the team|board",
     "leadership|leadership team|our team|team|meet the team|meet our team|executive team|management|board of directors|board members|board|"
     "our leadership|our people|people|staff|trustees|board of trustees|governance|corporate governance"),
    ("news", "News", "",
     "news|latest news|newsletter|news & events|all news|more news|view all news|see all news|nieuws|nachrichten|actualités|noticias|aktuelles|notizie|"
     "news and events|news & updates|news & announcements|nyheter|novinky|aktualności|notícias|voir toutes les actualités|nieuwsbrief|newsletters"),
    ("blog", "Blog", "blog",
     "blog|blogs|our blog|magazin|magazine|stories|articles|insights|блог"),
    ("privacy", "Privacy policy", "privacy|privacy policy|privacy notice|data protection|privacy statement",
     "privacy policy|privacy|privacy notice|privacy statement|datenschutz|datenschutzerklärung|politique de confidentialité|política de privacidad|"
     "política de privacidade|privacyverklaring|personvern|personvernerklæring|polityka prywatności|données personnelles|your privacy choices|privacy center|"
     "do not sell my personal information|data protection|gdpr"),
    ("terms", "Terms", "terms|terms of use|terms and conditions|terms of service|tos|conditions",
     "terms of use|terms & conditions|terms and conditions|terms of service|terms|agb|términos y condiciones|nutzungsbedingungen|algemene voorwaarden|"
     "conditions générales de vente|cgv|cgu|terms & conditions of use"),
    ("cookies", "Cookie policy", "cookie policy|cookies|cookie settings|cookie notice",
     "cookie policy|cookies|cookies policy|política de cookies|cookie settings|cookie notice"),
    ("legal", "Legal notice", "legal|imprint|impressum|legal notice|disclaimer",
     "legal|impressum|imprint|legal notice|legal notices|mentions légales|aviso legal|disclaimer|copyright|legal information|modern slavery statement"),
    ("accessibility", "Accessibility", "accessibility|accessibility statement",
     "accessibility|accessibility statement|accessibility policy|web accessibility|website accessibility|barrierefreiheit|accessibilité|accesibilidad|toegankelijkheid|accessibilità"),
    ("sitemap", "Site map", "sitemap|site map",
     "sitemap|site map|plan du site|mapa del sitio|sitemap xml|site links"),
    ("sustainability", "Sustainability", "sustainability|esg|corporate responsibility|csr|environment",
     "sustainability|esg|corporate responsibility|corporate social responsibility|csr|social responsibility|environment|nachhaltigkeit|sostenibilidad|"
     "our impact|impact|climate change"),
    ("donate", "Donate", "donate|donations|give|giving|ways to give|fundraising",
     "donate|donate now|donations|give|giving|support us|spenden|fundraising|make a donation"),
    ("volunteer", "Volunteer", "volunteer|volunteering|volunteer opportunities",
     "volunteer|volunteers|volunteering|volunteer opportunities|get involved|ehrenamt"),
    ("rewards", "Rewards", "rewards|loyalty|loyalty program|rewards program|membership|points",
     "rewards|loyalty|membership|loyalty program|members|become a member|rewards program"),
    ("deals", "Deals", "deals|offers|sale|promotions|coupons|specials|discounts|promo codes",
     "deals|offers|sale|promotions|coupons|specials|special offers|promos|clearance|outlet|angebote|aktionen|promociones|weekly ad"),
    ("franchise", "Franchise", "franchise|franchising|franchise opportunities",
     "franchise|franchising|franchise opportunities|devenir franchisé"),
    ("partners", "Partners", "partners|become a partner|affiliates|affiliate program|partner program",
     "partners|partner|our partners|become a partner|partner with us|affiliates|affiliate program|suppliers|partnerships"),
    ("admissions", "Admissions", "admissions|enrollment|enroll|apply|apply now|how to apply",
     "admissions|enrollment|enroll|enroll now|apply|apply now|how to apply|registration|inscriptions|anmeldung"),
    ("directions", "Directions", "directions|visit|plan your visit|getting here|parking|map",
     "directions|visit|visitors|plan your visit|getting here|parking|map|maps|how to find us|anfahrt|plan d'accès"),
    ("feedback", "Feedback", "feedback|leave feedback|complaints|report a problem",
     "feedback|website feedback|complaints|report a problem|report an issue|report a concern"),
    ("security", "Security", "security|trust|safety|trust center",
     "security|trust|safety|trust center|security & privacy|safety & security|safety and security"),
]


class Slot:
    __slots__ = ("id", "label", "asks", "texts")

    def __init__(self, sid: str, label: str, asks: str, texts: str):
        self.id, self.label = sid, label
        self.asks = tuple(a.strip() for a in asks.split("|") if a.strip())
        self.texts = tuple(t.strip() for t in texts.split("|") if t.strip())


SLOTS: list[Slot] = [Slot(*d) for d in _SLOT_DEF]
SLOT_BY_ID = {s.id: s for s in SLOTS}
# link text -> slot (first slot to list a text wins), and the typed words -> slot, by the words joined with one space
_TEXT_SLOT: dict[str, str] = {}
ASK_SLOT: dict[str, str] = {}
for _s in SLOTS:
    for _t in _s.texts:
        _TEXT_SLOT.setdefault(" ".join(words(_t)), _s.id)
    for _a in _s.asks:
        ASK_SLOT.setdefault(" ".join(words(_a)), _s.id)
_NOISE = re.compile(r"\s*\((?:opens?|link)[^)]*\)|\s*[›»>|:.]+\s*$|^\s*[›»>|·]+\s*", re.I)
_TEXT_FILL = frozenset(("us", "now", "here", "page", "centre", "center", "today", "our", "the", "your", "my"))


def slot_of_text(text: str) -> str | None:
    """The slot a link text fills, or None. Whole-text match after dropping a filler word at either end
    ("Contact Us", "Order Online", "Careers at ..." is NOT matched)."""
    t = " ".join(words(_NOISE.sub("", text or "")))
    if not t or len(t) > 40:
        return None
    if t in _TEXT_SLOT:
        return _TEXT_SLOT[t]
    w = t.split()
    if len(w) > 1 and w[0] in _TEXT_FILL and " ".join(w[1:]) in _TEXT_SLOT:
        return _TEXT_SLOT[" ".join(w[1:])]
    if len(w) > 1 and w[-1] in _TEXT_FILL and " ".join(w[:-1]) in _TEXT_SLOT:
        return _TEXT_SLOT[" ".join(w[:-1])]
    return None


def slot_of_query_words(w: list[str]) -> str | None:
    """The slot the leftover words of a search ask for: the whole leftover must be one slot's phrase (a filler
    word at the front - "my", "the" - or the back - "page", "online" - dropped when the rest matches)."""
    if not w or len(w) > 5:
        return None
    k = " ".join(w)
    if k in ASK_SLOT:
        return ASK_SLOT[k]
    if len(w) > 1 and w[0] in _FILLER_LEAD and " ".join(w[1:]) in ASK_SLOT:
        return ASK_SLOT[" ".join(w[1:])]
    if len(w) > 1 and w[-1] in _FILLER_TAIL and " ".join(w[:-1]) in ASK_SLOT:
        return ASK_SLOT[" ".join(w[:-1])]
    return None


# ---- registered domain ----------------------------------------------------------------------------------------------
_PRIVATE = frozenset(("github.io", "blogspot.com", "wordpress.com", "herokuapp.com", "vercel.app", "netlify.app", "pages.dev",
                      "web.app", "firebaseapp.com", "appspot.com", "azurewebsites.net", "myshopify.com", "wixsite.com",
                      "weebly.com", "tumblr.com", "gitlab.io", "fly.dev", "onrender.com"))
_SLD = re.compile(r"^(?:com|co|net|org|gov|edu|ac|or|ne|go|gob|nom|mil)$")


def reg_domain(host: str) -> str:
    p = (host or "").lower().strip(".").split(".")
    if p and p[0] == "www":
        p = p[1:]
    if len(p) <= 2:
        return ".".join(p)
    if ".".join(p[-2:]) in _PRIVATE or (len(p[-1]) == 2 and _SLD.match(p[-2])):
        return ".".join(p[-3:])
    return ".".join(p[-2:])


def host_of(url: str) -> str:
    try:
        h = (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def label_of(site: str) -> str:
    """The name part of a site: sony.com / sony.co.uk / shop.sony.com -> sony (the registered domain's first label)."""
    return reg_domain(site).split(".")[0]


# ---- the tables ---------------------------------------------------------------------------------------------------
class SiteTable:
    """site (host, no www.) -> payload, plus the name index: the registered domain's first label (letters and digits)
    -> hosts. Loaded from jsonl; a row whose URL is not https or not on the site's registered domain is dropped."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.by_label: dict[str, list[str]] = {}

    def _add(self, site: str, payload: dict) -> None:
        self.rows[site] = payload
        self.by_label.setdefault(norm_key(label_of(site)), []).append(site)

    def __len__(self) -> int:
        return len(self.rows)

    def hosts_named(self, name_key: str) -> list[str]:
        return list(self.by_label.get(name_key, ()))


def load_slots(path: str) -> SiteTable:
    """site-slots.jsonl rows: {"site": "nike.com", "slots": {"returns": {"text": "Returns", "url": "https://..."}}}."""
    t = SiteTable()
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
                site, rd = r["site"], reg_domain(r["site"])
                good = {}
                for sid, v in (r.get("slots") or {}).items():
                    u = str(v.get("url") or "")
                    if sid in SLOT_BY_ID and u.startswith("https://") and reg_domain(host_of(u)) == rd:
                        good[sid] = {"text": str(v.get("text") or SLOT_BY_ID[sid].label)[:40], "url": u}
                if good:
                    t._add(site, {"slots": good})
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
    return t


_TRACK = re.compile(r"^(?:utm_|mc_|ocid$|src$|ex_cid$|ref$|cmp$|cid$|icid$|fbclid$|gclid$|source$|campaign$)", re.I)


def clean_template(tpl: str) -> str:
    """The template without tracking parameters (utm_source=opensearch, ocid=..., src=mc_google); the one holding the
    placeholder is never dropped."""
    sp = urlsplit(tpl)
    if not sp.query:
        return tpl
    keep = [kv for kv in sp.query.split("&") if "{searchTerms}" in kv or not _TRACK.match(kv.split("=", 1)[0])]
    return sp._replace(query="&".join(keep)).geturl() if len(keep) != len(sp.query.split("&")) else tpl


def load_search(path: str) -> SiteTable:
    """site-search.jsonl rows: {"site": "sony.com", "template": "https://www.sony.com/search?q={searchTerms}"}."""
    t = SiteTable()
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
                site, tpl = r["site"], clean_template(str(r["template"]))
                if template_ok(tpl, site):
                    t._add(site, {"template": tpl})
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
    return t


def template_ok(tpl: str, site: str) -> bool:
    """https, the site's own registered domain, no credentials, the placeholder exactly once and not in the host."""
    try:
        sp = urlsplit(tpl)
    except ValueError:
        return False
    return (sp.scheme == "https" and bool(sp.hostname) and not sp.username and not sp.password
            and reg_domain(sp.hostname) == reg_domain(site) and tpl.count("{searchTerms}") == 1
            and "{searchTerms}" not in (sp.netloc or "") and len(tpl) <= 500 and tpl.count("{") == 1 and tpl.count("}") == 1)


def fill_template(tpl: str, text: str) -> str | None:
    """The template with the typed words in its one placeholder, percent-encoded as a whole (a space is %20, so
    the words work in a path or a query alike; & # / ? = + % never leave the value). None for empty or hostile text."""
    t = "".join(ch for ch in unicodedata.normalize("NFC", text or "") if ch.isprintable() or ch == " ")
    t = " ".join(t.split())[:100]
    if not t or tpl.count("{searchTerms}") != 1:
        return None
    return tpl.replace("{searchTerms}", quote(t, safe="", errors="replace"))


# ---- splitting a search into name + leftover ------------------------------------------------------------------------
def splits(q: str, max_name: int = 3, max_rest: int = 5):
    """Every way to read q as name + leftover with the name at the start or the end, longest name first, the start
    before the end: yields (name_words, rest_words, 'start'|'end'), all as the typed words (case kept, punctuation
    kept) - normalisation is the caller's. A name has 1..max_name words, the leftover 1..max_rest."""
    w = (q or "").split()
    n = len(w)
    for k in range(min(max_name, n - 1), 0, -1):
        if n - k <= max_rest:
            yield w[:k], w[k:], "start"
        if n - k <= max_rest:
            yield w[n - k:], w[:n - k], "end"


def whole_query_wins(q: str, res: list[dict], card: dict | None, table_labels=()) -> bool:
    """An exact title for the whole query: the card's title, or one of the first rows' titles (a Wikipedia article,
    a site, a place), reads as the query. "apple pay", "amazon music", "target field" are things, not a name + words."""
    k = norm_key(q)
    if not k:
        return True
    if card and norm_key(re.sub(r"\s*\(.*\)$", "", card.get("title") or "")) == k:
        return True
    for r in (res or [])[:5]:
        title = (r.get("title") or "")
        for part in (title.rsplit(" — Wikipedia", 1)[0], title.split(" — ")[0], title.split(" | ")[0], title.split(" - ")[0]):
            if norm_key(re.sub(r"\s*\(.*\)$", "", part)) == k:
                return True
    return k in table_labels


def _row_sites(res: list[dict], card: dict | None) -> list[str]:
    """Registered domains of the card's official site and of the first rows: the engine's own evidence for what a
    name means."""
    out = []
    for l in (card or {}).get("links", []) or []:
        if l.get("title") == "Official site":
            out.append(reg_domain(host_of(l.get("url") or "")))
    for r in (res or [])[:5]:
        out.append(reg_domain(host_of(r.get("url") or "")))
    return [x for x in out if x]


def _pref(table: SiteTable):
    """Sort key among hosts of one name: the apex (sony.com, not apps.sony.com), a generic ending, the most data."""
    return lambda h: (h != reg_domain(h), not h.endswith((".com", ".org", ".net")), -_weight(table.rows[h]), len(h), h)


def resolve_site(name_key: str, table: SiteTable, hint: list[str], resolver=None) -> str | None:
    """The host a name means: among the table's hosts whose first label is the name, the one the engine already
    shows (card official site or a top row), else the best on a generic ending (the apex over a subdomain, then the
    most data). resolver(name_key) -> [official host, ...] is the engine's own name lookup (an article's official
    site), asked only when no label matches ("angela merkel" -> her office's site). A name of one or two letters
    resolves only with a hint."""
    cands = table.hosts_named(name_key)
    if cands:
        hinted = [h for h in cands if reg_domain(h) in hint]
        if hinted:
            return sorted(hinted, key=_pref(table))[0]
        return sorted(cands, key=_pref(table))[0] if len(name_key) >= 3 else None
    if resolver is not None:
        try:
            for h in resolver(name_key) or ():
                for cand in sorted((x for x in table.rows if reg_domain(x) == reg_domain(h)), key=_pref(table)):
                    return cand
        except Exception:  # noqa: BLE001 - a lookup that failed says nothing
            return None
    return None


def card_site(name_key: str, card: dict | None, table: SiteTable) -> str | None:
    """The card's official site when the card is the named thing (its title, without a "(...)" or a corporate ending, is
    the name) and the table holds that site: "angela merkel" -> her office's site, whose name is not hers."""
    if not card or not table.rows:
        return None
    t = re.sub(r"\s*\(.*\)$", "", card.get("title") or "")
    keys = {norm_key(t), norm_key(re.sub(r",?\s+(inc|ltd|llc|corp|corporation|company|co|plc|gmbh|ag|sa)\.?$", "", t, flags=re.I))}
    if name_key not in keys:
        return None
    for l in card.get("links") or []:
        if l.get("title") == "Official site":
            h = host_of(l.get("url") or "")
            if h in table.rows:
                return h
            for cand in table.rows:                       # www. / a sibling host of the same registered domain
                if reg_domain(cand) == reg_domain(h):
                    return cand
    return None


def _weight(payload: dict) -> int:
    return len(payload.get("slots") or {}) or (1 if payload.get("template") else 0)


def _entity_title(name_words: list[str], card: dict | None) -> str:
    if card and norm_key(card.get("title") or "") == norm_key(" ".join(name_words)):
        return re.sub(r"\s*\(.*\)$", "", card["title"])
    return " ".join(w[:1].upper() + w[1:] for w in name_words)


def _place_after(res: list[dict], reg: str) -> int:
    """Index after the entity's own row (its site, among the first five rows): the new row goes under the exact tiers.
    Without such a row, after the first."""
    for i, r in enumerate(res[:5]):
        if reg_domain(host_of(r.get("url") or "")) == reg:
            j = i + 1
            while j < len(res) and j < 5 and res[j].get("in_card"):
                j += 1
            return j
    return 1 if res else 0


def apply_slots(q: str, res: list[dict], card: dict | None, table: SiteTable | None, resolver=None, lead: bool = False) -> list[dict]:
    """Insert the slot row for "name + intent words" (or "intent words + name"): the longest name that the table knows,
    the leftover being exactly one slot's phrase. The row sits under the entity's own rows; a row for the same page
    already in the list is moved there instead of repeated (never lower than it already was). lead=True puts the row first. Returns a new list, or res itself when nothing applies."""
    if not table or not q or not res:
        return res
    if whole_query_wins(q, res, card, table.by_label):
        return res
    hint = _row_sites(res, card)
    for name_w, rest_w, where in splits(q):
        sid = slot_of_query_words([x for w in rest_w for x in words(w)])
        if not sid:
            continue
        nk = norm_key(" ".join(name_w))
        if len(nk) < 2:
            continue
        site = resolve_site(nk, table, hint, resolver) or card_site(nk, card, table)
        slot = table.rows[site]["slots"].get(sid) if site else None
        if not slot:
            continue
        url, reg = slot["url"], reg_domain(site)
        title = f"{_entity_title(name_w, card)} — {slot['text']}"
        row = {"url": url, "title": title, "content": "", "source": "site", "score": 0.0, "slot": sid}
        key = _norm_page(url)
        rest = [r for r in res if _norm_page(r.get("url") or "") != key]
        i0 = next((i for i, r in enumerate(res) if _norm_page(r.get("url") or "") == key), None)
        if i0 is not None:
            row = dict(res[i0], slot=sid)
        at = 0 if lead else _place_after(rest, reg)
        if i0 is not None and i0 <= at:                            # already as high as the row would go: never demoted
            return res[:i0] + [row] + res[i0 + 1:]
        return rest[:at] + [row] + rest[at:]
    return res


def _norm_page(url: str) -> str:
    try:
        u = urlsplit(url or "")
    except ValueError:
        return url or ""
    return host_of(url) + (u.path.rstrip("/") or "") + (("?" + u.query) if u.query else "")


_QUESTION = re.compile(r"^(?:how|what|why|when|where|who|which|can|should|does|do|is|are)\b", re.I)


def bridge_guard(q: str, rest_w: list[str], res: list[dict], card: dict | None, site: str) -> bool:
    """The stronger guard (2026-11-10; measured on mix-300, where the first version fired on 9 of 305 searches, none of
    them a site search): the bridge is for "this is the maker's site and the engine has no answer for the words", so
      1. the template's host is the site's apex (sony.com, www.sony.com), never a subdomain (play.google.com, idoc.idaho.gov);
      2. the entity's own site LEADS the list: row 0, or row 1 under the card's article / a Wikipedia row (an entity the
         list does not lead with is a topic, not a destination: "microsoft earnings" led by Google Finance);
      3. not a question ("how to remove wallpaper"), and not a government search (a state's name is not a maker);
      4. the list does not already answer the words: no entity row holds every leftover word in its title or address."""
    reg = reg_domain(site)
    if site != reg:
        return False
    if _QUESTION.match((q or "").strip()):
        return False
    if any(r.get("source") == "gov" or reg_domain(host_of(r.get("url") or "")).endswith((".gov", ".mil")) for r in (res or [])[:3]):
        return False
    i = 0
    while i < len(res) and i < 1 and (res[i].get("source") == "wikipedia" or res[i].get("in_card")):
        i += 1
    official = {reg_domain(host_of(l.get("url") or "")) for l in (card or {}).get("links", []) or [] if l.get("title") == "Official site"}
    leads = i < len(res) and reg_domain(host_of(res[i].get("url") or "")) == reg
    if not leads and not (reg in official and i >= 1):
        return False
    want = {w for x in rest_w for w in words(x) if len(w) > 1}
    for r in (res or [])[:5]:
        if reg_domain(host_of(r.get("url") or "")) == reg:
            have = set(words((r.get("title") or "") + " " + (r.get("url") or "")))
            if want and want <= have:
                return False
    return True


def apply_bridge(q: str, res: list[dict], card: dict | None, table: SiteTable | None, resolver=None) -> list[dict]:
    """After the official site: a row that opens the site's own search for the words left after the name
    ("sony wh-1000xm5" -> Search sony.com for wh-1000xm5), marked bridge: true. Only when the name means a site the engine
    itself already shows (the card's official site, or one of the first rows), the leftover is not one slot's phrase, and
    the whole query is not a title. Nothing is fetched; the click is the visitor's."""
    if not table or not q or not res:
        return res
    if whole_query_wins(q, res, card, table.by_label):
        return res
    if any(r.get("bridge") for r in res):
        return res
    hint = _row_sites(res, card)
    for name_w, rest_w, where in splits(q):
        rest_text = " ".join(rest_w)
        if slot_of_query_words([x for w in rest_w for x in words(w)]):
            continue                                              # a slot's phrase belongs to the slot row
        if not words(rest_text):
            continue
        nk = norm_key(" ".join(name_w))
        site = resolve_site(nk, table, hint, resolver)
        if not site or reg_domain(site) not in hint:
            continue                                              # the engine must itself show the site the name means
        if not bridge_guard(q, rest_w, res, card, site):
            continue
        url = fill_template(table.rows[site]["template"], rest_text)
        if not url:
            continue
        shown = " ".join(rest_text.split())[:100]
        row = {"url": url, "title": f"Search {reg_domain(site)} for {shown}", "content": "", "source": "site",
               "score": 0.0, "bridge": True}
        reg = reg_domain(site)
        at = _place_after(res, reg)
        return res[:at] + [row] + res[at:]
    return res
