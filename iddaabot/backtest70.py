"""Geçmiş sezonlarda %70 kupon kuralının ileriye dönük testi.

Canlı botun kuralları değişmez. Burada rules70 ile aynı eşikler kullanılır:
bu sezon, maç gününden önce oynanmış iç saha / dış saha maçları, en az 3 maç,
her gerekli yüzde >= %70. Kupon kurucu da canlıdakiyle aynı bantları kullanır
(ayak 1.20–2.50, toplam 2.00–3.00, günde en fazla 3 kupon, maç tek kupona).

iddaa'nın eski oranları bu dosyada yok. Fiyat olarak football-data.co.uk
tablosundaki Bet365 sütunları kullanılır (kapanış değil: B365H, B365>2.5).
Bet365 boşsa piyasa ortalaması (Avg*) devreye girer. CSV'de oranı olmayan
pazarlar sadece isabet olarak yazılır. iddaa genelde daha düşük öder; aynı
kuponlar bir de her ayağı 0.92 ile çarparak ölçülür.

Çalıştırma:
    python -m iddaabot.backtest70
    python -m iddaabot.backtest70 --out /tmp/bt70.json
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np
import pandas as pd

from . import footballdata, rules70
from .engine import Candidate

# (football-data kodu, ESPN lig kodu, görünen ad). Belçika da istatistik dosyası olduğu için dahil.
LEAGUES = (
    ("T1", "tur.1", "Süper Lig"),
    ("E0", "eng.1", "Premier Lig"),
    ("SP1", "esp.1", "La Liga"),
    ("I1", "ita.1", "Serie A"),
    ("D1", "ger.1", "Bundesliga"),
    ("F1", "fra.1", "Ligue 1"),
    ("N1", "ned.1", "Hollanda"),
    ("P1", "por.1", "Portekiz"),
    ("B1", "bel.1", "Belçika"),
)
DEFAULT_SEASONS = (2023, 2024, 2025, 2026)
TUNE_SEASONS = frozenset({2023, 2024})
VALID_SEASONS = frozenset({2025, 2026})
# Kalibrasyon filtresi, bakılmadan kilitlendi: ayar yıllarında n>=150 ve
# (ilan edilen ortalama − gerçekleşen) <= 8 puan olan pazarlar kalır.
CAL_MIN_N = 150
CAL_MAX_GAP = 0.08
MIN_TUNE_COUPONS = 40
HAIRCUT = 0.92
CORNER_LINES = (8.5, 9.5, 10.5)

# 1X2 ve 2.5 alt/üst dışında ücretsiz CSV'de bahisçi oranı yok.
_ODDS_1X2 = {
    "1": (("B365H", "bet365"), ("AvgH", "ortalama")),
    "X": (("B365D", "bet365"), ("AvgD", "ortalama")),
    "2": (("B365A", "bet365"), ("AvgA", "ortalama")),
}
_ODDS_OU25 = {
    "over": (("B365>2.5", "bet365"), ("Avg>2.5", "ortalama")),
    "under": (("B365<2.5", "bet365"), ("Avg<2.5", "ortalama")),
}


def _ou(line: float, side: str, period: str = "FT", scope: str = "total", subject: str = "goals") -> dict:
    return {"kind": "ou", "subject": subject, "period": period, "scope": scope, "line": line, "side": side}


def catalog() -> list[dict]:
    """Canlı kodun desteklediği ve skordan okunabilen pazarlar."""
    specs = []
    for line in (0.5, 1.5, 2.5, 3.5):
        for side in ("over", "under"):
            specs.append(_ou(line, side))
    for line in (0.5, 1.5):
        for scope in ("home", "away"):
            specs.append(_ou(line, "over", scope=scope))
    for line in (0.5, 1.5):
        for side in ("over", "under"):
            specs.append(_ou(line, side, period="H1"))
    for line in CORNER_LINES:
        for side in ("over", "under"):
            specs.append(_ou(line, side, subject="corners"))
    for sel in ("1", "X", "2"):
        specs.append({"kind": "1x2", "subject": "goals", "period": "FT", "sel": sel})
    for sel in (("1", "X"), ("1", "2"), ("X", "2")):
        specs.append({"kind": "dc", "period": "FT", "sel": sel})
    specs.append({"kind": "btts", "period": "FT", "yes": True})
    specs.append({"kind": "btts", "period": "FT", "yes": False})
    return specs


SPECS = catalog()


def spec_key(spec: dict) -> str:
    k = spec["kind"]
    if k == "1x2":
        return f"1x2|{spec['sel']}"
    if k == "dc":
        return "dc|" + "".join(spec["sel"])
    if k == "btts":
        return "btts|" + ("y" if spec.get("yes") else "n")
    return "|".join((
        "ou", spec.get("subject", "goals"), spec.get("period", "FT"),
        spec.get("scope", "total"), f"{float(spec['line']):g}", spec["side"],
    ))


def market_label(spec: dict) -> str:
    k = spec["kind"]
    if k == "1x2":
        return f"MS {spec['sel']}"
    if k == "dc":
        return "ÇŞ " + "".join(spec["sel"])
    if k == "btts":
        return "KG Var" if spec.get("yes") else "KG Yok"
    side = "Üst" if spec["side"] == "over" else "Alt"
    scope = spec.get("scope", "total")
    who = {"home": "Ev ", "away": "Dep ", "total": ""}[scope]
    per = "İY " if spec.get("period") == "H1" else ""
    subj = "Korner " if spec.get("subject") == "corners" else ""
    return f"{per}{who}{subj}{float(spec['line']):g} {side}"


def family_of(spec: dict) -> str:
    k = spec["kind"]
    if k == "1x2":
        return "MS"
    if k == "dc":
        return "ÇŞ"
    if k == "btts":
        return "KG"
    if spec.get("subject") == "corners":
        return "Korner"
    if spec.get("period") == "H1":
        return "İY"
    if spec.get("scope", "total") in ("home", "away"):
        return "Takım gol"
    return "Alt/Üst"


def _hit_arrays(hg: np.ndarray, ag: np.ndarray, spec: dict) -> np.ndarray | None:
    """rules70._hits ile aynı karar, satır satır."""
    k = spec["kind"]
    if k == "ou":
        sc = spec.get("scope", "total")
        v = hg + ag if sc == "total" else (hg if sc == "home" else ag)
        return v > spec["line"] if spec["side"] == "over" else v < spec["line"]
    if k == "btts":
        both = (hg > 0) & (ag > 0)
        return both if spec.get("yes") else ~both
    res = np.where(hg > ag, "1", np.where(hg == ag, "X", "2"))
    if k == "1x2":
        return res == spec["sel"]
    if k == "dc":
        return np.isin(res, list(spec["sel"]))
    return None


def pair_rate(hg: np.ndarray | None, ag: np.ndarray | None, spec: dict) -> tuple[int, int] | None:
    """(tutan, maç). Boş veya skoru olmayan satırlar rules70._rate gibi düşer."""
    if hg is None or ag is None or len(hg) == 0:
        return None
    mask = np.isfinite(hg) & np.isfinite(ag)
    if not np.any(mask):
        return None
    hit = _hit_arrays(hg[mask], ag[mask], spec)
    if hit is None:
        return None
    return int(np.sum(hit)), int(hit.size)


def _scope(spec: dict) -> str:
    return spec.get("scope", "total") if spec["kind"] == "ou" else "total"


def _passes(parts: list[tuple[int, int]], thresh: float, min_games: int) -> bool:
    return bool(parts) and all(n >= min_games and (k / n) >= thresh for k, n in parts)


@dataclass
class Pick:
    league: str
    league_name: str
    season: int
    date: date
    match_id: str
    home: str
    away: str
    market: str
    family: str
    key: str
    claimed_avg: float
    claimed_min: float
    n_min: int
    p_smooth: float
    hit: bool
    odds: float | None
    odds_src: str
    ks: tuple[int, ...] = ()
    ns: tuple[int, ...] = ()
    mbs: int = 1


@dataclass
class CouponView:
    date: date
    season: int
    league: str          # "hepsi" ya da ESPN lig kodu
    league_name: str
    odds: float
    won: bool
    legs: list[Pick] = field(default_factory=list)


def _odd_at(arr: np.ndarray | None, i: int) -> float | None:
    if arr is None:
        return None
    v = float(arr[i])
    if not math.isfinite(v) or v <= 1.01:
        return None
    return v


def lookup_odds(spec: dict, i: int, cols: dict[str, np.ndarray | None]) -> tuple[float | None, str]:
    """Bet365, yoksa piyasa ortalaması. Kapanış sütunları (B365C*) kullanılmaz."""
    pairs = None
    if spec["kind"] == "1x2" and spec.get("period", "FT") == "FT":
        pairs = _ODDS_1X2.get(spec["sel"])
    elif (spec["kind"] == "ou" and spec.get("subject", "goals") == "goals"
          and spec.get("period", "FT") == "FT" and spec.get("scope", "total") == "total"
          and float(spec["line"]) == 2.5):
        pairs = _ODDS_OU25.get(spec["side"])
    if not pairs:
        return None, ""
    for name, src in pairs:
        v = _odd_at(cols.get(name), i)
        if v is not None:
            return v, src
    return None, ""


def _col(df: pd.DataFrame, name: str) -> np.ndarray | None:
    if name not in df.columns:
        return None
    return pd.to_numeric(df[name], errors="coerce").to_numpy(dtype=float)


def walk_frame(df: pd.DataFrame, league: str, league_name: str, season: int,
               thresh: float | None = None, min_games: int | None = None) -> list[Pick]:
    """Tek sezon. Her maç gününde yalnız o günden önceki maçlar kullanılır.

    Aynı gündeki maçlar birbirini görmez. Çağıran, sezonları ayrı ayrı vermelidir;
    önceki sezon bu çerçeveye karışmaz (canlı bot da yalnız içinde bulunulan sezona bakar).
    """
    thresh = rules70.THRESH if thresh is None else thresh
    min_games = rules70.MIN_GAMES if min_games is None else min_games
    if df is None or df.empty:
        return []
    d = df.sort_values(["Date", "HomeTeam", "AwayTeam"], kind="mergesort").reset_index(drop=True)
    d["Date"] = pd.to_datetime(d["Date"]).dt.normalize()
    dates = [ts.date() for ts in d["Date"]]
    home = d["HomeTeam"].astype(str).to_numpy()
    away = d["AwayTeam"].astype(str).to_numpy()
    ft_h, ft_a = _col(d, "FTHG"), _col(d, "FTAG")
    ht_h, ht_a = _col(d, "HTHG"), _col(d, "HTAG")
    c_h, c_a = _col(d, "HC"), _col(d, "AC")
    odd_cols = {name: _col(d, name) for name, _src in (*_ODDS_1X2["1"], *_ODDS_1X2["X"], *_ODDS_1X2["2"],
                                                        *_ODDS_OU25["over"], *_ODDS_OU25["under"])}

    def series_for(spec: dict):
        if spec.get("subject") == "corners":
            return c_h, c_a
        if spec.get("period") == "H1":
            return ht_h, ht_a
        return ft_h, ft_a

    home_hist: dict[str, list[int]] = defaultdict(list)
    away_hist: dict[str, list[int]] = defaultdict(list)
    out: list[Pick] = []
    n = len(d)
    i = 0
    while i < n:
        j = i + 1
        while j < n and dates[j] == dates[i]:
            j += 1
        for r in range(i, j):
            h_ix = home_hist.get(home[r]) or []
            a_ix = away_hist.get(away[r]) or []
            h_arr = np.asarray(h_ix, dtype=int) if h_ix else None
            a_arr = np.asarray(a_ix, dtype=int) if a_ix else None
            for spec in SPECS:
                if not rules70.supported(spec):
                    continue
                hg_all, ag_all = series_for(spec)
                if hg_all is None or ag_all is None:
                    continue
                scope = _scope(spec)
                parts: list[tuple[int, int]] = []
                if scope in ("total", "home"):
                    if h_arr is None:
                        parts = []
                        rate = None
                    else:
                        rate = pair_rate(hg_all[h_arr], ag_all[h_arr], spec)
                    if rate is None:
                        continue
                    parts.append(rate)
                if scope in ("total", "away"):
                    if a_arr is None:
                        rate = None
                    else:
                        rate = pair_rate(hg_all[a_arr], ag_all[a_arr], spec)
                    if rate is None:
                        continue
                    parts.append(rate)
                if not _passes(parts, thresh, min_games):
                    continue
                cur = pair_rate(hg_all[r:r + 1], ag_all[r:r + 1], spec)
                if cur is None:
                    continue
                ks = tuple(k for k, _n in parts)
                ns = tuple(_n for _k, _n in parts)
                rates = [kk / nn for kk, nn in parts]
                odds, src = lookup_odds(spec, r, odd_cols)
                out.append(Pick(
                    league=league, league_name=league_name, season=season, date=dates[r],
                    match_id=f"{league}|{season}|{dates[r].isoformat()}|{home[r]}|{away[r]}",
                    home=home[r], away=away[r], market=market_label(spec), family=family_of(spec),
                    key=spec_key(spec), claimed_avg=float(sum(rates) / len(rates)),
                    claimed_min=float(min(rates)), n_min=int(min(ns)),
                    p_smooth=float(min((kk + 1) / (nn + 2) for kk, nn in parts)),
                    hit=bool(cur[0] == 1), odds=odds, odds_src=src, ks=ks, ns=ns,
                ))
        for r in range(i, j):
            home_hist[home[r]].append(r)
            away_hist[away[r]].append(r)
        i = j
    return out


def walk_seasons(frames: dict[int, pd.DataFrame], league: str, league_name: str) -> list[Pick]:
    """Her sezon ayrı. 2023/24'ün son haftası, 2024/25'in ilk maçına karışmaz."""
    picks: list[Pick] = []
    for season in sorted(frames):
        picks.extend(walk_frame(frames[season], league, league_name, season))
    return picks


def build_coupons(items: list[tuple[object, list]], sizes: tuple[int, ...] = (2, 3, 4, 5),
                  max_coupons: int = 3) -> list:
    """rules70.coupons ile aynı arama. items: (maç, options() sırasındaki adaylar).

    Adaylar burada oran bandına süzülür ve maç başı en fazla 3 tanesi kalır.
    sizes varsayılanı (2, 3, 4, 5) iken canlı fonksiyonla aynı kuponu seçer.
    """
    pool = []
    lo, hi = rules70.LEG_ODDS
    t_lo, t_hi = rules70.TOTAL_ODDS
    for match, cs in items:
        kept = [c for c in cs if c.odds and lo <= c.odds <= hi]
        kept.sort(key=lambda c: (-c.p, -c.odds))
        if kept:
            pool.append((match, kept[:3]))
    pool.sort(key=lambda x: -x[1][0].p)
    out, used = [], set()
    for _ in range(max_coupons):
        cand = [(m, cs) for m, cs in pool if m.fx.espn_id not in used][:12]
        best, best_p = None, -1.0
        for k in sizes:
            if len(cand) < k:
                break
            for ms in itertools.combinations(cand, k):
                opts = [cs for _, cs in ms]
                lo_o = math.prod(min(c.odds for c in cs) for cs in opts)
                hi_o = math.prod(max(c.odds for c in cs) for cs in opts)
                if hi_o < t_lo or lo_o > t_hi:
                    continue
                for choice in itertools.product(*opts):
                    if max(c.mbs for c in choice) > k:
                        continue
                    o = math.prod(c.odds for c in choice)
                    if not t_lo <= o <= t_hi:
                        continue
                    p = math.prod(c.p for c in choice)
                    if p > best_p + 1e-12:
                        best_p = p
                        best = [(m, c) for (m, _), c in zip(ms, choice)]
        if not best:
            break
        out.append(best)
        used |= {m.fx.espn_id for m, _ in best}
    return out


def _candidates(picks: list[Pick]) -> list:
    cs = []
    for p in picks:
        if p.odds is None:
            continue
        c = Candidate(market=p.market, outcome=p.market, spec={"key": p.key}, odds=p.odds,
                      p=p.p_smooth, ev=p.p_smooth * p.odds - 1, conf="orta", margin=None, mbs=p.mbs)
        c.pick = p
        cs.append(c)
    return cs


def coupons_from_picks(picks: list[Pick], *, league: str, league_name: str,
                       sizes: tuple[int, ...] = (2, 3, 4, 5)) -> list[CouponView]:
    """Aynı takvim günündeki maçlardan kupon. league='hepsi' ise ligler karışık, canlı bot gibi."""
    by_day: dict[date, dict[str, list[Pick]]] = defaultdict(lambda: defaultdict(list))
    for p in picks:
        by_day[p.date][p.match_id].append(p)
    coupons: list[CouponView] = []
    for day in sorted(by_day):
        items = []
        for mid, legs in sorted(by_day[day].items()):
            cs = _candidates(legs)
            if not cs:
                continue
            match = type("M", (), {})()
            match.fx = type("F", (), {"espn_id": mid})()
            items.append((match, cs))
        if len(items) < 2:
            continue
        for legs in build_coupons(items, sizes=sizes):
            lp = [c.pick for _, c in legs]
            won = all(p.hit for p in lp)
            odds = math.prod(p.odds for p in lp)
            season = lp[0].season
            coupons.append(CouponView(date=day, season=season, league=league, league_name=league_name,
                                      odds=odds, won=won, legs=lp))
    return coupons


def paid_odds(cp: CouponView, mult: float = 1.0) -> float:
    return math.prod(p.odds * mult for p in cp.legs)


def profit(cp: CouponView, mult: float = 1.0) -> float:
    if not cp.won:
        return -1.0
    return paid_odds(cp, mult) - 1.0


def losing_streak(coupons: list[CouponView]) -> int:
    best = cur = 0
    for cp in sorted(coupons, key=lambda c: (c.date, c.league, c.odds)):
        if cp.won:
            cur = 0
        else:
            cur += 1
            best = max(best, cur)
    return best


def max_drawdown(coupons: list[CouponView], mult: float = 1.0) -> float:
    """Düz 1 birimlik kasada en derin düşüş (zirveden, birim cinsinden)."""
    peak = equity = 0.0
    worst = 0.0
    for cp in sorted(coupons, key=lambda c: (c.date, c.league, c.odds)):
        equity += profit(cp, mult)
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return worst


@dataclass
class Block:
    n: int
    claimed: float
    actual: float
    hits: int

    def as_dict(self) -> dict:
        return {"n": self.n, "claimed": self.claimed, "actual": self.actual, "hits": self.hits,
                "gap": (self.actual - self.claimed) if self.n else None}


def _block(picks: list[Pick]) -> Block:
    n = len(picks)
    if n == 0:
        return Block(0, 0.0, 0.0, 0)
    hits = sum(1 for p in picks if p.hit)
    return Block(n, sum(p.claimed_avg for p in picks) / n, hits / n, hits)


def _bucket(claimed: float) -> str:
    if claimed < 0.75:
        return "70-75"
    if claimed < 0.80:
        return "75-80"
    if claimed < 0.90:
        return "80-90"
    return "90+"


def _sample_bucket(n_min: int) -> str:
    return "3-5" if n_min <= 5 else "6+"


@dataclass
class CouponStats:
    n: int
    wins: int
    win_rate: float
    avg_odds: float
    avg_paid: float
    roi: float
    streak: int
    drawdown: float
    still_in_band: int

    def as_dict(self) -> dict:
        return {
            "n": self.n, "wins": self.wins, "win_rate": self.win_rate,
            "avg_odds": self.avg_odds, "avg_paid": self.avg_paid, "roi": self.roi,
            "losing_streak": self.streak, "max_drawdown": self.drawdown,
            "still_in_band": self.still_in_band,
            "legs": getattr(self, "legs", {}),
        }


def coupon_stats(coupons: list[CouponView], mult: float = 1.0) -> CouponStats:
    n = len(coupons)
    if n == 0:
        return CouponStats(0, 0, 0.0, 0.0, 0.0, 0.0, 0, 0.0, 0)
    wins = sum(1 for c in coupons if c.won)
    paid = [paid_odds(c, mult) for c in coupons]
    prof = [profit(c, mult) for c in coupons]
    band = sum(1 for o in paid if rules70.TOTAL_ODDS[0] <= o <= rules70.TOTAL_ODDS[1])
    sizes = {k: sum(1 for c in coupons if len(c.legs) == k) for k in (2, 3, 4, 5)}
    stats = CouponStats(
        n=n, wins=wins, win_rate=wins / n,
        avg_odds=sum(c.odds for c in coupons) / n,
        avg_paid=sum(paid) / n,
        roi=sum(prof) / n,
        streak=losing_streak(coupons),
        drawdown=max_drawdown(coupons, mult),
        still_in_band=band,
    )
    stats.legs = sizes  # type: ignore[attr-defined]
    return stats


def single_roi(picks: list[Pick], mult: float = 1.0) -> dict:
    priced = [p for p in picks if p.odds]
    if not priced:
        return {"n": 0, "roi": None, "avg_odds": None}
    prof = [(p.odds * mult - 1.0) if p.hit else -1.0 for p in priced]
    return {"n": len(priced), "roi": sum(prof) / len(prof),
            "avg_odds": sum(p.odds * mult for p in priced) / len(priced)}


def group_rows(picks: list[Pick], keyfn) -> list[dict]:
    buckets: dict = defaultdict(list)
    for p in picks:
        buckets[keyfn(p)].append(p)
    rows = []
    for key in sorted(buckets, key=lambda k: (-len(buckets[k]), str(k))):
        b = _block(buckets[key])
        rows.append({"key": key, **b.as_dict()})
    return rows


def filter_picks(picks: list[Pick], *, thresh: float, min_games: int,
                 families: set[str] | None = None, keys: set[str] | None = None) -> list[Pick]:
    out = []
    for p in picks:
        if not _passes(list(zip(p.ks, p.ns)), thresh, min_games):
            continue
        if families is not None and p.family not in families:
            continue
        if keys is not None and p.key not in keys:
            continue
        out.append(p)
    return out


# Basitlik sırası: beraberlikte daha sade kural kazanır. Liste, sonuç görülmeden kilitlendi.
VARIANTS = (
    {"id": "mevcut", "label": "Mevcut kural (%70, en az 3 maç)", "thresh": 0.70, "min_games": 3,
     "sizes": (2, 3, 4, 5), "families": None},
    {"id": "en_az_6", "label": "En az 6 maç", "thresh": 0.70, "min_games": 6,
     "sizes": (2, 3, 4, 5), "families": None},
    {"id": "yuzde_80", "label": "Eşik %80", "thresh": 0.80, "min_games": 3,
     "sizes": (2, 3, 4, 5), "families": None},
    {"id": "iki_ayak", "label": "Kuponda en fazla 2 maç", "thresh": 0.70, "min_games": 3,
     "sizes": (2,), "families": None},
    {"id": "sadece_25", "label": "Sadece 2.5 Alt/Üst", "thresh": 0.70, "min_games": 3,
     "sizes": (2, 3, 4, 5), "families": None,
     "keys": {"ou|goals|FT|total|2.5|over", "ou|goals|FT|total|2.5|under"}},
    {"id": "sadece_ms", "label": "Sadece maç sonucu", "thresh": 0.70, "min_games": 3,
     "sizes": (2, 3, 4, 5), "families": {"MS"}},
    {"id": "en_az_6_ve_80", "label": "En az 6 maç ve %80", "thresh": 0.80, "min_games": 6,
     "sizes": (2, 3, 4, 5), "families": None},
)


def calibrated_keys(tune_picks: list[Pick], min_n: int = CAL_MIN_N, max_gap: float = CAL_MAX_GAP) -> set[str]:
    """Ayar yıllarına bakarak aşırı iyimser pazarları eler. Doğrulama yılları girmez."""
    by: dict[str, list[Pick]] = defaultdict(list)
    for p in tune_picks:
        if p.season not in TUNE_SEASONS:
            raise ValueError("kalibrasyon filtresi yalnız ayar sezonlarını görmeli")
        by[p.key].append(p)
    keep = set()
    for key, rows in by.items():
        b = _block(rows)
        if b.n >= min_n and (b.claimed - b.actual) <= max_gap:
            keep.add(key)
    return keep


def _variant_picks(picks: list[Pick], spec: dict, extra_keys: set[str] | None = None) -> list[Pick]:
    keys = spec.get("keys")
    if extra_keys is not None:
        keys = extra_keys if keys is None else (set(keys) & extra_keys)
    return filter_picks(picks, thresh=spec["thresh"], min_games=spec["min_games"],
                        families=spec.get("families"), keys=keys)


def _run_variant(picks: list[Pick], spec: dict, extra_keys: set[str] | None = None,
                 with_leagues: bool = False) -> dict:
    chosen = _variant_picks(picks, spec, extra_keys)
    cross = coupons_from_picks(chosen, league="hepsi", league_name="Tüm ligler", sizes=spec["sizes"])
    by_league = []
    if with_leagues:
        for _fd, espn, name in LEAGUES:
            sub = [p for p in chosen if p.league == espn]
            cps = coupons_from_picks(sub, league=espn, league_name=name, sizes=spec["sizes"])
            by_league.append({
                "league": name, "code": espn,
                "picks": _block(sub).as_dict(),
                "coupons": coupon_stats(cps).as_dict(),
                "coupons_092": coupon_stats(cps, HAIRCUT).as_dict(),
            })
    by_season = []
    for season in sorted({p.season for p in chosen} | {c.season for c in cross}):
        sp = [p for p in chosen if p.season == season]
        sc = [c for c in cross if c.season == season]
        by_season.append({"season": season, "partial": season == 2026,
                          "picks": _block(sp).as_dict(),
                          "coupons": coupon_stats(sc).as_dict(),
                          "coupons_092": coupon_stats(sc, HAIRCUT).as_dict()})
    return {
        "id": spec["id"], "label": spec["label"],
        "picks": _block(chosen).as_dict(),
        "priced": single_roi(chosen),
        "priced_092": single_roi(chosen, HAIRCUT),
        "coupons": coupon_stats(cross).as_dict(),
        "coupons_092": coupon_stats(cross, HAIRCUT).as_dict(),
        "by_season": by_season,
        "by_league": by_league,
        "leg_hit": _block([p for c in cross for p in c.legs]).as_dict(),
        "leg_markets": group_rows([p for c in cross for p in c.legs], lambda p: p.market),
    }


def choose_variant(tune_roi: dict[str, dict]) -> str:
    """Ayar yıllarının kupon ROI'si. Beraberlikte VARIANTS sırasındaki sade kural.

    Doğrulama sezonunun kârına bakılmaz. tune_roi değerleri coupon_stats().as_dict().
    """
    order = [v["id"] for v in VARIANTS] + ["kalibrasyon"]
    eligible = [k for k in order if k in tune_roi and tune_roi[k]["n"] >= MIN_TUNE_COUPONS]
    if not eligible:
        return "mevcut"
    return min(eligible, key=lambda k: (-tune_roi[k]["roi"], order.index(k)))


def evaluate_variants(picks: list[Pick]) -> dict:
    tune = [p for p in picks if p.season in TUNE_SEASONS]
    valid = [p for p in picks if p.season in VALID_SEASONS]
    keep = calibrated_keys(tune)
    specs = list(VARIANTS) + [{
        "id": "kalibrasyon",
        "label": f"Ayar yıllarında dürüst kalan pazarlar (n≥{CAL_MIN_N}, sapma ≤{CAL_MAX_GAP:.0%})",
        "thresh": 0.70, "min_games": 3, "sizes": (2, 3, 4, 5), "families": None, "keys": keep,
    }]
    tune_runs = {s["id"]: _run_variant(tune, s) for s in specs}
    valid_runs = {s["id"]: _run_variant(valid, s) for s in specs}
    chosen = choose_variant({k: run["coupons"] for k, run in tune_runs.items()})
    return {
        "chosen": chosen,
        "kept_markets": sorted(keep),
        "tune": tune_runs,
        "valid": valid_runs,
    }


def build_report(picks: list[Pick], meta: dict) -> dict:
    base = _run_variant(picks, VARIANTS[0], with_leagues=True)
    variants = evaluate_variants(picks)
    priced = [p for p in picks if p.odds]
    return {
        "meta": meta,
        "picks": _block(picks).as_dict(),
        "priced_picks": _block(priced).as_dict(),
        "by_family": group_rows(picks, lambda p: p.family),
        "by_market": group_rows(picks, lambda p: p.market),
        "by_bucket": group_rows(picks, lambda p: _bucket(p.claimed_avg)),
        "by_sample": group_rows(picks, lambda p: _sample_bucket(p.n_min)),
        "by_league_picks": group_rows(picks, lambda p: p.league_name),
        "by_season_picks": group_rows(picks, lambda p: f"{p.season}/{(p.season + 1) % 100:02d}"),
        "coupons": base["coupons"],
        "coupons_092": base["coupons_092"],
        "coupon_legs": base["leg_hit"],
        "leg_markets": base["leg_markets"],
        "singles": base["priced"],
        "singles_092": base["priced_092"],
        "by_season": base["by_season"],
        "by_league": base["by_league"],
        "cross_n": base["coupons"]["n"],
        "variants": variants,
        "odds_src": group_rows(priced, lambda p: p.odds_src or "yok"),
    }


def _pct(x, digits=1) -> str:
    if x is None:
        return "—"
    return f"%{x * 100:.{digits}f}"


def _num(x, digits=2) -> str:
    if x is None:
        return "—"
    return f"{x:.{digits}f}"


def format_report(rep: dict) -> str:
    lines = []
    m = rep["meta"]
    lines.append(f"Maç {m.get('matches', '?')}  seçim {rep['picks']['n']}  "
                 f"ilan {_pct(rep['picks']['claimed'])}  tutan {_pct(rep['picks']['actual'])}")
    lines.append("AİLE")
    for row in rep["by_family"]:
        lines.append(f"  {row['key']}: n={row['n']} ilan={_pct(row['claimed'])} tutan={_pct(row['actual'])} "
                     f"fark={_pct(row['gap'])}")
    lines.append("DİLİM")
    for row in sorted(rep["by_bucket"], key=lambda r: r["key"]):
        lines.append(f"  {row['key']}: n={row['n']} ilan={_pct(row['claimed'])} tutan={_pct(row['actual'])}")
    lines.append("ÖRNEK")
    for row in sorted(rep["by_sample"], key=lambda r: r["key"]):
        lines.append(f"  {row['key']}: n={row['n']} ilan={_pct(row['claimed'])} tutan={_pct(row['actual'])}")
    c, h = rep["coupons"], rep["coupons_092"]
    lines.append(f"KUPON n={c['n']} tutma={_pct(c['win_rate'])} ort_oran={_num(c['avg_odds'])} "
                 f"ROI={_pct(c['roi'])} seri={c['losing_streak']} dd={_num(c['max_drawdown'])} "
                 f"ayak={c.get('legs')}")
    lines.append(f"KUPON92 n={h['n']} ort_odenen={_num(h['avg_paid'])} ROI={_pct(h['roi'])} "
                 f"bantta={h['still_in_band']} seri={h['losing_streak']}")
    lines.append(f"TEKLİ n={rep['singles']['n']} ROI={_pct(rep['singles']['roi'])} "
                 f"ROI92={_pct(rep['singles_092']['roi'])}")
    lines.append("KUPON AYAK")
    for row in rep.get("leg_markets") or []:
        lines.append(f"  {row['key']}: n={row['n']} ilan={_pct(row['claimed'])} tutan={_pct(row['actual'])}")
    lines.append("SEZON")
    for row in rep["by_season"]:
        cc, hh = row["coupons"], row["coupons_092"]
        lines.append(f"  {row['season']}{'*' if row['partial'] else ''} seçim n={row['picks']['n']} "
                     f"tutan={_pct(row['picks']['actual'])} kupon n={cc['n']} "
                     f"tutma={_pct(cc['win_rate'])} ROI={_pct(cc['roi'])} ROI92={_pct(hh['roi'])} "
                     f"seri={cc['losing_streak']}")
    lines.append("LİG (lig içi kupon)")
    for row in rep["by_league"]:
        cc = row["coupons"]
        lines.append(f"  {row['league']}: seçim n={row['picks']['n']} tutan={_pct(row['picks']['actual'])} "
                     f"kupon n={cc['n']} tutma={_pct(cc['win_rate'])} ort={_num(cc['avg_odds'])} "
                     f"ROI={_pct(cc['roi'])} ROI92={_pct(row['coupons_092']['roi'])} seri={cc['losing_streak']}")
    lines.append(f"SEÇİLEN {rep['variants']['chosen']}")
    for split in ("tune", "valid"):
        lines.append(split.upper())
        for spec in list(VARIANTS) + [{"id": "kalibrasyon"}]:
            run = rep["variants"][split][spec["id"]]
            cc, hh = run["coupons"], run["coupons_092"]
            lines.append(f"  {spec['id']}: seçim n={run['picks']['n']} tutan={_pct(run['picks']['actual'])} "
                         f"kupon n={cc['n']} tutma={_pct(cc['win_rate'])} ROI={_pct(cc['roi'])} "
                         f"ROI92={_pct(hh['roi'])} seri={cc['losing_streak']}")
    lines.append("PAZAR")
    for row in rep["by_market"]:
        if row["n"] < 30:
            continue
        lines.append(f"  {row['key']}: n={row['n']} ilan={_pct(row['claimed'])} tutan={_pct(row['actual'])} "
                     f"fark={_pct(row['gap'])}")
    return "\n".join(lines)


async def load_frames(seasons: tuple[int, ...]) -> tuple[dict, dict]:
    frames: dict[str, dict[int, pd.DataFrame]] = {}
    notes = {"files": [], "missing": [], "matches": 0}
    for code, espn, name in LEAGUES:
        frames[espn] = {}
        for year in seasons:
            df = await footballdata.fetch_season(code, year)
            if df is None or df.empty:
                notes["missing"].append(f"{code} {year}")
                continue
            frames[espn][year] = df
            notes["files"].append({"code": code, "league": name, "season": year, "rows": int(len(df))})
            notes["matches"] += int(len(df))
            print(f"  {name} {year}/{(year + 1) % 100:02d}: {len(df)} maç", flush=True)
    return frames, notes


def run_frames(frames: dict[str, dict[int, pd.DataFrame]], meta: dict) -> dict:
    picks: list[Pick] = []
    names = {espn: name for _c, espn, name in LEAGUES}
    for _code, espn, name in LEAGUES:
        got = walk_seasons(frames.get(espn, {}), espn, name)
        print(f"  {name}: {len(got)} seçim", flush=True)
        picks.extend(got)
    meta = dict(meta)
    meta["names"] = names
    meta["generated"] = datetime.now().isoformat(timespec="seconds")
    meta["rules"] = {
        "thresh": rules70.THRESH, "min_games": rules70.MIN_GAMES,
        "leg_odds": list(rules70.LEG_ODDS), "total_odds": list(rules70.TOTAL_ODDS),
        "haircut": HAIRCUT, "corner_lines": list(CORNER_LINES),
        "odds": "B365 (yoksa Avg); kapanış sütunları yok; 1X2 ve 2.5 dışında fiyat yok",
        "history": "yalnız içinde bulunulan sezon, maç gününden önceki iç/dış saha",
        "mbs": "CSV'de yok, 1 kabul edildi",
    }
    return build_report(picks, meta)


async def amain(seasons: tuple[int, ...], out: str) -> dict:
    print("Dosyalar indiriliyor...", flush=True)
    frames, notes = await load_frames(seasons)
    print("Maç günü maç günü hesaplanıyor...", flush=True)
    rep = run_frames(frames, notes)
    text = format_report(rep)
    print(text, flush=True)
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False)
        print(f"JSON: {out}", flush=True)
    return rep


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="%70 kupon kuralının geçmiş sezon testi")
    p.add_argument("--seasons", nargs="*", type=int, default=list(DEFAULT_SEASONS))
    p.add_argument("--out", default="")
    args = p.parse_args(argv)
    asyncio.run(amain(tuple(args.seasons), args.out))


if __name__ == "__main__":
    main()
