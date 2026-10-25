"""Ayarlar: .env dosyasından okunur."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:  # dotenv opsiyonel
    pass

TZ = ZoneInfo("Europe/Istanbul")
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ESPN lig kodu -> (görünen ad, football-data.co.uk kodu veya None)
LEAGUES: dict[str, tuple[str, str | None]] = {
    "tur.1": ("Süper Lig", "T1"),
    "eng.1": ("Premier Lig", "E0"),
    "esp.1": ("La Liga", "SP1"),
    "ita.1": ("Serie A", "I1"),
    "ger.1": ("Bundesliga", "D1"),
    "fra.1": ("Ligue 1", "F1"),
    "uefa.champions": ("Şampiyonlar Ligi", None),
    "uefa.europa": ("Avrupa Ligi", None),
    # gol profili farklı ligler: Hollanda/İsviçre/Avusturya/Belçika gollü, Portekiz golsüz
    "ned.1": ("Hollanda Eredivisie", "N1"),
    "por.1": ("Portekiz Ligi", "P1"),
    "sui.1": ("İsviçre Süper Lig", None),
    "bel.1": ("Belçika Pro Lig", "B1"),
    "aut.1": ("Avusturya Bundesliga", None),
}

# Kuponların kurulacağı lig ve kupalar (kullanıcının seçtikleri)
COUPON_LEAGUES = tuple(LEAGUES)

# Ek ligler: "sadece piyasa" modunda değer için istatistik gerekmez, uluslararası oran yeterli.
# Ne kadar çok maç taranırsa iddaa'nın geride kaldığı anı yakalama şansı o kadar artar.
EXTRA_LEAGUES: dict[str, tuple[str, str | None]] = {
    "uefa.europa.conf": ("Konferans Ligi", None),
    "eng.2": ("İngiltere Championship", "E1"), "eng.3": ("İngiltere League One", "E2"),
    "ger.2": ("Almanya 2. Bundesliga", "D2"), "esp.2": ("İspanya La Liga 2", "SP2"),
    "ita.2": ("İtalya Serie B", "I2"), "fra.2": ("Fransa Ligue 2", "F2"),
    "sco.1": ("İskoçya Premiership", "SC0"),
    "gre.1": ("Yunanistan Süper Lig", "G1"), "den.1": ("Danimarka Süper Lig", None),
    "nor.1": ("Norveç Eliteserien", None), "swe.1": ("İsveç Allsvenskan", None),
    "usa.1": ("ABD MLS", None), "mex.1": ("Meksika Liga MX", None),
    "bra.1": ("Brezilya Serie A", None), "arg.1": ("Arjantin Primera", None),
    "ksa.1": ("Suudi Pro Lig", None), "jpn.1": ("Japonya J1", None),
}
if os.getenv("EXTRA_LEAGUES", "1") == "1":
    LEAGUES.update(EXTRA_LEAGUES)


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    telegram_token: str = os.getenv("TELEGRAM_TOKEN", "")
    allowed_chat_id: int = int(os.getenv("ALLOWED_CHAT_ID", "0") or 0)
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-flash-latest")

    # Strateji
    bankroll: float = _f("BANKROLL", 1000)          # TL, /kasa ile değişir
    kelly_fraction: float = _f("KELLY_FRACTION", 0.25)
    max_stake_pct: float = _f("MAX_STAKE_PCT", 0.03)
    min_stake: float = _f("MIN_STAKE", 10)
    min_odds: float = _f("MIN_ODDS", 1.40)
    max_odds: float = _f("MAX_ODDS", 4.00)
    min_prob: float = _f("MIN_PROB", 0.30)
    max_ev: float = _f("MAX_EV", 0.25)  # bundan büyük fark büyük ihtimalle veri/eşleştirme hatasıdır
    # Güvene göre minimum beklenen değer (EV)
    min_ev: dict = field(default_factory=lambda: {
        "yüksek": _f("MIN_EV_HIGH", 0.03),
        "orta": _f("MIN_EV_MID", 0.05),
        "düşük": _f("MIN_EV_LOW", 0.08),
    })
    # Model harmanı (log beklenen gol üzerinde)
    w_market: float = _f("W_MARKET", 1.0)
    w_dc: float = _f("W_DC", 0.0)
    # İstatistik modeli: "gol" (atılan/yenen gol), "sut" (isabetli şut, xG yerine) veya "karma"
    stat_model: str = os.getenv("STAT_MODEL", "gol")
    # Korner/kart modelleri geçmiş oranla doğrulanamadığı için varsayılan olarak öneriye girmez
    allow_count_markets: bool = os.getenv("ALLOW_COUNT_MARKETS", "0") == "1"
    # Sadece ana pazarlar (tam maç MS, ÇŞ, Alt/Üst, KG, handikap) öneriye girer; İY/MS, skor vb.
    # modelin dağılım varsayımlarına dayanır ve geçmiş oranla doğrulanamadı (analizde yine görünür)
    core_only: bool = os.getenv("CORE_ONLY", "1") == "1"
    # "Günün Kuponu (en iyi fiyat)": değer olmasa da her gün kupon; sabit küçük tutar (kasanın %'si)
    daily_coupon: bool = os.getenv("DAILY_COUPON", "0") == "1"
    # İstatistik kuponları: belirlenen lig ve kupalarda, olasılığı en yüksek seçeneklerle (değer aranmaz)
    stat_coupons: bool = os.getenv("STAT_COUPONS", "1") == "1"
    # Oran avantajlı (değerli) tekli seçimler ve ⚡ bildirimleri — /deger ac|kapat
    value_picks: bool = os.getenv("VALUE_PICKS", "0") == "1"
    fun_stake_pct: float = _f("FUN_STAKE_PCT", 0.01)
    # Kupon modu: "70" = sadece takım istatistiği %70+ olan seçeneklerden kupon (varsayılan);
    # "eski" = 2-3 Oran / Banko / Sürpriz kuponları
    coupon_mode: str = os.getenv("KUPON_MODU", "70")

    # /guncelle: kodu GitHub'dan çeker (depo herkese açıksa token gerekmez)
    github_repo: str = os.getenv("GITHUB_REPO", "ssezenbay-cpu/iddaa-bot")
    github_token: str = os.getenv("GITHUB_TOKEN", "")

    # Zamanlama (TR saati)
    daily_report_hour: int = int(os.getenv("DAILY_REPORT_HOUR", "10"))
    days_ahead: int = int(os.getenv("DAYS_AHEAD", "0"))  # günlük rapor: bugün + kaç gün


settings = Settings()
