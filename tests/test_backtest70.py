"""%70 kuponunun geçmiş testi: sızıntı yok, canlı kupon kurucuyla aynı seçim."""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from iddaabot import rules70
from iddaabot.backtest70 import (
    HAIRCUT,
    CouponView,
    Pick,
    build_coupons,
    choose_variant,
    coupons_from_picks,
    filter_picks,
    losing_streak,
    pair_rate,
    profit,
    spec_key,
    walk_frame,
    walk_seasons,
    catalog,
)
from iddaabot.engine import Candidate


def _df(rows):
    return pd.DataFrame(rows)


def _base(**kw):
    row = dict(HTHG=0, HTAG=0, HC=5, AC=4, FTHG=1, FTAG=0)
    row.update(kw)
    return row


def test_pair_rate_matches_live_rule():
    rng = np.random.default_rng(1)
    for spec in catalog():
        if spec.get("subject") == "corners":
            cols = ["HC", "AC"]
        elif spec.get("period") == "H1":
            cols = ["HTHG", "HTAG"]
        else:
            cols = ["FTHG", "FTAG"]
        data = {c: rng.integers(0, 5, size=25).astype(float) for c in cols}
        data[cols[0]][4] = np.nan
        data[cols[1]][7] = np.nan
        df = pd.DataFrame(data)
        got = pair_rate(df[cols[0]].to_numpy(float), df[cols[1]].to_numpy(float), spec)
        live = rules70._rate(df, spec)
        assert got == live


def test_no_lookahead_and_min_games():
    rows = []
    # İki iç saha golü: üçüncü maçta örnek 2, listeye giremez.
    rows.append(_base(Date="2024-08-01", HomeTeam="H", AwayTeam="X", FTHG=2, FTAG=0))
    rows.append(_base(Date="2024-08-08", HomeTeam="H", AwayTeam="Y", FTHG=2, FTAG=0))
    rows.append(_base(Date="2024-08-15", HomeTeam="H", AwayTeam="Z", FTHG=2, FTAG=0))
    # Aynı günde ikinci bir iç saha maçı da geçmişe yazılırsa 3/3 olur; yazılmamalı.
    rows.append(_base(Date="2024-08-15", HomeTeam="H", AwayTeam="Q", FTHG=2, FTAG=0))
    rows.append(_base(Date="2024-08-22", HomeTeam="H", AwayTeam="W", FTHG=0, FTAG=1))
    picks = walk_frame(_df(rows), "tur.1", "Süper Lig", 2024)
    ev = [p for p in picks if p.market == "Ev 0.5 Üst"]
    assert [p.date for p in ev] == [date(2024, 8, 22)]
    assert ev[0].hit is False
    # 15 Ağustos'ta iki maç var; ikisi de ancak ertesi haftanın geçmişine girer.
    assert ev[0].n_min == 4 and ev[0].claimed_min == 1
    assert ev[0].p_smooth == pytest.approx((4 + 1) / (4 + 2))
    # 15 Ağustos'ta hiç Ev 0.5 Üst yok (ne 2 maçlık örnek, ne aynı gün sızıntısı).
    assert not any(p.date == date(2024, 8, 15) and p.market == "Ev 0.5 Üst" for p in picks)


def test_season_does_not_leak():
    past = [
        _base(Date="2023-09-01", HomeTeam="H", AwayTeam="A", FTHG=3, FTAG=0),
        _base(Date="2023-09-08", HomeTeam="H", AwayTeam="B", FTHG=3, FTAG=0),
        _base(Date="2023-09-15", HomeTeam="H", AwayTeam="C", FTHG=3, FTAG=0),
    ]
    nxt = [_base(Date="2024-08-10", HomeTeam="H", AwayTeam="D", FTHG=3, FTAG=0)]
    split = walk_seasons({2023: _df(past), 2024: _df(nxt)}, "tur.1", "Süper Lig")
    assert not any(p.season == 2024 for p in split)
    leaked = walk_frame(pd.concat([_df(past), _df(nxt)], ignore_index=True), "tur.1", "Süper Lig", 2024)
    assert any(p.market == "Ev 0.5 Üst" and p.home == "H" for p in leaked)


