"""ESPN açık verisi: fikstür, sonuçlar, uluslararası oranlar, son maçlar, H2H, kadrolar, hakem."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from .http import get_json
from .markets import american_to_decimal

log = logging.getLogger(__name__)
SITE = "https://site.api.espn.com/apis/site/v2/sports/soccer"


@dataclass
class Fixture:
    espn_id: str
    league: str
    kickoff: datetime
    home: str
    away: str
    home_id: str
    away_id: str
    state: str                 # pre / in / post
    completed: bool
    status_name: str
    home_score: int | None = None
    away_score: int | None = None
    ht_home: int | None = None
    ht_away: int | None = None
    ext_1x2: tuple[float, float, float] | None = None   # ondalık oranlar
    ext_ou: tuple[float, float, float] | None = None    # (çizgi, üst oranı, alt oranı)
    ext_provider: str = ""
    form_home: str = ""
    form_away: str = ""
    venue: str = ""


def _odds_val(node, *path):
    cur = node
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _pick(odds: dict, side: str):
    """moneyline.home.{current|close|open}.odds veya eski biçim homeTeamOdds.moneyLine."""
    for stage in ("current", "close", "open"):
        v = _odds_val(odds, "moneyline", side, stage, "odds")
        if v is not None:
            return american_to_decimal(v)
    legacy = {"home": ("homeTeamOdds", "moneyLine"), "away": ("awayTeamOdds", "moneyLine"),
              "draw": ("drawOdds", "moneyLine")}[side]
    return american_to_decimal(_odds_val(odds, *legacy))


def parse_odds(odds_list) -> tuple:
    if not odds_list:
        return None, None, ""
    o = sorted(odds_list, key=lambda x: -(x.get("provider", {}).get("priority", 0) or 0))[0]
    prov = (o.get("provider") or {}).get("name", "")
    h, d, a = _pick(o, "home"), _pick(o, "draw"), _pick(o, "away")
    x12 = (h, d, a) if all(v and v > 1 for v in (h, d, a)) else None
    line = o.get("overUnder")
    ov = un = None
    for stage in ("current", "close", "open"):
        if ov is None:
            ov = american_to_decimal(_odds_val(o, "total", "over", stage, "odds"))
        if un is None:
            un = american_to_decimal(_odds_val(o, "total", "under", stage, "odds"))
        if line is None:
            lv = _odds_val(o, "total", "over", stage, "line")
            if lv is not None:
                m = re.search(r"(\d+(?:\.\d+)?)", str(lv))
                line = float(m.group(1)) if m else None
    ov = ov or american_to_decimal(o.get("overOdds"))
    un = un or american_to_decimal(o.get("underOdds"))
    ou = None
    try:
        if line is not None and ov and un and float(line) % 1 == 0.5:
            ou = (float(line), ov, un)
    except (TypeError, ValueError):
        ou = None
    return x12, ou, prov


def _score(c) -> int | None:
    try:
        return int(float(c.get("score")))
    except (TypeError, ValueError):
        return None


def _regulation(comp, home, away):
    """Uzatma varsa normal süre skorunu (ilk iki devre) döndür."""
    try:
        lh = [float(x.get("value", x.get("displayValue", 0))) for x in home.get("linescores", [])]
        la = [float(x.get("value", x.get("displayValue", 0))) for x in away.get("linescores", [])]
        if len(lh) >= 2 and len(la) >= 2:
            return int(lh[0]), int(la[0]), int(lh[0] + lh[1]), int(la[0] + la[1])
    except (TypeError, ValueError):
        pass
    return None


def parse_scoreboard(js: dict, league: str) -> list[Fixture]:
    out = []
    for ev in js.get("events", []):
        try:
            comp = ev["competitions"][0]
            cs = comp["competitors"]
            home = next(c for c in cs if c.get("homeAway") == "home")
            away = next(c for c in cs if c.get("homeAway") == "away")
        except (KeyError, IndexError, StopIteration):
            continue
        st = (ev.get("status") or {}).get("type") or {}
        ko = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
        x12, ou, prov = parse_odds(comp.get("odds"))
        f = Fixture(
            espn_id=str(ev["id"]), league=league, kickoff=ko,
            home=home["team"].get("displayName", ""), away=away["team"].get("displayName", ""),
            home_id=str(home["team"].get("id", "")), away_id=str(away["team"].get("id", "")),
            state=st.get("state", ""), completed=bool(st.get("completed")), status_name=st.get("name", ""),
            ext_1x2=x12, ext_ou=ou, ext_provider=prov,
            form_home=home.get("form", "") or "", form_away=away.get("form", "") or "",
            venue=((comp.get("venue") or {}).get("fullName", "")),
        )
        if f.state == "post" or f.completed:
            f.home_score, f.away_score = _score(home), _score(away)
            reg = _regulation(comp, home, away)
            if reg:
                f.ht_home, f.ht_away, f.home_score, f.away_score = reg
        out.append(f)
    return out


async def scoreboard(league: str, day: date, ttl: float = 600) -> list[Fixture]:
    url = f"{SITE}/{league}/scoreboard?dates={day:%Y%m%d}&limit=200"
    try:
        return parse_scoreboard(await get_json(url, ttl=ttl), league)
    except Exception as e:  # noqa: BLE001
        log.warning("ESPN %s %s alınamadı: %s", league, day, e)
        return []


async def _range(league: str, start: date, end: date, ttl: float) -> list[Fixture] | None:
    url = f"{SITE}/{league}/scoreboard?dates={start:%Y%m%d}-{end:%Y%m%d}&limit=300"
    try:
        return parse_scoreboard(await get_json(url, ttl=ttl), league)
    except Exception as e:  # noqa: BLE001
        log.info("ESPN aralık sorgusu %s başarısız (%s), günlük sorguya geçiliyor", league, e)
        return None


async def fixtures(leagues: list[str], start: date, days: int, ttl: float = 600) -> list[Fixture]:
    """start'tan itibaren days+1 günün maçları. Önce tek aralık sorgusu, olmazsa gün gün."""
    out, seen = [], set()
    end = start + timedelta(days=days)
    for lg in leagues:
        got = await _range(lg, start, end, ttl) if days >= 1 else None
        if got is None:
            got = []
            for k in range(days + 1):
                got += await scoreboard(lg, start + timedelta(days=k), ttl)
        for f in got:
            if f.espn_id not in seen:
                seen.add(f.espn_id)
                out.append(f)
    return out


