import os
import sys
import tempfile
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="iddaabot_test_"))
# Testler sunucudaki .env ayarlarından (eşikler, anahtarlar) etkilenmesin
for _k, _v in {"W_MARKET": "0.6", "W_DC": "0.4", "MIN_EV_HIGH": "0.03", "MIN_EV_MID": "0.05",
               "MIN_EV_LOW": "0.08", "MAX_EV": "0.25", "MIN_ODDS": "1.40", "MAX_ODDS": "4.00",
               "MIN_PROB": "0.30", "KELLY_FRACTION": "0.25", "MAX_STAKE_PCT": "0.03", "MIN_STAKE": "10",
               "BANKROLL": "1000", "STAT_MODEL": "gol", "CORE_ONLY": "1", "ALLOW_COUNT_MARKETS": "0",
               "EXTRA_LEAGUES": "1", "GEMINI_API_KEY": "", "ALLOWED_CHAT_ID": "0",
               "VALUE_PICKS": "1", "STAT_COUPONS": "1", "DAILY_COUPON": "1", "KUPON_MODU": "eski"}.items():
    os.environ[_k] = _v

TEAMS = ["Galatasaray", "Fenerbahce", "Besiktas", "Trabzonspor", "Buyuksehyr", "Kasimpasa",
         "Samsunspor", "Goztep", "Eyupspor", "Konyaspor", "Alanyaspor", "Kayserispor",
         "Antalyaspor", "Rizespor", "Gaziantep", "Kocaelispor", "Genclerbirligi", "Karagumruk"]


def simulate_league(seed=1, seasons=(2023, 2024, 2025), with_odds=True):
    """Bilinen güçlerle sentetik lig: football-data.co.uk sütun yapısında."""
    rng = np.random.default_rng(seed)
    n = len(TEAMS)
    att = rng.normal(0, 0.30, n)
    att -= att.mean()
    de = rng.normal(0, 0.25, n)
    de -= de.mean()
    mu, home = 0.12, 0.28
    catt, cdef = rng.normal(0, 0.15, n), rng.normal(0, 0.12, n)
    from iddaabot.markets import score_matrix
    frames = {}
    for s in seasons:
        rows = []
        day = datetime(s, 8, 10)
        pairs = [(i, j) for i in range(n) for j in range(n) if i != j]
        rng.shuffle(pairs)
        for k, (i, j) in enumerate(pairs):
            d = day + timedelta(days=7 * (k // 9))
            lh = np.exp(mu + home + att[i] + de[j])
            la = np.exp(mu + att[j] + de[i])
            h1h, h1a = rng.poisson(lh * 0.44), rng.poisson(la * 0.44)
            hg, ag = h1h + rng.poisson(lh * 0.56), h1a + rng.poisson(la * 0.56)
            hc = rng.poisson(np.exp(1.65 + 0.1 + catt[i] + cdef[j]))
            ac = rng.poisson(np.exp(1.65 + catt[j] + cdef[i]))
            row = dict(Div="T1", Date=d.strftime("%d/%m/%Y"), HomeTeam=TEAMS[i], AwayTeam=TEAMS[j],
                       FTHG=hg, FTAG=ag, HTHG=h1h, HTAG=h1a, HS=rng.poisson(13), AS=rng.poisson(10),
                       HST=rng.poisson(5), AST=rng.poisson(4), HC=hc, AC=ac, HF=rng.poisson(12),
                       AF=rng.poisson(13), HY=rng.poisson(2), AY=rng.poisson(2.3), HR=rng.binomial(1, .05),
                       AR=rng.binomial(1, .06))
            if with_odds:
                m = score_matrix(lh, la)
                ii, jj = np.indices(m.shape)
                p = np.array([m[ii > jj].sum(), np.trace(m), m[ii < jj].sum()])
                po = m[(ii + jj) > 2.5].sum()
                noisy = np.clip(p * np.exp(rng.normal(0, 0.04, 3)), 0.02, None)
                noisy /= noisy.sum()
                row.update({"AvgH": 1 / (noisy[0] * 1.06), "AvgD": 1 / (noisy[1] * 1.06),
                            "AvgA": 1 / (noisy[2] * 1.06), "Avg>2.5": 1 / (po * 1.05),
                            "Avg<2.5": 1 / ((1 - po) * 1.05)})
                b = np.clip(p * np.exp(rng.normal(0, 0.05, 3)), 0.02, None)
                b /= b.sum()
                row.update({"B365H": round(1 / (b[0] * 1.05), 2), "B365D": round(1 / (b[1] * 1.05), 2),
                            "B365A": round(1 / (b[2] * 1.05), 2), "B365>2.5": round(1 / (po * 1.05), 2),
                            "B365<2.5": round(1 / ((1 - po) * 1.05), 2),
                            "B365CH": round(1 / (p[0] * 1.05), 2), "B365CD": round(1 / (p[1] * 1.05), 2),
                            "B365CA": round(1 / (p[2] * 1.05), 2)})
            rows.append(row)
        frames[s] = pd.DataFrame(rows)
    truth = dict(att=dict(zip(TEAMS, att)), de=dict(zip(TEAMS, de)), mu=mu, home=home)
    return frames, truth


@pytest.fixture(scope="session")
def league():
    from iddaabot.footballdata import parse_csv
    frames, truth = simulate_league()
    parsed = {}
    for s, df in frames.items():
        parsed[s] = parse_csv(df.to_csv(index=False))
        parsed[s]["Season"] = s
    return parsed, truth
