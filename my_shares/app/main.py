"""My Shares – Home-Assistant-App: Aktienliste mit Empfehlungsindex 0–10."""
import asyncio
import logging
import os
import re
from datetime import datetime, timedelta

from aiohttp import web

from db import CURRENCIES, DB, DEFAULTS, LISTS, now_iso
from fx import convert
from sources import SOURCE_KEYS, AVGate, Fetcher, SourceError, evaluate, ms_normalize

VERSION = "1.2.0"
DATA_DIR = os.environ.get("DATA_DIR", "/data")
PORT = int(os.environ.get("PORT", "8099"))
WWW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "www")
INGRESS_IPS = {"172.30.32.2", "127.0.0.1", "::1"}
ALLOW_ALL = os.environ.get("ALLOW_ALL") == "1"
PAUSE = float(os.environ.get("FETCH_PAUSE", "1.5"))   # Sekunden zwischen zwei Aktien

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("my_shares")


class Engine:
    def __init__(self, db):
        self.db = db
        self.running = False
        self.progress = {"done": 0, "total": 0, "current": ""}
        self.queue = set()
        self.next_run = None
        self.wake = asyncio.Event()
        self.fx = db.meta("fx")
        self.fx_try = None
        self.av = AVGate(db)

    # ---------- Wechselkurse ----------
    async def update_fx(self, force=False):
        """EZB-Kurse holen: höchstens alle 6 h, nach Fehler frühestens nach 30 min erneut."""
        now = datetime.now()
        if not force and self.fx and self.fx.get("fetched"):
            if now - datetime.fromisoformat(self.fx["fetched"]) < timedelta(hours=6):
                return
        if not force and self.fx_try and now - self.fx_try < timedelta(minutes=30):
            return
        self.fx_try = now
        f = Fetcher()
        try:
            fx = await f.ecb()
            fx["fetched"] = now_iso()
            self.fx = fx
            self.db.set_meta("fx", fx)
            self.fx_try = None
            log.info("EZB-Kurse vom %s geladen (1 EUR = %.4f USD)", fx["date"], fx["rates"]["USD"])
        except SourceError as e:
            log.warning("Wechselkurse: %s", e.msg)
        finally:
            await f.close()

    # ---------- Zeitplan ----------
    def compute_next(self):
        s = self.db.settings()
        if not s.get("schedule_enabled") or not s.get("schedule_days"):
            self.next_run = None
            return
        try:
            hh, mm = (int(x) for x in str(s["schedule_time"]).split(":")[:2])
        except ValueError:
            hh, mm = 7, 30
        now = datetime.now()
        for add in range(0, 8):
            d = (now + timedelta(days=add)).replace(hour=hh, minute=mm, second=0, microsecond=0)
            if d > now and d.weekday() in s["schedule_days"]:
                self.next_run = d
                return
        self.next_run = None

    async def scheduler(self):
        self.compute_next()
        await self.update_fx()
        while True:
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=20)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()
            await self.update_fx()
            if self.next_run and datetime.now() >= self.next_run and not self.running:
                log.info("Geplanter Lauf startet")
                self.compute_next()
                await self.run()
            if self.queue and not self.running:
                ids, self.queue = list(self.queue), set()
                await self.run(ids)

    def request(self, ids=None):
        if ids:
            self.queue.update(ids)
        else:
            self.queue.add(None)
        self.wake.set()

    # ---------- Berechnung ----------
    async def run(self, ids=None):
        if None in (ids or []):
            ids = None
        shares = self.db.shares()
        if ids:
            shares = [s for s in shares if s["id"] in ids]
        if not shares:
            return
        self.running = True
        self.progress = {"done": 0, "total": len(shares), "current": ""}
        settings = self.db.settings()
        await self.update_fx()
        f = Fetcher()
        errors = 0
        try:
            for i, sh in enumerate(shares):
                self.progress["current"] = sh.get("name") or sh["symbol"]
                try:
                    r = await evaluate(f, sh, settings, {"fx": self.fx, "av": self.av})
                    prev = sh["score"] if sh["score"] is not None else sh["score_prev"]
                    self.db.update_share(sh["id"], **r, score_prev=prev, updated_at=now_iso(), error=None)
                    self.db.add_history(sh["id"], r["score"], r["price"])
                except SourceError as e:
                    errors += 1
                    self.db.update_share(sh["id"], error=e.msg or e.status, updated_at=now_iso())
                    log.warning("%s: %s", sh["symbol"], e.msg)
                except Exception as e:  # noqa: BLE001
                    errors += 1
                    log.exception("%s: unerwarteter Fehler", sh["symbol"])
                    self.db.update_share(sh["id"], error=f"interner Fehler: {type(e).__name__}", updated_at=now_iso())
                self.progress["done"] = i + 1
                if i < len(shares) - 1:
                    await asyncio.sleep(PAUSE)
        finally:
            await f.close()
            self.running = False
            self.progress["current"] = ""
            self.db.set_meta("last_run", {"ts": now_iso(), "count": len(shares), "errors": errors, "all": not ids})
            log.info("Lauf fertig: %d Aktien, %d Fehler", len(shares), errors)


