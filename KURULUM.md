# iddaa Strateji Botu — Kurulum ve Kullanım

## Ne yapar?

Her gün saat 10:00'da Süper Lig, Premier Lig, La Liga, Serie A, Bundesliga, Ligue 1, Şampiyonlar Ligi ve Avrupa Ligi maçlarını tarar. iddaa bültenindeki her maçın modellenebilen tüm pazarlarını fiyatlar: MS, ÇŞ, alt/üst, İY, İY/MS, handikap, KG, MS+KG, MS+alt/üst, takım golleri, tek/çift, gol aralığı, skor, korner ve kart. Sadece **oranın gerçek olasılıktan yüksek olduğu** seçimleri önerir. Bunlardan kupon kurar, sonuçları takip eder ve karne çıkarır.

Bot bahis oynamaz, sadece analiz eder. iddaa oranları tüm lisanslı bayilerde aynıdır. Önerileri Nesine, Bilyoner, Misli veya istediğin sitede oynayabilirsin.

## Veri kaynakları (hepsi ücretsiz)

| Ne | Kaynak |
|---|---|
| iddaa oranları, MBS, tüm pazarlar | iddaa.com resmi bülteni |
| Fikstür, sonuç, son maçlar, H2H, ilk 11, hakem, uluslararası oranlar | ESPN açık verisi |
| İki sezonluk maçlar: gol, İY gol, şut, isabetli şut, korner, faul, kart | football-data.co.uk |
| Sakat/cezalı oyuncular, muhtemel 11, motivasyon | Gemini + Google Arama |
| Geçmiş iddaa oranları (sadece backtest için) | Maçkolik arşivi |

## Kurulum (yaklaşık 10 dakika)

**1. Telegram botu oluştur.** Telegram'da @BotFather'a `/newbot` yaz, bota isim ver ve verdiği token'ı kopyala. FinTrack botundan ayrı, yeni bir bot olsun.

**2. Gemini anahtarı.** FinTrack'te kullandığın anahtarı kullanabilirsin. Yoksa https://aistudio.google.com/apikey adresinden ücretsiz alabilirsin. Google aramalı istekler ayda 5.000'e kadar ücretsiz; bot günde 10-15 kullanır.

**3. Dosyaları sunucuya at.** Windows'ta zip'in olduğu klasörde PowerShell aç:

```
scp .\iddaa-bot.zip root@SUNUCU_IP:~/
ssh root@SUNUCU_IP
apt-get install -y unzip && unzip iddaa-bot.zip && cd iddaa-bot
bash deploy/install.sh
```

Kurulum betiği token'ı, Gemini anahtarını ve kasa tutarını soracak. Ardından testleri çalıştırıp botu sistem servisi olarak başlatacak. Sunucu yeniden başlasa da bot kendiliğinden açılır.

**4. Telegram'dan başlat.** Bota `/start` yaz. İlk yazan hesap sahibi olarak kaydedilir; bundan sonra başka kimse kullanamaz. Ardından `/teshis` yaz ve tüm kaynakların ✅ olduğunu kontrol et.

**5. Gerçek parayla oynamadan önce:** `/backtest hepsi` çalıştır (6 lig, toplam 10-15 dk). Sonuçları değerlendirmek için aşağıdaki "Backtest nasıl okunur" bölümüne bak.

## Komutlar

