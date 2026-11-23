"""Cities outside the United States for the sun and moon answers: "sunset in london", "sunrise in paris france".

The list is world_cities.tsv.gz, built by tools/build_world_cities.py from GeoNames (CC BY 4.0), cities of 15,000
people or more. Nothing is fetched. A name is matched whole, by its English or plain-letter spelling; several cities
of one name: the biggest, unless the question adds the country or the province ("london ontario", "san jose costa
rica", "vancouver bc").
"""

import gzip
import os
import re
import threading
import unicodedata

DATA = os.environ.get("SEARCH_WORLD_CITIES", os.path.join(os.path.dirname(os.path.abspath(__file__)), "world_cities.tsv.gz"))
# what people type for a country beside its GeoNames name
COUNTRY_ALIASES = {"uk": "GB", "u.k.": "GB", "britain": "GB", "great britain": "GB", "england": "GB", "scotland": "GB",
                   "wales": "GB", "uae": "AE", "holland": "NL", "russia": "RU", "south korea": "KR", "korea": "KR",
                   "czech republic": "CZ", "turkey": "TR", "ivory coast": "CI", "vietnam": "VN", "taiwan": "TW"}
# Canada's provinces by their postal letters ("vancouver bc")
PROVINCES = {"ab": "Alberta", "bc": "British Columbia", "mb": "Manitoba", "nb": "New Brunswick",
             "nl": "Newfoundland and Labrador", "ns": "Nova Scotia", "on": "Ontario", "pe": "Prince Edward Island",
             "qc": "Quebec", "sk": "Saskatchewan"}
_lock = threading.Lock()
_loaded: list = []


def key(text: str) -> str:
    """Lower case, accents and punctuation folded: "São Paulo" and "sao paulo", "St. John's" and "st johns"."""
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    t = re.sub(r"['’.]", "", t)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


class Cities:
    def __init__(self, lines):
        self.by_name: dict[str, list[dict]] = {}
        self.countries: dict[str, str] = dict(COUNTRY_ALIASES)       # typed name -> code
        for line in lines:
            c = line.rstrip("\n").split("\t")
            if len(c) != 9:
                continue
            city = {"name": c[0], "code": c[2], "country": c[3], "division": c[4], "lat": float(c[5]), "lon": float(c[6]),
                    "pop": int(c[7]), "tz": c[8]}
            self.countries.setdefault(key(c[3]), c[2])
            for k in dict.fromkeys((key(c[0]), key(c[1]))):
                if k:
                    self.by_name.setdefault(k, []).append(city)     # the file is biggest first, so each list is too

    def find(self, place: str) -> tuple[dict, bool] | None:
        """-> (the city, whether the question named its country or province), or None."""
        t = key(place)
        if not t:
            return None
        words = t.split()
        for n in (3, 2, 1):                                          # "san jose costa rica", "london ontario", "paris fr"
            if len(words) <= n:
                continue
            head, tail = " ".join(words[:-n]), " ".join(words[-n:])
            rows = self.by_name.get(head) or (self.by_name.get("saint " + head[3:]) if head.startswith("st ") else None)
            if not rows:
                continue
            code = self.countries.get(tail)
            for city in rows:
                division = key(city["division"])
                if (code and city["code"] == code) or (division and tail == division) or \
                        (city["code"] == "CA" and PROVINCES.get(tail) == city["division"]):
                    return city, True
        rows = self.by_name.get(t) or (self.by_name.get("saint " + t[3:]) if t.startswith("st ") else None)
        return (rows[0], False) if rows else None


def cities():
    """The loaded list, or None on an engine without the file."""
    with _lock:
        if not _loaded:
            try:
                with gzip.open(DATA, "rt", encoding="utf-8") as handle:
                    _loaded.append(Cities(handle))
            except Exception:
                _loaded.append(None)
        return _loaded[0]
