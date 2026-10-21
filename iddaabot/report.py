"""Telegram mesaj biçimleri (HTML)."""
from __future__ import annotations

from datetime import datetime
from html import escape

from .config import TZ, settings
from .engine import KIND_TR, STAT_KINDS, Coupon, DayScan, MatchAnalysis, likely_options, risk_scenario, singles, surprise_options

FLAG = {"tur.1": "🇹🇷", "eng.1": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "esp.1": "🇪🇸", "ita.1": "🇮🇹", "ger.1": "🇩🇪",
        "fra.1": "🇫🇷", "uefa.champions": "⭐", "uefa.europa": "🟠"}
DAYS = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]
MONTHS = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül",
          "Ekim", "Kasım", "Aralık"]
CONF_ICON = {"yüksek": "🟢", "orta": "🟡", "düşük": "🟠"}


def e(s) -> str:
    return escape(str(s), quote=False)


def pct(p: float) -> str:
    return f"%{p * 100:.0f}"


def when(dt: datetime) -> str:
    d = dt.astimezone(TZ)
    return f"{DAYS[d.weekday()]} {d:%d.%m %H:%M}"


def tl(x: float) -> str:
    return f"{x:,.0f}".replace(",", ".") + " TL"


def implied(c) -> str:
    imp = 1 / c.odds / (1 + c.margin) if c.margin else 1 / c.odds
    return pct(imp)


def mode_text() -> str:
    sh = settings.w_dc / (settings.w_market + settings.w_dc) if (settings.w_market + settings.w_dc) else 0
    if sh == 0:
        return "sadece piyasa (iddaa oranı uluslararası adil orandan yüksekse değer)"
    return f"piyasa %{(1 - sh) * 100:.0f} + {settings.stat_model} modeli %{sh * 100:.0f}"


def pick_block(a: MatchAnalysis, c, n: int | None = None, pid: int | None = None) -> str:
    head = f"{n}) " if n else ""
    tag = f" <code>#{pid}</code>" if pid else ""
    mbs = f" • MBS {c.mbs}" + (" (tek oynanamaz)" if c.mbs > 1 else "")
    lam = f"{a.lam[0]:.2f}–{a.lam[1]:.2f}" if a.lam else "?"
    return (f"{head}{FLAG.get(a.fx.league, '')} <b>{e(a.title)}</b> <i>({when(a.fx.kickoff)}{mbs})</i>{tag}\n"
            f"   ✅ <b>{e(c.market)}: {e(c.outcome)}</b> @ <b>{c.odds:.2f}</b>\n"
            f"   Olasılık {pct(c.p)} (iddaa ima {implied(c)}) • Değer <b>{c.ev * 100:+.1f}%</b> • "
            f"{CONF_ICON[c.conf]} {c.conf}\n"
            f"   Beklenen gol {lam} • Önerilen: <b>{tl(c.stake)}</b>"
            + (f"\n   ⚠️ {e(rs)}" if (rs := risk_scenario(a, c)) else ""))


def nearest(analyses: list[MatchAnalysis], n: int = 6):
    """Her maçtan ana pazarlardaki en yüksek değerli seçim; eşiğe yakınlığa göre sıralı."""
    from .engine import is_core
    rows = []
    for a in analyses:
        if not a.ev or a.data_conf == "yok":
            continue
        cs = [c for c in a.candidates if is_core(c) and settings.min_odds <= c.odds <= settings.max_odds
              and c.p >= settings.min_prob]
        if cs:
            c = max(cs, key=lambda c: c.ev - settings.min_ev[c.conf])
            rows.append((a, c))
    return sorted(rows, key=lambda x: -(x[1].ev - settings.min_ev[x[1].conf]))[:n]


