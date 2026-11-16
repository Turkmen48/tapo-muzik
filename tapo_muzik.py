#!/usr/bin/env python3
"""
tapo_muzik.py - Bilgisayarda calan sese gore Tapo ampulun rengini degistirir.

Bilgisayarin hoparlor/kulaklik cikisini (WASAPI loopback) dinler ve ampulu yerel
agdan surer:
  - sarkinin temposunu bulur ve vuruslari onceden tahmin eder; komut, ampulun
    yanit suresi kadar erken gonderilir ki isik vurusla ayni anda atsin
  - parlaklik her vurusta atar; renk her vurusta degil, 4 vurusta (olcude) bir degisir
  - sarki girerken/biterken ve kisik-yuksek bolumlerde parlaklik fade in / fade out yapar
  - gecisler sarkinin sertligine gore ayarlanir: sakin sarkida renkler yavasca
    birbirine kayar, davullu/sert sarkida aninda atlar
  - duzenli ritmi olmayan bolumlerde algilanan vuruslara tepki verir (biraz gecikmeli)

Kurulum (Windows, Python 3.11+):
    pip install tapo PyAudioWPatch numpy

Kullanim:
    python tapo_muzik.py --ip 192.168.1.50 --email hesabin@mail.com

Sifre sorulur (ya da TAPO_SIFRE ortam degiskeninden okunur). Cikis: Ctrl+C
"""

import argparse
import asyncio
import getpass
import math
import os
import random
import sys
import time
from collections import deque

import numpy as np

HOP_SANIYE = 0.02       # ses 20 ms'lik parcalar halinde islenir
SESSIZLIK_RMS = 0.003   # bunun altindaki ses "sessizlik" sayilir
AKI_HZ = (30, 10000)    # vurus ve sertlik analizinin baktigi frekans araligi
TEMPO_ARALIK = (0.4, 1.0)  # aranan vurus periyodu, sn (150-60 bpm; daha hizli sarkilar yari tempoda izlenir)
GUVEN_ESIGI = 0.3       # ritim en az bu kadar duzenliyse vuruslar onceden tahmin edilir
ONCULUK = 0.04          # isik vurustan gec kaliyorsa artir, erken davraniyorsa azalt (sn)
RENK_VURUSU = 4         # duzenli ritimde kac vurusta bir renk degissin (yumusak sarkida 2 kati)
GAMA = 1.5              # parlaklik egrisi: kisik seviyelerde adimlar goze esit gorunsun diye
TABAN_PARLAKLIK = 8     # sessizlikte ve sarkinin en kisik aninda inilen parlaklik %
DINAMIK_DB = 20.0       # sarkinin en yuksek aninin bu kadar dB alti "en kisik" sayilir
TEPE_TABAN_DB = -30.0   # "en yuksek an" bundan asagi kabul edilmez (kisik girisler los kalsin)
VURUS_TABANI = 0.10     # bir vurus, o anki toplam gurlugun en az bu kadari kadar yeni ses getirmeli
SERTLIK_ARALIK = (0.02, 0.07)  # aki/gurluk orani: ilki ve alti tam yumusak, ikincisi ve ustu tam sert
GONDERIM_ARALIK = 0.08  # ampule iki komut arasinda en az bu kadar sn beklenir


def ara(a, b, s):
    """s=0 icin a, s=1 icin b; arasi dogrusal."""
    return a + (b - a) * s


def yaklas(dt, tau):
    """Ustel yumusatma: tau zaman sabitiyle (sn), dt surede hedefe yaklasma orani."""
    return 1.0 - math.exp(-dt / tau) if tau > 1e-3 else 1.0


