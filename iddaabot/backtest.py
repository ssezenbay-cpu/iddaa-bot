"""Geçmiş maçlarda ileriye dönük (geleceği görmeyen) test.

Her hafta için:
  1) Model sadece o haftadan ÖNCEKİ maçlarla yeniden kurulur (Dixon-Coles, 2 sezon).
  2) Piyasa bileşeni o haftanın kapanış öncesi ortalama oranlarından alınır (canlıdaki
     uluslararası oran bileşeninin karşılığı).
  3) Canlıdaki aynı kurallarla değerli seçimler bulunur ve iki "bahis şirketine" karşı oynanır:
     - Bet365 kapanış öncesi oranları (MS + 2.5 Alt/Üst)
     - iddaa oranları (Maçkolik arşivi, MS) — erişilebilirse
Çıktı: bahis sayısı, isabet, ROI, anlamlılık (t), en büyük düşüş, EV dilimleri,
kapanış oranını yenme (CLV) ve olasılık doğruluğu (log-loss: model vs piyasa).
"""
from __future__ import annotations

import argparse
import asyncio
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

import numpy as np
import pandas as pd

from . import dixon_coles, footballdata, mackolik
from .footballdata import combine_stat, fit_sot
from .config import settings
from .engine import growth, kelly_fraction, tv_distance
from .markets import GoalModel, devig, fit_lambdas
from .matching import pair_score

LEAGUE_TR = {"T1": "Süper Lig", "E0": "Premier Lig", "SP1": "La Liga", "I1": "Serie A",
             "D1": "Bundesliga", "F1": "Ligue 1"}


def _col(df, *names):
    for n in names:
        if n in df.columns:
            return n
    return None


@dataclass
class Bet:
    date: object
    match: str
    book: str
    market: str
    sel: str
    odds: float
    p: float
    ev: float
    conf: str
    won: bool
    closing: float | None = None
    strategy: str = "mevcut"

    @property
    def profit(self) -> float:
        return self.odds - 1 if self.won else -1.0


@dataclass
class Result:
    league: str
    season: int
    n_matches: int = 0
    bets: list[Bet] = field(default_factory=list)
    all_value: list[Bet] = field(default_factory=list)   # eşik altı dahil, EV>0 tüm seçimler
    ll: dict = field(default_factory=lambda: defaultdict(list))
    iddaa_matched: int = 0
    notes: list[str] = field(default_factory=list)


# İstatistik modelinin harmandaki payı (log beklenen gol üzerinde) — hepsi aynı anda ölçülür
WEIGHTS = [0.1, 0.2, 0.3, 0.4, 0.6]
MODELS = {"gol": "gol modeli", "sut": "isabetli şut modeli", "karma": "gol+şut karma"}


def current_share() -> float:
    return round(settings.w_dc / (settings.w_market + settings.w_dc), 2)


def strategies() -> dict[str, tuple]:
    """strateji adı -> (istatistik modeli, payı)"""
    cur = current_share()
    st = {"mevcut": (settings.stat_model, cur)}
    if cur > 0:
        st["piyasa"] = ("gol", 0.0)
    return st


def _key_label(k) -> str:
    m, w = k
    if w == 0:
        return "sadece piyasa"
    if w == 1.0:
        return f"sadece {MODELS[m]}"
    return f"{MODELS[m]} %{w * 100:.0f}"


def _conf(agree: float) -> str:
    return "yüksek" if agree < 0.06 else ("orta" if agree < 0.12 else "düşük")


def _is_value(odds, p, ev, conf) -> bool:
    return (settings.min_odds <= odds <= settings.max_odds and p >= settings.min_prob
            and settings.min_ev[conf] <= ev <= settings.max_ev)


def _blend(lam_mkt, lam_dc, w):
    return tuple(math.exp((1 - w) * math.log(a) + w * math.log(b)) for a, b in zip(lam_mkt, lam_dc))


def _probs(lam, rho, h1):
    gm = GoalModel(lam[0], lam[1], rho, h1)
    H, A = gm.v["H"], gm.v["A"]
    return {"MS 1": gm.p(H > A), "MS X": gm.p(H == A), "MS 2": gm.p(H < A),
            "2.5 Üst": gm.p(H + A > 2.5), "2.5 Alt": gm.p(H + A < 2.5)}