# ---------- Hilfsfunktionen ----------
def label(score, s):
    if score is None:
        return "none"
    if score >= float(s["buy_threshold"]):
        return "buy"
    if score <= float(s["sell_threshold"]):
        return "sell"
    return "hold"


def public_settings(s):
    out = {k: v for k, v in s.items() if not k.endswith("_key")}
    out["finnhub_key_set"] = bool(s.get("finnhub_key"))
    out["av_key_set"] = bool(s.get("av_key"))
    return out


def display(x, cur, fx):
    """Geldwerte einer Aktie in der Anzeigewährung (None, wenn kein Kurs bekannt)."""
    c = x.get("currency")
    price = convert(x.get("price"), c, cur, fx)
    d = {"price": price, "target": convert(x.get("target"), c, cur, fx), "fx_ok": price is not None or not x.get("price")}
    d["buy"] = convert(x.get("buy_price"), x.get("buy_currency") or cur, cur, fx)
    d["wish"] = convert(x.get("wish_price"), x.get("wish_currency") or cur, cur, fx)
    q = x.get("quantity") or 0
    d["value"] = q * price if q and price is not None else None
    d["pl"] = d["pl_pct"] = None
    if d["value"] is not None and d["buy"]:
        cost = q * d["buy"]
        d["pl"], d["pl_pct"] = d["value"] - cost, d["value"] / cost - 1
    d["wish_gap"] = price / d["wish"] - 1 if price and d["wish"] else None
    return d


def stats(shares, s):
    out = {}
    for lst in LISTS:
        items = [x for x in shares if x.get("list") == lst]
        scored = [x for x in items if x["score"] is not None]
        counts = {"buy": 0, "hold": 0, "sell": 0, "none": 0}
        for x in items:
            counts[label(x["score"], s)] += 1
        value = cost = covered = wnum = wsum = 0.0
        missing = 0
        for x in items:
            d = x["disp"]
            if lst != "depot" or not x.get("quantity") or not x.get("price"):
                continue
            if d["value"] is None:
                missing += 1
                continue
            value += d["value"]
            if x["score"] is not None:
                wsum += d["value"]
                wnum += d["value"] * x["score"]
            if d["pl"] is not None:
                cost += d["value"] - d["pl"]
                covered += d["value"]
        if wsum:
            avg, weighted = wnum / wsum, True
        else:
            avg = sum(x["score"] for x in scored) / len(scored) if scored else None
            weighted = False
        last = max([x["updated_at"] for x in items if x.get("updated_at")] or [None])
        out[lst] = {
            "count": len(items), "avg": round(avg, 1) if avg is not None else None, "weighted": weighted,
            "counts": counts, "value": value if value else None, "fx_missing": missing,
            "pl": covered - cost if cost else None, "pl_pct": covered / cost - 1 if cost else None,
            "wish_hits": sum(1 for x in items if x["disp"]["wish_gap"] is not None and x["disp"]["wish_gap"] <= 0),
            "last_update": last, "errors": sum(1 for x in items if x.get("error"))}
    return out


