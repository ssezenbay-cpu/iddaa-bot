"""Değer motorunun ileriye dönük geçmiş testi.

Canlıdaki /deger mantığı, eşikler değiştirilmeden uygulanır:
- Olasılık, keskin yabancı orandan (Pinnacle açılış; yoksa piyasa ortalaması) kurulan gol modeli.
- Güven, o güne kadarki Dixon-Coles modeliyle piyasanın uyumu (canlıdaki gibi). İstatistik
  payı varsayılan 0 olduğu için olasılığın kendisi piyasadan gelir; model yalnız eşiği seçer.
- Bahis fiyatı Bet365 açılışı (B365H, B365>2.5). Kapanış sütunları (B365CH, PSCH, AvgCH) bahis
  fiyatı olarak kullanılmaz.
- Maç başına en fazla bir seçim: büyümesi en yüksek değerli seçenek.
- CSV'de fiyatı olan ana pazarlar: MS 1/X/2 ve 2.5 alt/üst.

Kapanış değeri (CLV): alınan oran, Pinnacle kapanışından (yoksa piyasa ortalaması kapanışından)
yüksek mi. iddaa benzeri fiyat için aynı motor bir de açılış × 0.92 ile çalıştırılır.
Eşikler ayar sezonuna göre oynatılmaz; 2023/24–2024/25 ve 2025/26–2026/27 ayrı yazılır.

    python -m iddaabot.backtest_value --out /tmp/bt_value.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from . import dixon_coles, footballdata
from .config import settings
from .engine import Candidate, growth, is_value, market_lambdas, stake_for, tv_distance
from .markets import GoalModel, devig, selection_prob

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
TEST_SEASONS = (2023, 2024, 2025, 2026)
TUNE = frozenset({2023, 2024})
VALID = frozenset({2025, 2026})
HAIRCUT = 0.92
BANKROLL = 1000.0

# iddaa'da tekli (MBS 1) oynanabilenler ile kupona yazılması gerekenler.
# Canlı bültende mbc alandan gelir; tarihî dosyada MBS yoktur. Not, güncel bülten alışkanlığıdır:
# MS ve 2.5 alt/üst büyük maçlarda çoğu zaman tekli (MBS 1). Korner, kart, İY/MS, skor ve
# birçok yan pazar MBS 2 veya 3 ister; onlar bu testte zaten yok çünkü CSV'de fiyatları yok.
MBS_NOTE = (
    "iddaa her maça bir MBS (en az kaçlı kupon) yazar; bot bunu bültendeki mbc alanından okur. "
    "25 Kasım 2026 bülteninde Maç Sonucu satırlarının bir kısmı MBS 1 (tekli), bir kısmı MBS 2, "
    "çoğu MBS 3 idi. Alt/Üst de aynı şekilde dağılmıştı. Büyük ligde tekli daha sık açılır, "
    "yine de maç maç değişir. Bu geçmiş test MBS'yi yok sayar ve tek bahis gibi hesaplar: "
    "MBS 2 veya 3 olan bir oranı iddaa'da tek başına oynayamazsın. Korner, kart, skor ve "
    "ilk yarı/maç sonucu da çoğu zaman kupona yazılmak zorundadır; onların eski fiyatı CSV'de yok."
)

_SPECS = {
    "MS 1": {"kind": "1x2", "subject": "goals", "period": "FT", "sel": "1"},
    "MS X": {"kind": "1x2", "subject": "goals", "period": "FT", "sel": "X"},
    "MS 2": {"kind": "1x2", "subject": "goals", "period": "FT", "sel": "2"},
    "2.5 Üst": {"kind": "ou", "subject": "goals", "period": "FT", "scope": "total", "line": 2.5, "side": "over"},
    "2.5 Alt": {"kind": "ou", "subject": "goals", "period": "FT", "scope": "total", "line": 2.5, "side": "under"},
}


def _num(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x) or x <= 1.01:
        return None
    return x


def _col(df: pd.DataFrame, *names: str) -> str | None:
    for n in names:
        if n in df.columns:
            return n
    return None


@dataclass
class VBet:
    season: int
    league: str
    league_name: str
    date: datetime
    match: str
    market: str
    selection: str
    odds: float
    p: float
    ev: float
    conf: str
    stake: float
    won: bool
    close: float | None
    close_src: str
    model_src: str

    @property
    def flat(self) -> float:
        return self.odds - 1 if self.won else -1.0

    @property
    def kelly_pnl(self) -> float:
        return self.stake * self.flat

    @property
    def clv(self) -> float | None:
        if not self.close:
            return None
        return self.odds / self.close - 1


def confidence(lam_mkt, lam_dc) -> str:
    """Canlı analyse(): dış oran varsa ve istatistik modeli varsa uyuma göre, yoksa orta."""
    if lam_mkt and lam_dc:
        gap = tv_distance(lam_mkt, lam_dc)
        return "yüksek" if gap < 0.06 else ("orta" if gap < 0.12 else "düşük")
    if lam_mkt:
        return "orta"
    return "yok"


def _blend(lam_mkt, lam_dc, w: float):
    if not lam_dc or w <= 0:
        return lam_mkt
    return tuple(math.exp((1 - w) * math.log(a) + w * math.log(b)) for a, b in zip(lam_mkt, lam_dc))


def _weight() -> float:
    s = settings.w_market + settings.w_dc
    return settings.w_dc / s if s else 0.0


def market_sane(spec_probs: list[tuple[dict, float]], odds: list[float]) -> bool:
    """Canlıdaki şüpheli pazar eleği: model ile oran 25 puandan fazla ayrışırsa pazar atılır."""
    if len(spec_probs) != len(odds) or len(odds) < 2:
        return True
    if abs(sum(p for _, p in spec_probs) - 1) >= 0.03:
        return True
    try:
        fair = devig(odds)
    except ValueError:
        return False
    tv = 0.5 * sum(abs(p - q) for (_, p), q in zip(spec_probs, fair))
    return tv <= 0.25


def choose(prob: dict[str, float], price: dict[str, float], conf: str,
           bankroll: float = BANKROLL) -> tuple[str, Candidate] | None:
    """Bir maçtaki değerli seçeneklerden büyümesi en yükseğini seçer. Yoksa None."""
    if conf not in settings.min_ev:
        return None
    groups = {
        "MS": ["MS 1", "MS X", "MS 2"],
        "OU": ["2.5 Üst", "2.5 Alt"],
    }
    best = None
    best_g = -1.0
    for names in groups.values():
        rows = []
        for name in names:
            o = price.get(name)
            p = prob.get(name)
            if o is None or p is None:
                continue
            rows.append((name, o, p))
        if len(rows) < 2:
            continue
        if not market_sane([(_SPECS[n], p) for n, _, p in rows], [o for _, o, _ in rows]):
            continue
        for name, o, p in rows:
            ev = p * o - 1
            c = Candidate(market=name, outcome=name, spec=_SPECS[name], odds=o, p=p, ev=ev,
                          conf=conf, margin=None, mbs=1)
            if not is_value(c):
                continue
            k, stake = stake_for(p, o, bankroll)
            c.kelly, c.stake = k, stake
            g = growth(p, o, min(k * settings.kelly_fraction, settings.max_stake_pct))
            if g > best_g + 1e-15:
                best_g, best = g, (name, c)
    return best


def _prices(row, cols: dict[str, str | None], mult: float = 1.0) -> dict[str, float]:
    out = {}
    mapping = {
        "MS 1": "b365h", "MS X": "b365d", "MS 2": "b365a",
        "2.5 Üst": "b365o", "2.5 Alt": "b365u",
    }
    for sel, key in mapping.items():
        col = cols.get(key)
        if not col:
            continue
        v = _num(row.get(col))
        if v is None:
            continue
        out[sel] = v * mult
    return out


def _close(row, cols: dict[str, str | None], sel: str) -> tuple[float | None, str]:
    pin = {"MS 1": "pshc", "MS X": "psdc", "MS 2": "psac", "2.5 Üst": "pco", "2.5 Alt": "pcu"}
    avg = {"MS 1": "avgc_h", "MS X": "avgc_d", "MS 2": "avgc_a", "2.5 Üst": "avgc_o", "2.5 Alt": "avgc_u"}
    for src, mp in (("pinnacle", pin), ("ortalama", avg)):
        col = cols.get(mp[sel])
        if not col:
            continue
        v = _num(row.get(col))
        if v is not None:
            return v, src
    return None, ""


def _triplet(row, cols, h, d, a, o, u):
    hv, dv, av = (_num(row.get(cols[h])) if cols.get(h) else None,
                  _num(row.get(cols[d])) if cols.get(d) else None,
                  _num(row.get(cols[a])) if cols.get(a) else None)
    if not (hv and dv and av):
        return None, None
    ou = None
    ov = _num(row.get(cols[o])) if cols.get(o) else None
    uv = _num(row.get(cols[u])) if cols.get(u) else None
    if ov and uv:
        ou = (2.5, ov, uv)
    return (hv, dv, av), ou


def _won(sel: str, hg: int, ag: int) -> bool:
    return {"MS 1": hg > ag, "MS X": hg == ag, "MS 2": hg < ag,
            "2.5 Üst": hg + ag > 2.5, "2.5 Alt": hg + ag < 2.5}[sel]


def _h1_share(train: pd.DataFrame) -> float:
    if "HTHG" not in train.columns or "HTAG" not in train.columns:
        return 0.44
    t = train.dropna(subset=["HTHG", "HTAG", "FTHG", "FTAG"])
    if len(t) < 80:
        return 0.44
    ft = t["FTHG"].sum() + t["FTAG"].sum()
    if ft <= 0:
        return 0.44
    return float(np.clip((t["HTHG"].sum() + t["HTAG"].sum()) / ft, 0.38, 0.50))


def _fit(train: pd.DataFrame, ref: datetime):
    if len(train) < 80:
        return None
    try:
        return dixon_coles.fit(
            train["HomeTeam"].tolist(), train["AwayTeam"].tolist(),
            train["FTHG"].astype(int).tolist(), train["FTAG"].astype(int).tolist(),
            [d.to_pydatetime() for d in train["Date"]], ref)
    except Exception:
        return None


def column_map(df: pd.DataFrame) -> dict[str, str | None]:
    return {
        "psh": _col(df, "PSH"), "psd": _col(df, "PSD"), "psa": _col(df, "PSA"),
        "po": _col(df, "P>2.5"), "pu": _col(df, "P<2.5"),
        "avgh": _col(df, "AvgH", "BbAvH"), "avgd": _col(df, "AvgD", "BbAvD"), "avga": _col(df, "AvgA", "BbAvA"),
        "avgo": _col(df, "Avg>2.5", "BbAv>2.5"), "avgu": _col(df, "Avg<2.5", "BbAv<2.5"),
        "b365h": _col(df, "B365H"), "b365d": _col(df, "B365D"), "b365a": _col(df, "B365A"),
        "b365o": _col(df, "B365>2.5"), "b365u": _col(df, "B365<2.5"),
        "pshc": _col(df, "PSCH"), "psdc": _col(df, "PSCD"), "psac": _col(df, "PSCA"),
        "pco": _col(df, "PC>2.5"), "pcu": _col(df, "PC<2.5"),
        "avgc_h": _col(df, "AvgCH"), "avgc_d": _col(df, "AvgCD"), "avgc_a": _col(df, "AvgCA"),
        "avgc_o": _col(df, "AvgC>2.5"), "avgc_u": _col(df, "AvgC<2.5"),
    }


def walk_league(frames: dict[int, pd.DataFrame], espn: str, name: str,
                seasons: tuple[int, ...] = TEST_SEASONS) -> tuple[list[VBet], list[VBet]]:
    """Bahisçi fiyatı ve aynı modelin iddaa benzeri (×0.92) fiyatı. Eğitim maç gününden öncedir."""
    hist = pd.concat([frames[y] for y in sorted(frames)], ignore_index=True)
    hist["Date"] = pd.to_datetime(hist["Date"])
    hist = hist.dropna(subset=["HomeTeam", "AwayTeam", "FTHG", "FTAG"]).sort_values("Date")
    raw: list[VBet] = []
    cut: list[VBet] = []
    w = _weight()
    for year in seasons:
        if year not in frames:
            continue
        test = frames[year].copy()
        test["Date"] = pd.to_datetime(test["Date"])
        test = test.dropna(subset=["HomeTeam", "AwayTeam", "FTHG", "FTAG"])
        if test.empty:
            continue
        cols = column_map(test)
        weeks = test["Date"].dt.to_period("W-MON")
        for wk in sorted(weeks.unique()):
            start = wk.start_time
            games = test[weeks == wk]
            train = hist[(hist["Date"] < start) & (hist["Date"] >= start - timedelta(days=730))]
            model = _fit(train, start.to_pydatetime())
            h1 = _h1_share(train) if model is not None else 0.44
            rho = model.rho if model is not None else -0.06
            prior = model.league_avg_goals if model is not None else 2.65
            for rd in games.to_dict("records"):
                pin = _triplet(rd, cols, "psh", "psd", "psa", "po", "pu")
                avg = _triplet(rd, cols, "avgh", "avgd", "avga", "avgo", "avgu")
                if pin[0]:
                    src, x12, ou = "pinnacle", pin[0], pin[1]
                elif avg[0]:
                    src, x12, ou = "ortalama", avg[0], avg[1]
                else:
                    continue
                try:
                    lam_mkt = market_lambdas(x12, ou, prior)
                except ValueError:
                    continue
                if not lam_mkt:
                    continue
                lam_dc = None
                if model is not None and rd["HomeTeam"] in model.attack and rd["AwayTeam"] in model.attack:
                    lam_dc = model.lambdas(rd["HomeTeam"], rd["AwayTeam"])
                lam = _blend(lam_mkt, lam_dc, w)
                conf = confidence(lam_mkt, lam_dc)
                gm = GoalModel(lam[0], lam[1], rho, h1)
                prob = {}
                for sel, spec in _SPECS.items():
                    p = selection_prob(spec, gm, None, None)
                    if p is not None:
                        prob[sel] = p
                price = _prices(rd, cols, 1.0)
                cheap = _prices(rd, cols, HAIRCUT)
                hg, ag = int(rd["FTHG"]), int(rd["FTAG"])
                when = pd.Timestamp(rd["Date"]).to_pydatetime()
                title = f"{rd['HomeTeam']} - {rd['AwayTeam']}"
                for dest, book in ((raw, price), (cut, cheap)):
                    picked = choose(prob, book, conf, BANKROLL)
                    if not picked:
                        continue
                    sel, c = picked
                    close, close_src = _close(rd, cols, sel)
                    dest.append(VBet(
                        season=year, league=espn, league_name=name, date=when, match=title,
                        market="MS" if sel.startswith("MS") else "2.5",
                        selection=sel, odds=c.odds, p=c.p, ev=c.ev, conf=c.conf, stake=c.stake,
                        won=_won(sel, hg, ag), close=close, close_src=close_src, model_src=src,
                    ))
    return raw, cut


def summarize(bets: list[VBet]) -> dict:
    n = len(bets)
    if n == 0:
        return {"n": 0, "hits": 0, "hit_rate": None, "flat_roi": None, "kelly_roi": None,
                "avg_odds": None, "avg_stake": None, "staked": 0, "clv": None, "clv_beat": None,
                "clv_n": 0, "losing_streak": 0}
    hits = sum(1 for b in bets if b.won)
    flat = sum(b.flat for b in bets)
    staked = sum(b.stake for b in bets)
    kelly = sum(b.kelly_pnl for b in bets)
    clvs = [b.clv for b in bets if b.clv is not None]
    streak = cur = 0
    for b in sorted(bets, key=lambda z: (z.date, z.league, z.match)):
        if b.won:
            cur = 0
        else:
            cur += 1
            streak = max(streak, cur)
    return {
        "n": n, "hits": hits, "hit_rate": hits / n, "flat_roi": flat / n,
        "kelly_roi": (kelly / staked) if staked else None,
        "avg_odds": sum(b.odds for b in bets) / n,
        "avg_stake": (staked / n) if n else None, "staked": staked,
        "clv": (sum(clvs) / len(clvs)) if clvs else None,
        "clv_beat": (sum(1 for v in clvs if v > 0) / len(clvs)) if clvs else None,
        "clv_n": len(clvs), "losing_streak": streak,
    }


def _by(bets: list[VBet], keyfn) -> list[dict]:
    groups = defaultdict(list)
    for b in bets:
        groups[keyfn(b)].append(b)
    rows = []
    for key in sorted(groups, key=lambda k: str(k)):
        rows.append({"key": key, **summarize(groups[key])})
    return rows


def build_report(raw: list[VBet], haircut_bets: list[VBet], meta: dict) -> dict:
    def block(bets):
        return {
            "all": summarize(bets),
            "tune": summarize([b for b in bets if b.season in TUNE]),
            "valid": summarize([b for b in bets if b.season in VALID]),
            "by_season": _by(bets, lambda b: b.season),
            "by_league": _by(bets, lambda b: b.league_name),
            "by_market": _by(bets, lambda b: b.selection),
            "by_conf": _by(bets, lambda b: b.conf),
            "model_src": _by(bets, lambda b: b.model_src),
            "close_src": _by([b for b in bets if b.close_src], lambda b: b.close_src),
        }
    return {"meta": meta, "book": block(raw), "iddaa": block(haircut_bets), "mbs": MBS_NOTE}


def _pct(x) -> str:
    if x is None:
        return "—"
    return f"%{x * 100:.1f}"


def _line(s: dict) -> str:
    return (f"n={s['n']} isabet={_pct(s['hit_rate'])} düz={_pct(s['flat_roi'])} "
            f"kelly={_pct(s['kelly_roi'])} oran={s['avg_odds'] and round(s['avg_odds'], 2)} "
            f"CLV={_pct(s['clv'])} yenen={_pct(s['clv_beat'])} clv_n={s['clv_n']} seri={s['losing_streak']}")


def format_report(rep: dict) -> str:
    lines = ["BAHISÇİ (Bet365 açılış)"]
    for name in ("all", "tune", "valid"):
        lines.append(f"  {name}: {_line(rep['book'][name])}")
    lines.append("SEZON")
    for row in rep["book"]["by_season"]:
        lines.append(f"  {row['key']}: {_line(row)}")
    lines.append("LİG")
    for row in rep["book"]["by_league"]:
        lines.append(f"  {row['key']}: {_line(row)}")
    lines.append("SEÇİM")
    for row in rep["book"]["by_market"]:
        lines.append(f"  {row['key']}: {_line(row)}")
    lines.append("GÜVEN")
    for row in rep["book"]["by_conf"]:
        lines.append(f"  {row['key']}: {_line(row)}")
    lines.append("MODEL")
    for row in rep["book"]["model_src"]:
        lines.append(f"  {row['key']}: n={row['n']}")
    lines.append("KAPANIŞ")
    for row in rep["book"]["close_src"]:
        lines.append(f"  {row['key']}: {_line(row)}")
    lines.append("IDDAA x0.92")
    for name in ("all", "tune", "valid"):
        lines.append(f"  {name}: {_line(rep['iddaa'][name])}")
    lines.append("IDDAA SEZON")
    for row in rep["iddaa"]["by_season"]:
        lines.append(f"  {row['key']}: {_line(row)}")
    lines.append("IDDAA SEÇİM")
    for row in rep["iddaa"]["by_market"]:
        lines.append(f"  {row['key']}: {_line(row)}")
    return "\n".join(lines)


async def load_frames(seasons: tuple[int, ...]) -> dict[str, dict[int, pd.DataFrame]]:
    need = tuple(sorted(set(seasons) | {min(seasons) - 1}))
    out: dict[str, dict[int, pd.DataFrame]] = {}
    for code, espn, name in LEAGUES:
        out[espn] = {}
        for year in need:
            df = await footballdata.fetch_season(code, year)
            if df is None or df.empty:
                print(f"  eksik {name} {year}", flush=True)
                continue
            out[espn][year] = df
            print(f"  {name} {year}: {len(df)} maç", flush=True)
    return out


def run_loaded(frames: dict[str, dict[int, pd.DataFrame]], seasons: tuple[int, ...] = TEST_SEASONS) -> dict:
    raw, cut = [], []
    for _code, espn, name in LEAGUES:
        print(f"  hesap {name}", flush=True)
        a, b = walk_league(frames[espn], espn, name, seasons)
        raw.extend(a)
        cut.extend(b)
    meta = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "w_dc": _weight(),
        "min_ev": dict(settings.min_ev),
        "min_odds": settings.min_odds, "max_odds": settings.max_odds,
        "kelly": settings.kelly_fraction, "max_stake_pct": settings.max_stake_pct,
        "bankroll": BANKROLL,
        "note": "Eşikler sonuç görüldükten sonra değiştirilmedi.",
    }
    return build_report(raw, cut, meta)


async def amain(out: str) -> dict:
    print("Dosyalar...", flush=True)
    frames = await load_frames(TEST_SEASONS)
    print("Değer motoru...", flush=True)
    rep = await asyncio.to_thread(run_loaded, frames, TEST_SEASONS)
    text = format_report(rep)
    print(text, flush=True)
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False)
        print(f"JSON: {out}", flush=True)
    return rep


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Değer motorunun geçmiş testi")
    p.add_argument("--out", default="")
    args = p.parse_args(argv)
    asyncio.run(amain(args.out))


if __name__ == "__main__":
    main()
