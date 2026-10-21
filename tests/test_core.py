import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from iddaabot import dixon_coles, espn, iddaa
from iddaabot.markets import (CountModel, GoalModel, basic_probs, devig, fit_lambdas, score_matrix,
                              selection_prob, american_to_decimal)
from iddaabot.settle import evaluate


# ------------------------------------------------------------ olasılık motoru
def test_goalmodel_consistency():
    gm = GoalModel(1.6, 1.1, -0.06, 0.44)
    s = gm.summary()
    assert abs(s["1"] + s["X"] + s["2"] - 1) < 1e-9
    fm = basic_probs(score_matrix(1.6, 1.1, -0.06))
    # devre modeli ile tam maç matrisi aynı sonucu vermeli (Poisson toplamı + aynı DC düzeltmesi)
    for k in ("1", "X", "2", "Ü2.5", "KGV"):
        assert abs(s[k] - fm[k]) < 2e-3, k


def test_all_market_kinds_sum_to_one():
    gm = GoalModel(1.4, 1.2)
    P = lambda sp: selection_prob(sp, gm, None, None)  # noqa: E731
    htft = sum(P({"kind": "htft", "ht": a, "ft": b}) for a in "1X2" for b in "1X2")
    assert abs(htft - 1) < 1e-9
    rng = sum(P({"kind": "range", "lo": lo, "hi": hi}) for lo, hi in ((0, 1), (2, 3), (4, 5), (6, 99)))
    assert abs(rng - 1) < 1e-9
    combo = sum(P({"kind": "combo", "parts": [{"kind": "1x2", "sel": a}, {"kind": "btts", "yes": y}]})
                for a in "1X2" for y in (True, False))
    assert abs(combo - 1) < 1e-9
    hc = sum(P({"kind": "handicap", "sel": s, "hh": 0, "ha": 1}) for s in "1X2")
    assert abs(hc - 1) < 1e-9
    # handikap (0:1) ev sahibi = 2+ farkla kazanır
    v = gm.v
    assert abs(P({"kind": "handicap", "sel": "1", "hh": 0, "ha": 1}) - gm.p(v["H"] - v["A"] >= 2)) < 1e-12
    h1 = P({"kind": "1x2", "period": "H1", "sel": "X"})
    assert 0.35 < h1 < 0.5  # ilk yarı beraberlik tipik aralık
    team = P({"kind": "ou", "scope": "home", "line": 0.5, "side": "over"})
    assert abs(team - (1 - math.exp(-1.4))) < 0.02


def test_count_model():
    cm = CountModel(mh=5.5, ma=4.5, disp_team=1.3, disp_total=1.25)
    p = cm.p_over(9.5)
    assert 0.4 < p < 0.6
    assert abs(cm.p_1x2("1") + cm.p_1x2("X") + cm.p_1x2("2") - 1) < 1e-6
    assert cm.p_over(4.5, "home") > cm.p_over(4.5, "away")


def test_devig_and_fit():
    p = devig([2.10, 3.40, 3.60])
    assert abs(sum(p) - 1) < 1e-9 and p[0] > p[2]
    true = basic_probs(score_matrix(1.7, 0.9))
    lam = fit_lambdas((true["1"], true["X"], true["2"]), (2.5, true["Ü2.5"]))
    assert abs(lam[0] - 1.7) < 0.05 and abs(lam[1] - 0.9) < 0.05
    assert american_to_decimal("+110") == pytest.approx(2.10)
    assert american_to_decimal("-160") == pytest.approx(1.625)
    assert american_to_decimal("EVEN") == 2.0


# ------------------------------------------------------------ Dixon-Coles
def test_dixon_coles_recovers_strength(league):
    seasons, truth = league
    df = pd.concat(seasons.values())
    m = dixon_coles.fit(df.HomeTeam.tolist(), df.AwayTeam.tolist(), df.FTHG.tolist(), df.FTAG.tolist(),
                        [d.to_pydatetime() for d in df.Date])
    est = np.array([m.attack[t] for t in truth["att"]])
    tru = np.array(list(truth["att"].values()))
    assert np.corrcoef(est, tru)[0, 1] > 0.8
    assert abs(m.home_adv - truth["home"]) < 0.12


