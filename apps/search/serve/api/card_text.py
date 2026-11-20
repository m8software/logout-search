"""card_text: the card's text and picture, the part of a card that is the same for every search that shows it (cards once).

A card has two kinds of content. Its links, description, related articles and which article it is decide rows of the list, so
they belong to the answer (stage "card"). Its lead and its picture decide nothing else: nothing in a finished answer reads
them. They are made here, from the release data and the article alone, kept ONCE per subject in the store's `cards` table
(result_store.py) and put on the stored answer when it is served (join). A stored answer still carries the lead and picture it
was built with: an engine that has no card set, or a set older than the answer, shows those.

This file is not part of the card stage's code version (stage_card.py is): a change here leaves every stored answer current
and is rolled out by rebuilding the card set (twin/cards_build.py): one lookup per subject, no search, no reranker.
VERSION is this file's own hash."""
from __future__ import annotations

import base64
import hashlib
import re
from urllib.parse import quote

FIELDS = ("lead", "image")            # what the card set holds, one set per field: a lead fix does not re-ship the pictures
# A card's lead comes in two lengths. An article that is a row of the list gives the card the row's text, the first ROW_CUT
# characters of its lead (search_api._wiki); an article the card stage looks up by title (a short name's card: "outlook" ->
# Microsoft Outlook, wiki_top) gives the whole lead. The set keeps both ("lead", and "lead_full" where the whole lead is
# longer) and the join gives an answer the length it was built with: a stored lead longer than ROW_CUT was the whole one.
ROW_CUT = 300
SET_FIELDS = ("lead", "lead_full", "image")


