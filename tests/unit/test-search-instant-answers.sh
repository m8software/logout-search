#!/usr/bin/env bash
# requires: cmd:python3
################################################################################
# Unit tests for apps/search/serve/api/instant_answers.py: arithmetic, unit
# conversion, the time in a place and currency conversion, answered on the box in the answer box
# /instant carries. Standard library only: no database, no network. The clock and the exchange
# rates are passed in (fixed numbers here; the poller that keeps the real ones is tested in
# test-search-news.sh and test-search-news-sqlite.sh).
#
# The second half matters as much as the first: what must NOT be answered ("9/11", "24/7",
# "2x4", "prime time") — a wrong box on top of a search is worse than no box.
#
# Usage: bash tests/unit/test-search-instant-answers.sh   (PY=/path/to/python to override)
################################################################################
set -uo pipefail

SCRIPT_DIR_T="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR_T/../.." && pwd)"
API="${REPO_ROOT}/apps/search/serve/api"
PY="${PY:-python3}"

PASS=0
FAIL=0
RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; NC=$'\033[0m'
ok()  { echo "  ${GREEN}PASS${NC} $1"; PASS=$((PASS+1)); }
bad() { echo "  ${RED}FAIL${NC} $1"; shift; [[ $# -gt 0 ]] && printf '       %s\n' "$@"; FAIL=$((FAIL+1)); }

[[ -f "${API}/instant_answers.py" ]] || { echo "SKIP: ${API}/instant_answers.py not present"; exit 0; }
if ! "$PY" -c "import zoneinfo; zoneinfo.ZoneInfo('Asia/Tokyo')" 2>/dev/null; then
    echo "SKIP: $PY has no IANA time zone data (tzdata)"; exit 0
fi

out=$(API="$API" REPO="$REPO_ROOT" "$PY" - 2>&1 <<'PYEOF'
import datetime as dt, os, re, sys
sys.path.insert(0, os.environ["API"])
import instant_answers as ia
NOW = dt.datetime(2026, 10, 6, 19, 42, tzinfo=dt.timezone.utc)      # Tuesday; US and Europe on summer time
WINTER = dt.datetime(2026, 1, 15, 19, 42, tzinfo=dt.timezone.utc)
def got(q, now=NOW):
    a = ia.instant(q, now)
    return (a["answer"], a["title"], a["detail"], a["question"]) if a else None
def check(name, value, want):
    print(f"OK {name}" if value == want else f"BAD {name}: got {value!r}, want {want!r}")
def big(q, now=NOW):
    a = ia.instant(q, now)
    return a["answer"] if a else None

# --- the issue's own examples
check("18% of 240", got("18% of 240"), ("43.2", "18% of 240", "", "calc"))
check("5 miles in km", got("5 miles in km"), ("8.04672 km", "5 miles", "1 mile = 1.60934 km", "unit"))
check("350f to c", got("350f to c"), ("176.67 °C", "350 °F", "", "unit"))
check("2^10", got("2^10"), ("1,024", "2^10", "", "calc"))
check("cups in a liter", got("cups in a liter"), ("4.22675 US cups", "1 L", "", "unit"))
check("time in tokyo", got("time in tokyo"), ("4:42 AM", "Tokyo", "Wednesday, October 7 · JST (UTC+9)", "time"))
check("3pm est to pst", got("3pm est to pst"), ("12:00 PM PDT", "3:00 PM EDT", "the same day · UTC−7", "time"))

# --- arithmetic
for q, want in (("2+2", "4"), ("what is 10/2", "5"), ("(3 + 4) * 5", "35"), ("2 + 3 * 4", "14"), ("10 - 2 - 3", "5"),
                ("2^3^2", "512"), ("-2^2", "-4"), ("sqrt(16)", "4"), ("1,000 * 3", "3,000"), ("10 % 3", "1"),
                ("15% of 80", "12"), ("0.1 + 0.2", "0.3"), ("7 divided by 2", "3.5"), ("3 x 4", "12"), ("3 × 4", "12"),
                ("12 ÷ 4", "3"), ("2 ** 0.5", "1.414213562"), ("1/3", None), ("1 / 3", "0.3333333333"),
                ("what is 1/3", "0.3333333333"), ("2^100", "1.2676506 × 10^30"), ("2^64", "18,446,744,073,709,551,616"), ("1 / 3000000000", "3.333333333 × 10^-10"), ("2.5 ^ 5000", None), ("10 ^ 400 * 1.5", None), ("0.1 * 3", "0.3"), ("3 * 0.9999999999999", "3"), ("1 / 1000000", "0.000001"),
                ("5 plus 5 times 2", "15"), ("50% of 50% of 100", "25"), ("pi * 2", "6.283185307"), ("abs(-5) + 1", "6"),
                ("100 - 10%", "99.9"), ("1.5 * 4 =", "6"), ("calculate 12 * 12?", "144")):
    check(f"sum {q!r}", big(q), want)
# a bare 9/11 is a search; every way of ASKING for the division answers it (owner, 2026-10-06)
for q in ("9 / 11", "9/11 =", "9/11=", "=9/11", "= 9/11", "what is 9/11", "calc 9/11", "9 divided by 11", "9 over 11", "9÷11"):
    check(f"asked for: {q!r}", big(q), "0.8181818182")
check("the same for 24/7, 50/50 and a date", (big("24/7 ="), big("=50/50"), big("2024-10-06 =")), ("3.428571429", "1", "2,008"))
check("the title of an asked sum has no stray =", ia.instant("=9/11")["title"], "9 ÷ 11")
check("the title shows the sum with proper signs", ia.instant("3 * 4 / 2")["title"], "3 × 4 ÷ 2")

# --- units
for q, want in (("100 kg to lbs", "220.462 lb"), ("how many feet in a mile", "5,280 feet"), ("minutes in a day", "1,440 minutes"),
                ("8 oz in ml", "236.588 mL"), ("8 oz in grams", "226.796 g"), ("ounces in a pound", "16 oz"),
                ("how many ounces in a cup", "8 US fl oz"), ("72 degrees fahrenheit in celsius", "22.22 °C"),
                ("-40 f to c", "-40 °C"), ("0 c to k", "273.15 K"), ("100 c to f", "212 °F"), ("60 mph to km/h", "96.5606 km/h"),
                ("1 acre in sq ft", "43,560 sq ft"), ("5 in to cm", "12.7 cm"), ("1 inch in cm", "2.54 cm"),
                ("2 metric tons in lbs", "4,409.25 lb"), ("1 nautical mile in km", "1.852 km"), ("convert 10 km to miles", "6.21371 miles"),
                ("1.5 hours in minutes", "90 minutes"), ("1 gallon in liters", "3.78541 L"), ("1 mile in feet", "5,280 feet"),
                ("how many cups in 2 liters", "8.45351 US cups"), ("1 foot in inches", "12 inches"), ("1 km in m", "1,000 m")):
    check(f"units {q!r}", big(q), want)
check("the detail gives the rate for one", ia.instant("100 kg to lbs")["detail"], "1 kg = 2.20462 lb")
check("singular and plural labels", (ia.instant("1 mile in km")["title"], ia.instant("2 miles in km")["title"]), ("1 mile", "2 miles"))
check("US measures say so", ia.instant("1 cup in ml")["title"], "1 US cup")

# --- time
check("london in summer is BST", got("what time is it in london"), ("8:42 PM", "London", "Tuesday, October 6 · BST (UTC+1)", "time"))
check("london in winter is GMT", got("time in london", WINTER), ("7:42 PM", "London", "Thursday, January 15 · GMT (UTC)", "time"))
check("<place> time", big("tokyo time"), "4:42 AM")
check("a city that is not a zone name", got("time in san francisco")[:2], ("12:42 PM", "San Francisco"))
check("a half-hour zone", got("time in india"), ("1:12 AM", "India", "Wednesday, October 7 · IST (UTC+5:30)", "time"))
check("est in winter is EST, in summer the region's clock (EDT)", (got("3pm est to pst", WINTER)[1], got("3pm est to pst")[1]), ("3:00 PM EST", "3:00 PM EDT"))
check("24-hour input needs minutes", (big("15:30 london to new york"), big("15 london to new york")), ("10:30 AM EDT", None))
check("across midnight says the next day", got("9pm pst to tokyo"), ("1:00 PM JST", "9:00 PM PDT", "the next day · UTC+9", "time"))
check("backwards says the day before", got("1am tokyo to pst")[2], "the day before · UTC−7")
check("12am and 12pm", (got("12am est to utc")[1], got("12pm est to utc")[1]), ("12:00 AM EDT", "12:00 PM EDT"))
check("zone names with spaces", big("time in new york"), "3:42 PM")
check("the answer follows the clock it is given", big("time in tokyo", WINTER), "4:42 AM")
check("a different minute gives a different answer", big("time in tokyo", NOW + dt.timedelta(minutes=1)), "4:43 AM")

# --- one number minus another is a subtraction (owner 2026-11-06, "87-9" showed the year 879)
for q, want in (("87-9", "78"), ("100-1", "99"), ("5-3", "2"), ("40-1", "39")):
    check(f"subtraction: {q!r}", (ia.instant(q, NOW) or {}).get("answer"), want)
for q in ("1986-87", "2024-2025", "2024-10", "07-04", "555-1234", "1999-00"):
    check(f"not a subtraction: {q!r}", ia.instant(q, NOW), None)

# --- what is a search, not a question for this box
for q in ("9/11", "24/7", "50/50", "2024-10-06", "555-1234", "1-800-555-1234", "iphone 15", "24", "windows 11", "3m", "7-eleven",
          "men in black", "time", "prime time", "time in narnia", "1/0", "9^9^9", "2x4", "4x4", "4 x 4 trucks", "covid 19",
          "top 10 movies", "5 miles", "miles", "c to f", "the 100", "100 usd to eur", "10 kg to km", "what is love", "a + b",
          "time in", "-5", "300", "3 musketeers", "route 66", "b-52", "1 2 3", "pi", "-500 c to f", "sqrt(-1)", "sqrt(16",
          "pounds to dollars", "day to day", "how many people in a mile", "5 miles in", "10 mod 0", "", "x", "2 +", "(2 + 2",
          "25:00 london to tokyo", "=", "= =", "=iphone", "9/11 = 4", "3pm narnia to pst", "13pm est to pst", "what is the time", "new york times", "1 + 1 apples"):
    check(f"not answered: {q!r}", ia.instant(q, NOW), None)
check("a very long query is not parsed", ia.instant("1 + " * 40 + "1", NOW), None)
check("deep nesting does not crash", ia.instant("(" * 30 + "1+1" + ")" * 30, NOW)["answer"], "2")

# --- currency: the rates are passed in, as search_api does from the news file
RATES = {"date": "2026-10-06", "checked": "2026-10-06T17:42:00Z", "per_eur": {"USD": 1.08, "GBP": 0.85, "JPY": 160.0, "CAD": 1.5, "AUD": 1.6, "CHF": 0.95, "CNY": 7.5,
                                            "INR": 90.0, "MXN": 20.0, "PHP": 60.0, "IDR": 17000.0, "KRW": 1500.0, "SEK": 11.0}}
NO_CHECK = {k: v for k, v in RATES.items() if k != "checked"}
def money(q, rates=RATES, now=NOW):
    a = ia.instant(q, now, rates)
    return (a["answer"], a["title"], a["detail"], a["question"]) if a else None
ECB, CROSS = "ECB reference rate of Oct 6 · checked 2 h ago", "calculated from ECB reference rates of Oct 6 · checked 2 h ago"
check("100 usd to eur", money("100 usd to eur"), ("92.59 EUR", "100 USD", f"1 USD = 0.9259 EUR · {ECB}", "currency"))
check("50 euros in dollars", money("50 euros in dollars"), ("54.00 USD", "50 EUR", f"1 EUR = 1.08 USD · {ECB}", "currency"))
check("100 gbp to jpy goes through the euro and says it was calculated", money("100 gbp to jpy"),
      ("18,824 JPY", "100 GBP", f"1 GBP = 188.24 JPY · {CROSS}", "currency"))
check("no amount means one", money("usd to eur"), ("0.9259 EUR", "1 USD", f"1 USD = 0.9259 EUR · {ECB}", "currency"))
for q, want in (("100 USD to EUR?", "92.59 EUR"), ("100 usd in eur", "92.59 EUR"), ("100 usd into eur", "92.59 EUR"), ("100 usd as eur", "92.59 EUR"),
                ("100 usd = eur", "92.59 EUR"), ("convert 100 usd to eur", "92.59 EUR"), ("what is 100 usd in eur", "92.59 EUR"),
                ("how much is 100 dollars in euros", "92.59 EUR"), ("1,000 usd to eur", "925.93 EUR"), ("12.50 usd to eur", "11.57 EUR"),
                # names and plurals
                ("1 dollar to euros", "0.9259 EUR"), ("100 dollars to euro", "92.59 EUR"), ("10 pounds to euros", "11.76 EUR"),
                ("10 sterling to eur", "11.76 EUR"), ("1000 yen to usd", "6.75 USD"), ("10 yuan to rupees", "120.00 INR"),
                ("100 pesos to dollars", "5.40 USD"), ("100 philippine pesos to dollars", "1.80 USD"), ("10 francs to eur", "10.53 EUR"),
                ("10 swiss francs to eur", "10.53 EUR"), ("100 canadian dollars to australian dollars", "106.67 AUD"),
                ("100 canadian dollar to usd", "72.00 USD"), ("100 us dollars to cad", "138.89 CAD"), ("100 u.s. dollars to cad", "138.89 CAD"),
                # every code the rates carry, and the euro
                ("100 sek to eur", "9.09 EUR"), ("100 eur to sek", "1,100.00 SEK"), ("100 cad to mxn", "1,333.33 MXN"),
                # symbols
                ("$100 to eur", "92.59 EUR"), ("$ 100 in euros", "92.59 EUR"), ("€50 in dollars", "54.00 USD"), ("£10 to eur", "11.76 EUR"),
                ("¥1000 to usd", "6.75 USD"), ("100€ to usd", "108.00 USD"), ("100 usd to €", "92.59 EUR"), ("$100 cad to usd", "72.00 USD"),
                # asked the other way round
                ("how many euros is 100 dollars", "92.59 EUR"), ("how many pesos in a dollar", "18.52 MXN"), ("pesos in a dollar", "18.52 MXN"),
                ("a dollar in euros", "0.9259 EUR"),
                # how the number is written: no decimals for yen-like sums from 100 up, four digits under 1
                ("100 usd to jpy", "14,815 JPY"), ("0.5 usd to jpy", "74.07 JPY"), ("1 usd to krw", "1,389 KRW"), ("1 eur to idr", "17,000 IDR"),
                ("1 idr to eur", "0.00005882 EUR"), ("1 jpy to eur", "0.00625 EUR"), ("0.5 eur to usd", "0.54 USD"), ("1 gbp to eur", "1.18 EUR"),
                ("1000000 eur to usd", "1,080,000.00 USD")):
    a = ia.instant(q, NOW, RATES)
    check(f"{q} -> {want}", (a or {}).get("answer"), want)
    check(f"{q} is a currency answer", (a or {}).get("question"), "currency")
check("the rate line: four digits under 1", money("3 jpy to eur")[2], f"1 JPY = 0.00625 EUR · {ECB}")
check("the rate line: two decimals from 100 up, with separators", money("2 eur to idr")[2], f"1 EUR = 17,000.00 IDR · {ECB}")
check("the title keeps the amount as typed", money("1,234.5 usd to eur")[1], "1,234.5 USD")
# weight or money: the other side decides
check("pounds to kg is weight", money("pounds to kg")[3], "unit")
check("5 pounds in kg is weight", money("5 pounds in kg"), ("2.26796 kg", "5 lb", "1 lb = 0.453592 kg", "unit"))
check("how many pounds in a kg is weight", money("how many pounds in a kg")[3], "unit")
check("pounds to dollars is money", money("pounds to dollars"), ("1.27 USD", "1 GBP", f"1 GBP = 1.2706 USD · {CROSS}", "currency"))
check("5 pounds in dollars is money", money("5 pounds in dollars"), ("6.35 USD", "5 GBP", f"1 GBP = 1.2706 USD · {CROSS}", "currency"))
check("how many pounds in a dollar is money", money("how many pounds in a dollar")[0], "0.787 GBP")
check("the rates do not disturb the other answers", [money("5 miles in km")[3], money("time in tokyo")[3], money("2+2")[3], money("3pm est to pst")[3]],
      ["unit", "time", "calc", "time"])
# what is not a currency question, even with rates at hand
for q in ("dollar tree", "euro 2028", "100 usd", "usd", "dollars", "euro", "100 usd to", "to eur", "usd eur", "100 usd to usd", "100 dollars to bucks",
          "100 usd to xyz", "100 xyz to eur", "100 dollars to donuts", "0 usd to eur", "-5 usd to eur", "100 usd to eur to gbp", "$100", "$100 gbp to usd",
          "€100 usd to gbp", "$100€ to gbp", "dollar to euro history", "pounds in a kilo of dollars", "how many dollars", "how many people in a dollar",
          "100 nok to eur", "back to the future", "cad to cam", "9999999999999999 usd to eur"):
    check(f"not a currency question: {q!r}", ia.instant(q, NOW, RATES), None)
# no rates, broken rates, old rates: no answer and no error
check("no rates: no answer", [ia.instant("100 usd to eur", NOW), ia.instant("100 usd to eur", NOW, None), ia.instant("100 usd to eur", NOW, {})], [None, None, None])
for name, r in (("no date", {"per_eur": {"USD": 1.08}}), ("a date that is not one", {"date": "yesterday", "per_eur": {"USD": 1.08}}),
                ("no table", {"date": "2026-10-06"}), ("an empty table", {"date": "2026-10-06", "per_eur": {}}),
                ("a zero rate", {"date": "2026-10-06", "per_eur": {"USD": 0}}), ("a negative rate", {"date": "2026-10-06", "per_eur": {"USD": -1.08}}),
                ("a rate that is text", {"date": "2026-10-06", "per_eur": {"USD": "1.08"}}), ("a rate that is not finite", {"date": "2026-10-06", "per_eur": {"USD": float("inf")}}),
                ("rates that are a list", [1.08])):
    check(f"broken rates ({name}): no answer", ia.instant("100 usd to eur", NOW, r), None)
def dated(d):
    return (ia.instant("100 usd to eur", NOW, dict(RATES, date=d)) or {}).get("detail", None)
check("rates 7 days old still answer, and say their date", dated("2026-09-29"), "1 USD = 0.9259 EUR · ECB reference rate of Sep 29 · checked 2 h ago")
check("rates 8 days old do not", dated("2026-09-28"), None)
check("Friday's rates on a Monday answer", dated("2026-10-03"), "1 USD = 0.9259 EUR · ECB reference rate of Oct 3 · checked 2 h ago")
check("rates dated in the future do not", dated("2026-10-09"), None)
check("the age is counted from the clock passed in", (ia.instant("100 usd to eur", NOW + dt.timedelta(days=7), NO_CHECK) or {}).get("answer"), "92.59 EUR")
# when the rate was last checked (the poller's last successful fetch), relative to the clock passed in
def checked(ago_s=None, **kw):
    r = dict(RATES, **kw)
    if ago_s is not None:
        r["checked"] = (NOW - dt.timedelta(seconds=ago_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    a = ia.instant("100 usd to eur", NOW, r)
    return a["detail"].removeprefix("1 USD = 0.9259 EUR · ECB reference rate of Oct 6") if a else None
M, H, D = 60, 3600, 86400
for ago_s, want in ((0, "just now"), (4 * M + 59, "just now"), (5 * M, "5 min ago"), (59 * M + 59, "59 min ago"), (H, "1 h ago"),
                    (2 * H + 30 * M, "2 h ago"), (24 * H - 1, "23 h ago"), (D, "yesterday"), (2 * D - 1, "yesterday"), (2 * D, "2 days ago"),
                    (3 * D - 1, "2 days ago"), (3 * D, "3 days ago")):
    check(f"checked {ago_s} s ago reads {want!r}", checked(ago_s), f" · checked {want}")
check("a last check more than 3 days old is no answer: nobody has verified the rate", [checked(3 * D + 1), checked(5 * D)], [None, None])
check("... even when the rate's own date is recent enough", ia.instant("100 usd to eur", NOW, dict(RATES, date="2026-10-05", checked="2026-10-02T00:00:00Z")), None)
check("no 'checked' (an older table): the rate date alone",
      [ia.instant("100 usd to eur", NOW, NO_CHECK)["detail"], ia.instant("100 usd to eur", NOW, dict(RATES, checked=None))["detail"]],
      ["1 USD = 0.9259 EUR · ECB reference rate of Oct 6", "1 USD = 0.9259 EUR · ECB reference rate of Oct 6"])
check("... and the 7-day rule on the rate date still holds without it", [ia.instant("100 usd to eur", NOW, dict(NO_CHECK, date="2026-09-28")),
      ia.instant("100 usd to eur", NOW + dt.timedelta(days=8), NO_CHECK)], [None, None])
check("a 'checked' that is not a time is no answer", [checked(checked="soon"), checked(checked=1791312120.0), checked(checked="")], [None, None, None])
check("a check a few minutes ahead of the clock is just now; a day ahead is no answer", [checked(-120), checked(-2 * D)], [" · checked just now", None])
check("a time without a zone is UTC, and an offset is honoured", [checked(checked="2026-10-06T17:42:00"), checked(checked="2026-10-06T19:42:00+02:00")],
      [" · checked 2 h ago", " · checked 2 h ago"])
check("the cross-rate line says checked too", money("100 gbp to jpy")[2], "1 GBP = 188.24 JPY · calculated from ECB reference rates of Oct 6 · checked 2 h ago")
check("the 3-day rule is counted from the clock passed in", ia.instant("100 usd to eur", NOW + dt.timedelta(days=4), dict(RATES, date="2026-10-09")), None)
check("wants_rates: only a query shaped like a currency question asks for the rates",
      [ia.wants_rates(q) for q in ("100 usd to eur", "pounds to dollars", "$5 in yen", "5 pounds in kg", "dollar tree", "time in tokyo", "2+2", "iphone 15", "", "x" * 200)],
      [True, True, True, False, False, False, False, False, False, False])
check("a currency answer has the box's keys and no url", sorted(ia.instant("100 usd to eur", NOW, RATES)), ["answer", "detail", "question", "title"])

# --- shape: the answer box search_api.answer already fills
a = ia.instant("2+2", NOW)
check("the keys are the answer box's, with no url", sorted(a), ["answer", "detail", "question", "title"])
check("every value is text", all(isinstance(v, str) for v in a.values()), True)

# --- wiring
api = os.environ["API"]
src = open(api + "/search_api.py", encoding="utf-8").read()
check("/instant asks instant_answer first and does not cache it", "ans = instant_answer(q) or cached((\"answer\", q.lower())" in src, True)
check("a missing module means no answer, not an error", "except ImportError:\n        return None\n" in src, True)
fn = src[src.index("def instant_answer(q: str)"):src.index("def answer(q: str)")]
check("the rates are read through the cache, only for a currency-shaped query, and passed in",
      ("if wants is None or not wants(q):\n        return instant_answers.instant(q)\n" in fn, 'instant_answers.instant(q, rates=cached(("fx_rates",), lambda: [fx_rates()])[0])' in fn), (True, True))
check("an older module without wants_rates still answers", 'getattr(instant_answers, "wants_rates", None)' in fn, True)
fx = src[src.index("def fx_rates()"):src.index("def sports_no_game(")]
check("the rates come from the news file only: none on the Postgres backend", 'if NEWS_BACKEND != "sqlite":\n        return None' in fx, True)
check("search_api never fetches rates", re.findall(r"ecb\.europa|eurofxref", src), [])
for f, where in (("Dockerfile", api), ("Dockerfile.rtc", api), ("dev-engine.sh", os.environ["REPO"] + "/apps/search/tools")):
    check(f"{f} ships instant_answers.py", "instant_answers.py" in open(f"{where}/{f}", encoding="utf-8").read(), True)
check("nothing is evaluated or fetched", re.findall(r"\b(eval|exec|urllib|socket|subprocess|requests)\b", re.sub(r'""".*?"""', "", open(api + "/instant_answers.py", encoding="utf-8").read(), flags=re.S)), [])
# "try" is the lira's code and an English word: without an amount it is a sentence
_R = {"date": "2026-10-06", "checked": "2026-10-06T17:42:00Z", "per_eur": {"USD": 1.08, "TRY": 45.0, "GBP": 0.85}}
check("'try to usd' is not a lira question", ia.instant("try to usd", NOW, _R), None)
check("'100 try to usd' is", (ia.instant("100 try to usd", NOW, _R) or {}).get("title"), "100 TRY")
check("'lira to usd' is", (ia.instant("lira to usd", NOW, _R) or {}).get("question"), "currency")
print("END")
PYEOF
)
while IFS= read -r line; do
    case "$line" in
        OK\ *)  ok "${line#OK }" ;;
        BAD\ *) bad "${line#BAD }" ;;
    esac
done <<< "$out"
grep -q -E '^(OK|BAD) ' <<< "$out" || bad "python produced no verdict" "$out"
# a check that raises ends the python early: fewer lines, no BAD. That must not read as a pass.
[[ "$(tail -1 <<< "$out")" == "END" ]] || bad "the checks stopped early" "$(tail -4 <<< "$out")"

echo ""
echo "Total: $((PASS+FAIL))  PASS: ${PASS}  FAIL: ${FAIL}"
[[ $FAIL -eq 0 ]]
