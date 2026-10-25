"""%70 kuralı: takım istatistiği %70 ve üstü olan seçenekler ve bunlardan kurulan kuponlar.

Kural (kullanıcının istediği gibi):
- Takıma ait seçenekler (Ev 0.5 Üst, Dep 1.5 Alt, ev/deplasman korner): o takımın bu sezonki
  ilgili maçlarındaki oranı %70+ olmalı. Ev sahibi için iç saha, deplasman için dış saha maçları.
  Örnek: deplasman takımı dış saha maçlarının %72'sinde gol attıysa → Dep 0.5 Üst.
- Maça ait seçenekler (2.5 Üst, KG Var, MS 1, ÇŞ, İY 0.5 Üst, toplam korner): ev sahibinin iç saha
  maçlarında da, deplasmanın dış saha maçlarında da %70+ olmalı.
Seçenekler iddaa bülteninde o maç için açılmış pazarlardan alınır (oranı olanlar).
"""
from __future__ import annotations

import itertools
import math

import numpy as np
import pandas as pd

from .config import COUPON_LEAGUES, settings
from .engine import Candidate, Coupon, MatchAnalysis

THRESH = 0.70
MIN_GAMES = 3
LEG_ODDS = (1.20, 2.50)      # kupon ayağı oran aralığı
TOTAL_ODDS = (2.0, 3.0)      # kupon toplam oranı
LIST_MIN_ODDS = 1.15         # listede gösterilecek en düşük oran (1.05'lik seçenekler kalabalık eder)
KIND = "%70 Kuponu"
COLS = {"goals": {"FT": ("FTHG", "FTAG"), "H1": ("HTHG", "HTAG")},
        "corners": {"FT": ("HC", "AC")}}


def _values(rows: pd.DataFrame, subj: str, per: str):
    cols = COLS.get(subj, {}).get(per)
    if not cols or any(c not in rows.columns for c in cols):
        return None
    d = rows[list(cols)].dropna()
    return d[cols[0]].astype(float), d[cols[1]].astype(float)


def _hits(spec: dict, hg, ag):
    """Satır başına seçenek tuttu mu (maç yönünde: hg ev sahibi, ag deplasman)."""
    k = spec["kind"]
    if k == "ou":
        sc = spec.get("scope", "total")
        v = hg + ag if sc == "total" else (hg if sc == "home" else ag)
        return v > spec["line"] if spec["side"] == "over" else v < spec["line"]
    if k == "btts":
        both = (hg > 0) & (ag > 0)
        return both if spec.get("yes") else ~both
    res = pd.Series(np.where(hg > ag, "1", np.where(hg == ag, "X", "2")), index=hg.index)
    if k == "1x2":
        return res == spec["sel"]
    if k == "dc":
        return res.isin(tuple(spec["sel"]))
    return None


def supported(spec: dict | None) -> bool:
    if not spec:
        return False
    subj, per, k = spec.get("subject", "goals"), spec.get("period", "FT"), spec.get("kind")
    if subj not in COLS or per not in COLS[subj]:
        return False
    if subj == "corners":
        return k == "ou"
    return k in ("ou", "btts", "1x2", "dc") and not (k == "btts" and per != "FT") and not (k == "dc" and per != "FT")


def _rate(rows, spec):
    v = _values(rows, spec.get("subject", "goals"), spec.get("period", "FT"))
    if v is None or len(v[0]) == 0:
        return None
    h = _hits(spec, *v)
    if h is None:
        return None
    return int(h.sum()), int(len(h))


def evaluate(a: MatchAnalysis, spec: dict):
    """(geçti mi, ayrıntı) — ayrıntı: [(takım, yer, tutan, maç)] ve kapsam."""
    if not (a.ld and a.fd_home and a.fd_away) or not supported(spec):
        return None
    d = a.ld.df_cur
    H = d[d["HomeTeam"] == a.fd_home]            # ev sahibinin iç saha maçları
    A = d[d["AwayTeam"] == a.fd_away]            # deplasmanın dış saha maçları
    scope = spec.get("scope", "total") if spec["kind"] == "ou" else "total"
    parts = []
    if scope in ("total", "home"):
        r = _rate(H, spec)
        if not r:
            return None
        parts.append((a.fx.home, "iç saha", *r))
    if scope in ("total", "away"):
        r = _rate(A, spec)
        if not r:
            return None
        parts.append((a.fx.away, "dış saha", *r))
    ok = all(n >= MIN_GAMES and k / n >= THRESH for _, _, k, n in parts)
    return ok, parts, scope


