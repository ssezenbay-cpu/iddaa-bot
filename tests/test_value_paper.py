"""Değer motoru seçimi ve kâğıt takip. Canlı eşikler ve sızıntı."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pandas as pd

from iddaabot import backtest_value as bv
from iddaabot import espn, paper, storage
from iddaabot.config import settings
from iddaabot.engine import Candidate


def _cand(odds, p, conf="orta"):
    ev = p * odds - 1
    spec = {"kind": "1x2", "subject": "goals", "period": "FT", "sel": "1"}
    return Candidate(market="MS 1", outcome="1", spec=spec, odds=odds, p=p, ev=ev,
                     conf=conf, margin=None, mbs=1)


def test_choose_takes_the_value_side_only():
    prob = {"MS 1": 0.55, "MS X": 0.25, "MS 2": 0.20, "2.5 Üst": 0.50, "2.5 Alt": 0.50}
    # 0.55 * 2.20 - 1 = 0.21, eşiğin üstünde. Beraberlik 0.25*3.4-1 negatif.
    price = {"MS 1": 2.20, "MS X": 3.40, "MS 2": 4.20, "2.5 Üst": 1.90, "2.5 Alt": 1.90}
    picked = bv.choose(prob, price, "orta", 1000)
    assert picked is not None
    assert picked[0] == "MS 1"
    assert picked[1].stake >= settings.min_stake
    assert bv.choose(prob, {k: 1.50 for k in price}, "orta", 1000) is None


def test_degenerate_odds_do_not_crash():
    prob = {"MS 1": 0.50, "MS X": 0.30, "MS 2": 0.20}
    price = {"MS 1": 1.02, "MS X": 1.02, "MS 2": 80.0}
    assert bv.choose(prob, price, "orta", 1000) is None


def test_haircut_can_remove_a_thin_edge():
    prob = {"MS 1": 0.48, "MS X": 0.28, "MS 2": 0.24}
    price = {"MS 1": 2.30, "MS X": 3.30, "MS 2": 3.60}
    # 0.48*2.30 - 1 = 0.104 geçer; 0.48*2.30*0.92 - 1 = 0.0158, orta eşik %5'in altında kalır.
    assert bv.choose(prob, price, "orta", 1000) is not None
    cheap = {k: v * bv.HAIRCUT for k, v in price.items()}
    assert bv.choose(prob, cheap, "orta", 1000) is None


def test_clv_sign():
    b = bv.VBet(2024, "tur.1", "Süper Lig", datetime(2024, 9, 1), "A - B", "MS", "MS 1",
                2.10, 0.55, 0.15, "orta", 20, True, 2.00, "pinnacle", "pinnacle")
    assert b.clv == (2.10 / 2.00 - 1)
    b2 = bv.VBet(2024, "tur.1", "Süper Lig", datetime(2024, 9, 1), "A - B", "MS", "MS 1",
                 1.90, 0.55, 0.04, "orta", 20, False, 2.00, "pinnacle", "pinnacle")
    assert b2.clv < 0


def test_walk_does_not_train_on_future_matches(monkeypatch):
    seen = []

    def fake_fit(train, ref):
        if len(train):
            seen.append((pd.Timestamp(ref), pd.Timestamp(train["Date"].max())))
        return None

    monkeypatch.setattr(bv, "_fit", fake_fit)
    rows = []
    for i, day in enumerate((1, 2, 8, 9)):
        rows.append(dict(Date=f"2024-09-{day:02d}", HomeTeam="H", AwayTeam="A", FTHG=1, FTAG=0,
                         HTHG=1, HTAG=0, PSH=1.8, PSD=3.6, PSA=4.5, B365H=2.4, B365D=3.3, B365A=3.2,
                         PSCH=1.7, PSCD=3.7, PSCA=5.0))
    df = pd.DataFrame(rows)
    bv.walk_league({2024: df}, "tur.1", "Süper Lig", (2024,))
    assert seen
    for ref, last in seen:
        assert last < ref


def test_paper_keeps_first_odds_and_updates_close(monkeypatch):
    storage.init()
    monkeypatch.setattr(settings, "paper_mode", True)
    ko = datetime.now(timezone.utc) - timedelta(hours=5)
    fx = type("F", (), {"espn_id": "p1", "league": "tur.1", "kickoff": ko, "home": "A", "away": "B"})()
    c = _cand(2.20, 0.55)
    c.kelly, c.stake = 0.08, 25
    a = type("A", (), {"fx": fx, "ev": True, "best": c, "candidates": [c]})()
    assert paper.observe([a]) == 1
    assert paper.observe([a]) == 0
    c.odds = 1.80
    assert paper.observe([a]) == 0
    row = storage.paper_all()[0]
    assert row["odds"] == 2.20
    assert row["close_odds"] == 1.80
    assert row["stake"] == 25

    async def board(league, day, ttl=900):
        return [espn.Fixture(espn_id="p1", league="tur.1", kickoff=ko, home="A", away="B",
                             home_id="1", away_id="2", state="post", completed=True,
                             status_name="STATUS_FULL_TIME", home_score=2, away_score=0,
                             ht_home=1, ht_away=0)]

    monkeypatch.setattr(espn, "scoreboard", board)
    assert asyncio.run(paper.settle_due()) == 1
    done = storage.paper_all()[0]
    assert done["status"] == "won"
    assert done["flat_profit"] == 2.20 - 1
    assert done["profit"] == 25 * (2.20 - 1)
    text = paper.report_text()
    assert "Kâğıt takip" in text and "Kapanışa göre" in text


def test_paper_off_records_nothing(monkeypatch):
    storage.init()
    monkeypatch.setattr(settings, "paper_mode", False)
    ko = datetime.now(timezone.utc) + timedelta(hours=3)
    fx = type("F", (), {"espn_id": "p2", "league": "tur.1", "kickoff": ko, "home": "C", "away": "D"})()
    c = _cand(2.10, 0.55)
    c.kelly, c.stake = 0.05, 15
    a = type("A", (), {"fx": fx, "ev": True, "best": c, "candidates": [c]})()
    assert paper.observe([a]) == 0
    assert all(r["espn_id"] != "p2" for r in storage.paper_all())


def test_kagit_command_toggles(monkeypatch):
    from iddaabot import bot
    storage.init()
    storage.set_setting("owner_chat_id", 7)
    monkeypatch.setattr(settings, "paper_mode", False)

    class Msg:
        def __init__(self):
            self.texts = []

        async def reply_text(self, text):
            self.texts.append(text)

    msg = Msg()
    upd = type("U", (), {"message": msg, "effective_chat": type("C", (), {"id": 7})()})()
    ctx = type("X", (), {"args": ["ac"]})()
    asyncio.run(bot.cmd_kagit(upd, ctx))
    assert settings.paper_mode is True
    assert storage.get_setting("paper_mode") is True
    ctx.args = ["kapat"]
    asyncio.run(bot.cmd_kagit(upd, ctx))
    assert settings.paper_mode is False
