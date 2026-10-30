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
    """Wywołanie Gemini z ponawianiem (429/503) i modelem zapasowym."""
    import time
    key = os.environ["GEMINI_API_KEY"]
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    if search:
        body["tools"] = [{"google_search": {}}]
    if as_json:
        body["generationConfig"] = {"responseMimeType": "application/json"}
    last = None
    for model in (GEMINI_MODEL, "gemini-flash-lite-latest"):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(3):
            r = S.post(url, params={"key": key}, json=body, timeout=180)
            if r.status_code == 200:
                parts = r.json()["candidates"][0]["content"]["parts"]
                return "".join(p.get("text", "") for p in parts)
            log(f"Gemini {model} {r.status_code} (próba {attempt + 1}): {r.text[:200]}")
            last = r
            if r.status_code not in (429, 500, 503):
                break
            time.sleep(20 * (attempt + 1))
    last.raise_for_status()


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


BK = ["bazakonkurencyjnosci.funduszeeuropejskie.gov.pl"]
# (zapytanie, domeny lub None, grupa). Ok. 14 zapytań x 2 przebiegi/dzień = ok. 840 z 1000 darmowych/mies.
WEB_QUERIES = [
    # Baza Konkurencyjności - firmy z dotacjami UE muszą tu publikować zapytania
    ("zapytanie ofertowe druk materiałów promocyjnych ulotek roll-up", BK, "przetarg"),
    ("zapytanie ofertowe gadżety reklamowe z logo długopisy kubki", BK, "przetarg"),
    ("zapytanie ofertowe druk katalogów broszur kalendarzy folderów", BK, "przetarg"),
    ("zapytanie ofertowe opakowania kartonowe nadruk etykiety naklejki", BK, "przetarg"),
    # firmy prywatne poza BK
    ("sp. z o.o. zapytanie ofertowe druk materiałów reklamowych", None, "przetarg"),
    ("projekt dofinansowany zapytanie ofertowe materiały promocyjne firma", None, "przetarg"),
    ("firma zapytanie ofertowe opakowania kartonowe z nadrukiem", None, "przetarg"),
    ("zapytanie ofertowe gadżety reklamowe naklejki notesy z logo firma", None, "przetarg"),
    # ogólne (także urzędy)
    ("zapytanie ofertowe druk ulotek plakatów kalendarzy 2027", None, "przetarg"),
    ("zapytanie ofertowe druk wielkoformatowy banery roll-up", None, "przetarg"),
    ("rozeznanie rynku usługa poligraficzna druk", None, "przetarg"),
    ("bip zapytanie cenowe wykonanie druku", None, "przetarg"),
    # potencjalni klienci: firmy, które dostały dotację na promocję/targi/eksport
    ("firma otrzymała dofinansowanie promocja marki targi zagraniczne 2026", None, "lead"),
    ("lista projektów wybranych do dofinansowania internacjonalizacja MŚP promocja 2026", None, "lead"),
]


def web_search():
    """Darmowe wyszukiwanie (Tavily) + ocena Gemini. Zwraca (przetargi, leady)."""
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        return [], []
    hits, urls = [], set()
    for q, domains, grp in WEB_QUERIES:
        body = {"query": q, "search_depth": "basic", "max_results": 10, "time_range": "week"}
        if domains:
            body["include_domains"] = domains
        else:
            body["country"] = "poland"
        try:
            r = S.post("https://api.tavily.com/search", timeout=60,
                       headers={"Authorization": f"Bearer {key}"}, json=body)
            if r.status_code != 200:
                log(f"Tavily {r.status_code}: {r.text[:200]}")
                continue
        except Exception as e:
            log(f"Tavily błąd: {e}")
            continue
        for h in r.json().get("results", []):
            u = h.get("url", "")
            if u and u not in urls:
                urls.add(u)
                hits.append({"url": u, "grupa": grp, "tytul": h.get("title", ""),
                             "fragment": (h.get("content") or "")[:600]})
    log(f"Internet: {len(hits)} wyników wyszukiwania")
    if not hits:
        return [], []
    prompt = (
        f"{PROFILE}\nDrukarnia pracuje w 90% dla FIRM prywatnych.\n\nDzisiaj jest {NOW:%Y-%m-%d}. "
        "Poniżej wyniki wyszukiwania. Zwróć obiekt JSON z dwiema listami:\n"
        '1) "przetargi": TYLKO aktualne zapytania ofertowe/przetargi w Polsce pasujące do drukarni '
        "(termin w przyszłości lub brak terminu). Odrzuć oferty drukarni, sklepy, poradniki, rozstrzygnięcia, minione terminy. "
        'Pola: {"url", "tytul", "zamawiajacy", "typ": "firma" lub "instytucja", "dotacja": true jeśli finansowane z UE/dotacji, '
        '"termin": "RRRR-MM-DD" lub null, "wartosc_pln": liczba lub null, "opis": "max 10 słów: co drukować"}.\n'
        '2) "leady": FIRMY prywatne (nie urzędy), które niedawno dostały dotację/dofinansowanie i mogą potrzebować '
        "druku (promocja, targi, nowy produkt, eksport). Tylko gdy nazwa firmy jest w wyniku. "
        'Pola: {"url", "firma", "powod": "max 12 słów: na co dotacja i co mogą chcieć drukować"}.\n'
        "Nie podawaj danych osobowych (imion, telefonów, maili). Jeśli nic nie pasuje, zwróć puste listy.\n\n"
        + json.dumps(hits, ensure_ascii=False)
    )
    res = json.loads(gemini(prompt, as_json=True))
    if isinstance(res, list):
        res = {"przetargi": res, "leady": []}
    out, leads = [], []
    for it in res.get("przetargi", []) or []:
        url = text_of(it.get("url"))
        if url not in urls:
            continue  # tylko linki faktycznie znalezione
        val = it.get("wartosc_pln")
        firma = text_of(it.get("typ")).lower().startswith("firma")
        out.append({
            "id": "web:" + url.split("#")[0].rstrip("/"),
            "source": "Firmy prywatne" if firma else "Strony internetowe – instytucje (do weryfikacji)",
            "title": text_of(it.get("tytul")), "buyer": text_of(it.get("zamawiajacy")),
            "city": "", "province": "", "cpv": "",
            "deadline": fmt_date(it.get("termin")),
            "value_pln": float(val) if isinstance(val, (int, float)) else None,
            "summary": text_of(it.get("opis")) or text_of(it.get("tytul")),
            "url": url, "firm": firma, "grant": bool(it.get("dotacja")),
        })
    for it in res.get("leady", []) or []:
        url = text_of(it.get("url"))
        if url not in urls or not text_of(it.get("firma")):
            continue
        leads.append({"id": "lead:" + url.split("#")[0].rstrip("/") + "|" + text_of(it.get("firma")).lower(),
                      "firma": text_of(it.get("firma")), "powod": text_of(it.get("powod")), "url": url})
    log(f"Internet: pasujących przetargów {len(out)}, potencjalnych klientów {len(leads)}")
    return out, leads


