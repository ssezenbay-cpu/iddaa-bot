import asyncio
import re
from datetime import datetime, timedelta, timezone

import pandas as pd

from iddaabot import bot, engine, espn, footballdata, gemini, iddaa, storage

from .test_core import CFG, sample_events


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)


def _patch(monkeypatch, league):
    seasons, _ = league
    ld = footballdata.build_league("T1", pd.concat([seasons[2024], seasons[2025]], ignore_index=True),
                                   seasons[2025], datetime(2026, 5, 1))
    ko = datetime.now(timezone.utc) + timedelta(hours=3)
    fx = [espn.Fixture(espn_id="777", league="tur.1", kickoff=ko, home="Galatasaray", away="Fenerbahçe",
                       home_id="432", away_id="436", state="pre", completed=False, status_name="STATUS_SCHEDULED",
                       ext_1x2=(1.80, 3.80, 4.40), ext_ou=(2.5, 1.80, 2.00), ext_provider="DraftKings"),
          espn.Fixture(espn_id="778", league="uefa.champions", kickoff=ko + timedelta(hours=1),
                       home="Bayern Munich", away="Galatasaray", home_id="1", away_id="432", state="pre",
                       completed=False, status_name="STATUS_SCHEDULED", ext_1x2=(1.30, 6.0, 9.0),
                       ext_ou=(3.5, 1.9, 1.9))]
    js = sample_events(ko)
    js2 = sample_events(ko + timedelta(hours=1))
    js2["data"]["events"][0].update({"i": 999, "hn": "Bayern Münih", "an": "Galatasaray"})
    js["data"]["events"] += js2["data"]["events"]
    evs = iddaa.parse_events(js, CFG, {})

    async def f_fixtures(leagues, start, days, ttl=600):
        return fx

    async def f_events():
        return evs

    async def f_load_all():
        return {"T1": ld}

    async def f_summary(league, eid, ttl=0):
        return espn.Summary(last5={"432": ["G 2-0 (İ) Konyaspor – Süper Lig"]}, referee="")

    async def f_news(key, home, away, league, when):
        return {"ev_eksikler": [{"oyuncu": "Oyuncu A", "durum": "sakat", "kilit": True}], "dep_eksikler": [],
                "ev_gol_carpani": 0.95, "dep_gol_carpani": 1.0, "notlar": "Test notu.", "kaynaklar": []}

    monkeypatch.setattr(espn, "fixtures", f_fixtures)
    monkeypatch.setattr(iddaa, "fetch_events", f_events)
    monkeypatch.setattr(footballdata, "load_all", f_load_all)
    monkeypatch.setattr(espn, "summary", f_summary)
    monkeypatch.setattr(gemini, "team_news", f_news)


def test_publish_and_analiz(monkeypatch, league):
    storage.init()
    _patch(monkeypatch, league)
    fb = FakeBot()
    asyncio.run(bot.publish(fb, 1, "Günün Analizi", 0))
    assert fb.sent and "Günün Analizi" in fb.sent[0]
    print(fb.sent[0])
    sc = asyncio.run(engine.scan(0))
    assert sc.n_fixtures == 2 and sc.n_iddaa == 2
    ucl = next(a for a in sc.analyses if a.fx.league == "uefa.champions")
    assert ucl.ev and ucl.ev.home == "Bayern Münih" and ucl.lam_dc is None and ucl.data_conf == "orta"
    a = asyncio.run(engine.find_fixture("fenerbahce"))
    assert a and a.fx.espn_id == "777"
    asyncio.run(engine.enrich(a, 1000))
    assert a.news and a.summary.last5


