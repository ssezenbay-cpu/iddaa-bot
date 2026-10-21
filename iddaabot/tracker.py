"""Sonuç takibi, oran hareketi (CLV) ve karne."""
from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import espn, iddaa, storage
from .settle import evaluate

log = logging.getLogger(__name__)
VOID_STATUS = ("STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_ABANDONED", "STATUS_SUSPENDED")


async def settle_due() -> list[dict]:
    """Bitmiş maçların seçimlerini sonuçlandırır. Bildirim için sonuçlanan satırları döndürür."""
    out = []
    now = datetime.now(timezone.utc)
    cache: dict[tuple, list] = {}
    for r in storage.pending_picks():
        ko = datetime.fromisoformat(r["kickoff"])
        if now < ko + timedelta(hours=2, minutes=10):
            continue
        fx = None
        for d in (ko.date(), (ko - timedelta(days=1)).date(), (ko + timedelta(days=1)).date()):
            key = (r["league"], d)
            if key not in cache:
                cache[key] = await espn.scoreboard(r["league"], d, ttl=900)
            fx = next((f for f in cache[key] if f.espn_id == r["espn_id"]), None)
            if fx:
                break
        if fx is None:
            if now > ko + timedelta(days=3):
                storage.settle_pick(r["id"], "void", "maç bulunamadı")
            continue
        if fx.status_name in VOID_STATUS:
            storage.settle_pick(r["id"], "void", fx.status_name)
            out.append({**dict(r), "status": "void", "score": "ertelendi/iptal"})
            continue
        if not fx.completed or fx.home_score is None:
            continue
        spec = json.loads(r["spec"])
        corners = cards = None
        if spec.get("subject") in ("corners", "cards"):
            sm = await espn.summary(r["league"], r["espn_id"], ttl=3600)
            corners, cards = sm.corners(), sm.cards()
        ht = (fx.ht_home, fx.ht_away) if fx.ht_home is not None else None
        res = evaluate(spec, (fx.home_score, fx.away_score), ht, corners, cards)
        score = f"{fx.home_score}-{fx.away_score}" + (f" (İY {ht[0]}-{ht[1]})" if ht else "")
        if res is None:
            if now > ko + timedelta(hours=6):
                storage.settle_pick(r["id"], "manual", score)
                out.append({**dict(r), "status": "manual", "score": score})
            continue
        st = "won" if res else "lost"
        storage.settle_pick(r["id"], st, score)
        out.append({**dict(r), "status": st, "score": score})
    return out


async def refresh_odds() -> None:
    """Başlamamış önerilerin güncel iddaa oranını kaydeder (kapanış oranı yaklaşımı)."""
    pend = [r for r in storage.pending_picks()
            if datetime.fromisoformat(r["kickoff"]) > datetime.now(timezone.utc)]
    if not pend:
        return
    try:
        events = {e.id: e for e in await iddaa.fetch_events()}
    except Exception as e:  # noqa: BLE001
        log.warning("Oran güncelleme başarısız: %s", e)
        return
    for r in pend:
        ev = events.get(r["iddaa_id"])
        if not ev:
            continue
        for m in ev.markets:
            if m.name == r["market"]:
                for o in m.outcomes:
                    if o.name == r["outcome"]:
                        storage.update_last_odds(r["id"], o.odd)


def _stats(rows, key_profit="flat_profit", key_stake=None):
    n = len(rows)
    if n == 0:
        return None
    won = sum(1 for r in rows if r["status"] == "won")
    lost = sum(1 for r in rows if r["status"] == "lost")
    rets = [r[key_profit] or 0.0 for r in rows if r["status"] in ("won", "lost")]
    staked = (sum(r[key_stake] or 0 for r in rows if r["status"] in ("won", "lost"))
              if key_stake else len(rets))
    prof = sum(rets)
    roi = prof / staked if staked else 0.0
    t = None
    if len(rets) >= 10:
        mu = sum(rets) / len(rets)
        sd = math.sqrt(sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)) or 1e-9
        t = mu / (sd / math.sqrt(len(rets)))
    exp_hits = sum(r["prob"] for r in rows if r["status"] in ("won", "lost"))
    return {"n": n, "won": won, "lost": lost, "void": n - won - lost, "profit": prof,
            "staked": staked, "roi": roi, "t": t, "exp_hits": exp_hits}


def report_card() -> dict:
    rows = storage.all_settled_picks()
    res = {"all": _stats(rows), "tl": _stats(rows, "profit", "stake")}
    by_conf, by_kind = defaultdict(list), defaultdict(list)
    for r in rows:
        by_conf[r["conf"]].append(r)
        by_kind[r["kind"] or "?"].append(r)
    res["conf"] = {k: _stats(v) for k, v in by_conf.items()}
    res["kind"] = {k: _stats(v) for k, v in by_kind.items()}
    clv = [r["odds"] / r["last_odds"] - 1 for r in rows if r["last_odds"] and r["status"] != "void"]
    res["clv"] = (sum(clv) / len(clv), len(clv)) if clv else None
    cev = [r["p_close"] * r["odds"] - 1 for r in rows if r["p_close"] and r["status"] != "void"]
    res["close_ev"] = (sum(cev) / len(cev), len(cev), sum(1 for x in cev if x > 0) / len(cev)) if cev else None
    played = [r for r in rows if r["played"]]
    if played:
        st = sum(r["played_stake"] or 0 for r in played if r["status"] in ("won", "lost"))
        pr = sum(((r["odds"] - 1) * (r["played_stake"] or 0)) if r["status"] == "won"
                 else (-(r["played_stake"] or 0) if r["status"] == "lost" else 0) for r in played)
        res["played"] = {"n": len(played), "staked": st, "profit": pr, "roi": pr / st if st else 0}
    cps = storage.all_coupons()
    if cps:
        res["coupons"] = {}
        for kind in sorted({c["kind"] for c in cps}):
            ks = [c for c in cps if c["kind"] == kind]
            res["coupons"][kind] = {"n": len(ks), "won": sum(1 for c in ks if c["status"] == "won"),
                                    "profit": sum(c["profit"] or 0 for c in ks),
                                    "staked": sum(c["stake"] or 0 for c in ks)}
    return res