# ---------- e-mail ----------
def is_maz(c):
    p = (c.get("province", "") + " " + c.get("city", "")).lower()
    return "mazowieck" in p or "pl14" in p or "warszaw" in p


def line(c):
    star = "⭐ " if is_maz(c) else ""
    badge = ""
    if c.get("firm") and c.get("grant"):
        badge = ' <span style="background:#d4f5d4;padding:1px 6px;border-radius:4px">📞 KONTAKT REKOMENDOWANY (firma z dotacją)</span>'
    elif c.get("firm"):
        badge = " 🏢"
    bits = [f"<b>{star}Przetarg: {escape(c.get('summary') or c['title'])}</b>{badge}"]
    if c["buyer"]:
        bits.append(escape(c["buyer"] + (f" ({c['city']})" if c["city"] else "")))
    if c["value_pln"]:
        bits.append(f"ok. {int(c['value_pln']):,} zł".replace(",", " "))
    if c["deadline"]:
        bits.append(f"oferty do {escape(c['deadline'])}")
    bits.append(f'<a href="{escape(c["url"])}">link</a>')
    return "<li>" + " · ".join(bits) + "</li>"


def send(items, leads):
    when = NOW.strftime("%d.%m.%Y %H:%M")
    order = {"Firmy prywatne": 0}
    if items or leads:
        groups = {}
        for c in sorted(items, key=lambda c: (order.get(c["source"], 1), not c.get("grant"), not is_maz(c), c["source"])):
            groups.setdefault(c["source"], []).append(c)
        html = f"<p>Nowe przetargi dla WIP-Druk ({when}). 🏢 = firma · 📞 = kontakt rekomendowany · ⭐ = Mazowsze.</p>"
        for src, lst in groups.items():
            html += f"<h3>{escape(src)} ({len(lst)})</h3><ul>" + "".join(line(c) for c in lst) + "</ul>"
        if leads:
            html += f"<h3>🎯 Potencjalni klienci – firmy z nową dotacją ({len(leads)})</h3><ul>"
            for l in leads:
                html += (f"<li><b>📞 Kontakt rekomendowany: {escape(l['firma'])}</b> · {escape(l['powod'])} · "
                         f'<a href="{escape(l["url"])}">źródło</a></li>')
            html += "</ul><p style='color:#888;font-size:12px'>Potencjalni klienci to tylko informacja – nic nie jest wysyłane automatycznie.</p>"
        n = len(items)
        word = "nowy" if n == 1 else ("nowe" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "nowych")
        subject = f"Przetargi WIP-Druk: {n} {word}" + (f" · potencjalni klienci: {len(leads)}" if leads else "") + f" ({NOW:%d.%m, %H:%M})"
    else:
        html = f"<p>Brak nowych przetargów ({when}).</p>"
        subject = f"Przetargi WIP-Druk: brak nowych ({NOW:%d.%m, %H:%M})"
    if WARNINGS:
        html += "<p style='color:#888'>Uwaga: " + escape("; ".join(WARNINGS)) + "</p>"
    html += "<p style='color:#888;font-size:12px'>Wiadomość automatyczna. Dane z BZP, TED, Bazy Konkurencyjności i wyszukiwania internetowego; zawsze sprawdź szczegóły w ogłoszeniu.</p>"
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

    leads = []
    if WEB_SEARCH:
        try:
            web, lds = web_search()
            cands += [c for c in web if c["id"] not in seen]
            leads = [l for l in lds if l["id"] not in seen]
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

    send(final, leads)

    today = NOW.strftime("%Y-%m-%d")
    for c in raw:            # wszystkie pobrane oznaczamy jako widziane
        seen.setdefault(c["id"], today)
    for c in final + leads:
        seen.setdefault(c["id"], today)
    cutoff = (NOW - timedelta(days=60)).strftime("%Y-%m-%d")
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    json.dump(seen, open(SEEN_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=0)


if __name__ == "__main__":
    main()
