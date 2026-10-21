"""Önbellekli asenkron HTTP yardımcıları."""
from __future__ import annotations

import hashlib
import json
import logging
import time

import httpx

from .config import DATA_DIR

log = logging.getLogger(__name__)
CACHE = DATA_DIR / "cache"
CACHE.mkdir(parents=True, exist_ok=True)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(25.0, connect=10.0),
            headers={"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                     "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8"},
            follow_redirects=True,
        )
    return _client


def _path(url: str, suffix: str):
    return CACHE / (hashlib.sha1(url.encode()).hexdigest() + suffix)


async def get_text(url: str, ttl: float, headers: dict | None = None) -> str:
    """ttl saniye içinde önbellekten; hata olursa eski önbelleği döndürür."""
    p = _path(url, ".txt")
    if p.exists() and time.time() - p.stat().st_mtime < ttl:
        return p.read_text(encoding="utf-8", errors="replace")
    try:
        r = await client().get(url, headers=headers)
        r.raise_for_status()
        text = r.text
        p.write_text(text, encoding="utf-8")
        return text
    except Exception as e:  # noqa: BLE001
        if p.exists():
            log.warning("%s alınamadı (%s), eski önbellek kullanılıyor", url, e)
            return p.read_text(encoding="utf-8", errors="replace")
        raise


async def get_json(url: str, ttl: float, headers: dict | None = None):
    return json.loads(await get_text(url, ttl, headers))
