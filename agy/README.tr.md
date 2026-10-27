# quota-guard · Antigravity CLI

[![CI](https://github.com/BunyaminGP/quota-guard/actions/workflows/ci.yml/badge.svg)](https://github.com/BunyaminGP/quota-guard/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](../LICENSE)

**Google Antigravity CLI (`agy`)** için kota duyarlı yaşam döngüsü eklentisi ve otomatik devam çalıştırıcısı.

*[English README.md](README.md)*

> **[quota-guard](../README.tr.md)** projesinin parçası: Claude Code, Codex CLI ve Antigravity CLI için tek eklenti.
> Kurulum için [ana README](../README.tr.md#kurulum)'ye bak; bu sayfa Antigravity CLI ayrıntılarını anlatır.

quota-guard'ın Antigravity bölümü, kimlik bilgisi/anahtar dosyalarına dokunmadan `agy -p "/usage" --output-format json` çıktısındaki `.command.data.groups[].buckets[]` verisini izler. 5 saatlik oturum veya 7 günlük haftalık kota eşikleri aşıldığında modelin `.agy-quota/progress.md` dosyasına kontrol noktası (checkpoint) yazmasını sağlar, tetiklenen tüm pencerelerin en geç sıfırlanma zamanına (`resets_at`) kadar bekler ve konuşmayı yakalanan kesin konuşma kimliğiyle (`--conversation <ID>`) kaldığı yerden sürdürür.

## Temel Özellikler

- **Kimlik Dosyasına Dokunmayan Kota Okuyucu (`scripts/usage.py`)**: `remaining_fraction` değerini kullanılan yüzdeye (`(1 - remaining_fraction) * 100`) dönüştürür, tüm kota pencerelerini liste olarak korur ve `modelName` / `--model` değerine göre ilgili model grubunu (`Gemini Models`, `Claude and GPT models`, `Gemini 4 Argon`) filtreler.
- **Yerel Antigravity Kanca Sözleşmesi (`hooks.json`, `scripts/quota_gate.py`)**:
  - `PostToolUse`: Antigravity sözleşmesine uygun olarak her zaman `{}` döndürür; `task_boundary`, `TodoWrite`, `Task*` ve `update_plan` araçlarını `.agy-quota/todos_state.json` içinde takip eder.
  - `PreInvocation`: Yumuşak (soft) veya sert (hard) durdurmada `injectSteps` ile modele önce `.agy-quota/progress.md` dosyasına checkpoint yazma talimatı enjekte eder.
  - `PreToolUse`: Durdurma aktifken `.agy-quota/progress.md` güncellemesi dışındaki araçları `"decision": "deny"` ile engeller, checkpoint yazma imkânını korur.
  - `PostInvocation`: Modelin checkpoint yazma fırsatını kesmeden turu sonlandırır (`"terminationBehavior": "terminate"`); checkpoint yazılmadan sert kesme gerçekleşirse `.agy-quota/progress.md` içine eksik checkpoint uyarısını açıkça yazar.
- **Güvenli Opt-In Hard Stash**: `hard_abort_enabled` varsayılan olarak kapalıdır ve yalnızca `AGY_HARD_ABORT=1` ortam değişkeniyle açılabilir. Açık olduğunda bile yalnız görev başında çalışma ağacı temizse (`checkpoint_clean: true`) ve `refs/stash` referansının gerçekten değiştiği doğrulanırsa görevi `reverted` / `pending` olarak işaretler.
- **Platform Bağımsız Çalıştırıcı (`scripts/agy_runner.py`, `bin/agy-run`, `bin/agy-run.cmd`)**:
  - Uzun görevleri `stdin` üzerinden `agy` sürecine aktarır.
  - Yakalanan kesin konuşma kimliğiyle `--conversation <ID>` üzerinden devam eder; başka bir oturumu yanlışlıkla sürdürmemek için `-c` / `--continue` yedeklemesi kullanmaz.
  - Zaman aşımında (`AGY_ROUND_TIMEOUT`) tüm CLI süreç ağacını sonlandırır.
  - En son sıfırlanma zamanına (`resets_at`) tampon süre (`AGY_RESUME_BUFFER`) ekleyerek bekler ve `progress.md` içinde `^AGY_QUOTA_DONE` görüldüğünde başarıyla durur.

## Kurulum ve Doğrulama

Önerilen: depo kökünü Antigravity eklenti yöneticisiyle kur, sonra Antigravity'yi
yeniden başlat:
```bash
agy plugin validate . --strict   # quota-guard klasöründen
agy plugin install .
```

Kurulum betikleri alternatiftir ve `agy-run` başlatıcısını da `~/.local/bin`
altına koyar (ikisinden birini kullan, ikisini birden değil):
```bash
./agy/install.sh        # Unix / macOS / Linux / Git Bash
```
```powershell
.\agy\install.ps1      # Windows PowerShell
```

`agy` 1.4.x notu: `agy -p --mode X`, `--mode`'u istem (prompt) sanıyor; bu yüzden
`agy-run` istemi tek bir stream-json olayı olarak gönderir
(`agy --mode X --input-format stream-json --output-format stream-json -p=`).

## Normal sohbette otomatik devam

Varsayılan olarak açık. Kapatmak için `AGY_CHAT_AUTO_RESUME=0` ya da
`~/.quota-guard/settings.json` içinde `"chat_auto_resume": false` (projenin
config dosyasından asla okunmaz). `agy-run` etkilenmez: kendini
`AGY_QUOTA_RUNNER=1` ve `.agy-quota/runner.json` ile işaretler, kendi döngüsünü
kullanır.

Bir eşik normal bir `agy` sohbetini durdurduğunda durdurma talimatı ajana
Antigravity'nin kendi **`schedule`** aracını bir kez çağırmasını da söyler: reset
zamanına (+ `AGY_RESUME_BUFFER` saniye, varsayılan 60) kurulan tek seferlik bir
arka plan zamanlayıcısı (uzak bir haftalık reset en fazla 6 saatlik adımlarla).
Durmuş sohbette izin verilen tek araç bu. Ajan turu bunu çağırmadan bitirirse bir
kez hatırlatılır, sonra sana ⚠️ bildirimi gelir.

Zamanlayıcı dolunca aynı sohbet uyanır. Kanca bunu `schedule` aracının
`PostToolUse` olayı olarak görür, kotayı yeniden kontrol eder ve

- reset olduysa 🔋 *kota yenilendi* gönderir, ajan `.agy-quota/progress.md`
  dosyasından devam eder; ilk araç çağrısı ▶️ *devam ediyor*, turun sonu (`Stop`
  kancası) ✅ *bitti* bildirimini gönderir;
- hâlâ eşiğin üstündeyse durmuş kalır ve ajandan yeni bir zamanlayıcı ister.

Reset olana kadar Antigravity penceresi açık kalmalı.

## Kullanım

```bash
# Komut satırından doğrudan görev başlatma
AGY_LANG=tr agy-run "Kimlik doğrulama modülünü yenile ve birim testlerini çalıştır"

# Çok satırlı görev dosyasını stdin üzerinden iletme
AGY_LANG=tr agy-run < gorev.md

# Belirli bir model ve izin modu belirleme
agy-run --model gemini-2.5-pro --mode accept-edits < gorev.md
```

## Yapılandırma

Proje düzeyindeki kota eşiklerini `.agy-quota/config.json` içinde tanımlayabilirsiniz:

```json
{
  "session_soft_limit": 80,
  "session_hard_limit": 95,
  "weekly_soft_limit": 97,
  "weekly_hard_limit": 98,
  "language": "tr"
}
```

Ortam değişkenleri önceliklidir:
- `AGY_SESSION_SOFT_LIMIT`, `AGY_SESSION_HARD_LIMIT`
- `AGY_WEEKLY_SOFT_LIMIT`, `AGY_WEEKLY_HARD_LIMIT`
- `AGY_LANG=tr`
- `AGY_HARD_ABORT=1` (yalnızca ortam değişkeniyle açılabilir; proje yapılandırma dosyalarından asla okunmaz)
- `AGY_NOTIFY_URL=https://ntfy.sh/konu-adiniz` (düz metin webhook bildirimleri; proje yapılandırma dosyalarından asla okunmaz)

## Güvenlik Garantileri

- **Anahtar / Kimlik Dosyalarına Dokunulmaz:** API anahtarları, OAuth jetonları veya `~/.gemini/` altındaki kimlik dosyaları asla okunmaz veya saklanmaz.
- **Depodan Veri Sızdırma Engellenir:** Hassas ayarlar (`AGY_HARD_ABORT` ve `AGY_NOTIFY_URL`) klonlanan güvensiz depolardaki config dosyalarından asla okunmaz, yalnızca kullanıcı ortam değişkenlerinden okunur.
- **Fail-Open Tasarım:** Kota servisine erişilemediğinde kancalar hata fırlatıp Antigravity'yi kilitlemez, güvenli şekilde açık kalır.

## Lisans

Bu proje [MIT Lisansı](../LICENSE) kapsamında lisanslanmıştır.
