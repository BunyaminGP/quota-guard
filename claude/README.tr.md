# quota-guard · Claude Code

[![CI](https://github.com/BunyaminGP/quota-guard/actions/workflows/ci.yml/badge.svg)](https://github.com/BunyaminGP/quota-guard/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](../LICENSE)
[![Changelog](https://img.shields.io/badge/changelog-md-lightgrey.svg)](CHANGELOG.md)

Claude Code'u **kota-farkındalıklı** çalıştırır: verdiğin yüzde eşiklerine
gelince işi güvenli bir noktada durdurur, ilerlemeyi diske yazar ve kota
**reset olunca kaldığı yerden otomatik devam eder**.

*[English README.md](README.md)*

> **[quota-guard](../README.tr.md)** projesinin parçası: Claude Code, Codex CLI ve Antigravity CLI için tek eklenti.
> Kurulum için [ana README](../README.tr.md#kurulum)'ye bak; bu sayfa Claude Code ayrıntılarını anlatır.

> Bu dosya İngilizce ana README'nin Türkçe çevirisidir; proje kodu, hook
> mesajları ve GitHub üzerindeki birincil dokümantasyon İngilizcedir.

## Çözdüğü sorun

Normalde limit dolduğu an Claude Code aniden durur — neredeyse, yarım kalmış
düzenlemeler, hiçbir kayıt olmadan. Bu araç limite *toslamadan önce*, siz
hangi noktada ve nasıl durmasını istiyorsanız öyle durur:

- **YUMUŞAK eşik** (varsayılan %80 session / %97 haftalık): hâlâ pay var —
  Claude mevcut todo maddesini bitirir, sonra temiz kapanış yapar (commit +
  ilerleme notu) ve durur. Session ve haftalık birbirinden bağımsız
  katmanlardır — ikisinden biri diğerinden bağımsız tetiklenebilir.
- **SERT eşik** (varsayılan %95 session / %98 haftalık): madde ortasında bile
  tetiklenebilir, çünkü o noktada güvenle bitirecek pay kalmamış olabilir.
  **Varsayılan olarak kapalı.** Açarsanız, Claude'un o maddede yaptığı
  değişiklikler `git stash` ile geri alınır, madde tekrar `pending` olur ve
  reset sonrası sıfırdan yapılır. 8 maddelik bir listede 6. madde yarıda sert
  eşiğe çarparsa: 6.'nın yaptığı iş stash'e taşınır, reset olur, 6. sıfırdan
  yapılır, 7-8 normal devam eder.

## Kurulum

**Önerilen — Claude Code plugin'i olarak:**

```
/plugin marketplace add BunyaminGP/quota-guard
/plugin install quota-guard@quota-guard
```

Bu kadar — elle dosya kopyalama yok, `settings.json` düzenleme yok. Hook'lar
otomatik kaydolur ve o andan itibaren her oturumu korumaya başlar.

**Not:** bir plugin'in `bin/` klasörü sadece Claude'un **kendi** iç Bash tool
çağrıları için PATH'e ekleniyor — sizin terminalinize değil. `cc-run` sizin
çalıştırmanız için var (headless, kendi kendine devam eden bir Claude
çalışması başlatan şey o), o yüzden onu kendi shell'inizden düz `cc-run`
komutuyla çağırabilmek için küçük bir ek adım gerekiyor — aşağıdaki
[cc-run'ı kendi terminalinizden kullanma](#cc-runı-kendi-terminalinizden-kullanma)
bölümüne bakın. Hook'ların kendisi (asıl kota koruması) buna ihtiyaç
duymuyor — plugin kurulur kurulmaz zaten çalışıyor.

Takımınızla version control üzerinden paylaşmak isterseniz user yerine
project scope'una kurun:

```
claude plugin install quota-guard@quota-guard --scope project
```

### Yapılandırma ekranı

Plugin'i etkinleştirdiğinizde Claude Code size eşikleri sorar — aracın
gerçek "ayarlar ekranı" bu, plugin'in `userConfig` alanı üzerinden
tanımlanıyor:

- **Yumuşak eşik — session (%)** — varsayılan 80
- **Sert eşik — session (%)** — varsayılan 95
- **Yumuşak eşik — haftalık (%)** — varsayılan 97
- **Sert eşik — haftalık (%)** — varsayılan 98
- **Kota yenilenince sohbete otomatik devam et** — varsayılan açık. Bkz.
  [Normal sohbette otomatik devam](#normal-sohbette-otomatik-devam-cc-runsız)
- **Sert eşikte otomatik geri almayı (git stash) aç** — varsayılan kapalı
- **Bildirim webhook URL'i (opsiyonel)** — varsayılan boş (kapalı). Bkz.
  [Bildirimler](#bildirimler)

Sonradan, yeniden kurmadan değiştirmek için:

```
claude plugin install quota-guard@quota-guard --config session_soft=70 --config hard_abort_enabled=true
```

(değiştirmek istediğiniz her alan için `--config key=value` tekrarlayın),
ya da bir oturum içinde `/plugin` yazıp plugin için "configure" seçeneğini
kullanın. Değerler plugin'in dosyalarında değil, kendi
`~/.claude/settings.json`'ınızda `pluginConfigs` altında saklanır.

**Fallback — manuel kurulum** (plugin sistemi olmayan eski Claude Code
sürümleri için):

```bash
git clone https://github.com/BunyaminGP/quota-guard
cd quota-guard/claude
bash install.sh
```

Bu, aracı `~/.claude/cc-quota-guard/` altına kopyalar ve `PostToolUse`
hook'larını `~/.claude/settings.json`'a merge eder (idempotent — güncelleme
için tekrar çalıştırmak güvenli).

## cc-run'ı kendi terminalinizden kullanma

`cc-run`'ın kendi shell'inizde düz komut olarak çalışması için bir kerelik
kurulum (hook'lar/koruma için gerekli değil — sadece `cc-run` wrapper'ının
kendisi için):

**Windows / PowerShell:**

```powershell
Get-Content shell\cc-run.ps1 | Add-Content $PROFILE
. $PROFILE
```

(Git for Windows gerektirir, Git Bash için — `cc-run` bir bash script'i,
PowerShell onu direkt çalıştıramaz). Bu, plugin'in güncel kurulum yolunu her
çağrıda yeniden bulan bir `cc-run` fonksiyonu tanımlar, plugin güncellemeleri
sonrası bozulmaz.

**macOS / Linux:**

```bash
cat shell/cc-run.sh >> ~/.bashrc   # ya da ~/.zshrc
source ~/.bashrc
```

Aynı mantık — her çağrıda plugin'in kurulum yolunu yeniden bulan bir shell
fonksiyonu. Plugin sistemi yerine manuel `install.sh` fallback'ini
kullandıysanız zaten sabit bir yolunuz var:
`~/.claude/cc-quota-guard/bin/cc-run` — sadece `~/.local/bin`'in (
`install.sh`'ın sembolik link koyduğu yer) `PATH`'inizde olduğundan emin
olun, bu adımı atlayabilirsiniz.

## Kullanım

Tam otomatik (dur + reset'te otomatik devam):

```bash
cc-run "auth servisini 3 adımda refactor et: ..."
cc-run --threshold 80 --session-hard 95 --weekly-soft 97 --weekly-hard 98 @gorev.md
```

- `--threshold N` — session **YUMUŞAK** eşiği (varsayılan 80). Madde biter,
  sonra durur.
- `--session-hard N` — session **SERT** eşiği (varsayılan 95). Madde
  ortasında tetiklenebilir.
- `--weekly-soft N` — haftalık **YUMUŞAK** eşik (varsayılan 97). Madde biter,
  sonra durur.
- `--weekly-hard N` — haftalık **SERT** eşik (varsayılan 98). Madde
  ortasında tetiklenebilir.
- `--enable-hard-abort` — SERT eşik tetiklendiğinde madde ortasındaki işi
  otomatik geri almayı (`git stash`) etkinleştirir. **Bu bayrağı vermezseniz
  kapalıdır.** Vermezseniz SERT eşik de YUMUŞAK gibi sadece temiz kapanışa
  zorlar — hiçbir şey otomatik dokunulmaz. Açmadan önce aşağıdaki
  [Güvenlik](#güvenlik--hard-abort-açmadan-önce-okuyun) bölümünü okuyun.
- `--model AD` — hangi modelin çalışacağı (`opus`, `sonnet`, `fable`, ya da
  tam model adı). Vermezseniz `claude` CLI'nizin kendi varsayılanı kullanılır
  — `--model` vermeden `claude` çalıştırmakla aynı.
- Görev: düz metin ya da `@dosya.md`.

`cc-run` her turun başında **çözümlenmiş** model adını yazdırır (ör.
`🧠 model: claude-sonnet-5`) — `opus`/`sonnet` gibi takma adlar "en son
sürüm" demek, yani hangi tarihli spesifik modelin gerçekten çalıştığını
görebileceğiniz tek yer burası. Ayrıca hangi tool'ların çağrıldığını ve her
turun sonunda bir maliyet/model dökümü de gösterir. Bu, `--output-format
stream-json`'ı `scripts/stream_render.py`'dan geçirmekle oluyor — aynı
çalışma, ekstra maliyet yok, sadece ham metin yerine ayrıştırılmış hâli.

## Normal sohbette otomatik devam (cc-run'sız)

Bunun için `cc-run` gerekmez. Bir eşik Claude'u sıradan, etkileşimli bir
sohbette durdurduğunda, sohbet reset'ten sonra kendiliğinden devam eder:

1. Hook Claude'u eskisi gibi durdurur (ilerleme `.cc-quota/progress.md`'ye
   yazılır) ve ayrıca ona Claude Code'un yerleşik **CronCreate** aracıyla
   kendi uyanışını kurmasını söyler — reset'ten yaklaşık bir dakika sonrası
   için tek seferlik bir zamanlayıcı. Durdurma mesajı saati gösterir:
   *"Otomatik devam: 14:15 (Claude Code penceresi açık kalmalı)."*
2. Claude o turu bitirince bir `Stop` hook'u uyanışın gerçekten kurulduğunu
   kontrol eder. Kurulmadıysa Claude'dan bir kez daha istenir; yine yoksa
   "sohbete kendin 'devam' yaz" diyen bir ⚠️ bildirim gelir.
3. O saatte zamanlayıcı sohbete bir devam mesajı gönderir. Bir
   `UserPromptSubmit` hook'u kotayı yeniden okur: gerçekten yenilendiyse
   **🔋 Kota yenilendi** bildirimi gelir ve Claude `progress.md`'den devam
   eder; yenilenmediyse (reset gecikti) Claude sessizce yeni bir uyanış kurar.
4. Ondan sonraki ilk araç çağrısı **▶️ Claude kaldığı yerden çalışmaya devam
   ediyor**, o turun sonu da **✅ Bitti** bildirimi gönderir.

Sınırlar — bu CronCreate'e dayandığı için:

- o saate kadar Claude Code penceresi açık (ve bilgisayar uyanık) kalmalı —
  zamanlayıcı sadece o oturumda yaşar;
- sadece sohbet boştayken tetiklenir; o an bir şey yazıyorsanız sizi bekler;
- limit, hook araya giremeden dolarsa (ör. tek bir çok uzun cevap) yardımcı
  olamaz. O zaman `StopFailure` hook'undan ⛔ "kullanım limiti hatasıyla
  durdu" bildirimi gelir; reset sonrası sohbete kendiniz "devam" yazarsınız.

Varsayılan olarak açık. `chat_auto_resume` ayarıyla ya da
`CC_CHAT_AUTO_RESUME=0` ile kapatılır. `cc-run` çalışmaları etkilenmez — o
zaten kendi kendine devam ettiği için sohbet mantığı onun yoluna girmez.

**Kota harcamadan deneyin.** Simülasyon, hook'a birkaç dakikalığına sahte bir
kota gösterir:

```bash
python3 scripts/usage.py --simulate 96 --reset-in 3   # şu an %96, 3 dk sonra reset, sonra %3
python3 scripts/usage.py                               # SIMULATED okumayı gösterir
python3 scripts/usage.py --simulate-off                # erken kapat
```

Sonra bir sohbette Claude'a küçük, çok adımlı bir görev verin. Sahte okuma
`~/.claude/.cc-quota-sim.json`'da durur (ev dizininiz, asla proje değil —
klonlanan bir repo kotanızı sahteleyemesin diye) ve sahte reset'ten 30 dakika
sonra kendini kapatır. Açıkken hook'un bu sürümünü çalıştıran her Claude Code
penceresini etkiler.

Etkileşimli oturumlar için eşikler ortam değişkenleriyle de verilebilir
(aşağıdaki [Bir ortam değişkeni ayarlama](#bir-ortam-değişkeni-ayarlama)
bölümüne bakın):

```bash
export CC_SESSION_SOFT=80 CC_SESSION_HARD=95 CC_WEEKLY_SOFT=97 CC_WEEKLY_HARD=98 CC_HARD_ABORT=1
```

```powershell
$env:CC_SESSION_SOFT = "80"; $env:CC_SESSION_HARD = "95"; $env:CC_WEEKLY_SOFT = "97"; $env:CC_WEEKLY_HARD = "98"; $env:CC_HARD_ABORT = "1"
```

## Nasıl çalışır

1. **Hook** (`scripts/quota_gate.py`) — tek bir `PostToolUse` `*`
   matcher'ına takılı (her tool çağrısı); planlama araçlarını ikinci bir
   matcher'a ihtiyaç duymadan kendi içinde ayırt eder:
   - Bir `TodoWrite` çağrısında — ya da bazı Claude Code sürümlerinin onun
     yerine kullandığı `TaskCreate`/`TaskUpdate`/`TaskList` çağrılarından
     birinde: hangi maddenin `in_progress` olduğunu ve hangi commit'ten
     başladığını `.cc-quota/todos_state.json`'a kaydeder; YUMUŞAK eşikleri
     kontrol eder (session ve haftalık birbirinden bağımsız katmanlardır,
     ikisinden biri tek başına tetiklenebilir). İki araç ailesi de tanınıyor
     çünkü birbirinin yerine
     geçmiyorlar — sadece birini tanıyan bir eklenti, diğerini kullanan bir
     Claude Code sürümünde YUMUŞAK eşiğin (ve hard-abort'un in_progress
     takibinin) hiç tetiklenmediğini fark bile etmezdi.
   - **Her** çağrıda: SERT eşikleri kontrol eder. Bir maddenin içindeki iş
     (birçok Edit/Bash/Write çağrısı) tek bir planlama-aracı çağrısından
     sonra uzun sürebilir; sadece orada bakmak bir kota patlamasını çok geç
     fark ettirir. Planınız haftalık toplamdan ayrı, sadece Sonnet'e özel
     7 günlük bir rakam raporluyorsa, o da aynı `weekly_soft`/`weekly_hard`
     eşiklerine karşı kontrol edilir — yoğun Sonnet kullanan bir oturum,
     toplam haftalık kota hâlâ rahat görünürken o kapasiteyi tüketebilirdi.
2. **State dosyaları**
   - `.cc-quota/progress.md` — ne bitti, sırada ne var, ve (sert geri alma
     tetiklendiyse) hangi madde geri alındı. Reset sonrası buradan devam
     edilir.
   - `.cc-quota/todos_state.json` — hook'un kendi todo anlık görüntüsü.
3. **Wrapper** (`bin/cc-run`) — Claude'u başlatır; bir eşik onu durdurunca
   `.cc-quota/STOP.json`'daki reset zamanını okur, o zamana kadar uyur,
   `claude -c` ile devam ettirir. Bir round tamamen başarısız olur ya da
   `CC_ROUND_TIMEOUT` süresini aşarak asılı kalırsa (ör. tam devam etme
   anında ağ kesilmesi), sessizce pes etmek yerine backoff ile yeniden
   dener. `progress.md`'de `CC_QUOTA_DONE` görünce durur.
4. **Sohbette otomatik devam** — aynı hook script'i, `UserPromptSubmit`,
   `Stop` ve `StopFailure` olaylarına da kayıtlı (yardımcılar
   `scripts/chat_resume.py`'de, durum `.cc-quota/chat_resume.json`'da). Bkz.
   [Normal sohbette otomatik devam](#normal-sohbette-otomatik-devam-cc-runsız).

Kota kaynağı: Anthropic'in **dokümante edilmemiş** OAuth usage endpoint'i
(`/api/oauth/usage`) — session (5s) ve haftalık yüzdeleri + reset zamanlarını
verir.

## Bildirimler

Opsiyonel: önemli anlarda (bir eşik Claude'u durdurdu, `cc-run` devam etti,
görev bitti, ya da `cc-run` retry'lardan sonra pes etti — normal sohbette de:
kota yenilendi, Claude devam ediyor, bitti) terminale bakmak yerine bir push
bildirimi al.

**Kurulum (ntfy.sh — şu ana kadar test edilen tek backend):**

1. [ntfy](https://ntfy.sh) uygulamasını kur (iOS/Android), ya da tarayıcıdan
   ntfy.sh üzerinden kullan.
2. Kendi seçtiğin bir konu (topic) adına abone ol. Public sunucuda **hiçbir
   kimlik doğrulama yok** — konu adını bilen herkes ona yayınlanan her şeyi
   görebilir — o yüzden düz bir kelime değil, tahmin edilmesi zor bir isim
   seç. Örnek: `ccqg-adin-a1b2c3`, `test` değil.
3. `CC_NOTIFY_URL`'i o adrese ayarla — ortam değişkeni olarak (bkz.
   [Bir ortam değişkeni ayarlama](#bir-ortam-değişkeni-ayarlama)), ya da
   plugin'in yapılandırma ekranında (`notify_url`):

   ```bash
   export CC_NOTIFY_URL=https://ntfy.sh/senin-konu-adin
   ```

   ```powershell
   $env:CC_NOTIFY_URL = "https://ntfy.sh/senin-konu-adin"
   ```

Bu kadar. Sıradaki eşik vuruşu, resume, tamamlanma ya da pes etme bir
bildirim gönderir.

**Ne gönderilir:** sana zaten gösterilen aynı kısa durum satırı (hook'un
`systemMessage`'ı, ya da `cc-run`'ın kendi terminal satırı) — ör. *"Kota
eşiği aşıldı (5 saatlik session %86) — Claude temiz bir şekilde topluyor.
Sıfırlanma: ..."*. Asla görev içeriği, dosya yolu ya da todo madde metni
gitmez.

**Bu araçtaki her şey gibi fail-open:** `CC_NOTIFY_URL` ayarlanmamışsa,
URL yanlışsa, ağ yoksa ya da zaman aşımına uğrarsa — hiçbir şey bozulmaz.
Bildirim sessizce atlanır; hook ve `cc-run` bildirim hiç yokmuş gibi devam
eder. `CC_NOTIFY_TIMEOUT` (varsayılan 5sn) tek bir bildirim denemesinin
alabileceği azami süreyi sınırlar.

**Ham metin HTTP POST body'si kabul eden herhangi bir endpoint aynı
şekilde çalışır** — ama sadece ntfy.sh gerçekten test edildi. Slack'in ya
da Discord'un JSON webhook şeklini (`{"text": ...}` / `{"content": ...}`)
konuşmuyor; `CC_NOTIFY_URL`'i doğrudan onlardan birine yöneltmek muhtemelen
doğru render olmaz, çünkü JSON değil düz metin body POST ediliyor.

**Güvenlik notu:** `hard_abort_enabled` gibi, `notify_url`/`CC_NOTIFY_URL`
de bilerek `.cc-quota/config.json`'dan **asla** okunmaz — sadece ortam
değişkeni ya da plugin'in kendi yapılandırma ekranı bunu ayarlayabilir. Bir
bildirim URL'i session durumunun nereye gittiğini belirler; klonlanmış/
güvenilmeyen bir projenin `config.json`'ının bunu ayarlayabilmesi, o
projenin session'ın durumunu senin kontrolünde olmayan bir sunucuya
sessizce yönlendirebilmesi demek olurdu.

## Güvenlik — hard-abort açmadan önce okuyun

`--enable-hard-abort`, otomatik bir hook'un çalışma ağacınızda sizin
onayınız olmadan `git stash` çalıştırmasına izin verir. Bunu anladıktan
sonra bilerek açmak makul; ama az önce plugin'i kuran birinin **varsayılanı**
olması makul değil. Açmadan önce bilmeniz gerekenler:

- **"Madde başında temiz ağaç" varsayımına dayanır.** `git stash` çalışma
  ağacını son commit'e döndürür. Bunun doğru sonucu vermesi için her
  maddenin bitişinde gerçekten commit atılmış olması gerekir (araç zaten
  buna yönlendiriyor). Önceki madde commit'lenmediyse, stash onu da süpürür
  — kasıtlı değil, bilinen bir sınırlama.
- **Geri döndürülebilir ama otomatik değil.** `git stash` hiçbir şeyi silmez
  — `git stash list` / `git stash pop` ile bakılabilir — ama sizin için
  otomatik geri getirmez. Bu kasıtlı: otomatik bir "geri alma"nın kendisi de
  sessizce yanlış bir şey yapabilirdi.
- **Git yoksa geri alma da yok.** Proje bir git deposu değilse, hard-abort
  sessizce temiz-kapanışa düşer — açmamış gibi davranır.
- **Önce atılabilir bir yerde deneyin.** Gerçek işe güvenmeden önce bir
  scratch repo'da.
- **Bir proje bunu sizin için sessizce açamaz.** `hard_abort_enabled`
  sadece SİZİN kontrol ettiğiniz bir yerden ayarlanabilir — `CC_HARD_ABORT`
  ortam değişkeni ya da plugin'in kendi yapılandırma ekranı. Bilerek
  `.cc-quota/config.json`'dan hiç okunmuyor, çünkü o dosya projenin içinde
  yaşıyor ve klonladığınız bir repoda zaten commit'lenmiş halde gelebilir.
  Sadece yüzde eşikleri proje tarafından ayarlanabilir — onlar sadece NE
  ZAMAN durduğunuzu değiştirir, yıkıcı bir şeyin olup olmayacağını değil.
- **Eski/bırakılmış bir `STOP.json` korumayı sonsuza kadar kapatamaz.** Hook,
  var olan bir `STOP.json`'ı sadece `resets_at` hâlâ gelecekteyken "zaten
  durduk, tekrar kontrol etme" olarak sayıyor. Süresi geçmiş, bozuk ya da
  klonlanan projeyle birlikte gelen biri yok sayılıp temizleniyor — korumayı
  sessizce kapatamıyor.
- **Uydurma bir `todos_state.json` tek başına stash tetikleyemez.**
  Hard-abort geri alması için bir `in_progress` iddiasına güvenmeden önce
  hook, `todos_state.json`'ın git tarafından takip edilip edilmediğini
  kontrol eder. Bu dosya yerel çalışma zamanı durumu olmalı (aşağıdaki
  `.gitignore` notuna bakın); *takip edilen* bir kopya, hook tarafından az
  önce yazılmadığının, repoyla birlikte geldiğinin işaretidir — bu durumda
  `in_progress` iddiası güvenilmeyip yok sayılır.
- **`.cc-quota` bir symlink (ya da klasör olmayan bir şey) olup bundan
  kurtulamaz.** Klonlanan bir repo `.cc-quota`'yı diskte başka bir yere
  symlink olarak commit'leyebilir (git'in symlink'leri varsayılan olarak
  gerçek symlink'e çevirdiği platformlarda) ve hook'un yaptığı her yazma
  projenin dışına, oraya gidebilirdi. Hook her yazmadan (ve kendi state
  dosyalarının her okumasından) önce bunu kontrol eder, symlink'i takip
  etmek yerine reddeder.
- **Yüzde eşikleri sınırlanır.** `session_soft` / `session_hard` /
  `weekly_soft` / `weekly_hard`, hangi kaynaktan gelirse gelsin (plugin config,
  `.cc-quota/config.json`, `CC_*` ortam değişkenleri), yalnızca `(0, 100]`
  aralığında kabul edilir. Bu, klonlanan bir repo'nun `config.json`'ının
  örneğin `session_hard: 99999` vererek korumayı etkisizleştirmesini, ya da
  `session_soft: 0` vererek her tool çağrısını bloklamasını engeller — ayrıca
  yanlış yazılmış bir ortam değişkeninin (ör. `CC_SESSION_SOFT=80%`)
  yok sayılmak yerine hook'u çökertmesini önler.
- **Kendi projenizin `.gitignore`'una `.cc-quota/` ekleyin.** Bu, yerel
  çalışma zamanı durumu (todo anlık görüntüleri, stop işaretçileri) —
  commit'lenecek bir şey değil; izlenmemesi ayrıca gerçek değişikliklerle
  birlikte `git stash`'e sürüklenmesini de önler ve yukarıdaki korumaların
  hiç devreye girmesine gerek bırakmaz.

## Gereksinimler

- `bash`, `python3`
- Claude Code CLI (`claude`), Pro/Max **aboneliği** (OAuth login) — bu aracın
  okuduğu usage endpoint'i sadece bu faturalandırma modunda var
- Geçerli bir OAuth token — `CLAUDE_CODE_OAUTH_TOKEN` ayarlıysa oradan,
  yoksa `~/.claude/.credentials.json`'dan (Claude Code'a giriş yapınca
  otomatik oluşur), yoksa macOS'a özel olarak, bu dosya yoksa sistem
  Keychain'inden (bkz. [Dürüst uyarılar](#dürüst-uyarılar) — Keychain yolu
  Claude Code'un kendi belgelenen davranışından uygulandı, gerçek bir
  Mac'te henüz doğrulanmadı).
- `--enable-hard-abort` kullanmayı planlıyorsanız git

### Bu araç, kullanım-başı ödeme (API key) faturalandırmasında hiçbir şey yapmaz

Claude Code'u OAuth abonelik girişi yerine `ANTHROPIC_API_KEY` ile (ya da
başka bir API-key tabanlı kurulumla) çalıştırıyorsanız, okunacak bir "5
saatlik session %"si ya da "haftalık %"si zaten yok — bu kavram tamamen
Pro/Max abonelik planlarına özgü. Hook bu durumda fail-open davranır: hata
vermez, hiçbir şeyi de bloklamaz, sessizce hiç tetiklenmez. Bu, "zayıflatılmış
bir koruma" değil, **sıfır koruma** demektir — araç şu an $ bazlı bir
harcama/bütçe takibi yapmıyor (kullanım-başı ödeme için asıl karşılığı bu
olurdu). Faturalandırma modunuz buysa ve koruma istiyorsanız lütfen bir
issue açın — bu, OAuth usage endpoint'i yerine farklı bir mekanizma
(belirlediğiniz bir bütçeye karşı token maliyetini takip etmek) gerektirir.

## Doğrulama / hata ayıklama

```bash
python3 scripts/usage.py          # okunan yüzdeler
python3 scripts/usage.py --probe  # API'nin HAM cevabı (alan adı doğrulama)
```

(Plugin kurulumunda `scripts/` yerine plugin'in kurulu olduğu yolu kullanın —
bir hook içinden `python3 "$CLAUDE_PLUGIN_ROOT/scripts/usage.py"`, ya da
cache yolunu `claude plugin list` ile bulun.)

`--probe` çıktısında `.five_hour.utilization` / `.seven_day.utilization`
görünmüyorsa Anthropic şemayı değiştirmiş olabilir — `scripts/usage.py`
içindeki `_normalize()` fonksiyonundaki alan adlarını güncelleyin.

## Dürüst uyarılar

- **API resmi değil.** `/api/oauth/usage` dokümante edilmemiş; Anthropic
  haber vermeden değiştirebilir/kaldırabilir. Hook **fail-open**: kotayı
  okuyamazsa Claude'u asla durdurmaz. API çökerse koruma sessizce devre dışı
  kalır.
- **Tespit anlık değil.** Hook artık her tool çağrısında tetiklendiği için
  (sadece `TodoWrite`'ta değil) gerçek gecikme kabaca `CC_USAGE_CACHE_TTL`
  kadardır (varsayılan 30sn) — "bir maddenin tamamı"ndan çok daha sıkı, ama
  bu pencere içindeki tek bir çok büyük tool çağrısı yine de bir eşiği
  aşabilir. Eşikleri %100'e değil biraz altına koyun.
- **Makine açık kalmalı.** Wrapper reset'e kadar uyur; bilgisayar
  uyursa/kapanırsa kendiliğinden devam etmez. *Makine açıkken yaşanan geçici
  bir ağ kesintisi* farklı bir durum ve artık ele alınıyor: bir round (ilk
  ya da devam) tamamen başarısız olur ya da `CC_ROUND_TIMEOUT` süresince
  (varsayılan 1sa — ör. tam devam etme anında ağ kesikse) hiç ilerleme
  kaydetmezse, `cc-run` sessizce pes etmek yerine `CC_MAX_RETRIES` sayısına
  kadar backoff ile yeniden dener.
- **Bağlam kaybına güvenmeyin.** `claude -c` konuşmayı geri getirir ama tam
  garanti değildir. Gerçek hafıza `progress.md` + git commit'leridir; kritik
  olan her şeyi oraya yazın.
- **`acceptEdits` + git izni uyarısı.** Wrapper varsayılan olarak
  `--permission-mode acceptEdits --allowedTools "Bash(git add:*),Bash(git commit:*),Bash(git status:*)"` ile çalışır
  (sormadan devam edebilsin *ve commit atabilsin* diye). Git izni, gerçek
  bir headless çalıştırmanın kalıcı olarak takılıp kalmasından sonra
  eklendi: `acceptEdits` tek başına dosya düzenlemelerini otomatik
  onaylıyor ama Bash tool çağrılarını **onaylamıyor**, ve `git commit`
  özellikle varsayılan olarak onay istiyor — headless bir çalıştırmada
  onaylayacak kimse olmadığı için Claude pes edip commit atmadan turu
  bitiriyordu, bu da aracın tüm ilerleme takibi mantığını sessizce
  geçersiz kılıyordu. Gerçek bir görevin ihtiyaç duyabileceği başka Bash
  komutları (test suite çalıştırmak, paket kurmak gibi) bu varsayımın
  **kapsamında değil**, aynı duvara çarpabilir — görevin bunlara headless
  ihtiyaç duyuyorsa `CC_CLAUDE_ARGS` ile kendin ekle (varsayılanın
  tamamının yerini alır, o yüzden `--permission-mode acceptEdits
  --allowedTools "Bash(git add:*),Bash(git commit:*),Bash(git status:*)" ...` kısmını da tekrar dahil et, ihtiyacın
  olan başka şeylerle birlikte). Hiçbiri kabul edilemezse `CC_CLAUDE_ARGS=""`
  ile kapatıp etkileşimli çalıştırın. Güncel bayraklar için `claude
  --help`'e bakın — zamanla değişebilir.
- **Plan.** Usage endpoint Pro/Max'te çalışır. Farklı bir planda `--probe`
  boş/farklı dönebilir.
- **macOS Keychain fallback'i uygulandı ama gerçek donanımda henüz
  doğrulanmadı.** `scripts/usage.py` sırasıyla `CLAUDE_CODE_OAUTH_TOKEN`'a,
  `~/.claude/.credentials.json`'a, sonra — macOS'ta, o dosya yoksa —
  `security find-generic-password -s "Claude Code-credentials" -w`
  komutuyla sistem Keychain'ine bakar. Servis adı ve genel davranış Claude
  Code'un kendi dokümantasyonundan ve herkese açık bug raporlarından
  doğrulandı, gerçek bir Mac'te çalıştırılarak değil (bu projede Mac yok).
  Kurulumunuzda çalışmazsa `python3 scripts/usage.py --probe` nerede
  başarısız olduğunu gösterir — lütfen o çıktıyla bir issue açın.

## Ayarlar

### Bir ortam değişkeni ayarlama

Aşağıdaki her `CC_*` değişkeni aynı şekilde ayarlanır — sözdizimi shell'e
göre değişir. `CC_SESSION_SOFT=80`'i herhangi biri için örnek olarak alalım:

**bash / zsh (Linux, macOS, Git Bash):**
```bash
export CC_SESSION_SOFT=80          # bu shell oturumunun geri kalanında geçerli kalır
CC_SESSION_SOFT=80 cc-run "..."    # ya da satır içi, sadece tek bir komut için
```

**PowerShell (Windows):**
```powershell
$env:CC_SESSION_SOFT = "80"
cc-run "..."
```
Bu sadece o anki PowerShell penceresi için geçerli. Kalıcı yapmak için
`$env:` satırını `$PROFILE`'ına ekle (yukarıdaki
[cc-run'ı kendi terminalinizden kullanma](#cc-runı-kendi-terminalinizden-kullanma)
bölümünde kullanılan dosyanın aynısı).

Eşikleri ayarlayabileceğiniz üç yer var, şu sırayla kontrol edilir —
**her biri altındakini ezer**:

1. **`CC_*` ortam değişkenleri** — açık, tek seferlik override (`cc-run`
   bayraklarını elle verdiğinizde ya da manuel `export` ile)
2. **`.cc-quota/config.json`** (proje bazlı) — `session_soft`,
   `session_hard`, `weekly_soft`, `weekly_hard`, `language` anahtarları
   (`hard_abort_enabled` **hariç** — bu anahtarın bu dosyadan bilerek
   dışlanma sebebi için
   [Güvenlik](#güvenlik--hard-abort-açmadan-önce-okuyun) bölümüne bakın)
3. **Plugin'in yapılandırma ekranı** (yukarıda) — 1 ya da 2'yi vermediğiniz
   her yerde geçerli olan kişisel varsayılanınız

Üçü de hiçbir yerde ayarlanmadıysa sabit varsayılan devreye girer:
80 / 95 / 97 / 98 / geri alma kapalı.

`hard_abort_enabled` ve `notify_url`, bu üç katmanlı sistemin iki istisnası
— ikisi de sadece **1** ya da **3**'ten okunabilir, `.cc-quota/config.json`'dan
asla — ikisi de güvenlikle ilgili çünkü (klonlanmış/güvenilmeyen bir
projenin config.json'ı ne otomatik `git stash`'i sessizce açabilmeli, ne de
session durumunu senin kontrolünde olmayan bir URL'e sessizce
yönlendirebilmeli). Bkz. [Güvenlik](#güvenlik--hard-abort-açmadan-önce-okuyun)
ve [Bildirimler](#bildirimler).

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `CC_SESSION_SOFT` | 80 | Session YUMUŞAK eşiği (%) — madde biter, sonra durur |
| `CC_SESSION_HARD` | 95 | Session SERT eşiği (%) — madde ortasında tetiklenebilir |
| `CC_WEEKLY_SOFT` | 97 | Haftalık YUMUŞAK eşik (%) — madde biter, sonra durur |
| `CC_WEEKLY_HARD` | 98 | Haftalık SERT eşik (%) — madde ortasında tetiklenebilir |
| `CC_HARD_ABORT` | (kapalı) | SERT eşikte otomatik geri almayı (`git stash`) açar. `cc-run --enable-hard-abort` ile aynı |
| `CC_NOTIFY_URL` | (kapalı) | Önemli anlarda push bildirimi için opsiyonel webhook URL'i — ntfy.sh'e karşı test edildi. `.cc-quota/config.json`'dan asla okunmaz. Bkz. [Bildirimler](#bildirimler) |
| `CC_NOTIFY_TIMEOUT` | 5 | Tek bir bildirim denemesinin alabileceği azami süre (sn) — her iki durumda da fail-open |
| `CC_USAGE_CACHE_TTL` | 30 | Kota cache süresi (sn) — aynı zamanda sert eşik tespit gecikmesi |
| `CC_RESUME_BUFFER` | 60 | Reset sonrası ek bekleme (sn) |
| `CC_CLAUDE_ARGS` | `--permission-mode acceptEdits` | `claude`'a geçilecek ekstra bayraklar |
| `CC_ROUND_TIMEOUT` | 3600 | Tek bir round'un (ilk ya da devam) hiç ilerleme kaydetmeden çalışabileceği azami süre (sn) — aşılırsa asılı kaldığı varsayılıp (ör. ağ kesildi) yeniden denenir. `0` sınırı kapatır. |
| `CC_MAX_RETRIES` | 5 | Bir round'un art arda kaç kez anormal biterse (sıfır olmayan çıkış, `CC_ROUND_TIMEOUT` dahil) `cc-run`'ın yeniden denemek yerine pes edeceği |
| `CC_RETRY_BACKOFF` | 30 | Denemeler arası temel saniye; her art arda başarısızlıkta doğrusal artar (N. deneme `N × CC_RETRY_BACKOFF` sn bekler) |
| `CC_LANG` | `en` | `en` ya da `tr` — `cc-run`'ın kendi terminal mesajlarının ve hook'un kullanıcıya gösterdiği durum satırının dili. Aşağıdaki [Dil / Language](#dil--language) bölümüne bakın. |

## Dil / Language

Birbirinden bağımsız iki şey yerelleştirilebilir:

- **`cc-run`'ın kendi terminal çıktısı** (başlıyor/devam ediyor/uyuyor/yeniden
  deniyor/bitti satırları). Öncelik: `CC_LANG` ortam değişkeni >
  `.cc-quota/config.json`'ın `"language"` anahtarı > `en`. `cc-run` Claude
  Code'un hook sistemi tarafından değil senin tarafından doğrudan çağrıldığı
  için plugin'in yapılandırma ekranındaki ayarı hiç görmez (o ortam değişkeni
  sadece hook çağrılarında set edilir) — `CC_LANG`'ı kendin ayarla, ya da
  `.cc-quota/config.json`'a `"language": "tr"` ekle.
- **Hook'un `systemMessage`'ı** (bir eşik Claude'u durdurduğunda Claude
  Code'un sana gösterdiği okunabilir satır). Öncelik: `CC_LANG` >
  `.cc-quota/config.json` > plugin'in yapılandırma ekranı
  (`--config language=tr`) > `en`.

**Claude'un kendisi bu ayardan bağımsız olarak her zaman İngilizce talimat
alır.** Sadece bir *insanın* okuduğu metin (`cc-run`'ın terminal satırları,
hook'un `systemMessage`'ı) çevriliyor — Claude'a ne yapacağını söyleyen
`reason` alanı bir model talimatı, senin doğrudan okuduğun bir şey değil, ve
bu projenin Claude'a yönelik talimatları sadece İngilizce test edilmiş
durumda.

### Yeni bir dil eklemek

Kullanıcıya yönelik her metin tek bir yerde yaşıyor: `locales/<kod>.json`
(her dil için bir dosya, ör. `locales/en.json`, `locales/tr.json`). Hem
`cc-run` hem de hook, `scripts/i18n.py` üzerinden aynı dosyaları okuyor —
yani çevrilecek tek bir katalog var, iki değil.

**Yeni bir dil eklemek için `locales/en.json`'ı `locales/<kod>.json` olarak
kopyalayıp değerleri çevir — hepsi bu, hiçbir yerde kod değişikliği
gerekmez.** Dil hemen seçilebilir hale gelir (`CC_LANG=<kod>`,
`.cc-quota/config.json`'ın `"language"` anahtarı, ya da plugin'in
yapılandırma ekranı). Birkaç not:

- Her `{yer_tutucuyu}` aynen koru; sadece etrafındaki metni çevir. Yer
  tutucular Python `str.format()` alanlarıdır, yani kelime sırası tamamen
  serbest — `{item}`'ı cümlenin gerektirdiği yere koyabilirsin.
- Kısmi bir çeviri sorun değil. Dosyanda olmayan herhangi bir anahtar (ya da
  kimsenin hiç dosya eklemediği bir kod) otomatik olarak İngilizce'ye düşer
  — bkz. `i18n.msg()`'in geri düşüş zinciri. Eksik bir anahtar yüzünden
  hiçbir şey çökmez.
- `bin/cc-run`'da python3'ün varlığı doğrulanmadan *önce* var olan dört
  önyükleme (bootstrap) hata mesajı var (görev verilmedi, görev dosyası
  bulunamadı, `claude`/`python3` bulunamadı) — bunlar `i18n.py`'ye
  ulaşamıyor, bkz. dosyanın başındaki `err()`. Bunlar sadece sabit
  İngilizce/Türkçe; bir `locales/` dosyası eklemek bu dört mesajı
  genişletmiyor (bu istisnai durumların ne kadar nadir olduğu düşünülürse
  kabul edilebilir, dar bir sınır).

## Kaldırma

Plugin kurulumu:

```
/plugin uninstall quota-guard@quota-guard
```

Manuel kurulum:

```bash
rm -rf ~/.claude/cc-quota-guard ~/.claude/.cc-quota-cache.json ~/.local/bin/cc-run
# settings.json'dan quota_gate.py girdilerini (PostToolUse, UserPromptSubmit, Stop, StopFailure) elle sil
```

## Katkı

Issue ve PR'lar açık — kurulum, test suite'ini çalıştırma ve kota tespiti/
geri alma mantığına dokunan bir PR'dan beklenen tasarım ilkeleri
("fail-open, yıkıcı olan her şeye opt-in, sınırlamayı belgele") için
[CONTRIBUTING.md](../CONTRIBUTING.md)'ye bakın (İngilizce). Neyin zaten
yayınlandığı için [CHANGELOG.md](CHANGELOG.md)'ye bakın.

## Lisans

MIT — bkz. [LICENSE](../LICENSE).
