"""Telegram mesaj biçimleri (HTML)."""
from __future__ import annotations

from datetime import datetime
from html import escape

from .config import TZ, settings
from .engine import KIND_TR, STAT_KINDS, Coupon, DayScan, MatchAnalysis, likely_options, risk_scenario, singles, surprise_options

FLAG = {"tur.1": "🇹🇷", "eng.1": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "esp.1": "🇪🇸", "ita.1": "🇮🇹", "ger.1": "🇩🇪",
        "fra.1": "🇫🇷", "uefa.champions": "⭐", "uefa.europa": "🟠", "ned.1": "🇳🇱", "por.1": "🇵🇹",
        "sui.1": "🇨🇭", "bel.1": "🇧🇪", "aut.1": "🇦🇹"}
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


def hist_text(c, short: bool = False) -> str:
    """Geçmişte benzer olasılıklı maçlarda bu seçeneğin tutma oranı."""
    h = getattr(c, "hist", None)
    if not h:
        return ""
    r, n, scope = h
    if short:
        return f"geçmiş {pct(r)}"
    where = "bu ligde" if scope == "lig" else "9 ligde"
    return f"Geçmiş: {where} benzer oranlı {n:,} maçta {pct(r)} tuttu".replace(",", ".", 1)


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
            h = hist_text(c)
            if h:
                lines.append(f"     📊 {h}")
            r = leg_reason(a, c)
            if r:
                lines.append(f"     <i>{e(r)}</i>")
        else:
            lines.append(f"   • {e(a.title)} ({when(a.fx.kickoff)}) — {e(c.market)}: <b>{e(c.outcome)}</b> @ {c.odds:.2f} ({pct(c.p)})")
    if stat_kind:
        lines.append(f"   Toplam oran <b>{cp.odds:.2f}</b> • Tutma olasılığı <b>{pct(cp.p)}</b> • "
                     f"Önerilen: <b>{tl(cp.stake)}</b>")
        if cp.kind == "2-3 Oran Kuponu":
            lines.append("   <i>Her seçim hem botun hesabında hem geçmişte %70'in üstünde. Yine de toplam oran 2 ise "
                         "kupon 10'da ~4-5 kez tutar; tutarı sabit tut, kaybedince artırma.</i>")
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


def short_pick(c) -> str:
    """Seçimin kısa adı: 'ÇŞ 1-2', '2.5 Üst', 'KG Var', 'MS 1'."""
    sp = c["spec"] if isinstance(c, dict) else getattr(c, "spec", None)
    market = c["market"] if isinstance(c, dict) else c.market
    outcome = c["outcome"] if isinstance(c, dict) else c.outcome
    if isinstance(sp, str):
        import json
        try:
            sp = json.loads(sp)
        except ValueError:
            sp = None
    sp = sp or {}
    subj, per, k = sp.get("subject", "goals"), sp.get("period", "FT"), sp.get("kind")
    pre = {"H1": "İY ", "H2": "2Y "}.get(per, "")
    if subj == "goals" and k == "1x2":
        return f"{pre or 'MS '}{sp['sel']}".replace("İY ", "İY MS ").replace("2Y ", "2Y MS ")
    if subj == "goals" and per == "FT" and k == "dc":
        return "ÇŞ " + "-".join(sp["sel"])
    if subj in ("goals", "corners") and k == "ou":
        who = {"home": "Ev ", "away": "Dep "}.get(sp.get("scope", "total"), "")
        what = "Korner " if subj == "corners" else ""
        return f"{pre}{who}{what}{sp['line']:g} {'Üst' if sp['side'] == 'over' else 'Alt'}"
    if subj == "goals" and per == "FT" and k == "btts":
        return "KG Var" if sp.get("yes") else "KG Yok"
    return f"{market}: {outcome}"


def hhmm(dt: datetime) -> str:
    """Bugünse '20:00', değilse 'Paz 20:00'."""
    d = dt.astimezone(TZ)
    return f"{d:%H:%M}" if d.date() == datetime.now(TZ).date() else f"{DAYS[d.weekday()]} {d:%H:%M}"


