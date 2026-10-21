"""SQLite: yayınlanan öneriler, kuponlar, ayarlar ve karne."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import DATA_DIR

DB = DATA_DIR / "iddaa_bot.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS picks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT, espn_id TEXT, league TEXT, kickoff TEXT, home TEXT, away TEXT,
  iddaa_id INTEGER, mbs INTEGER, market TEXT, outcome TEXT, spec TEXT, kind TEXT,
  odds REAL, prob REAL, ev REAL, conf TEXT, stake REAL, last_odds REAL,
  status TEXT DEFAULT 'pending', score TEXT, profit REAL, flat_profit REAL,
  played INTEGER DEFAULT 0, played_stake REAL, settled_at TEXT,
  is_single INTEGER DEFAULT 0, prematch_sent INTEGER DEFAULT 0,
  UNIQUE(espn_id, market, outcome)
);
CREATE TABLE IF NOT EXISTS coupons (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT, kind TEXT, pick_ids TEXT, odds REAL, prob REAL, ev REAL, stake REAL,
  status TEXT DEFAULT 'pending', profit REAL, played INTEGER DEFAULT 0, played_stake REAL
);
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
"""


@contextmanager
def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init() -> None:
    with conn() as c:
        c.executescript(SCHEMA)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(picks)")}
        if "p_close" not in cols:  # eski kurulumlar için
            c.execute("ALTER TABLE picks ADD COLUMN p_close REAL")


def match_has_single(espn_id: str) -> bool:
    with conn() as db:
        return db.execute("SELECT 1 FROM picks WHERE espn_id=? AND is_single=1", (espn_id,)).fetchone() is not None


def update_close(espn_id: str, market: str, outcome: str, p: float, odds: float) -> None:
    """Maç başlamadan hemen önceki piyasa olasılığı ve iddaa oranı (kapanış değeri ölçümü için)."""
    with conn() as db:
        db.execute("UPDATE picks SET p_close=?, last_odds=? WHERE espn_id=? AND market=? AND outcome=? "
                   "AND status='pending'", (p, odds, espn_id, market, outcome))


def get_setting(k: str, default=None):
    with conn() as c:
        r = c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return json.loads(r["v"]) if r else default