def test_match_level_needs_both_sides_and_away_orientation():
    rows = []
    for i, d in enumerate(("2024-08-01", "2024-08-08", "2024-08-15")):
        rows.append(_base(Date=d, HomeTeam="H", AwayTeam=f"P{i}", FTHG=1, FTAG=0))
        # Deplasman takımı dışarıda yenildi (ev sahibi kazandı) → MS 1 desteklenir.
        rows.append(_base(Date=d, HomeTeam=f"Q{i}", AwayTeam="A", FTHG=2, FTAG=0))
    rows.append(_base(Date="2024-08-22", HomeTeam="H", AwayTeam="A", FTHG=1, FTAG=0, B365H=1.40))
    good = walk_frame(_df(rows), "eng.1", "Premier Lig", 2024)
    ms = [p for p in good if p.date == date(2024, 8, 22) and p.market == "MS 1"]
    assert len(ms) == 1 and ms[0].hit and ms[0].odds == pytest.approx(1.40)
    assert ms[0].odds_src == "bet365" and ms[0].ks == (3, 3)

    bad = []
    for i, d in enumerate(("2024-08-01", "2024-08-08", "2024-08-15")):
        bad.append(_base(Date=d, HomeTeam="H", AwayTeam=f"P{i}", FTHG=1, FTAG=0))
        # Dışarıda kazandı → MS 1'in deplasman ayağı tutmaz.
        bad.append(_base(Date=d, HomeTeam=f"Q{i}", AwayTeam="A", FTHG=0, FTAG=2))
    bad.append(_base(Date="2024-08-22", HomeTeam="H", AwayTeam="A", FTHG=1, FTAG=0, B365H=1.40))
    no = walk_frame(_df(bad), "eng.1", "Premier Lig", 2024)
    assert not any(p.market == "MS 1" for p in no)


def test_both_teams_for_total_and_team_market_is_one_sided():
    rows = []
    for i, d in enumerate(("2024-09-01", "2024-09-08", "2024-09-15")):
        # Ev sahibi içeride 3-0 (2.5 üst). Deplasman takımı dışarıda 0-0 (2.5 alt).
        rows.append(_base(Date=d, HomeTeam="H", AwayTeam=f"P{i}", FTHG=3, FTAG=0, HTHG=2, HTAG=0))
        rows.append(_base(Date=d, HomeTeam=f"Q{i}", AwayTeam="A", FTHG=0, FTAG=0))
    rows.append(_base(Date="2024-09-22", HomeTeam="H", AwayTeam="A", FTHG=3, FTAG=0, HTHG=1, HTAG=0))
    picks = walk_frame(_df(rows), "esp.1", "La Liga", 2024)
    day = [p for p in picks if p.date == date(2024, 9, 22)]
    assert any(p.market == "Ev 0.5 Üst" for p in day)
    assert not any(p.market == "2.5 Üst" for p in day)
    assert not any(p.market == "Dep 0.5 Üst" for p in day)
    # Ev 1.5 Üst yalnız ev sahibine bakar. İY 0.5 Üst iki tarafa da bakar; deplasman 0-0 kaldı.
    assert any(p.market == "Ev 1.5 Üst" and p.claimed_min == 1 for p in day)
    assert not any(p.market == "İY 0.5 Üst" for p in day)


def test_coupon_builder_matches_live(monkeypatch):
    table = {}
    analyses = []
    for i in range(6):
        a_id = str(i)
        cs = [
            Candidate(market="MS 1", outcome="MS 1", spec={}, odds=1.35 + i * 0.02, p=0.82 - i * 0.01,
                      ev=0, conf="orta", margin=None, mbs=1),
            Candidate(market="2.5 Alt", outcome="2.5 Alt", spec={}, odds=1.70, p=0.60,
                      ev=0, conf="orta", margin=None, mbs=1),
            Candidate(market="ucuz", outcome="ucuz", spec={}, odds=1.05, p=0.95,
                      ev=0, conf="orta", margin=None, mbs=1),
        ]
        if i == 5:
            cs.append(Candidate(market="MBS", outcome="MBS", spec={}, odds=1.30, p=0.90,
                                ev=0, conf="orta", margin=None, mbs=4))
        table[a_id] = cs
        analyses.append(type("M", (), {"fx": type("F", (), {"espn_id": a_id})()})())

    def like_options(a, min_odds=1.0):
        cs = [c for c in table[a.fx.espn_id] if c.odds and c.odds >= min_odds]
        return sorted(cs, key=lambda c: (-c.p, -c.odds))

    monkeypatch.setattr(rules70, "options", like_options)
    live = rules70.coupons(analyses, 1000)
    mine = build_coupons([(a, table[a.fx.espn_id]) for a in analyses])
    assert len(live) == len(mine) >= 1
    for left, right in zip(live, mine):
        assert left.odds == pytest.approx(math_prod(c.odds for _, c in right))
        assert [a.fx.espn_id for a, _ in left.legs] == [a.fx.espn_id for a, _ in right]
        assert [c.market for _, c in left.legs] == [c.market for _, c in right]
        assert max(c.mbs for _, c in left.legs) <= len(left.legs)