@dataclass
class Summary:
    last5: dict = field(default_factory=dict)       # team_id -> [ "G 2-1 (D) Rakip", ...]
    h2h: list = field(default_factory=list)
    lineups: dict = field(default_factory=dict)     # team_id -> [oyuncu]
    referee: str = ""
    stats: dict = field(default_factory=dict)       # 'home'/'away' -> {istatistik adı: değer}
    last_dates: dict = field(default_factory=dict)  # team_id -> [önceki maç tarihleri]

    def fatigue(self, tid: str, kickoff: datetime) -> tuple[int, int] | None:
        """(son maçtan bu yana gün, son 7 gündeki maç sayısı)"""
        ds = [d for d in self.last_dates.get(tid, []) if d < kickoff]
        if not ds:
            return None
        rest = (kickoff - max(ds)).days
        return rest, sum(1 for d in ds if (kickoff - d).days <= 7)

    def count(self, side: str, *keys: str) -> int | None:
        d = self.stats.get(side) or {}
        tot, found = 0.0, False
        for k in keys:
            for name, v in d.items():
                if name.lower() == k.lower():
                    tot += v
                    found = True
        return int(tot) if found else None

    def corners(self):
        h, a = self.count("home", "wonCorners", "cornerKicks", "corners"), \
            self.count("away", "wonCorners", "cornerKicks", "corners")
        return (h, a) if h is not None and a is not None else None

    def cards(self):
        h, a = self.count("home", "yellowCards", "redCards"), self.count("away", "yellowCards", "redCards")
        return (h, a) if h is not None and a is not None else None


def parse_summary(js: dict) -> Summary:
    s = Summary()
    box = js.get("boxscore") or {}
    for t in box.get("lastFiveGames") or box.get("form") or []:
        tid = str((t.get("team") or {}).get("id", ""))
        rows = []
        for e in t.get("events", [])[:10]:
            res = {"W": "G", "D": "B", "L": "M"}.get(e.get("gameResult", ""), "?")
            opp = (e.get("opponent") or {}).get("displayName", "")
            where = "D" if e.get("atVs") == "@" else "İ"
            rows.append(f"{res} {e.get('score', '')} ({where}) {opp} – {e.get('competitionName', '')}".strip())
        s.last5[tid] = rows
        dates = []
        for e in t.get("events", []):
            try:
                dates.append(datetime.fromisoformat(str(e.get("gameDate", "")).replace("Z", "+00:00")))
            except ValueError:
                pass
        s.last_dates[tid] = dates
    for t in js.get("headToHeadGames") or []:
        for e in t.get("events", [])[:6]:
            s.h2h.append(f"{(e.get('gameDate') or '')[:10]} {e.get('score', '')} "
                         f"{'@' if e.get('atVs') == '@' else 'vs'} {(e.get('opponent') or {}).get('displayName', '')}")
        break
    for r in js.get("rosters") or []:
        tid = str((r.get("team") or {}).get("id", ""))
        starters = [p["athlete"]["displayName"] for p in r.get("roster", [])
                    if p.get("starter") and p.get("athlete")]
        if starters:
            s.lineups[tid] = starters
    for t in box.get("teams") or []:
        side = t.get("homeAway")
        if side in ("home", "away"):
            vals = {}
            for st in t.get("statistics") or []:
                try:
                    vals[st.get("name", "")] = float(st.get("displayValue", st.get("value")))
                except (TypeError, ValueError):
                    pass
            s.stats[side] = vals
    offs = ((box.get("gameInfo") or js.get("gameInfo") or {}).get("officials")) or []
    if offs:
        s.referee = offs[0].get("fullName", "") or offs[0].get("displayName", "")
    return s


async def summary(league: str, event_id: str, ttl: float = 1800) -> Summary:
    try:
        return parse_summary(await get_json(f"{SITE}/{league}/summary?event={event_id}", ttl=ttl))
    except Exception as e:  # noqa: BLE001
        log.warning("ESPN özet %s alınamadı: %s", event_id, e)
        return Summary()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
