"""Analiz motoru: fikstür + iddaa oranları + modeller -> her seçeneğin değeri -> öneriler ve kuponlar."""
from __future__ import annotations

import itertools
import re
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import espn, footballdata, gemini, history, iddaa
from .config import COUPON_LEAGUES, LEAGUES, TZ, settings
from .markets import CountModel, GoalModel, devig, fit_lambdas, overround, selection_prob
from .matching import assign, pair_score, similarity

log = logging.getLogger(__name__)

CONF_ORDER = {"düşük": 0, "orta": 1, "yüksek": 2}
ORDER_CONF = {v: k for k, v in CONF_ORDER.items()}
# Pazar türüne göre azami güven (model varsayımlarına ne kadar dayandığına göre)
TIER_CAP = {
    "1x2": "yüksek", "dc": "yüksek", "btts": "yüksek", "handicap": "yüksek",
    "ou": "yüksek", "combo": "orta", "win_to_nil": "orta", "win_both": "düşük",
    "htft": "düşük", "score": "düşük", "range": "düşük", "oddeven": "düşük",
    "half_more": "düşük", "first_goal": "düşük",
}
KIND_TR = {
    "1x2": "Maç Sonucu", "dc": "Çifte Şans", "ou": "Alt/Üst", "btts": "Karşılıklı Gol",
    "handicap": "Handikap", "htft": "İY/MS", "score": "Skor", "range": "Gol Aralığı",
    "oddeven": "Tek/Çift", "combo": "Kombine", "half_more": "Yarı Golü", "win_both": "Her İki Yarı",
    "win_to_nil": "Gol Yemeden", "first_goal": "İlk Gol",
}


def min_conf(a: str, b: str) -> str:
    return ORDER_CONF[min(CONF_ORDER[a], CONF_ORDER[b])]


@dataclass
class Candidate:
    market: str
    outcome: str
    spec: dict
    odds: float
    p: float
    ev: float
    conf: str
    margin: float | None
    kelly: float = 0.0
    stake: float = 0.0
    growth: float = 0.0
    mbs: int = 1

    @property
    def label(self) -> str:
        return f"{self.market}: {self.outcome}"

    @property
    def kind(self) -> str:
        k = self.spec["kind"]
        subj = self.spec.get("subject", "goals")
        return f"{k}:{subj}" if subj != "goals" else k


@dataclass
class MatchAnalysis:
    fx: espn.Fixture
    ev: iddaa.IddaaEvent | None
    league_name: str
    ld: footballdata.LeagueData | None = None
    fd_home: str | None = None
    fd_away: str | None = None
    lam_mkt: tuple | None = None
    mkt_source: str = ""
    lam_dc: tuple | None = None
    lam: tuple | None = None
    gm: GoalModel | None = None
    corners: CountModel | None = None
    cards: CountModel | None = None
    data_conf: str = "düşük"
    agreement: float | None = None
    news: dict | None = None
    referee: str = ""
    candidates: list[Candidate] = field(default_factory=list)
    picks: list[Candidate] = field(default_factory=list)   # eşik üstü, büyümeye göre sıralı
    n_markets: int = 0
    n_priced: int = 0
    n_suspect: int = 0
    notes: list[str] = field(default_factory=list)
    base_notes: list[str] = field(default_factory=list)
    summary: espn.Summary | None = None

    @property
    def best(self) -> Candidate | None:
        return self.picks[0] if self.picks else None

    @property
    def title(self) -> str:
        return f"{self.fx.home} – {self.fx.away}"


# ----------------------------------------------------------------- yardımcılar
def _ms_and_ou(ev: iddaa.IddaaEvent):
    """iddaa bülteninden MS (1,X,2) ve 2.5 alt/üst oranları."""
    ms = ou = None
    for m in ev.markets:
        t = m.mtype or {}
        if t.get("type") == "1x2" and t.get("subject") == "goals" and t.get("period") == "FT":
            d = {o.spec["sel"]: o.odd for o in m.outcomes if o.spec}
            if set(d) == {"1", "X", "2"}:
                ms = (d["1"], d["X"], d["2"])
        if (t.get("type") == "ou" and t.get("subject") == "goals" and t.get("period") == "FT"
                and t.get("scope") == "total" and t.get("line") == 2.5):
            d = {o.spec["side"]: o.odd for o in m.outcomes if o.spec}
            if set(d) == {"over", "under"}:
                ou = (2.5, d["over"], d["under"])
    return ms, ou


