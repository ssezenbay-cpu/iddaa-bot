"""Olasılık motoru: gol (devre devre), korner ve kart dağılımlarından her bahis seçeneğinin olasılığı.

Gol modeli: her devre için Poisson, tam maç skoruna Dixon-Coles düzeltmesi uygulanmış ortak dağılım
J[h1, a1, h2, a2]. Her bahis seçeneği bu dört değişken üzerinde bir koşul (maske) olarak yazılır,
böylece MS, İY/MS, MS+KG, handikap, takım golleri, skor vb. tek bir tutarlı modelden çıkar.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.optimize import brentq, minimize
from scipy.stats import nbinom, poisson

MAX_GOALS = 10          # tam maç matrisi (piyasa uydurma için)
HALF_MAX = 8            # devre başına en fazla gol
DEFAULT_RHO = -0.06
DEFAULT_H1_SHARE = 0.44  # gollerin ilk yarıya düşen payı (ligden ölçülür)


# ----------------------------------------------------------------- yardımcılar
def dc_tau(lh: float, la: float, rho: float, size: int = MAX_GOALS + 1) -> np.ndarray:
    t = np.ones((size, size))
    t[0, 0] = max(1 - lh * la * rho, 1e-6)
    t[0, 1] = max(1 + lh * rho, 1e-6)
    t[1, 0] = max(1 + la * rho, 1e-6)
    t[1, 1] = max(1 - rho, 1e-6)
    return t


def score_matrix(lh: float, la: float, rho: float = DEFAULT_RHO) -> np.ndarray:
    g = np.arange(MAX_GOALS + 1)
    m = np.outer(poisson.pmf(g, lh), poisson.pmf(g, la)) * dc_tau(lh, la, rho)
    return m / m.sum()


def basic_probs(m: np.ndarray) -> dict[str, float]:
    """Tam maç matrisinden temel olasılıklar (piyasa uydurma ve hızlı özet için)."""
    i, j = np.indices(m.shape)
    out = {"1": float(m[i > j].sum()), "X": float(np.trace(m)), "2": float(m[i < j].sum())}
    for line in (0.5, 1.5, 2.5, 3.5, 4.5):
        out[f"Ü{line}"] = float(m[(i + j) > line].sum())
    out["KGV"] = float(m[(i >= 1) & (j >= 1)].sum())
    return out


def devig(odds: list[float]) -> list[float]:
    """Power yöntemiyle marjı temizlenmiş olasılıklar."""
    inv = [1.0 / o for o in odds]
    s = sum(inv)
    if s <= 1.0:
        return [x / s for x in inv]
    k = brentq(lambda k: sum(x ** k for x in inv) - 1.0, 1.0, 20.0)
    return [x ** k for x in inv]


def overround(odds: list[float]) -> float:
    return sum(1.0 / o for o in odds) - 1.0


def fit_lambdas(p1x2, ou=None, prior_total: float = 2.65):
    """Piyasa olasılıklarına (1X2 + varsa (çizgi, üst olasılığı)) uyan beklenen goller."""
    if p1x2 is None:
        return None
    t1, tx, t2 = p1x2

    def loss(x):
        lh, la = math.exp(x[0]), math.exp(x[1])
        mp = basic_probs(score_matrix(lh, la))
        e = (mp["1"] - t1) ** 2 + (mp["X"] - tx) ** 2 + (mp["2"] - t2) ** 2
        if ou is not None and f"Ü{ou[0]}" in mp:
            e += 2.0 * (mp[f"Ü{ou[0]}"] - ou[1]) ** 2
        else:
            e += 0.002 * (lh + la - prior_total) ** 2
        return e

    res = minimize(loss, x0=[math.log(1.4), math.log(1.1)], method="Nelder-Mead",
                   options={"xatol": 1e-4, "fatol": 1e-10, "maxiter": 500})
    return (min(max(math.exp(res.x[0]), 0.05), 6.0), min(max(math.exp(res.x[1]), 0.05), 6.0))


def american_to_decimal(v) -> float | None:
    if v is None:
        return None
    s = str(v).strip().upper()
    if s in ("EVEN", "EV", "PK"):
        return 2.0
    try:
        a = float(s.replace("+", ""))
    except ValueError:
        return None
    if a == 0:
        return None
    if abs(a) < 100:
        return a if a > 1 else None
    return 1 + a / 100 if a > 0 else 1 + 100 / abs(a)


def nb_dist(mean: float, disp: float):
    """Ortalama ve dağılım katsayısı (varyans/ortalama) ile sayım dağılımı."""
    mean = max(mean, 1e-6)
    if disp <= 1.02:
        return poisson(mean)
    n = mean / (disp - 1.0)
    return nbinom(n, 1.0 / disp)


# ----------------------------------------------------------------- gol modeli
class GoalModel:
    def __init__(self, lh: float, la: float, rho: float = DEFAULT_RHO,
                 h1_share: float = DEFAULT_H1_SHARE):
        self.lh, self.la, self.rho, self.h1 = lh, la, rho, h1_share
        g = np.arange(HALF_MAX + 1)
        ph1, pa1 = poisson.pmf(g, lh * h1_share), poisson.pmf(g, la * h1_share)
        ph2, pa2 = poisson.pmf(g, lh * (1 - h1_share)), poisson.pmf(g, la * (1 - h1_share))
        J = np.einsum("a,b,c,d->abcd", ph1, pa1, ph2, pa2)
        h1, a1, h2, a2 = np.indices(J.shape)
        H, A = h1 + h2, a1 + a2
        tau = np.ones_like(J)
        tau[(H == 0) & (A == 0)] = max(1 - lh * la * rho, 1e-6)
        tau[(H == 0) & (A == 1)] = max(1 + lh * rho, 1e-6)
        tau[(H == 1) & (A == 0)] = max(1 + la * rho, 1e-6)
        tau[(H == 1) & (A == 1)] = max(1 - rho, 1e-6)
        J = J * tau
        self.J = J / J.sum()
        self.v = {"h1": h1, "a1": a1, "h2": h2, "a2": a2, "H": H, "A": A}

    def p(self, mask: np.ndarray) -> float:
        return float(self.J[mask].sum())

    def period(self, per: str):
        v = self.v
        if per == "H1":
            return v["h1"], v["a1"]
        if per == "H2":
            return v["h2"], v["a2"]
        return v["H"], v["A"]

    def summary(self) -> dict[str, float]:
        H, A = self.v["H"], self.v["A"]
        return {"1": self.p(H > A), "X": self.p(H == A), "2": self.p(H < A),
                "Ü2.5": self.p(H + A > 2.5), "KGV": self.p((H > 0) & (A > 0))}

    def top_scores(self, n: int = 3) -> list[tuple[str, float]]:
        H, A = self.v["H"], self.v["A"]
        res = []
        for x in range(6):
            for y in range(6):
                res.append((f"{x}-{y}", self.p((H == x) & (A == y))))
        return sorted(res, key=lambda z: -z[1])[:n]


@dataclass
class CountModel:
    """Korner veya kart için ev/deplasman beklenen sayı ve dağılım katsayıları."""
    mh: float
    ma: float
    disp_team: float = 1.3
    disp_total: float = 1.3
    h1_share: float = 0.46

    def total(self, per: str = "FT"):
        f = self.h1_share if per == "H1" else (1 - self.h1_share if per == "H2" else 1.0)
        return nb_dist((self.mh + self.ma) * f, self.disp_total)

    def team(self, side: str, per: str = "FT"):
        f = self.h1_share if per == "H1" else (1 - self.h1_share if per == "H2" else 1.0)
        return nb_dist((self.mh if side == "home" else self.ma) * f, self.disp_team)

    def p_over(self, line: float, scope: str = "total", per: str = "FT") -> float:
        d = self.total(per) if scope == "total" else self.team(scope, per)
        return float(d.sf(math.floor(line)))  # P(X > line) — buçuklu çizgiler

    def p_1x2(self, sel: str, per: str = "FT") -> float:
        k = np.arange(0, 40)
        ph, pa = self.team("home", per).pmf(k), self.team("away", per).pmf(k)
        M = np.outer(ph, pa)
        i, j = np.indices(M.shape)
        return float(M[{"1": i > j, "X": i == j, "2": i < j}[sel]].sum())


# ----------------------------------------------------------------- seçim tanımları
RES = {"1": "1", "0": "X", "X": "X", "x": "X", "2": "2"}


def res_mask(h, a, sel: str):
    return {"1": h > a, "X": h == a, "2": h < a}[sel]


def selection_prob(spec: dict, gm: GoalModel | None, corners: CountModel | None,
                   cards: CountModel | None) -> float | None:
    """spec: classify_outcome() çıktısı. Modellenemiyorsa None."""
    kind = spec["kind"]
    subj = spec.get("subject", "goals")
    per = spec.get("period", "FT")
    if subj in ("corners", "cards"):
        cm = corners if subj == "corners" else cards
        if cm is None:
            return None
        if kind == "ou":
            p = cm.p_over(spec["line"], spec.get("scope", "total"), per)
            return p if spec["side"] == "over" else 1 - p
        if kind == "1x2":
            return cm.p_1x2(spec["sel"], per)
        return None
    if gm is None:
        return None
    h, a = gm.period(per)
    v = gm.v
    if kind == "1x2":
        return gm.p(res_mask(h, a, spec["sel"]))
    if kind == "dc":
        s1, s2 = spec["sel"]
        return gm.p(res_mask(h, a, s1) | res_mask(h, a, s2))
    if kind == "ou":
        scope = spec.get("scope", "total")
        g = h + a if scope == "total" else (h if scope == "home" else a)
        over = gm.p(g > spec["line"])
        return over if spec["side"] == "over" else 1 - over
    if kind == "btts":
        p = gm.p((h > 0) & (a > 0))
        return p if spec["yes"] else 1 - p
    if kind == "oddeven":
        p = gm.p((h + a) % 2 == 1)
        return p if spec["odd"] else 1 - p
    if kind == "range":
        lo, hi = spec["lo"], spec["hi"]
        return gm.p((h + a >= lo) & (h + a <= hi))
    if kind == "score":
        return gm.p((h == spec["x"]) & (a == spec["y"]))
    if kind == "handicap":
        return gm.p(res_mask(h + spec["hh"], a + spec["ha"], spec["sel"]))
    if kind == "htft":
        return gm.p(res_mask(v["h1"], v["a1"], spec["ht"]) & res_mask(v["H"], v["A"], spec["ft"]))
    if kind == "combo":
        m = None
        for part in spec["parts"]:
            pm = _part_mask(gm, part)
            if pm is None:
                return None
            m = pm if m is None else (m & pm)
        return gm.p(m)
    if kind == "half_more":
        g1, g2 = v["h1"] + v["a1"], v["h2"] + v["a2"]
        return gm.p({"1": g1 > g2, "2": g2 > g1, "X": g1 == g2}[spec["sel"]])
    if kind == "win_both":
        if spec["side"] == "home":
            m = (v["h1"] > v["a1"]) & (v["h2"] > v["a2"])
        else:
            m = (v["a1"] > v["h1"]) & (v["a2"] > v["h2"])
        p = gm.p(m)
        return p if spec["yes"] else 1 - p
    if kind == "win_to_nil":
        m = (v["H"] > v["A"]) & (v["A"] == 0) if spec["side"] == "home" else (v["A"] > v["H"]) & (v["H"] == 0)
        p = gm.p(m)
        return p if spec["yes"] else 1 - p
    if kind == "first_goal":
        p00 = gm.p((v["H"] == 0) & (v["A"] == 0))
        if spec["sel"] == "none":
            return p00
        share = gm.lh / (gm.lh + gm.la)
        return (1 - p00) * (share if spec["sel"] == "home" else 1 - share)
    return None


def _part_mask(gm: GoalModel, part: dict):
    h, a = gm.period(part.get("period", "FT"))
    k = part["kind"]
    if k == "1x2":
        return res_mask(h, a, part["sel"])
    if k == "dc":
        s1, s2 = part["sel"]
        return res_mask(h, a, s1) | res_mask(h, a, s2)
    if k == "ou":
        g = h + a
        return (g > part["line"]) if part["side"] == "over" else (g < part["line"])
    if k == "btts":
        m = (h > 0) & (a > 0)
        return m if part["yes"] else ~m
    return None
