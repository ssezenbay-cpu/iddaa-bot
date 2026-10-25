"""Geçmiş isabet: benzer olasılıklı maçlarda bu seçenek gerçekte ne sıklıkla tuttu.

Tablo (hist_rates.json) 9 ligin 2012/13-2025/26 sezonlarından (yaklaşık 30 bin maç, Bet365 oranları,
marjı temizlenmiş) üretildi. Gol pazarları (alt/üst, KG) maçın 2.5 üst olasılığına göre, MS ve çifte
şans kendi olasılığına göre kovalanır (her kova %5 genişliğinde).
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .markets import selection_prob

MIN_N = 60          # kovada bundan az maç varsa 9 ligin toplamına bakılır
_OU25 = {"kind": "ou", "subject": "goals", "period": "FT", "scope": "total", "line": 2.5, "side": "over"}


@lru_cache(maxsize=1)
def _table() -> dict:
    p = Path(__file__).with_name("hist_rates.json")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def meta() -> dict:
    return _table().get("meta", {})


def _key(spec: dict) -> tuple[str, str] | None:
    if spec.get("subject", "goals") != "goals" or spec.get("period", "FT") != "FT":
        return None
    k = spec.get("kind")
    if k == "ou" and spec.get("scope", "total") == "total":
        ln = float(spec["line"])
        if ln in (0.5, 1.5, 2.5, 3.5, 4.5):
            return "goals", f"{'o' if spec['side'] == 'over' else 'u'}{ln}"
    if k == "btts":
        return "goals", "btts_y" if spec.get("yes") else "btts_n"
    if k == "1x2" and spec.get("sel") in ("1", "X", "2"):
        return "res", spec["sel"]
    if k == "dc":
        sel = "".join(spec.get("sel", ()))
        return ("res", sel) if sel in ("1X", "X2", "12") else None
    return None


def rate(league: str, spec: dict, gm) -> tuple[float, int, str] | None:
    """(geçmiş tutma oranı, maç sayısı, kapsam) ya da None. gm: maç oranlarından kurulan gol modeli."""
    t = _table()
    key = _key(spec)
    if not t or key is None or gm is None:
        return None
    group, name = key
    q = selection_prob(_OU25 if group == "goals" else spec, gm, None, None)
    if q is None:
        return None
    nb = t["meta"]["kova"]
    i = min(max(int(q * nb), 0), nb - 1)
    for lg, label in ((league, "lig"), ("all", "9 lig")):
        rows = t["leagues"].get(lg, {}).get(group, {}).get(name)
        if not rows:
            continue
        hit, n = rows[i]
        if n >= MIN_N:
            return hit / n, n, label
    return None
