"""Telegram botu: komutlar ve zamanlanmış görevler."""
from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import Counter
from datetime import datetime, time as dtime, timedelta, timezone
from functools import wraps
from html import escape

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from . import backtest, engine, espn, footballdata, gemini, iddaa, mackolik, report, storage, tracker, updater
from .config import LEAGUES, TZ, settings
from .matching import pair_score

log = logging.getLogger("iddaabot")
LOCK = asyncio.Lock()
LIG_ALIAS = {"t1": "T1", "superlig": "T1", "süperlig": "T1", "tr": "T1", "turkiye": "T1",
             "e0": "E0", "premier": "E0", "ingiltere": "E0", "pl": "E0",
             "sp1": "SP1", "laliga": "SP1", "ispanya": "SP1", "i1": "I1", "seriea": "I1", "italya": "I1",
             "d1": "D1", "bundesliga": "D1", "almanya": "D1", "f1": "F1", "ligue1": "F1", "fransa": "F1"}

HELP = """🤖 <b>iddaa Strateji Botu</b>

<b>Günlük</b>
/bugun — bugünün maçları: değerli seçimler + kuponlar
/yarin — yarının maçları
/kupon — günün kuponları: Banko (2-3 maç) ve İstatistik Kuponu (3-4 maç) • /kupon yarin
/deger ac|kapat — oran avantajlı tekli seçimler ve ⚡ bildirimleri (varsayılan kapalı)
/analiz <i>takım</i> — o takımın sıradaki maçının tam analizi (ör. /analiz fenerbahçe)
/analiz <i>takım - takım</i> — belirli bir maç (ör. /analiz fenerbahçe - galatasaray)

<b>Takip</b>
/karne — önerilerin gerçek sonuçları (ROI, isabet, CLV)
/oynadim <i>no tutar</i> — oynadığını işaretle (ör. /oynadim 12 50, kupon: /oynadim K3 100)
/sonuc <i>no kazandi|kaybetti|iade</i> — otomatik sonuçlanamayanı elle işaretle
/kasa <i>tutar</i> — kasanı güncelle (önerilen tutarlar buna göre)

<b>Test ve ayar</b>
/backtest <i>lig sezon</i> — geçmiş maç testi (ör. /backtest superlig 2025, /backtest hepsi 2024 2025)
/agirlik <i>0-1</i> — istatistik modelinin payı (0 = sadece piyasa)
/teshis — veri kaynakları çalışıyor mu
/guncelle — son sürümü GitHub'dan kur • /surum — sürüm • /geri — önceki sürüme dön
/ayarlar — strateji eşikleri

Her gün saat {hour}:00'da günün raporu gelir. Maçlara 2.5 saat kala oranlar 15 dakikada bir
yeniden taranır; iddaa uluslararası piyasanın gerisinde kalırsa ⚡ ile bildirilir. Maçtan ~1 saat
önce kadro/oran kontrolü, maç bitince sonuç bildirimi otomatik gelir."""


# ----------------------------------------------------------------- yardımcılar
def owner_id() -> int:
    return settings.allowed_chat_id or int(storage.get_setting("owner_chat_id", 0) or 0)


def bankroll() -> float:
    return float(storage.get_setting("bankroll", settings.bankroll))


def owner_only(fn):
    @wraps(fn)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        if not chat:
            return
        oid = owner_id()
        if oid == 0 and fn.__name__ == "cmd_start":
            storage.set_setting("owner_chat_id", chat.id)
            oid = chat.id
        if chat.id != oid:
            log.warning("Yetkisiz erişim denemesi: %s", chat.id)
            return
        return await fn(update, ctx)
    return wrapper