class Tempo:
    """Vurus takibi: yenilik egrisinin son ~8 saniyesinden tempoyu (otokorelasyon)
    ve vuruslarin yerini (egriye tarak oturtma) bulur, bir sonraki vurusun ne zaman
    gelecegini onceden soyler. Boylece ampule komut, vurus duyulmadan yola cikabilir."""

    def __init__(self):
        self.egri = deque(maxlen=int(8.0 / HOP_SANIYE))
        self.periyot = 0.0  # iki vurus arasi sure, sn
        self.sonraki = 0.0  # sonraki vurusun ani (Analiz.sure saatiyle)
        self.guven = 0.0    # 0..1: ritim ne kadar duzenli
        self.kilit = False  # ritim ~1 sn'dir duzenli: tahminlere guvenilebilir
        self.ust_uste = 0
        self.adim = 0

    def sifirla(self):
        self.egri.clear()
        self.periyot = self.guven = 0.0
        self.kilit, self.ust_uste = False, 0

    def ekle(self, yeni, sure):
        self.egri.append(yeni)
        self.adim += 1
        if self.periyot and sure >= self.sonraki:
            self.sonraki += self.periyot
        n = len(self.egri)
        if self.adim % 10 or n < self.egri.maxlen // 2:  # 0.2 sn'de bir, en az 4 sn veriyle
            return
        # hafif yayilmis egri: vurus araligi 20 ms'lik adimlarin tam kati olmasa da tepeler ortussun
        x = np.convolve(self.egri, (0.25, 0.5, 0.25), "same")

        # tempo: egri kac adim kaydirilinca kendine en cok benziyor?
        f = np.fft.rfft(x - x.mean(), 2 * n)
        ak = np.fft.irfft(f * np.conj(f))[:n]
        if ak[0] <= 0.0:
            self.guven, self.kilit, self.ust_uste = 0.0, False, 0
            return
        ak = ak / ak[0] * n / (n - np.arange(n))
        kayma = np.arange(round(TEMPO_ARALIK[0] / HOP_SANIYE), round(TEMPO_ARALIK[1] / HOP_SANIYE) + 1)
        skor = ak[kayma] + 0.5 * ak[2 * kayma]  # gercek vurus periyodu 2 katinda da tekrar eder
        # iki aday yakinsa 120 bpm'e yakin olani sec (yoksa vurus yerine iki vurusluk kalip secilebilir)
        skor = skor * np.exp(-0.5 * (np.log2(kayma * HOP_SANIYE / 0.5) / 0.9) ** 2)
        i = int(np.argmax(skor))
        if self.periyot:  # onceki tempo hala iyi aciklamaya yakinsa onda kal (tempo ziplamasin)
            j = int(np.clip(round(self.periyot / HOP_SANIYE) - kayma[0], 1, len(kayma) - 2))
            j += int(np.argmax(skor[j - 1:j + 2])) - 1
            if skor[j] >= 0.8 * skor[i]:
                i = j
        p = float(kayma[i]) + _tepe_kaymasi(skor, i)
        self.guven = float(np.clip(ak[kayma[i]], 0.0, 1.0))
        # kilit: guven ust uste 5 olcumde (~1 sn) esigi asinca acilir, belirgin dusunce kapanir
        self.ust_uste = self.ust_uste + 1 if self.guven >= GUVEN_ESIGI else 0
        self.kilit = self.ust_uste >= 5 or (self.kilit and self.guven >= 0.6 * GUVEN_ESIGI)

        # faz: vuruslar tamponun sonundan kac adim geride? (son vuruslar daha agir basar)
        k = np.arange(int((n - 1) / p))
        geri = np.rint(k * p).astype(int)
        konum = n - 1 - np.arange(int(p))[:, None] - geri[None, :]
        fskor = (x[np.maximum(konum, 0)] * (konum >= 0) * np.exp(-k * p * HOP_SANIYE / 4.0)).sum(1)
        faz = int(np.argmax(fskor))
        if self.periyot:  # onceki tahminle uyumlu faz hala iyiyse onda kal (vurus <-> ara vurus ziplamasin)
            eski = round((sure - self.sonraki) / HOP_SANIYE) % len(fskor)
            aday = max(((eski + d) % len(fskor) for d in (-1, 0, 1)), key=lambda a: fskor[a])
            if fskor[aday] >= 0.8 * fskor[faz]:
                faz = aday
        faz_kesirli = faz + _tepe_kaymasi(fskor, faz)

        self.periyot = p * HOP_SANIYE
        self.sonraki = sure - faz_kesirli * HOP_SANIYE + self.periyot