def set_setting(k: str, v) -> None:
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO settings(k, v) VALUES(?, ?)", (k, json.dumps(v)))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save_pick(a, c, single: bool = True) -> int | None:
    """a: MatchAnalysis, c: Candidate. Aynı seçim tekrar kaydedilmez (ilk önerilen oran geçerli).
    single=False: sadece kupon ayağı (tekli karnesine girmez)."""
    with conn() as db:
        cur = db.execute(
            """INSERT OR IGNORE INTO picks(created_at, espn_id, league, kickoff, home, away, iddaa_id, mbs,
               market, outcome, spec, kind, odds, prob, ev, conf, stake, last_odds)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now_iso(), a.fx.espn_id, a.fx.league, a.fx.kickoff.isoformat(), a.fx.home, a.fx.away,
             a.ev.id if a.ev else None, getattr(c, "mbs", None), c.market, c.outcome,
             json.dumps(c.spec, ensure_ascii=False), c.kind, c.odds, c.p, c.ev, c.conf, c.stake, c.odds))
        pid = cur.lastrowid if cur.rowcount else None
        if pid is None:
            r = db.execute("SELECT id FROM picks WHERE espn_id=? AND market=? AND outcome=?",
                           (a.fx.espn_id, c.market, c.outcome)).fetchone()
            pid = r["id"] if r else None
        if pid and single:
            db.execute("UPDATE picks SET is_single=1 WHERE id=?", (pid,))
        return pid


def save_coupon(kind: str, pick_ids: list[int], odds: float, prob: float, ev: float, stake: float) -> int:
    with conn() as db:
        key = json.dumps(sorted(pick_ids))
        r = db.execute("SELECT id FROM coupons WHERE pick_ids=? AND kind=?", (key, kind)).fetchone()
        if r:
            return r["id"]
        return db.execute(
            "INSERT INTO coupons(created_at, kind, pick_ids, odds, prob, ev, stake) VALUES(?,?,?,?,?,?,?)",
            (now_iso(), kind, key, odds, prob, ev, stake)).lastrowid


def pending_picks() -> list[sqlite3.Row]:
    with conn() as db:
        return db.execute("SELECT * FROM picks WHERE status='pending' ORDER BY kickoff").fetchall()


def get_pick(pid: int):
    with conn() as db:
        return db.execute("SELECT * FROM picks WHERE id=?", (pid,)).fetchone()


def update_last_odds(pid: int, odds: float) -> None:
    with conn() as db:
        db.execute("UPDATE picks SET last_odds=? WHERE id=?", (odds, pid))


def settle_pick(pid: int, status: str, score: str = "") -> None:
    with conn() as db:
        r = db.execute("SELECT * FROM picks WHERE id=?", (pid,)).fetchone()
        if not r:
            return
        if status == "won":
            profit, flat = r["stake"] * (r["odds"] - 1), r["odds"] - 1
        elif status == "lost":
            profit, flat = -r["stake"], -1.0
        else:
            profit, flat = 0.0, 0.0
        db.execute("UPDATE picks SET status=?, score=?, profit=?, flat_profit=?, settled_at=? WHERE id=?",
                   (status, score, profit, flat, now_iso(), pid))
        _settle_coupons(db)


def _settle_coupons(db) -> None:
    for cp in db.execute("SELECT * FROM coupons WHERE status='pending'").fetchall():
        ids = json.loads(cp["pick_ids"])
        rows = [db.execute("SELECT status, odds FROM picks WHERE id=?", (i,)).fetchone() for i in ids]
        st = [r["status"] for r in rows if r]
        if "lost" in st:
            db.execute("UPDATE coupons SET status='lost', profit=? WHERE id=?", (-cp["stake"], cp["id"]))
        elif st and all(s in ("won", "void") for s in st):
            odds = 1.0
            for r in rows:
                if r["status"] == "won":
                    odds *= r["odds"]
            db.execute("UPDATE coupons SET status='won', profit=? WHERE id=?",
                       (cp["stake"] * (odds - 1), cp["id"]))


def mark_played(pid: int, stake: float, table: str = "picks") -> bool:
    assert table in ("picks", "coupons")
    with conn() as db:
        return db.execute(f"UPDATE {table} SET played=1, played_stake=? WHERE id=?", (stake, pid)).rowcount > 0


def all_settled_picks() -> list[sqlite3.Row]:
    with conn() as db:
        return db.execute("SELECT * FROM picks WHERE is_single=1 AND status IN ('won','lost','void')").fetchall()


def mark_prematch(pid: int) -> None:
    with conn() as db:
        db.execute("UPDATE picks SET prematch_sent=1 WHERE id=?", (pid,))


def all_coupons() -> list[sqlite3.Row]:
    with conn() as db:
        return db.execute("SELECT * FROM coupons WHERE status IN ('won','lost')").fetchall()


def coupon_legs(cp) -> list[sqlite3.Row]:
    with conn() as db:
        rows = [db.execute("SELECT * FROM picks WHERE id=?", (i,)).fetchone() for i in json.loads(cp["pick_ids"])]
    return sorted((r for r in rows if r), key=lambda r: r["kickoff"])


def recent_coupons(since_iso: str) -> list[sqlite3.Row]:
    """since_iso tarihinden sonra oluşturulmuş tüm kuponlar (bekleyenler dahil)."""
    with conn() as db:
        return db.execute("SELECT * FROM coupons WHERE created_at>=? ORDER BY id", (since_iso,)).fetchall()


def pending_coupon_ids() -> set[int]:
    with conn() as db:
        return {r["id"] for r in db.execute("SELECT id FROM coupons WHERE status='pending'").fetchall()}


def get_coupons(ids) -> list[sqlite3.Row]:
    ids = list(ids)
    if not ids:
        return []
    with conn() as db:
        return db.execute(f"SELECT * FROM coupons WHERE id IN ({','.join('?' * len(ids))}) ORDER BY id",
                          ids).fetchall()
