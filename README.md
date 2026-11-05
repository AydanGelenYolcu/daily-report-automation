# Daily Report Automation

**Language / Dil:** Türkçe | [English](README.en.md)

### Netmera, GA4 ve tablo verilerini tek günlük raporda birleştirin.

**Daily Report Automation**, Netmera ve Google Analytics 4 (GA4) sonuçlarını mevcut yerel Excel dosyaları ve Google Sheets tablolarıyla bir araya getiren taşınabilir bir Windows raporlama uygulamasıdır. Günlük veriyi tarih sütununda veya başlık satırında doğru günle eşleştirin; farklı kaynaklardaki hücreleri formüllerle birleştirin; sonucu seçtiğiniz Excel dosyasına ya da Google Sheet'e yazın.

**Bir rapor akışında:** Netmera/GA4 verisini alın → ilgili tarihi satır veya başlıkta bulun → Excel ya da Google Sheets'ten ek değerleri okuyun → toplama, çıkarma, çarpma, bölme veya ortalama hesabı yapın → sonucu yerel Excel'e veya Google Sheets'e aktarın. Tarih aralığını gün gün çalıştırabilir, akışı belirli gün ve saatlerde zamanlayabilirsiniz.

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Windows-0078D4)
![Version](https://img.shields.io/badge/version-0.19.5-167D64)
![Demo](https://img.shields.io/badge/screenshots-synthetic_data-7257D5)

<p align="center"><img src="docs/media/demo.gif" alt="Sentetik verilerle proje, iş listesi, sonuçlar ve hesaplama editörü" width="100%"></p>

> Görseller ve örnek işler tamamen sentetiktir. Depo kişisel proje ayarı, şirket raporu, OAuth istemcisi, erişim belirteci veya giriş bilgisi içermez. Netmera ile resmî bir bağlantısı olmayan bağımsız bir projedir.

## Neler yapar?

| İşlem | Kullanım |
|---|---|
| **Netmera raporları** | Event Insight, People, Tag ve Segment toplamlarını okuma |
| **Segment hesaplama** | Mevcut kuralları kullanma, bağlı tarih kutularını uygun sırada değiştirme, hesaplamayı bekleme |
| **GA4 raporları** | Metrik, kırılım, filtre ve tarih tanımıyla rapor işleri oluşturma |
| **Birden fazla veri kaynağı** | Rapor sonuçlarını mevcut `.xlsx` dosyası veya Google Sheets hücrelerinden okunan değerlerle birleştirme |
| **Tarih ve satır eşleştirme** | Tarih sütununda doğru satırı veya yatay başlıklarda doğru günü bulup hedef hücreyi belirleme |
| **Hesaplamalar** | Netmera/GA4 sonuçları ile Excel/Sheets hücrelerini `A + B`, `A - B`, `A * B`, `A / B` veya `(A + B) / 2` şeklinde hesaplama |
| **Excel / Sheets çıktısı** | Sonuçları mevcut yerel Excel dosyasına veya Google Sheets'e sabit hücre, tarih eşlemesi ya da günlük kayma ile yazma |
| **Gün gün çalışma** | Tarih aralığındaki günleri ayrı çekme ve kendi hücrelerine yazma |
| **Zamanlama** | Windows Görev Zamanlayıcı üzerinden seçilen gün/saatlerde çalıştırma |
| **Proje aktarımı** | İşleri, formülleri, tablo bağlantılarını, hücre ve GA4 mülk eşlemelerini JSON ile taşıma |

### Yerel Excel ve Google Sheets ile günlük eşleme

Mevcut `.xlsx` dosyasını veya Google Sheets sayfasını **kaynak** olarak seçip bir hücreyi sabit adresinden okuyabilir ya da çalışılan tarihin bulunduğu satır/sütunu otomatik aratabilirsiniz. Örneğin A sütununda tarih, C sütununda değer varsa uygulama hedef günü A sütununda bulur ve aynı satırdaki C değerini hesaplamada kullanır. Tarihler yatay başlıklardaysa başlık satırında günü bulup ilgili sütundaki değeri okur.

**Örnek:** Günlük segment sonucu `25`, aynı tarihin Excel satırındaki C hücresi `5` ise `A + B` sonucu `30` olur. `A` ile `B` ikiye bölünürse sonuç `15` olur. Hesaplama sonucunu projenin seçili çıktı hedefi olan yerel Excel dosyasına veya Google Sheets'e tarih eşlemesiyle yazabilirsiniz. Yerel Excel için dosyayı seçin; Google Sheets için uygulamada Google bağlantısını kurun. Ayrıntılar [Google bağlantısı](GOOGLE_SHEETS.md) rehberinde.

### Portable Windows paketi

Depo, uygulamayı ve sabitlenmiş bağımlılıkları gömülü Python ile tek bir taşınabilir Windows ZIP paketine toplar. Hedef bilgisayarda Python kurmak gerekmez: ZIP'i açıp `NetmeraOtomasyonu.cmd` dosyasını çalıştırın. Netmera paneli otomasyonu için bilgisayarda Chrome veya Edge kurulu olmalıdır. Paket oluşturma komutu [geliştirme bölümünde](#geliştirme-ve-doğrulama) yer alır.

## Uygulama içinde

<p><img src="docs/media/projects.png" alt="Demo proje kartları" width="49%"> <img src="docs/media/jobs.png" alt="İş listesi ve iş türleri" width="49%"></p>
<p><img src="docs/media/results.png" alt="Sentetik sonuçlar ve çalışma akışı" width="49%"> <img src="docs/media/formula.png" alt="Hesaplama işinin formül editörü" width="49%"></p>

**Örnek akış:** Segment sonucu `A = 25` olsun. Başka bir tablonun tarih sütununda ilgili günü bulup aynı satırdaki `B = 5` değerini alın. `A + B` ifadesinin sonucu `30`; `(A + B) / 2` ifadesinin sonucu `15` olarak hedef hücreye yazılır. Boş, bulunamayan veya sayı olmayan kaynak hücrede hesaplama hata verir.

## Hızlı başlangıç

Geliştirme için Python 3.12 ve Google Chrome veya Microsoft Edge kullanılır. PowerShell:

```powershell
git clone https://github.com/AydanGelenYolcu/daily-report-automation.git
cd daily-report-automation
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\NetmeraOtomasyonu.cmd
```

1. **Demo (sahte veri)** projesini açın.
2. İşleri inceleyin ve **Dene / Test et** ile sentetik sonuçları görün.
3. Kendi projenizi oluşturup rapor tanımı, kaynak bağlantısı ve çıktı eşlemelerini seçin.
4. Gerçek kaynakta küçük bir raporu panelle karşılaştırdıktan sonra zamanlamayı kurun.

Uygulama arayüzü `127.0.0.1` üzerinde açılır. Netmera için ayrı otomasyon tarayıcı profili kullanılır; Google bağlantısı uygulama içinden kurulur. Docker gerekmez.

### Çalıştırma seçenekleri

```powershell
# Yerel arayüz
.\.venv\Scripts\python.exe -m netmera_automation ui

# Projeleri listele
.\.venv\Scripts\python.exe -m netmera_automation profiller

# Sentetik demo çalışması
.\.venv\Scripts\python.exe -m netmera_automation run --profile demo-sahte-veri
```

## Nasıl çalışır?

```mermaid
flowchart LR
    UI["Yerel arayüz"] --> Jobs["İş tanımı ve doğrulama"]
    Jobs --> Netmera["Netmera / Playwright"]
    Jobs --> GA4["GA4 rapor istemcisi"]
    Netmera --> Calc["Sonuçlar ve hesaplamalar"]
    GA4 --> Calc
    Cells["Excel / Sheets kaynak hücreleri"] --> Calc
    Calc --> Map["Tarih / hücre eşlemesi"]
    Map --> Excel["Yerel Excel"]
    Map --> Sheets["Google Sheets"]
```

| Dosya / klasör | Sorumluluk |
|---|---|
| `netmera_automation/server.py` | Yerel HTTP arayüzü ve arka plan görevleri |
| `jobs.py`, `runner.py` | Tanımlar, doğrulama ve çalışma sırası |
| `panel.py`, `ga4/` | Kaynaklardan veri okuma |
| `formula.py`, `cell_mapping.py` | Hesaplamalar ve çıktı yerleşimi |
| `ui/src/`, `tools/build_ui.py` | Arayüz kaynakları ve birleşik HTML üretimi |
| `tests/` | Sentetik veri ve sahte panellerle doğrulama |

## Teknolojiler, diller ve API'ler

GitHub kaynak dosyalarına göre dilleri otomatik olarak **Python, HTML ve Batchfile** gösterir. Arayüzde HTML içine gömülü CSS ve JavaScript de kullanılır.

| Alan | Teknoloji / API | Kullanım |
|---|---|---|
| Uygulama | Python 3.12 | Rapor işleri, zamanlama, formül ve veri aktarım akışı |
| Arayüz | HTML, CSS, JavaScript | Yerel web arayüzü; `127.0.0.1` üzerinde çalışır |
| Netmera | Playwright + bilgisayardaki Chrome/Edge | Netmera panelini tarayıcı üzerinden kullanıp rapor sonuçlarını okur; Netmera REST API'si kullandığı iddia edilmez |
| Google Analytics 4 | Google Analytics Data API v1beta | GA4 rapor verilerini salt okunur sorgular |
| GA4 mülkleri | Google Analytics Admin API v1beta | Erişilebilen hesap ve mülk özetlerini listeler |
| Google Sheets | Google Sheets API v4 | Kaynak hücrelerini okur ve eşlenen rapor hücrelerine yazar |
| Google bağlantısı | OAuth 2.0 masaüstü akışı | Kullanıcı hesabı üzerinden Analytics ve Sheets erişimi sağlar; oturum Windows DPAPI ile yerelde korunur |
| Yerel tablolar | `openpyxl`, `.xlsx` | Excel hücrelerini kaynak olarak okur ve seçilen çalışma kitabına çıktı yazar |
| Taşınabilir başlatıcı | Windows Batchfile (`.cmd`) | Gömülü Python içeren ZIP paketinden uygulamayı başlatır |

## Kararlılık ve veri güvenliği

- Kalabalık iş listeleri sayfalanır; arama tüm işlerde çalışır.
- Eski proje istekleri yeni seçimi geri çeviremez. Tek iş anahtarı yalnız o işi kaydeder.
- Durum yoklaması tam raporu tekrar tekrar taşımaz; büyük sonuçlar ayrıca yüklenir.
- Açılışta yeni GA4 rapor işi otomatik eklenmez; kullanıcı güncelleme düğmesiyle başlatır.
- Formüller izin verilen ifade ağacıyla değerlendirilir; serbest kod çalıştırılmaz.
- Giriş bilgileri Windows DPAPI ile kullanıcı hesabına bağlı saklanır. Oturumlar ve kullanıcı raporları Git'e girmez.

[Google bağlantısı](GOOGLE_SHEETS.md) · [Güvenlik ve gizlilik](SECURITY.md)

## Geliştirme ve doğrulama

```powershell
# Arayüz kaynakları değişince
.\.venv\Scripts\python.exe tools\build_ui.py

# Aktarım, kararlılık ve hesaplama testleri
.\.venv\Scripts\python.exe -m unittest tests.test_project_transfer tests.test_ui_stability tests.test_formula

# Taşınabilir Windows ZIP'i üret
.\.venv\Scripts\python.exe tools\build_portable.py
```

Tarayıcı testleri kurulu Chrome'u kullanır; sahte panel istekleri yerelde karşılanır. Demo ve test sonuçları canlı Netmera/Google erişiminin doğrulandığı anlamına gelmez. Panel arayüzü veya hesap izinleri değiştiğinde ilgili okuma adımlarının yeniden kontrol edilmesi gerekir.
