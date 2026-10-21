"""football-data.co.uk: geçmiş maçlar, istatistikler ve oranlar.

Buradan kurulanlar:
- Gol modeli (Dixon-Coles, zaman ağırlıklı, 2 sezon)
- Korner modeli ve kart modeli (takım bazlı Poisson + aşırı dağılım katsayısı)
- İlk yarı gol payı, hakem kart eğilimi (veri varsa)
- Sezon istatistikleri ve puan durumu

Site kişisel kullanım içindir; dosyalar günde en fazla bir kez indirilir.
"""
from __future__ import annotations

import asyncio
import io
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from . import dixon_coles
from .config import LEAGUES, TZ
from .http import get_text
from .markets import CountModel

log = logging.getLogger(__name__)
BASE = "https://www.football-data.co.uk/mmz4281/{season}/{code}.csv"
DAY = 24 * 3600


def season_code(start_year: int) -> str:
    return f"{start_year % 100:02d}{(start_year + 1) % 100:02d}"


def current_start_year(now: datetime | None = None) -> int:
    now = now or datetime.now(TZ)
    return now.year if now.month >= 7 else now.year - 1


def parse_csv(text: str) -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(text), encoding_errors="replace", on_bad_lines="skip")
    df.columns = [str(c).strip().lstrip("﻿") for c in df.columns]
    df = df.dropna(subset=["HomeTeam", "AwayTeam", "FTHG", "FTAG"])
    df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce")
    df = df.dropna(subset=["Date"]).copy()
    for c in ("FTHG", "FTAG"):
        df[c] = df[c].astype(int)
    df["HomeTeam"] = df["HomeTeam"].astype(str).str.strip()
    df["AwayTeam"] = df["AwayTeam"].astype(str).str.strip()
    return df.sort_values("Date").reset_index(drop=True)


async def fetch_season(code: str, start_year: int, ttl: float = DAY) -> pd.DataFrame | None:
    url = BASE.format(season=season_code(start_year), code=code)
    try:
        df = parse_csv(await get_text(url, ttl=ttl))
        df["Season"] = start_year
        return df
    except Exception as e:  # noqa: BLE001
        log.warning("football-data %s %s alınamadı: %s", code, season_code(start_year), e)
        return None


def _has(df, *cols) -> bool:
    return all(c in df.columns for c in cols) and df[list(cols)].notna().all(axis=1).sum() >= 60


def _fit_counts(df: pd.DataFrame, hc: str, ac: str, ref: datetime):
    d = df.dropna(subset=[hc, ac])
    if len(d) < 60:
        return None
    m = dixon_coles.fit(d["HomeTeam"].tolist(), d["AwayTeam"].tolist(),
                        d[hc].astype(int).tolist(), d[ac].astype(int).tolist(),
                        [x.to_pydatetime() for x in d["Date"]], ref)
    lh = np.array([m.lambdas(h, a)[0] for h, a in zip(d["HomeTeam"], d["AwayTeam"])])
    la = np.array([m.lambdas(h, a)[1] for h, a in zip(d["HomeTeam"], d["AwayTeam"])])
    x, y = d[hc].to_numpy(float), d[ac].to_numpy(float)
    disp_team = float(np.clip(np.mean(np.r_[(x - lh) ** 2 / lh, (y - la) ** 2 / la]), 1.0, 3.0))
    disp_tot = float(np.clip(np.mean((x + y - lh - la) ** 2 / (lh + la)), 1.0, 3.0))
    return m, disp_team, disp_tot