| Komut | Ne yapar |
|---|---|
| `/bugun` | Bugünün değerli seçimleri + kuponlar |
| `/yarin` | Yarının maçları |
| `/kupon` | Günün kuponları, her maç tek bir kupona girer: **2-3 Oran Kuponu** (2-4 maç, toplam oran 2-3, her seçim hem botun hesabında hem geçmişte %70+), **Banko** (2-3 maç, toplam oran 1.40-2.00, her seçim %74+), **Sürpriz Kupon** (2-3 maç, seçim oranları 2.00-4.50, toplam oran 6-40, tutarı yarım). Ligler: Süper Lig, 5 büyük lig, ŞL, UEL, Hollanda, Portekiz, İsviçre, Belçika, Avusturya |
| `/kupon superlig aksam` | Filtreli kupon. Lig: `superlig`, `premier`, `laliga`, `seriea`, `bundesliga`, `ligue1`, `hollanda`, `portekiz`, `isvicre`, `belcika`, `avusturya`, `sl` (Şampiyonlar Ligi), `uel` (Avrupa Ligi). Saat: `ogle` (17:00 öncesi), `aksam` (17-21), `gece` (21 sonrası). `yarin` eklenebilir |
| `/liste` | Günün tüm maçları, lige göre: her maçın en olası ✅ ve sürpriz 🎲 seçeneği, oranı ve olasılığı (`/liste yarin`, `/liste premier` da olur) |
| `/kuponlar` | Yapılan kuponlar tuttu mu: her maçın yanında ✅ tuttu, ❌ yattı, ⏳ oynanmadı, 🔴 oynanıyor; kuponun kendisi TUTTU/YATTI ve özet (kaç kupon tuttu, maç bazında isabet, önerilen tutarlarla kâr/zarar). Varsayılan dün + bugün; `/kuponlar bugun`, `dun`, `hafta`, `ay`. Komutsuz da olur: "kuponlar tuttu mu", "dünkü kupon ne oldu" yazman yeterli. Bir kupon sonuçlanınca bot ayrıca kendisi haber verir |
| `/deger ac` | Oran avantajlı tekli seçimleri ve ⚡ son dakika bildirimlerini açar (`/deger kapat`) |
| `/guncelle` | Son sürümü GitHub'dan kurar (testler geçmezse kurmaz) |
| `/analiz Galatasaray` | Tek maçın tam dökümü: lig sırası, iç saha/deplasman istatistikleri, son maçlar, H2H, sakat/cezalılar, ilk 11, model olasılıkları, tüm değerli seçenekler |
| `/karne` | Önerilerin gerçek sonuçları: ROI, isabet, CLV, güvene ve pazara göre kırılım |
| `/oynadim 12 50` | #12 nolu öneriyi 50 TL ile oynadığını kaydeder (kupon: `/oynadim K3 100`) |
| `/sonuc 12 kazandi` | Otomatik sonuçlanamayanı (ör. ilk golü kim atar) elle işaretler |
| `/kasa 2000` | Kasayı günceller; önerilen tutarlar buna göre hesaplanır |
| `/backtest hepsi 2024 2025` | Geçmiş sezon testi (6 lig, iki sezon); en isabetli ayarı kendisi uygular |
| `/agirlik 0.1 sut` | İstatistik modelinin payı ve türü (gol / sut / karma); `/agirlik 0` = sadece piyasa |
| `/teshis` | Kaynakların çalışıp çalışmadığını gösterir |
| `/ayarlar` | Strateji eşiklerini gösterir |

Otomatik gelenler: 10:00'da günün raporu, maçtan yaklaşık 1 saat önce ilk 11 ve oran kontrolü (oran düştüyse "artık değer yok" uyarısı), maç bitince sonuç bildirimi, pazartesi haftalık karne.

## Strateji nasıl çalışır?