def _won(sel, hg, ag) -> bool:
    return {"MS 1": hg > ag, "MS X": hg == ag, "MS 2": hg < ag,
            "2.5 Üst": hg + ag > 2.5, "2.5 Alt": hg + ag < 2.5}[sel]


def _logloss(p3, hg, ag) -> float:
    k = 0 if hg > ag else (1 if hg == ag else 2)
    return -math.log(max(p3[k], 1e-9))


def run_sync(code: str, season: int, seasons: dict[int, pd.DataFrame], mk_rows: list) -> Result:
    res = Result(league=code, season=season)
    test = seasons[season]
    hist = pd.concat([seasons[s] for s in sorted(seasons)], ignore_index=True).sort_values("Date")
    cH, cD, cA = _col(test, "AvgH", "BbAvH"), _col(test, "AvgD", "BbAvD"), _col(test, "AvgA", "BbAvA")
    cO, cU = _col(test, "Avg>2.5", "BbAv>2.5"), _col(test, "Avg<2.5", "BbAv<2.5")
    bH, bD, bA, bO, bU = (_col(test, "B365H"), _col(test, "B365D"), _col(test, "B365A"),
                          _col(test, "B365>2.5"), _col(test, "B365<2.5"))
    kH, kD, kA, kO, kU = (_col(test, "B365CH"), _col(test, "B365CD"), _col(test, "B365CA"),
                          _col(test, "B365C>2.5"), _col(test, "B365C<2.5"))
    if not (cH and cD and cA):
        res.notes.append("Bu sezon dosyasında ortalama oran sütunları yok.")
        return res
    strats = strategies()
    keys = {("gol", 0.0)} | {(m, w) for m in MODELS for w in WEIGHTS + [1.0]} | set(strats.values())
    has_sot = "HST" in test.columns and "AST" in test.columns
    weeks = test["Date"].dt.to_period("W-MON")
    for wk in sorted(weeks.unique()):
        start = wk.start_time
        games = test[weeks == wk]
        train = hist[(hist["Date"] < start) & (hist["Date"] >= start - timedelta(days=730))]
        if len(train) < 150:
            continue
        model = dixon_coles.fit(train["HomeTeam"].tolist(), train["AwayTeam"].tolist(),
                                train["FTHG"].tolist(), train["FTAG"].tolist(),
                                [d.to_pydatetime() for d in train["Date"]], start.to_pydatetime())
        sot_model, conv = (fit_sot(train, start.to_pydatetime()) if has_sot and "HST" in train.columns
                           else (None, 0.3))
        ht_ok = train[["HTHG", "HTAG"]].notna().all(axis=1) if "HTHG" in train else None
        h1 = 0.44
        if ht_ok is not None and ht_ok.sum() > 100:
            t = train[ht_ok]
            h1 = float(np.clip((t.HTHG.sum() + t.HTAG.sum()) / (t.FTHG.sum() + t.FTAG.sum()), 0.38, 0.5))
        for rd in games.to_dict("records"):
            home_t, away_t, mdate = rd["HomeTeam"], rd["AwayTeam"], rd["Date"]
            if home_t not in model.attack or away_t not in model.attack:
                continue
            avg = tuple(_f(rd.get(c)) for c in (cH, cD, cA))
            if any(x is None for x in avg):
                continue
            ou = None
            o25, u25 = _f(rd.get(cO)) if cO else None, _f(rd.get(cU)) if cU else None
            if o25 and u25:
                ou = (2.5, devig([o25, u25])[0])
            p_mkt = devig(list(avg))
            lam_mkt = fit_lambdas(tuple(p_mkt), ou, model.league_avg_goals)
            lam_g = model.lambdas(home_t, away_t)
            lam_s = None
            if sot_model and home_t in sot_model.attack and away_t in sot_model.attack:
                sh, sa = sot_model.lambdas(home_t, away_t)
                lam_s = (sh * conv, sa * conv)
            # şut verisi olmayan maçta şut/karma modeli gol modeline düşer (canlıdaki davranışla aynı)
            stat = {m: combine_stat(lam_g, lam_s, m) for m in MODELS}
            res.n_matches += 1
            hg, ag = int(rd["FTHG"]), int(rd["FTAG"])
            probs_w = {}
            for m, w in keys:
                if m not in stat:
                    continue
                lam = lam_mkt if w == 0 else (stat[m] if w == 1.0 else _blend(lam_mkt, stat[m], w))
                probs_w[(m, w)] = pr = _probs(lam, model.rho, h1)
                res.ll[(m, w)].append(_logloss((pr["MS 1"], pr["MS X"], pr["MS 2"]), hg, ag))
            name = f"{home_t} - {away_t}"

            def consider(book, odds_map, closing_map):
                for sname, key in strats.items():
                    probs = probs_w.get(key if key in probs_w else ("gol", key[1]))
                    if probs is None:
                        continue
                    lam_st = stat.get(key[0]) or lam_g
                    conf = _conf(tv_distance(lam_mkt, lam_st))
                    cands = []
                    for sel, o in odds_map.items():
                        o = _f(o)
                        if o is None:
                            continue
                        p = probs[sel]
                        ev = p * o - 1
                        cl = _f(closing_map.get(sel)) if closing_map else None
                        b = Bet(mdate, name, book, "MS" if sel.startswith("MS") else "2.5 A/Ü",
                                sel, o, p, ev, conf, _won(sel, hg, ag), cl, sname)
                        if ev > 0:
                            res.all_value.append(b)
                        if _is_value(o, p, ev, conf):
                            f = min(kelly_fraction(p, o) * settings.kelly_fraction, settings.max_stake_pct)
                            cands.append((growth(p, o, f), b))
                    if cands:
                        res.bets.append(max(cands, key=lambda x: x[0])[1])

            if bH:
                consider("Bet365",
                         {"MS 1": rd.get(bH), "MS X": rd.get(bD), "MS 2": rd.get(bA),
                          **({"2.5 Üst": rd.get(bO), "2.5 Alt": rd.get(bU)} if bO else {})},
                         {"MS 1": rd.get(kH), "MS X": rd.get(kD), "MS 2": rd.get(kA),
                          **({"2.5 Üst": rd.get(kO), "2.5 Alt": rd.get(kU)} if kO else {})} if kH else None)
            if mk_rows:
                mk = _find_mk(mk_rows, home_t, away_t, mdate)
                if mk and mk.odds and _same_score(mk, hg, ag):
                    res.iddaa_matched += 1
                    consider("iddaa", {"MS 1": mk.odds[0], "MS X": mk.odds[1], "MS 2": mk.odds[2]}, None)
    return res


