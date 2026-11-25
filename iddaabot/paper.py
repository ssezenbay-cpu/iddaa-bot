"""Kâğıt takip: değer motorunun önereceği seçimler kaydedilir, bahis oynanmaz.

Varsayılan kapalıdır. Açılınca sabah kuponu ve /deger aynı kalır; ek olarak her değer
seçiminin oranı, olasılığı ve tutarı yazılır. Maç yaklaşınca iddaa oranı kapanış olarak
güncellenir, maç bitince sonuç işlenir. Rapor: /kagit rapor
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from . import espn, storage
from .config import settings
from .settle import evaluate

log = logging.getLogger(__name__)
VOID_STATUS = ("STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_ABANDONED", "STATUS_SUSPENDED")


def set_mode(on: bool) -> None:
    storage.set_setting("paper_mode", bool(on))
    settings.paper_mode = bool(on)


def observe(analyses) -> int:
    """Yeni değer seçimlerini kaydeder; daha önce görülenlerin kapanış oranını günceller.

    Aynı taramada yeni yazılan seçimin kapanış oranı, giriş oranıyla doldurulmaz.
    """
    if not settings.paper_mode:
        return 0
    fresh = set()
    n = 0
    for a in analyses:
        c = a.best
        if not c or not a.ev:
            continue
        if storage.save_paper(a, c):
            fresh.add((a.fx.espn_id, c.market, c.outcome))
            n += 1
    for a in analyses:
        if not a.ev:
            continue
        for c in a.candidates:
            key = (a.fx.espn_id, c.market, c.outcome)
            if key in fresh or not c.odds:
                continue
            storage.paper_update_close(*key, c.odds)
    return n


async def settle_due() -> int:
    """Bitmiş kâğıt seçimlerini ESPN skoruyla sonuçlandırır. Bildirim göndermez."""
    if not settings.paper_mode and not storage.paper_pending():
        return 0
    now = datetime.now(timezone.utc)
    cache: dict = {}
    done = 0
    for r in storage.paper_pending():
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
                storage.paper_settle(r["id"], "void", "maç bulunamadı")
                done += 1
            continue
        if fx.status_name in VOID_STATUS:
            storage.paper_settle(r["id"], "void", fx.status_name)
            done += 1
            continue
        if not fx.completed or fx.home_score is None:
            continue
        spec = json.loads(r["spec"])
        ht = (fx.ht_home, fx.ht_away) if fx.ht_home is not None else None
        res = evaluate(spec, (fx.home_score, fx.away_score), ht)
        score = f"{fx.home_score}-{fx.away_score}"
        if res is None:
            continue
        storage.paper_settle(r["id"], "won" if res else "lost", score)
        done += 1
    return done


def _clv(rows) -> tuple[float | None, float | None, int]:
    vals = []
    for r in rows:
        close = r["close_odds"]
        if not close or close <= 1 or not r["odds"]:
            continue
        vals.append(r["odds"] / close - 1)
    if not vals:
        return None, None, 0
    return sum(vals) / len(vals), sum(1 for v in vals if v > 0) / len(vals), len(vals)


def report_text() -> str:
    rows = storage.paper_all()
    if not rows:
        state = "açık" if settings.paper_mode else "kapalı"
        return (f"📄 <b>Kâğıt takip</b> ({state})\nHenüz kayıt yok. Değer motoru bir seçim görünce "
                "buraya yazılır. Bahis oynanmaz.")
    settled = [r for r in rows if r["status"] in ("won", "lost")]
    pending = [r for r in rows if r["status"] == "pending"]
    wins = sum(1 for r in settled if r["status"] == "won")
    n = len(settled)
    hit = wins / n if n else 0.0
    flat = sum(r["flat_profit"] or 0 for r in settled)
    staked = sum(r["stake"] or 0 for r in settled)
    kelly = sum(r["profit"] or 0 for r in settled)
    clv, beat, nclv = _clv(settled)
    state = "açık" if settings.paper_mode else "kapalı"
    lines = [f"📄 <b>Kâğıt takip</b> ({state}) — bahis oynanmadı"]
    lines.append(f"Kayıt {len(rows)} • biten {n} • bekleyen {len(pending)}")
    if n:
        lines.append(f"İsabet {wins}/{n} ({hit * 100:.1f}%)")
        lines.append(f"Düz bahis getiri {flat / n * 100:+.1f}%")
        if staked:
            lines.append(f"Kelly tutarla getiri {kelly / staked * 100:+.1f}% "
                         f"(toplam tutar {staked:.0f} TL, kâr {kelly:+.0f} TL)")
    if clv is not None:
        lines.append(f"Kapanışa göre: ortalama {clv * 100:+.1f}% • "
                     f"kapanışı yenen {beat * 100:.0f}% ({nclv} bahis)")
        lines.append("<i>Kapanış = aynı seçeneğin maçtan önce görülen son iddaa oranı. "
                     "Artıysa, aldığın oran kapanıştan yüksekti.</i>")
    else:
        lines.append("Kapanış oranı henüz yok (seçim bir kez görüldü).")
    return "\n".join(lines)