def coupon_simple(cp: Coupon, cid: int | None = None) -> str:
    """Sade kupon: maç, saat, seçim, oran. Ayrıntılar 'Neden?' butonunda."""
    from . import rules70
    icon = {"Banko Kuponu": "🛡", "2-3 Oran Kuponu": "🎫", "Sürpriz Kupon": "🎲"}.get(cp.kind, "🎫")
    tag = f"  <code>#K{cid}</code>" if cid else ""
    L = [f"{icon} <b>{e(cp.kind)}</b>{tag}"]
    for k, (a, c) in enumerate(cp.legs, 1):
        L.append(f"{k}. {FLAG.get(a.fx.league, '')} {e(a.title)} · {hhmm(a.fx.kickoff)}")
        extra = f" · {e(rules70.stat_label(c))}" if getattr(c, "rates", None) else ""
        L.append(f"   👉 <b>{e(short_pick(c))}</b> · {c.odds:.2f}{extra}")
    if cp.kind == rules70.KIND:
        L.append(f"<b>Oran {cp.odds:.2f}</b> · {tl(cp.stake)}")
    else:
        L.append(f"<b>Oran {cp.odds:.2f}</b> · Tutma şansı {pct(cp.p)} · {tl(cp.stake)}")
    return "\n".join(L)


def coupon70_detail(cp: Coupon, cid: int | None = None) -> str:
    """'Neden?' — her seçimin istatistiği (kaç maçın kaçında tuttu)."""
    from . import rules70
    L = [f"🔍 <b>{e(cp.kind)}</b>" + (f" <code>#K{cid}</code>" if cid else "") + " — neden bu seçimler?"]
    for k, (a, c) in enumerate(cp.legs, 1):
        L.append(f"{k}. {e(a.title)} — <b>{e(short_pick(c))}</b> @ {c.odds:.2f}")
        L.append(f"   {e(rules70.reason(c))}")
    L.append(f"\n<i>Kural: takıma ait seçenekte takımın, maça ait seçenekte iki takımın da bu sezonki "
             f"iç saha / dış saha maçlarında %{rules70.THRESH * 100:.0f}+ tutması (en az {rules70.MIN_GAMES} maç).</i>")
    return "\n".join(L)


def list70(analyses: list[MatchAnalysis], title: str) -> str:
    """Günün %70 listesi: maç maç, %70 kuralını geçen tüm seçenekler ve oranları."""
    from . import rules70
    from .config import COUPON_LEAGUES, LEAGUES
    by = {}
    for a in analyses:
        cs = rules70.options(a, min_odds=rules70.LIST_MIN_ODDS)
        if cs:
            by.setdefault(a.fx.league, []).append((a, cs))
    if not by:
        return (f"📊 {title}: bugün takım istatistiği %70 üstü olan seçenek çıkmadı "
                "(istatistik sadece lig maçları olan liglerde hesaplanır).")
    n = sum(len(cs) for v in by.values() for _, cs in v)
    m = sum(len(v) for v in by.values())
    L = [f"📊 <b>{title}</b> ({m} maç, {n} seçenek)"]
    for lg in COUPON_LEAGUES:
        if lg not in by:
            continue
        L.append("")
        L.append(f"{FLAG.get(lg, '')} <b>{e(LEAGUES[lg][0])}</b>")
        for a, cs in sorted(by[lg], key=lambda x: x[0].fx.kickoff):
            L.append(f"{hhmm(a.fx.kickoff)} {e(a.title)}")
            for c in cs[:6]:
                L.append(f"   • {e(short_pick(c))} · {c.odds:.2f} · {e(rules70.stat_label(c))}")
            if len(cs) > 6:
                L.append(f"   <i>+{len(cs) - 6} seçenek daha</i>")
    L.append("")
    L.append("<i>Ev: ev sahibinin iç saha maçları • Dep: deplasmanın dış saha maçları (bu sezon, lig).</i>")
    return "\n".join(L)


