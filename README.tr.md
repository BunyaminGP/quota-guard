# quota-guard

[![CI](https://github.com/BunyaminGP/quota-guard/actions/workflows/ci.yml/badge.svg)](https://github.com/BunyaminGP/quota-guard/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Claude Code**, **OpenAI Codex CLI** ve **Google Antigravity CLI (`agy`)** için
tek eklenti: kodlama ajanını abonelik kullanım limitine çarpmadan önce temiz bir
kontrol noktasında durdurur, **kota yenilenince aynı sohbete kendiliğinden devam
eder** ve her adımda telefonuna bildirim gönderir.

*[English: README.md](README.md)*

## Ne yapar

- Ajan çalışırken 5 saatlik oturum ve haftalık kotanı izler.
- **Yumuşak eşik** (varsayılan oturum %80 / haftalık %97): ajan elindeki maddeyi
  bitirir, kontrol noktası yazar (`progress.md`) ve temiz şekilde durur.
  **Sert eşik** (%95 / %98): hemen durur.
- **Normal sohbette, ek komut olmadan:** reset zamanına bir uyandırma kurulur,
  kota yeniden kontrol edilir ve aynı sohbet kontrol noktasından devam eder.
- **Bildirimler** (ntfy ya da düz metin POST alan herhangi bir URL):
  ⏸ durdu (devam saatiyle) → 🔋 kota yenilendi → ▶️ devam ediyor → ✅ bitti,
  bir şey sana kaldıysa ⚠️.
- Gözetimsiz çalıştırmalar için isteğe bağlı çalıştırıcılar: `cc-run`,
  `codex-run`, `agy-run`.

## Sohbet nasıl uyanıyor

Her aracın sohbeti uyandırma yolu farklı; her biri kendi yolunu kullanır:

| Araç | Uyandırma | Kanca olayları |
|---|---|---|
| Claude Code | ajan reset zamanına tek seferlik bir `CronCreate` işi kurar | PostToolUse, UserPromptSubmit, Stop, StopFailure |
| Codex CLI | arka planda küçük bir izleyici; resette `codex queue --thread <id>` çalıştırır | PostToolUse, Stop |
| Antigravity CLI | ajan Antigravity'nin `schedule` aracıyla tek seferlik zamanlayıcı kurar | PreInvocation, PreToolUse, PostToolUse, PostInvocation, Stop |

Üçü de uyanınca kotayı yeniden kontrol eder. Reset gecikmişse yeniden bekler.
Ortak zamanlama ve durum mantığı `common/resume_core.py` içinde.

## Kurulum

Sadece kullandığın araç için kur: aşağıdaki tarifler birbirinden bağımsız. Her
CLI yalnızca kendi manifestini okur ve yalnızca kendi kancalarını yükler.
`PATH` üzerinde Python 3.10+ gerekir.

**Claude Code**: Claude Code'un içinde:

```
/plugin marketplace add BunyaminGP/quota-guard
/plugin install quota-guard@quota-guard
```

(ya da terminalden: `claude plugin marketplace add BunyaminGP/quota-guard`, sonra
`claude plugin install quota-guard@quota-guard`). Claude Code'u yeniden başlat.
Eşikler, dil, bildirim URL'si ve otomatik devam `/plugin` → quota-guard →
*Configure* ekranından ayarlanır.

**Codex CLI**: terminalden:

```bash
codex plugin marketplace add BunyaminGP/quota-guard
codex plugin add quota-guard@quota-guard
```

Sonra Codex'i aç, `/hooks` ekranında quota-guard'ın iki kancasına **trust**
ver. Codex onaylanmamış kancaları çalıştırmaz; güncellemeden sonra tekrar bak.
Eklenti sistemi olmadan: repoyu klonla ve `python quota-guard/codex/install.py`
çalıştır.

**Antigravity CLI**: `agy plugin install` yerel bir klasör istiyor, önce klonla:

```bash
git clone https://github.com/BunyaminGP/quota-guard
agy plugin install ./quota-guard
```

Antigravity'yi yeniden başlat. `quota-guard/agy/install.ps1` / `install.sh`
alternatiftir ve `agy-run` başlatıcısını da kurar (ikisinden birini kullan).

**Bildirimler (isteğe bağlı, tüm araçlar):** telefonunda bir ntfy konusuna abone
ol ve URL'sini `~/.quota-guard/settings.json` dosyasına yaz (sonraki bölüm).
Claude Code bunu *Configure* ekranından da alabilir.

## Üçünün ortak ayarları

`~/.quota-guard/settings.json` (kullanıcı klasörün):

```json
{
  "notify_url": "https://ntfy.sh/<uzun-rastgele-bir-konu>",
  "language": "tr",
  "chat_auto_resume": true
}
```

Araca özel ayarlar yine önceliklidir: Claude eklenti seçenekleri /
`CC_NOTIFY_URL`, `CODEX_QUOTA_NOTIFY_URL`, `AGY_NOTIFY_URL` vb. Bildirim URL'si
ve otomatik devam anahtarı **projenin dosyalarından asla okunmaz**; klonlanan
bir repo bildirimlerini başka yere yönlendiremez. ntfy konuları herkese açıktır:
adı tahmin eden okuyabilir, uzun ve rastgele bir ad seç.

## Sınırlar

- Reset olana kadar sohbet penceresi açık kalmalı. Pencere kapanırsa, bilgisayar
  uykuya geçer ya da yeniden başlarsa uyandırma olmaz. Kontrol noktası yine
  durur; "devam" demen yeterli.
- Claude Code ve Antigravity: zamanlayıcıyı ajanın kendisi kurar. Kurmazsa bir
  kez hatırlatılır, sonra sana ⚠️ bildirimi gelir.
- Codex: devam mesajının Codex'in o sohbet için tuttuğu kuyruğa düştüğü
  doğrulandı. Açık duran bir TUI'nin onu kendiliğinden çalıştırıp
  çalıştırmadığı Codex'in app-server daemon'una bağlı. 🔋 bildiriminden sonra
  bir şey olmazsa `devam` yaz.
- Sadece abonelik planlarında çalışır (Claude Pro/Max, ChatGPT planları, Google
  AI planları). API anahtarıyla ödemede izlenecek kota yok.
- Sert eşikte `git stash` ile otomatik geri alma varsayılan olarak kapalı. Açmadan
  önce ilgili aracın README'sindeki güvenlik bölümünü oku.

## Depo yapısı

```
.claude-plugin/        Claude Code manifesti + marketplace (kancalar: claude/hooks/hooks.json)
.codex-plugin/         Codex manifesti (kancalar: codex/hooks/hooks.json)
.antigravity-plugin/   Antigravity manifesti; kancaları ./hooks.json (betikler agy/ içinde)
common/                resume_core.py: ortak devam zamanlaması, durum ve güvenli yazma
claude/ codex/ agy/    araca özel kancalar, çalıştırıcılar, testler ve belgeler
```

Ayrıntılar: [Claude Code](claude/README.tr.md) · [Codex CLI](codex/README.tr.md) ·
[Antigravity CLI](agy/README.tr.md)

## Testler

```bash
cd claude && python -m pytest -q && bash tests/test_cc_run.sh
cd codex  && python -m pytest -q
cd agy    && python -m pytest -q
```

Testler sahte CLI'lar ve yerel HTTP sunucuları kullanır: model çağrısı ve gerçek
bildirim yok. CI üç paketi Ubuntu, macOS ve Windows'ta çalıştırır.

## Geçmiş

2.0.0, [cc-quota-guard](https://github.com/BunyaminGP/cc-quota-guard),
codex-quota-guard ve agy-quota-guard projelerini tek repo ve tek eklentide
birleştirir. Bkz. [CHANGELOG.md](CHANGELOG.md).

## Lisans

[MIT](LICENSE)
