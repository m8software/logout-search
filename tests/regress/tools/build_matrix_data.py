#!/usr/bin/env python3
"""Builds tests/regress/sets/matrix/<type>.tsv: the search types of the matrix group. Sources: the open evalsets
(apps/search/tools/evalsets/open/*.tsv, gov-eval), the golden/must names, and lists written here by hand. Never visitor
searches, never the evalsets that name the owner's home (place*.tsv). Deterministic (seeded). Run from the repo root:
  python3 tests/regress/tools/build_matrix_data.py
Each row: query <TAB> expect <TAB> flags.  expect: 'host:example.com' (the first result's host contains it), 'card:Title'
(the card's title contains it) or empty.  flags: 'near' (ask also with a home position), 'gov:ST' (state of the home point)."""
import os, random, re, sys
R = random.Random(20261117)
T = "apps/search/tools/evalsets/"
OUT = "tests/regress/sets/matrix/"
os.makedirs(OUT, exist_ok=True)
N = 230

def rows(path):
    out = []
    for ln in open(path, encoding="utf-8"):
        if ln.startswith("#") or not ln.strip():
            continue
        out.append(ln.rstrip("\n").split("\t"))
    return out[1:] if out and out[0][0] in ("query", "key") else out

def pick(lst, n=N):
    seen, uniq = set(), []
    for x in lst:                                  # a list of rows (lists) or of strings: first of each distinct query
        k = x[0] if isinstance(x, list) else x
        if k not in seen:
            seen.add(k); uniq.append(x)
    lst = uniq
    R.shuffle(lst)
    return lst[:n]

def clean(q):
    return q.strip() and 2 <= len(q) <= 80 and not re.search(r"\d{4,}", q)

def write(name, items):
    seen, out = set(), []
    for it in items:
        q = it[0].strip()
        if q.lower() in seen or not clean(q):
            continue
        seen.add(q.lower()); out.append("\t".join([q] + list(it[1:]) + [""] * (3 - len(it))))
    open(OUT + name + ".tsv", "w", encoding="utf-8").write("\n".join(out) + "\n")
    print(f"{name:10s} {len(out)}")
    if len(out) < 200:
        print("  !! under 200", file=sys.stderr)

def hostexp(x):
    x = x.split("/")[0]
    return "host:" + x if "." in x else ""