async def send(bot, chat_id: int, text: str) -> None:
    for part in report.split(text):
        try:
            await bot.send_message(chat_id, part, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception as e:  # noqa: BLE001  (HTML hatası olursa düz metin)
            log.warning("HTML gönderilemedi (%s), düz metin deneniyor", e)
            await bot.send_message(chat_id, re.sub(r"<[^>]+>", "", part), disable_web_page_preview=True)


async def publish(bot, chat_id: int, title: str, days_ahead: int, start: datetime | None = None) -> None:
    async with LOCK:
        br = bankroll()
        sc = await engine.scan(days_ahead=days_ahead, start=start, bankroll=br)
        near = [a for a in sc.analyses if a.ev and engine.near_value(a)]
        near = sorted(near, key=lambda a: -max((c.ev for c in a.candidates), default=0))[:12]
        sem = asyncio.Semaphore(4)

        async def _enrich(a):
            async with sem:
                try:
                    await engine.enrich(a, br, with_news=True)
                except Exception:  # noqa: BLE001
                    log.exception("zenginleştirme %s", a.title)
        await asyncio.gather(*(_enrich(a) for a in near))
        pick_ids = {}
        if not settings.value_picks:
            for a in sc.analyses:  # değerli tekli seçimler kapalı: raporda ve kuponlarda yer almaz
                a.picks = []
        for a, c in engine.singles(sc.analyses):
            pick_ids[(a.fx.espn_id, c.market, c.outcome)] = storage.save_pick(a, c, single=True)
        cps = []
        coupons = (engine.stat_coupons(sc.analyses, br) if settings.stat_coupons else []) + \
            (engine.build_coupons(sc.analyses, br) if settings.value_picks else [])
        if settings.daily_coupon and not any(cp.kind == "Değer Kuponu" for cp in coupons):
            pc = engine.price_coupon(sc.analyses, br)
            if pc:
                coupons.append(pc)
        for cp in coupons:
            ids = [storage.save_pick(a, c, single=False) for a, c in cp.legs]
            cps.append((cp, storage.save_coupon(cp.kind, ids, cp.odds, cp.p, cp.ev, cp.stake)))
    await send(bot, chat_id, report.daily(sc, title, pick_ids, cps, br))


@owner_only
async def cmd_deger(update: Update, ctx):
    """/deger ac | kapat — oran avantajlı tekli seçimler ve ⚡ son dakika bildirimleri."""
    if ctx.args and ctx.args[0].lower() in ("ac", "aç", "kapat"):
        v = ctx.args[0].lower() in ("ac", "aç")
        storage.set_setting("value_picks", v)
        settings.value_picks = v
    await update.message.reply_text(
        f"Değerli tekli seçimler ve ⚡ bildirimleri: {'açık' if settings.value_picks else 'kapalı'}. "
        f"Değiştirmek için /deger ac veya /deger kapat.")


@owner_only
async def cmd_kupon(update: Update, ctx):
    """Değer olsun olmasın bugünün (veya /kupon yarin) en iyi fiyatlı kuponu."""
    tomorrow = bool(ctx.args) and ctx.args[0].lower().startswith("yar")
    await update.message.reply_text("⏳ Kupon hazırlanıyor...")
    start = ((datetime.now(TZ) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
             if tomorrow else None)
    async with LOCK:
        br = bankroll()
        sc = await engine.scan(days_ahead=0, start=start, bankroll=br)
        coupons = engine.stat_coupons(sc.analyses, br) + (
            [cp for cp in engine.build_coupons(sc.analyses, br) if cp.kind == "Değer Kuponu"]
            if settings.value_picks else [])
        out = []
        for cp in coupons:
            ids = [storage.save_pick(a, c, single=False) for a, c in cp.legs]
            out.append((cp, storage.save_coupon(cp.kind, ids, cp.odds, cp.p, cp.ev, cp.stake)))
    if not out:
        n = sum(1 for a in sc.analyses if a.ev and a.fx.ext_1x2)
        await send(ctx.bot, update.effective_chat.id,
                   f"Kupon kurulamadı: iddaa ile karşılaştırılabilen maç sayısı {n}. "
                   f"/teshis ile eşleşmeyi kontrol et.")
        return
    await send(ctx.bot, update.effective_chat.id, "\n\n".join(report.coupon_block(cp, cid) for cp, cid in out))


# ----------------------------------------------------------------- komutlar
@owner_only
async def cmd_start(update: Update, ctx):
    await send(ctx.bot, update.effective_chat.id, HELP.format(hour=settings.daily_report_hour))


@owner_only
async def cmd_bugun(update: Update, ctx):
    await update.message.reply_text("⏳ Bugünün bülteni taranıyor (1-2 dk sürebilir)...")
    await publish(ctx.bot, update.effective_chat.id, "Günün Analizi", 0)


@owner_only
async def cmd_yarin(update: Update, ctx):
    await update.message.reply_text("⏳ Yarının bülteni taranıyor...")
    start = (datetime.now(TZ) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    await publish(ctx.bot, update.effective_chat.id, "Yarının Analizi", 0, start)


@owner_only
async def cmd_analiz(update: Update, ctx):
    q = " ".join(ctx.args).strip()
    if not q:
        await update.message.reply_text("Kullanım: /analiz fenerbahçe  veya  /analiz fenerbahçe - galatasaray")
        return
    await update.message.reply_text(f"⏳ {q} için önümüzdeki 7 günün maçı aranıyor ve analiz ediliyor...")
    async with LOCK:
        a = await engine.find_fixture(q, days=7)
        if not a:
            await update.message.reply_text("Önümüzdeki 7 günde bu takımın maçı bulunamadı (Süper Lig, 5 büyük lig, ŞL, UEL).")
            return
        await engine.enrich(a, bankroll(), with_news=True)
        leagues = await footballdata.load_all()
        lh = la = None
        if not a.ld:
            lh = engine.home_league_stats(a.fx.home, leagues)
            la = engine.home_league_stats(a.fx.away, leagues)
            lh = lh if lh[0] else None
            la = la if la[0] else None
    await send(ctx.bot, update.effective_chat.id, report.detail(a, lh, la))


@owner_only
async def cmd_karne(update: Update, ctx):
    await send(ctx.bot, update.effective_chat.id, report.karne(tracker.report_card()))


@owner_only
async def cmd_kasa(update: Update, ctx):
    if ctx.args:
        try:
            v = float(ctx.args[0].replace(".", "").replace(",", "."))
            storage.set_setting("bankroll", v)
        except ValueError:
            await update.message.reply_text("Kullanım: /kasa 2000")
            return
    await update.message.reply_text(f"💼 Kasa: {report.tl(bankroll())}. Önerilen tutarlar buna göre hesaplanır "
                                    f"(¼ Kelly, tek bahiste en fazla %{settings.max_stake_pct * 100:.0f}).")


@owner_only
async def cmd_oynadim(update: Update, ctx):
    if len(ctx.args) < 2:
        await update.message.reply_text("Kullanım: /oynadim 12 50   (kupon için /oynadim K3 100)")
        return
    ref, amount = ctx.args[0].lstrip("#").upper(), ctx.args[1]
    try:
        amt = float(amount.replace(",", "."))
        ok = storage.mark_played(int(ref[1:]), amt, "coupons") if ref.startswith("K") else \
            storage.mark_played(int(ref), amt, "picks")
    except ValueError:
        ok = False
    await update.message.reply_text("✅ Kaydedildi, karnede 'Senin oynadıkların' altında görünecek." if ok
                                    else "Bu numarada öneri bulunamadı.")


@owner_only
async def cmd_sonuc(update: Update, ctx):
    m = {"kazandi": "won", "kazandı": "won", "kaybetti": "lost", "iade": "void"}
    if len(ctx.args) < 2 or ctx.args[1].lower() not in m:
        await update.message.reply_text("Kullanım: /sonuc 12 kazandi | kaybetti | iade")
        return
    try:
        pid = int(ctx.args[0].lstrip("#"))
    except ValueError:
        await update.message.reply_text("Geçersiz numara.")
        return
    if not storage.get_pick(pid):
        await update.message.reply_text("Bu numarada öneri yok.")
        return
    storage.settle_pick(pid, m[ctx.args[1].lower()], "elle")
    await update.message.reply_text("✅ İşaretlendi.")


@owner_only
async def cmd_ayarlar(update: Update, ctx):
    s = settings
    txt = (f"⚙️ <b>Strateji</b>\n"
           f"Kasa: {report.tl(bankroll())} • Kelly payı: {s.kelly_fraction} • Tek bahis üst sınırı: %{s.max_stake_pct * 100:.0f}\n"
           f"Oran aralığı: {s.min_odds:.2f} – {s.max_odds:.2f} • En düşük olasılık: %{s.min_prob * 100:.0f}\n"
           f"En düşük değer (EV): yüksek güven %{s.min_ev['yüksek'] * 100:.0f} • orta %{s.min_ev['orta'] * 100:.0f} • "
           f"düşük %{s.min_ev['düşük'] * 100:.0f}\n"
           f"Model harmanı: piyasa %{s.w_market / (s.w_market + s.w_dc) * 100:.0f} / istatistik "
           f"%{s.w_dc / (s.w_market + s.w_dc) * 100:.0f} (/agirlik ile değişir)\n"
           f"Gemini modeli: {escape(s.gemini_model)} ({'açık' if s.gemini_api_key else 'anahtar yok'})\n"
           f"<i>Değiştirmek için sunucudaki .env dosyasını düzenleyip botu yeniden başlat.</i>")
    await send(ctx.bot, update.effective_chat.id, txt)


@owner_only
async def cmd_backtest(update: Update, ctx):
    args = [x.lower() for x in ctx.args]
    codes = list(backtest.LEAGUE_TR) if (args and args[0] == "hepsi") else \
        [LIG_ALIAS.get(args[0], "T1") if args else "T1"]
    seasons = []
    for x in args[1:]:
        if x.isdigit():
            seasons.append(int(x[:4]) if len(x) >= 4 else 2000 + int(x[:2]))
    seasons = seasons or [footballdata.current_start_year() - 1]
    runs = [(c, y) for y in seasons for c in codes]
    await update.message.reply_text(
        f"🧪 Backtest başladı: {', '.join(backtest.LEAGUE_TR[c] for c in codes)} • sezon: "
        + ", ".join(f"{y}/{(y + 1) % 100:02d}" for y in seasons)
        + f" • {len(runs)} test, her biri yaklaşık 1 dk.")
    results = []
    for c, y in runs:
        try:
            res = await backtest.run(c, y)
            results.append(res)
            if len(runs) == 1:
                await send(ctx.bot, update.effective_chat.id, backtest.format_report(res))
            else:
                await update.message.reply_text(f"✓ {backtest.LEAGUE_TR[c]} {y}/{(y + 1) % 100:02d}: "
                                                f"{res.n_matches} maç test edildi")
        except Exception as e:  # noqa: BLE001
            log.exception("backtest")
            await update.message.reply_text(f"{backtest.LEAGUE_TR[c]} {y}: hata — {e}")
    if len(runs) > 1:
        await send(ctx.bot, update.effective_chat.id, backtest.format_summary(results))
        # Yeterli örnek varsa en isabetli ayarı otomatik uygula (log-loss'a göre; kâra göre değil, aşırı uyumu önler)
        pooled: dict = {}
        for r in results:
            for k, v in r.ll.items():
                pooled.setdefault(k, []).extend(v)
        n = sum(r.n_matches for r in results)
        best = backtest.best_setting(pooled)
        if best and n >= 1500:
            model, share, gain = best
            storage.set_setting("w_share", share)
            storage.set_setting("stat_model", model)
            apply_weight(share, model)
            await send(ctx.bot, update.effective_chat.id,
                       f"⚙️ <b>Ayar güncellendi ({n} maça göre):</b> {report.mode_text()}.\n"
                       f"Değiştirmek istersen: /agirlik 0.1 sut  (pay, model: gol | sut | karma)")


@owner_only
async def cmd_agirlik(update: Update, ctx):
    """İstatistik modelinin harmandaki payını değiştirir (0 = sadece piyasa)."""
    if ctx.args:
        model = next((a.lower() for a in ctx.args if a.lower() in backtest.MODELS), settings.stat_model)
        nums = [a for a in ctx.args if a.lower() not in backtest.MODELS]
        try:
            v = float(nums[0].replace(",", ".").replace("%", "")) if nums else settings.w_dc
            v = v / 100 if v > 1 else v
            if not 0 <= v <= 1:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Kullanım: /agirlik 0.1 sut   (pay 0-1; model: gol | sut | karma; "
                                            "0 = sadece piyasa)")
            return
        storage.set_setting("w_share", v)
        storage.set_setting("stat_model", model)
        apply_weight(v, model)
    await update.message.reply_text(f"⚖️ Model: {report.mode_text()}.")


def apply_weight(share: float, model: str | None = None) -> None:
    settings.w_dc = float(share)
    settings.w_market = float(1 - share)
    if model:
        settings.stat_model = model


@owner_only
async def cmd_teshis(update: Update, ctx):
    await update.message.reply_text("🩺 Kaynaklar test ediliyor...")
    L = ["🩺 <b>Teşhis</b>"]
    evs = []
    try:
        evs = await iddaa.fetch_events()
        nm = sum(len(e.markets) for e in evs)
        nc = sum(1 for e in evs for m in e.markets if m.mtype)
        unknown = Counter(m.name for e in evs for m in e.markets if not m.mtype)
        L.append(f"✅ iddaa bülteni: {len(evs)} futbol maçı, {nm} pazar ({nc} tanesi modellenebilir)")
        if unknown:
            L.append("   Modellenmeyen en sık pazarlar: " + escape(", ".join(k for k, _ in unknown.most_common(12))))
    except Exception as e:  # noqa: BLE001
        L.append(f"❌ iddaa bülteni: {escape(str(e))}")
    try:
        fx = await espn.fixtures(list(LEAGUES), datetime.now(TZ).date(), 3)
        odds = sum(1 for f in fx if f.ext_1x2)
        lgs = len({f.league for f in fx})
        L.append(f"✅ ESPN: 4 günde {len(fx)} maç, {lgs} lig ({odds} tanesinde uluslararası oran)")
        if evs:
            up = [f for f in fx if f.state == "pre" and f.ext_1x2][:80]
            ok = [f for f in up if engine.match_iddaa(f, evs)]
            L.append(f"{'✅' if len(ok) >= 0.8 * max(len(up), 1) else '⚠️'} iddaa eşleştirme: "
                     f"{len(ok)}/{len(up)} maç")
            for f in [f for f in up if f not in ok][:8]:
                near = [x for x in evs if abs((x.kickoff - f.kickoff).total_seconds()) <= 25 * 60]
                best = max(near, key=lambda x: pair_score(f.home, f.away, x.home, x.away), default=None)
                L.append(f"   • {escape(f.home)} – {escape(f.away)} ({f.kickoff.astimezone(TZ):%d.%m %H:%M}) → "
                         + (f"en yakın iddaa: {escape(best.home)} – {escape(best.away)}" if best
                            else "aynı saatte iddaa maçı yok"))
    except Exception as e:  # noqa: BLE001
        L.append(f"❌ ESPN: {escape(str(e))}")
    lg = await footballdata.load_all()
    if lg:
        L.append("✅ football-data: " + ", ".join(f"{k} {len(v.df_cur)} maç" for k, v in lg.items()))
    else:
        L.append("❌ football-data: indirilemedi")
    if settings.gemini_api_key:
        try:
            js = await gemini._call("Sadece 'tamam' yaz.", grounded=False)
            ok = bool(js.get("candidates"))
            L.append(f"{'✅' if ok else '❌'} Gemini ({escape(gemini.last_model or settings.gemini_model)})")
        except Exception as e:  # noqa: BLE001
            L.append(f"❌ Gemini: {escape(str(e))[:200]}")
    else:
        L.append("⚪ Gemini: anahtar girilmemiş (kadro/haber analizi kapalı)")
    try:
        mid, weeks, season = mackolik.DEFAULT_IDS["T1"]
        rows = await mackolik.fetch_league(mid, 1, season)
        L.append(f"{'✅' if rows else '❌'} Maçkolik arşivi: 1. haftada {len(rows)} maç"
                 + (f", oranlı {sum(1 for r in rows if r.odds)}" if rows else ""))
    except Exception as e:  # noqa: BLE001
        L.append(f"❌ Maçkolik arşivi: {escape(str(e))}")
    await send(ctx.bot, update.effective_chat.id, "\n".join(L))


# ----------------------------------------------------------------- zamanlanmış görevler
async def job_daily(ctx: ContextTypes.DEFAULT_TYPE):
    if owner_id():
        try:
            await publish(ctx.bot, owner_id(), "Günün Analizi", settings.days_ahead)
        except Exception:  # noqa: BLE001
            log.exception("günlük rapor")


async def job_settle(ctx: ContextTypes.DEFAULT_TYPE):
    if not owner_id():
        return
    try:
        rows = await tracker.settle_due()
    except Exception:  # noqa: BLE001
        log.exception("sonuçlandırma")
        return
    if not rows:
        return
    icon = {"won": "✅", "lost": "❌", "void": "↩️", "manual": "❔"}
    L = ["📣 <b>Sonuçlar</b>"]
    for r in rows:
        pr = ""
        if r["status"] == "won":
            pr = f" +{r['stake'] * (r['odds'] - 1):.0f} TL"
        elif r["status"] == "lost":
            pr = f" -{r['stake']:.0f} TL"
        extra = " — otomatik belirlenemedi, /sonuc ile işaretle" if r["status"] == "manual" else ""
        tag = f"#{r['id']}" if r.get("is_single") else f"#{r['id']} (kupon ayağı)"
        L.append(f"{icon[r['status']]} {tag} {escape(r['home'])} – {escape(r['away'])} ({escape(r['score'])}): "
                 f"{escape(r['market'])}: {escape(r['outcome'])} @ {r['odds']:.2f}{pr}{extra}")
    await send(ctx.bot, owner_id(), "\n".join(L))


async def job_odds(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        await tracker.refresh_odds()
    except Exception:  # noqa: BLE001
        log.exception("oran güncelleme")


async def job_prematch(ctx: ContextTypes.DEFAULT_TYPE):
    """Maçtan ~1 saat önce: ilk 11 ve oran kontrolü."""
    if not owner_id():
        return
    now = datetime.now(timezone.utc)
    due = [r for r in storage.pending_picks() if r["is_single"] and not r["prematch_sent"]
           and timedelta(minutes=40) <= datetime.fromisoformat(r["kickoff"]) - now <= timedelta(minutes=75)]
    if not due:
        return
    await tracker.refresh_odds()
    for r in due:
        r = storage.get_pick(r["id"])
        sm = await espn.summary(r["league"], r["espn_id"], ttl=600)
        L = [f"⏰ <b>{escape(r['home'])} – {escape(r['away'])}</b> yaklaşık 1 saat sonra • #{r['id']}",
             f"Öneri: {escape(r['market'])}: <b>{escape(r['outcome'])}</b> @ {r['odds']:.2f}"]
        lo = r["last_odds"] or r["odds"]
        ev_now = r["prob"] * lo - 1
        if abs(lo - r["odds"]) > 0.005:
            L.append(f"Oran şimdi {lo:.2f} → değer {ev_now * 100:+.1f}%"
                     + (" ⚠️ <b>artık değer yok, oynama</b>" if ev_now < 0 else ""))
        if sm.lineups:
            L.append("👥 İlk 11 açıklandı:")
            for tid, players in sm.lineups.items():
                L.append(f"   {escape(', '.join(players))}")
            L.append("<i>Kilit oyuncu yoksa /analiz ile tekrar kontrol edebilirsin.</i>")
        else:
            L.append("İlk 11 henüz açıklanmadı.")
        storage.mark_prematch(r["id"])
        await send(ctx.bot, owner_id(), "\n".join(L))


async def job_late(ctx: ContextTypes.DEFAULT_TYPE):
    """Maçlara 20 dk - 2.5 saat kala: uluslararası oranlar oynarken iddaa geride kalırsa değer doğar.
    Yeni değerli seçimi bildirir; mevcut önerilerin kapanış olasılığını kaydeder (karne için)."""
    if not owner_id() or LOCK.locked():
        return
    now = datetime.now(timezone.utc)
    async with LOCK:
        br = bankroll()
        try:
            sc = await engine.scan(days_ahead=1, only_upcoming=True, bankroll=br)
        except Exception:  # noqa: BLE001
            log.exception("son dakika taraması")
            return
        window = [a for a in sc.analyses if a.ev and timedelta(minutes=5) <= a.fx.kickoff - now <= timedelta(minutes=150)]
        for a in window:  # kapanış ölçümü: önerilmiş seçimlerin güncel piyasa olasılığı ve oranı
            for c in a.candidates:
                storage.update_close(a.fx.espn_id, c.market, c.outcome, c.p, c.odds)
        if not settings.value_picks:
            return
        fresh = [a for a in window if a.fx.kickoff - now >= timedelta(minutes=20)
                 and not storage.match_has_single(a.fx.espn_id) and engine.near_value(a)]
        for a in fresh[:6]:
            try:
                await engine.enrich(a, br, with_news=True)
            except Exception:  # noqa: BLE001
                log.exception("zenginleştirme")
        new = []
        for a in fresh:
            if a.best:
                new.append((a, a.best, storage.save_pick(a, a.best, single=True)))
    if new:
        L = ["⚡ <b>Son dakika değer</b> — maç saatine yakın oranlar değişti, iddaa geride kaldı"]
        for k, (a, c, pid) in enumerate(new, 1):
            L.append(report.pick_block(a, c, k, pid))
            L.append("")
        L.append("<i>Oynarsan /oynadim #no tutar ile işaretle.</i>")
        await send(ctx.bot, owner_id(), "\n".join(L))


async def job_weekly(ctx: ContextTypes.DEFAULT_TYPE):
    if owner_id():
        await send(ctx.bot, owner_id(), "🗓 <b>Haftalık karne</b>\n" + report.karne(tracker.report_card()))


# ----------------------------------------------------------------- Telegram'dan güncelleme
def _restart_soon(delay: float = 2.0) -> None:
    """Süreci kapatır; systemd (Restart=always) botu yeni kodla birkaç saniyede açar."""
    asyncio.get_running_loop().call_later(delay, os._exit, 0)


@owner_only
async def on_document(update: Update, ctx):
    doc = update.message.document
    name = (doc.file_name or "").lower()
    if not (name.endswith(".zip") and "iddaa-bot" in name):
        await update.message.reply_text("Bu dosyayı tanımadım. Güncelleme için iddaa-bot ile başlayan zip'i gönder.")
        return
    await update.message.reply_text(f"📦 {doc.file_name} alındı. Testler çalıştırılıyor (1-2 dk)...")
    updater.WORK.mkdir(parents=True, exist_ok=True)
    path = updater.WORK / "upload.zip"
    f = await doc.get_file()
    await f.download_to_drive(custom_path=str(path))
    async with LOCK:
        try:
            ver, summary = await asyncio.to_thread(updater.apply_zip, path)
        except updater.UpdateError as e:
            await send(ctx.bot, update.effective_chat.id, f"❌ <b>Güncelleme uygulanmadı.</b> Bot eski sürümle "
                                                           f"çalışmaya devam ediyor.\n<pre>{escape(str(e))[:1500]}</pre>")
            return
        except Exception as e:  # noqa: BLE001
            log.exception("güncelleme")
            await update.message.reply_text(f"❌ Beklenmeyen hata, güncelleme uygulanmadı: {e}")
            return
    await update.message.reply_text(f"✅ Testler geçti ({summary}). Sürüm {ver} kuruldu, bot yeniden başlıyor...")
    _restart_soon()


@owner_only
async def cmd_surum(update: Update, ctx):
    await update.message.reply_text(f"🤖 Bot sürümü: {updater.current_version()}\n"
                                    "Güncellemek için /guncelle yaz (ya da zip dosyasını bu sohbete gönder). "
                                    "Sorun olursa /geri ile önceki sürüme dönülür.")


def _gh() -> tuple[str, str]:
    return (storage.get_setting("github_repo") or settings.github_repo,
            storage.get_setting("github_token") or settings.github_token)


@owner_only
async def cmd_github(update: Update, ctx):
    """/github sahip/depo [token] — güncellemelerin çekileceği GitHub deposu."""
    if not ctx.args:
        repo, tok = _gh()
        await update.message.reply_text(f"GitHub deposu: {repo} • token: {'var' if tok else 'yok (depo herkese açık olmalı)'}\n"
                                        "Değiştirmek için: /github sahip/depo [token]")
        return
    repo = ctx.args[0].strip()
    storage.set_setting("github_repo", repo)
    if len(ctx.args) > 1:
        storage.set_setting("github_token", ctx.args[1].strip())
        try:  # token sohbette kalmasın
            await update.message.delete()
        except Exception:  # noqa: BLE001
            pass
    await ctx.bot.send_message(update.effective_chat.id, f"✅ GitHub deposu kaydedildi: {repo}. Artık /guncelle yazman yeterli.")


@owner_only
async def cmd_guncelle(update: Update, ctx):
    """GitHub'daki son sürümü indirir, testleri çalıştırır, geçerse kurar ve yeniden başlar."""
    repo, tok = _gh()
    await update.message.reply_text(f"🔄 {repo} kontrol ediliyor...")
    hdr = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if tok:
        hdr["Authorization"] = f"Bearer {tok}"
    from .http import client
    try:
        r = await client().get(f"https://api.github.com/repos/{repo}/commits?per_page=1", headers=hdr)
        if r.status_code == 404:
            await update.message.reply_text("Depo bulunamadı. Depo gizliyse token gerekir: /github sahip/depo TOKEN")
            return
        if r.status_code == 409:
            await update.message.reply_text("Depo henüz boş; kod yüklenince tekrar dene.")
            return
        r.raise_for_status()
        last = r.json()[0]
        sha, msg = last["sha"][:7], last["commit"]["message"].splitlines()[0][:120]
        if storage.get_setting("installed_sha") == sha:
            await update.message.reply_text(f"✅ Zaten güncel (sürüm {updater.current_version()}, {sha}).")
            return
        z = await client().get(f"https://api.github.com/repos/{repo}/zipball", headers=hdr, timeout=120)
        z.raise_for_status()
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(f"❌ GitHub'a ulaşılamadı: {e}")
        return
    updater.WORK.mkdir(parents=True, exist_ok=True)
    path = updater.WORK / "github.zip"
    path.write_bytes(z.content)
    await update.message.reply_text(f"📦 Yeni sürüm bulundu ({sha}: {msg}). Testler çalıştırılıyor (1-2 dk)...")
    async with LOCK:
        try:
            ver, summary = await asyncio.to_thread(updater.apply_zip, path)
        except updater.UpdateError as e:
            await send(ctx.bot, update.effective_chat.id, f"❌ <b>Güncelleme uygulanmadı.</b> Bot eski sürümle "
                                                           f"çalışmaya devam ediyor.\n<pre>{escape(str(e))[:1500]}</pre>")
            return
    storage.set_setting("installed_sha", sha)
    await update.message.reply_text(f"✅ Testler geçti ({summary}). Sürüm {ver} kuruldu, bot yeniden başlıyor...")
    _restart_soon()


@owner_only
async def cmd_geri(update: Update, ctx):
    ver = await asyncio.to_thread(updater.rollback)
    if not ver:
        await update.message.reply_text("Geri dönülecek yedek yok.")
        return
    await update.message.reply_text(f"↩️ Sürüm {ver} geri yüklendi, bot yeniden başlıyor...")
    _restart_soon()


async def post_init(app: Application):
    storage.init()
    mk = updater.pop_marker()
    if mk and owner_id():
        ver, _, info = mk.partition("\n")
        try:
            await app.bot.send_message(owner_id(), f"✅ Bot yeni sürümle çalışıyor: {ver} ({info.strip()}).")
        except Exception:  # noqa: BLE001
            log.exception("güncelleme bildirimi")
    jq = app.job_queue
    jq.run_daily(job_daily, time=dtime(settings.daily_report_hour, 0, tzinfo=TZ), name="gunluk")
    jq.run_daily(job_weekly, time=dtime(settings.daily_report_hour, 30, tzinfo=TZ), days=(0,), name="haftalik")
    jq.run_repeating(job_settle, interval=3600, first=300, name="sonuc")
    jq.run_repeating(job_odds, interval=2 * 3600, first=900, name="oran")
    jq.run_repeating(job_prematch, interval=15 * 60, first=60, name="maconcesi")
    jq.run_repeating(job_late, interval=15 * 60, first=180, name="sondakika")
    await app.bot.set_my_commands([
        ("bugun", "Bugünün değerli seçimleri ve kuponlar"), ("kupon", "Günün kuponu"), ("yarin", "Yarının analizi"),
        ("analiz", "Tek maç tam analiz: /analiz takım"), ("karne", "Sonuç karnesi"),
        ("oynadim", "Oynadığını işaretle"), ("sonuc", "Elle sonuç gir"), ("kasa", "Kasa tutarı"),
        ("backtest", "Geçmiş maç testi"), ("agirlik", "Model harmanı"), ("teshis", "Kaynak kontrolü"),
        ("ayarlar", "Strateji eşikleri"), ("guncelle", "Son sürümü kur"), ("surum", "Sürüm bilgisi"), ("geri", "Önceki sürüme dön"),
        ("yardim", "Komutlar"),
    ])


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not settings.telegram_token:
        raise SystemExit("TELEGRAM_TOKEN .env dosyasında yok.")
    storage.init()
    w = storage.get_setting("w_share")
    if w is None:
        # 6 lig x 2 sezon backtest (4.130 maç): istatistik modeli eklemek isabeti düşürdü -> sadece piyasa
        w = 0.0
        storage.set_setting("w_share", w)
    apply_weight(float(w), storage.get_setting("stat_model", settings.stat_model))
    vp = storage.get_setting("value_picks")
    if vp is not None:
        settings.value_picks = bool(vp)
    app = Application.builder().token(settings.telegram_token).post_init(post_init).build()
    for name, fn in (("start", cmd_start), ("yardim", cmd_start), ("bugun", cmd_bugun), ("yarin", cmd_yarin),
                     ("analiz", cmd_analiz), ("karne", cmd_karne), ("kasa", cmd_kasa),
                     ("oynadim", cmd_oynadim), ("sonuc", cmd_sonuc), ("ayarlar", cmd_ayarlar),
                     ("backtest", cmd_backtest), ("teshis", cmd_teshis), ("agirlik", cmd_agirlik),
                     ("surum", cmd_surum), ("geri", cmd_geri), ("kupon", cmd_kupon),
                     ("guncelle", cmd_guncelle), ("github", cmd_github), ("deger", cmd_deger)):
        app.add_handler(CommandHandler(name, fn, block=False))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document, block=False))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
