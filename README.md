# 🗞 AI Bülteni

AI dünyasını her gün tarayıp **okurun kendi profiline göre kişiselleştirilmiş, Türkçe bir bülten** hazırlayan
ve Telegram'dan gönderen bot. Tamamen ücretsiz servislerle çalışabilir (GitHub Actions + Groq + Gemini ücretsiz kotaları).

```
topla → tekilleştir → ön filtre (anahtar kelime + embedding) → LLM puanla → LLM yaz → Telegram
```

Bültenin bölümleri (ör. "Senin Alanın", "AI'da Önemli Gelişmeler") **gizli profil dosyasında** tanımlanır;
repoda sadece genel örnekler durur. Bkz. [Kişiselleştirme ve gizlilik](#kişiselleştirme-ve-gizlilik).

## Nasıl çalışır?

| Adım | Ne yapar | Nerede |
|---|---|---|
| Topla | arXiv, HF Daily Papers, lab blogları, bültenler, Hacker News, Reddit, GitHub Trending + profildeki kişisel kaynaklar | `src/bulten/collectors/`, `config/sources.yaml` |
| Tekilleştir | Aynı makale arXiv + HF + HN'de çıksa bile tek öğe; daha önce görülen/gönderilen her şey elenir | `src/bulten/dedup.py`, `src/bulten/semantic.py` |
| Ön filtre | Yerel çok dilli embedding modeli + anahtar kelimeler + popülerlik → ~1000 öğeden ~70 aday | `src/bulten/prefilter.py` |
| Puanla | LLM her adayı 0–10 puanlar ve bölüme atar (varsayılan: Groq gpt-oss-120b) | `src/bulten/scorer.py`, `prompts/score.md` |
| Yaz | LLM seçilen haberlerden akıcı Türkçe bülten yazar (varsayılan: Gemini Flash) | `src/bulten/writer.py`, `prompts/write.md` |
| Gönder | Telegram'a HTML biçiminde, gerekirse birkaç mesaja bölerek | `src/bulten/telegram.py` |

**Dayanıklılık:** Bir kaynak çökerse diğerleriyle devam eder. Bir LLM hata verir ya da kotası dolarsa
zincirdeki sonraki modele geçer. Yazım tamamen başarısız olursa sade bir liste gönderilir. Telegram'a
gönderim başarısız olursa durum kaydedilmez, haberler ertesi gün kaybolmaz.

## Kurulum (bir kerelik, ~15 dk)

### 1. Telegram botu
1. Telegram'da **@BotFather**'a yaz → `/newbot` → isim ver → sana bir **token** verir.
2. Oluşan botunla sohbeti aç ve `/start` yaz.
3. Chat ID'ni öğren: `TELEGRAM_BOT_TOKEN=<token> python -m bulten.telegram`

### 2. LLM anahtarları (ücretsiz)
- **Groq:** https://console.groq.com/keys
- **Gemini:** https://aistudio.google.com/apikey
- *(Opsiyonel)* **OpenRouter:** https://openrouter.ai/keys — üçüncü yedek

> Ücretsiz kotalar değişebilir; bir model kotadan çıkarsa `config/settings.yaml → llm.profiles` içinden model adını değiştir.

### 3. Kişisel dosyalar
```bash
cp config/context.example.md config/context.md      # kim olduğun, ne iş yaptığın, ne görmek istediğin
cp config/profile.example.yaml config/profile.yaml  # bülten bölümlerin ve kişisel kaynakların
```
İkisi de `.gitignore`'dadır, repoya girmez.

### 4. GitHub secrets
```bash
gh secret set CONTEXT_MD < config/context.md
gh secret set PROFILE_YAML < config/profile.yaml
gh secret set GROQ_API_KEY        # değeri sorar
gh secret set GEMINI_API_KEY
gh secret set TELEGRAM_BOT_TOKEN
gh secret set TELEGRAM_CHAT_ID
```

### 5. İlk çalıştırma
**Actions → Günlük AI Bülteni → Run workflow** → `dry_run` işaretli çalıştır: bülten Telegram'a
"🧪 ÖNİZLEME" olarak gelir, durum kaydedilmez. Beğendiysen bundan sonra bülten her gece kendiliğinden
hazırlanır ve en geç sabah Telegram'da olur (gece gelirse bildirim sesi çalmaz).

> İlk çalışmada RSS'i olmayan sayfalardaki mevcut linkler "görüldü" olarak kaydedilir; bu kaynaklardan
> haberler ertesi günden itibaren gelir.

## Yerelde çalıştırma

```bash
uv venv && uv pip install -e ".[dev]"
cp .env.example .env                        # anahtarları doldur

python -m bulten --collect-only             # sadece kaynakları + ön filtreyi test et (LLM yok)
python -m bulten --dry-run                  # tam bülten üret, ekrana yaz, Telegram'a gönderme
python -m bulten                            # gerçek çalışma
python -m bulten.flash --dry-run            # flaş haber kontrolü
python -m bulten.compare                    # LLM profillerini karşılaştır
pytest                                      # testler
```

## Kişiselleştirme ve gizlilik

Repo herkese açık olabilsin diye kişiye özel her şey koddan ayrıdır:

| Ne | Nerede | Repoda mı? |
|---|---|---|
| Okur profili (iş, ilgi alanları, üslup) | `config/context.md` / `CONTEXT_MD` secret'ı | ❌ |
| Bölümler, kişisel kaynaklar, flaş kaynakları, ek anahtar kelimeler | `config/profile.yaml` / `PROFILE_YAML` secret'ı | ❌ |
| Genel ayarlar, genel kaynaklar, örnek bölümler | `config/settings.yaml`, `config/sources.yaml` | ✅ |

`profile.yaml`, `settings.yaml` ile aynı yapıdadır ve onun üzerine yazılır (listeler tamamen değiştirilir);
`extra_sources:` ile kaynak eklenir. Örnek: [`config/profile.example.yaml`](config/profile.example.yaml).
Profili değiştirince secret'ı da güncelle: `gh secret set PROFILE_YAML < config/profile.yaml`

Ayrıca:
- **Durum dosyaları** (`data/*.json`) okunabilir bilgi içermez: linkler, başlıklar ve kaynak adları tek yönlü
  hash'lenir; gönderilen haberler için başlık yerine sadece embedding vektörü saklanır.
- **Actions logları** public repoda herkese açıktır: profildeki kaynak adları ve bölüm başlıkları loglarda `***`
  olarak maskelenir; dry-run ve karşılaştırma çıktıları loga/artifact'e değil Telegram'a gönderilir.
- **Arşiv** (taranan tüm haberler + puanlar + bültenler) bot reposuna değil, ayrı bir **gizli** repoya yazılır.

## 🗄 Arşiv (opsiyonel)

Her gece o gün ilk kez görülen **tüm öğeler** (başlık, link, kaynak, tarih, özet, popülerlik, ön filtre puanı,
adaylarda LLM puanı/bölümü/gerekçesi, gönderildi mi) ve **günün bülteni** ayrı bir **gizli** repoya eklenir:

```
veri/YYYY/MM/YYYY-MM-DD.jsonl.gz    bultenler/YYYY/YYYY-MM-DD.md
```

Kurulum: `<kullanıcı>/ai-bulten-arsiv` adında **private** bir repo aç, sonra sadece o repoya yazabilen bir
deploy key oluşturup bu reponun secret'ı yap (anahtar ekrana basılmaz, iş bitince silinir):

```bash
ssh-keygen -q -t ed25519 -N "" -C ai-bulten-arsiv -f /tmp/arsiv_key && gh repo deploy-key add /tmp/arsiv_key.pub --repo <kullanıcı>/ai-bulten-arsiv --allow-write --title "ai-bulten bot" && gh secret set ARCHIVE_DEPLOY_KEY --repo <kullanıcı>/ai-bulten < /tmp/arsiv_key; rm -f /tmp/arsiv_key /tmp/arsiv_key.pub
```

Farklı bir repo adı için `ARCHIVE_REPO` değişkenini (*Actions → Variables*) `kullanıcı/repo` olarak ayarla.
`ARCHIVE_DEPLOY_KEY` yoksa arşiv adımı sessizce atlanır. Veriyi okumak için arşiv reposunun README'sine bak.

## ⚡ Flaş haberler

Günlük bülten gece hazırlanır; ama büyük bir gelişme (yeni öncü model, okurun alanında çığır açan bir açık model,
sektörü sarsan bir olay) gün içinde çıkarsa sabahı beklemezsin. **Flaş Haber Kontrolü** workflow'u her saat:

1. Sadece hızlı ve önemli kaynaklara bakar (lab blogları, 250+ puanlı HN, 100+ upvote'lu HF makaleleri, profilde eklenenler),
2. Günlük bültende ya da daha önce flaşta gönderilmiş haberleri (farklı başlıkla olsa bile) eler,
3. LLM'e çok katı bir ölçütle sorar; sadece 9/10 ve üzeri haberler `⚡ FLAŞ` olarak hemen gönderilir,
4. Günde en fazla 3 flaş; gece saatlerinde sessiz bildirim. Flaşta giden haber o geceki bültende tekrar etmez.

Ayarlar: `settings.yaml → flash`, ölçütler: `prompts/flash.md`.

> GitHub zamanlanmış çalışmaları yoğunlukta geciktirebilir, hatta atlayabilir (gözlenen: 40 saatte 40 yerine 8 çalışma).
> Dakikası dakikasına kontrol için aşağıdaki harici zamanlayıcıyı kur; GitHub'ın kendi zamanlaması yedek olarak kalır.

### Harici zamanlayıcı (opsiyonel, önerilir)

[cron-job.org](https://cron-job.org) (ücretsiz) flaş workflow'unu GitHub API'si üzerinden tetikler.

1. **Sadece bu repoyu tetikleyebilen bir token oluştur:** GitHub → *Settings → Developer settings →
   Fine-grained tokens → Generate new token*
   - *Repository access:* **Only select repositories** → bu repo
   - *Permissions → Repository permissions → Actions:* **Read and write** (başka izin verme)
   - *Expiration:* en fazla 1 yıl — bitmeden yenilemeyi unutma
2. **cron-job.org'da bir cronjob oluştur:**
   - *URL:* `https://api.github.com/repos/<kullanıcı>/<repo>/actions/workflows/flash.yml/dispatches`
   - *Zamanlama:* her 30 dakikada bir
   - *Advanced → Request method:* `POST`
   - *Advanced → Headers:*
     `Authorization: Bearer <token>` · `Accept: application/vnd.github+json` · `X-GitHub-Api-Version: 2026-03-10`
   - *Advanced → Request body:* `{"ref": "main"}`
   - Başarısız çalıştırmalar için e-posta bildirimini aç
3. **Test et:** cron-job.org'daki "Test run" 200 (ya da 204) dönmeli ve *Actions* sekmesinde yeni bir
   "Flaş Haber Kontrolü" çalıştırması (tetikleyici: `workflow_dispatch`) görünmeli.

> Token yalnızca bu reponun workflow'larını tetikleyebilir/iptal edebilir; koda veya secret'lara erişemez.
> Token sızarsa GitHub'dan hemen iptal et (*Fine-grained tokens → Revoke*).

## Tekrar eden haberler

Aynı haber farklı başlıklarla birçok kaynakta çıkabilir. Başlıklar embedding modeliyle karşılaştırılır
(benzerlik ≥ 0.86, gerçek veriyle ölçülerek seçildi) ve iki güvenlik kuralıyla birleştirilir: başlıklardaki
sürüm numaraları çelişiyorsa (Gemini 3.8 ↔ 3.7) ya da iki öğe aynı blogdan geliyorsa birleştirilmez.
Son 7 günde gönderilmiş bir haberin farklı başlıklı tekrarı da atlanır.

## LLM profilleri ve karşılaştırma

| Profil | Puanlama | Yazım | Maliyet |
|---|---|---|---|
| `free` (varsayılan) | Groq gpt-oss-120b | Gemini Flash | Ücretsiz |
| `hybrid` | Groq (ücretsiz) | Claude Sonnet 5 | ~2$/ay |
| `claude_sonnet` | Claude Haiku 4.5 | Claude Sonnet 5 | ~3$/ay |
| `claude` | Claude Haiku 4.5 | Claude Opus 5 | ~7$/ay |

> Claude API, claude.ai aboneliğinden **ayrı ve ücretlidir** (https://console.anthropic.com → `ANTHROPIC_API_KEY`
> secret'ı). Anahtar yoksa Claude hiç kullanılmaz.

**Profil seçimi:** *Settings → Secrets and variables → Actions → Variables* altında `LLM_PROFILE` (ör. `hybrid`);
tek seferlik denemek için "Run workflow" ekranındaki `profile` seçeneği.

**Karşılaştırma:** *Actions → LLM Profil Karşılaştırması → Run workflow*. Tüm profiller aynı haber listesini
alır; rapor (süre, token, maliyet, puanlama uyumu, seçilen haberler) ve her profilin bülteni Telegram'a gelir.

## Notlar
- GitHub, 60 gün boyunca hiç commit almayan repolarda zamanlanmış workflow'ları durdurur; bot her gün durum
  dosyasını commit ettiği için sorun olmaz.
- Reddit, GitHub Actions IP'lerini sık sık engelliyor; bu kaynaklar `optional: true` olduğu için sessizce atlanır.
