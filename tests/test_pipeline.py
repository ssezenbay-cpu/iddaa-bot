import asyncio
import json
import re
from datetime import datetime, timedelta, timezone

import pandas as pd

from iddaabot import backtest, engine, espn, footballdata, iddaa, mackolik, report, storage, tracker
from iddaabot.config import settings

from .test_core import CFG, sample_events


def _fixture(ko, ext=True):
    return espn.Fixture(espn_id="401", league="tur.1", kickoff=ko, home="Galatasaray", away="Fenerbahçe",
                        home_id="432", away_id="436", state="pre", completed=False, status_name="STATUS_SCHEDULED",
                        ext_1x2=(1.80, 3.80, 4.40) if ext else None, ext_ou=(2.5, 1.80, 2.00) if ext else None,
                        ext_provider="DraftKings")


def _league_data(league):
    seasons, _ = league
    df_all = pd.concat([seasons[2024], seasons[2025]], ignore_index=True)
    return footballdata.build_league("T1", df_all, seasons[2025], datetime(2026, 5, 1))


def test_analyse_end_to_end(league):
    ld = _league_data(league)
    assert ld.model and ld.corners and ld.cards
    ko = datetime.now(timezone.utc) + timedelta(hours=6)
    ev = iddaa.parse_events(sample_events(ko), CFG, {})[0]
    fx = _fixture(ko)
    assert engine.match_iddaa(fx, [ev]) is ev
    a = engine.MatchAnalysis(fx=fx, ev=ev, league_name="Süper Lig", ld=ld, fd_home="Galatasaray", fd_away="Fenerbahce")
    engine.analyse(a, 1000)
    assert a.lam and a.lam_mkt and a.lam_dc and a.gm and a.corners
    assert a.n_priced >= 15
    assert a.data_conf in ("yüksek", "orta", "düşük")
    # ext piyasa GS favori (1.80) – iddaa MS1 2.05 veriyor: MS1 değerli çıkmalı
    ms1 = next(c for c in a.candidates if c.market == "Maç Sonucu" and c.outcome == "1")
    assert ms1.ev > 0.0
    for c in a.picks:
        assert engine.is_value(c) and c.stake >= settings.min_stake
    txt = report.detail(a)
    print(re.sub(r"<[^>]+>", "", txt))
    assert ("Değerli seçimler" in txt or "Öneri: Pas" in txt) and "Korner kullandı" in txt
    # ikinci analiz aynı sonucu vermeli (durum sıfırlanıyor)
    n1 = len(a.candidates)
    engine.analyse(a, 1000)
    assert len(a.candidates) == n1 and a.notes.count(a.notes[0] if a.notes else "") <= 1


def test_coupons_and_daily_report(league):
    ld = _league_data(league)
    ko = datetime.now(timezone.utc) + timedelta(hours=6)
    analyses = []
    for k, (h, aw, fh, fa) in enumerate([("Galatasaray", "Fenerbahçe", "Galatasaray", "Fenerbahce"),
                                         ("Beşiktaş", "Trabzonspor", "Besiktas", "Trabzonspor"),
                                         ("Başakşehir", "Kasımpaşa", "Buyuksehyr", "Kasimpasa")]):
        js = sample_events(ko + timedelta(hours=k))
        js["data"]["events"][0].update({"i": 100 + k, "hn": h, "an": aw})
        ev = iddaa.parse_events(js, CFG, {})[0]
        fx = _fixture(ko + timedelta(hours=k))
        fx.espn_id, fx.home, fx.away = str(500 + k), h, aw
        a = engine.MatchAnalysis(fx=fx, ev=ev, league_name="Süper Lig", ld=ld, fd_home=fh, fd_away=fa)
        analyses.append(engine.analyse(a, 1000))
    cps = engine.build_coupons(analyses, 1000)
    for cp in cps:
        assert len({a.fx.espn_id for a, _ in cp.legs}) == len(cp.legs)
        assert cp.ev >= 0
    sc = engine.DayScan(analyses=analyses, n_fixtures=3, n_iddaa=3, iddaa_ok=True)
    storage.init()
    ids = {(a.fx.espn_id, c.market, c.outcome): storage.save_pick(a, c) for a, c in engine.singles(analyses)}
    txt = report.daily(sc, "Günün Analizi", ids, [(cp, i + 1) for i, cp in enumerate(cps)], 1000)
    assert "Değerli seçimler" in txt
    # basit HTML denge kontrolü
    for tag in ("b", "i", "code"):
        assert txt.count(f"<{tag}>") == txt.count(f"</{tag}>"), tag
    assert all(len(p) <= 4096 for p in report.split(txt))


def test_settlement_and_card(league, monkeypatch):
    storage.init()
    ld = _league_data(league)
    ko = datetime.now(timezone.utc) - timedelta(hours=4)
    ev = iddaa.parse_events(sample_events(ko), CFG, {})[0]
    fx = _fixture(ko)
    fx.espn_id = "999"
    a = engine.analyse(engine.MatchAnalysis(fx=fx, ev=ev, league_name="Süper Lig", ld=ld,
                                            fd_home="Galatasaray", fd_away="Fenerbahce"), 1000)
    c_ms = next(c for c in a.candidates if c.market == "Maç Sonucu" and c.outcome == "1")
    c_ht = next(c for c in a.candidates if c.market == "İlk Yarı / Maç Sonucu" and c.outcome == "0/1")
    p1 = storage.save_pick(a, c_ms)
    p2 = storage.save_pick(a, c_ht)
    done = espn.Fixture(espn_id="999", league="tur.1", kickoff=ko, home="Galatasaray", away="Fenerbahçe",
                        home_id="1", away_id="2", state="post", completed=True, status_name="STATUS_FULL_TIME",
                        home_score=2, away_score=1, ht_home=1, ht_away=1)

    async def fake_sb(league, day, ttl=0):
        return [done]
    monkeypatch.setattr(espn, "scoreboard", fake_sb)
    out = asyncio.run(tracker.settle_due())
    st = {r["id"]: r["status"] for r in out}
    assert st[p1] == "won" and st[p2] == "won"
    rc = tracker.report_card()
    assert rc["all"]["won"] >= 2
    assert "Karne" in report.karne(rc)