# brand / company
hb = [r for r in rows(T + "open/head-brand.tsv") if len(r) > 2]
write("brand", [(r[0], hostexp(r[2])) for r in pick(hb, 400)][:N])
# person
op = [r for r in rows(T + "open/own-title.tsv") if len(r) > 4 and "kind:person" in r[4]]
write("person", [(r[0], hostexp(r[2])) for r in pick(op, 400)][:N])
# place: cities, landmarks, near-me (home position added by the runner)
city_list = ["tokyo","delhi","shanghai","sao paulo","mexico city","cairo","mumbai","beijing","dhaka","osaka","new york","karachi","buenos aires","istanbul","kolkata","manila","lagos","rio de janeiro","kinshasa","guangzhou","los angeles","moscow","shenzhen","lahore","bangalore","paris","bogota","jakarta","chennai","lima","bangkok","seoul","nagoya","hyderabad","london","tehran","chicago","chengdu","nanjing","wuhan","ho chi minh city","luanda","ahmedabad","kuala lumpur","hong kong","hangzhou","riyadh","baghdad","santiago","madrid","pune","houston","dallas","toronto","dar es salaam","miami","singapore","philadelphia","atlanta","fukuoka","khartoum","barcelona","johannesburg","saint petersburg","qingdao","washington dc","yangon","alexandria","guadalajara","boston","phoenix","san francisco","seattle","denver","montreal","vancouver","sydney","melbourne","brisbane","perth","auckland","dublin","edinburgh","manchester","glasgow","amsterdam","rotterdam","brussels","vienna","prague","warsaw","budapest","berlin","munich","hamburg","frankfurt","cologne","zurich","geneva","milan","rome","naples","turin","venice","florence","lisbon","porto","athens","oslo","stockholm","gothenburg","copenhagen","helsinki","reykjavik","nairobi","accra","addis ababa","casablanca","marrakesh","tunis","algiers","cape town","durban","lusaka","harare","kampala","kigali","dakar","abidjan","austin","nashville","portland","san diego","las vegas","new orleans","detroit","minneapolis","cleveland","pittsburgh","baltimore","charlotte","orlando","tampa","salt lake city","kansas city","st louis","cincinnati","columbus","indianapolis","milwaukee","honolulu","anchorage","albuquerque","tucson","sacramento","oakland","raleigh","richmond","buffalo","omaha","tulsa","memphis"]
landmarks = ["eiffel tower","statue of liberty","golden gate bridge","big ben","colosseum","taj mahal","great wall of china","machu picchu","sydney opera house","christ the redeemer","petra","stonehenge","acropolis","burj khalifa","empire state building","space needle","mount rushmore","grand canyon","niagara falls","yellowstone","yosemite","kremlin","red square","brandenburg gate","notre dame","louvre","sagrada familia","alhambra","leaning tower of pisa","trevi fountain","vatican","hagia sophia","blue mosque","pyramids of giza","sphinx","angkor wat","forbidden city","tiananmen square","mount fuji","tokyo tower","tower bridge","buckingham palace","tower of london","edinburgh castle","loch ness","giants causeway","cliffs of moher","neuschwanstein castle","matterhorn","mont blanc","versailles","arc de triomphe","champs elysees","times square","central park","brooklyn bridge","lincoln memorial","washington monument","white house","capitol building","liberty bell","alcatraz","hollywood sign","walt disney world","universal studios","las vegas strip","hoover dam","mount everest","kilimanjaro","victoria falls","table mountain","serengeti","great barrier reef","uluru","milford sound","hobbiton","easter island","galapagos islands","amazon rainforest","iguazu falls","chichen itza","teotihuacan","tikal","cn tower","banff national park","lake louise","yellowstone lake","glacier national park","zion national park","arches national park","bryce canyon","monument valley","death valley","mount denali","mount st helens","crater lake","old faithful","everglades","smithsonian","ellis island","pearl harbor","gettysburg","fort knox","kennedy space center","mount vernon","monticello","plymouth rock","independence hall","alamo","french quarter"]
nearkinds = ["pizza","pharmacy","gas station","coffee shop","grocery store","hospital","urgent care","library","park","hardware store","bank","atm","post office","gym","laundromat","bakery","barber shop","car wash","dentist","veterinarian","hotel","bookstore","movie theater","florist","pet store","ice cream","sushi","tacos","burgers","diner","bus station","pharmacy open now","bar","thrift store","car repair","dry cleaner","tire shop","chinese food","playground","school"]
forms = ["{} near me","{} nearby","closest {}","{} open now"]
nearme = [(f.format(k), "", "near") for k in nearkinds for f in forms]
write("place", [(c, "", "") for c in city_list] + [(l, "", "") for l in landmarks] + nearme)
# how-to
tasks = ["unclog a kitchen sink","caulk a bathtub","change a flat tire","boil an egg","tie a tie","jump start a car","remove a stripped screw","paint a room","fix a leaky faucet","reset a router","clean a cast iron pan","bake sourdough bread","write a resume","calculate compound interest","convert pdf to word","take a screenshot on a mac","make cold brew coffee","grow tomatoes from seed","patch drywall","replace a light switch","install a ceiling fan","remove a wine stain","season a steak","roast a chicken","meditate for beginners","learn to code","back up an iphone","free up disk space on windows","speed up a slow laptop","set up a vpn","change a car battery","check tire pressure","build a raised garden bed","prune roses","compost at home","brew kombucha","make pancakes from scratch","sharpen a kitchen knife","hang a picture frame","fix a squeaky door","unfreeze a pipe","clean a washing machine","descale a kettle","get red wine out of carpet","make a paper airplane","tie a bowline knot","do a push up properly","stretch your lower back","lower cholesterol naturally","fall asleep faster","write a cover letter","negotiate a salary","file taxes online","start a podcast","film a video with a phone","edit a photo for free","create a budget","pay off credit card debt","apply for a passport","renew a drivers license","change a name on a social security card","register to vote","adopt a rescue dog","train a puppy to sit","potty train a toddler","knit a scarf","sew a button","iron a shirt","fold a fitted sheet","clean a keyboard","replace a phone screen","charge a dead laptop","connect a printer to wifi","recover a deleted file","reset a forgotten password"]
howto = []
for t in tasks:
    howto += [f"how to {t}", f"how do i {t}", f"{t} step by step", f"easiest way to {t}"]
write("howto", [(q, "", "") for q in pick(howto, 400)])
# fact questions
companies = ["microsoft","apple","google","amazon","tesla","nvidia","meta","netflix","walmart","target","costco","ford","general motors","boeing","intel","amd","oracle","ibm","salesforce","adobe","starbucks","mcdonalds","nike","disney","coca cola","pepsi","pfizer","johnson and johnson","exxon","chevron","jpmorgan","goldman sachs","visa","mastercard","paypal","uber","airbnb","spotify","twitter","snap","pinterest","shopify","zoom","slack","dropbox","samsung","sony","toyota","honda","bmw"]
countries = ["france","germany","japan","brazil","canada","australia","india","china","italy","spain","portugal","egypt","kenya","nigeria","argentina","chile","peru","colombia","mexico","norway","sweden","finland","denmark","poland","greece","turkey","iran","iraq","israel","jordan","thailand","vietnam","indonesia","malaysia","philippines","pakistan","bangladesh","nepal","new zealand","ireland","scotland","switzerland","austria","hungary","romania","ukraine","russia","south africa","morocco","ethiopia","ghana","cuba","jamaica","panama","ecuador","bolivia","uruguay","venezuela","south korea","north korea","mongolia","kazakhstan","saudi arabia","qatar","lebanon","tunisia","algeria"]
things = ["mount everest","eiffel tower","statue of liberty","big ben","empire state building","burj khalifa","taj mahal","golden gate bridge","niagara falls","the great pyramid","lebron james","michael jordan","shaquille oneal","tom cruise","dwayne johnson","usain bolt","a giraffe","an elephant","a blue whale","space needle","cn tower","leaning tower of pisa","k2","mount fuji","kilimanjaro","denali","angel falls","hoover dam","washington monument","sears tower"]
facts = [(f"ceo of {c}", "", "") for c in companies] + [(f"capital of {c}", "", "") for c in countries] + [(f"how tall is {t}", "", "") for t in things] + [(f"population of {c}", "", "") for c in countries[:45]] + [(f"who founded {c}", "", "") for c in companies[:35]]
ft = [r for r in rows(T + "open/facts.tsv") if len(r) > 2]
write("fact", facts + [(r[0], "", "") for r in pick(ft, 60)])
# misspellings
ty = [r for r in rows(T + "open/typo-wiki.tsv") + rows(T + "open/typo-site.tsv") if len(r) > 2]
write("misspell", [(r[0], "", "") for r in pick(ty, N)])
# product
mp = [r for r in rows(T + "open/maker-product.tsv") + rows(T + "open/maker-plus-product.tsv") if len(r) > 2]
write("product", [(r[0], hostexp(r[2])) for r in pick(mp, N)])
# government task: the 50-state set, home position of the state
gov = [r for r in rows("apps/search/tools/gov-eval/gov-services.tsv") if len(r) > 4]
bystate = {}
for r in gov:
    bystate.setdefault(r[1], []).append(r)
