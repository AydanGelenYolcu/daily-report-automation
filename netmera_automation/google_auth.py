"""Sheets erişimi: GA4'ün ortak OAuth istemcisi ve Windows korumalı oturumu kullanılır."""
import base64
from datetime import datetime
import json
import os
from pathlib import Path
import re
import threading
import time
import urllib.request
import urllib.error
from urllib.parse import urlencode, urlsplit, parse_qs

from . import store, login_config
from .paths import APP_ROOT

SCOPE='https://www.googleapis.com/auth/spreadsheets'
AUTH_URL='https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL='https://oauth2.googleapis.com/token'
LOCK=threading.RLock()


class GoogleError(ValueError):
    def __init__(self,message,reason=None):
        super().__init__(message);self.reason=reason


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise GoogleError('Google isteği yönlendirildi; erişim bilgisi başka adrese gönderilmedi.')


EXPIRED=('Google bağlantısının süresi doldu veya izin geri alındı. Google bağlantısı → '
         '“Google hesabımla bağlan” ile yeniden bağlan. OAuth uygulaması Testing durumundaysa bu her 7 günde bir olur.')
# Google'ın herkese açık hata kodları → kullanıcının yapacağı somut adım. Ham yanıt metni kaydedilmez.
REASONS={
    'invalid_grant':EXPIRED,
    'invalid_client':'Google OAuth istemcisi tanınmadı (Google Cloud’da silinmiş olabilir). Desktop app JSON dosyasını yeniden indirip seç.',
    'unauthorized_client':'Bu OAuth istemcisi masaüstü girişi için yetkili değil. Google Cloud’da “Desktop app” türünde yeni istemci oluşturup JSON’unu seç.',
    'SERVICE_DISABLED':'Google Sheets API bu Google Cloud projesinde kapalı. Kurulum rehberi Adım 2: APIs & Services → Library → Google Sheets API → Enable. Açtıktan sonra birkaç dakika bekleyip yeniden dene.',
    'accessNotConfigured':'Google Sheets API bu Google Cloud projesinde kapalı. Kurulum rehberi Adım 2: APIs & Services → Library → Google Sheets API → Enable.',
    'ACCESS_TOKEN_SCOPE_INSUFFICIENT':'Google Sheets izni verilmemiş. Hesabı yeniden bağla ve izin ekranında Sheets kutusunu işaretle.',
    'FAILED_PRECONDITION':'Google bu dosyada işleme izin vermedi. Dosya büyük olasılıkla Drive’daki bir Excel (.xlsx) dosyası: Sheets’te adın yanında “.XLSX” görünür. '
                          'Dosya → Google E-Tablolar olarak kaydet, oluşan yeni dosyanın bağlantısını Ayarlar’a yapıştır.',
    'redirect_uri_mismatch':'OAuth istemcisi masaüstü uygulaması türünde değil. Google Cloud’da “Desktop app” istemcisi oluşturup JSON’unu seç.',
    'invalid_request':'Google giriş isteğini reddetti. “Google ile bağlan”ı yeniden dene; tekrarlarsa Desktop app JSON dosyasını yeniden indirip seç.',
    'RATE_LIMIT_EXCEEDED':'Google kota sınırı; otomatik istek tekrarı yapılmadı. Birkaç dakika sonra yeniden dene.',
}


def _reason(exc):
    """HTTP hata gövdesinden yalnız Google'ın kısa hata kodunu çıkarır."""
    try:
        raw=exc.read(65537)
        if len(raw)>65536:return None
        body=json.loads(raw)
    except Exception:return None
    error=body.get('error') if isinstance(body,dict) else None
    if isinstance(error,str):return error[:60]
    if isinstance(error,dict):
        for detail in error.get('details') or []:
            if isinstance(detail,dict) and isinstance(detail.get('reason'),str):return detail['reason'][:60]
        for item in error.get('errors') or []:
            if isinstance(item,dict) and isinstance(item.get('reason'),str):return item['reason'][:60]
        if isinstance(error.get('status'),str):return error['status'][:60]
    return None


