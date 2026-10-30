"""Monitor przetargów dla WIP-Druk.
Źródła: BZP (e-Zamówienia), TED (UE), strony internetowe (Tavily).
Ocena przydatności i krótki opis: Gemini (darmowy klucz API).
Wysyłka: Brevo (darmowy plan).
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("Europe/Warsaw")
NOW = datetime.now(TZ)
SEEN_FILE = "seen.json"
LOOKBACK_DAYS = 2          # ile dni wstecz sprawdzać (z zapasem; duplikaty odsiewa seen.json)
MIN_VALUE_PLN = 5000       # próg; jeśli brak kwoty -> pokazujemy
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
WEB_SEARCH = os.getenv("WEB_SEARCH", "1") == "1"
DEBUG = os.getenv("DEBUG", "0") == "1"

PROFILE = (
    "WIP-Druk to drukarnia offsetowa i cyfrowa z Warszawy (prepress, introligatornia). "
    "Bierze zlecenia: ulotki, plakaty, foldery, broszury, wizytówki, kalendarze, katalogi, "
    "teczki, zaproszenia, druk wielkoformatowy (banery, roll-upy), nietypowe opakowania "
    "z kartonu lub papieru, a także gadżety reklamowe i drobne artykuły biurowe (najlepiej z nadrukiem/logo): długopisy, naklejki, kubki, karteczki samoprzylepne, notesy, bloczki. NIE bierze: książek, czasopism, gazet, formularzy, papieru "
    "firmowego, ani zakupu drukarek/tonerów/serwisu urządzeń drukujących."
)

KEYWORDS = [
    "druk", "wydruk", "drukow", "ulotk", "plakat", "folder", "broszur", "wizytów",
    "kalendarz", "katalog", "teczk", "zaprosze", "baner", "banner", "roll-up", "rollup",
    "wielkoformat", "poligraf", "materiałów promocyjn", "materiały promocyjn",
    "materiałów informacyjn", "materiały informacyjn", "opakowa", "pudeł", "karton",
    "długopis", "naklej", "etykiet", "kubk", "kubek", "karteczk", "notes", "bloczk",
    "gadżet", "gadzet", "upominki", "artykuły biurowe", "artykułów biurowych",
]
EXCLUDE = [
    "drukark", "toner", "tusz", "urządze", "wielofunkcyj", "ciągłości druku",
    "druku podążając", "serwis", "eksploatac", "dzierżaw", "druk 3d", "drukarki 3d",
    "książk", "czasopism", "gazet", "formularz", "papier ksero", "papieru ksero",
    "papier do drukar", "papieru do drukar",
]
CPV_PREFIXES = ["798", "2214", "2215", "2216", "2245", "2246", "2281", "2285", "4461",
                "3019", "2282", "39294"]  # 3019 art. biurowe (długopisy, notesy), 39294 artykuły promocyjne

S = requests.Session()
S.headers["User-Agent"] = "wipdruk-przetargi/1.0"
WARNINGS = []


def log(*a):
    print(*a, flush=True)


def g(d, *keys, default=None):
    """Pobierz pierwszą istniejącą wartość z listy możliwych kluczy."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, "", []):
            return d[k]
    return default


def text_of(v):
    if isinstance(v, dict):  # TED zwraca teksty jako {"pol": "...", "eng": "..."}
        v = v.get("pol") or v.get("POL") or next(iter(v.values()), "")
    if isinstance(v, list):
        v = ", ".join(text_of(x) for x in v)
    return str(v or "").strip()


def matches(title, cpv):
    t = title.lower()
    if any(x in t for x in EXCLUDE):
        return False
    kw = any(x in t for x in KEYWORDS)
    cpv_digits = re.findall(r"\d{8}", cpv or "")
    cp = any(c.startswith(p) for c in cpv_digits for p in CPV_PREFIXES)
    return kw or cp


def fmt_date(v):
    v = text_of(v)
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", v)
    return f"{m.group(3)}.{m.group(2)}.{m.group(1)}" if m else v