def board(analyses: list[MatchAnalysis], title: str) -> str:
    """Günün maçları: her maç için en güvenli ✅ ve (varsa) sürpriz 🎲 seçenek."""
    from .config import COUPON_LEAGUES, LEAGUES
    by = {}
    for a in analyses:
        if a.fx.league in COUPON_LEAGUES and a.ev:
            lo = likely_options(a)
            if lo:
                by.setdefault(a.fx.league, []).append((a, lo[0], (surprise_options(a) or [None])[0]))
    if not by:
        return f"📋 {title}: seçtiğin liglerde oranı açılmış maç yok."
    n = sum(len(v) for v in by.values())
    L = [f"📋 <b>{title}</b> ({n} maç)"]
    for lg in COUPON_LEAGUES:
        if lg not in by:
            continue
        L.append("")
        L.append(f"{FLAG.get(lg, '')} <b>{e(LEAGUES[lg][0])}</b>")
        for a, c, s in sorted(by[lg], key=lambda x: x[0].fx.kickoff):
            row = f"✅ {e(short_pick(c))} · {c.odds:.2f} · {pct(c.p_safe)}"
            if s:
                row += f"   🎲 {e(short_pick(s))} · {s.odds:.2f}"
            L.append(f"{hhmm(a.fx.kickoff)} {e(a.title)}")
            L.append(f"   {row}")
    L.append("")
    L.append("<i>✅ en güvenli seçenek ve tutma şansı · 🎲 sürpriz seçenek</i>")
    return "\n".join(L)


def analysis_simple(a: MatchAnalysis) -> str:
    """Tek maç: en olası seçenekler, form, eksikler. Ayrıntılı istatistik butonda."""
    from .engine import is_core
    fx = a.fx
    L = [f"🔎 <b>{e(a.title)}</b>", f"{FLAG.get(fx.league, '')} {e(a.league_name)} · {when(fx.kickoff)}"]
    if not a.ev:
        L.append("\nBu maç iddaa bülteninde henüz yok.")
    if settings.coupon_mode == "70":
        from . import rules70
        cs = rules70.options(a, min_odds=rules70.LIST_MIN_ODDS) if a.ev else []
        L.append("\n<b>%70 üstü seçenekler</b>")
        if cs:
            L.extend(f"• {e(short_pick(c))} · {c.odds:.2f} · {e(rules70.stat_label(c))}" for c in cs[:8])
        else:
            L.append("Bu maçta takım istatistiği %70 üstü olan seçenek yok." if a.ld else
                     "Bu lig/kupa için takım istatistiği yok (sadece lig maçlarından hesaplanır).")
        opts = []
    else:
        opts = likely_options(a)
        if not opts:
            opts = sorted((c for c in (a.all_cands or a.candidates) if is_core(c) and c.odds >= 1.25),
                          key=lambda c: -c.p)
            for c in opts:
                c.p_safe = c.p
    if opts:
        L.append("\n<b>En olası seçenekler</b>")
        seen = set()
        for c in opts:
            sp = short_pick(c)
            if sp in seen:
                continue
            seen.add(sp)
            L.append(f"✅ {e(sp)} · {c.odds:.2f} · {pct(c.p_safe)}")
            if len(seen) == 4:
                break
        so = surprise_options(a) if a.ev else []
        if so:
            L.append(f"🎲 Sürpriz: {e(short_pick(so[0]))} · {so[0].odds:.2f} · {pct(so[0].p_safe)}")
    sm = a.summary
    if sm and sm.last5:
        form = []
        for tid, nm in ((fx.home_id, fx.home), (fx.away_id, fx.away)):
            rows = sm.last5.get(tid) or []
            if rows:
                form.append(f"{e(nm)} {' '.join(r.split()[0] for r in rows[:5])}")
        if form:
            L.append("\n<b>Son 5 maç</b> (G galibiyet, B beraberlik, M mağlubiyet)")
            L.extend(form)
    if a.news:
        miss = []
        for side, key in ((fx.home, "ev_eksikler"), (fx.away, "dep_eksikler")):
            ks = [x.get("oyuncu", "") for x in (a.news.get(key) or []) if x.get("oyuncu")]
            miss.append(f"{e(side)}: {e(', '.join(ks[:5])) if ks else 'yok'}")
        L.append("\n<b>Eksikler</b>")
        L.extend(miss)
        if a.news.get("notlar"):
            L.append(f"<i>{e(a.news['notlar'][:300])}</i>")
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