# ------------------------------------------------------------ iddaa bülteni
CFG = {
    "1_1": {"n": "Maç Sonucu"}, "2_1": {"n": "Çifte Şans"}, "3_1": {"n": "Alt/Üst {0}"},
    "4_1": {"n": "Karşılıklı Gol"}, "5_1": {"n": "İlk Yarı / Maç Sonucu"}, "6_1": {"n": "Handikaplı Maç Sonucu ({0})"},
    "7_1": {"n": "Maç Sonucu ve Karşılıklı Gol"}, "8_1": {"n": "Toplam Korner Sayısı Alt/Üst"},
    "9_1": {"n": "İlk Yarı Sonucu"}, "10_1": {"n": "Toplam Gol"}, "11_1": {"n": "Maç Skoru"},
    "12_1": {"n": "Tek / Çift"}, "13_1": {"n": "Hangi Yarıda Daha Fazla Gol Olur"},
    "14_1": {"n": "Ev Sahibi Alt/Üst {0}"}, "15_1": {"n": "Oyuncu Gol Atar"},
    "16_1": {"n": "Maç Sonucu ve Alt/Üst 2.5"}, "17_1": {"n": "İlk Yarı Alt/Üst {0}"},
}


HTFT = {"1/1": 3.2, "1/0": 14.0, "1/2": 28.0, "0/1": 5.0, "0/0": 5.2, "0/2": 8.5,
        "2/1": 24.0, "2/0": 14.0, "2/2": 6.5}


def sample_events(kickoff: datetime):
    o = lambda *xs: [{"n": n, "odd": v} for n, v in xs]  # noqa: E731
    return {"data": {"events": [{
        "i": 4321, "hn": "Galatasaray", "an": "Fenerbahçe", "ci": 1, "d": int(kickoff.timestamp()), "mbc": 1,
        "m": [
            {"t": 1, "st": 1, "o": o(("1", 2.05), ("0", 3.50), ("2", 3.20))},
            {"t": 2, "st": 1, "o": o(("1-X", 1.30), ("1-2", 1.28), ("X-2", 1.70))},
            {"t": 3, "st": 1, "sov": 2.5, "o": o(("Alt", 1.95), ("Üst", 1.70))},
            {"t": 3, "st": 1, "sov": 1.5, "o": o(("Alt", 3.40), ("Üst", 1.22))},
            {"t": 4, "st": 1, "o": o(("Var", 1.65), ("Yok", 2.05))},
            {"t": 5, "st": 1, "o": o(*[(f"{a}/{b}", HTFT[f"{a}/{b}"]) for a in "102" for b in "102"])},
            {"t": 6, "st": 1, "sov": "0:1", "o": o(("1", 3.90), ("X", 3.60), ("2", 1.70))},
            {"t": 7, "st": 1, "o": o(("1 ve Var", 4.50), ("0 ve Var", 4.60), ("2 ve Var", 5.20),
                                     ("1 ve Yok", 4.10), ("0 ve Yok", 9.00), ("2 ve Yok", 6.80))},
            {"t": 8, "st": 1, "sov": 9.5, "o": o(("Alt", 1.80), ("Üst", 1.85))},
            {"t": 9, "st": 1, "o": o(("1", 2.70), ("0", 2.10), ("2", 3.70))},
            {"t": 10, "st": 1, "o": o(("0-1 Gol", 4.00), ("2-3 Gol", 1.95), ("4-5 Gol", 3.50), ("6+ Gol", 12.0))},
            {"t": 11, "st": 1, "o": o(("1-0", 8.0), ("2-1", 8.5), ("1-1", 6.5), ("Diğer", 3.0))},
            {"t": 12, "st": 1, "o": o(("Tek", 1.85), ("Çift", 1.85))},
            {"t": 13, "st": 1, "o": o(("1. Yarı", 3.10), ("2. Yarı", 1.95), ("Eşit", 3.40))},
            {"t": 14, "st": 1, "sov": 1.5, "o": o(("Alt", 1.75), ("Üst", 1.95))},
            {"t": 15, "st": 1, "o": o(("Icardi", 2.5),)},
            {"t": 16, "st": 1, "o": o(("1 ve Alt", 5.5), ("1 ve Üst", 3.4), ("0 ve Alt", 4.6),
                                      ("0 ve Üst", 12.0), ("2 ve Alt", 7.0), ("2 ve Üst", 5.0))},
            {"t": 17, "st": 1, "sov": 0.5, "o": o(("Alt", 3.0), ("Üst", 1.33))},
        ]}]}}


