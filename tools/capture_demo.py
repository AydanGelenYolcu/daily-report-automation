"""Yalnız geçici, sentetik projelerle README görselleri üretir."""
import os
from pathlib import Path
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    with tempfile.TemporaryDirectory(prefix='netmera-public-demo-') as temp:
        os.environ.update(NETMERA_OTOMASYON_DATA=str(Path(temp)/'data'),
                          ANALYTICS_CEKME_DATA=str(Path(temp)/'analytics'),
                          NETMERA_GA4_LEGACY=str(Path(temp)/'no-legacy'))
        from netmera_automation import store, server, runner
        from playwright.sync_api import sync_playwright
        store.ensure_defaults()
        p=store.load_profile('demo-sahte-veri')
        p['name']='Demo — Günlük Rapor'
        p['jobs'].append({'type':'segment_run','label':'Demo segment sayısı','name':'Demo segment','period':{'type':'yesterday'}})
        p['jobs'].append({'type':'formula','label':'Ortalama etkileşim','expression':'(A + B) / 2',
                          'variables':[{'name':'A','kind':'job','job_id':p['jobs'][1]['id'],'column':'All Total'},
                                       {'name':'B','kind':'job','job_id':p['jobs'][2]['id'],'column':'All Total'}]})
        p=store.save_profile(p)
        runner.run_profile(p,write=False,log=lambda _:None)
        app=server.App(server._free_port(0))
        app.httpd=ThreadingHTTPServer(('127.0.0.1',app.port),app.make_handler())
        thread=threading.Thread(target=app.httpd.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{app.port}'
        out=ROOT/'docs'/'media';out.mkdir(parents=True,exist_ok=True)
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(channel='chrome',headless=True)
                context=browser.new_context(viewport={'width':1440,'height':1040},device_scale_factor=1,color_scheme='light')
                context.route('**/*',lambda r:r.continue_() if r.request.url.startswith(base+'/') else r.abort())
                page=context.new_page()
                page.goto(f'{base}/?t={app.token}')
                page.locator('[data-open-project="demo-sahte-veri"]').wait_for()
                page.screenshot(path=str(out/'projects.png'))
                page.locator('[data-open-project="demo-sahte-veri"]').click()
                page.locator('#jobList .job').first.wait_for()
                page.wait_for_timeout(250)
                page.screenshot(path=str(out/'jobs.png'))
                page.locator('.nav-oge[href="#/calistir"]').click()
                page.locator('#resBody tr').first.wait_for()
                page.locator('#testBtn').click()
                page.wait_for_function("document.querySelector('#statusPill').textContent === 'Hazır' && document.querySelector('#console').textContent.includes('Bitti:')",timeout=30000)
                page.locator('#resBody').scroll_into_view_if_needed()
                page.screenshot(path=str(out/'results.png'))
                page.locator('.nav-oge[href="#/isler"]').click()
                page.locator(f'[data-edit="{len(p["jobs"])-1}"]').click()
                page.locator('#jobDlg').wait_for()
                page.wait_for_timeout(500)
                page.screenshot(path=str(out/'formula.png'))
                browser.close()
        finally:
            app.httpd.shutdown();app.httpd.server_close();thread.join(5)
        print('Synthetic screenshots generated in docs/media')


if __name__=='__main__':
    main()
