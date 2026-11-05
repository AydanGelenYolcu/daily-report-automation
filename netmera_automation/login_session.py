"""Yalnız Netmera /login formunda, çalışma başına en çok bir giriş denemesi."""
from __future__ import annotations

import re
import time
from urllib.parse import urlparse

from . import login_config
from .panel import PanelError


def is_login_page(page):
    url=urlparse(getattr(page,'url',''))
    return url.scheme=='https' and url.netloc=='panel.netmera.com' and url.path.rstrip('/')=='/login'


class AutoLogin:
    def __init__(self, profile, reader, log=print, cancel=None):
        self.profile,self.reader,self.page=profile,reader,reader.page
        self.log,self.cancel,self.attempted=log,cancel,False

    def _origin(self):
        url=urlparse(self.page.url)
        if url.scheme!='https' or url.netloc!='panel.netmera.com':
            raise PanelError('ORIGIN_MISMATCH')
        if self.cancel and self.cancel.is_set():
            raise PanelError('CANCELLED')

    def _challenge(self):
        return (self.page.locator('input[autocomplete="one-time-code"]:visible, input[name="otp"]:visible, '
                                  'input[autocomplete="new-password"]:visible, iframe[title*="captcha" i]:visible').count()>0
                or self.page.get_by_text(re.compile(r'(verification code|two.factor authentication|enter.*one.time code)',re.I)).count()>0)

    def _receipt(self,config,state,code=None):
        try:
            login_config.receipt(self.profile['id'],config,state,code)
        except Exception:
            raise PanelError('LOGIN_CONFIG_REQUIRED',detail='Giriş deneme kaydı yazılamadı; veri klasörüne erişimi kontrol et.') from None

    def _form(self):
        form=self.page.locator('form:visible').filter(has=self.page.locator('input#password[type="password"]'))
        deadline=time.monotonic()+self.profile['timeout_seconds']
        while True:
            self._origin()
            if not is_login_page(self.page) and self.reader.current_apps():
                self.reader.verify_app()
                return None  # Oturum kendi kendine geri yüklendi; parola gerekmez.
            if self._challenge():
                raise PanelError('AUTO_LOGIN_ACTION_REQUIRED')
            if is_login_page(self.page) and form.count()==1:
                break
            if time.monotonic()>=deadline:
                raise PanelError('LOGIN_FORM_UNVERIFIED',detail='Giriş formu veya açık uygulama bekleme süresinde yüklenmedi.')
            self.page.wait_for_timeout(150)
        if (form.count()!=1 or form.locator('#username').count()!=1 or form.locator('#rememberMe[type="checkbox"]').count()!=1
                or form.locator('#username').get_attribute('type') not in ('text','email')
                or form.get_by_role('button',name='Login',exact=True).count()!=1
                or form.locator('button[type="submit"]').count()!=1
                or form.get_by_role('button',name='Login',exact=True).get_attribute('type')!='submit'
                or form.get_attribute('action') not in (None,'','/login','https://panel.netmera.com/login')):
            raise PanelError('LOGIN_FORM_UNVERIFIED')
        if (form.get_attribute('method') or 'get').lower()=='get' and any(form.locator(s).get_attribute('name') for s in ('#username','#password')):
            raise PanelError('LOGIN_FORM_UNVERIFIED')
        # ElementHandle aynı belgeye bağlı kalır. Yazma sırasında başka kökene
        # yönlenilirse locator yeni sayfadaki benzer kutuya tekrar bağlanamaz.
        handle=form.element_handle()
        controls={key:handle.query_selector(selector) for key,selector in
                  (('username','#username'),('password','#password'),('remember','#rememberMe'),('submit','button[type="submit"]'))}
        if not all(controls.values()):
            raise PanelError('LOGIN_FORM_UNVERIFIED')
        self._origin()
        return controls

    def recover(self):
        self._origin()
        if self.attempted:
            raise PanelError('AUTO_LOGIN_LIMIT')
        self.log('   · Netmera oturumu ve giriş ekranının yüklenmesi kontrol ediliyor')
        # Gerçek parola yalnız gözlenmiş, tek form ve doğru köken doğrulanınca yazılır.
        try:
            form=self._form()
        except PanelError:
            raise
        except Exception:
            raise PanelError('LOGIN_FORM_UNVERIFIED') from None
        if form is None:
            self.log('   ✓ Mevcut Netmera oturumu doğrulandı; giriş bilgileri kullanılmadı')
            return True
        try:
            config,secret=login_config.prepare(self.profile['id'])
        except login_config.LoginConfigError as exc:
            raise PanelError('LOGIN_CONFIG_REQUIRED',detail=str(exc)) from None
        if secret is None:
            raise PanelError('LOGIN_REQUIRED')
        self.attempted=True
        self._receipt(config,'attempting')
        self.log('   · Oturum sona ermiş; kayıtlı bilgilerle Netmera girişi bir kez deneniyor')
        try:
            self._origin()
            form['username'].fill(secret['username'])
            self._origin()
            form['password'].fill(secret['password'])
            form['remember'].set_checked(config['keep_logged_in'])
            self._origin()
            if not is_login_page(self.page):
                raise PanelError('LOGIN_FORM_UNVERIFIED')
            form['submit'].click()
            deadline=time.monotonic()+self.profile['timeout_seconds']
            while time.monotonic()<deadline:
                self._origin()
                if self._challenge():
                    raise PanelError('AUTO_LOGIN_ACTION_REQUIRED')
                if not is_login_page(self.page) and self.reader.current_apps():
                    self.reader.verify_app()
                    self._receipt(config,'success')
                    self.log('   ✓ Netmera girişi ve hedef uygulama doğrulandı; iş yeniden okunacak')
                    return True
                if is_login_page(self.page) and self.page.locator('[role="alert"]:visible, .ant-message-error:visible, .ant-form-item-explain-error:visible').count():
                    raise PanelError('AUTO_LOGIN_FAILED')
                self.page.wait_for_timeout(150)
            raise PanelError('AUTO_LOGIN_FAILED' if is_login_page(self.page) else 'AUTO_LOGIN_ACTION_REQUIRED')
        except PanelError as exc:
            self._receipt(config,'needs_action' if exc.code=='AUTO_LOGIN_ACTION_REQUIRED' else 'failed',exc.code)
            raise
        except Exception:
            self._receipt(config,'failed','AUTO_LOGIN_FAILED')
            # Playwright'ın call log'u doldurulan şifreyi içerebilir: aynen iletilmez.
            raise PanelError('AUTO_LOGIN_FAILED') from None
        finally:
            secret=None
            if is_login_page(self.page):
                try:
                    for selector in ('form #password','form #username'):
                        box=self.page.locator(selector)
                        if box.count()==1:
                            box.fill('',timeout=500)
                except Exception:
                    pass
