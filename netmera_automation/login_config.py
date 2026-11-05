"""Profilin yerel giriş ayarı. Düz metin şifre/anahtar dosyası oluşturmaz.

Windows DPAPI CurrentUser; aynı Windows hesabındaki diğer süreçlere karşı kasa değildir.
İş tanımı/ZIP/Excel'e taşınmaz. API'ye yalnız status() sonucu verilir.
"""
from __future__ import annotations

import base64
import ctypes
from datetime import datetime
import json
import os
import re
import uuid
from zoneinfo import ZoneInfo

from . import store
from .paths import TIMEZONE, check_local_path


class LoginConfigError(ValueError):
    pass


def config_path(profile_id):
    if not isinstance(profile_id,str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,39}',profile_id):
        raise LoginConfigError('Giriş ayarı için proje kimliği geçersiz.')
    return check_local_path(store.profile_dir(profile_id)) / 'giris-ayarlari.json'


def _new():
    return {'version':1,'enabled':False,'keep_logged_in':True,'revision':uuid.uuid4().hex,'protected_credentials':None}


def _read(profile_id):
    path=config_path(profile_id)
    try:
        if not path.exists():
            path.parent.mkdir(parents=True,exist_ok=True)
            try:
                fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd,'w',encoding='utf-8') as handle:
                    json.dump(_new(),handle,ensure_ascii=False,indent=2)
        if path.stat().st_size > 65536:
            raise ValueError()
        data=store.read_json(path)
        if (not isinstance(data,dict) or set(data) != set(_new()) or data['version'] != 1
                or type(data['enabled']) is not bool or type(data['keep_logged_in']) is not bool
                or not isinstance(data['revision'],str) or not re.fullmatch(r'[a-f0-9]{32}',data['revision'])
                or (data['protected_credentials'] is not None and
                    (not isinstance(data['protected_credentials'],str) or len(data['protected_credentials']) > 32768))):
            raise ValueError()
        return data
    except (OSError,ValueError,TypeError,KeyError):
        raise LoginConfigError('Giriş config dosyası okunamadı. Giriş ayarlarını temizleyip arayüzden yeniden kaydet; JSON dosyasına e-posta/şifre yazma.') from None


def _dpapi(data: bytes, profile_id: str, *, decrypt=False) -> bytes:
    if os.name != 'nt':
        raise LoginConfigError('Korumalı giriş bilgileri yalnız Windows üzerinde kullanılabilir.')
    from ctypes import wintypes as W
    class Blob(ctypes.Structure):
        _fields_=[('cbData',W.DWORD),('pbData',ctypes.POINTER(ctypes.c_ubyte))]
    source=ctypes.create_string_buffer(data)
    entropy=ctypes.create_string_buffer(('NetmeraOtomasyon:giris:v1:'+profile_id).encode('ascii'))
    src=Blob(len(data),ctypes.cast(source,ctypes.POINTER(ctypes.c_ubyte)))
    ent=Blob(len(entropy.raw)-1,ctypes.cast(entropy,ctypes.POINTER(ctypes.c_ubyte)))
    out=Blob()
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.LocalFree.argtypes=[ctypes.c_void_p]; kernel.LocalFree.restype=ctypes.c_void_p
    crypto=ctypes.WinDLL('crypt32',use_last_error=True)
    fn=crypto.CryptUnprotectData if decrypt else crypto.CryptProtectData
    fn.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p if decrypt else W.LPCWSTR,
                 ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,W.DWORD,ctypes.POINTER(Blob)]
    fn.restype=W.BOOL
    try:
        ok=fn(ctypes.byref(src),None if decrypt else 'Netmera Otomasyonu',ctypes.byref(ent),None,None,1,ctypes.byref(out))
        if not ok:
            raise LoginConfigError('Windows giriş bilgisini koruyamadı/açamadı. Bu bilgisayardaki kendi Windows hesabında bilgileri yeniden gir.')
        return ctypes.string_at(out.pbData,out.cbData)
    except LoginConfigError:
        raise
    except Exception:
        raise LoginConfigError('Windows giriş koruması kullanılamadı; bilgiler düz metin kaydedilmedi.') from None
    finally:
        ctypes.memset(source,0,len(source))
        if out.pbData:
            if decrypt:
                ctypes.memset(out.pbData,0,out.cbData)
            kernel.LocalFree(ctypes.cast(out.pbData,ctypes.c_void_p))