LEG_ICON = {"won": "✅", "lost": "❌", "void": "↩️", "manual": "❔"}
CP_STATUS = {"won": "✅ <b>TUTTU</b>", "lost": "❌ <b>YATTI</b>"}


def _cp_payout(cp, legs) -> float:
    odds = 1.0
    for r in legs:
        if r["status"] == "won":
            odds *= r["odds"]
    return cp["stake"] * odds


def coupon_result_block(cp, legs, now: datetime | None = None) -> str:
    """Bir kuponun sonucu: her maçın yanında ✅ / ❌ / ⏳ / 🔴."""
    now = now or datetime.now(TZ)
    st = cp["played_stake"] if cp["played"] else cp["stake"]
    if cp["status"] == "won":
        pay = _cp_payout(cp, legs) * st / cp["stake"] if cp["stake"] else 0
        head = f"✅ <b>{e(cp['kind'])}</b> · {cp['odds']:.2f} · <b>TUTTU</b> (+{tl(pay - st)})"
    elif cp["status"] == "lost":
        head = f"❌ <b>{e(cp['kind'])}</b> · {cp['odds']:.2f} · <b>YATTI</b> (-{tl(st)})"
    else:
        done = sum(1 for r in legs if r["status"] in ("won", "void"))
        head = f"⏳ <b>{e(cp['kind'])}</b> · {cp['odds']:.2f} · devam ediyor ({done}/{len(legs)})"
    L = [head + f"  <code>#K{cp['id']}</code>"]
    for r in legs:
        ko = datetime.fromisoformat(r["kickoff"]).astimezone(TZ)
        if r["status"] in LEG_ICON:
            icon, sc = LEG_ICON[r["status"]], f" {e(r['score'].split(' (')[0]) if r['score'] else ''}"
        elif ko <= now:
            icon, sc = "🔴", " oynanıyor"
        else:
            icon, sc = "⏳", f" {hhmm(ko)}"
        L.append(f"   {icon} {e(r['home'])} – {e(r['away'])}{sc} · {e(short_pick(dict(r)))}")
        if r["status"] == "manual":
            L.append(f"      sonucu bulamadım: /sonuc {r['id']} kazandi | kaybetti")
    return "\n".join(L)


def coupon_results(items, title: str, now: datetime | None = None) -> str:
    """items: [(kupon satırı, ayak satırları)]. Kısa özet + her kupon ✅/❌ (çok kupon varsa tek satır)."""
    if not items:
        return f"🎫 {title}: bu aralıkta kupon yok."
    won = [cp for cp, _ in items if cp["status"] == "won"]
    lost = [cp for cp, _ in items if cp["status"] == "lost"]
    pend = len(items) - len(won) - len(lost)
    staked = sum(cp["stake"] for cp in won + lost)
    ret = sum(_cp_payout(cp, ls) for cp, ls in items if cp["status"] == "won")
    L = [f"🎫 <b>{title}</b>",
         f"✅ {len(won)} tuttu · ❌ {len(lost)} yattı" + (f" · ⏳ {pend} bekliyor" if pend else "")]
    if staked:
        net = ret - staked
        L.append(f"Kâr/zarar: <b>{'+' if net >= 0 else '-'}{tl(abs(net))}</b>")
    L.append("")
    if len(items) > 12:
        for cp, ls in items:
            day = datetime.fromisoformat(ls[0]["kickoff"]).astimezone(TZ).strftime("%d.%m") if ls else ""
            icon = {"won": "✅", "lost": "❌"}.get(cp["status"], "⏳")
            L.append(f"{icon} {day} {e(cp['kind'])} · {cp['odds']:.2f}")
    else:
        L.append("\n\n".join(coupon_result_block(cp, ls, now) for cp, ls in items))
    return "\n".join(L)