def leg_reason(a: MatchAnalysis, c) -> str:
    """Seçimin istatistik gerekçesi (bu sezon, lig maçları)."""
    if not (a.ld and a.fd_home and a.fd_away):
        return ""
    h, w = a.ld.team_stats(a.fd_home, "home"), a.ld.team_stats(a.fd_away, "away")
    if not h or not w:
        return ""
    k = c.spec["kind"]
    if k in ("1x2", "dc", "handicap"):
        return (f"{a.fx.home} iç saha {h['G']}-{h['B']}-{h['M']} ({h['atilan']:.1f}/{h['yenen']:.1f}) • "
                f"{a.fx.away} deplasman {w['G']}-{w['B']}-{w['M']} ({w['atilan']:.1f}/{w['yenen']:.1f})")
    if k == "ou":
        return (f"Maç başı toplam gol: {a.fx.home} iç saha {h['atilan'] + h['yenen']:.1f}, "
                f"{a.fx.away} deplasman {w['atilan'] + w['yenen']:.1f} • 2.5Ü oranı {pct(h['ust25'])} / {pct(w['ust25'])}")
    if k == "btts":
        return (f"KG var oranı {pct(h['kg'])} / {pct(w['kg'])} • gol yemedi {pct(h['gol_yemedi'])} / "
                f"{pct(w['gol_yemedi'])}")
    return ""


def coupon_block(cp: Coupon, cid: int | None = None) -> str:
    tag = f" <code>#K{cid}</code>" if cid else ""
    lines = [f"🎫 <b>{cp.kind}</b>{tag}"]
    stat_kind = cp.kind in STAT_KINDS
    for a, c in cp.legs:
        if stat_kind:
            ps = f"istatistik {pct(c.p_stat)}" if getattr(c, "p_stat", None) is not None else "istatistik –"
            pm = f"piyasa {pct(c.p_mkt)}" if getattr(c, "p_mkt", None) is not None else "piyasa –"
            lines.append(f"   • {e(a.title)} ({when(a.fx.kickoff)}) — {e(c.market)}: <b>{e(c.outcome)}</b> @ {c.odds:.2f}"
                         f" ({ps} • {pm})")
            r = leg_reason(a, c)
            if r:
                lines.append(f"     <i>{e(r)}</i>")
        else:
            lines.append(f"   • {e(a.title)} ({when(a.fx.kickoff)}) — {e(c.market)}: <b>{e(c.outcome)}</b> @ {c.odds:.2f} ({pct(c.p)})")
    if stat_kind:
        lines.append(f"   Toplam oran <b>{cp.odds:.2f}</b> • Tutma olasılığı <b>{pct(cp.p)}</b> • "
                     f"Önerilen: <b>{tl(cp.stake)}</b>")
        if cp.kind == "Sürpriz Kupon":
            lines.append("   <i>Yüksek oranlı ama istatistik ve oranların ikisinin de 'olabilir' dediği sonuçlar. "
                         "Sık tutmaz; tutar bu yüzden diğer kuponların yarısı.</i>")
    else:
        lines.append(f"   Toplam oran <b>{cp.odds:.2f}</b> • Tutma olasılığı {pct(cp.p)} • Değer {cp.ev * 100:+.1f}% • "
                     f"Önerilen: <b>{tl(cp.stake)}</b>")
    if cp.kind.startswith("Günün Kuponu"):
        if cp.ev < 0:
            lines.append(f"   <i>Bugün değerli seçim yok; bunlar iddaa'nın uluslararası adil orana en yakın fiyat verdiği "
                         f"seçimler. Uzun vadede 100 TL'de beklenen kayıp ~{-cp.ev * 100:.0f} TL; tutar bu yüzden "
                         f"kasanın %{settings.fun_stake_pct * 100:.0f}'i ile sınırlı.</i>")
        else:
            lines.append("   <i>Seçimler eşiğin altında ama adil orana göre hafif artı beklentili.</i>")
    return "\n".join(lines)