def _version() -> str:
    with open(__file__, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


VERSION = _version()

def card_image(row) -> dict | None:
    """A card_images row -> the card's picture: a data: URI, its size, and the credit
    (author, licence + deed, the file's Commons page)."""
    if not row:
        return None
    img, w, h, author, licence, licence_url, file = row
    out = {"src": "data:image/webp;base64," + base64.b64encode(bytes(img)).decode("ascii"), "w": w, "h": h,
           "licence": licence, "source": "https://commons.wikimedia.org/wiki/File:" + quote(file.replace(" ", "_"))}
    if author:
        out["author"] = author
    if licence_url:
        out["licence_url"] = licence_url
    return out

# What the lead keeps of stripped pronunciation markup: "Roblox (, ) is", "The Eiffel Tower ( ; )
# is" (card UI audit, 2026-09-30).
_EMPTY_PARENS = re.compile(r"\s*\(\s*[,;:\s]*\)")

# Wiki markup that reaches a lead (the wiki index keeps 300 characters of the article's text as it was written):
# [[target|label]] and [[target]] links, a link cut by the 300-character cap ("[[European mainland|the contine"),
# {{templates}}, {| tables, <ref>...</ref> and other tags, bold and italic quote marks, [http://x label] links, HTML entities.
_WIKI_LINK = re.compile(r"\[\[(?:[^\[\]|]*\|)?([^\[\]|]*)\]\]")
_WIKI_LINK_CUT = re.compile(r"\[\[(?:[^\[\]|]*\|)?([^\[\]|]*)$")
_WIKI_TEMPLATE = re.compile(r"\{\{[^{}]*\}\}")
_WIKI_TEMPLATE_CUT = re.compile(r"\{\{[^{}]*$")
_WIKI_REF = re.compile(r"<ref\b[^>]*/>|<ref\b[^>]*>.*?</ref>|<ref\b[^>]*>.*$", re.S | re.I)
_WIKI_TAG = re.compile(r"</?[A-Za-z][^<>]*>?|<!--.*?-->")
_WIKI_QUOTES = re.compile(r"'{2,5}")
_WIKI_TABLE = re.compile(r"\{\|.*$|\s\|[-}+].*$|\|\|.*$", re.S)                      # a table that starts where the lead should be: nothing after it is text
_WIKI_EXTLINK = re.compile(r"\[(?:https?:)?//\S+\s+([^\]]*)\]")
_ENTITY = re.compile(r"&(nbsp|amp|lt|gt|quot|#\d+|#x[0-9a-fA-F]+|ndash|mdash);")
_ENTITIES = {"nbsp": " ", "amp": "&", "lt": "<", "gt": ">", "quot": '"', "ndash": "–", "mdash": "—"}

def _entity(m):
    k = m.group(1)
    if k in _ENTITIES:
        return _ENTITIES[k]
    return chr(int(k[2:], 16)) if k[:2] == "#x" else chr(int(k[1:]))

# Text-bearing templates are rendered as Wikipedia shows them, not deleted: "Nordstrom.{{As of|2024}}, Nordstrom Rack operates" reads
# "Nordstrom. As of 2024, Nordstrom Rack operates". Only the simple, non-nested forms; the rest (boxes, tables, citations) are removed as before.
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")
_TPL_SIMPLE = re.compile(r"\{\{\s*([^{}|]{1,24}?)\s*(?:\|([^{}]*))?\}\}")


def _month_name(x: str) -> str | None:
    x = x.strip()
    if x.isdigit() and 1 <= int(x) <= 12:
        return _MONTHS[int(x) - 1]
    for m in _MONTHS:
        if m.lower() == x.lower() or m[:3].lower() == x.lower():
            return m
    return None


def _template_args(raw: str | None) -> tuple[list[str], dict[str, str]]:
    pos, named = [], {}
    for part in (raw or "").split("|"):
        k, eq, v = part.partition("=")
        if eq and re.fullmatch(r"[A-Za-z_ -]{1,16}", k.strip()):
            named[k.strip().lower()] = v.strip()
        else:
            pos.append(part.strip())
    return pos, named


def _date_text(pos: list[str], us: bool = False) -> str:
    y = pos[0] if pos else ""
    mo = _month_name(pos[1]) if len(pos) > 1 else None
    d = pos[2] if len(pos) > 2 and pos[2].isdigit() else None
    if not re.fullmatch(r"\d{3,4}", y):
        return ""
    if mo and d:
        return f"{mo} {int(d)}, {y}" if us else f"{int(d)} {mo} {y}"
    return f"{mo} {y}" if mo else y


def _render_template(m: "re.Match") -> str:
    """The text a simple template shows, or the template unchanged when it is not one of these (the removal that follows takes it out)."""
    name = re.sub(r"\s+", " ", m.group(1)).lower()
    pos, named = _template_args(m.group(2))
    if name in ("as of", "asof"):
        when = _date_text(pos, named.get("df", "").startswith("us") or named.get("df") == "mdy")
        if not when:
            return m.group(0)
        word = "Since" if named.get("since") in ("y", "yes") else "As of"
        if named.get("lc") in ("y", "yes"):
            word = word.lower()
        text = named.get("alt") or f"{word} {when}"
        return "\ue001" + text                         # \ue001: a space is put before it unless one is there already
    if name in ("circa", "c.") and pos and pos[0]:
        return "c. " + pos[0]
    if (name == "lang" or name.startswith("lang-")) and pos:
        return pos[-1] if name == "lang" else pos[0]
    if name in ("nowrap", "nobr", "small", "big", "nobold") and pos:
        return pos[0]
    if name == "abbr" and pos:
        return pos[0]
    if name in ("nbsp", "spaces"):
        return " "
    if name == "start date" and pos:
        d = _date_text(pos)
        return d or m.group(0)
    if name == "nihongo" and len(pos) >= 2 and pos[0]:
        extra = ", ".join(x for x in pos[1:3] if x)
        return f"{pos[0]} ({extra})" if extra else pos[0]
    return m.group(0)


# What a removed template or an emptied bracket leaves behind, the unambiguous cases only: a bracket that starts or ends with stray marks
# ("(, born", "(; 12 May", "(;,; 232 BCE)", "(1871, )") and doubled commas or semicolons. Nothing else is rewritten: "X., Y" stays.
_GAP = "\\s\u200b-\u200f\u2060\ufeff"                    # spaces and the invisible marks a removed pronunciation template leaves
_STRAY_OPEN = re.compile(r"\([" + _GAP + r"]*[,;:](?:[" + _GAP + r",;:])*")
_STRAY_CLOSE = re.compile(r"(?:[" + _GAP + r",;:])*[,;:][" + _GAP + r"]*\)")
_STRAY_DOUBLE = re.compile(r"([,;])(?:[" + _GAP + r"]*\1)+")


# What the importer left where it deleted a text-bearing template, in data built before it rendered them (wikiparse.py,
#): "Nordstrom., Nordstrom Rack" ({{As of}} between a full stop and a comma) and "1861May 26" ({{snd}}, the
# spaced dash between two dates). A full stop after an abbreviation keeps its comma ("Inc., commonly known").
_STOP_COMMA = re.compile(r"\b([A-Za-z][a-z]{3,})\.,\s+([a-z]?)(?=\w)")
_ABBREV = {"inc", "corp", "ltd", "bros", "assn", "dept", "univ", "intl", "govt", "approx", "prof", "sept", "jan", "feb",
           "aug", "oct", "nov", "dec", "est"}
_YEAR_MONTH = re.compile(r"(\d{4})(" + "|".join(("January", "February", "March", "April", "May", "June", "July", "August",
                                                 "September", "October", "November", "December")) + r")(?= \d)")


def _stop_comma(m: "re.Match") -> str:
    if m.group(1).lower() in _ABBREV:
        return m.group(0)
    return m.group(1) + ". " + m.group(2).upper()


def collapse_stray_punctuation(t: str) -> str:
    """t with the marks a removed template or emptied bracket leaves in a bracket, doubled commas or semicolons, a comma
    left after a full stop, and a lost dash between a year and a date, collapsed or put back."""
    t = _STRAY_OPEN.sub("(", t)
    t = _STRAY_CLOSE.sub(")", t)
    t = _STRAY_DOUBLE.sub(r"\1", t)
    if ".," in t:
        t = _STOP_COMMA.sub(_stop_comma, t)
    return _YEAR_MONTH.sub("\\1 \u2013 \\2", t)


def strip_wiki_markup(text: str) -> str:
    """The plain text of a lead that may carry wiki markup, its stray punctuation collapsed. Nothing else changes."""
    if not text:
        return text
    if not any(c in text for c in "[]{}<>&'"):
        return collapse_stray_punctuation(text) if any(c in text for c in ",;:") or _YEAR_MONTH.search(text) else text
    if "{{" in text:
        text = _TPL_SIMPLE.sub(_render_template, text)
        text = re.sub(r"(?<=[^\s(\[\ue001])\ue001", " ", text).replace("\ue001", "")     # a rendered "As of" follows a space
    t = _WIKI_REF.sub("", text)
    t = _WIKI_LINK.sub(r"\1", t)
    t = _WIKI_LINK_CUT.sub(r"\1", t)
    for _ in range(3):                                     # templates nest
        t2 = _WIKI_TEMPLATE.sub("", t)
        if t2 == t:
            break
        t = t2
    t = _WIKI_TEMPLATE_CUT.sub("", t)
    t = _WIKI_TABLE.sub("", t)
    t = _WIKI_EXTLINK.sub(r"\1", t)
    t = _WIKI_TAG.sub("", t)
    t = _WIKI_QUOTES.sub("", t)
    return collapse_stray_punctuation(re.sub(r" {2,}", " ", _ENTITY.sub(_entity, t)))

# The wiki index keeps only the start of the lead (300 characters, sometimes fewer): it is cut where the cap fell, often
# in the middle of a word ("...the company re", "...banking and auxili", "...[[European mainland|the contine"). A
# Wikipedia lead ends with a sentence end, so one that does not was cut.
_LEAD_SENTENCE_END = re.compile(r"[.!?][\"')\]”]*(?=\s+[A-Z0-9\"'(“]|\s*$)")
_NOT_SENTENCE = re.compile(r"(?:\b[A-Z]|\b(?:Mr|Mrs|Ms|Dr|St|Jr|Sr|Inc|Ltd|Co|Corp|No|vs|etc|Prof|Gen|Col|Lt|Sgt|Mt|Ft|approx|ca|fl|c|b|d|r))\.$")

def cut_lead(text: str) -> str:
    """A lead cut by the 300-character cap ends where a sentence ends or, failing that, at a word with an ellipsis:
    never in the middle of a word. A lead that ends on its own (a sentence end) is unchanged."""
    if len(text) < 40 or text.rstrip()[-1:] in ".!?…)\"'”":
        return text
    best = 0
    for m in _LEAD_SENTENCE_END.finditer(text):
        if m.end() >= 60 and not _NOT_SENTENCE.search(text[:m.end()]):
            best = m.end()
    if best:
        return text[:best].rstrip()
    head = text.rsplit(" ", 1)[0] if " " in text else text      # the last word may be cut: it goes
    return head.rstrip(" ,;:–—-") + "…"

def clean_lead(text: str) -> str:
    """A lead for the card: wiki markup stripped, empty brackets and the spacing they leave removed, and a lead cut
    by the 300-character cap ended at a sentence or a word."""
    raw = text or ""
    t = re.sub(r"\s+([,;.])", r"\1", _EMPTY_PARENS.sub("", strip_wiki_markup(raw))).strip()
    return cut_lead(t)


def text_of(lead_raw: str | None, img_row) -> dict:
    """{"lead", "lead_full", "image"} of a subject from its whole stored lead and picture row: exactly what card_of puts on a
    card built from the article's row (lead) or from the article looked up by title (lead_full; None when it is no longer)."""
    raw = lead_raw or ""
    cut, full = clean_lead(raw[:ROW_CUT]), clean_lead(raw)
    return {"lead": cut, "lead_full": full if full != cut else None, "image": card_image(img_row)}


def join(card: dict | None, entry, row_at: float, row_ct: str | None, subject) -> dict | None:
    """The card as served: its lead and picture from the card set when the set has them for this subject and they are not
    older than what the answer carries; otherwise the card as stored. entry(field) -> (value, version, at) or None;
    row_at / row_ct: when the answer was written and the VERSION that made its own text when it was computed fresh (None: an
    answer from before the card set, one patched in place, or one whose card came from a stored part: its text is never
    newer than a set built after it). The embedded text wins only when another version made it after the set's entry."""
    if not isinstance(card, dict) or not card.get("url"):
        return card
    s = subject(card["url"])
    out = card
    for f in FIELDS:
        try:
            e = entry(s, f)
            if f == "lead" and e and len(card.get("lead") or "") > ROW_CUT:
                e2 = entry(s, "lead_full")            # this answer's card carries the whole lead
                e = e2 if e2 and e2[0] is not None else e
        except Exception:  # noqa: BLE001 - the set is extra: the stored card stands
            e = None
        if not e:
            continue
        val, ver, at, title = e
        if title != card.get("title"):
            continue                                  # another article under this address: not this card's text
        if row_ct is not None and row_ct != ver and row_at > at:
            continue                                  # the answer's own text is newer than the set's
        if out is card:
            out = dict(card)
        if val is None:
            out.pop(f, None)                          # the subject has no picture (any more)
        else:
            out[f] = val
    return out