def _tepe_kaymasi(y, i):
    """y[i] yerel tepe ise gercek tepenin i'ye gore kesirli kaymasi (parabol uydurma, -0.5..0.5)."""
    a, b, c = y[i - 1], y[i], y[(i + 1) % len(y)]
    payda = a - 2 * b + c
    return float(np.clip(0.5 * (a - c) / payda, -0.5, 0.5)) if payda < 0 else 0.0


class Analiz:
    """Sesi inceler; sarkinin o anki `sertlik` degerini (0 = yumusak, 1 = sert) tutar.

    Yontem "spektral aki": spektrum ~20 logaritmik banda bolunur ve her bandin
    gurlugu (genligi) hesaplanir. Son 3 adimin en yuksegine gore *artan* gurlugun
    toplami "aki"dir; yeni bir ses (davul, nota) girdigi anda ziplar. Surekli
    seslerin kendi dalgalanmasi (son ~1.5 sn'nin medyani) akidan dusulur.
      vurus   : aki, son ~1.5 sn'nin ortalamasini ve son vurusun sonen izini asarsa
      sertlik : son birkac saniyede aki / toplam gurluk. Kisa, patlamali seslerde
                (davul) yuksek; uzun, kesintisiz seslerde (pad, yayli, vokal) dusuk
      seviye  : ses yuksekligi, sarkinin en yuksek anina gore 0..1
    """

    def __init__(self, rate, hop, hassasiyet=1.4, min_aralik=0.33):
        self.rate = rate
        self.hassasiyet = hassasiyet
        self.min_aralik = min_aralik
        self.pencere = np.zeros(hop * 4, dtype=np.float32)  # 80 ms
        self.hann = np.hanning(len(self.pencere)).astype(np.float32)
        frekans = np.fft.rfftfreq(len(self.pencere), 1.0 / rate)
        self.kenar = np.unique(np.searchsorted(frekans, np.geomspace(*AKI_HZ, 25)))
        self.onceki = deque(maxlen=3)         # son adimlarin bant gurlukleri (bos: sessizlikten cikiliyor)
        self.gecmis = deque(maxlen=int(1.5 / HOP_SANIYE))
        self.aki_ort = self.toplam_ort = 0.0  # ~4 sn'lik ortalamalar
        self.guc_ort = 0.0                    # ~0.3 sn'lik ortalama ses gucu
        self.tepe_db = TEPE_TABAN_DB
        self.iz = 0.0                         # son vurusun sonen izi: ardindan gelen zayif sesleri eler
        self.sure = 0.0
        self.son_vurus = -1e9
        self.sertlik = 0.5
        self.tempo = Tempo()

    def isle(self, mono):
        """mono: float32 ornekler (-1..1). (seviye 0..1, vurus_gucu 0..1) dondurur;
        sessizlikte seviye 0, vurus yoksa vurus_gucu 0."""
        n = len(mono)
        if n == 0:
            return 0.0, 0.0
        dt = n / self.rate
        w = len(self.pencere)
        if n >= w:
            self.pencere[:] = mono[-w:]
        else:
            self.pencere[:-n] = self.pencere[n:]
            self.pencere[-n:] = mono

        guc = np.abs(np.fft.rfft(self.pencere * self.hann)) ** 2
        gurluk = np.add.reduceat(guc[: self.kenar[-1]], self.kenar[:-1]) ** 0.5
        return self.degerlendir(dt, float(np.mean(mono * mono)), gurluk)

    def degerlendir(self, dt, anlik, gurluk):
        """anlik: parcanin ortalama gucu, gurluk: bant genlikleri."""
        self.sure += dt
        self.guc_ort += (anlik - self.guc_ort) * yaklas(dt, 0.3)
        if anlik < SESSIZLIK_RMS ** 2:
            self.onceki.clear()
            self.tempo.sifirla()
            return 0.0, 0.0
        db = 10.0 * math.log10(self.guc_ort + 1e-12)
        self.tepe_db = max(TEPE_TABAN_DB, db, self.tepe_db - 0.25 * dt)
        seviye = min(1.0, max(0.0, 1.0 - (self.tepe_db - db) / DINAMIK_DB))

        if not self.onceki:  # sessizlikten cikis: karsilastirilacak onceki adim yok
            self.onceki.append(gurluk)
            return seviye, 0.0
        # son 3 adimin en yuksegine gore artis: titresen/vuran surekli sesler vurus sayilmaz
        aki = float(np.maximum(gurluk - np.max(self.onceki, axis=0), 0.0).sum())
        toplam = float(gurluk.sum())
        self.onceki.append(gurluk)

        # surekli seslerin kendi dalgalanmasi (medyan) dusulur; geriye yalnizca ani girisler kalir
        hazir = len(self.gecmis) >= self.gecmis.maxlen // 2
        ortalama = sum(self.gecmis) / len(self.gecmis) if self.gecmis else 0.0
        medyan = float(np.median(self.gecmis)) if self.gecmis else 0.0
        self.gecmis.append(aki)
        yeni = max(aki - medyan, 0.0)
        self.tempo.ekle(yeni, self.sure)

        a = yaklas(dt, 4.0)
        self.aki_ort += (yeni - self.aki_ort) * a
        self.toplam_ort += (toplam - self.toplam_ort) * a
        alt, ust = SERTLIK_ARALIK
        oran = self.aki_ort / (self.toplam_ort + 1e-12)
        self.sertlik = min(1.0, max(0.0, (oran - alt) / (ust - alt)))

        self.iz *= math.exp(-dt / 0.3)
        if not (hazir and aki > self.hassasiyet * ortalama and yeni > self.iz
                and yeni > VURUS_TABANI * toplam
                and self.sure - self.son_vurus >= self.min_aralik):
            return seviye, 0.0
        self.son_vurus = self.sure
        self.iz = yeni
        return seviye, min(1.0, max(0.3, yeni / (0.4 * toplam)))


