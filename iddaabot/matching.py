"""Farklı kaynaklardaki takım adlarını eşleştirme (iddaa Türkçe, ESPN İngilizce, football-data kısaltmalı)."""
from __future__ import annotations

import re
import unicodedata

import numpy as np
from rapidfuzz import fuzz
from scipy.optimize import linear_sum_assignment

_STOP = {
    "fc", "cf", "sk", "jk", "as", "ac", "afc", "sc", "ssc", "ss", "us", "bk", "fk", "if", "sv",
    "vfb", "vfl", "tsg", "cd", "ud", "rc", "rcd", "sd", "ca", "club", "calcio", "de", "the",
    "1899", "1846", "1848", "1904", "1907", "1909", "1910", "1913", "04", "05", "1", "a.s.",
    "kulubu", "futbol", "sporting club", "stade", "olympique", "ogc", "losc", "osc",
}

# normalize edilmiş ad -> ortak ad (Türkçe ad / kısaltma farkları)
ALIASES = {
    "bayern munih": "bayern munich", "munih": "munich", "marsilya": "marseille",
    "kizilyildiz": "crvena zvezda", "red star belgrade": "crvena zvezda",
    "kopenhag": "copenhagen", "kobenhavn": "copenhagen",
    "sahtar donetsk": "shakhtar donetsk", "sakhtar donetsk": "shakhtar donetsk",
    "sporting lizbon": "sporting cp", "sporting lisbon": "sporting cp",
    "slavia prag": "slavia prague", "sparta prag": "sparta prague", "slavia praha": "slavia prague",
    "sparta praha": "sparta prague", "dinamo kiev": "dynamo kyiv", "dinamo kyiv": "dynamo kyiv",
    "psg": "paris saint germain", "paris sg": "paris saint germain",
    "inter": "internazionale", "inter milan": "internazionale",
    "m gladbach": "monchengladbach", "mgladbach": "monchengladbach",
    "borussia m gladbach": "monchengladbach", "borussia monchengladbach": "monchengladbach",
    "koln": "cologne", "fc koln": "cologne", "ein frankfurt": "eintracht frankfurt",
    "frankfurt": "eintracht frankfurt", "leverkusen": "bayer leverkusen",
    "dortmund": "borussia dortmund", "leipzig": "rb leipzig", "rasenballsport leipzig": "rb leipzig",
    "man city": "manchester city", "man united": "manchester united", "man utd": "manchester united",
    "manchester utd": "manchester united", "nott m forest": "nottingham forest",
    "nottm forest": "nottingham forest", "wolves": "wolverhampton", "wolverhampton wanderers": "wolverhampton",
    "spurs": "tottenham", "tottenham hotspur": "tottenham", "newcastle united": "newcastle",
    "west ham united": "west ham", "brighton hove albion": "brighton", "brighton and hove albion": "brighton",
    "leeds united": "leeds", "sheffield utd": "sheffield united",
    "ath madrid": "atletico madrid", "atl madrid": "atletico madrid", "atletico de madrid": "atletico madrid",
    "ath bilbao": "athletic club", "athletic bilbao": "athletic club", "bilbao": "athletic club",
    "betis": "real betis", "sociedad": "real sociedad", "celta": "celta vigo", "espanol": "espanyol",
    "vallecano": "rayo vallecano", "alaves": "deportivo alaves",
    "verona": "hellas verona", "st etienne": "saint etienne",
    "buyuksehyr": "basaksehir", "istanbul basaksehir": "basaksehir", "rams basaksehir": "basaksehir",
    "basaksehir fk": "basaksehir", "karagumruk": "fatih karagumruk", "goztep": "goztepe",
    "rizespor": "caykur rizespor", "ad demirspor": "adana demirspor", "gaziantep": "gaziantep",
    "gaziantep fk": "gaziantep", "genclerbirligi": "genclerbirligi", "eyupspor": "eyupspor",
    "olimpiakos": "olympiacos", "olympiakos": "olympiacos", "olympiakos pireus": "olympiacos",
    "glasgow rangers": "rangers", "brugge": "club brugge", "salzburg": "red bull salzburg",
    "rb salzburg": "red bull salzburg", "bodo glimt": "bodo glimt", "midtjylland": "midtjylland",
    "zenit": "zenit st petersburg", "plzen": "viktoria plzen",
}

# Tek kelimelik Türkçe şehir/ülke adları (ad içinde geçerse çevrilir)
WORD_ALIASES = {
    "munih": "munich", "marsilya": "marseille", "kopenhag": "copenhagen", "lizbon": "lisbon",
    "prag": "prague", "kiev": "kyiv", "bruksel": "brussels", "koln": "cologne", "atina": "athens",
    "sahtar": "shakhtar", "belgrad": "belgrade", "varsova": "warsaw", "viyana": "vienna",
}


def norm(name: str) -> str:
    s = name.replace("ı", "i").replace("İ", "I")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("&", " and ").replace("'", " ").replace(".", " ").replace("-", " ").replace("/", " ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if s in ALIASES:
        return ALIASES[s]
    toks = [t for t in s.split() if t not in _STOP]
    s2 = " ".join(toks) or s
    if s2 in ALIASES:
        return ALIASES[s2]
    s3 = " ".join(WORD_ALIASES.get(t, t) for t in s2.split())
    return ALIASES.get(s3, s3)


def similarity(a: str, b: str) -> float:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 100.0
    s = max(fuzz.WRatio(na, nb), fuzz.token_set_ratio(na, nb))
    # bir ad diğerinin tamamını içeriyorsa (ör. "galatasaray" / "galatasaray as")
    short, long_ = sorted((na, nb), key=len)
    if len(short) >= 5 and (short in long_.split() or long_.startswith(short + " ")):
        s = max(s, 88.0)
    return float(s)


def assign(names_a: list[str], names_b: list[str], min_score: float = 70.0) -> dict[str, str]:
    """İki ad listesini (aynı lig) global olarak en iyi şekilde eşleştirir."""
    if not names_a or not names_b:
        return {}
    sim = np.array([[similarity(a, b) for b in names_b] for a in names_a])
    r, c = linear_sum_assignment(-sim)
    return {names_a[i]: names_b[j] for i, j in zip(r, c) if sim[i, j] >= min_score}


def pair_score(h1: str, a1: str, h2: str, a2: str) -> float:
    """İki maçın aynı maç olma skoru (ev+deplasman benzerliği)."""
    sh, sa = similarity(h1, h2), similarity(a1, a2)
    if min(sh, sa) < 45:
        return 0.0
    return sh + sa
