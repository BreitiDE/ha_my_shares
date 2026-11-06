"""Datenquellen und Umrechnung auf den Index 0–10.

0 = stark verkaufen · 5 = halten · 10 = stark kaufen.
Alle Abrufe laufen über curl_cffi mit Chrome-Fingerabdruck, weil Yahoo
einfache HTTP-Clients mit HTTP 429 abweist.
"""
import logging
import re

from curl_cffi.requests import AsyncSession

log = logging.getLogger("my_shares.sources")

TIMEOUT = 20
SOURCE_KEYS = ("yahoo", "finnhub", "fmp", "target", "technical")

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

    async def close(self):
        try:
            await self.s.close()
        except Exception:  # noqa: BLE001
            pass

    async def _json(self, method, url, **kw):
        try:
            r = await self.s.request(method, url, **kw)
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
                await self.s.get(warm, allow_redirects=True)
            except Exception:  # noqa: BLE001
                pass
            try:
                r = await self.s.get("https://query1.finance.yahoo.com/v1/test/getcrumb")
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

    # ---------- Financial Modeling Prep ----------
    async def fmp(self, symbol, key):
        data = await self._json("GET", "https://financialmodelingprep.com/stable/grades-consensus",
                                params={"symbol": symbol, "apikey": key})
        if isinstance(data, dict):
            msg = data.get("Error Message") or data.get("error") or ""
            raise SourceError("noaccess" if msg else "nodata", str(msg)[:80])
        if not data:
            raise SourceError("nodata")
        out = dict(data[0])
        try:
            pt = await self._json("GET", "https://financialmodelingprep.com/stable/price-target-consensus",
                                  params={"symbol": symbol, "apikey": key})
            if isinstance(pt, list) and pt:
                out["target"] = pt[0].get("targetConsensus")
        except SourceError:
            pass
        return out


async def evaluate(f, share, settings):
    """Holt alle Quellen für eine Aktie und berechnet den Index.

    Gibt ein Dict mit Feldern für die Tabelle shares zurück.
    Ohne Kurs (Yahoo-Chart) wird SourceError geworfen.
    """
    sym = share["symbol"]
    w = settings["weights"]
    src = {}

    def off(k):
        return float(w.get(k) or 0) <= 0

    chart = await f.yahoo_chart(sym)
    price = chart["price"]

    # 1 Yahoo-Analystenkonsens
    ysum = None
    if off("yahoo"):
        src["yahoo"] = {"status": "off"}
    else:
        try:
            ysum = await f.yahoo_summary(sym)
            if ysum["mean"]:
                src["yahoo"] = {"status": "ok", "score": round(clamp((5 - ysum["mean"]) / 4 * 10), 2),
                                "mean": ysum["mean"], "n": ysum["n"]}
            else:
                src["yahoo"] = {"status": "nodata"}
        except SourceError as e:
            src["yahoo"] = {"status": e.status, "msg": e.msg}

    # 2 Finnhub, 3 FMP
    fmp_target = None
    for k, fn, key in (("finnhub", f.finnhub, settings.get("finnhub_key")), ("fmp", f.fmp, settings.get("fmp_key"))):
        if off(k):
            src[k] = {"status": "off"}
            continue
        if not key:
            src[k] = {"status": "nokey"}
            continue
        try:
            d = await fn(sym, key)
            sc, n = counts_score(d)
            if sc is None:
                src[k] = {"status": "nodata"}
            else:
                src[k] = {"status": "ok", "score": sc, "n": n, "period": d.get("period"),
                          "counts": [int(d.get(x) or 0) for x in ("strongBuy", "buy", "hold", "sell", "strongSell")]}
            if k == "fmp":
                fmp_target = d.get("target")
        except SourceError as e:
            src[k] = {"status": e.status, "msg": e.msg}

    # 4 Kursziel-Potenzial (Yahoo, ggf. FMP)
    targets = []
    if ysum and ysum.get("target"):
        targets.append(ysum["target"])
    if fmp_target and 0.2 < fmp_target / price < 5:
        targets.append(fmp_target)
    target = sum(targets) / len(targets) if targets else None
    if off("target"):
        src["target"] = {"status": "off"}
    elif target:
        pot = target / price - 1
        src["target"] = {"status": "ok", "score": round(lin(pot, 0.30), 2), "target": target, "price": price,
                         "pot": pot, "n": len(targets)}
    else:
        src["target"] = {"status": "nodata"}

    # 5 eigene Technik
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
    analysts = max([s.get("n") or 0 for k, s in src.items() if k in ("yahoo", "finnhub", "fmp")] + [0])

    return {"price": price, "currency": chart["currency"], "change_pct": chart["change_pct"],
            "name": chart["name"] or share.get("name") or sym,
            "exchange": share.get("exchange") or chart["exchange"],
            "target": target, "score": score, "nsources": used, "analysts": analysts, "sources": src}