def test_espn_parse():
    js = {"events": [{"id": "401888391", "date": "2026-10-18T17:00Z",
                      "status": {"type": {"state": "pre", "completed": False, "name": "STATUS_SCHEDULED"}},
                      "competitions": [{"venue": {"fullName": "RAMS Park"},
                                        "competitors": [
                                            {"homeAway": "home", "team": {"id": "432", "displayName": "Galatasaray"}, "form": "WWDLW"},
                                            {"homeAway": "away", "team": {"id": "436", "displayName": "Fenerbahce"}}],
                                        "odds": [{"provider": {"name": "DraftKings", "priority": 1},
                                                  "overUnder": 2.5, "drawOdds": {"moneyLine": 260},
                                                  "moneyline": {"home": {"close": {"odds": "+105"}},
                                                                "away": {"close": {"odds": "+250"}}},
                                                  "total": {"over": {"close": {"odds": "-125", "line": "o2.5"}},
                                                            "under": {"close": {"odds": "+100"}}}}]}]}]}
    f = espn.parse_scoreboard(js, "tur.1")[0]
    assert f.ext_1x2 == (2.05, 3.6, 3.5)
    assert f.ext_ou[0] == 2.5 and abs(f.ext_ou[1] - 1.8) < 1e-9
    s = espn.parse_summary({"boxscore": {"teams": [
        {"homeAway": "home", "statistics": [{"name": "wonCorners", "displayValue": "7"},
                                            {"name": "yellowCards", "displayValue": "2"},
                                            {"name": "redCards", "displayValue": "1"}]},
        {"homeAway": "away", "statistics": [{"name": "wonCorners", "displayValue": "3"},
                                            {"name": "yellowCards", "displayValue": "4"},
                                            {"name": "redCards", "displayValue": "0"}]}],
        "gameInfo": {"officials": [{"fullName": "Michael Oliver"}]},
        "lastFiveGames": [{"team": {"id": "432"}, "events": [{"gameResult": "W", "score": "2-0", "atVs": "vs",
                                                              "opponent": {"displayName": "X"}}]}]}})
    assert s.corners() == (7, 3) and s.cards() == (3, 4) and s.referee == "Michael Oliver"
    assert s.last5["432"][0].startswith("G 2-0")


def test_mackolik_parse():
    # Sunucudan alınan gerçek yanıt biçimi: tek tırnak, seyrek dizi, yılsız tarih
    payload = ("[[4308325,'08/08','MS',572,'Gaziantep FK',1,'Galatasaray',,4,0,3,6.15,4.42,1.24,1.83,1,1,"
               "2.15,1.37,1,0,0,0,'0 - 3',0],[4308323,'09/08','MS',8,'Samsunspor',7,'Gençlerbirliği',,4,2,1,"
               "1.52,3.39,4.12,1,1,1.5,1.68,1.67,0,0,0,0,'1 - 0',0]]")
    rows = mackolik._rows(mackolik._parse_payload(payload))
    m = mackolik.parse_row(rows[0], 2025)
    assert m.date.year == 2025 and m.date.month == 8 and m.date.day == 8
    assert m.home == "Gaziantep FK" and (m.hg, m.ag) == (0, 3) and m.odds == (6.15, 4.42, 1.24)
    m2 = mackolik.parse_row(rows[1], 2025)
    assert (m2.hg, m2.ag) == (2, 1) and m2.ht == (1, 0) and m2.away == "Gençlerbirliği"
    jan = mackolik.parse_row([1, "17/01", "MS", 1, "A", 2, "B", None, 4, 1, 1, 2.0, 3.0, 4.0] + [0] * 9 + ["0 - 0"], 2025)
    assert jan.date.year == 2026
    wrapped = json.dumps({"d": json.dumps([[1, "17/08/2025", 1, 11, "X", 12, "Y", 0, 0, "2", "1", "1,85", "3,60", "4,10"]])})
    assert mackolik.parse_row(mackolik._rows(mackolik._parse_payload(wrapped))[0]).ag == 1


def test_backtest_runs(league):
    seasons, _ = league
    res = backtest.run_sync("T1", 2025, seasons, [])
    assert res.n_matches > 200
    assert {b.strategy for b in res.bets} == {"mevcut", "piyasa"}
    lp = sum(res.ll[("gol", 0.0)]) / len(res.ll[("gol", 0.0)])
    lm = sum(res.ll[("gol", 0.4)]) / len(res.ll[("gol", 0.4)])
    assert abs(lm - lp) < 0.05 and len(res.ll[("sut", 1.0)]) == res.n_matches
    bs = backtest.best_setting(res.ll)
    assert bs and bs[0] in backtest.MODELS
    txt = backtest.format_report(res)
    assert "Backtest" in txt and "log-loss" in txt and "Strateji B" in txt
    print(re.sub(r"</?[bi]>", "", txt))
    summ = backtest.format_summary([res, res])
    assert "özeti" in summ
    print(re.sub(r"</?[bi]>", "", summ))
