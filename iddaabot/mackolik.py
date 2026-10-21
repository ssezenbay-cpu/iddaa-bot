"""Maçkolik arşivi: geçmiş maçların iddaa MS oranları (sadece backtest için).

Kimlikler sezona özeldir. Varsayılanlar iddaa-mcp açık kaynak projesindeki listeden alınmıştır;
hangi sezona ait olduğu dönen maç tarihlerinden otomatik anlaşılır. Başka sezonlar için
data/mackolik_ids.json dosyasına {"T1": {"2024": 12345}} biçiminde ekleme yapılabilir.
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime

from .config import DATA_DIR
from .http import get_text

log = logging.getLogger(__name__)
URL = "https://arsiv.mackolik.com/AjaxHandlers/FixtureHandler.aspx?command=getMatches&id={id}&week={week}"
# (kimlik, hafta sayısı, sezon başlangıç yılı) — 70381 = Süper Lig 2025/26 (sunucuda doğrulandı)
DEFAULT_IDS = {"T1": (70381, 34, 2025), "E0": (70266, 38, 2025), "SP1": (70368, 38, 2025),
               "I1": (70184, 38, 2025), "D1": (70371, 34, 2025), "F1": (70351, 34, 2025)}


@dataclass
class MkMatch:
    date: datetime
    home: str
    away: str
    hg: int | None
    ag: int | None
    odds: tuple[float, float, float] | None
    ht: tuple[int, int] | None


def league_ids(code: str) -> list[tuple[int, int, int]]:
    """(kimlik, hafta sayısı, sezon başlangıç yılı) listesi."""
    out = []
    f = DATA_DIR / "mackolik_ids.json"
    if f.exists():
        try:
            for season, mid in (json.loads(f.read_text()).get(code) or {}).items():
                weeks = DEFAULT_IDS.get(code, (0, 38, 0))[1]
                out.append((int(mid), weeks, int(season)))
        except (ValueError, OSError) as e:
            log.warning("mackolik_ids.json okunamadı: %s", e)
    if code in DEFAULT_IDS:
        out.append(DEFAULT_IDS[code])
    return out


def _parse_payload(text: str):
    t = text.strip()
    try:
        data = json.loads(t)
    except ValueError:
        if not t or t[0] not in "[{":
            raise ValueError("beklenmeyen biçim")
        py = re.sub(r"\btrue\b", "True", re.sub(r"\bfalse\b", "False", re.sub(r"\bnull\b", "None", t)))
        # JS seyrek dizi: [1,,2] -> [1,None,2]
        py = re.sub(r"(?<=[\[,])\s*(?=[,\]])", "None", py)
        data = ast.literal_eval(py)
    if isinstance(data, dict) and isinstance(data.get("d"), str):
        return _parse_payload(data["d"])
    return data


def _rows(node, depth=0):
    if depth > 5 or node is None:
        return []
    if isinstance(node, list) and node and isinstance(node[0], (list, dict)):
        return node
    if isinstance(node, dict):
        for v in node.values():
            r = _rows(v, depth + 1)
            if r:
                return r
    return []


def _num(v):
    try:
        x = float(str(v).replace(",", ".").strip())
        return x
    except (TypeError, ValueError):
        return None


def _date(v, season_start: int | None = None):
    s = str(v).strip()
    m = re.search(r"Date\((\d{10,13})", s)
    if m:
        ts = int(m.group(1))
        return datetime.fromtimestamp(ts / 1000 if ts > 1e11 else ts)
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})", s)
    if m:
        y = int(m.group(3))
        return datetime(y + 2000 if y < 100 else y, int(m.group(2)), int(m.group(1)))
    m = re.fullmatch(r"(\d{1,2})[./](\d{1,2})", s)   # Maçkolik arşivi: "08/08" (yılsız)
    if m and season_start:
        d, mo = int(m.group(1)), int(m.group(2))
        return datetime(season_start if mo >= 7 else season_start + 1, mo, d)
    return None


def parse_row(e, season_start: int | None = None) -> MkMatch | None:
    if isinstance(e, list):
        if len(e) < 14:
            return None
        d = {"date": e[1], "home": e[4], "away": e[6], "hg": e[9], "ag": e[10],
             "oh": e[11], "od": e[12], "oa": e[13], "ht": e[23] if len(e) > 23 else None}
    elif isinstance(e, dict):
        g = lambda *ks: next((e[k] for k in ks if k in e and e[k] not in (None, "")), None)  # noqa: E731
        d = {"date": g("date", "match_date", "d"), "home": g("home_team", "homeTeam", "HomeTeamName", "hn"),
             "away": g("away_team", "awayTeam", "AwayTeamName", "an"), "hg": g("home_goals", "hs"),
             "ag": g("away_goals", "as"), "oh": g("odds_home", "ms1"), "od": g("odds_draw", "msx", "ms0"),
             "oa": g("odds_away", "ms2"), "ht": g("halftime_score", "iy")}
    else:
        return None
    dt = _date(d["date"], season_start)
    if not dt or not d["home"] or not d["away"]:
        return None
    oh, od, oa = _num(d["oh"]), _num(d["od"]), _num(d["oa"])
    odds = (oh, od, oa) if all(x and x > 1 for x in (oh, od, oa)) else None
    hg, ag = _num(d["hg"]), _num(d["ag"])
    ht = None
    if d["ht"]:
        m = re.search(r"(\d+)\s*[-:]\s*(\d+)", str(d["ht"]))
        if m:
            ht = (int(m.group(1)), int(m.group(2)))
    return MkMatch(dt, str(d["home"]).strip(), str(d["away"]).strip(),
                   int(hg) if hg is not None else None, int(ag) if ag is not None else None, odds, ht)


async def fetch_league(mid: int, weeks: int, season_start: int | None = None) -> list[MkMatch]:
    out = []
    for w in range(1, weeks + 1):
        try:
            text = await get_text(URL.format(id=mid, week=w), ttl=30 * 24 * 3600)
            for e in _rows(_parse_payload(text)):
                m = parse_row(e, season_start)
                if m:
                    out.append(m)
        except Exception as e:  # noqa: BLE001
            log.warning("Maçkolik %s hafta %s: %s", mid, w, e)
            if w <= 2 and not out:
                break  # bağlantı/biçim sorunu — boşuna devam etme
        await asyncio.sleep(0.4)
    return out


async def season_odds(code: str, season_start: int) -> list[MkMatch]:
    """İstenen sezonun (ör. 2025 = 2025/26) iddaa oranları; yoksa boş liste."""
    for mid, weeks, season in league_ids(code):
        if season != season_start:
            continue
        rows = await fetch_league(mid, weeks, season_start)
        if rows:
            return rows
    return []