def market_lambdas(x12, ou, prior_total=2.65):
    if not x12:
        return None
    p = devig(list(x12))
    pou = (ou[0], devig([ou[1], ou[2]])[0]) if ou else None
    return fit_lambdas(tuple(p), pou, prior_total)


def tv_distance(l1, l2) -> float:
    a, b = GoalModel(*l1).summary(), GoalModel(*l2).summary()
    return 0.5 * sum(abs(a[k] - b[k]) for k in ("1", "X", "2"))


def kelly_fraction(p: float, o: float) -> float:
    return max((p * o - 1) / (o - 1), 0.0) if o > 1 else 0.0


def stake_for(p: float, o: float, bankroll: float, combo: bool = False) -> tuple[float, float]:
    k = kelly_fraction(p, o)
    cap = settings.max_stake_pct / (2 if combo else 1)
    f = min(k * settings.kelly_fraction, cap)
    stake = bankroll * f
    stake = max(round(stake / 5) * 5, settings.min_stake) if f > 0 else 0.0
    return k, stake


def growth(p: float, o: float, f: float) -> float:
    if f <= 0 or f >= 1:
        return 0.0
    return p * math.log(1 + f * (o - 1)) + (1 - p) * math.log(1 - f)


# ----------------------------------------------------------------- tek maç analizi
def analyse(ma: MatchAnalysis, bankroll: float) -> MatchAnalysis:
    fx, ev, ld = ma.fx, ma.ev, ma.ld
    ma.candidates, ma.picks, ma.n_markets, ma.n_priced, ma.n_suspect = [], [], 0, 0, 0
    ma.notes = list(ma.base_notes)
    ma.lam_mkt = ma.lam_dc = ma.lam = None
    ma.agreement = None
    prior_total = ld.model.league_avg_goals if ld and ld.model else 2.65
    iddaa_ms, iddaa_ou = _ms_and_ou(ev) if ev else (None, None)

    # 1) piyasa bileşeni: önce uluslararası oran, yoksa iddaa'nın marjı temizlenmiş oranı
    ext = False
    if fx.ext_1x2:
        ma.lam_mkt = market_lambdas(fx.ext_1x2, fx.ext_ou, prior_total)
        ma.mkt_source = f"uluslararası piyasa ({fx.ext_provider or 'ESPN'})"
        ext = True
    elif iddaa_ms:
        ma.lam_mkt = market_lambdas(iddaa_ms, iddaa_ou, prior_total)
        ma.mkt_source = "iddaa (marjı temizlenmiş)"

    # 2) istatistik modeli (aynı ligde oynayan takımlar)
    if ld and ma.fd_home and ma.fd_away:
        ma.lam_dc = ld.goal_lambdas(ma.fd_home, ma.fd_away)
    if ma.lam_dc and ma.news:
        ma.lam_dc = (ma.lam_dc[0] * ma.news.get("ev_gol_carpani", 1.0),
                     ma.lam_dc[1] * ma.news.get("dep_gol_carpani", 1.0))

    # 3) harman + güven
    if ma.lam_mkt and ma.lam_dc:
        wm, wd = settings.w_market, settings.w_dc
        s = wm + wd
        ma.lam = tuple(math.exp((wm * math.log(m) + wd * math.log(d)) / s)
                       for m, d in zip(ma.lam_mkt, ma.lam_dc))
        ma.agreement = tv_distance(ma.lam_mkt, ma.lam_dc)
        if ext:
            ma.data_conf = "yüksek" if ma.agreement < 0.06 else ("orta" if ma.agreement < 0.12 else "düşük")
        else:
            ma.data_conf = "orta" if ma.agreement < 0.08 else "düşük"
    elif ma.lam_mkt and ext:
        ma.lam, ma.data_conf = ma.lam_mkt, "orta"
    elif ma.lam_dc:
        ma.lam, ma.data_conf = ma.lam_dc, "düşük"
    elif ma.lam_mkt:
        ma.lam, ma.data_conf = ma.lam_mkt, "yok"
        ma.notes.append("Bu maç için iddaa dışında bağımsız veri yok; değer hesaplanamaz.")
    if ma.lam and not ext and settings.w_dc == 0:
        ma.data_conf = "yok"  # sadece piyasa modunda iddaa'yı kendisiyle kıyaslamak anlamsız
        if not any("uluslararası oran yok" in n for n in ma.notes):
            ma.notes.append("Uluslararası oran yok; sadece piyasa modunda bu maç için değer aranmaz.")
    if not ma.lam:
        ma.notes.append("Model kurulamadı (oran ve istatistik yok).")
        return ma

    rho = ld.model.rho if ld and ld.model else -0.06
    h1 = ld.h1_share if ld else 0.44
    ma.gm = GoalModel(ma.lam[0], ma.lam[1], rho, h1)
    if ld and ma.fd_home and ma.fd_away:
        ma.corners = ld.count_model("corners", ma.fd_home, ma.fd_away)
        ma.cards = ld.count_model("cards", ma.fd_home, ma.fd_away, ma.referee or None)

    if not ev:
        ma.notes.append("iddaa bülteninde bulunamadı.")
        return ma

    # 4) bültendeki her seçeneği fiyatla
    for m in ev.markets:
        ma.n_markets += 1
        if not m.mtype:
            continue
        specs = [(o, o.spec) for o in m.outcomes if o.spec]
        if not specs:
            continue
        probs = [(o, sp, selection_prob(sp, ma.gm, ma.corners, ma.cards)) for o, sp in specs]
        probs = [x for x in probs if x[2] is not None]
        if not probs:
            continue
        full = len(probs) == len(m.outcomes) and abs(sum(p for _, _, p in probs) - 1) < 0.03
        margin = overround([o.odd for o in m.outcomes]) if full else None
        if full and len(probs) >= 2:
            # Güvenlik: model ile bahis şirketi bu pazarda kökten ayrışıyorsa (30+ puan) pazarı büyük
            # ihtimalle yanlış yorumluyoruzdur (yön, devre, takım). Gerçek değer bu kadar büyük olmaz.
            fair = devig([o.odd for o, _, _ in probs])
            tv = 0.5 * sum(abs(p - q) for (_, _, p), q in zip(probs, fair))
            if tv > 0.25:
                ma.n_suspect += 1
                continue
        ma.n_priced += 1
        for o, sp, p in probs:
            subj = sp.get("subject", "goals")
            if subj == "corners":
                conf = min_conf(ma.data_conf if ma.data_conf != "yok" else "düşük", "orta")
            elif subj == "cards":
                conf = "düşük"
            else:
                if ma.data_conf == "yok":
                    continue
                conf = min_conf(ma.data_conf, TIER_CAP.get(sp["kind"], "düşük"))
            c = Candidate(market=m.name, outcome=o.name, spec=sp, odds=o.odd, p=p,
                          ev=p * o.odd - 1, conf=conf, margin=margin, mbs=m.mbs)
            c.kelly, c.stake = stake_for(p, o.odd, bankroll)
            c.growth = growth(p, o.odd, min(c.kelly * settings.kelly_fraction, settings.max_stake_pct))
            ma.candidates.append(c)

    ma.picks = sorted((c for c in ma.candidates if is_value(c)), key=lambda c: -c.growth)
    sus = [c for c in ma.candidates if c.ev > settings.max_ev and settings.min_odds <= c.odds <= settings.max_odds]
    if sus:
        c = max(sus, key=lambda c: c.ev)
        ma.notes.append(f"{c.market}: {c.outcome} seçeneğinde model ile oran arasında aşırı fark "
                        f"(%{c.ev * 100:.0f}). Gerçek değer değil, büyük ihtimalle veri/eşleştirme hatası; önerilmedi.")
    return ma