class Sahne:
    """Analiz sonuclarini ampulun o an olmasi gereken renk ve parlakligina cevirir.
    Butun gecisler `sertlik`e gore ayarlanir: yumusak sarkida renk yavasca kayar,
    parlaklik sarkinin yukselip alcalmasini izler; sert sarkida renk aninda atlar,
    parlaklik her vurusta patlayip soner.
    Ritim duzenliyse vuruslar onceden bilinir (bkz. Tempo): gonderici komutu vurustan
    once yola cikarir ve darbeyi `vurus()` ile baslatir; renk her vurusta degil,
    RENK_VURUSU vurusta bir degisir. Duzenli ritim yoksa algilanan vuruslara tepki verilir."""

    def __init__(self, maks_parlaklik):
        self.maks = maks_parlaklik
        self.hue = self.hedef_hue = float(random.randrange(360))
        self.zarf = 0.0       # ses seviyesini izleyen yavas zarf: fade in / fade out
        self.darbe = 0.0      # vurusta ziplayip sonen deger
        self.parlak = 0.0     # cikis parlakligi 0..1
        self.renk_yasi = 0.0  # son renk degisiminden beri gecen sure
        self.renk_hizi = 0.0  # hedef renge yaklasma hizi, derece/sn
        self.sertlik = 0.5
        self.ritim = None     # (sonraki vurusun ani [time.monotonic saatiyle], periyot) ya da None
        self.son_vurus = 0.0  # vurus() ile islenen son vurusun ani
        self.vurus_no = 0
        self.gecikme = 0.1    # ampulun komutlara ortalama yanit suresi, sn

    def ilerle(self, dt, seviye, vurus_gucu, sertlik, ritim=None):
        s = self.sertlik = sertlik
        self.ritim = ritim
        # fade in / fade out: zarf sese yavas yetisir, sert sarkida daha cabuk
        tau = ara(1.5, 0.3, s) if seviye > self.zarf else ara(3.0, 0.8, s)
        self.zarf += (seviye - self.zarf) * yaklas(dt, tau)

        self.darbe *= math.exp(-dt / (0.3 * ritim[1] if ritim else ara(0.9, 0.2, s)))
        self.renk_yasi += dt
        if not ritim:  # duzenli ritim yok: algilanan vurusa tepki ver, rengi seyrek degistir
            if vurus_gucu:
                self.darbe = max(self.darbe, vurus_gucu)
            bekleme = ara(5.0, 1.5, s)
            if seviye > 0 and (self.renk_yasi >= bekleme + 2.0 or (vurus_gucu and self.renk_yasi >= bekleme)):
                self.renk_degistir()
        self._yumusat(dt)

    def sonraki_vurus(self):
        """Henuz islenmemis ilk vurusun ani; duzenli ritim yoksa None."""
        if not self.ritim:
            return None
        an, periyot = self.ritim
        return an if an - self.son_vurus > 0.6 * periyot else an + periyot

    def vurus(self, an):
        """Onceden bilinen vurus: darbeyi baslat, RENK_VURUSU vurusta bir renk degistir."""
        self.son_vurus = an
        self.darbe = 1.0
        self.vurus_no += 1
        if self.vurus_no % (RENK_VURUSU if self.sertlik >= 0.5 else 2 * RENK_VURUSU) == 0:
            self.renk_degistir()
        self._yumusat(HOP_SANIYE)
        self.parlak = self.zarf  # darbe tam vurusta tepede olsun (yumusatma onu geciktirmesin)

    def renk_degistir(self):
        """Yeni hedef renk: yumusak sarkida komsu bir tona yavas gecis, sert sarkida
        belirgin ve ani bir atlama."""
        s = self.sertlik
        adim = ara(35, 135, s)
        self.hedef_hue = (self.hedef_hue + adim) % 360
        self.renk_hizi = adim / ara(3.0, 0.6, s / 0.75) if s < 0.75 else 1e9  # derece/sn
        self.renk_yasi = 0.0

    def _yumusat(self, dt):
        s = self.sertlik
        # renk: hedefe en kisa yoldan, sabit hizla (esit adimlar goze en puruzsuz gorunur)
        fark = (self.hedef_hue - self.hue + 180) % 360 - 180
        self.hue = (self.hue + max(-self.renk_hizi * dt, min(self.renk_hizi * dt, fark))) % 360
        # parlaklik: vuruslar arasinda `derinlik` kadar kisilir, vurusta zarfa cikar
        derinlik = ara(0.08, 0.45, s)
        hedef = self.zarf * (1.0 - derinlik * (1.0 - self.darbe))
        self.parlak += (hedef - self.parlak) * yaklas(dt, ara(0.35, 0.0, s))

    def cikis(self):
        """(hue 0-359, parlaklik %): ampule gonderilecek tam sayilar."""
        taban = min(TABAN_PARLAKLIK, self.maks)
        return round(self.hue) % 360, max(1, round(taban + (self.maks - taban) * self.parlak ** GAMA))