def request_json(url,*,method='GET',body=None,token=None,form=False):
    parsed=urlsplit(url)
    if parsed.scheme!='https' or parsed.netloc not in ('oauth2.googleapis.com','sheets.googleapis.com') or parsed.username or parsed.fragment:
        raise GoogleError('Google bağlantı adresi geçersiz.')
    headers={'Accept':'application/json'}
    if token:headers['Authorization']='Bearer '+token
    data=None
    if body is not None:
        data=urlencode(body).encode() if form else json.dumps(body,allow_nan=False).encode()
        headers['Content-Type']='application/x-www-form-urlencoded' if form else 'application/json'
    try:
        req=urllib.request.Request(url,data=data,headers=headers,method=method)
        with urllib.request.build_opener(NoRedirect()).open(req,timeout=25) as response:
            raw=response.read(4_000_001)
        if len(raw)>4_000_000:raise GoogleError('Google yanıtı boyut sınırını aşıyor; daha küçük hedef kullanın.')
        result=json.loads(raw)
        if not isinstance(result,dict):raise GoogleError('Google yanıtı beklenen yapıda değil.')
        if 'error' in result:raise GoogleError('Google işlemi reddetti; ham hata yanıtı kaydedilmedi.')
        return result
    except GoogleError:raise
    except urllib.error.HTTPError as exc:
        reason=_reason(exc)
        messages={400:'Google isteği reddetti. OAuth kurulumu veya hedef aralığı geçersiz olabilir; bağlantıyı/ayarı kontrol edin.',
                  401:'Google oturumu geçersiz. Google hesabını yeniden bağlayın.',
                  403:'Google erişimi reddedildi. Bağlı Google hesabının hedef dosyada “Düzenleyen” yetkisi olduğunu kontrol et; '
                      'yetki varsa engel kurum politikasından gelebilir, Google yöneticine danış.',
                  404:'Google Sheets dosyası bulunamadı. Dosya bağlantısını ve dosyanın bağlı Google hesabıyla paylaşıldığını kontrol et.',
                  429:REASONS['RATE_LIMIT_EXCEEDED']}
        message=REASONS.get(reason) or messages.get(exc.code,'Google isteği başarısız oldu. Yazım durumu belirsizse hedefi kontrol edip aynı rapor gününü yeniden deneyin.')
        if reason and reason not in REASONS and re.fullmatch(r'[A-Za-z_]{2,60}',reason):
            message+=f' (Google kodu: {reason})'  # yalnız kısa genel kod; ham yanıt yazılmaz
        raise GoogleError(message,reason) from None
    except Exception:
        raise GoogleError('Google bağlantısı kurulamadı veya yanıt okunamadı. Ham yanıt ve erişim bilgisi kaydedilmedi.') from None


def _path(pid):
    return login_config.config_path(pid).with_name('google-sheets-korumali.json')


def _read(pid):
    path=_path(pid)
    if not path.exists():return None
    try:
        if path.stat().st_size>65536:raise ValueError()
        raw=store.read_json(path)
        if not isinstance(raw,dict) or set(raw)!={'version','protected'} or raw['version']!=1:raise ValueError()
        value=json.loads(login_config._dpapi(base64.b64decode(raw['protected'],validate=True),'google:'+pid,decrypt=True))
        if value.get('profile_id')!=pid or value.get('kind')!='google-sheets':raise ValueError()
        return value
    except Exception:
        raise GoogleError('Google kaydı bu Windows hesabında açılamadı. Bağlantıyı temizleyip yerelde yeniden kurun.') from None


