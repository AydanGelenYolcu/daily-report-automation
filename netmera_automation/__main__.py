"""Komutlar:
  python -m netmera_automation                 arayüzü açar
  python -m netmera_automation run --profile ID [--test] [--zamanli]
  python -m netmera_automation login --profile ID
  python -m netmera_automation profiller
"""
from __future__ import annotations

import argparse
from datetime import datetime
import os
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="netmera_automation", description="Netmera Otomasyonu")
    sub = parser.add_subparsers(dest="command")
    ui = sub.add_parser("ui", help="Arayüzü aç (varsayılan)")
    ui.add_argument("--no-browser", action="store_true")
    ui.add_argument("--port", type=int)
    run = sub.add_parser("run", help="Profilin etkin işlerini çalıştır")
    run.add_argument("--profile", required=True)
    run.add_argument("--test", action="store_true", help="Excel'e yazma")
    run.add_argument("--zamanli", action="store_true", help="Görev Zamanlayıcı çağrısı: çıktıyı günlüğe yaz")
    run.add_argument("--tarih", help="Event işleri için gün veya aralık: 2026-10-10 ya da 2026-10-10:2026-10-12")
    login = sub.add_parser("login", help="Panel oturumunu bir kez aç")
    login.add_argument("--profile", required=True)
    sub.add_parser("profiller", help="Profilleri listele")
    args = parser.parse_args(argv)

    if args.command in (None, "ui"):
        from .server import serve
        return serve(open_browser=not getattr(args, "no_browser", False), port=getattr(args, "port", None))

    from . import runner, store
    from .jobs import JobError
    from .panel import PanelError
    from .paths import PathError

    if args.command == "profiller":
        store.ensure_defaults()
        for p in store.list_profiles():
            print(f"{p['id']:<24} {p['name']} ({p['source']}, {sum(j['enabled'] for j in p['jobs'])} etkin iş)")
        return 0

    log_file = None
    if getattr(args, "zamanli", False):
        folder = store.profile_dir(args.profile) / "gunluk"
        folder.mkdir(parents=True, exist_ok=True)
        log_file = (folder / f"{datetime.now():%Y-%m}.log").open("a", encoding="utf-8")

    def log(text: str):
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {text}"
        if log_file:
            log_file.write(line + "\n")
            log_file.flush()
        elif sys.stdout:
            print(line)

    scheduled = getattr(args,'zamanli',False)
    exit_code, summary, failure, notify = 4, None, None, False
    receipt = {'status':'running','started_at':runner.now().isoformat(),'completed_at':None,'pid':os.getpid()}
    try:
        if scheduled:
            store.write_json(store.profile_dir(args.profile) / 'zamanli-son-durum.json',receipt)
        profile = store.load_profile(args.profile)
        notify = profile.get('notify') is True
        if args.command == "login":
            return 0 if runner.login(profile, log=log) else 1
        dates = None
        if args.tarih:
            from .jobs import parse_day_range
            start, _, end = args.tarih.partition(":")
            dates = parse_day_range(start, end or None, "--tarih")
        summary = runner.run_profile(profile, write=not args.test, dates=dates, log=log)
        exit_code = 5 if summary['failed'] else 6 if summary['write'] and not summary.get('excel_written') else 0
        return exit_code
    except PanelError as exc:
        failure = exc.message(); log(f"✗ {failure}"); exit_code = 2
        return exit_code
    except (runner.RunError, store.BusyError, JobError, PathError, ValueError) as exc:
        failure = str(exc); log(f"✗ {failure}"); exit_code = 2
        return exit_code
    except Exception as exc:  # zamanlanmış görevde sessiz kalmasın
        failure = f"Beklenmeyen hata: {type(exc).__name__}"; log(f"✗ {failure}"); exit_code = 4
        return exit_code
    finally:
        if scheduled:
            receipt.update(status='success' if exit_code == 0 else 'partial' if exit_code == 5 else 'excel_error' if exit_code == 6 else 'error',
                           completed_at=runner.now().isoformat(),exit_code=exit_code,error=failure)
            if summary:
                receipt.update({key:summary.get(key) for key in ('run_id','ok','failed','excel','excel_written')})
            try:
                store.write_json(store.profile_dir(args.profile) / 'zamanli-son-durum.json',receipt)
            except OSError:
                log('Zamanlanmış çalışma durum dosyası yazılamadı; Windows sonuç kodunu kontrol edin.')
            if notify and args.command == 'run':
                from .notify import run_text, show
                if not show(*run_text(summary, failure)):
                    log('ⓘ Windows bildirimi gösterilemedi (şirket ayarı engellemiş olabilir); çalışma etkilenmedi.')
        if log_file:
            log_file.close()


if __name__ == "__main__":
    raise SystemExit(main())