def ses_baslat(loop, kuyruk):
    """Varsayilan cikis cihazinin loopback kaydini baslatir (yalnizca Windows)."""
    try:
        import pyaudiowpatch as pyaudio
    except ImportError:
        sys.exit("PyAudioWPatch kurulu degil:  pip install PyAudioWPatch")

    pa = pyaudio.PyAudio()
    try:
        cihaz = pa.get_default_wasapi_loopback()
    except (OSError, LookupError) as e:
        pa.terminate()
        sys.exit(f"Loopback ses cihazi bulunamadi: {e}")

    rate = int(cihaz["defaultSampleRate"])
    kanal = int(cihaz["maxInputChannels"])
    hop = int(rate * HOP_SANIYE)

    def koy(veri):
        if not kuyruk.full():
            kuyruk.put_nowait(veri)

    def geri_cagri(in_data, frame_count, time_info, status):
        veri = np.frombuffer(in_data, dtype=np.int16).astype(np.float32) / 32768.0
        if kanal > 1:
            veri = veri.reshape(-1, kanal).mean(axis=1)
        try:
            loop.call_soon_threadsafe(koy, veri)
        except RuntimeError:  # program kapanirken
            pass
        return (None, pyaudio.paContinue)

    stream = pa.open(
        format=pyaudio.paInt16,
        channels=kanal,
        rate=rate,
        input=True,
        input_device_index=cihaz["index"],
        frames_per_buffer=hop,
        stream_callback=geri_cagri,
    )

    def kapat():
        try:
            stream.stop_stream()
            stream.close()
        finally:
            pa.terminate()

    return rate, hop, cihaz["name"], kapat