def _save(pid,value):
    try:
        blob=login_config._dpapi(json.dumps({**value,'kind':'google-sheets','profile_id':pid}).encode(),'google:'+pid)
        path=_path(pid);path.parent.mkdir(parents=True,exist_ok=True)
        store.write_json(path,{'version':1,'protected':base64.b64encode(blob).decode()})
    except Exception:
        raise GoogleError('Google bilgileri Windows korumasıyla kaydedilemedi; düz metin kayıt yapılmadı.') from None


def _shared():
    """Ortak Google ayarı (GA4 ile aynı istemci JSON'u ve giriş); okunamazsa (None, hata metni)."""
    from . import google_shared
    from .ga4.istemci import ClientError
    try:return google_shared.status(),None
    except ClientError as exc:return None,str(exc)


def status(pid):
    """Google bağlantısı tektir (kullanıcı kararı 2026-10-28): istemci JSON'u ve Google girişi GA4 ile ortaktır.
    Eski projeye özel oturumlar kullanılmaz; eksik izinde başka hesaba geçilmez."""
    base={'configured':False,'connected':False,'expired':False,'team':False,'own_client':False,'shared':False,'account':None,'shared_client':False}
    try:
        try:team=bool(team_client());team_error=None
        except GoogleError as exc:team,team_error=False,str(exc)
        shared,shared_error=_shared()
        shared=shared or {}
        # GA4 hesabı bütün projeler için tek kaynaktır.
        via_shared=bool(shared.get('connected') and shared.get('sheets'))
        connected=via_shared
        expired=bool(not connected and shared.get('expired'))
        configured=bool(shared.get('client')) or team
        if via_shared:message=f'Ortak Google bağlantısı kullanılıyor ({shared.get("email") or "Google hesabı"}; GA4 ile aynı). Dosya erişimini “Dosya erişimini doğrula” ile kontrol edebilirsin.'
        elif shared.get('connected'):message='Ortak Google bağlantısında Google Sheets izni yok. Google bağlantısı → “Google hesabımla bağlan” ile yeniden bağlan ve izin ekranında E-Tablolar kutusunu işaretle.'
        elif expired:message=EXPIRED
        elif shared.get('client'):message='Ortak OAuth dosyası hazır (GA4 ile aynı). Google bağlantısı → “Google hesabımla bağlan” ile devam et.'
        elif team:message='Ekip istemcisi hazır; dosya seçmen gerekmez. Google bağlantısı → “Google hesabımla bağlan”a bas ve kendi hesabını seç.'
        elif shared_error or team_error:message=shared_error or team_error
        else:message='Google Cloud’dan indirdiğin OAuth JSON dosyasını seçerek başla (GA4 ve Google Sheets için tek dosya).'
        return {**base,'configured':configured,'connected':connected,'expired':expired,'team':team,'own_client':False,
                'shared':via_shared,'account':shared.get('email') if via_shared else None,'shared_client':bool(shared.get('client')),'message':message}
    except GoogleError as exc:return {**base,'message':str(exc)}


def _client(raw):
    try:
        if not isinstance(raw,dict) or 'installed' not in raw or 'web' in raw:raise ValueError()
        client=raw['installed'];cid=client['client_id'];secret=client['client_secret']
        if not isinstance(cid,str) or not cid.endswith('.apps.googleusercontent.com') or len(cid)>512:raise ValueError()
        if not isinstance(secret,str) or not 1<=len(secret)<=1024 or any(ord(c)<33 for c in secret):raise ValueError()
        # Google'ın indirme dosyalarında önceki URI yazımları da bulunur.
        # Bunlar yalnız doğrulanır; istekler daima yukarıdaki sabit güncel adreslere gider.
        if client.get('auth_uri',AUTH_URL) not in (AUTH_URL,'https://accounts.google.com/o/oauth2/auth'):raise ValueError()
        if client.get('token_uri',TOKEN_URL) not in (TOKEN_URL,'https://accounts.google.com/o/oauth2/token'):raise ValueError()
    except (ValueError,KeyError,TypeError):raise GoogleError('Google Desktop app türünde OAuth istemci JSON dosyasını seçin; web/service-account dosyası uygun değil.') from None
    return {'client_id':cid,'client_secret':secret}