# ---------- BZP ----------
def fetch_bzp():
    url = "https://ezamowienia.gov.pl/mo-board/api/v1/Board/Search"
    date_from = (NOW - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT00:00:00")
    date_to = NOW.strftime("%Y-%m-%dT23:59:59")
    out, seen_ids = [], set()
    for page in range(1, 401):
        params = {
            "NoticeType": "ContractNotice",
            "PublicationDateFrom": date_from,
            "PublicationDateTo": date_to,
            "PageSize": 500,
            "PageNumber": page,
        }
        r = S.get(url, params=params, timeout=60)
        r.raise_for_status()
        data = r.json()
        items = data if isinstance(data, list) else g(data, "items", "data", "notices", default=[])
        if DEBUG and page == 1:
            log("BZP przykładowy rekord:", json.dumps(items[:1], ensure_ascii=False)[:2000])
        new = 0
        for it in items:
            oid = str(g(it, "objectId", "noticeId", "id", "bzpNumber", "noticeNumber", default=""))
            if not oid or oid in seen_ids:
                continue
            seen_ids.add(oid)
            new += 1
            out.append({
                "id": "bzp:" + oid,
                "source": "BZP",
                "title": text_of(g(it, "orderObject", "title", "name")),
                "buyer": text_of(g(it, "organizationName", "contractingAuthorityName", "buyerName")),
                "city": text_of(g(it, "organizationCity", "city")),
                "province": text_of(g(it, "organizationProvince", "province")),
                "cpv": text_of(g(it, "cpvCode", "cpvCodes", "cpv")),
                "deadline": fmt_date(g(it, "submittingOffersDate", "offersSubmissionDate", "deadline")),
                "value_pln": None,
                "url": f"https://ezamowienia.gov.pl/mo-client-board/bzp/notice-details/id/{oid}",
            })
        if not items or new == 0:
            break
    log(f"BZP: pobrano {len(out)} ogłoszeń")
    return out


# ---------- TED ----------
def fetch_ted():
    url = "https://api.ted.europa.eu/v3/notices/search"
    since = (NOW - timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
    fields = ["publication-number", "notice-title", "buyer-name", "buyer-city",
              "classification-cpv", "deadline-receipt-tender-date-lot", "notice-type",
              "estimated-value-lot", "estimated-value-cur-lot"]
    out = []
    for page in range(1, 11):
        body = {"query": f"buyer-country=POL AND publication-date>={since}",
                "fields": fields, "limit": 250, "page": page}
        r = S.post(url, json=body, timeout=60)
        r.raise_for_status()
        notices = r.json().get("notices", [])
        if DEBUG and page == 1:
            log("TED przykładowy rekord:", json.dumps(notices[:1], ensure_ascii=False)[:2000])
        for n in notices:
            pub = text_of(n.get("publication-number"))
            ntype = text_of(n.get("notice-type")).lower()
            if ntype and not (ntype.startswith("cn") or ntype.startswith("pin")):
                continue  # tylko ogłoszenia o zamówieniu i wstępne (przyszłe)
            val = None
            try:
                v = n.get("estimated-value-lot")
                v = v[0] if isinstance(v, list) else v
                cur = text_of(n.get("estimated-value-cur-lot")).upper()
                if v and (not cur or "PLN" in cur):
                    val = float(v)
            except Exception:
                pass
            out.append({
                "id": "ted:" + pub,
                "source": "TED (planowany)" if ntype.startswith("pin") else "TED",
                "title": text_of(n.get("notice-title")),
                "buyer": text_of(n.get("buyer-name")),
                "city": text_of(n.get("buyer-city")),
                "province": "",
                "cpv": text_of(n.get("classification-cpv")),
                "deadline": fmt_date(n.get("deadline-receipt-tender-date-lot")),
                "value_pln": val,
                "url": f"https://ted.europa.eu/pl/notice/-/detail/{pub}",
            })
        if len(notices) < 250:
            break
    log(f"TED: pobrano {len(out)} ogłoszeń")
    return out


# ---------- Gemini ----------
def gemini(prompt, search=False, as_json=False):
    key = os.environ["GEMINI_API_KEY"]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    if search:
        body["tools"] = [{"google_search": {}}]
    if as_json:
        body["generationConfig"] = {"responseMimeType": "application/json"}
    r = S.post(url, params={"key": key}, json=body, timeout=180)
    if r.status_code != 200:
        log(f"Gemini {r.status_code}: {r.text[:400]}")
    r.raise_for_status()
    parts = r.json()["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts)


def judge(cands):
    """Gemini ocenia przydatność i pisze krótki opis."""
    if not cands:
        return []
    slim = [{"id": c["id"], "tytul": c["title"][:300], "zamawiajacy": c["buyer"], "cpv": c["cpv"][:200]}
            for c in cands]
    prompt = (
        f"{PROFILE}\n\nOceń poniższe ogłoszenia. Dla każdego zwróć obiekt JSON: "
        '{"id": ..., "pasuje": true/false, "opis": "max 10 słów po polsku: co i ile drukować"}. '
        "pasuje=true tylko gdy drukarnia realnie może wykonać zlecenie. "
        "Zwróć wyłącznie tablicę JSON.\n\n" + json.dumps(slim, ensure_ascii=False)
    )
    res = json.loads(gemini(prompt, as_json=True))
    by_id = {x.get("id"): x for x in res if isinstance(x, dict)}
    keep = []
    for c in cands:
        j = by_id.get(c["id"])
        if j is None or j.get("pasuje"):
            c["summary"] = (j or {}).get("opis") or c["title"]
            keep.append(c)
    return keep


WEB_QUERIES = [  # 12 zapytań x 2 przebiegi/dzień = ok. 720 z 1000 darmowych miesięcznie
    "zapytanie ofertowe druk ulotek plakatów folderów",
    "zapytanie ofertowe druk kalendarzy 2027",
    "zapytanie ofertowe druk katalogów broszur",
    "zapytanie ofertowe druk teczek zaproszeń wizytówek",
    "zapytanie ofertowe druk materiałów promocyjnych informacyjnych projekt unijny",
    "zapytanie ofertowe druk wielkoformatowy banery roll-up",
    "zapytanie ofertowe opakowania kartonowe papierowe nadruk",
    "rozeznanie rynku usługa poligraficzna druk",
    "zaproszenie do złożenia oferty usługi drukarskie",
    "bip zapytanie cenowe wykonanie druku",
    "zapytanie ofertowe gadżety reklamowe długopisy kubki z nadrukiem",
    "zapytanie ofertowe naklejki notesy karteczki z logo",
]


def web_search():
    """Darmowe wyszukiwanie (Tavily, 1000 zapytań/mies.) + filtr Gemini."""
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        return []
    hits, urls = [], set()
    for q in WEB_QUERIES:
        r = S.post("https://api.tavily.com/search", timeout=60,
                   headers={"Authorization": f"Bearer {key}"},
                   json={"query": q, "search_depth": "basic", "max_results": 10,
                         "time_range": "week", "country": "poland"})
        if r.status_code != 200:
            log(f"Tavily {r.status_code}: {r.text[:300]}")
        r.raise_for_status()
        for h in r.json().get("results", []):
            u = h.get("url", "")
            if u and u not in urls:
                urls.add(u)
                hits.append({"url": u, "tytul": h.get("title", ""), "fragment": (h.get("content") or "")[:600]})
    log(f"Internet: {len(hits)} wyników wyszukiwania")
    if not hits:
        return []
    prompt = (
        f"{PROFILE}\n\nDzisiaj jest {NOW:%Y-%m-%d}. Poniżej wyniki wyszukiwania stron internetowych. "
        "Wybierz TYLKO te, które są aktualnym zapytaniem ofertowym/przetargiem w Polsce (firma, urząd, szkoła itp. "
        "szuka wykonawcy druku), pasującym do drukarni, z terminem składania ofert w przyszłości lub bez podanego terminu. "
        "Odrzuć: oferty drukarni, sklepy, poradniki, ogłoszenia z minionym terminem, rozstrzygnięcia. "
        'Zwróć tablicę JSON: [{"url":..., "tytul":..., "zamawiajacy":..., "termin":"RRRR-MM-DD lub null", '
        '"wartosc_pln": liczba lub null, "opis":"max 10 słów: co drukować"}]. Jeśli nic nie pasuje, zwróć [].\n\n'
        + json.dumps(hits, ensure_ascii=False)
    )
    items = json.loads(gemini(prompt, as_json=True))
    out = []
    for it in items if isinstance(items, list) else []:
        url = text_of(it.get("url"))
        if url not in urls:
            continue  # tylko linki faktycznie znalezione, bez wymyślonych
        val = it.get("wartosc_pln")
        out.append({
            "id": "web:" + url.split("#")[0].rstrip("/"),
            "source": "Strony internetowe (do weryfikacji)",
            "title": text_of(it.get("tytul")), "buyer": text_of(it.get("zamawiajacy")),
            "city": "", "province": "", "cpv": "",
            "deadline": fmt_date(it.get("termin")),
            "value_pln": float(val) if isinstance(val, (int, float)) else None,
            "summary": text_of(it.get("opis")) or text_of(it.get("tytul")),
            "url": url,
        })
    log(f"Internet: pasujących {len(out)}")
    return out


# ---------- e-mail ----------
def is_maz(c):
    p = (c.get("province", "") + " " + c.get("city", "")).lower()
    return "mazowieck" in p or "pl14" in p or "warszaw" in p


def line(c):
    star = "⭐ " if is_maz(c) else ""
    bits = [f"<b>{star}Przetarg: {escape(c.get('summary') or c['title'])}</b>"]
    if c["buyer"]:
        bits.append(escape(c["buyer"] + (f" ({c['city']})" if c["city"] else "")))
    if c["value_pln"]:
        bits.append(f"ok. {int(c['value_pln']):,} zł".replace(",", " "))
    if c["deadline"]:
        bits.append(f"oferty do {escape(c['deadline'])}")
    bits.append(f'<a href="{escape(c["url"])}">link</a>')
    return "<li>" + " · ".join(bits) + "</li>"


def send(items):
    when = NOW.strftime("%d.%m.%Y %H:%M")
    if items:
        groups = {}
        for c in sorted(items, key=lambda c: (not is_maz(c), c["source"])):
            groups.setdefault(c["source"], []).append(c)
        html = f"<p>Nowe przetargi dla WIP-Druk ({when}). ⭐ = Mazowsze.</p>"
        for src, lst in groups.items():
            html += f"<h3>{escape(src)} ({len(lst)})</h3><ul>" + "".join(line(c) for c in lst) + "</ul>"
        subject = f"Przetargi WIP-Druk: {len(items)} nowych ({NOW:%d.%m})"
    else:
        html = f"<p>Brak nowych przetargów ({when}).</p>"
        subject = f"Przetargi WIP-Druk: brak nowych ({NOW:%d.%m %H:%M})"
    if WARNINGS:
        html += "<p style='color:#888'>Uwaga: " + escape("; ".join(WARNINGS)) + "</p>"
    html += "<p style='color:#888;font-size:12px'>Wiadomość automatyczna. Dane z BZP, TED i wyszukiwania internetowego; zawsze sprawdź szczegóły w ogłoszeniu.</p>"
    r = S.post("https://api.brevo.com/v3/smtp/email", timeout=60,
               headers={"api-key": os.environ["BREVO_API_KEY"], "accept": "application/json"},
               json={"sender": {"email": os.environ["MAIL_FROM"], "name": "Przetargi WIP-Druk"},
                     "to": [{"email": e.strip()} for e in os.environ["MAIL_TO"].split(",")],
                     "subject": subject, "htmlContent": html})
    r.raise_for_status()
    log("Wysłano:", subject)


# ---------- main ----------
def main():
    try:
        seen = json.load(open(SEEN_FILE, encoding="utf-8"))
    except Exception:
        seen = {}

    raw, ok_sources = [], 0
    for name, fn in (("BZP", fetch_bzp), ("TED", fetch_ted)):
        try:
            raw += fn()
            ok_sources += 1
        except Exception as e:
            log(f"BŁĄD {name}: {e}")
            WARNINGS.append(f"źródło {name} nie odpowiedziało")

    fresh = [c for c in raw if c["id"] not in seen]
    cands = [c for c in fresh if matches(c["title"], c["cpv"])]
    log(f"Nowe: {len(fresh)}, wstępnie pasujące: {len(cands)}")

    try:
        cands = judge(cands)
    except Exception as e:
        log(f"BŁĄD Gemini (ocena): {e}")
        WARNINGS.append("ocena AI niedostępna - pokazano wszystkie dopasowania")
        for c in cands:
            c["summary"] = c["title"]

    if WEB_SEARCH:
        try:
            cands += [c for c in web_search() if c["id"] not in seen]
        except Exception as e:
            log(f"BŁĄD Gemini (internet): {e}")
            WARNINGS.append("wyszukiwanie internetowe niedostępne")

    final, ids = [], set()
    for c in cands:
        if c["id"] in ids:
            continue
        if c["value_pln"] is not None and c["value_pln"] < MIN_VALUE_PLN:
            continue
        ids.add(c["id"])
        final.append(c)

    if ok_sources == 0 and not final:
        log("Żadne źródło nie działa - przerywam (GitHub wyśle powiadomienie o błędzie).")
        sys.exit(1)

    send(final)

    today = NOW.strftime("%Y-%m-%d")
    for c in raw:            # wszystkie pobrane oznaczamy jako widziane
        seen.setdefault(c["id"], today)
    for c in final:
        seen.setdefault(c["id"], today)
    cutoff = (NOW - timedelta(days=60)).strftime("%Y-%m-%d")
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    json.dump(seen, open(SEEN_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=0)


if __name__ == "__main__":
    main()