def options(a: MatchAnalysis, min_odds: float = 1.0) -> list[Candidate]:
    """Maçın iddaa bülteninde %70 kuralını geçen seçenekleri (oranlı). c.p: sıralama için
    küçük örnekte temkinli oran ((tutan+1)/(maç+2)); c.rates: [(takım, yer, tutan, maç)]."""
    if not a.ev or a.fx.league not in COUPON_LEAGUES or not a.ld:
        return []
    out, seen = [], set()
    for m in a.ev.markets:
        for o in m.outcomes:
            sp = o.spec
            if not supported(sp) or not o.odd or o.odd < min_odds:
                continue
            key = (m.name, o.name)
            if key in seen:
                continue
            seen.add(key)
            r = evaluate(a, sp)
            if not r or not r[0]:
                continue
            _, parts, scope = r
            p = min((k + 1) / (n + 2) for _, _, k, n in parts)
            c = Candidate(market=m.name, outcome=o.name, spec=sp, odds=o.odd, p=p, ev=p * o.odd - 1,
                          conf="orta", margin=None, mbs=m.mbs)
            c.rates, c.scope = parts, scope
            c.p_safe = p
            c.raw = min(k / n for _, _, k, n in parts)
            out.append(c)
    return sorted(out, key=lambda c: (-c.p, -c.odds))


def stat_label(c) -> str:
    """'Ev %80 / Dep %75' ya da 'Dep %72'."""
    if c.scope == "total":
        (_, _, k1, n1), (_, _, k2, n2) = c.rates
        return f"Ev %{k1 / n1 * 100:.0f} / Dep %{k2 / n2 * 100:.0f}"
    _, _, k, n = c.rates[0]
    return f"{'Ev' if c.scope == 'home' else 'Dep'} %{k / n * 100:.0f}"


def reason(c) -> str:
    """'Galatasaray iç saha maçlarının 4/5'inde (%80) • Fenerbahçe dış saha maçlarının 3/4'ünde (%75)'"""
    return " • ".join(f"{t} {yer} maçlarında {k}/{n} (%{k / n * 100:.0f})" for t, yer, k, n in c.rates)


def coupons(analyses: list[MatchAnalysis], bankroll: float, max_coupons: int = 3) -> list[Coupon]:
    """%70 seçeneklerinden toplam oranı 2-3 olan kuponlar; her maç tek kupona girer."""
    pool = []
    for a in analyses:
        cs = [c for c in options(a) if LEG_ODDS[0] <= c.odds <= LEG_ODDS[1]]
        if cs:
            pool.append((a, cs[:3]))
    pool.sort(key=lambda x: -x[1][0].p)
    out, used = [], set()
    st = max(round(bankroll * settings.fun_stake_pct / 5) * 5, settings.min_stake)
    for _ in range(max_coupons):
        cand = [(a, cs) for a, cs in pool if a.fx.espn_id not in used][:12]
        best, best_p = None, -1.0
        for k in (2, 3, 4, 5):
            if len(cand) < k:
                break
            for ms in itertools.combinations(cand, k):
                for choice in itertools.product(*(cs for _, cs in ms)):
                    if max(c.mbs for c in choice) > k:
                        continue
                    o = math.prod(c.odds for c in choice)
                    if not TOTAL_ODDS[0] <= o <= TOTAL_ODDS[1]:
                        continue
                    p = math.prod(c.p for c in choice)
                    if p > best_p + 1e-12:
                        best_p, best = p, [(a, c) for (a, _), c in zip(ms, choice)]
        if not best:
            break
        o = math.prod(c.odds for _, c in best)
        out.append(Coupon(kind=KIND, legs=best, odds=o, p=best_p, ev=best_p * o - 1, stake=st))
        used |= {a.fx.espn_id for a, _ in best}
    return out