def save_client(pid,raw):
    """Seçilen JSON ortak istemci dosyasına yazılır (GA4 ile aynı; en son seçilen geçerlidir)."""
    client=_client(raw)
    from . import google_shared
    with LOCK:google_shared.save({'installed':client})
    return status(pid)


# Ekip istemcisi: Google Cloud kurulumunu bir kişi bir kez yapar, bu dosya uygulama klasörüyle dağıtılır.
# Herkes yine kendi Google hesabıyla bağlanır; dosyada hesap veya erişim belirteci yoktur.
# Google'a göre masaüstü uygulamasının istemci sırrı gizli tutulamaz; yine de şirket içi dosyadır.
TEAM_FILE=APP_ROOT/'config'/'google-ekip-istemcisi.json'


def team_client():
    """Geçerli ekip istemcisi (yoksa None). Bozuk dosya GoogleError verir; sessizce yok sayılmaz."""
    if not TEAM_FILE.is_file():return None
    try:
        if TEAM_FILE.stat().st_size>65536:raise ValueError()
        raw=json.loads(TEAM_FILE.read_text(encoding='utf-8-sig'))
    except (OSError,ValueError):raise GoogleError('Ekip istemcisi dosyası (config/google-ekip-istemcisi.json) okunamadı. Kurulumu yapan kişiden dosyayı yeniden iste.') from None
    try:return _client(raw)
    except GoogleError:raise GoogleError('Ekip istemcisi dosyası (config/google-ekip-istemcisi.json) Desktop app istemcisi değil. Kurulumu yapan kişiden dosyayı yeniden iste.') from None


def save_team(pid):
    """Ortak GA4 OAuth istemcisini ekip dosyası olarak uygulama klasörüne yazar."""
    with LOCK:
        from . import google_shared
        value=google_shared.shared_client()
        if not value:raise GoogleError('Önce Google Cloud’dan indirdiğin OAuth JSON dosyasını seç.')
        TEAM_FILE.parent.mkdir(parents=True,exist_ok=True)
        store.write_json(TEAM_FILE,{'installed':{'client_id':value['client_id'],'client_secret':value['client_secret'],
                                                 'auth_uri':AUTH_URL,'token_uri':TOKEN_URL,'redirect_uris':['http://localhost']}})
    return status(pid)


CLIENT_FILE=re.compile(r'client_secret_[A-Za-z0-9._-]{1,200}(?: \(\d{1,3}\))?\.json')


def downloads_dir():
    """Windows'ta İndirilenler'in gerçek yeri (OneDrive/başka sürücüye taşınmış olabilir)."""
    if os.name=='nt':
        try:
            import ctypes
            from ctypes import wintypes
            class GUID(ctypes.Structure):
                _fields_=[('a',wintypes.DWORD),('b',wintypes.WORD),('c',wintypes.WORD),('d',ctypes.c_ubyte*8)]
            folder=GUID(0x374DE290,0x123F,0x4565,(ctypes.c_ubyte*8)(0x91,0x64,0x39,0xC4,0x92,0x5E,0x46,0x7B))
            out=ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(folder),0,None,ctypes.byref(out))==0:
                try:return Path(out.value)
                finally:ctypes.windll.ole32.CoTaskMemFree(out)
        except Exception:pass
    return Path.home()/'Downloads'


def find_download(max_age_days=30):
    """İndirilenler'deki en yeni Google OAuth dosyasının adı; içerik okunmaz."""
    cutoff=time.time()-max_age_days*86400;found=[]
    try:
        for path in downloads_dir().iterdir():
            if not CLIENT_FILE.fullmatch(path.name):continue
            info=path.stat()
            if path.is_file() and info.st_size<=65536 and info.st_mtime>=cutoff:found.append((info.st_mtime,path.name))
    except OSError:return None
    if not found:return None
    mtime,name=max(found)
    return {'name':name,'modified':datetime.fromtimestamp(mtime).astimezone().isoformat(timespec='seconds')}