g = []
for st, lst in sorted(bystate.items()):
    R.shuffle(lst)
    g += [(r[4], "", f"near gov:{st}") for r in lst[:5]]
write("gov", g)
# account / login
logins = [r[0] for r in pick(hb, 140)]
ac = [r for r in rows(T + "open/account.tsv") if len(r) > 3]
write("account", [(f"{b} login", "", "") for b in logins] + [(f"{b} sign in", "", "") for b in logins[:40]] + [(r[0], "", "") for r in pick(ac, 80)])
# short names (2-3 letters): the open sets' aliases + brand abbreviations
short = []
for f in sorted(os.listdir(T + "open")):
    if f.endswith(".tsv") and not f.startswith(("place", "typo-place", "ambiguous-place", "exclude", "judged-info.", "ambiguous-judged")):
        short += [r[0] for r in rows(T + "open/" + f) if 2 <= len(r[0].strip()) <= 3 and r[0].isascii() and r[0].isalpha()]
import itertools, string
abbr = "uk eu un ny la dc ai us is go me ok to hp lg ea ibm bbc cnn nba nfl nhl mlb fbi cia nsa nasa irs dmv fda cdc epa usa nyc sf atx hbo tnt amc mtv vh1 espn fox abc nbc cbs aol msn ups dhl fedex att cvs bmw kia gmc vw ge gm hm ikea lego ups ebay wsj nyt ap upi pbs npr ted ufc wwe wnba mls atp wta pga nascar f1 imf wto who nato opec asean".split()
write("short", [(s, "", "") for s in dict.fromkeys(abbr + pick(short, 400))])
# long phrases and natural questions
ji = [r[0] for r in rows(T + "open/judged-info.tsv") if len(r[0].split()) >= 5]
lng = [f"what is the best way to {t}" for t in tasks] + [f"can you explain how to {t} for someone who has never done it" for t in tasks[:40]] + [f"why does my {x} keep happening and what should i do about it" for x in ["laptop overheat","car battery die","basement flood","sink gurgle","router disconnect","phone freeze"]]
write("long", [(q, "", "") for q in ji + lng])
# live: weather, sports, news
teams = ["yankees","mets","red sox","dodgers","giants","cubs","braves","astros","phillies","padres","lakers","celtics","knicks","nets","warriors","heat","bulls","bucks","suns","76ers","patriots","cowboys","eagles","jets","chiefs","49ers","packers","steelers","ravens","bills","bruins","rangers","devils","islanders","flyers","penguins","maple leafs","canadiens","oilers","lightning","manchester united","arsenal","chelsea","liverpool","real madrid","barcelona","bayern munich","juventus","psg","inter miami"]
topics = ["election","inflation","interest rates","stock market","oil prices","wildfire","hurricane","earthquake","space launch","ai regulation","climate summit","supreme court","tariffs","housing market","jobs report","vaccine","measles outbreak","bird flu","power outage","airline strike","olympics","world cup","super bowl","oscars","grammys","nobel prize","transit strike","bridge collapse","tech layoffs","chip shortage"]
live = [(f"weather {c}", "", "near") for c in city_list[:110]] + [(f"{t} score", "", "") for t in teams] + [(f"{t} news", "", "") for t in topics] + [(f"{t} schedule", "", "") for t in teams[:40]] + [(f"latest news {t}", "", "") for t in topics]
write("live", live)
# plain words and definitions
words = [w.strip() for w in open("/usr/share/dict/words", encoding="utf-8", errors="ignore") if re.fullmatch(r"[a-z]{4,9}", w.strip())] if os.path.exists("/usr/share/dict/words") else []
words = pick(words, 150)
write("word", [(w, "", "") for w in words] + [(f"define {w}", "", "") for w in words[:50]] + [(f"{w} meaning", "", "") for w in words[50:100]])