def _f(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x > 1 and not math.isnan(x) else None


def _find_mk(rows, home, away, mdate):
    best, sc = None, 0.0
    for m in rows:
        if abs((m.date.date() - mdate.date()).days) > 1:
            continue
        s = pair_score(home, away, m.home, m.away)
        if s > sc:
            best, sc = m, s
    return best if sc >= 130 else None


def _same_score(mk, hg, ag) -> bool:
    """Maçkolik satırının gerçekten bu maç olduğunu skorla doğrula."""
    return mk.hg is None or (mk.hg == hg and mk.ag == ag)


async def run(code: str, season: int | None = None, use_mackolik: bool = True) -> Result:
    season = season if season is not None else footballdata.current_start_year() - 1
    seasons = {}
    for y in (season - 2, season - 1, season):
        df = await footballdata.fetch_season(code, y, ttl=7 * 24 * 3600 if y < season else 24 * 3600)
        if df is not None:
            seasons[y] = df
    if season not in seasons:
        r = Result(league=code, season=season)
        r.notes.append("Test sezonu verisi indirilemedi.")
        return r
    mk_rows = []
    if use_mackolik:
        try:
            mk_rows = await mackolik.season_odds(code, season)
        except Exception as e:  # noqa: BLE001
            mk_rows = []
            note = f"Maçkolik erişilemedi: {e}"
        else:
            note = None if mk_rows else "Bu sezon için Maçkolik iddaa oranı bulunamadı; sadece Bet365 testi yapıldı."
    else:
        note = None
    res = await asyncio.to_thread(run_sync, code, season, seasons, mk_rows)
    if note:
        res.notes.append(note)
    return res


# ----------------------------------------------------------------- rapor
def _summ(bets: list[Bet]):
    if not bets:
        return None
    pr = [b.profit for b in bets]
    n = len(pr)
    mu = sum(pr) / n
    sd = math.sqrt(sum((x - mu) ** 2 for x in pr) / (n - 1)) if n > 1 else 0.0
    t = mu / (sd / math.sqrt(n)) if sd > 0 else 0.0
    cum, peak, dd = 0.0, 0.0, 0.0
    for x in pr:
        cum += x
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    cl = [b.odds / b.closing - 1 for b in bets if b.closing]
    return {"n": n, "hits": sum(b.won for b in bets), "exp": sum(b.p for b in bets), "profit": sum(pr),
            "roi": mu, "t": t, "dd": dd, "avg_odds": sum(b.odds for b in bets) / n,
            "clv": (sum(cl) / len(cl)) if cl else None, "clv_beat": (sum(1 for x in cl if x > 0) / len(cl)) if cl else None}


def _sig(t: float) -> str:
    return "anlamlı ✅" if t >= 2 else ("anlamlı ❌ (zarar)" if t <= -2 else "şans payı yüksek")


def _line(label: str, s: dict | None) -> str:
    if not s:
        return f"  {label}: bahis çıkmadı"
    txt = (f"  {label}: {s['n']} bahis • tutan {s['hits']} (beklenen {s['exp']:.0f}) • "
           f"ROI <b>{s['roi'] * 100:+.1f}%</b> • t {s['t']:.2f} ({_sig(s['t'])})")
    if s.get("clv") is not None:
        txt += f" • CLV {s['clv'] * 100:+.1f}%"
    return txt


def _ev_buckets(bets: list[Bet]) -> list[str]:
    out = []
    for lo, hi in ((0, .03), (.03, .06), (.06, .10), (.10, 9)):
        sub = _summ([b for b in bets if lo <= b.ev < hi])
        if sub and sub["n"] >= 5:
            lab = f"%{lo * 100:.0f}-{hi * 100:.0f}" if hi < 9 else f"%{lo * 100:.0f}+"
            out.append(f"EV {lab}: {sub['n']} → {sub['roi'] * 100:+.0f}%")
    return out


MIN_GAIN = 0.001  # log-loss'ta bundan küçük iyileşme gürültü sayılır


def ll_table(ll: dict) -> list[tuple]:
    """[(anahtar, ortalama log-loss, örnek sayısı)] — sadece tüm maçlarda ölçülen satırlar karşılaştırılır."""
    base = ll.get(("gol", 0.0)) or []
    rows = [(k, sum(v) / len(v), len(v)) for k, v in ll.items() if isinstance(k, tuple) and v]
    return [r for r in rows if r[2] == len(base)] or rows


def best_setting(ll: dict):
    """(model, pay, iyileşme) — iyileşme MIN_GAIN altındaysa sadece piyasa önerilir."""
    rows = ll_table(ll)
    if not rows:
        return None
    base = next((v for k, v, _ in rows if k[1] == 0), None)
    best = min((r for r in rows if r[0][1] not in (0.0, 1.0)), key=lambda r: r[1], default=None)
    if base is None or best is None or base - best[1] < MIN_GAIN:
        return ("gol", 0.0, 0.0 if best is None or base is None else base - best[1])
    return (best[0][0], best[0][1], base - best[1])


def _ll_block(ll: dict) -> list[str]:
    rows = ll_table(ll)
    if not rows:
        return []
    cur = (settings.stat_model, current_share())
    rows.sort(key=lambda r: r[1])
    L = ["📐 <b>Olasılık doğruluğu</b> (MS log-loss, düşük = iyi; en iyi 6 + piyasa + şu anki)"]
    show = rows[:6] + [r for r in rows[6:] if r[0][1] == 0 or r[0] == cur]
    for i, (k, v, _) in enumerate(show):
        tags = (" ← şu anki" if k == cur or (cur[1] == 0 and k[1] == 0) else "") + (" ⭐" if i == 0 else "")
        L.append(f"  {_key_label(k)}: {v:.4f}{tags}")
    b = best_setting(ll)
    if b and b[1] == 0:
        L.append(f"➜ Hiçbir istatistik modeli piyasayı anlamlı şekilde iyileştirmiyor "
                 f"(en iyi fark {b[2]:.4f}) — <b>sadece piyasa</b> kullanılmalı ⚠️")
    elif b:
        L.append(f"➜ En iyi ayar: <b>{MODELS[b[0]]} %{b[1] * 100:.0f}</b> (piyasaya göre {b[2]:.4f} daha isabetli) ✅")
    return L


def format_report(res: Result) -> str:
    lg = LEAGUE_TR.get(res.league, res.league)
    L = [f"🧪 <b>Backtest — {lg} {res.season}/{(res.season + 1) % 100:02d}</b>",
         f"Test edilen maç: {res.n_matches}" + (f" • iddaa oranı eşleşen: {res.iddaa_matched}" if res.iddaa_matched else "")]
    L += [""] + _ll_block(res.ll)
    for sname, (m, w) in strategies().items():
        title = (f"Strateji A — şu anki ayar ({_key_label((m, w))})" if sname == "mevcut"
                 else "Strateji B — sadece piyasa (oranı, uluslararası piyasanın adil oranıyla karşılaştır)")
        L += ["", f"💰 <b>{title}</b>"]
        for book in ("Bet365", "iddaa"):
            bs = [b for b in res.bets if b.book == book and b.strategy == sname]
            if book == "iddaa" and not res.iddaa_matched:
                continue
            L.append(_line(book, _summ(bs)))
        allv = [b for b in res.all_value if b.book == "Bet365" and b.strategy == sname
                and settings.min_odds <= b.odds <= settings.max_odds]
        ev = _ev_buckets(allv)
        if ev:
            L.append("  EV dilimleri (Bet365): " + " • ".join(ev))
    L.append("")
    L.append("<i>Bir sezon gürültülüdür; asıl karar için /backtest hepsi özetine bak.</i>")
    for n in res.notes:
        L.append(f"⚠️ {n}")
    return "\n".join(L)


def format_summary(results: list[Result]) -> str:
    """Tüm liglerin birleşik özeti — karar için asıl bakılacak yer."""
    rs = [r for r in results if r.n_matches]
    if not rs:
        return "Özet için yeterli veri yok."
    ll = defaultdict(list)
    for r in rs:
        for k, v in r.ll.items():
            ll[k].extend(v)
    bets = [b for r in rs for b in r.bets]
    allv = [b for r in rs for b in r.all_value]
    n = sum(r.n_matches for r in rs)
    L = [f"📊 <b>Backtest özeti — {len(rs)} lig, {n} maç</b>", ""] + _ll_block(ll)
    for sname, (m, w) in strategies().items():
        title = f"Strateji A ({_key_label((m, w))})" if sname == "mevcut" else "Strateji B (sadece piyasa)"
        L += ["", f"💰 <b>{title}</b>"]
        for book in ("Bet365", "iddaa"):
            s = _summ([b for b in bets if b.book == book and b.strategy == sname])
            if s or book == "Bet365":
                L.append(_line(book, s))
        ev = _ev_buckets([b for b in allv if b.book == "Bet365" and b.strategy == sname
                          and settings.min_odds <= b.odds <= settings.max_odds])
        if ev:
            L.append("  EV dilimleri: " + " • ".join(ev))
    L += ["", "<b>Nasıl okunur:</b> Gerçek bir avantaj varsa (1) log-loss'ta istatistikli satırlar piyasadan "
          "düşük çıkar, (2) EV dilimleri yükseldikçe ROI de yükselir, (3) t ≥ 2 olur. Üçü yoksa model "
          "piyasayı yenmiyor demektir; gerçek parayla oynanmamalı."]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="Geçmiş maç testi")
    ap.add_argument("--lig", default="T1", help="T1, E0, SP1, I1, D1, F1")
    ap.add_argument("--sezon", type=int, default=None, help="Başlangıç yılı, ör. 2025 = 2025/26")
    ap.add_argument("--mackolik-yok", action="store_true")
    a = ap.parse_args()
    res = asyncio.run(run(a.lig, a.sezon, not a.mackolik_yok))
    import re
    print(re.sub(r"</?[bi]>", "", format_report(res)))


if __name__ == "__main__":
    main()
