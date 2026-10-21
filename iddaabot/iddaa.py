"""iddaa.com resmi bülteni: maçlar, MBS ve tüm pazar oranları.

Nesine, Bilyoner, Misli vb. tüm lisanslı bayiler aynı resmi iddaa oranlarını kullanır.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .http import get_json

log = logging.getLogger(__name__)
BASE = "https://sportsbookv2.iddaa.com/sportsbook"
HDR = {"Origin": "https://www.iddaa.com", "Referer": "https://www.iddaa.com/"}


# ----------------------------------------------------------------- veri yapıları
@dataclass
class Outcome:
    name: str
    odd: float
    spec: dict | None = None   # modellenebiliyorsa seçim tanımı


@dataclass
class Market:
    name: str
    sov: str | None
    outcomes: list[Outcome]
    mtype: dict | None = None  # classify_market çıktısı
    mbs: int = 3               # bu pazarın minimum bahis sayısı

    @property
    def label(self) -> str:
        return self.name


@dataclass
class IddaaEvent:
    id: int
    home: str
    away: str
    kickoff: datetime
    competition: str
    mbs: int
    markets: list[Market] = field(default_factory=list)


# ----------------------------------------------------------------- ad normalize
def _n(s: str) -> str:
    s = str(s).replace("ı", "i").replace("İ", "i")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace(",", ".")
    s = re.sub(r"[^a-z0-9/+.:\- ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _line(sov, name: str) -> float | None:
    for src in (sov, name):
        if src is None:
            continue
        for g in re.findall(r"\d+(?:[.,]\d+)?", str(src)):
            v = float(g.replace(",", "."))
            if abs(v * 2 - round(v * 2)) < 1e-9 and round(v * 2) % 2 == 1:  # yalnız x.5 çizgiler
                return v
    return None


SKIP = ("oyuncu", "golcu", "penalti", "ofsayt", " tac", "dakika", "dk ", "faul", "isabetli",
        "kurtaris", "uzatma", "tur atlar", "turu gecer", "sampiyon", "kazanma sekli", "son gol",
        "en cok gol atan", "kirmizi kart gor", "ilk korner", "son korner", "kimin", "asist",
        "beraberlikte iade", "aralik korner", "korner araligi", "kart araligi", "ev sahibi gol atar",
        "deplasman gol atar", "gol atar mi", "ilk 10", "ilk 15", "ilk 30", "10 dakika", "15 dakika",
        "ust/alt ve", "kart puan", "puani")

RES = {"1": "1", "0": "X", "x": "X", "2": "2", "ev sahibi": "1", "beraberlik": "X", "deplasman": "2"}
DC = {"1x": ("1", "X"), "10": ("1", "X"), "x1": ("1", "X"), "01": ("1", "X"),
      "12": ("1", "2"), "21": ("1", "2"),
      "x2": ("X", "2"), "02": ("X", "2"), "2x": ("X", "2"), "20": ("X", "2")}
YES = {"var", "evet", "olur"}
NO = {"yok", "hayir", "olmaz"}


def classify_market(name: str, sov=None) -> dict | None:
    n = _n(name)
    if any(w in f" {n} " for w in SKIP):
        return None
    if ("ilk yari" in n or n.startswith("iy")) and "mac sonucu" in n and ("/" in n or "iy/ms" in n):
        return {"type": "htft"}
    per = "H1" if ("ilk yari" in n or "1. yari" in n or n.startswith("iy ")) else (
        "H2" if ("ikinci yari" in n or "2. yari" in n) else "FT")
    subj = "corners" if "korner" in n else ("cards" if "kart" in n else "goals")
    scope = "home" if "ev sahibi" in n else ("away" if "deplasman" in n else "total")
    line = _line(sov, name)

    if " ve " in n:
        if subj != "goals" or scope != "total":
            return None
        pieces = n.split(" ve ")
        tags = {("H1" if ("ilk yari" in p or "1. yari" in p) else
                 ("H2" if ("ikinci yari" in p or "2. yari" in p) else "FT")) for p in pieces}
        if len(tags) != 1:
            return None  # farklı devreleri karıştıran kombine (desteklenmiyor)
        return {"type": "combo", "period": tags.pop(), "line": line, "name": n}
    if subj != "goals":
        if "alt" in n and "ust" in n and line is not None:
            return {"type": "ou", "subject": subj, "period": per, "scope": scope, "line": line}
        if "sonucu" in n and scope == "total":
            return {"type": "1x2", "subject": subj, "period": per}
        return None
    if "handikap" in n:
        hh, ha = _handicap(sov, name)
        if hh is None:
            return None
        return {"type": "handicap", "period": per, "hh": hh, "ha": ha}
    if "cifte sans" in n:
        return {"type": "dc", "period": per}
    if "karsilikli gol" in n:
        return {"type": "btts", "period": per}
    if "tek" in n and "cift" in n:
        return {"type": "oddeven", "period": per}
    if "hangi yari" in n:
        return {"type": "half_more"}
    if "her iki yariyi" in n:
        return {"type": "win_both", "scope": scope}
    if "gol yemeden" in n:
        return {"type": "win_to_nil", "scope": scope}
    if "ilk gol" in n:
        return {"type": "first_goal"} if per == "FT" else None
    if "skor" in n and "toplam" not in n and "alt" not in n:
        return {"type": "score"} if per == "FT" else None
    if "toplam gol" in n and not ("alt" in n and "ust" in n):
        return {"type": "range", "period": per}
    if "alt" in n and "ust" in n:
        if line is None:
            return None
        return {"type": "ou", "subject": "goals", "period": per, "scope": scope, "line": line}
    if "sonucu" in n and scope == "total":
        return {"type": "1x2", "subject": "goals", "period": per}
    return None


def _handicap(sov, name) -> tuple[int | None, int | None]:
    for src in (sov, name):
        if src is None:
            continue
        s = str(src)
        m = re.search(r"(\d+)\s*:\s*(\d+)", s)
        if m:  # iddaa gösterimi "0:1" = deplasman 1 gol avantajlı (yönü kesin olan tek biçim)
            return int(m.group(1)), int(m.group(2))
    return None, None


def classify_outcome(mtype: dict, oname: str) -> dict | None:
    t = mtype["type"]
    o = _n(oname)
    o = re.sub(r"\(.*?\)", "", o).strip()
    per = mtype.get("period", "FT")
    subj = mtype.get("subject", "goals")
    if t == "1x2":
        sel = RES.get(o)
        return {"kind": "1x2", "subject": subj, "period": per, "sel": sel} if sel else None
    if t == "ou":
        side = "over" if "ust" in o else ("under" if "alt" in o else None)
        if side is None:
            return None
        return {"kind": "ou", "subject": subj, "period": per, "scope": mtype["scope"],
                "line": mtype["line"], "side": side}
    if t == "dc":
        key = re.sub(r"[\s\-/]", "", o)
        return {"kind": "dc", "period": per, "sel": DC[key]} if key in DC else None
    if t == "btts":
        if o in YES:
            return {"kind": "btts", "period": per, "yes": True}
        if o in NO:
            return {"kind": "btts", "period": per, "yes": False}
        return None
    if t == "oddeven":
        if o.startswith("tek"):
            return {"kind": "oddeven", "period": per, "odd": True}
        if o.startswith("cift"):
            return {"kind": "oddeven", "period": per, "odd": False}
        return None
    if t == "half_more":
        if o.startswith("1") or "ilk" in o:
            return {"kind": "half_more", "sel": "1"}
        if o.startswith("2") or "ikinci" in o:
            return {"kind": "half_more", "sel": "2"}
        if "esit" in o or o in ("x", "0"):
            return {"kind": "half_more", "sel": "X"}
        return None
    if t in ("win_both", "win_to_nil"):
        side = mtype["scope"]
        if side == "total":
            if o in ("1", "ev sahibi"):
                return {"kind": t, "side": "home", "yes": True}
            if o in ("2", "deplasman"):
                return {"kind": t, "side": "away", "yes": True}
            return None
        if o in YES:
            return {"kind": t, "side": side, "yes": True}
        if o in NO:
            return {"kind": t, "side": side, "yes": False}
        return None
    if t == "first_goal":
        if o in ("1", "ev sahibi"):
            return {"kind": "first_goal", "sel": "home"}
        if o in ("2", "deplasman"):
            return {"kind": "first_goal", "sel": "away"}
        if o in NO or "olmaz" in o or o in ("gol yok", "golsuz"):
            return {"kind": "first_goal", "sel": "none"}
        return None
    if t == "score":
        m = re.fullmatch(r"(\d+)\s*[-:]\s*(\d+)", o)
        return {"kind": "score", "x": int(m.group(1)), "y": int(m.group(2))} if m else None
    if t == "range":
        m = re.search(r"(\d+)\s*-\s*(\d+)", o)
        if m:
            return {"kind": "range", "period": per, "lo": int(m.group(1)), "hi": int(m.group(2))}
        m = re.search(r"(\d+)\s*\+", o) or re.search(r"(\d+)\s*(?:ve|veya)\s*(?:uzeri|ustu)", o)
        if m:
            return {"kind": "range", "period": per, "lo": int(m.group(1)), "hi": 99}
        return None
    if t == "handicap":
        sel = RES.get(o)
        return {"kind": "handicap", "period": per, "sel": sel, "hh": mtype["hh"],
                "ha": mtype["ha"]} if sel else None
    if t == "htft":
        parts = re.split(r"\s*/\s*", o)
        if len(parts) == 2 and parts[0] in RES and parts[1] in RES:
            return {"kind": "htft", "ht": RES[parts[0]], "ft": RES[parts[1]]}
        return None
    if t == "combo":
        return _combo(mtype, o)
    return None


def _combo(mtype: dict, o: str) -> dict | None:
    toks = [x for x in re.split(r"\s+ve\s+|\s*&\s*|\s*\+\s*|\s*,\s*|\s+", o) if x]
    per, line = mtype["period"], mtype["line"]
    parts = []
    for tk in toks:
        if tk in YES or tk in NO:
            parts.append({"kind": "btts", "period": per, "yes": tk in YES})
        elif tk in ("alt", "ust"):
            if line is None:
                return None
            parts.append({"kind": "ou", "period": per, "line": line,
                          "side": "over" if tk == "ust" else "under"})
        elif tk in RES and len(tk) == 1:
            parts.append({"kind": "1x2", "period": per, "sel": RES[tk]})
        elif re.sub(r"[\-/]", "", tk) in DC:
            parts.append({"kind": "dc", "period": per, "sel": DC[re.sub(r"[\-/]", "", tk)]})
        elif re.fullmatch(r"\d+(\.\d+)?", tk):
            continue  # çizgi değeri (2.5) tekrar yazılmış olabilir
        else:
            return None
    if len(parts) != 2:
        return None
    return {"kind": "combo", "parts": parts}


# ----------------------------------------------------------------- çekme
async def fetch_events() -> list[IddaaEvent]:
    ev = await get_json(f"{BASE}/events?st=1&type=0&version=0", ttl=300, headers=HDR)
    try:
        cfg = (await get_json(f"{BASE}/get_market_config", ttl=12 * 3600, headers=HDR))["data"]["m"]
    except Exception as e:  # noqa: BLE001
        log.warning("Pazar ayarları alınamadı: %s", e)
        cfg = {}
    try:
        comps = {str(c["i"]): c["n"] for c in (await get_json(f"{BASE}/competitions", ttl=12 * 3600, headers=HDR))["data"]}
    except Exception as e:  # noqa: BLE001
        log.warning("Lig listesi alınamadı: %s", e)
        comps = {}
    return parse_events(ev, cfg, comps)


def _fill(raw: str, m: dict, sov) -> str:
    """Pazar adındaki {0}, {h} gibi yer tutucuları pazarın kendi alanıyla (yoksa sov ile) doldurur."""
    def rep(mt):
        k = mt.group(1)
        v = m.get(k)
        if v is None:
            v = sov
        if v is None:
            return mt.group(0)
        if k == "h":  # handikap: sov ev sahibine eklenen gol (1 -> "(1:0)", -1 -> "(0:1)")
            try:
                h = float(v)
                if h == int(h):
                    h = int(h)
                    return f"({max(h, 0)}:{max(-h, 0)})"
            except (TypeError, ValueError):
                pass
        return str(v)
    return re.sub(r"\{(\w+)\}", rep, raw)


def parse_events(ev_json: dict, cfg: dict, comps: dict) -> list[IddaaEvent]:
    out = []
    for e in (ev_json.get("data") or {}).get("events", []):
        try:
            ko = datetime.fromtimestamp(int(e["d"]), tz=timezone.utc)
        except (KeyError, ValueError, TypeError):
            continue
        markets = []
        for m in e.get("m") or []:
            key = f"{m.get('t')}_{m.get('st')}"
            raw = (cfg.get(key) or {}).get("n") or ""
            sov = m.get("sov")
            name = _fill(raw, m, sov)
            outs = [Outcome(str(o.get("n", "")), float(o.get("odd") or 0)) for o in m.get("o") or []]
            outs = [o for o in outs if o.odd > 1.01]
            if not name or not outs:
                continue
            ev_mbs = int(e.get("mbc") or 0) or 3
            mk = Market(name=name, sov=None if sov is None else str(sov), outcomes=outs,
                        mbs=int(m.get("mbc") or 0) or ev_mbs)
            mk.mtype = classify_market(name, sov)
            if mk.mtype:
                for o in outs:
                    o.spec = classify_outcome(mk.mtype, o.name)
            markets.append(mk)
        out.append(IddaaEvent(
            id=int(e.get("i", 0)), home=str(e.get("hn", "")).strip(), away=str(e.get("an", "")).strip(),
            kickoff=ko, competition=comps.get(str(e.get("ci")), ""), mbs=int(e.get("mbc") or 0) or 3,
            markets=markets))
    return out
