"""Zaman ağırlıklı Dixon-Coles modeli (lig içi takım hücum/savunma gücü)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
from scipy.optimize import minimize, minimize_scalar
from scipy.special import gammaln

from .markets import dc_tau  # noqa: F401  (dışarıya da sunulur)

HALF_LIFE_DAYS = 240.0
RIDGE = 0.8  # az maçlı (yeni çıkan) takımları lig ortalamasına çeker


@dataclass
class DCModel:
    teams: list[str]
    attack: dict[str, float]
    defence: dict[str, float]
    home_adv: float
    intercept: float
    rho: float
    n_matches: int
    league_avg_goals: float

    def lambdas(self, home: str, away: str) -> tuple[float, float]:
        lh = np.exp(self.intercept + self.home_adv + self.attack[home] + self.defence[away])
        la = np.exp(self.intercept + self.attack[away] + self.defence[home])
        return float(lh), float(la)


def fit(home: list[str], away: list[str], hg: list[int], ag: list[int],
        dates: list[datetime], ref_date: datetime | None = None) -> DCModel:
    teams = sorted(set(home) | set(away))
    idx = {t: k for k, t in enumerate(teams)}
    n = len(teams)
    hi = np.array([idx[t] for t in home])
    ai = np.array([idx[t] for t in away])
    x = np.asarray(hg, float)
    y = np.asarray(ag, float)
    ref = ref_date or max(dates)
    age = np.array([(ref - d).days for d in dates], float)
    w = np.power(0.5, np.clip(age, 0, None) / HALF_LIFE_DAYS)

    def unpack(p):
        return p[:n], p[n:2 * n], p[2 * n], p[2 * n + 1]

    def nll(p):
        att, de, mu, h = unpack(p)
        llh = mu + h + att[hi] + de[ai]
        lla = mu + att[ai] + de[hi]
        lh, la = np.exp(llh), np.exp(lla)
        ll = w * (x * llh - lh + y * lla - la)
        f = -ll.sum() + RIDGE * (att @ att + de @ de)
        # analitik gradyan
        rh = w * (x - lh)
        ra = w * (y - la)
        g_att = -(np.bincount(hi, rh, n) + np.bincount(ai, ra, n)) + 2 * RIDGE * att
        g_de = -(np.bincount(ai, rh, n) + np.bincount(hi, ra, n)) + 2 * RIDGE * de
        g_mu = -(rh.sum() + ra.sum())
        g_h = -rh.sum()
        return f, np.concatenate([g_att, g_de, [g_mu, g_h]])

    p0 = np.zeros(2 * n + 2)
    p0[-2] = 0.1
    p0[-1] = 0.25
    res = minimize(nll, p0, jac=True, method="L-BFGS-B")
    att, de, mu, h = unpack(res.x)
    lh_all = np.exp(mu + h + att[hi] + de[ai])
    la_all = np.exp(mu + att[ai] + de[hi])

    def rho_nll(r):
        tau = np.ones_like(x)
        m00 = (x == 0) & (y == 0)
        m01 = (x == 0) & (y == 1)
        m10 = (x == 1) & (y == 0)
        m11 = (x == 1) & (y == 1)
        tau[m00] = 1 - lh_all[m00] * la_all[m00] * r
        tau[m01] = 1 + lh_all[m01] * r
        tau[m10] = 1 + la_all[m10] * r
        tau[m11] = 1 - r
        if np.any(tau <= 0):
            return 1e12
        return -(w * np.log(tau)).sum()

    rho = float(minimize_scalar(rho_nll, bounds=(-0.25, 0.15), method="bounded").x)
    return DCModel(
        teams=teams,
        attack={t: float(att[idx[t]]) for t in teams},
        defence={t: float(de[idx[t]]) for t in teams},
        home_adv=float(h),
        intercept=float(mu),
        rho=rho,
        n_matches=len(x),
        league_avg_goals=float((x + y).mean()) if len(x) else 2.6,
    )


def log_likelihood(model: DCModel, home, away, hg, ag) -> float:
    """Test amaçlı: Poisson log-olabilirlik."""
    tot = 0.0
    for h, a, x, y in zip(home, away, hg, ag):
        lh, la = model.lambdas(h, a)
        tot += x * np.log(lh) - lh - gammaln(x + 1) + y * np.log(la) - la - gammaln(y + 1)
    return float(tot)
