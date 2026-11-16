# tapo-muzik

Bilgisayarda çalan sese göre TP-Link Tapo renkli ampulü (L530 / L535) süren Python betiği.

- Hoparlör/kulaklık çıkışını doğrudan dinler (WASAPI loopback); mikrofon gerekmez.
- Şarkının temposunu bulur ve vuruşları önceden tahmin eder. Komut, ampulün yanıt
  süresi kadar erken gönderilir ki ışık vuruşla aynı anda atsın.
- Parlaklık her vuruşta atar; renk 4 vuruşta (ölçüde) bir değişir.
- Şarkı girerken/biterken ve kısık-yüksek bölümlerde fade in / fade out yapar.
- Geçişler şarkının sertliğine göre ayarlanır: sakin şarkıda renkler yavaşça
  birbirine kayar, davullu/sert şarkıda anında atlar.
- Çıkışta (Ctrl+C) ampul eski ayarına döner.

## Gereksinimler

- Windows, Python 3.11+
- PC ile aynı ağda bir Tapo renkli ampul (L535E için yazıldı)
- TP-Link (Tapo) hesabının e-postası ve şifresi

## Kurulum

```
pip install -r requirements.txt
```

## Kullanım

```
python tapo_muzik.py --ip 192.168.1.50 --email hesabin@mail.com
```

Şifre çalışırken sorulur (ya da `TAPO_SIFRE` ortam değişkeninden okunur).
Ampulün IP adresi: Tapo uygulaması > ampul > ayarlar > Cihaz Bilgisi.

Seçenekler: `--parlaklik` (en yüksek parlaklık %), `--min-aralik` ve `--hassasiyet`
(ritimsiz bölümlerde vuruş algılama).

## İnce ayar

Dosyanın başındaki sabitler:

| Sabit | Ne işe yarar |
|---|---|
| `ONCULUK` | Işık vuruştan geç kalıyorsa artır, erken davranıyorsa azalt |
| `RENK_VURUSU` | Kaç vuruşta bir renk değişsin |
| `SERTLIK_ARALIK` | Şarkının "yumuşak" / "sert" sayıldığı eşikler |
| `TABAN_PARLAKLIK` | Sessizlikte inilen parlaklık % |
| `GONDERIM_ARALIK` | Ampule iki komut arası en az süre |

Çalışırken ekranda sertlik, tempo, parlaklık, renk ve ampulün yanıt süresi görünür.

## Not

Yeni Tapo yazılımları TPAP protokolünü kullanıyor ve python-kasa bunu desteklemiyor;
bu yüzden ampulle [tapo](https://github.com/mihai-dinculescu/tapo) kütüphanesi üzerinden konuşulur.