**1. Gerçek olasılık.** Varsayılan mod **sadece piyasa**:
- **Piyasa:** ESPN'deki uluslararası oranların (DraftKings vb.) marjı temizlenir ve beklenen gole çevrilir. Bu oranlar iddaa'dan daha keskindir. Uluslararası oran yoksa o maçta değer aranmaz; iddaa'yı kendisiyle kıyaslamak anlamsızdır.
- **İstatistik modelleri** (gol modeli, isabetli şut modeli, karma): İki sezonun tüm lig maçlarından kurulur. 6 lig x 2 sezonluk backtest'te (4.130 maç) piyasaya eklemek isabeti düşürdü, bu yüzden varsayılan payları %0. Bundan sonraki `/backtest hepsi` çalıştırmalarında hangi model ve pay en isabetliyse (yeterince fark varsa) bot onu kendisi uygular; `/agirlik` ile elle de değiştirilebilir. Rakamlar raporlarda bilgi olarak görünmeye devam eder.
- **Son dakika taraması:** Maçlara 20 dk ile 2.5 saat kala oranlar 15 dakikada bir yeniden taranır. Gerçekçi avantaj, haberle uluslararası oranlar oynarken iddaa'nın geride kaldığı bu anlarda doğar; çıkarsa ⚡ ile bildirilir.
- Sonuç her devre için ayrı gol dağılımıdır; tüm pazarlar bu tek tutarlı modelden çıkar. **Öneriye sadece ana pazarlar girer** (tam maç MS, ÇŞ, Alt/Üst, KG, handikap). İY/MS, skor, korner ve kart analizde görünür ama önerilmez, çünkü geçmiş oranla doğrulanamadılar.

**2. Değer (EV).** `olasılık × oran − 1`. Örnek: %55 olasılığa 2.00 oran → +10% değer.

**3. Güven ve eşik.** Piyasa ile istatistik modeli ne kadar uyumluysa güven o kadar yüksek; ikisi çok ayrışıyorsa daha yüksek değer şartı aranır.

| Güven | Gereken en az değer |
|---|---|
| 🟢 yüksek | %3 |
| 🟡 orta | %5 |
| 🟠 düşük | %8 |

**Diğer filtreler:**
- Oran 1.40 ile 4.00 arasında olmalı; düşük ihtimalli sürpriz oranlar elenir.
- %25'ten büyük değer görünürse veri hatası sayılır ve önerilmez.
- Her maçtan en fazla 1 seçim önerilir.

**4. Tutar.** ¼ Kelly kullanılır: değer ve olasılığa göre kasanın bir yüzdesi, tek bahiste en fazla %3. Kayıptan sonra tutarı artırma (martingale) yoktur, çünkü uzun vadede kasayı bitirir.