def is_core(c: Candidate) -> bool:
    sp = c.spec
    return (sp.get("subject", "goals") == "goals" and sp.get("period", "FT") == "FT"
            and TIER_CAP.get(sp["kind"]) == "yüksek" and sp.get("scope", "total") == "total")


def is_value(c: Candidate) -> bool:
    if c.spec.get("subject", "goals") != "goals" and not settings.allow_count_markets:
        return False
    if settings.core_only and c.spec.get("subject", "goals") == "goals" and not is_core(c):
        return False
    return (settings.min_odds <= c.odds <= settings.max_odds and c.p >= settings.min_prob
            and settings.min_ev[c.conf] <= c.ev <= settings.max_ev)


def risk_scenario(a: MatchAnalysis, c: Candidate, n: int = 2) -> str:
    """Bu seçimi kaybettiren en olası skorlar (modelden, uydurma senaryo değil)."""
    from .settle import evaluate
    if not a.gm or c.spec.get("subject", "goals") != "goals":
        return ""
    H, A = a.gm.v["H"], a.gm.v["A"]
    rows = []
    for x in range(7):
        for y in range(7):
            r = evaluate(c.spec, (x, y), None)
            if r is False:
                rows.append((a.gm.p((H == x) & (A == y)), f"{x}-{y}"))
    if not rows:
        return ""
    rows.sort(reverse=True)
    lose = 1 - c.p
    return (f"Kaybetme ihtimali %{lose * 100:.0f}; en olası kaybettiren skorlar: "
            + ", ".join(f"{s} (%{p * 100:.0f})" for p, s in rows[:n]))