def test_late_scan_and_close_value(monkeypatch, league):
    from iddaabot import report, tracker
    from iddaabot.config import settings
    storage.init()
    _patch(monkeypatch, league)
    bot.apply_weight(0.0, "gol")
    monkeypatch.setattr(bot, "owner_id", lambda: 1)

    class Ctx:
        bot = FakeBot()
    ctx = Ctx()
    asyncio.run(bot.job_late(ctx))
    # ekstra: kapanış olasılığı yazıldı mı (önceden kaydedilmiş seçim varsa)
    for r in storage.pending_picks():
        assert r["p_close"] is None or 0 < r["p_close"] < 1
    if ctx.bot.sent:
        assert "Son dakika" in ctx.bot.sent[0]
    # sadece piyasa modunda istatistik modeli harmana girmez
    sc = asyncio.run(engine.scan(0))
    a = next(x for x in sc.analyses if x.fx.espn_id == "777")
    assert a.lam == a.lam_mkt
    assert "sadece piyasa" in report.mode_text()
    # korner/kart önerilere girmez
    assert all(c.spec.get("subject", "goals") == "goals" for c in a.picks)
    bot.apply_weight(0.4, "gol")


def test_risk_and_fatigue(league):
    from iddaabot import report
    ko = datetime.now(timezone.utc) + timedelta(hours=3)
    sm = espn.parse_summary({"boxscore": {"lastFiveGames": [
        {"team": {"id": "432"}, "events": [
            {"gameResult": "W", "score": "2-0", "gameDate": (ko - timedelta(days=3)).strftime("%Y-%m-%dT%H:%MZ")},
            {"gameResult": "L", "score": "0-1", "gameDate": (ko - timedelta(days=6)).strftime("%Y-%m-%dT%H:%MZ")},
            {"gameResult": "D", "score": "1-1", "gameDate": (ko - timedelta(days=13)).strftime("%Y-%m-%dT%H:%MZ")}]}]}})
    assert sm.fatigue("432", ko) == (3, 2)
    from iddaabot.markets import GoalModel
    a = engine.MatchAnalysis(fx=espn.Fixture(espn_id="1", league="tur.1", kickoff=ko, home="A", away="B",
                                             home_id="432", away_id="2", state="pre", completed=False,
                                             status_name=""), ev=None, league_name="Süper Lig")
    a.gm, a.lam = GoalModel(1.5, 1.0), (1.5, 1.0)
    c = engine.Candidate(market="Maç Sonucu", outcome="1", spec={"kind": "1x2", "sel": "1"}, odds=2.0,
                         p=0.5, ev=0.0, conf="orta", margin=None)
    rs = engine.risk_scenario(a, c)
    assert "1-1" in rs or "0-0" in rs
    a.summary = sm
    assert "Fikstür yükü" in report.detail(a)


def test_find_specific_match(monkeypatch, league):
    _patch(monkeypatch, league)
    a = asyncio.run(engine.find_fixture("galatasaray - bayern"))   # sıra ters yazıldı
    assert a and a.fx.espn_id == "778"
    a = asyncio.run(engine.find_fixture("fenerbahçe - galatasaray"))
    assert a and a.fx.espn_id == "777"
    a = asyncio.run(engine.find_fixture("galatasaray"))              # tek takım: en yakın maç
    assert a and a.fx.espn_id == "777"


def test_daily_report_shows_near_misses(monkeypatch, league):
    from iddaabot import report
    from iddaabot.config import LEAGUES, settings
    _patch(monkeypatch, league)
    assert "ned.1" in LEAGUES and "uefa.europa.conf" in LEAGUES
    bot.apply_weight(0.0, "gol")
    monkeypatch.setattr(settings, "min_ev", {"yüksek": 0.9, "orta": 0.9, "düşük": 0.9})  # hiç değer çıkmasın
    sc = asyncio.run(engine.scan(0))
    txt = report.daily(sc, "Günün Analizi", {}, [], 1000)
    assert "Uluslararası oranı olan: 2" in txt
    bot.apply_weight(0.4, "gol")