def test_parse_and_classify():
    ko = datetime.now(timezone.utc) + timedelta(hours=5)
    evs = iddaa.parse_events(sample_events(ko), CFG, {"1": "Türkiye Süper Lig"})
    ev = evs[0]
    assert ev.mbs == 1 and ev.competition == "Türkiye Süper Lig"
    gm = GoalModel(1.5, 1.2)
    cm = CountModel(5.4, 4.6)
    for m in ev.markets:
        if m.name.startswith("Oyuncu"):
            assert m.mtype is None
            continue
        assert m.mtype is not None, m.name
        specs = [o.spec for o in m.outcomes]
        if m.name == "Maç Skoru":
            assert specs[-1] is None and all(specs[:-1])  # "Diğer" modellenmez
            continue
        assert all(specs), (m.name, [o.name for o in m.outcomes])
        total = sum(selection_prob(s, gm, cm, None) for s in specs)
        if m.name.startswith("Çifte"):
            assert abs(total - 2) < 1e-6
        else:
            assert abs(total - 1) < 1e-6, (m.name, total)


@pytest.mark.parametrize("name,sov,typ", [
    ("Alt/Üst 3.5", None, "ou"), ("Altı/Üstü 2,5", None, "ou"), ("İlk Yarı Altı/Üstü 1.5", None, "ou"),
    ("Deplasman Alt/Üst 0.5", None, "ou"), ("Handikaplı Maç Sonucu (1:0)", None, "handicap"),
    ("İkinci Yarı Sonucu", None, "1x2"), ("Ev Sahibi Gol Yemeden Kazanır", None, "win_to_nil"),
    ("Altı/Üstü 2.5 ve Karşılıklı Gol", None, "combo"), ("İlk Golü Kim Atar", None, "first_goal"),
    ("İlk Yarı Toplam Korner Sayısı Alt/Üst", 4.5, "ou"), ("Toplam Kart Sayısı Alt/Üst", 4.5, "ou"),
])
def test_classify_names(name, sov, typ):
    mt = iddaa.classify_market(name, sov)
    assert mt and mt["type"] == typ
    if "Korner" in name:
        assert mt["subject"] == "corners" and mt["period"] == "H1"
    if "Kart" in name:
        assert mt["subject"] == "cards"


@pytest.mark.parametrize("name", ["Oyuncu Gol Atar", "İlk 10 Dakika Gol", "Penaltı Olur mu",
                                  "Kart Puanı Alt/Üst", "Maç Sonucu (Beraberlikte İade)", "Alt/Üst 3"])
def test_skip_names(name):
    assert iddaa.classify_market(name, None) is None


# ------------------------------------------------------------ sonuçlandırma
def test_settle():
    ft, ht = (2, 1), (0, 1)
    assert evaluate({"kind": "1x2", "sel": "1"}, ft, ht) is True
    assert evaluate({"kind": "htft", "ht": "2", "ft": "1"}, ft, ht) is True
    assert evaluate({"kind": "htft", "ht": "X", "ft": "1"}, ft, None) is None
    assert evaluate({"kind": "ou", "line": 2.5, "side": "over", "scope": "total"}, ft, None) is True
    assert evaluate({"kind": "btts", "yes": True}, ft, None) is True
    assert evaluate({"kind": "handicap", "sel": "X", "hh": 0, "ha": 1}, ft, None) is True
    assert evaluate({"kind": "combo", "parts": [{"kind": "1x2", "sel": "1"}, {"kind": "ou", "line": 2.5,
                                                                             "side": "under"}]}, ft, None) is False
    assert evaluate({"kind": "half_more", "sel": "2"}, ft, ht) is True
    assert evaluate({"kind": "ou", "subject": "corners", "scope": "total", "line": 9.5, "side": "over"},
                    ft, None, corners=(6, 5)) is True
    assert evaluate({"kind": "ou", "subject": "corners", "scope": "total", "line": 9.5, "side": "over"},
                    ft, None) is None
    assert evaluate({"kind": "first_goal", "sel": "home"}, ft, ht) is None
    assert evaluate({"kind": "range", "lo": 2, "hi": 3}, ft, None) is True
    assert evaluate({"kind": "score", "x": 2, "y": 1}, ft, None) is True