# ----------------------------------------------------------------- günlük tarama
async def _map_fd_names(analyses: list[MatchAnalysis], leagues: dict[str, footballdata.LeagueData]):
    by_lg: dict[str, list[MatchAnalysis]] = {}
    for a in analyses:
        by_lg.setdefault(a.fx.league, []).append(a)
    for lg, items in by_lg.items():
        code = LEAGUES[lg][1]
        if code and code in leagues:
            ld = leagues[code]
            names = sorted({x for a in items for x in (a.fx.home, a.fx.away)})
            mp = assign(names, ld.teams)
            for a in items:
                a.ld, a.fd_home, a.fd_away = ld, mp.get(a.fx.home), mp.get(a.fx.away)
        elif lg.startswith("uefa."):  # Avrupa kupası: takımlar farklı liglerden
            for a in items:
                a.base_notes.append("Avrupa maçı: takımlar farklı liglerden; model piyasa ağırlıklı, "
                                    "korner/kart modellenmez.")
        else:
            for a in items:
                a.base_notes.append("Bu lig için ücretsiz istatistik verisi yok; analiz piyasa oranlarına dayanır.")


def home_league_stats(team: str, leagues: dict[str, footballdata.LeagueData]):
    best = (0.0, None, None)
    for ld in leagues.values():
        for t in ld.teams:
            s = similarity(team, t)
            if s > best[0]:
                best = (s, ld, t)
    return (best[1], best[2]) if best[0] >= 90 else (None, None)


def match_iddaa(fx: espn.Fixture, events: list[iddaa.IddaaEvent]) -> iddaa.IddaaEvent | None:
    best, score = None, 0.0
    for e in events:
        if abs((e.kickoff - fx.kickoff).total_seconds()) > 25 * 60:
            continue
        s = pair_score(fx.home, fx.away, e.home, e.away)
        if s > score:
            best, score = e, s
    return best if score >= 140 else None


@dataclass
class DayScan:
    analyses: list[MatchAnalysis]
    n_fixtures: int
    n_iddaa: int
    iddaa_ok: bool
    errors: list[str] = field(default_factory=list)


def _now() -> datetime:
    """Şimdiki zaman (TR). Testlerde sabitlenebilsin diye ayrı fonksiyon."""
    return datetime.now(TZ)


