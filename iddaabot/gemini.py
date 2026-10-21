"""Gemini + Google Arama: sakat/cezalı oyuncular, muhtemel 11, maç öncesi haberler."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

from .config import DATA_DIR, settings
from .http import client

log = logging.getLogger(__name__)
URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
CACHE_F = DATA_DIR / "gemini_cache.json"
TTL = 6 * 3600

PROMPT = """Sen deneyimli bir futbol analistisin. Google araması yaparak aşağıdaki maç hakkında
GÜNCEL (son 7 gün) bilgileri bul. Tahmin uydurma; bulamadığın bilgiyi boş bırak.

Maç: {home} (ev sahibi) - {away} (deplasman)
Organizasyon: {league}
Tarih (TR saati): {when}

Bulman gerekenler:
1. Her iki takımın sakat, cezalı ve şüpheli oyuncuları (önem derecesiyle: kilit oyuncu mu?)
2. Muhtemel 11'ler veya rotasyon beklentisi (yoğun fikstür, Avrupa maçı sonrası yorgunluk)
3. Teknik direktör değişikliği, kulüp içi kriz, motivasyon (derbi, küme düşme, şampiyonluk yarışı)
4. Hava/zemin gibi gol sayısını etkileyebilecek durumlar
5. Maçın hakemi ve bu sezonki maç başı kart ortalaması (bulabilirsen)

Sonra bu bilgilere göre, piyasanın henüz fiyatlamamış olabileceği etkiyi düşünerek her takımın
beklenen gol sayısı için bir çarpan öner (1.00 = etki yok; kilit forvet yoksa ör. 0.92;
rakibin kilit stoperi/kalecisi yoksa ör. 1.06). Çarpanlar 0.85 ile 1.10 arasında olmalı.

YALNIZCA şu JSON'u döndür (```json bloğu içinde):
{{"ev_eksikler": [{{"oyuncu": "", "durum": "sakat|cezalı|şüpheli", "kilit": true}}],
  "dep_eksikler": [{{"oyuncu": "", "durum": "", "kilit": false}}],
  "ev_gol_carpani": 1.00,
  "dep_gol_carpani": 1.00,
  "hakem": "Ad Soyad — maç başı X sarı kart (bulunamadıysa boş)",
  "notlar": "2-4 cümlelik Türkçe özet: kadro durumu, motivasyon, dikkat edilmesi gerekenler",
  "bilgi_guveni": "düşük|orta|yüksek"}}
"""


_mem: dict | None = None


def _load_cache() -> dict:
    global _mem
    if _mem is None:
        try:
            _mem = json.loads(CACHE_F.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _mem = {}
    return _mem


def _save_cache(c: dict) -> None:
    now = time.time()
    for k in [k for k, v in c.items() if now - v.get("_t", 0) > 3 * 24 * 3600]:
        c.pop(k, None)
    tmp = CACHE_F.with_suffix(".tmp")
    tmp.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CACHE_F)


def _extract_json(text: str) -> dict | None:
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S) or re.search(r"(\{.*\})", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


def _clamp(v, lo=0.85, hi=1.10) -> float:
    try:
        return float(min(max(float(v), lo), hi))
    except (TypeError, ValueError):
        return 1.0


FALLBACKS = [m.strip() for m in os.getenv(
    "GEMINI_FALLBACKS", "gemini-3.6-flash,gemini-3.5-flash-lite,gemini-2.5-flash").split(",") if m.strip()]
RETRY = {429, 500, 502, 503, 504}
last_model: str = ""


async def _call(prompt: str, grounded: bool) -> dict:
    """Yoğunluk (503/429) olursa bekleyip tekrar dener, sonra yedek modellere geçer."""
    global last_model
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    if grounded:
        body["tools"] = [{"google_search": {}}]
    models = [settings.gemini_model] + [m for m in FALLBACKS if m != settings.gemini_model]
    err: Exception | None = None
    for model in models:
        for wait in (0, 3, 8):
            if wait:
                await asyncio.sleep(wait)
            try:
                r = await client().post(URL.format(model=model), json=body,
                                        headers={"x-goog-api-key": settings.gemini_api_key}, timeout=90)
            except Exception as e:  # noqa: BLE001  (ağ hatası)
                err = e
                continue
            if r.status_code in RETRY:
                err = RuntimeError(f"{model}: HTTP {r.status_code}")
                continue
            if r.status_code == 404:  # model adı bu hesapta yok
                err = RuntimeError(f"{model}: model bulunamadı")
                break
            r.raise_for_status()
            last_model = model
            return r.json()
    raise err or RuntimeError("Gemini yanıt vermedi")


async def team_news(key: str, home: str, away: str, league: str, when: str) -> dict | None:
    """Maç haberleri. key: önbellek anahtarı (ESPN maç kimliği)."""
    if not settings.gemini_api_key:
        return None
    cache = _load_cache()
    if key in cache and time.time() - cache[key].get("_t", 0) < TTL:
        return cache[key]
    prompt = PROMPT.format(home=home, away=away, league=league, when=when)
    js = None
    for grounded in (True, False):
        try:
            js = await _call(prompt, grounded)
            break
        except Exception as e:  # noqa: BLE001
            log.warning("Gemini (arama=%s) hata: %s", grounded, e)
    if js is None:
        return None
    try:
        cand = js["candidates"][0]
        text = "".join(p.get("text", "") for p in cand["content"]["parts"])
    except (KeyError, IndexError):
        return None
    data = _extract_json(text) or {"notlar": text.strip()[:600]}
    data["ev_gol_carpani"] = _clamp(data.get("ev_gol_carpani", 1.0))
    data["dep_gol_carpani"] = _clamp(data.get("dep_gol_carpani", 1.0))
    srcs = []
    for ch in (cand.get("groundingMetadata") or {}).get("groundingChunks") or []:
        w = ch.get("web") or {}
        if w.get("uri"):
            srcs.append({"title": w.get("title", ""), "uri": w["uri"]})
    data["kaynaklar"] = srcs[:3]
    data["_t"] = time.time()
    cache[key] = data
    _save_cache(cache)
    return data