def math_prod(xs):
    p = 1.0
    for x in xs:
        p *= x
    return p


def test_daily_coupon_band_and_flat_haircut():
    rows = []
    for n, (h, a) in enumerate((("H1", "A1"), ("H2", "A2"), ("H3", "A3"))):
        for i, d in enumerate(("2024-10-01", "2024-10-08", "2024-10-15")):
            rows.append(_base(Date=d, HomeTeam=h, AwayTeam=f"P{n}{i}", FTHG=1, FTAG=0))
            rows.append(_base(Date=d, HomeTeam=f"Q{n}{i}", AwayTeam=a, FTHG=2, FTAG=0))
        rows.append(_base(Date="2024-10-22", HomeTeam=h, AwayTeam=a, FTHG=1, FTAG=0, B365H=1.40))
    picks = walk_frame(_df(rows), "tur.1", "Süper Lig", 2024)
    cps = coupons_from_picks(picks, league="tur.1", league_name="Süper Lig")
    assert len(cps) == 1
    cp = cps[0]
    assert cp.won and cp.odds == pytest.approx(1.40 ** 3)
    assert 2.0 <= cp.odds <= 3.0
    assert [p.market for p in cp.legs] == ["MS 1", "MS 1", "MS 1"]
    assert len({p.match_id for p in cp.legs}) == 3
    assert profit(cp) == pytest.approx(cp.odds - 1)
    assert profit(cp, HAIRCUT) == pytest.approx((1.40 * HAIRCUT) ** 3 - 1)


def test_filter_sample_and_threshold():
    def pk(k, n, family="MS", key="1x2|1"):
        return Pick("tur.1", "Süper Lig", 2024, date(2024, 9, 1), "m", "H", "A", "MS 1", family, key,
                    k / n, k / n, n, (k + 1) / (n + 2), True, 1.5, "bet365", ks=(k,), ns=(n,))

    rows = [pk(3, 5), pk(8, 10), pk(3, 4, family="Alt/Üst", key="ou|goals|FT|total|2.5|over")]
    assert len(filter_picks(rows, thresh=0.70, min_games=6)) == 1
    assert len(filter_picks(rows, thresh=0.80, min_games=3)) == 1
    only = filter_picks(rows, thresh=0.70, min_games=3, keys={"ou|goals|FT|total|2.5|over"})
    assert [p.family for p in only] == ["Alt/Üst"]
    assert spec_key({"kind": "ou", "subject": "goals", "period": "FT", "scope": "total",
                     "line": 2.5, "side": "over"}) in {p.key for p in only}


def test_league_breakdown_matches_espn_code():
    from iddaabot.backtest70 import VARIANTS, _run_variant

    def pk(league, name, mid, odd):
        return Pick(league, name, 2024, date(2024, 11, 2), mid, "H", "A", "2.5 Üst", "Alt/Üst",
                    "ou|goals|FT|total|2.5|over", 0.8, 0.8, 8, 0.75, True, odd, "bet365",
                    ks=(8,), ns=(10,))

    picks = [pk("tur.1", "Süper Lig", "a", 1.50), pk("tur.1", "Süper Lig", "b", 1.40)]
    run = _run_variant(picks, VARIANTS[0], with_leagues=True)
    by = {row["league"]: row for row in run["by_league"]}
    assert by["Süper Lig"]["picks"]["n"] == 2
    assert by["Süper Lig"]["coupons"]["n"] == 1
    assert by["Premier Lig"]["picks"]["n"] == 0


def test_choose_variant_uses_tune_roi_only():
    order_first = {"mevcut": {"n": 100, "roi": 0.01}, "en_az_6": {"n": 80, "roi": 0.05},
                   "yuzde_80": {"n": 10, "roi": 0.50}}
    assert choose_variant(order_first) == "en_az_6"
    tie = {"mevcut": {"n": 100, "roi": 0.02}, "en_az_6": {"n": 90, "roi": 0.02}}
    assert choose_variant(tie) == "mevcut"
    assert losing_streak([
        CouponView(date(2024, 1, d), 2023, "hepsi", "Tüm", 2.0, won, [])
        for d, won in ((1, True), (2, False), (3, False), (4, False), (5, True), (6, False))
    ]) == 3