async def scan(days_ahead: int | None = None, start: datetime | None = None,
               only_upcoming: bool = True, bankroll: float | None = None) -> DayScan:
    days_ahead = settings.days_ahead if days_ahead is None else days_ahead
    bankroll = bankroll if bankroll is not None else settings.bankroll
    real_now = _now()
    now_tr = start or real_now
    end_tr = (now_tr + timedelta(days=days_ahead)).replace(hour=23, minute=59, second=59)
    errors = []
    fx = await espn.fixtures(list(LEAGUES), (now_tr - timedelta(days=1)).date(), days_ahead + 1)
    now_utc = real_now.astimezone(timezone.utc)
    fx = [f for f in fx if f.kickoff <= end_tr.astimezone(timezone.utc)
          and f.kickoff.astimezone(TZ).date() >= now_tr.date()
          and (not only_upcoming or (f.state == "pre" and f.kickoff > now_utc))]
    try:
        events = await iddaa.fetch_events()
        iddaa_ok = True
    except Exception as e:  # noqa: BLE001
        events, iddaa_ok = [], False
        errors.append(f"iddaa bülteni alınamadı: {e}")
    leagues = await footballdata.load_all()
    analyses = []
    for f in sorted(fx, key=lambda z: z.kickoff):
        analyses.append(MatchAnalysis(fx=f, ev=match_iddaa(f, events), league_name=LEAGUES[f.league][0]))
    await _map_fd_names(analyses, leagues)
    for a in analyses:
        analyse(a, bankroll)
    return DayScan(analyses=analyses, n_fixtures=len(fx),
                   n_iddaa=sum(1 for a in analyses if a.ev), iddaa_ok=iddaa_ok, errors=errors)


# ----------------------------------------------------------------- kuponlar
@dataclass
class Coupon:
    kind: str
    legs: list[tuple[MatchAnalysis, Candidate]]
    odds: float
    p: float
    ev: float
    stake: float


def _coupon(kind, legs, bankroll) -> Coupon:
    o = math.prod(c.odds for _, c in legs)
    p = math.prod(c.p for _, c in legs)
    _, st = stake_for(p, o, bankroll, combo=True)
    return Coupon(kind=kind, legs=list(legs), odds=o, p=p, ev=p * o - 1, stake=st)


def build_coupons(analyses: list[MatchAnalysis], bankroll: float) -> list[Coupon]:
    out = []
    # Değer kuponu: her maçın en iyi değerli seçimi, 2-3 maç, MBS kuralına uygun
    pool = [(a, c) for a, c in singles(analyses) if a.ev][:10]
    best, best_g = None, 0.0
    for k in (2, 3):
        for legs in itertools.combinations(pool, k):
            if max(c.mbs for _, c in legs) > k:
                continue
            cp = _coupon("Değer Kuponu", legs, bankroll)
            if cp.p < 0.15 or cp.odds > 12:
                continue
            f = min(kelly_fraction(cp.p, cp.odds) * settings.kelly_fraction, settings.max_stake_pct / 2)
            g = growth(cp.p, cp.odds, f)
            if g > best_g:
                best, best_g = cp, g
    if best:
        out.append(best)
    # Güven kuponu: yüksek olasılıklı ve kendi başına eksi beklentili olmayan seçimler
    safe = []
    for a in analyses:
        if not a.ev:
            continue
        cs = [c for c in a.candidates if c.p >= 0.62 and c.odds >= 1.25 and c.ev >= 0.0
              and c.conf != "düşük"]
        if cs:
            safe.append((a, max(cs, key=lambda c: (c.p * c.odds, c.p))))
    safe = sorted(safe, key=lambda x: -x[1].p)[:8]
    bestg = None
    for k in (2, 3):
        for legs in itertools.combinations(safe, k):
            if max(c.mbs for _, c in legs) > k:
                continue
            cp = _coupon("Güven Kuponu", legs, bankroll)
            if cp.odds < 1.9 or cp.ev < 0:
                continue
            if bestg is None or cp.p > bestg.p:
                bestg = cp
    if bestg:
        out.append(bestg)
    return out