@dataclass
class LeagueData:
    code: str
    df_all: pd.DataFrame
    df_cur: pd.DataFrame
    model: dixon_coles.DCModel | None
    corners: tuple | None = None     # (model, disp_team, disp_total)
    cards: tuple | None = None
    h1_share: float = 0.44
    ref_cards: dict = field(default_factory=dict)  # hakem -> kart çarpanı
    sot_model: dixon_coles.DCModel | None = None   # isabetli şut gücü (xG yerine)
    sot_conv: float = 0.30                         # isabetli şutun gole dönüşme oranı

    @property
    def teams(self) -> list[str]:
        t = set(self.df_cur["HomeTeam"]) | set(self.df_cur["AwayTeam"])
        return sorted(t) if t else (self.model.teams if self.model else [])

    def goal_lambdas(self, home: str, away: str, kind: str | None = None):
        """İstatistik modelinin beklenen golleri. kind: gol / sut / karma (varsayılan: ayar)."""
        from .config import settings
        kind = kind or settings.stat_model
        g = s = None
        if self.model and home in self.model.attack and away in self.model.attack:
            g = self.model.lambdas(home, away)
        if self.sot_model and home in self.sot_model.attack and away in self.sot_model.attack:
            sh, sa = self.sot_model.lambdas(home, away)
            s = (sh * self.sot_conv, sa * self.sot_conv)
        return combine_stat(g, s, kind)

    def count_model(self, kind: str, home: str, away: str, referee: str | None = None) -> CountModel | None:
        fitted = self.corners if kind == "corners" else self.cards
        if not fitted:
            return None
        m, dt, dtot = fitted
        if home not in m.attack or away not in m.attack:
            return None
        # takımın bu sezon en az 5 maçı yoksa güvenilmez
        n_h = ((self.df_cur["HomeTeam"] == home) | (self.df_cur["AwayTeam"] == home)).sum()
        n_a = ((self.df_cur["HomeTeam"] == away) | (self.df_cur["AwayTeam"] == away)).sum()
        if min(n_h, n_a) < 5:
            return None
        mh, ma = m.lambdas(home, away)
        if kind == "cards" and referee:
            f = self.referee_factor(referee)
            mh, ma = mh * f, ma * f
        return CountModel(mh=mh, ma=ma, disp_team=dt, disp_total=dtot,
                          h1_share=0.46 if kind == "corners" else 0.40)

    def referee_factor(self, name: str) -> float:
        if not self.ref_cards or not name:
            return 1.0
        parts = name.replace(".", " ").split()
        if not parts:
            return 1.0
        last, ini = parts[-1].lower(), parts[0][0].lower()
        for k, v in self.ref_cards.items():
            kp = k.replace(".", " ").split()
            if kp and kp[-1].lower() == last and kp[0][0].lower() == ini:
                return v
        return 1.0

    def team_stats(self, team: str, venue: str | None = None) -> dict:
        """Bu sezon maç başı ortalamalar. venue: 'home' / 'away' / None."""
        d = self.df_cur
        h = d[d["HomeTeam"] == team] if venue in (None, "home") else d.iloc[0:0]
        a = d[d["AwayTeam"] == team] if venue in (None, "away") else d.iloc[0:0]
        n = len(h) + len(a)
        if n == 0:
            return {}

        def avg(hc, ac):
            if hc not in d.columns or ac not in d.columns:
                return None
            v = pd.concat([h[hc], a[ac]]).dropna()
            return round(float(v.mean()), 2) if len(v) else None

        res = {
            "mac": n, "atilan": avg("FTHG", "FTAG"), "yenen": avg("FTAG", "FTHG"),
            "iy_atilan": avg("HTHG", "HTAG"), "iy_yenen": avg("HTAG", "HTHG"),
            "sut": avg("HS", "AS"), "sut_yenen": avg("AS", "HS"),
            "isabetli": avg("HST", "AST"), "isabetli_yenen": avg("AST", "HST"),
            "korner": avg("HC", "AC"), "korner_yenen": avg("AC", "HC"),
            "faul": avg("HF", "AF"), "sari": avg("HY", "AY"), "kirmizi": avg("HR", "AR"),
        }
        tot = pd.concat([h["FTHG"] + h["FTAG"], a["FTHG"] + a["FTAG"]])
        res["ust25"] = round(float((tot > 2.5).mean()), 2)
        res["kg"] = round(float(pd.concat([(h["FTHG"] > 0) & (h["FTAG"] > 0),
                                           (a["FTHG"] > 0) & (a["FTAG"] > 0)]).mean()), 2)
        res["gol_yemedi"] = round(float(pd.concat([h["FTAG"] == 0, a["FTHG"] == 0]).mean()), 2)
        w = (h["FTHG"] > h["FTAG"]).sum() + (a["FTAG"] > a["FTHG"]).sum()
        dr = (h["FTHG"] == h["FTAG"]).sum() + (a["FTAG"] == a["FTHG"]).sum()
        res["G"], res["B"], res["M"] = int(w), int(dr), int(n - w - dr)
        return res

    def table(self) -> list[tuple[str, int, int, int]]:
        rows: dict[str, list[int]] = {}
        for r in self.df_cur.itertuples():
            for t, gf, ga in ((r.HomeTeam, r.FTHG, r.FTAG), (r.AwayTeam, r.FTAG, r.FTHG)):
                x = rows.setdefault(t, [0, 0, 0])
                x[0] += 1
                x[1] += gf - ga
                x[2] += 3 if gf > ga else (1 if gf == ga else 0)
        return sorted(((t, *v) for t, v in rows.items()), key=lambda z: (-z[3], -z[2]))

    def position(self, team: str) -> tuple[int, int] | None:
        t = self.table()
        for k, row in enumerate(t, 1):
            if row[0] == team:
                return k, row[3]
        return None

    def h2h(self, home: str, away: str, n: int = 6) -> list[str]:
        d = self.df_all
        m = d[((d.HomeTeam == home) & (d.AwayTeam == away)) | ((d.HomeTeam == away) & (d.AwayTeam == home))]
        return [f"{r.Date:%d.%m.%y} {r.HomeTeam} {r.FTHG}-{r.FTAG} {r.AwayTeam}"
                for r in m.sort_values("Date", ascending=False).head(n).itertuples()]


