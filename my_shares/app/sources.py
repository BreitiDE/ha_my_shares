"""Datenquellen und Umrechnung auf den Index 0–10.

0 = stark verkaufen · 5 = halten · 10 = stark kaufen.
Alle Abrufe laufen über curl_cffi mit Chrome-Fingerabdruck, weil Yahoo
einfache HTTP-Clients mit HTTP 429 abweist.
"""
import asyncio
import logging
import re
import time
from urllib.parse import quote

from curl_cffi.requests import AsyncSession

log = logging.getLogger("my_shares.sources")

TIMEOUT = 20
SOURCE_KEYS = ("yahoo", "finnhub", "nasdaq", "marketscreener", "target", "technical")
US_ONLY = ("finnhub", "nasdaq")            # liefern nur für US-Aktien Daten

# Bloomberg-Börsencodes (OpenFIGI) -> Yahoo-Endung und Anzeigename
EXCHANGES = {
    "GY": (".DE", "Xetra"), "GR": (".F", "Frankfurt"), "GF": (".F", "Frankfurt"), "GD": (".DU", "Düsseldorf"),
    "GM": (".MU", "München"), "GS": (".SG", "Stuttgart"), "GH": (".HM", "Hamburg"), "GB": (".BE", "Berlin"),
    "GI": (".HA", "Hannover"),
    "US": ("", "USA"), "UN": ("", "NYSE"), "UW": ("", "Nasdaq"), "UQ": ("", "Nasdaq"), "UA": ("", "NYSE American"),
    "UR": ("", "Nasdaq"), "UP": ("", "NYSE Arca"),
    "LN": (".L", "London"), "FP": (".PA", "Paris"), "NA": (".AS", "Amsterdam"), "BB": (".BR", "Brüssel"),
    "SW": (".SW", "SIX"), "SE": (".SW", "SIX"), "VX": (".SW", "SIX"), "IM": (".MI", "Mailand"),
    "SM": (".MC", "Madrid"), "AV": (".VI", "Wien"), "DC": (".CO", "Kopenhagen"), "SS": (".ST", "Stockholm"),
    "NO": (".OL", "Oslo"), "FH": (".HE", "Helsinki"), "PL": (".LS", "Lissabon"), "ID": (".IR", "Dublin"),
    "CN": (".TO", "Toronto"), "CT": (".TO", "Toronto"), "JP": (".T", "Tokio"), "JT": (".T", "Tokio"),
    "HK": (".HK", "Hongkong"), "AU": (".AX", "Sydney"), "AT": (".AX", "Sydney"),
}
PREFERRED = ["GY", "US", "UN", "UW", "UQ", "LN", "FP", "NA", "SW", "SE", "IM", "SM", "AV", "BB", "GR", "GF"]

ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")
WKN_RE = re.compile(r"^[A-Z0-9]{6}$")


class SourceError(Exception):
    """Fehler mit Status für die Oberfläche: nodata, noaccess, error."""

    def __init__(self, status, msg=""):
        super().__init__(msg or status)
        self.status = status
        self.msg = msg


def is_us(symbol):
    """Yahoo-Symbole von US-Börsen haben keine Börsen-Endung (.DE, .L, …)."""
    return "." not in symbol and not symbol.startswith("^")


def parse_en_number(txt):
    return float(txt.replace(",", ""))


# MarketScreener-Konsens -> Skala 1 (stark kaufen) … 5 (stark verkaufen)
MS_CONSENSUS = {"BUY": 1, "STRONG BUY": 1, "OUTPERFORM": 2, "ACCUMULATE": 2, "OVERWEIGHT": 2,
                "HOLD": 3, "NEUTRAL": 3, "UNDERPERFORM": 4, "REDUCE": 4, "UNDERWEIGHT": 4, "SELL": 5, "STRONG SELL": 5}
MS_BASE = "https://www.marketscreener.com"
MS_QUOTE_RE = re.compile(r"/quote/stock/([A-Za-z0-9\-]+-\d+)/")


MS_ANY_RE = re.compile(r"marketscreener\.com/[a-z]+/[a-z]+/([A-Za-z0-9\-]+-\d+)(?:/|$|\?)")