async def gonderici(light, sahne):
    """Sahnenin o anki durumunu ampule yollar. Ampulde gecis suresi ayari yok;
    fade'ler ara degerler art arda gonderilerek yapilir. Ampul yavas kalirsa
    aradaki degerler atlanir, boylece komutlar birikip gecikme yaratmaz."""
    son = None
    while True:
        # Siradaki vurus yakinsa araya baska komut sokma: komut, ampulun yanit suresi
        # kadar ONCE yola cikar ki isik vurusla ayni anda degissin.
        an = sahne.sonraki_vurus()
        if an is not None:
            kalan = an - sahne.gecikme - ONCULUK - time.monotonic()
            if kalan < sahne.gecikme + 0.03:
                await asyncio.sleep(max(0.0, kalan))
                sahne.vurus(an)
        hue, parlaklik = sahne.cikis()
        if (son and abs((hue - son[0] + 180) % 360 - 180) < 3
                and abs(parlaklik - son[1]) < 2):  # gozle fark edilmeyecek kadar az degisti
            await asyncio.sleep(0.01)
            continue
        basla = time.monotonic()
        try:
            await light.set().brightness(parlaklik).hue_saturation(hue, 100).send(light)
            son = (hue, parlaklik)
            sahne.gecikme += (time.monotonic() - basla - sahne.gecikme) * 0.2
        except Exception as e:  # ag kopmasi, oturum suresi dolmasi vb. - bekle, devam et
            print(f"\n[uyari] ampule komut gitmedi: {e}")
            await asyncio.sleep(1.0)
            try:
                await light.refresh_session()
            except Exception:
                pass
        await asyncio.sleep(max(0.0, GONDERIM_ARALIK - (time.monotonic() - basla)))


async def ampule_baglan(ip, email, sifre):
    """(light, baslangictaki_durum) dondurur. tapo kutuphanesi, yeni Tapo
    yazilimlarinin kullandigi TPAP protokolunu destekler (python-kasa desteklemez)."""
    try:
        from tapo import ApiClient
    except ImportError:
        sys.exit("tapo kurulu degil:  pip install tapo")

    try:
        light = await ApiClient(email, sifre, timeout_s=10).l535(ip)
        durum = await light.get_device_info()
    except Exception as e:
        sys.exit(
            f"Ampule baglanilamadi ({ip}): {e}\n"
            " - IP dogru mu, PC ampulle ayni agda mi?\n"
            " - E-posta ve sifre TP-Link (Tapo) hesabindakiyle birebir ayni mi\n"
            "   (buyuk/kucuk harf dahil)?"
        )
    return light, durum


async def durumu_geri_yukle(light, d):
    try:
        ayar = light.set().brightness(d.brightness)
        if d.color_temp:
            ayar = ayar.color_temperature(d.color_temp)
        else:
            ayar = ayar.hue_saturation(d.hue or 0, d.saturation or 100)
        if not d.device_on:
            ayar = ayar.off()
        await ayar.send(light)
    except Exception as e:
        print(f"[uyari] eski isik ayari geri yuklenemedi: {e}")