# ----------------------------------------------------------------- istatistik kuponları
def stat_lambdas(a: MatchAnalysis):
    """Takım istatistiklerinden (gol + isabetli şut) beklenen goller; haber çarpanları dahil."""
    if not (a.ld and a.fd_home and a.fd_away):
        return None
    lam = a.ld.goal_lambdas(a.fd_home, a.fd_away, "karma")
    if lam and a.news:
        lam = (lam[0] * a.news.get("ev_gol_carpani", 1.0), lam[1] * a.news.get("dep_gol_carpani", 1.0))
    return lam


def likely_options(a: MatchAnalysis, min_odds: float = 1.25) -> list[Candidate]:
    """Bir maçın ana pazarlarındaki seçenekleri, istatistik VE piyasa olasılığıyla birlikte döndürür.
    c.p_stat: takım istatistiklerine göre, c.p_mkt: maç oranlarına göre, c.p_safe: ikisinin düşüğü.
    İkisinin de yüksek dediği seçenek seçilir; tek bir modelin abarttığı seçenek kupona girmez."""
    if not a.ev or a.fx.league not in COUPON_LEAGUES:
        return []
    rho = a.ld.model.rho if a.ld and a.ld.model else -0.06
    h1 = a.ld.h1_share if a.ld else 0.44
    lam_s = stat_lambdas(a)
    gm_s = GoalModel(*lam_s, rho, h1) if lam_s else None
    gm_m = GoalModel(*a.lam_mkt, rho, h1) if a.lam_mkt and a.mkt_source.startswith("uluslararası") else None
    if not gm_s and not gm_m:
        return []
    gm_h = gm_m or (GoalModel(*a.lam_mkt, rho, h1) if a.lam_mkt else None)   # geçmiş tablosu için oran modeli
    out = []
    for c in a.candidates:
        if not is_core(c) or c.odds < min_odds:
            continue
        ps = selection_prob(c.spec, gm_s, None, None) if gm_s else None
        pm = selection_prob(c.spec, gm_m, None, None) if gm_m else None
        vals = [x for x in (ps, pm) if x is not None]
        if not vals:
            continue
        c.p_stat, c.p_mkt, c.p_safe = ps, pm, min(vals)
        c.hist = history.rate(a.fx.league, c.spec, gm_h)   # (oran, maç sayısı, kapsam) ya da None
        out.append(c)
    return sorted(out, key=lambda c: (-c.p_safe, -c.odds))


STAT_KINDS = ("Banko Kuponu", "Günün İstatistik Kuponu", "Sürpriz Kupon")


def _stat_coupon(kind, pool, sizes, leg_min_p, leg_odds, total_odds, bankroll, objective="p",
                 stake_pct: float | None = None):
    """objective='p': tutma olasılığı en yüksek kombinasyon; 'er': olasılık x oran en yüksek (sürpriz)."""
    best, best_p = None, -1.0
    for k in sizes:
        for matches in itertools.combinations(pool, k):
            opts = [[c for c in cs if c.p_safe >= leg_min_p and leg_odds[0] <= c.odds <= leg_odds[1]][:3]
                    for _, cs in matches]
            if any(not o for o in opts):
                continue
            for choice in itertools.product(*opts):
                if max(c.mbs for c in choice) > k:
                    continue
                o = math.prod(c.odds for c in choice)
                if not total_odds[0] <= o <= total_odds[1]:
                    continue
                p = math.prod(c.p_safe for c in choice)
                score = p if objective == "p" else p * o
                if score > best_p + 1e-12:
                    best_p = score
                    best = [(a, c) for (a, _), c in zip(matches, choice)]
    if not best:
        return None
    o = math.prod(c.odds for _, c in best)
    p = math.prod(c.p_safe for _, c in best)
    pct = settings.fun_stake_pct if stake_pct is None else stake_pct
    st = max(round(bankroll * pct / 5) * 5, settings.min_stake)
    return Coupon(kind=kind, legs=best, odds=o, p=p, ev=p * o - 1, stake=st)