def ms_normalize(url):
    """Beliebige MarketScreener-Adresse einer Aktie (auch de./uk./…, /kurs/aktie/) -> www.…/quote/stock/NAME-ID/"""
    m = MS_ANY_RE.search(url or "")
    return f"{MS_BASE}/quote/stock/{m.group(1)}/" if m else None


def html_text(html):
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    txt = re.sub(r"(?s)<[^>]+>", " ", html)
    txt = txt.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", txt)


def parse_ms_consensus(html):
    """Liest Konsens, Analystenzahl, letzten Kurs und mittleres Kursziel aus der Konsens-Seite."""
    t = html_text(html)
    m = re.search(r"Mean consensus\s+([A-Za-z][A-Za-z ]{1,20}?)\s+Number of Analysts\s+(\d+)", t)
    if not m:
        raise SourceError("nodata", "kein Analystenkonsens auf der Seite")
    label = m.group(1).strip().upper()
    if label not in MS_CONSENSUS:
        raise SourceError("nodata", f"unbekannte Einstufung „{label}“")
    out = {"label": label, "n": int(m.group(2)), "mean": MS_CONSENSUS[label]}
    num = r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*([A-Z]{3})"
    lc = re.search(r"Last Close Price\s+" + num, t)
    at = re.search(r"Average target price\s+" + num, t)
    if lc and at and lc.group(2) == at.group(2):
        last, avg = parse_en_number(lc.group(1)), parse_en_number(at.group(1))
        if last > 0:
            out.update(last=last, target=avg, currency=at.group(2), pot=avg / last - 1)
    return out


SECRET_PARAMS = {"token", "apikey", "api_key", "crumb", "key"}


def show_url(url, params=None):
    """Adresse fürs Log: ohne https://www., ohne Keys."""
    from urllib.parse import parse_qsl, urlencode, urlsplit
    parts = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(parts.query) if k.lower() not in SECRET_PARAMS]
    q += [(k, v) for k, v in (params or {}).items() if k.lower() not in SECRET_PARAMS]
    host = parts.netloc[4:] if parts.netloc.startswith("www.") else parts.netloc
    return host + parts.path + ("?" + urlencode(q, safe=",") if q else "")


def clamp(x):
    return max(0.0, min(10.0, x))


def lin(value, span):
    """±span wird auf 0…10 abgebildet, 0 auf 5."""
    return clamp(5 + value / span * 5)


def raw(v):
    if isinstance(v, dict):
        return v.get("raw")
    return v


def counts_score(c):
    keys = ("strongBuy", "buy", "hold", "sell", "strongSell")
    sb, b, h, s, ss = (int(c.get(k) or 0) for k in keys)
    n = sb + b + h + s + ss
    if not n:
        return None, 0
    return round((10 * sb + 7.5 * b + 5 * h + 2.5 * s) / n, 2), n


def rsi(closes, period=14):
    if len(closes) <= period:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def technical(closes):
    """Trend (200-Tage-Linie), Momentum 3 und 12 Monate, RSI (gegenläufig)."""
    c = [x for x in closes if x]
    if len(c) < 60:
        return None, {}
    p = c[-1]
    parts, vals = {}, []
    if len(c) >= 200:
        d = p / (sum(c[-200:]) / 200) - 1
        parts["sma200"] = d
        vals.append(lin(d, 0.20))
    if len(c) >= 64:
        m3 = p / c[-64] - 1
        parts["m3"] = m3
        vals.append(lin(m3, 0.25))
    if len(c) >= 253:
        m12 = p / c[-253] - 1
        parts["m12"] = m12
        vals.append(lin(m12, 0.50))
    r = rsi(c[-300:])
    if r is not None:
        parts["rsi"] = r
        vals.append(clamp((70 - r) / 40 * 10))   # RSI 30 -> 10, 50 -> 5, 70 -> 0
    return (round(sum(vals) / len(vals), 2) if vals else None), parts