def test_teshis_runs(monkeypatch, league):
    from iddaabot import mackolik
    _patch(monkeypatch, league)
    monkeypatch.setattr(bot, "owner_id", lambda: 5)

    async def f_mk(mid, weeks, season=None):
        return []
    monkeypatch.setattr(mackolik, "fetch_league", f_mk)

    class Msg:
        async def reply_text(self, t, **k):
            pass

    class Chat:
        id = 5

    class Upd:
        effective_chat = Chat()
        message = Msg()

    class Ctx:
        bot = FakeBot()
        args = []
    ctx = Ctx()
    asyncio.run(bot.cmd_teshis(Upd(), ctx))
    out = "\n".join(ctx.bot.sent)
    assert "iddaa eşleştirme: 2/2" in out, out


def test_price_coupon_when_no_value(monkeypatch, league):
    from iddaabot import report
    from iddaabot.config import settings
    _patch(monkeypatch, league)
    bot.apply_weight(0.0, "gol")
    monkeypatch.setattr(settings, "min_ev", {"yüksek": 0.9, "orta": 0.9, "düşük": 0.9})
    sc = asyncio.run(engine.scan(0))
    assert not engine.singles(sc.analyses)
    cp = engine.price_coupon(sc.analyses, 5000)
    assert cp and len(cp.legs) >= 2 and len({a.fx.espn_id for a, _ in cp.legs}) == len(cp.legs)
    assert cp.stake == 50  # kasanın %1'i
    txt = report.coupon_block(cp, 1)
    assert "Günün Kuponu" in txt
    fb = FakeBot()
    storage.init()
    asyncio.run(bot.publish(fb, 1, "Günün Analizi", 0))
    assert "Günün Kuponu" in fb.sent[0]
    bot.apply_weight(0.4, "gol")


def test_stat_coupons(monkeypatch, league):
    from iddaabot import report
    from iddaabot.config import COUPON_LEAGUES
    _patch(monkeypatch, league)
    bot.apply_weight(0.0, "gol")
    assert "ned.1" not in COUPON_LEAGUES and "tur.1" in COUPON_LEAGUES
    sc = asyncio.run(engine.scan(0))
    gs = next(a for a in sc.analyses if a.fx.espn_id == "777")
    opts = engine.likely_options(gs)
    assert opts and all(c.p_stat is not None and c.p_mkt is not None for c in opts)
    assert all(c.p_safe == min(c.p_stat, c.p_mkt) for c in opts)
    assert opts[0].p_safe >= opts[-1].p_safe
    ucl = next(a for a in sc.analyses if a.fx.espn_id == "778")
    uo = engine.likely_options(ucl)
    assert uo and all(c.p_stat is None and c.p_mkt is not None for c in uo)   # Avrupa: istatistik yok
    cps = engine.stat_coupons(sc.analyses, 5000)
    assert cps, "kupon kurulmadı"
    for cp in cps:
        assert len({a.fx.espn_id for a, _ in cp.legs}) == len(cp.legs)
        assert abs(cp.p - __import__("math").prod(c.p_safe for _, c in cp.legs)) < 1e-12
        txt = report.coupon_block(cp, 1)
        assert "istatistik" in txt and "Tutma olasılığı" in txt
    fb = FakeBot()
    storage.init()
    asyncio.run(bot.publish(fb, 1, "Günün Analizi", 0))
    assert any(k in fb.sent[0] for k in ("Banko Kuponu", "Günün İstatistik Kuponu"))
    print(re.sub(r"<[^>]+>", "", fb.sent[0]))
    bot.apply_weight(0.4, "gol")


def test_value_off_by_choice(monkeypatch, league):
    from iddaabot.config import settings
    _patch(monkeypatch, league)
    bot.apply_weight(0.0, "gol")
    monkeypatch.setattr(settings, "value_picks", False)
    fb = FakeBot()
    storage.init()
    asyncio.run(bot.publish(fb, 1, "Günün Analizi", 0))
    txt = fb.sent[0]
    assert "Değerli seçimler" not in txt and "Değer Kuponu" not in txt
    assert "Banko Kuponu" in txt or "İstatistik Kuponu" in txt or "kupon kurulacak kadar" in txt
    bot.apply_weight(0.4, "gol")