def test_gemini_parse(monkeypatch):
    import asyncio
    from iddaabot import gemini
    from iddaabot.config import settings
    monkeypatch.setattr(settings, "gemini_api_key", "x")
    text = ('Bulgular:\n```json\n{"ev_eksikler": [{"oyuncu": "Icardi", "durum": "sakat", "kilit": true}],'
            '"dep_eksikler": [], "ev_gol_carpani": 0.7, "dep_gol_carpani": 1.04, "notlar": "Not", '
            '"bilgi_guveni": "orta"}\n```')

    async def fake_call(prompt, grounded):
        return {"candidates": [{"content": {"parts": [{"text": text}]},
                                "groundingMetadata": {"groundingChunks": [{"web": {"uri": "https://a.b", "title": "A"}}]}}]}
    monkeypatch.setattr(gemini, "_call", fake_call)
    d = asyncio.run(gemini.team_news("k1", "GS", "FB", "Süper Lig", "18.10 20:00"))
    assert d["ev_gol_carpani"] == 0.85 and d["dep_gol_carpani"] == 1.04  # sınırlandı
    assert d["ev_eksikler"][0]["oyuncu"] == "Icardi" and d["kaynaklar"][0]["uri"] == "https://a.b"


def test_handicap_placeholder_and_guard():
    from iddaabot import engine
    from iddaabot.config import settings
    cfg = {"1_1": {"n": "Maç Sonucu"}, "2_100": {"n": "Handikaplı Maç Sonucu {h}"}}
    ko = datetime.now(timezone.utc) + timedelta(hours=3)
    o = lambda *xs: [{"n": n, "odd": v} for n, v in xs]  # noqa: E731
    js = {"data": {"events": [{"i": 1, "hn": "A", "an": "B", "ci": 1, "d": int(ko.timestamp()), "mbc": 1, "m": [
        {"t": 1, "st": 1, "mbc": 1, "o": o(("1", 2.20), ("0", 3.30), ("2", 3.20))},
        {"t": 2, "st": 100, "mbc": 2, "sov": "1", "o": o(("1", 1.47), ("0", 4.24), ("2", 3.55))},
        {"t": 2, "st": 100, "mbc": 2, "sov": "-1", "o": o(("1", 4.40), ("0", 3.90), ("2", 1.50))},
    ]}]}}
    ev = iddaa.parse_events(js, cfg, {})[0]
    names = [m.name for m in ev.markets]
    assert "Handikaplı Maç Sonucu (1:0)" in names and "Handikaplı Maç Sonucu (0:1)" in names
    h = next(m for m in ev.markets if m.name.endswith("(1:0)"))
    assert h.mbs == 2 and h.outcomes[0].spec == {"kind": "handicap", "period": "FT", "sel": "1", "hh": 1, "ha": 0}
    # Ev +1: "1" = ev sahibi kaybetmez
    gm = GoalModel(1.3, 1.1)
    p1 = selection_prob(h.outcomes[0].spec, gm, None, None)
    s = gm.summary()
    assert abs(p1 - (s["1"] + s["X"])) < 1e-9
    fx = espn.Fixture(espn_id="1", league="tur.1", kickoff=ko, home="A", away="B", home_id="1", away_id="2",
                      state="pre", completed=False, status_name="", ext_1x2=(2.30, 3.30, 3.10))
    a = engine.analyse(engine.MatchAnalysis(fx=fx, ev=ev, league_name="Süper Lig"), 1000)
    assert a.n_suspect == 0 and any(c.market.startswith("Handikaplı") for c in a.candidates)
    # Yanlış yorumlanmış (ters çevrilmiş) bir pazar güvenlik filtresine takılmalı
    for oc in h.outcomes:
        oc.spec = dict(oc.spec, hh=-1)
    a = engine.analyse(a, 1000)
    assert a.n_suspect >= 1 and not any(c.market.endswith("(1:0)") for c in a.candidates)