async def calistir(args):
    sifre = os.environ.get("TAPO_SIFRE") or getpass.getpass("TP-Link hesap sifresi: ")
    light, onceki = await ampule_baglan(args.ip, args.email, sifre)
    print(f"Ampul: {onceki.nickname} ({onceki.model})")

    loop = asyncio.get_running_loop()
    kuyruk = asyncio.Queue(maxsize=100)
    rate, hop, cihaz_adi, ses_kapat = ses_baslat(loop, kuyruk)
    print(f"Dinlenen cikis: {cihaz_adi} ({rate} Hz)")
    print("Muzigi ac. Cikmak icin Ctrl+C.")

    alg = Analiz(rate, hop, args.hassasiyet, args.min_aralik)
    sahne = Sahne(args.parlaklik)
    gorev = asyncio.create_task(gonderici(light, sahne))  # ilk komut ampulu de acar
    son_yazi = 0.0
    saat_farki = None  # time.monotonic() - alg.sure: ses saatini duvar saatine cevirir

    try:
        while True:
            try:
                mono = await asyncio.wait_for(kuyruk.get(), timeout=0.1)
            except asyncio.TimeoutError:
                # hicbir sey calmiyorken loopback veri gondermez; fade out yine de ilerlesin
                saat_farki = None
                alg.tempo.sifirla()
                sahne.ilerle(0.1, 0.0, 0.0, alg.sertlik)
                continue

            seviye, vurus_gucu = alg.isle(mono)
            simdi = time.monotonic()
            dt = len(mono) / rate
            # parcalar bazen gec gelir: en az geciken parcayi esas al (hafif sizintiyla saat kaymasini izle)
            saat_farki = simdi - alg.sure if saat_farki is None else min(simdi - alg.sure, saat_farki + 0.0005 * dt)
            tempo = alg.tempo
            ritim = (tempo.sonraki + saat_farki, tempo.periyot) if tempo.kilit else None
            sahne.ilerle(dt, seviye, vurus_gucu, alg.sertlik, ritim)

            if simdi - son_yazi > 0.25:
                son_yazi = simdi
                hue, parlaklik = sahne.cikis()
                bpm = f"{60 / tempo.periyot:3.0f}" if ritim else " --"
                print(f"\rsertlik %{alg.sertlik * 100:3.0f}  tempo {bpm}  parlaklik %{parlaklik:3d}  renk {hue:3d}"
                      f"  ampul {sahne.gecikme * 1000:4.0f} ms ", end="", flush=True)
    except asyncio.CancelledError:
        pass
    finally:
        print("\nKapatiliyor...")
        ses_kapat()
        gorev.cancel()
        await asyncio.gather(gorev, return_exceptions=True)
        await durumu_geri_yukle(light, onceki)


def main():
    p = argparse.ArgumentParser(description="Tapo ampulu PC'de calan muzige gore renklendirir.")
    p.add_argument("--ip", required=True, help="ampulun IP adresi (Tapo uygulamasi > ampul > ayarlar > Cihaz Bilgisi)")
    p.add_argument("--email", default=os.environ.get("TAPO_EMAIL"), help="TP-Link hesap e-postasi (ya da TAPO_EMAIL)")
    p.add_argument("--hassasiyet", type=float, default=1.4,
                   help="ritimsiz bolumlerde vurus esigi; kucuk = daha hassas (varsayilan 1.4, aralik ~1.1-2.5)")
    p.add_argument("--min-aralik", type=float, default=0.33, help="iki vurus arasi en az sure, sn (varsayilan 0.33)")
    p.add_argument("--parlaklik", type=int, default=100, choices=range(1, 101), metavar="1-100", help="en yuksek parlaklik %% (varsayilan 100)")
    args = p.parse_args()
    if not args.email:
        p.error("--email gerekli (ya da TAPO_EMAIL ortam degiskeni)")

    try:
        asyncio.run(calistir(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()