def surprise_options(a: MatchAnalysis) -> list[Candidate]:
    """Oranı yüksek (2.00-4.50) ama istatistik ve piyasanın ikisinin de 'olabilir' dediği seçenekler:
    deplasman galibiyeti, beraberlik, KG var, 3.5 üst, handikap gibi. Olasılık x oran'a göre sıralı."""
    opts = [c for c in likely_options(a, min_odds=2.0) if c.odds <= 4.5 and c.p_safe >= 0.25]
    return sorted(opts, key=lambda c: -(c.p_safe * c.odds))


def stat_coupons(analyses: list[MatchAnalysis], bankroll: float) -> list[Coupon]:
    """Belirlenen lig/kupalarda takım istatistiklerine göre kuponlar. Bir maç en fazla bir kupona girer
    (aynı maç bozulunca birden fazla kupon birden yanmasın)."""
    def take(pool, used):
        rest = [(a, cs) for a, cs in pool if a.fx.espn_id not in used]
        return rest if len(rest) >= 2 else pool

    safe = [(a, likely_options(a)) for a in analyses]
    safe = sorted([(a, cs) for a, cs in safe if cs], key=lambda x: -x[1][0].p_safe)[:12]
    out, used = [], set()
    banko = _stat_coupon("Banko Kuponu", safe, (2, 3), 0.74, (1.25, 1.70), (1.60, 3.50), bankroll)
    if banko:
        out.append(banko)
        used |= {a.fx.espn_id for a, _ in banko.legs}
    daily = _stat_coupon("Günün İstatistik Kuponu", take(safe, used)[:10], (2, 3, 4), 0.55, (1.30, 2.10),
                         (2.8, 8.0), bankroll)
    if daily:
        out.append(daily)
        used |= {a.fx.espn_id for a, _ in daily.legs}
    sur = [(a, surprise_options(a)) for a in analyses]
    sur = sorted([(a, cs) for a, cs in sur if cs], key=lambda x: -(x[1][0].p_safe * x[1][0].odds))[:10]
    surprise = _stat_coupon("Sürpriz Kupon", take(sur, used), (3, 2), 0.25, (2.0, 4.5), (6.0, 40.0), bankroll,
                            objective="er", stake_pct=settings.fun_stake_pct / 2)
    if surprise:
        out.append(surprise)
    return out


def filter_analyses(analyses: list[MatchAnalysis], leagues: set[str] | None = None,
                    hours: tuple[int, int] | None = None) -> list[MatchAnalysis]:
    """Lig ve saat (TR) filtresi: /kupon superlig, /kupon aksam gibi."""
    out = []
    for a in analyses:
        if leagues and a.fx.league not in leagues:
            continue
        if hours:
            h = a.fx.kickoff.astimezone(TZ).hour
            if not hours[0] <= h < hours[1]:
                continue
        out.append(a)
    return out


def price_candidates(analyses: list[MatchAnalysis], min_p: float = 0.40,
                     odds_range: tuple[float, float] = (1.30, 2.60)) -> list[tuple[MatchAnalysis, Candidate]]:
    """Her maçtan, ana pazarlarda iddaa'nın adil orana en yakın (en az kesintili) seçimi."""
    rows = []
    for a in analyses:
        if not a.ev or a.data_conf == "yok":
            continue
        cs = [c for c in a.candidates if is_core(c) and odds_range[0] <= c.odds <= odds_range[1]
              and c.p >= min_p and c.ev <= settings.max_ev]
        if cs:
            rows.append((a, max(cs, key=lambda c: c.ev)))
    return sorted(rows, key=lambda x: -x[1].ev)