def _credentials(profile_id, config):
    if not config['protected_credentials']:
        return None
    try:
        blob=base64.b64decode(config['protected_credentials'],validate=True)
        secret=json.loads(_dpapi(blob,profile_id,decrypt=True).decode('utf-8'))
        if (not isinstance(secret,dict) or secret.get('profile_id') != profile_id
                or not isinstance(secret.get('username'),str) or not secret['username']
                or not isinstance(secret.get('password'),str) or not secret['password']):
            raise ValueError()
        return secret
    except (LoginConfigError,ValueError,TypeError,UnicodeError):
        raise LoginConfigError('Kayıtlı giriş bilgisi bu Windows hesabında açılamadı. E-posta ve şifreyi yeniden girip kaydet.') from None


def _attempt(profile_id, config):
    try:
        item=store.read_json(config_path(profile_id).with_name('giris-son-durum.json'),{})
        return item if isinstance(item,dict) and item.get('revision') == config['revision'] else {}
    except (ValueError,OSError):
        raise LoginConfigError('Giriş deneme kaydı okunamadı. Giriş ayarlarını yeniden kaydet.') from None


def receipt(profile_id, config, state, code=None):
    store.write_json(config_path(profile_id).with_name('giris-son-durum.json'),
                     {'revision':config['revision'],'state':state,'code':code,
                      'at':datetime.now(ZoneInfo(TIMEZONE)).isoformat(timespec='seconds')})


def status(profile_id):
    result={'available':True,'supported':os.name=='nt','enabled':False,'keep_logged_in':True,
            'has_credentials':False,'username':'','ready':False,'blocked':False,
            'path':str(config_path(profile_id)),'error':None,'last_attempt':None}
    try:
        config=_read(profile_id)
        result.update(enabled=config['enabled'],keep_logged_in=config['keep_logged_in'],has_credentials=bool(config['protected_credentials']))
        secret=_credentials(profile_id,config)
        if secret:
            result['username']=secret['username']
        attempt=_attempt(profile_id,config)
        blocked=attempt.get('state') in ('attempting','failed','needs_action')
        result.update(blocked=blocked,last_attempt={k:attempt.get(k) for k in ('state','code','at')} if attempt else None,
                      ready=bool(config['enabled'] and secret and not blocked))
        if blocked:
            result['error']=('Giriş denemesi başlatılmış. Sonucu bekle; çalışma kesildiyse giriş ayarını yeniden kaydet.' if attempt.get('state')=='attempting'
                             else 'Son giriş tamamlanamadı; tekrar denemek için bilgileri kontrol edip Giriş ayarlarını kaydet düğmesine bas.')
    except LoginConfigError as exc:
        result['error']=str(exc)
    return result


def save(profile_id, *, enabled, keep_logged_in, username='', password=''):
    config_path(profile_id)  # Kilit/klasör oluşturmadan önce kimliği doğrula.
    if type(enabled) is not bool or type(keep_logged_in) is not bool:
        raise LoginConfigError('Giriş seçenekleri geçersiz.')
    if (not isinstance(username,str) or len(username)>320 or any(c in username for c in '\x00\r\n')
            or not isinstance(password,str) or len(password)>1024 or '\x00' in password):
        raise LoginConfigError('E-posta veya şifre alanı geçersiz; bilgileri arayüzden yeniden gir.')
    username=username.strip()
    with store.profile_lock(profile_id):
        config=_read(profile_id)
        if password:
            if not username:
                raise LoginConfigError('Şifreyi kaydetmek için e-posta/kullanıcı adı gerekli.')
            payload=json.dumps({'profile_id':profile_id,'username':username,'password':password},ensure_ascii=False).encode('utf-8')
            config['protected_credentials']=base64.b64encode(_dpapi(payload,profile_id)).decode('ascii')
        elif config['protected_credentials']:
            previous=_credentials(profile_id,config)
            if username and username != previous['username']:
                raise LoginConfigError('Hesabı değiştirirken yeni şifreyi de gir.')
        elif enabled or username:
            raise LoginConfigError('Otomatik giriş için e-posta ve şifreyi bir kez doldur.')
        config.update(enabled=enabled,keep_logged_in=keep_logged_in,revision=uuid.uuid4().hex)
        store.write_json(config_path(profile_id),config)
    return status(profile_id)


def clear(profile_id):
    config_path(profile_id)
    with store.profile_lock(profile_id):
        store.write_json(config_path(profile_id),_new())
    return status(profile_id)


def prepare(profile_id):
    config=_read(profile_id)
    if not config['enabled']:
        return config,None
    if _attempt(profile_id,config).get('state') in ('attempting','failed','needs_action'):
        raise LoginConfigError('Önceki otomatik giriş tamamlanmadı. Giriş ayarlarını kontrol edip yeniden kaydet.')
    secret=_credentials(profile_id,config)
    if not secret:
        raise LoginConfigError('Otomatik giriş bilgileri eksik. Ayarlar bölümündeki giriş alanlarını doldur.')
    return config,secret