class Fetcher:
    def __init__(self):
        self.s = AsyncSession(impersonate="chrome", timeout=TIMEOUT)
        self.crumb = None
        self.calls = []          # Mitschnitt der Abrufe fürs Log: (Text, Millisekunden)

    async def _req(self, method, url, **kw):
        """HTTP-Abruf mit Mitschnitt. API-Keys und Crumb tauchen im Mitschnitt nie auf."""
        shown = show_url(url, kw.get("params"))
        t0 = time.monotonic()
        try:
            r = await self.s.request(method, url, **kw)
        except Exception as e:
            self.calls.append((f"{method} {shown} → {type(e).__name__}", int((time.monotonic() - t0) * 1000)))
            raise
        self.calls.append((f"{method} {shown} → {r.status_code}", int((time.monotonic() - t0) * 1000)))
        return r

    def take_calls(self):
        """Gibt die Abrufe seit dem letzten Aufruf zurück: (Detailtext, Gesamtdauer ms)."""
        calls, self.calls = self.calls, []
        if not calls:
            return None, None
        return " · ".join(c for c, _ in calls), sum(ms for _, ms in calls)

    async def close(self):
        try:
            await self.s.close()
        except Exception:  # noqa: BLE001
            pass

    async def _json(self, method, url, **kw):
        try:
            r = await self._req(method, url, **kw)
        except Exception as e:  # noqa: BLE001
            raise SourceError("error", f"nicht erreichbar ({type(e).__name__})") from e
        if r.status_code in (401, 402, 403):
            raise SourceError("noaccess", f"HTTP {r.status_code}")
        if r.status_code == 429:
            raise SourceError("error", "zu viele Abrufe (HTTP 429)")
        if r.status_code == 404:
            raise SourceError("nodata", "HTTP 404")
        if r.status_code >= 400:
            raise SourceError("error", f"HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            raise SourceError("error", "Antwort ist kein JSON") from e

    # ---------- Wechselkurse (EZB) ----------
    async def ecb(self):
        from fx import ECB_URL, parse_ecb
        try:
            r = await self._req("GET", ECB_URL)
        except Exception as e:  # noqa: BLE001
            raise SourceError("error", f"EZB nicht erreichbar ({type(e).__name__})") from e
        if r.status_code != 200:
            raise SourceError("error", f"EZB: HTTP {r.status_code}")
        try:
            return parse_ecb(r.text)
        except ValueError as e:
            raise SourceError("error", str(e)) from e

    # ---------- Suche: WKN / ISIN / Symbol ----------
    async def lookup(self, query):
        q = query.strip().upper().replace(" ", "")
        results, errors, tried = [], 0, 0
        if ISIN_RE.match(q) or WKN_RE.match(q):
            tried += 1
            try:
                results = await self._openfigi(q)
            except SourceError as e:
                errors += 1
                log.warning("OpenFIGI: %s", e.msg)
        if not results:
            tried += 1
            try:
                results = await self._yahoo_search(query.strip())
            except SourceError as e:
                errors += 1
                log.warning("Yahoo-Suche: %s", e.msg)
        if not results and errors == tried:
            raise SourceError("error", "Suche nicht erreichbar, bitte später erneut versuchen")
        return results[:10]

    async def _openfigi(self, q):
        id_type = "ID_ISIN" if ISIN_RE.match(q) else "ID_WERTPAPIER"
        data = await self._json("POST", "https://api.openfigi.com/v3/mapping",
                                json=[{"idType": id_type, "idValue": q}],
                                headers={"Content-Type": "application/json"})
        items = (data[0] or {}).get("data") or [] if data else []
        seen, out = set(), []
        for it in items:
            code = it.get("exchCode")
            if code not in EXCHANGES or not it.get("ticker"):
                continue
            suffix, exname = EXCHANGES[code]
            sym = it["ticker"].replace("/", "-").replace(" ", "-") + suffix
            if sym in seen:
                continue
            seen.add(sym)
            out.append({"symbol": sym, "name": (it.get("name") or "").title(), "exchange": exname,
                        "type": it.get("securityType2") or it.get("securityType") or "",
                        "rank": PREFERRED.index(code) if code in PREFERRED else 99})
        out.sort(key=lambda x: x["rank"])
        return out

    async def _yahoo_search(self, q):
        data = await self._json("GET", "https://query2.finance.yahoo.com/v1/finance/search",
                                params={"q": q, "quotesCount": 10, "newsCount": 0, "listsCount": 0})
        out = []
        for it in data.get("quotes") or []:
            if not it.get("symbol") or it.get("quoteType") not in ("EQUITY", "ETF", "MUTUALFUND"):
                continue
            out.append({"symbol": it["symbol"], "name": it.get("longname") or it.get("shortname") or it["symbol"],
                        "exchange": it.get("exchDisp") or it.get("exchange") or "",
                        "type": it.get("typeDisp") or it.get("quoteType") or "", "rank": 50})
        return out

    # ---------- Yahoo ----------
    async def yahoo_chart(self, symbol):
        data = await self._json("GET", f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                                params={"range": "2y", "interval": "1d"})
        res = ((data.get("chart") or {}).get("result") or [None])[0]
        if not res:
            err = ((data.get("chart") or {}).get("error") or {}).get("description")
            raise SourceError("nodata", err or "Symbol unbekannt")
        meta = res.get("meta") or {}
        closes = (((res.get("indicators") or {}).get("quote") or [{}])[0].get("close")) or []
        closes = [c for c in closes if c]
        price = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
        if not price:
            raise SourceError("nodata", "kein Kurs")
        prev = closes[-2] if len(closes) >= 2 else None
        if closes and abs(closes[-1] - price) / price > 0.0005:
            prev = closes[-1]
        return {"price": price, "currency": meta.get("currency"),
                "name": meta.get("longName") or meta.get("shortName"),
                "exchange": meta.get("fullExchangeName") or meta.get("exchangeName"),
                "change_pct": (price / prev - 1) if prev else None, "closes": closes}

    async def _yahoo_crumb(self, force=False):
        if self.crumb and not force:
            return self.crumb
        for warm in ("https://fc.yahoo.com", "https://finance.yahoo.com/"):
            try:
                await self._req("GET", warm, allow_redirects=True)
            except Exception:  # noqa: BLE001
                pass
            try:
                r = await self._req("GET", "https://query1.finance.yahoo.com/v1/test/getcrumb")
                t = (r.text or "").strip()
                if r.status_code == 200 and t and "<" not in t and len(t) < 40:
                    self.crumb = t
                    return t
            except Exception:  # noqa: BLE001
                pass
        raise SourceError("error", "Yahoo-Anmeldung (Crumb) fehlgeschlagen")

    async def yahoo_summary(self, symbol):
        url = f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
        for attempt in (0, 1):
            crumb = await self._yahoo_crumb(force=attempt == 1)
            try:
                data = await self._json("GET", url, params={"modules": "financialData,price", "crumb": crumb})
            except SourceError as e:
                if e.status == "noaccess" and attempt == 0:
                    continue
                raise
            res = ((data.get("quoteSummary") or {}).get("result") or [None])[0]
            if not res:
                raise SourceError("nodata", "keine Analystendaten")
            fd = res.get("financialData") or {}
            return {"mean": raw(fd.get("recommendationMean")),
                    "n": raw(fd.get("numberOfAnalystOpinions")) or 0,
                    "target": raw(fd.get("targetMeanPrice")),
                    "price": raw(fd.get("currentPrice"))}
        raise SourceError("error", "Yahoo lehnt den Abruf ab")

    # ---------- Finnhub ----------
    async def finnhub(self, symbol, key):
        data = await self._json("GET", "https://finnhub.io/api/v1/stock/recommendation",
                                params={"symbol": symbol, "token": key})
        if isinstance(data, dict) and data.get("error"):
            raise SourceError("noaccess", str(data["error"])[:80])
        if not data:
            raise SourceError("nodata")
        return data[0]

    # ---------- Nasdaq (nur US, ohne Key) ----------
    async def nasdaq(self, symbol):
        sym = symbol.replace("-", ".")            # Yahoo BRK-B -> Nasdaq BRK.B
        data = await self._json("GET", f"https://api.nasdaq.com/api/analyst/{sym}/targetprice",
                                headers={"Accept": "application/json, text/plain, */*",
                                         "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"})
        if not isinstance(data, dict):
            raise SourceError("error", "unerwartete Antwort")
        co = ((data.get("data") or {}).get("consensusOverview")) or {}
        if not co:
            raise SourceError("nodata")

        def num(v):
            if v in (None, "", "N/A", "--"):
                return None
            try:
                return float(str(v).replace("$", "").replace(",", ""))
            except ValueError:
                return None
        out = {"buy": int(num(co.get("buy")) or 0), "hold": int(num(co.get("hold")) or 0),
               "sell": int(num(co.get("sell")) or 0), "target": num(co.get("priceTarget"))}
        if not (out["buy"] + out["hold"] + out["sell"]):
            raise SourceError("nodata")
        return out

    # ---------- MarketScreener ----------
    async def _ms_get(self, url):
        try:
            r = await self._req("GET", url, headers={"Accept-Language": "en-US,en;q=0.9"}, allow_redirects=True)
        except Exception as e:  # noqa: BLE001
            raise SourceError("error", f"nicht erreichbar ({type(e).__name__})") from e
        if r.status_code in (403, 429, 503):
            raise SourceError("error", f"MarketScreener blockt den Abruf (HTTP {r.status_code})")
        if r.status_code == 404:
            raise SourceError("nodata", "Seite nicht gefunden")
        if r.status_code >= 400:
            raise SourceError("error", f"HTTP {r.status_code}")
        return r

    async def ms_search(self, query):
        """Kandidaten-Adressen (Reihenfolge wie auf der Suchseite)."""
        r = await self._ms_get(f"{MS_BASE}/search/?q={quote(query)}")
        direct = ms_normalize(str(r.url))
        if direct:
            return [direct]
        seen, out = set(), []
        for m in MS_QUOTE_RE.finditer(r.text):
            u = f"{MS_BASE}/quote/stock/{m.group(1)}/"
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out[:6]

    async def ms_consensus(self, base_url):
        r = await self._ms_get(base_url + "consensus/")
        return parse_ms_consensus(r.text)

async def ms_resolve(f, share, name, price, currency, fx):
    """Sucht die MarketScreener-Seite einer Aktie und prüft sie am Kurs (oder am Namen)."""
    from fx import convert
    queries = []
    ident = (share.get("ident") or "").upper()
    if ISIN_RE.match(ident):
        queries.append(ident)
    if name:
        queries.append(name)
    base = share["symbol"].split(".")[0]
    if base not in queries:
        queries.append(base)
    word = re.sub(r"[^a-z0-9]", "", (name or base).split()[0].lower())
    for q in queries[:2]:
        try:
            cands = await f.ms_search(q)
        except SourceError as e:
            if e.status == "error":
                raise
            continue
        for url in cands[:3]:
            await asyncio.sleep(1)
            try:
                cons = await f.ms_consensus(url)
            except SourceError as e:
                if e.status == "error":
                    raise
                continue
            slug = url.rstrip("/").rsplit("/", 1)[-1].lower().replace("-", "")
            ok = False
            if cons.get("last"):
                last = convert(cons["last"], cons["currency"], currency, fx)
                ok = last is not None and 0.75 < last / price < 1.33
                if last is None:                      # Währung nicht umrechenbar: Name muss passen
                    ok = bool(word) and word in slug
            else:
                ok = bool(word) and word in slug
            if ok:
                return url, cons
    raise SourceError("nodata", "Aktie auf MarketScreener nicht gefunden")


LOG_NAMES = {"yahoo": "Yahoo", "finnhub": "Finnhub", "nasdaq": "Nasdaq", "marketscreener": "MarketScreener"}
STATUS_LEVEL = {"ok": "ok", "nodata": "info", "usonly": "info", "nokey": "info", "noaccess": "warn",
                "limit": "err", "error": "err"}


def de(v, d=2):
    """Zahl im deutschen Format für Log-Meldungen."""
    if v is None:
        return "–"
    return f"{v:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def src_message(k, s, cur):
    st = s.get("status")
    if st == "usonly":
        return "Übersprungen: nur für US-Aktien"
    if st == "nokey":
        return "Übersprungen: kein API-Key"
    if st == "nodata":
        return "Keine Daten" + (f" ({s['msg']})" if s.get("msg") else "")
    if st == "noaccess":
        if k in ("nasdaq", "marketscreener", "yahoo"):
            return f"Abruf blockiert ({s.get('msg') or 'abgelehnt'}) – Index ohne diese Quelle berechnet"
        return f"Kein Zugriff ({s.get('msg') or 'abgelehnt'})"
    if st in ("error", "limit"):
        return f"Fehler: {s.get('msg') or 'unbekannt'}"
    tgt = f" · Kursziel {de(s['target'])} {cur}" if s.get("target") else ""
    if k == "yahoo":
        return f"Analystenkonsens {de(s['mean'])} · {s['n']} Analysten{tgt}"
    if k == "finnhub":
        return f"Empfehlungen {s.get('period') or ''}: " + " · ".join(str(c) for c in s.get("counts") or [])
    if k == "nasdaq":
        b, h, se = s.get("counts") or [0, 0, 0]
        return f"{b} kaufen · {h} halten · {se} verkaufen{tgt}"
    if k == "marketscreener":
        return f"Konsens „{s['label']}“ · {s['n']} Analysten" + (f" · Potenzial {de(s['pot'] * 100, 1)} %" if s.get("pot") is not None else "")
    return ""


async def evaluate(f, share, settings, ctx=None):
    """Holt alle Quellen für eine Aktie und berechnet den Index.

    ctx: {"fx": Wechselkurse, "log": log(level, quelle, meldung, detail, dauer)}.
    Ohne Kurs (Yahoo-Chart) wird SourceError geworfen.
    """
    ctx = ctx or {}
    log = ctx.get("log") or (lambda *a, **k: None)

    def log_src(k, extra="", warn=False):
        detail, dur = f.take_calls()
        s = src.get(k) or {}
        if s.get("status") in (None, "off"):
            return
        level = STATUS_LEVEL.get(s["status"], "info")
        if s["status"] == "noaccess" and k in ("nasdaq", "marketscreener", "yahoo"):
            level = "err"                      # Quellen ohne Key: Ablehnung heißt blockiert
        if warn and level == "ok":
            level = "warn"
        log(level, LOG_NAMES[k], src_message(k, s, chart["currency"]) + extra, detail, dur)

    sym = share["symbol"]
    w = settings["weights"]
    us = is_us(sym)
    src = {}
    ms_url = share.get("ms_url")

    def off(k):
        return float(w.get(k) or 0) <= 0

    f.take_calls()
    try:
        chart = await f.yahoo_chart(sym)
    except SourceError as e:
        detail, dur = f.take_calls()
        log("err", "Yahoo", f"Kurs nicht abrufbar: {e.msg}", detail, dur)
        raise
    price = chart["price"]
    detail, dur = f.take_calls()
    log("ok", "Yahoo", f"Kurs {de(price)} {chart['currency'] or ''} · {len(chart['closes'])} Tageskurse", detail, dur)
    name = chart["name"] or share.get("name") or sym
    pots = []          # Kursziel-Potenziale aller Quellen

    # 1 Yahoo-Analystenkonsens
    if off("yahoo"):
        src["yahoo"] = {"status": "off"}
    else:
        try:
            ysum = await f.yahoo_summary(sym)
            if ysum.get("target"):
                pots.append(ysum["target"] / price - 1)
            if ysum["mean"]:
                src["yahoo"] = {"status": "ok", "score": round(clamp((5 - ysum["mean"]) / 4 * 10), 2),
                                "mean": ysum["mean"], "n": ysum["n"], "target": ysum.get("target")}
            else:
                src["yahoo"] = {"status": "nodata"}
        except SourceError as e:
            src["yahoo"] = {"status": e.status, "msg": e.msg}
    log_src("yahoo")

    # 2 Finnhub (Key), 3 Nasdaq (ohne Key) – nur US-Aktien
    async def analyst(k):
        if off(k):
            src[k] = {"status": "off"}
            return
        if not us:
            src[k] = {"status": "usonly"}
            return
        if k == "finnhub" and not settings.get("finnhub_key"):
            src[k] = {"status": "nokey"}
            return
        try:
            if k == "finnhub":
                d = await f.finnhub(sym, settings["finnhub_key"])
                sc, n = counts_score(d)
                counts = [int(d.get(x) or 0) for x in ("strongBuy", "buy", "hold", "sell", "strongSell")]
            else:
                d = await f.nasdaq(sym)
                n = d["buy"] + d["hold"] + d["sell"]
                # Nasdaq fasst „stark kaufen“ und „kaufen“ zusammen -> Mitte der beiden Stufen
                sc = round((8.75 * d["buy"] + 5 * d["hold"] + 1.25 * d["sell"]) / n, 2)
                counts = [d["buy"], d["hold"], d["sell"]]
            if sc is None:
                src[k] = {"status": "nodata"}
            else:
                src[k] = {"status": "ok", "score": sc, "n": n, "period": d.get("period"), "counts": counts,
                          "target": d.get("target")}
            if d.get("target") and 0.2 < d["target"] / price < 5:
                pots.append(d["target"] / price - 1)
        except SourceError as e:
            src[k] = {"status": e.status, "msg": e.msg}

    for k in ("finnhub", "nasdaq"):
        await analyst(k)
        log_src(k)

    # 4 MarketScreener – alle Aktien
    if off("marketscreener"):
        src["marketscreener"] = {"status": "off"}
    else:
        ms_extra, ms_lost = "", False
        try:
            cons = None
            if ms_url:
                try:
                    cons = await f.ms_consensus(ms_url)
                except SourceError as e:
                    if e.msg != "Seite nicht gefunden":
                        raise
                    ms_url, ms_lost = None, True        # Seite verschwunden: neu suchen
            if not ms_url:
                ms_url, cons = await ms_resolve(f, share, name, price, chart["currency"], ctx.get("fx"))
                ms_extra = (" · gespeicherte Seite nicht mehr da, neu gefunden: " if ms_lost else " · Seite gefunden: ") \
                    + ms_url.rstrip("/").rsplit("/", 1)[-1]
            src["marketscreener"] = {"status": "ok", "score": round(clamp((5 - cons["mean"]) / 4 * 10), 2),
                                     "label": cons["label"], "n": cons["n"], "url": ms_url}
            if cons.get("pot") is not None and -0.8 < cons["pot"] < 4:
                pots.append(cons["pot"])
                src["marketscreener"]["pot"] = cons["pot"]
        except SourceError as e:
            src["marketscreener"] = {"status": e.status, "msg": e.msg, "url": ms_url}
        log_src("marketscreener", ms_extra, warn=ms_lost)

    # 5 Kursziel-Potenzial (Mittel aller Quellen)
    target = None
    if pots:
        pot = sum(pots) / len(pots)
        target = price * (1 + pot)
    if off("target"):
        src["target"] = {"status": "off"}
    elif pots:
        src["target"] = {"status": "ok", "score": round(lin(pot, 0.30), 2), "target": target, "price": price,
                         "pot": pot, "n": len(pots)}
    else:
        src["target"] = {"status": "nodata"}

    # 6 eigene Technik
    if off("technical"):
        src["technical"] = {"status": "off"}
    else:
        sc, parts = technical(chart["closes"])
        src["technical"] = {"status": "ok", "score": sc, **parts} if sc is not None else {"status": "nodata"}

    # Index = gewichteter Mittelwert der Quellen mit Daten
    num = den = 0.0
    used = 0
    for k in SOURCE_KEYS:
        s = src.get(k) or {}
        if s.get("status") == "ok" and s.get("score") is not None:
            wk = float(w.get(k) or 0)
            num += s["score"] * wk
            den += wk
            used += 1
    score = round(num / den, 1) if den and used >= int(settings.get("min_sources") or 1) else None
    prev = share.get("score")
    if score is None:
        log("warn", "Index", f"Zu wenig Daten: {used} von {len(SOURCE_KEYS)} Quellen (mindestens {settings.get('min_sources')})")
    else:
        log("ok", "Index", f"Index {de(score, 1)} aus {used} von {len(SOURCE_KEYS)} Quellen"
            + (f" (vorher {de(prev, 1)})" if prev is not None else ""))
    analysts = max([s.get("n") or 0 for k, s in src.items() if k in ("yahoo", "finnhub", "nasdaq", "marketscreener")] + [0])

    return {"price": price, "currency": chart["currency"], "change_pct": chart["change_pct"],
            "name": name, "exchange": share.get("exchange") or chart["exchange"],
            "target": target, "score": score, "nsources": used, "analysts": analysts, "sources": src,
            "ms_url": ms_url}