def daily(scan: DayScan, title: str, pick_ids: dict, coupons: list[tuple[Coupon, int]], bankroll: float) -> str:
    now = datetime.now(TZ)
    L = [f"📅 <b>{title}</b> — {now.day} {MONTHS[now.month - 1]}",
         f"Taranan maç: {scan.n_fixtures} • iddaa bülteninde: {scan.n_iddaa} • "
         f"fiyatlanan pazar: {sum(a.n_priced for a in scan.analyses)}"]
    for er in scan.errors:
        L.append(f"⚠️ {e(er)}")
    n_ext = sum(1 for a in scan.analyses if a.fx.ext_1x2)
    n_eval = sum(1 for a in scan.analyses if a.ev and a.fx.ext_1x2)
    L.append(f"Uluslararası oranı olan: {n_ext} • iddaa ile karşılaştırılabilen: {n_eval} "
             f"• lig sayısı: {len({a.fx.league for a in scan.analyses})}")
    unmatched = [a for a in scan.analyses if a.fx.ext_1x2 and not a.ev]
    if unmatched and len(unmatched) >= max(3, n_ext // 4):
        L.append(f"⚠️ {len(unmatched)} maç iddaa bülteninde eşleşmedi (ör. {e(unmatched[0].title)}). "
                 f"/teshis ile listeyi gönder, eşleştirmeyi düzelteyim.")
    rows = singles(scan.analyses)
    L.append("")
    stat_cps = [(cp, cid) for cp, cid in coupons if cp.kind in STAT_KINDS]
    other_cps = [(cp, cid) for cp, cid in coupons if (cp, cid) not in stat_cps]
    for cp, cid in stat_cps:
        L.append(coupon_block(cp, cid))
        L.append("")
    if settings.stat_coupons and not stat_cps:
        L.append("🎫 Bugün belirlenen lig ve kupalarda kupon kurulacak kadar yüksek olasılıklı seçenek bulunamadı.")
        L.append("")
    coupons = other_cps
    if not rows:
        if settings.value_picks:
            L.append("<i>Oran avantajlı (değerli) tekli seçim yok.</i>")
    else:
        L.append(f"💎 <b>Değerli seçimler ({len(rows)})</b>")
        for k, (a, c) in enumerate(rows[:10], 1):
            L.append(pick_block(a, c, k, pick_ids.get((a.fx.espn_id, c.market, c.outcome))))
            if a.news and a.news.get("notlar"):
                L.append(f"   🩹 <i>{e(a.news['notlar'][:220])}</i>")
            L.append("")
    if coupons:
        for cp, cid in coupons:
            L.append(coupon_block(cp, cid))
            L.append("")
    L.append("<i>Kupon olasılıkları: takım istatistikleri (gol + isabetli şut, iç saha/deplasman) ve maç oranları; "
             "ikisinin de yüksek dediği seçenekler alınır, gösterilen olasılık düşük olanıdır.</i>")
    L.append(f"<i>Kasa {tl(bankroll)} • ¼ Kelly, tek bahiste en fazla %{settings.max_stake_pct * 100:.0f}. "
             f"Oynadığını /oynadim #no tutar ile işaretle. Detay: /analiz takım</i>")
    return "\n".join(L)


def _stat_row(label, a, b, fmt="{:.1f}"):
    def f(x):
        return "–" if x is None else fmt.format(x)
    return f"   {label}: {f(a)} | {f(b)}"


def detail(a: MatchAnalysis, ld_home=None, ld_away=None) -> str:
    fx = a.fx
    L = [f"⚽ <b>{e(a.title)}</b>",
         f"{FLAG.get(fx.league, '')} {e(a.league_name)} • {when(fx.kickoff)}"
         + ("" if a.ev else " • iddaa bülteninde yok")]
    if fx.venue:
        L.append(f"🏟 {e(fx.venue)}" + (f" • Hakem: {e(a.referee)}" if a.referee else ""))
    # Puan durumu ve sezon istatistikleri
    lh, th = ld_home or (a.ld, a.fd_home)
    la, ta = ld_away or (a.ld, a.fd_away)
    if lh and th and la and ta:
        ph, pa = lh.position(th), la.position(ta)
        if ph and pa:
            L.append(f"📈 Lig sırası: {e(fx.home)} {ph[0]}. ({ph[1]} p) • {e(fx.away)} {pa[0]}. ({pa[1]} p)")
        sh, sa = lh.team_stats(th, "home"), la.team_stats(ta, "away")
        gh, ga = lh.team_stats(th), la.team_stats(ta)
        if sh and sa:
            L.append(f"📊 <b>Bu sezon maç başı</b> — {e(fx.home)} iç saha ({sh['mac']}) | {e(fx.away)} deplasman ({sa['mac']})")
            L.append(_stat_row("Attığı gol", sh["atilan"], sa["atilan"], "{:.2f}"))
            L.append(_stat_row("Yediği gol", sh["yenen"], sa["yenen"], "{:.2f}"))
            L.append(_stat_row("İY attığı / yediği", sh["iy_atilan"], sa["iy_atilan"], "{:.2f}")
                     + f"  (yediği {sh['iy_yenen'] or 0:.2f} | {sa['iy_yenen'] or 0:.2f})")
            L.append(_stat_row("Şut (isabetli)", sh["sut"], sa["sut"]) +
                     f"  ({sh['isabetli'] or 0:.1f} | {sa['isabetli'] or 0:.1f})")
            L.append(_stat_row("Rakibe izin verdiği şut", sh["sut_yenen"], sa["sut_yenen"]))
            L.append(_stat_row("Korner kullandı", sh["korner"], sa["korner"]))
            L.append(_stat_row("Korner verdi", sh["korner_yenen"], sa["korner_yenen"]))
            L.append(_stat_row("Faul", sh["faul"], sa["faul"]))
            L.append(_stat_row("Sarı kart", sh["sari"], sa["sari"]) +
                     f"  (kırmızı {sh['kirmizi'] or 0:.2f} | {sa['kirmizi'] or 0:.2f})")
            L.append(f"   2.5 Üst biten: {pct(sh['ust25'])} | {pct(sa['ust25'])} • KG: {pct(sh['kg'])} | {pct(sa['kg'])}"
                     f" • Gol yemedi: {pct(sh['gol_yemedi'])} | {pct(sa['gol_yemedi'])}")
        if gh and ga:
            L.append(f"   Genel G-B-M: {gh['G']}-{gh['B']}-{gh['M']} ({gh['atilan']:.1f}/{gh['yenen']:.1f}) | "
                     f"{ga['G']}-{ga['B']}-{ga['M']} ({ga['atilan']:.1f}/{ga['yenen']:.1f})")
    sm = a.summary
    if sm and sm.last_dates:
        fh, fa = sm.fatigue(fx.home_id, fx.kickoff), sm.fatigue(fx.away_id, fx.kickoff)
        if fh or fa:
            fmt = lambda f: f"{f[0]} gün dinlenme, son 7 günde {f[1]} maç" if f else "–"  # noqa: E731
            L.append(f"🔋 Fikstür yükü: {e(fx.home)} {fmt(fh)} | {e(fx.away)} {fmt(fa)}")
    if sm and sm.last5:
        L.append("🕑 <b>Son maçlar</b> (tüm kulvarlar)")
        for tid, nm in ((fx.home_id, fx.home), (fx.away_id, fx.away)):
            rows = sm.last5.get(tid) or []
            if rows:
                L.append(f"   <u>{e(nm)}</u>")
                L.extend(f"   {e(r)}" for r in rows[:6])
    h2h = (sm.h2h if sm and sm.h2h else []) or (a.ld.h2h(a.fd_home, a.fd_away) if a.ld and a.fd_home and a.fd_away else [])
    if h2h:
        L.append("🆚 <b>Son karşılaşmalar</b>")
        L.extend(f"   {e(x)}" for x in h2h[:6])
    if a.news:
        n = a.news
        L.append("🩹 <b>Kadro / haber</b> (Gemini + Google)")
        for side, key in ((fx.home, "ev_eksikler"), (fx.away, "dep_eksikler")):
            ks = [f"{x.get('oyuncu', '')} ({x.get('durum', '')}{', kilit' if x.get('kilit') else ''})"
                  for x in (n.get(key) or []) if x.get("oyuncu")]
            if ks:
                L.append(f"   {e(side)}: {e(', '.join(ks))}")
        if n.get("hakem"):
            L.append(f"   Hakem: {e(n['hakem'])}")
        if n.get("notlar"):
            L.append(f"   <i>{e(n['notlar'])}</i>")
        if n.get("ev_gol_carpani", 1) != 1 or n.get("dep_gol_carpani", 1) != 1:
            L.append(f"   Gol çarpanı (istatistik modeline): {n['ev_gol_carpani']:.2f} / {n['dep_gol_carpani']:.2f}")
        for s in n.get("kaynaklar") or []:
            L.append(f"   🔗 <a href=\"{e(s['uri'])}\">{e(s.get('title') or 'kaynak')}</a>")
    if sm and sm.lineups:
        L.append("👥 <b>İlk 11</b>")
        for tid, nm in ((fx.home_id, fx.home), (fx.away_id, fx.away)):
            if sm.lineups.get(tid):
                L.append(f"   {e(nm)}: {e(', '.join(sm.lineups[tid]))}")
    if a.gm:
        s = a.gm.summary()
        src = a.mkt_source or "—"
        mix = (mode_text() if a.lam_mkt and a.lam_dc else ("sadece piyasa" if a.lam_mkt else "sadece istatistik"))
        L.append("🧮 <b>Model</b>")
        L.append(f"   Beklenen gol {a.lam[0]:.2f} – {a.lam[1]:.2f} ({mix}; piyasa kaynağı: {e(src)})")
        if a.lam_mkt and a.lam_dc:
            L.append(f"   Piyasa {a.lam_mkt[0]:.2f}–{a.lam_mkt[1]:.2f} | İstatistik {a.lam_dc[0]:.2f}–{a.lam_dc[1]:.2f}"
                     f" • Uyum farkı {a.agreement * 100:.0f} puan")
        L.append(f"   MS 1 {pct(s['1'])} • X {pct(s['X'])} • 2 {pct(s['2'])} • 2.5Ü {pct(s['Ü2.5'])} • KG {pct(s['KGV'])}")
        L.append("   En olası skorlar: " + ", ".join(f"{k} {pct(v)}" for k, v in a.gm.top_scores(4)))
        if a.corners:
            L.append(f"   Korner beklentisi {a.corners.mh + a.corners.ma:.1f} ({a.corners.mh:.1f}–{a.corners.ma:.1f})"
                     + (f" • Kart {a.cards.mh + a.cards.ma:.1f}" if a.cards else ""))
        L.append(f"   Veri güveni: {CONF_ICON.get(a.data_conf, '⚪')} {a.data_conf}")
    if a.ev:
        L.append(f"💰 Bültendeki pazar: {a.n_markets} • modellenen: {a.n_priced}"
                 + (f" • tutarsız olduğu için atlanan: {a.n_suspect}" if a.n_suspect else ""))
    if a.picks:
        L.append("✅ <b>Değerli seçimler</b>")
        for c in a.picks[:6]:
            L.append(f"   • {e(c.market)}: <b>{e(c.outcome)}</b> @ {c.odds:.2f} → {pct(c.p)} (ima {implied(c)}) • "
                     f"<b>{c.ev * 100:+.1f}%</b> • {CONF_ICON[c.conf]} {c.conf} • MBS {c.mbs} • {tl(c.stake)}")
        b = a.picks[0]
        L.append(f"🎯 <b>Öneri: {e(b.market)} – {e(b.outcome)} @ {b.odds:.2f}</b> ({tl(b.stake)})")
        rs = risk_scenario(a, b)
        if rs:
            L.append(f"⚠️ Risk: {e(rs)}")
    elif a.candidates:
        L.append("🎯 <b>Öneri: Pas.</b> Ana pazarlarda (MS, ÇŞ, Alt/Üst, KG, handikap) oran, "
                 "olasılığı eşik kadar aşmıyor.")
    if a.candidates:
        top = sorted((c for c in a.candidates if c.odds >= 1.25), key=lambda c: -c.p)[:5]
        L.append("🔎 En olası seçenekler (değer şartı aranmadan):")
        for c in top:
            L.append(f"   • {e(c.market)}: {e(c.outcome)} @ {c.odds:.2f} → {pct(c.p)} • EV {c.ev * 100:+.1f}%")
    for nt in a.notes:
        L.append(f"ℹ️ {e(nt)}")
    return "\n".join(L)


def karne(rc: dict) -> str:
    a = rc.get("all")
    if not a:
        return "📒 Henüz sonuçlanmış öneri yok. Karne ilk maçlar bitince oluşur."
    L = ["📒 <b>Karne — bot önerileri</b>",
         f"Sonuçlanan: {a['n']} (✅ {a['won']} • ❌ {a['lost']} • ↩️ {a['void']})",
         f"Beklenen tutan (modele göre): {a['exp_hits']:.1f} • Gerçek: {a['won']}",
         f"Sabit 1 birim: kâr {a['profit']:+.2f} birim • ROI {a['roi'] * 100:+.1f}%"]
    t = rc.get("tl")
    if t:
        L.append(f"Kelly tutarlarıyla: {t['profit']:+.0f} TL / {t['staked']:.0f} TL yatırım • ROI {t['roi'] * 100:+.1f}%")
    if a.get("t") is not None:
        L.append(f"Anlamlılık t = {a['t']:.2f} " + ("(şans dışı görünüyor)" if abs(a["t"]) >= 2 else
                                                   "(henüz şansla açıklanabilir — daha fazla örnek gerek)"))
    if rc.get("clv"):
        v, n = rc["clv"]
        L.append(f"Oran hareketi (CLV, {n} seçim): {v * 100:+.1f}% "
                 + ("— oranlar bizim yönümüze düşüyor ✅" if v > 0 else "— piyasa bizim tersimize gidiyor ⚠️"))
    if rc.get("close_ev"):
        v, n, beat = rc["close_ev"]
        L.append(f"Kapanış değeri ({n} seçim): ort. {v * 100:+.1f}% • %{beat * 100:.0f}'inde maç başlarken de değerliydi "
                 + ("✅ (uzun vadede kazandıran işaret)" if v > 0 else "⚠️"))
    if rc.get("conf"):
        L.append("<b>Güvene göre</b>")
        for k in ("yüksek", "orta", "düşük"):
            s = rc["conf"].get(k)
            if s:
                L.append(f"   {CONF_ICON[k]} {k}: {s['n']} öneri, ROI {s['roi'] * 100:+.1f}%")
    if rc.get("kind"):
        L.append("<b>Pazara göre</b>")
        for k, s in sorted(rc["kind"].items(), key=lambda x: -x[1]["n"]):
            base, _, subj = k.partition(":")
            nm = KIND_TR.get(base, base) + {"corners": " (korner)", "cards": " (kart)"}.get(subj, "")
            L.append(f"   {e(nm)}: {s['n']} öneri, ROI {s['roi'] * 100:+.1f}%")
    if rc.get("coupons"):
        L.append("<b>🎫 Kuponlar</b>")
        for kind, c in rc["coupons"].items():
            L.append(f"   {e(kind)}: {c['n']} kupon, tutan {c['won']} • {c['profit']:+.0f} TL / {c['staked']:.0f} TL")
    if rc.get("played"):
        p = rc["played"]
        L.append(f"🙋 <b>Senin oynadıkların:</b> {p['n']} bahis • {p['profit']:+.0f} TL • ROI {p['roi'] * 100:+.1f}%")
    return "\n".join(L)


def board(analyses: list[MatchAnalysis], title: str) -> str:
    """Günün tüm maçları (belirlenen lig/kupalar): her maç için en olası ve sürpriz seçenek."""
    from .config import COUPON_LEAGUES, LEAGUES
    L = [f"📋 <b>{title}</b>"]
    by = {}
    for a in analyses:
        if a.fx.league in COUPON_LEAGUES:
            by.setdefault(a.fx.league, []).append(a)
    if not by:
        return f"📋 {title}: belirlenen lig ve kupalarda maç yok."
    for lg in COUPON_LEAGUES:
        if lg not in by:
            continue
        L.append("")
        L.append(f"{FLAG.get(lg, '')} <b>{e(LEAGUES[lg][0])}</b>")
        for a in sorted(by[lg], key=lambda x: x.fx.kickoff):
            t = a.fx.kickoff.astimezone(TZ).strftime("%H:%M")
            if not a.ev:
                L.append(f"   {t} {e(a.title)} — iddaa'da yok")
                continue
            lo, so = likely_options(a), surprise_options(a)
            s1 = f"{e(lo[0].market)}: <b>{e(lo[0].outcome)}</b> @{lo[0].odds:.2f} ({pct(lo[0].p_safe)})" if lo else "–"
            s2 = f"{e(so[0].market)}: {e(so[0].outcome)} @{so[0].odds:.2f} ({pct(so[0].p_safe)})" if so else "–"
            L.append(f"   {t} {e(a.title)}\n      ✅ {s1}\n      🎲 {s2}")
    L.append("")
    L.append("<i>✅ en olası seçenek • 🎲 sürpriz seçenek. Kendi kuponunu kurmak için. "
             "Hazır kupon: /kupon, lige göre: /kupon superlig, saate göre: /kupon aksam</i>")
    return "\n".join(L)


def split(text: str, limit: int = 3900) -> list[str]:
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        parts.append(cur)
    return parts