def combine_stat(g, s, kind: str):
    if kind == "sut":
        return s or g
    if kind == "karma" and g and s:
        return (float(np.sqrt(g[0] * s[0])), float(np.sqrt(g[1] * s[1])))
    return g or (s if kind != "gol" else None)


def fit_sot(df: pd.DataFrame, ref: datetime):
    """İsabetli şut modeli + dönüşüm oranı. Gol, şansa çok bağlıdır; isabetli şut daha istikrarlıdır."""
    d = df.dropna(subset=["HST", "AST"])
    if len(d) < 60:
        return None, 0.30
    m = dixon_coles.fit(d["HomeTeam"].tolist(), d["AwayTeam"].tolist(),
                        d["HST"].astype(int).tolist(), d["AST"].astype(int).tolist(),
                        [x.to_pydatetime() for x in d["Date"]], ref)
    shots = d["HST"].sum() + d["AST"].sum()
    conv = float((d["FTHG"].sum() + d["FTAG"].sum()) / shots) if shots else 0.30
    return m, float(np.clip(conv, 0.15, 0.5))


def build_league(code: str, df_all: pd.DataFrame, df_cur: pd.DataFrame,
                 ref_date: datetime) -> LeagueData:
    """Verilen maçlardan (ref_date öncesi) tüm modelleri kurar — canlı ve backtest ortak."""
    model = None
    if len(df_all) >= 60:
        model = dixon_coles.fit(df_all["HomeTeam"].tolist(), df_all["AwayTeam"].tolist(),
                                df_all["FTHG"].tolist(), df_all["FTAG"].tolist(),
                                [d.to_pydatetime() for d in df_all["Date"]], ref_date)
    ld = LeagueData(code=code, df_all=df_all, df_cur=df_cur, model=model)
    if _has(df_all, "HTHG", "HTAG"):
        ht = df_all["HTHG"].fillna(0).sum() + df_all["HTAG"].fillna(0).sum()
        ft = df_all["FTHG"].sum() + df_all["FTAG"].sum()
        if ft > 0:
            ld.h1_share = float(np.clip(ht / ft, 0.38, 0.50))
    if _has(df_all, "HST", "AST"):
        ld.sot_model, ld.sot_conv = fit_sot(df_all, ref_date)
    if _has(df_all, "HC", "AC"):
        ld.corners = _fit_counts(df_all, "HC", "AC", ref_date)
    if _has(df_all, "HY", "AY"):
        d = df_all.copy()
        d["HK"] = d["HY"].fillna(0) + d.get("HR", 0).fillna(0) if "HR" in d else d["HY"]
        d["AK"] = d["AY"].fillna(0) + d.get("AR", 0).fillna(0) if "AR" in d else d["AY"]
        ld.cards = _fit_counts(d, "HK", "AK", ref_date)
        if "Referee" in d.columns and d["Referee"].notna().sum() > 100:
            tot = d["HK"] + d["AK"]
            mean = tot.mean()
            g = d.assign(T=tot).groupby("Referee")["T"].agg(["sum", "count"])
            k = 8.0  # küçük örneklemi lig ortalamasına çek
            ld.ref_cards = {str(r): float(np.clip(((s + k * mean) / (c + k)) / mean, 0.75, 1.30))
                            for r, (s, c) in g.iterrows()}
    return ld


_cache: dict[str, LeagueData] = {}
_loaded_at: dict[str, float] = {}


async def load_league(code: str) -> LeagueData | None:
    if code in _cache and time.time() - _loaded_at[code] < DAY / 2:
        return _cache[code]
    y = current_start_year()
    prev, cur = await fetch_season(code, y - 1), await fetch_season(code, y)
    frames = [f for f in (prev, cur) if f is not None]
    if not frames:
        return _cache.get(code)
    df_all = pd.concat(frames, ignore_index=True).sort_values("Date").reset_index(drop=True)
    df_cur = cur if cur is not None else df_all.iloc[0:0]
    ld = await asyncio.to_thread(build_league, code, df_all, df_cur, datetime.now())
    _cache[code] = ld
    _loaded_at[code] = time.time()
    return ld


async def load_all() -> dict[str, LeagueData]:
    out = {}
    for _, (_, code) in LEAGUES.items():
        if code:
            ld = await load_league(code)
            if ld:
                out[code] = ld
    return out
