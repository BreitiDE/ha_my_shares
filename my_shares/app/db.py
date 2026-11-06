"""SQLite-Speicher für My Shares (liegt unter /data und überlebt Updates)."""
import json
import os
import sqlite3
import threading
from datetime import datetime

DEFAULTS = {
    "schedule_enabled": True,
    "schedule_time": "07:30",
    "schedule_days": [0, 1, 2, 3, 4],          # Mo–Fr
    "finnhub_key": "",
    "fmp_key": "",
    "weights": {"yahoo": 1.0, "finnhub": 1.0, "fmp": 1.0, "target": 1.0, "technical": 1.0},
    "min_sources": 2,
    "buy_threshold": 6.5,
    "sell_threshold": 3.5,
    "language": "auto",
    "currency": "EUR",
}

LISTS = ("depot", "watch")
CURRENCIES = ("EUR", "USD")

SCHEMA = """
CREATE TABLE IF NOT EXISTS shares(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ident TEXT NOT NULL,
  symbol TEXT NOT NULL,
  name TEXT, exchange TEXT, currency TEXT,
  quantity REAL,
  price REAL, change_pct REAL, target REAL,
  score REAL, score_prev REAL, nsources INTEGER DEFAULT 0, analysts INTEGER DEFAULT 0,
  sources TEXT,
  updated_at TEXT, error TEXT,
  added_at TEXT NOT NULL,
  list TEXT NOT NULL DEFAULT 'depot',
  buy_price REAL, buy_currency TEXT,
  wish_price REAL, wish_currency TEXT
);
CREATE TABLE IF NOT EXISTS history(share_id INTEGER NOT NULL, ts TEXT NOT NULL, score REAL, price REAL);
CREATE INDEX IF NOT EXISTS history_share ON history(share_id, ts);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""

SHARE_COLS = ("id", "ident", "symbol", "name", "exchange", "currency", "quantity", "price", "change_pct",
              "target", "score", "score_prev", "nsources", "analysts", "sources", "updated_at", "error", "added_at",
              "list", "buy_price", "buy_currency", "wish_price", "wish_currency")

# Spalten, die nach 1.0.0 dazugekommen sind (werden bei bestehenden Datenbanken ergänzt)
MIGRATIONS = [
    ("list", "TEXT NOT NULL DEFAULT 'depot'"),
    ("buy_price", "REAL"), ("buy_currency", "TEXT"),
    ("wish_price", "REAL"), ("wish_currency", "TEXT"),
]


def now_iso():
    return datetime.now().replace(microsecond=0).isoformat()


class DB:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.con.executescript(SCHEMA)
            have = {r["name"] for r in self.con.execute("PRAGMA table_info(shares)")}
            for col, decl in MIGRATIONS:
                if col not in have:
                    self.con.execute(f"ALTER TABLE shares ADD COLUMN {col} {decl}")
            self.con.commit()

    # ---------- Einstellungen ----------
    def settings(self):
        s = json.loads(json.dumps(DEFAULTS))
        with self.lock:
            for r in self.con.execute("SELECT k, v FROM settings"):
                try:
                    s[r["k"]] = json.loads(r["v"])
                except ValueError:
                    pass
        w = dict(DEFAULTS["weights"])
        w.update(s.get("weights") or {})
        s["weights"] = w
        return s

    def save_settings(self, values):
        with self.lock:
            for k, v in values.items():
                if k in DEFAULTS:
                    self.con.execute("INSERT OR REPLACE INTO settings(k, v) VALUES(?, ?)", (k, json.dumps(v)))
            self.con.commit()

    def meta(self, k, default=None):
        with self.lock:
            r = self.con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default

    def set_meta(self, k, v):
        with self.lock:
            self.con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (k, json.dumps(v)))
            self.con.commit()

    # ---------- Aktien ----------
    @staticmethod
    def _row(r):
        d = dict(r)
        d["sources"] = json.loads(d["sources"]) if d.get("sources") else {}
        return d

    def shares(self):
        with self.lock:
            rows = self.con.execute("SELECT * FROM shares ORDER BY name COLLATE NOCASE").fetchall()
        return [self._row(r) for r in rows]

    def share(self, sid):
        with self.lock:
            r = self.con.execute("SELECT * FROM shares WHERE id=?", (sid,)).fetchone()
        return self._row(r) if r else None

    def find_symbol(self, symbol):
        r = self.find_symbol_row(symbol)
        return r["id"] if r else None

    def find_symbol_row(self, symbol):
        with self.lock:
            r = self.con.execute("SELECT id, list FROM shares WHERE upper(symbol)=upper(?)", (symbol,)).fetchone()
        return dict(r) if r else None

    def add_share(self, ident, symbol, name, exchange, lst="depot", **extra):
        fields = {"ident": ident, "symbol": symbol, "name": name, "exchange": exchange, "list": lst,
                  "added_at": now_iso()}
        fields.update({k: v for k, v in extra.items() if k in SHARE_COLS})
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self.lock:
            cur = self.con.execute(f"INSERT INTO shares({cols}) VALUES({marks})", tuple(fields.values()))
            self.con.commit()
            return cur.lastrowid

    def update_share(self, sid, **fields):
        fields = {k: v for k, v in fields.items() if k in SHARE_COLS and k != "id"}
        if not fields:
            return
        if "sources" in fields and not isinstance(fields["sources"], str):
            fields["sources"] = json.dumps(fields["sources"])
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.lock:
            self.con.execute(f"UPDATE shares SET {sets} WHERE id=?", (*fields.values(), sid))
            self.con.commit()

    def delete_shares(self, ids):
        with self.lock:
            for sid in ids:
                self.con.execute("DELETE FROM shares WHERE id=?", (sid,))
                self.con.execute("DELETE FROM history WHERE share_id=?", (sid,))
            self.con.commit()

    def add_history(self, sid, score, price):
        with self.lock:
            self.con.execute("INSERT INTO history(share_id, ts, score, price) VALUES(?,?,?,?)",
                             (sid, now_iso(), score, price))
            self.con.commit()

    def history(self, sid, limit=120):
        with self.lock:
            rows = self.con.execute(
                "SELECT ts, score, price FROM history WHERE share_id=? ORDER BY ts DESC LIMIT ?", (sid, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]