def json_error(msg, status=400):
    return web.json_response({"error": msg}, status=status)


# ---------- Web ----------
@web.middleware
async def guard(request, handler):
    if not ALLOW_ALL and request.remote not in INGRESS_IPS:
        return web.Response(status=403, text="Nur über Home Assistant erreichbar")
    if request.method not in ("GET", "HEAD") and request.headers.get("X-App") != "1":
        return json_error("Header X-App fehlt", 403)
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("Fehler in %s", request.path)
        return json_error(f"Interner Fehler: {type(e).__name__}", 500)


def make_app(db, engine):
    routes = web.RouteTableDef()

    def static(name, ctype):
        async def h(_request):
            return web.FileResponse(os.path.join(WWW, name), headers={"Content-Type": ctype, "Cache-Control": "no-cache"})
        return h

    routes.get("/")(static("index.html", "text/html; charset=utf-8"))
    routes.get("/lang-en.js")(static("lang-en.js", "application/javascript; charset=utf-8"))
    routes.get("/favicon.svg")(static("favicon.svg", "image/svg+xml"))

    @routes.get("/favicon.ico")
    async def favicon_ico(_r):
        return web.Response(status=204)

    @routes.get("/api/state")
    async def state(request):
        s = db.settings()
        shares = db.shares()
        for x in shares:
            x["label"] = label(x["score"], s)
            x["disp"] = display(x, s["currency"], engine.fx)
        user = request.headers.get("X-Remote-User-Display-Name") or request.headers.get("X-Remote-User-Name") or ""
        return web.json_response({
            "version": VERSION, "user": user, "running": engine.running, "progress": engine.progress,
            "queued": bool(engine.queue), "next_run": engine.next_run.isoformat() if engine.next_run else None,
            "last_run": db.meta("last_run"), "settings": public_settings(s), "stats": stats(shares, s),
            "fx": {"date": (engine.fx or {}).get("date"), "usd": ((engine.fx or {}).get("rates") or {}).get("USD")},
            "currency": s["currency"], "shares": shares, "now": now_iso()})

    @routes.get("/api/shares/{id}")
    async def share_detail(request):
        sh = db.share(int(request.match_info["id"]))
        if not sh:
            return json_error("Aktie nicht gefunden", 404)
        st = db.settings()
        sh["label"] = label(sh["score"], st)
        sh["disp"] = display(sh, st["currency"], engine.fx)
        sh["history"] = db.history(sh["id"])
        return web.json_response(sh)

    @routes.post("/api/lookup")
    async def lookup(request):
        body = await request.json()
        q = str(body.get("query") or "").strip()
        if len(q) < 2:
            return json_error("Bitte WKN, ISIN oder Symbol eingeben")
        f = Fetcher()
        try:
            res = await f.lookup(q)
        except SourceError as e:
            return json_error(e.msg, 502)
        finally:
            await f.close()
        for r in res:
            row = db.find_symbol_row(r["symbol"])
            r["exists"] = row["list"] if row else None
        return web.json_response({"results": res})

    def parse_num(v, what):
        if v in (None, ""):
            return None
        txt = str(v).strip().replace(" ", "")
        if "," in txt:                      # deutsche Schreibweise 1.234,56
            txt = txt.replace(".", "").replace(",", ".")
        try:
            q = float(txt)
        except ValueError:
            raise ValueError(f"{what} ist keine Zahl") from None
        if q < 0:
            raise ValueError(f"{what} darf nicht negativ sein")
        return q

    def parse_qty(v):
        return parse_num(v, "Stückzahl")

    def money_fields(b, cur):
        """Kaufkurs/Wunschkurs aus dem Body, gespeichert in der aktuellen Anzeigewährung."""
        out = {}
        if "buy_price" in b:
            out["buy_price"] = parse_num(b["buy_price"], "Kaufkurs")
            out["buy_currency"] = cur if out["buy_price"] is not None else None
        if "wish_price" in b:
            out["wish_price"] = parse_num(b["wish_price"], "Wunschkurs")
            out["wish_currency"] = cur if out["wish_price"] is not None else None
        return out

    @routes.post("/api/shares")
    async def add_share(request):
        b = await request.json()
        symbol = str(b.get("symbol") or "").strip().upper()
        if not re.match(r"^[A-Z0-9.\-=^]{1,20}$", symbol):
            return json_error("Ungültiges Symbol")
        row = db.find_symbol_row(symbol)
        if row:
            where = "im Depot" if row["list"] == "depot" else "unter Beobachten"
            return json_error(f"{symbol} ist schon {where}")
        lst = b.get("list") or "depot"
        if lst not in LISTS:
            return json_error("Unbekannte Liste")
        cur = db.settings()["currency"]
        try:
            extra = money_fields(b, cur)
            if lst == "depot":
                extra["quantity"] = parse_qty(b.get("quantity"))
                extra.pop("wish_price", None), extra.pop("wish_currency", None)
            else:
                extra.pop("buy_price", None), extra.pop("buy_currency", None)
        except ValueError as e:
            return json_error(str(e))
        sid = db.add_share(str(b.get("ident") or symbol).strip().upper()[:20], symbol,
                           str(b.get("name") or "")[:120] or None, str(b.get("exchange") or "")[:40] or None,
                           lst, **extra)
        engine.request([sid])
        return web.json_response({"id": sid})

    @routes.patch("/api/shares/{id}")
    async def edit_share(request):
        sid = int(request.match_info["id"])
        sh = db.share(sid)
        if not sh:
            return json_error("Aktie nicht gefunden", 404)
        b = await request.json()
        fields = {}
        try:
            if "quantity" in b:
                fields["quantity"] = parse_qty(b.get("quantity"))
            money = money_fields(b, db.settings()["currency"])
            # Kaufkurs nur neu speichern, wenn er sich geändert hat (sonst bleibt die ursprüngliche Währung)
            for k in ("buy", "wish"):
                if f"{k}_price" in money:
                    old = display(sh, db.settings()["currency"], engine.fx)[k]
                    new = money[f"{k}_price"]
                    if old is None or new is None or abs(old - new) > 0.005:
                        fields[f"{k}_price"], fields[f"{k}_currency"] = new, money[f"{k}_currency"]
        except ValueError as e:
            return json_error(str(e))
        recalc = False
        if "symbol" in b:
            sym = str(b["symbol"] or "").strip().upper()
            if not re.match(r"^[A-Z0-9.\-=^]{1,20}$", sym):
                return json_error("Ungültiges Symbol")
            other = db.find_symbol(sym)
            if other and other != sid:
                return json_error(f"{sym} ist schon in einer Liste")
            if sym != sh["symbol"]:
                fields.update(symbol=sym, exchange=None, ms_url=None)
                recalc = True
        if "ms_url" in b:
            raw_url = str(b["ms_url"] or "").strip()
            url = ms_normalize(raw_url) if raw_url else None
            if raw_url and not (url and "marketscreener.com" in raw_url):
                return json_error("Bitte eine Adresse von marketscreener.com angeben (Seite der Aktie)")
            if url != sh.get("ms_url"):
                fields["ms_url"] = url
                recalc = True
        db.update_share(sid, **fields)
        if recalc:
            engine.request([sid])
        return web.json_response({"ok": True})

    @routes.post("/api/shares/{id}/move")
    async def move_share(request):
        sid = int(request.match_info["id"])
        sh = db.share(sid)
        if not sh:
            return json_error("Aktie nicht gefunden", 404)
        b = await request.json()
        to = b.get("list")
        if to not in LISTS:
            return json_error("Unbekannte Liste")
        if to == sh["list"]:
            return json_error("Die Aktie ist schon in dieser Liste")
        if to == "depot":
            try:
                qty = parse_qty(b.get("quantity"))
                money = money_fields({"buy_price": b.get("buy_price")}, db.settings()["currency"])
            except ValueError as e:
                return json_error(str(e))
            if not qty:
                return json_error("Bitte die Stückzahl angeben")
            db.update_share(sid, list="depot", quantity=qty, wish_price=None, wish_currency=None, **money)
        else:
            db.update_share(sid, list="watch", quantity=None, buy_price=None, buy_currency=None)
        return web.json_response({"ok": True})

    @routes.post("/api/shares/delete")
    async def delete_shares(request):
        b = await request.json()
        ids = [int(x) for x in b.get("ids") or []]
        db.delete_shares(ids)
        return web.json_response({"deleted": len(ids)})

    @routes.post("/api/run")
    async def run(request):
        b = await request.json() if request.can_read_body else {}
        ids = [int(x) for x in (b.get("ids") or [])]
        if not db.shares():
            return json_error("Die Liste ist leer")
        engine.request(ids or None)
        return web.json_response({"ok": True})

    @routes.get("/api/settings")
    async def get_settings(_r):
        return web.json_response(public_settings(db.settings()))

    @routes.put("/api/settings")
    async def put_settings(request):
        b = await request.json()
        cur = db.settings()
        new = {}
        if "schedule_enabled" in b:
            new["schedule_enabled"] = bool(b["schedule_enabled"])
        if "schedule_time" in b:
            if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", str(b["schedule_time"])):
                return json_error("Uhrzeit bitte als hh:mm angeben")
            new["schedule_time"] = b["schedule_time"]
        if "schedule_days" in b:
            days = sorted({int(d) for d in b["schedule_days"] if 0 <= int(d) <= 6})
            new["schedule_days"] = days
        for k in ("finnhub_key", "av_key"):
            if b.get(k + "_clear"):
                new[k] = ""
            elif b.get(k):
                v = str(b[k]).strip()
                if not re.match(r"^[A-Za-z0-9_\-]{8,80}$", v):
                    return json_error("Der API-Key enthält ungültige Zeichen")
                new[k] = v
        if "weights" in b:
            w = dict(cur["weights"])
            for k in SOURCE_KEYS:
                if k in b["weights"]:
                    try:
                        v = float(b["weights"][k])
                    except (TypeError, ValueError):
                        return json_error("Gewichte müssen Zahlen sein")
                    if not 0 <= v <= 5:
                        return json_error("Gewichte liegen zwischen 0 und 5")
                    w[k] = v
            new["weights"] = w
        if "min_sources" in b:
            m = int(b["min_sources"])
            if not 1 <= m <= 5:
                return json_error("Mindestanzahl Quellen liegt zwischen 1 und 5")
            new["min_sources"] = m
        buy = float(b.get("buy_threshold", cur["buy_threshold"]))
        sell = float(b.get("sell_threshold", cur["sell_threshold"]))
        if "buy_threshold" in b or "sell_threshold" in b:
            if not (0 <= sell < buy <= 10):
                return json_error("„Verkaufen bis“ muss kleiner sein als „Kaufen ab“ (0–10)")
            new["buy_threshold"], new["sell_threshold"] = buy, sell
        new_currency = False
        if "currency" in b:
            if b["currency"] not in CURRENCIES:
                return json_error("Unbekannte Währung")
            new_currency = b["currency"] != cur.get("currency")
            new["currency"] = b["currency"]
        if "language" in b:
            if b["language"] not in ("auto", "de", "en"):
                return json_error("Unbekannte Sprache")
            new["language"] = b["language"]
        db.save_settings(new)
        engine.compute_next()
        if new_currency and not engine.fx:
            await engine.update_fx(force=True)
        return web.json_response(public_settings(db.settings()))

    app = web.Application(middlewares=[guard], client_max_size=64 * 1024)
    app.add_routes(routes)
    return app


async def main():
    db = DB(os.path.join(DATA_DIR, "my_shares.db"))
    engine = Engine(db)
    app = make_app(db, engine)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("My Shares %s läuft auf Port %d (Daten: %s)", VERSION, PORT, DATA_DIR)
    await engine.scheduler()


if __name__ == "__main__":
    assert set(DEFAULTS["weights"]) == set(SOURCE_KEYS)
    asyncio.run(main())