**5. Kuponlar.**
- **2-3 Oran / Banko / Sürpriz:** Değer aranmaz; olasılığı yüksek seçenekler (Sürpriz'de olasılık × oran en yüksek olanlar). Bir maç bozulunca birden fazla kupon yanmasın diye her maç tek kupona girer. Çok maçlı günlerde kuponlar en güçlü maçlardan kurulur; geri kalanların hepsi `/liste`'de.
- **📊 Geçmiş isabet:** Kupondaki her seçimin altında, geçmişte benzer oranlı maçlarda o seçeneğin gerçekte kaç kez tuttuğu yazar (Süper Lig, Premier Lig, La Liga, Serie A, Bundesliga, Ligue 1, Hollanda, Portekiz, Belçika; 2012/13-2025/26, ~42 bin maç). Lig içinde yeterli maç yoksa 9 ligin toplamı kullanılır (İsviçre, Avusturya, ŞL, UEL). Bu test, piyasa oranından çıkan olasılığın gerçekleşenle 1-3 puan içinde tuttuğunu gösterdi; "son 10 maçın %70'i üst" tipi istatistik kuralları ise %60 civarında kaldı.
- **Değer Kuponu:** Kendi başına değerli 2-3 seçim. Kasa büyümesini en çok artıran kombinasyon seçilir.
- **Güven Kuponu:** Tutma olasılığı yüksek (%62+) ve tek tek eksi beklentili olmayan seçimler.

İkisinde de MBS kuralına uyulur. 4+ maçlı kupon önerilmez: iddaa marjı her maçta çarpılır ve kupon değeri hızla eksiye düşer.

**Değerli seçim çıkmayan gün "Pas" denir.** Bu bilerek böyle: iddaa marjı yüksek (MS'de genelde %8-12) ve çoğu oran olasılığın altında. Haftada birkaç seçim normaldir.

**6. Doğru ölçüm.** Karnedeki **kapanış değeri**, aldığın oranın maç başlarken piyasanın vardığı olasılığa göre hâlâ değerli olup olmadığını gösterir. Kısa vadede tutan-tutmayan şanstır; kapanış değeri sürekli artıdaysa strateji doğru çalışıyor demektir.

## Backtest nasıl okunur?

- **Log-loss: model < piyasa** ise istatistik modeli piyasaya bilgi katıyor demektir. En önemli göstergedir.
- **EV dilimleri:** "%10+" dilimi "%0-3" diliminden daha iyi gitmeli. Model "çok değer var" dediğinde gerçekten daha çok kazandırmıyorsa model kalibre değildir.
- **CLV:** Aldığın oran kapanış oranından iyiyse uzun vadede kazanma ihtimalin yüksektir. Bu ölçü, kısa dönem ROI'den çok daha güvenilirdir.
- **t ≥ 2:** Sonuç şansla açıklanamaz. Bir sezonda bu nadirdir; t < 2 ise henüz kanıt yok demektir.

Bu göstergeler kötü çıkarsa önce eşikleri yükselt (`.env` içindeki `MIN_EV_*`), sonra tekrar test et.

## Ayar değiştirme ve güncelleme

```
nano ~/iddaa-bot/.env               # eşikler, kasa, saat
sudo systemctl restart iddaa-bot
sudo journalctl -u iddaa-bot -f     # canlı log
```

**Güncelleme:** Telegram'da `/guncelle` yaz; bot son sürümü GitHub'dan indirir, testleri çalıştırır, geçerse kurar.
Depo gizliyse bir kez `/github sahip/depo TOKEN` ile token tanımlanır.

**Güncelleme (dosyayla, yedek yol):** Yeni `iddaa-bot-vX.zip` dosyasını Telegram'da bot sohbetine gönder (iPhone: dosyaya dokun → Paylaş → Telegram → bot). Bot testleri çalıştırır; geçerse eski sürümü yedekleyip yenisini kurar ve yeniden başlar. Testler geçmezse hiçbir şey değişmez. Sorun olursa `/geri` ile önceki sürüme dönülür, `/surum` sürümü gösterir.

**Bilgisayardan güncelleme** (yedek yol; ayarların ve karnen korunur), Windows'ta zip'in olduğu klasörde:

```
scp .\iddaa-bot.zip root@SUNUCU_IP:~/
ssh root@SUNUCU_IP
cd ~ && unzip -o iddaa-bot.zip && systemctl restart iddaa-bot
```

## Sorun giderme

- **`/teshis`'te iddaa ❌ 403:** iddaa.com yurt dışı sunucuları engelliyor olabilir (Hetzner Almanya/Finlandiya'da). Bu durumda bana yaz; Nesine bültenini yedek kaynak olarak ekleriz.
- **Maçkolik ❌:** Backtest sadece Bet365 oranlarıyla yapılır, canlı bot etkilenmez.
- **Gemini ❌:** Anahtarı ve modeli kontrol et (`GEMINI_MODEL=gemini-flash-latest`). Bot haber olmadan da çalışır.
- **Bir maç "iddaa bülteninde yok" diyorsa:** Takım adı eşleşmemiştir veya maç bültene henüz eklenmemiştir.
- **"Modellenmeyen en sık pazarlar" listesi (`/teshis`):** Buradaki pazarlar modele eklenebilir; listeyi bana gönder.

## Dürüst notlar

- Hiçbir model kazancı garanti etmez. Karnede 100-200 öneri birikmeden sonuca güvenme.
- Karnedeki "bot önerileri" ile "senin oynadıkların" ayrı tutulur. `/oynadim` ile işaretlersen gerçek sonucunu görürsün.
- football-data.co.uk verileri kişisel kullanım içindir. Bot dosyaları günde bir kez indirir; botu ticari amaçla kullanma.
- Sadece kaybetmeyi göze aldığın kasayla oyna.
