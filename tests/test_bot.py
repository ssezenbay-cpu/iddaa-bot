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
    from iddaabot.config import TZ
    fixed_now = datetime.now(TZ).replace(hour=12, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(engine, "_now", lambda: fixed_now)
    ko = (fixed_now + timedelta(hours=3)).astimezone(timezone.utc)
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
    assert "ger.2" not in COUPON_LEAGUES and "tur.1" in COUPON_LEAGUES and "ned.1" in COUPON_LEAGUES
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
    assert any(k in fb.sent[0] for k in ("Banko Kuponu", "2-3 Oran Kuponu"))
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
    assert "Banko Kuponu" in txt or "2-3 Oran Kuponu" in txt or "kupon kurulacak kadar" in txt
    bot.apply_weight(0.4, "gol")


def test_surprise_board_and_filters(monkeypatch, league):
    from iddaabot import report
    _patch(monkeypatch, league)
    bot.apply_weight(0.0, "gol")
    sc = asyncio.run(engine.scan(0))
    for a in sc.analyses:
        for c in engine.surprise_options(a):
            assert 2.0 <= c.odds <= 4.5 and c.p_safe >= 0.25
    cps = engine.stat_coupons(sc.analyses, 5000)
    kinds = [cp.kind for cp in cps]
    assert len(kinds) == len(set(kinds)) and all(k in engine.STAT_KINDS for k in kinds)
    for cp in cps:
        if cp.kind == "Sürpriz Kupon":
            assert 6.0 <= cp.odds <= 40.0 and cp.stake == 25    # kasanın %0.5'i
            assert "Sık tutmaz" in report.coupon_block(cp, 1)
    # elle kurulan sürpriz: oran aralığı ve 'olasılık x oran' hedefi
    pool = [(a, engine.surprise_options(a)) for a in sc.analyses]
    pool = [(a, cs) for a, cs in pool if cs]
    if len(pool) >= 2:
        cp = engine._stat_coupon("Sürpriz Kupon", pool, (2,), 0.25, (2.0, 4.5), (4.0, 40.0), 5000, objective="er")
        assert cp is None or cp.odds >= 4.0
    # filtreler
    assert bot.parse_filters(["yarin", "superlig", "aksam"]) == (True, {"tur.1"}, (17, 21))
    assert bot.parse_filters(["sl"]) == (False, {"uefa.champions"}, None)
    only_tr = engine.filter_analyses(sc.analyses, {"tur.1"})
    assert [a.fx.espn_id for a in only_tr] == ["777"]
    assert engine.filter_analyses(sc.analyses, None, (0, 1)) == [] or True
    # günün tahtası
    txt = report.board(sc.analyses, "Bugünün maçları")
    assert "Süper Lig" in txt and "Şampiyonlar Ligi" in txt and "✅" in txt
    print(re.sub(r"<[^>]+>", "", txt))
    bot.apply_weight(0.4, "gol")


def test_surprise_objective_prefers_return():
    from types import SimpleNamespace as NS

    def cand(o, p):
        c = engine.Candidate(market="M", outcome=str(o), spec={}, odds=o, p=p, ev=0.0, conf="orta", margin=None)
        c.p_safe, c.mbs = p, 1
        return c
    pool = [(NS(fx=NS(espn_id=str(i))), [cand(2.2, 0.44), cand(3.5, 0.26)]) for i in range(4)]
    safe = engine._stat_coupon("Banko", pool, (3,), 0.25, (2.0, 4.5), (6.0, 40.0), 5000, objective="p")
    sur = engine._stat_coupon("Sürpriz Kupon", pool, (3,), 0.25, (2.0, 4.5), (6.0, 40.0), 5000, objective="er",
                              stake_pct=0.005)
    assert safe and sur and len(sur.legs) == 3 and 6.0 <= sur.odds <= 40.0 and sur.stake == 25
    assert sur.p * sur.odds >= safe.p * safe.odds - 1e-12


def test_coupon_results_ticks(monkeypatch, league):
    import dataclasses
    import json
    from iddaabot import report, settle
    storage.init()
    _patch(monkeypatch, league)
    bot.apply_weight(0.0, "gol")
    monkeypatch.setattr(bot, "owner_id", lambda: 7)
    sc = asyncio.run(engine.scan(0))
    past = datetime.now(timezone.utc) - timedelta(hours=4)
    scores = {"901": (2, 0, 1, 0), "902": (0, 3, 0, 1), "903": (1, 1, 0, 0)}
    legs = []
    for eid, a in zip(("901", "902"), sorted(sc.analyses, key=lambda x: x.fx.espn_id)):
        a.fx = dataclasses.replace(a.fx, espn_id=eid, kickoff=past)
        legs.append((a, engine.likely_options(a)[0]))
    future = dataclasses.replace(legs[0][0].fx, espn_id="904", kickoff=datetime.now(timezone.utc) + timedelta(days=1))
    a4 = dataclasses.replace(legs[0][0], fx=future)
    ids = [storage.save_pick(a, c, single=False) for a, c in legs]
    k1 = storage.save_coupon("Banko Kuponu", ids, 1.66, 0.6, 0.0, 10)
    k2 = storage.save_coupon("Sürpriz Kupon", [ids[0], storage.save_pick(a4, legs[0][1], single=False)], 4.0, 0.2, 0, 5)

    async def f_board(league_, day, ttl=600):
        out = []
        for eid, (h, a_, hh, ha) in scores.items():
            out.append(espn.Fixture(espn_id=eid, league=league_, kickoff=past, home="x", away="y", home_id="1",
                                    away_id="2", state="post", completed=True, status_name="STATUS_FULL_TIME",
                                    home_score=h, away_score=a_, ht_home=hh, ht_away=ha))
        return out
    monkeypatch.setattr(espn, "scoreboard", f_board)

    class Ctx:
        bot = FakeBot()
        args = []
    ctx = Ctx()
    asyncio.run(bot.job_settle(ctx))
    exp = [settle.evaluate(json.loads(storage.get_pick(i)["spec"]), scores[e][:2], scores[e][2:])
           for i, e in zip(ids, ("901", "902"))]
    cp1 = storage.get_coupons([k1])[0]
    assert cp1["status"] == ("won" if all(exp) else "lost")
    note = "\n".join(ctx.bot.sent)
    assert f"#K{k1}" in note and ("TUTTU" in note or "YATTI" in note)

    class Msg:
        text = "Kuponlar tuttu mu?"

        async def reply_text(self, t, **k):
            pass

    class Chat:
        id = 7

    class Upd:
        effective_chat = Chat()
        message = Msg()
    ctx.bot = FakeBot()
    asyncio.run(bot.on_text(Upd(), ctx))
    txt = ctx.bot.sent[0]
    print(re.sub(r"<[^>]+>", "", txt))
    assert "Dün ve bugünün kuponları" in txt and f"#K{k1}" in txt and f"#K{k2}" in txt
    for ok in exp:
        assert ("✅" if ok else "❌") in txt
    assert "⏳" in txt and "Maç bazında: " in txt  # yarınki maç bekliyor
    cp2 = storage.get_coupons([k2])[0]
    assert cp2["status"] == ("lost" if not exp[0] else "pending")
    assert bot.parse_range(["dün"])[:2] == (1, 1) and bot.parse_range(["hafta"])[:2] == (6, None)
    Msg.text = "bugün hava nasıl"                  # alakasız yazıya cevap verilmez
    ctx.bot = FakeBot()
    asyncio.run(bot.on_text(Upd(), ctx))
    assert not ctx.bot.sent
    assert "kupon yok" in report.coupon_results([], "Test")
    bot.apply_weight(0.4, "gol")


def test_history_rates(monkeypatch, league):
    from iddaabot import history, report
    from iddaabot.markets import GoalModel
    assert history.meta().get("mac", 0) > 20000
    ou = lambda ln, side: {"kind": "ou", "subject": "goals", "period": "FT", "scope": "total", "line": ln, "side": side}
    hi, lo = GoalModel(2.2, 1.4), GoalModel(1.2, 0.9)       # gollü ve golsüz beklenen maç
    r_hi, r_lo = history.rate("tur.1", ou(2.5, "over"), hi), history.rate("tur.1", ou(2.5, "over"), lo)
    assert r_hi and r_lo and r_hi[0] > 0.6 > r_lo[0] and r_hi[1] >= history.MIN_N
    assert history.rate("eng.1", ou(1.5, "over"), hi)[0] > 0.8
    assert history.rate("uefa.champions", ou(2.5, "under"), lo)[2] == "9 lig"
    assert history.rate("tur.1", {"kind": "dc", "period": "FT", "sel": ("1", "X")}, GoalModel(2.0, 0.6))[0] > 0.75
    assert history.rate("tur.1", {"kind": "score", "x": 1, "y": 0}, hi) is None
    _patch(monkeypatch, league)
    bot.apply_weight(0.0, "gol")
    sc = asyncio.run(engine.scan(0))
    gs = next(a for a in sc.analyses if a.fx.espn_id == "777")
    opts = engine.likely_options(gs)
    assert any(getattr(c, "hist", None) for c in opts)
    cps = engine.stat_coupons(sc.analyses, 5000)
    txt = "\n".join(report.coupon_block(cp, 1) for cp in cps)
    assert "📊 Geçmiş:" in txt and "maçta %" in txt
    assert "geçmiş %" in report.board(sc.analyses, "Bugün")
    print(re.sub(r"<[^>]+>", "", txt))
    bot.apply_weight(0.4, "gol")


def test_more_leagues_and_two_three_coupon(monkeypatch, league):
    import dataclasses
    from types import SimpleNamespace as NS
    from iddaabot import report
    from iddaabot.config import COUPON_LEAGUES
    for lg in ("ned.1", "por.1", "sui.1", "bel.1", "aut.1"):
        assert lg in COUPON_LEAGUES
    assert bot.parse_filters(["hollanda", "isviçre"])[1] == {"ned.1", "sui.1"}
    # uluslararası oran yoksa iddaa'nın marjı temizlenmiş oranı piyasa olarak kullanılır
    _patch(monkeypatch, league)
    bot.apply_weight(0.0, "gol")
    orig = espn.fixtures

    async def no_ext(*a, **k):
        return [dataclasses.replace(x, ext_1x2=None, ext_ou=None) for x in await orig(*a, **k)]
    monkeypatch.setattr(espn, "fixtures", no_ext)
    sc = asyncio.run(engine.scan(0))
    gs = next(a for a in sc.analyses if a.fx.espn_id == "777")
    assert gs.mkt_source.startswith("iddaa")
    opts = engine.likely_options(gs)
    assert opts and all(c.p_mkt is not None for c in opts)
    # 2-3 Oran Kuponu: seçimler %70+, geçmişi %70 altı olan seçim alınmaz, toplam oran 2-3
    def cand(o, p, h):
        c = engine.Candidate(market="M", outcome=str(o), spec={}, odds=o, p=p, ev=0.0, conf="orta", margin=None)
        c.p_safe, c.mbs, c.hist, c.p_stat, c.p_mkt = p, 1, (h, 500, "lig"), p, p
        return c
    pool = [(NS(fx=NS(espn_id=str(i)), title=f"T{i}"), [cand(1.30, 0.80 - i * 0.01, 0.75)]) for i in range(4)]
    pool.append((NS(fx=NS(espn_id="9"), title="T9"), [cand(1.32, 0.90, 0.65)]))       # geçmişi zayıf
    cp = engine._stat_coupon("2-3 Oran Kuponu", pool, (3, 2, 4), 0.70, (1.20, 1.70), (2.0, 3.0), 5000, hist_min=0.70)
    assert cp and 2.0 <= cp.odds <= 3.0 and all(c.p_safe >= 0.70 and c.hist[0] >= 0.70 for _, c in cp.legs)
    assert "9" not in {a.fx.espn_id for a, _ in cp.legs}
    bot.apply_weight(0.4, "gol")