def price_coupon(analyses: list[MatchAnalysis], bankroll: float) -> Coupon | None:
    """Değerli seçim yoksa da kupon isteyenler için: beklenen kaybı en düşük 2-3 maçlık kupon.
    Tutar Kelly ile değil, kasanın sabit küçük bir yüzdesiyle (eğlence payı) hesaplanır."""
    pool = price_candidates(analyses)[:10]
    best, best_er = None, -9.0
    for k in (2, 3):
        for legs in itertools.combinations(pool, k):
            if max(c.mbs for _, c in legs) > k:
                continue
            o = math.prod(c.odds for _, c in legs)
            if not 2.0 <= o <= 8.0:
                continue
            er = math.prod(c.p * c.odds for _, c in legs)   # beklenen geri dönüş (1 = başabaş)
            if er > best_er + 1e-9 or (abs(er - best_er) < 1e-9 and best and o < best.odds):
                best, best_er = legs, er
    if not best:
        return None
    o = math.prod(c.odds for _, c in best)
    p = math.prod(c.p for _, c in best)
    st = max(round(bankroll * settings.fun_stake_pct / 5) * 5, settings.min_stake)
    return Coupon(kind="Günün Kuponu (en iyi fiyat)", legs=list(best), odds=o, p=p, ev=p * o - 1, stake=st)


def singles(analyses: list[MatchAnalysis]) -> list[tuple[MatchAnalysis, Candidate]]:
    rows = [(a, a.best) for a in analyses if a.best]
    return sorted(rows, key=lambda x: -x[1].growth)


async def enrich(a: MatchAnalysis, bankroll: float, with_news: bool = True) -> MatchAnalysis:
    """Maç özeti (son maçlar, H2H, kadro, hakem) ve Gemini haberleriyle yeniden analiz."""
    a.summary = await espn.summary(a.fx.league, a.fx.espn_id)
    a.referee = a.summary.referee
    if with_news:
        when = a.fx.kickoff.astimezone(TZ).strftime("%d.%m.%Y %H:%M")
        a.news = await gemini.team_news(a.fx.espn_id, a.fx.home, a.fx.away, a.league_name, when)
    return analyse(a, bankroll)


def near_value(a: MatchAnalysis, margin: float = 0.03) -> bool:
    """Eşiğe yakın aday var mı (haber kontrolüne değer mi)?"""
    return any(settings.min_odds <= c.odds <= settings.max_odds and c.p >= settings.min_prob
               and c.ev >= settings.min_ev[c.conf] - margin for c in a.candidates)


def split_teams(query: str) -> tuple[str, str] | None:
    """'Fenerbahçe - Galatasaray', 'fenerbahçe vs galatasaray' gibi iki takımlı sorguları ayırır."""
    parts = [p.strip() for p in re.split(r"\s+(?:-|–|—|vs\.?|v)\s+|\s*[–—]\s*", query.strip(), maxsplit=1)]
    return (parts[0], parts[1]) if len(parts) == 2 and all(parts) else None


def _team_score(q: str, a: MatchAnalysis, side: str) -> float:
    names = [a.fx.home if side == "home" else a.fx.away]
    if a.ev:
        names.append(a.ev.home if side == "home" else a.ev.away)
    return max(similarity(q, n) for n in names)


async def find_fixture(query: str, days: int = 7) -> MatchAnalysis | None:
    """Önümüzdeki günlerdeki maçı bulur. Tek takım yazılırsa o takımın ilk maçı, iki takım yazılırsa
    ('Fenerbahçe - Galatasaray') o maç seçilir (ev/deplasman sırası önemli değil)."""
    sc = await scan(days_ahead=days, only_upcoming=True)

    def search(pair):
        best, score = None, 0.0
        for a in sc.analyses:  # maçlar başlama saatine göre sıralı: eşitlikte en yakın maç kalır
            if pair:
                s = max(min(_team_score(pair[0], a, "home"), _team_score(pair[1], a, "away")),
                        min(_team_score(pair[0], a, "away"), _team_score(pair[1], a, "home")))
            else:
                s = max(_team_score(query, a, "home"), _team_score(query, a, "away"))
            if s > score:
                best, score = a, s
        return best if score >= 75 else None

    pair = split_teams(query)
    return (search(pair) if pair else None) or search(None)