def save_client_from_download(pid,name):
    if not isinstance(name,str) or not CLIENT_FILE.fullmatch(name):raise GoogleError('İndirilenler’de geçerli bir Google OAuth dosyası seçilmedi.')
    path=downloads_dir()/name
    try:
        if not path.is_file() or path.stat().st_size>65536:raise ValueError()
        raw=json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError,ValueError):raise GoogleError('İndirilenler’deki dosya okunamadı. “Başka dosya seç…” ile elle seç.') from None
    return save_client(pid,raw)


def clear(pid):
    """Bu projenin eski ayrı kaydını ve ortak Google girişini siler (OAuth JSON dosyası kalır). Ortak giriş GA4 için de kapanır."""
    from . import google_shared
    with LOCK:
        _path(pid).unlink(missing_ok=True)
        try:google_shared.logout()
        except Exception:pass  # ağ yoksa da yerel oturum silinir; ayrıntı GA4 tarafında
    return status(pid)


def _accept_tokens(value,response):
    token=response.get('access_token');refresh=response.get('refresh_token') or value.get('refresh_token')
    expires=response.get('expires_in')
    if not isinstance(token,str) or not token or not isinstance(refresh,str) or not refresh or type(expires) is not int or not 0<expires<=86400 or any(ord(c)<33 for c in token+refresh):
        raise GoogleError('Google çevrimdışı erişim belirteci vermedi; hesabı yeniden bağlayın.')
    kind=response.get('token_type','Bearer');scope=response.get('scope',SCOPE)
    if not isinstance(kind,str) or kind.casefold()!='bearer' or not isinstance(scope,str) or SCOPE not in scope.split():
        raise GoogleError('Google Sheets izni verilmedi. Yeniden bağlan ve izin ekranında Google E-Tablolar (Sheets) kutusunu işaretleyip devam et.')
    value={k:v for k,v in value.items() if k!='expired'}
    return {**value,'access_token':token,'refresh_token':refresh,'expires_at':time.time()+expires}


def access_token(pid):
    with LOCK:
        from . import google_shared
        from .ga4.istemci import ClientError
        shared,error=_shared()
        if error:raise GoogleError(error)
        if shared.get('expired'):raise GoogleError(EXPIRED,'invalid_grant')
        try:return google_shared.token()
        except ClientError as exc:
            if not shared.get('connected'):
                raise GoogleError('Google hesabını bağlayın: kenar çubuğunda Google bağlantısı.') from None
            raise GoogleError(str(exc)) from None


def connect(pid,log=print,cancel=None):
    """Ortak Google girişini açar: tek JSON, tek hesap; GA4 okuma ve Google Sheets yazma izinleri birlikte istenir."""
    from . import google_shared
    from .ga4.istemci import ClientError
    with store.profile_lock(pid):
        if cancel and cancel.is_set():raise GoogleError('Google bağlantısı iptal edildi.')
        try:
            google_shared.ensure_client(team_client())
            if not google_shared.shared_client():raise GoogleError('Önce Google masaüstü OAuth JSON dosyasını seçin.')
            log('Google izin ekranı normal tarayıcıda açılıyor. Kurum hesabınızla izin verin; şifreniz uygulamaya gelmez. '
                'Bu tek bağlantı GA4 ve Google Sheets için ortaktır.')
            email=google_shared.connect(cancel=cancel)
        except ClientError as exc:raise GoogleError(str(exc)) from None
        log(f'✓ Google hesabı bağlandı ({email or "Google hesabı"}). Henüz dosyaya yazılmadı. Sıradaki adım: Ayarlar → “Bağlantıyı doğrula”.')
        return {'google_connected':True}
