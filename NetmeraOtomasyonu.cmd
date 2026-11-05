@echo off
rem Netmera Otomasyonu arayuzunu acar. Kurulum gerektirmez (python klasoru paketle gelir).
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"
set "PY=python\python.exe"
if exist "%PY%" goto start
set "PY=.venv\Scripts\python.exe"
if exist "%PY%" goto start
echo.
echo Python bulunamadi: bu dosyanin yaninda "python" klasoru yok.
echo Calistirilan klasor: %~dp0
echo.
echo %~dp0 | findstr /i "\Temp\ .zip" >nul && (
  echo ZIP dosyasinin ICINDEN calistirdiniz. Once ZIP'e sag tiklayip "Tumunu ayikla" deyin,
  echo sonra ayiklanan klasordeki NetmeraOtomasyonu.cmd dosyasini calistirin.
) || (
  echo ZIP'i tamamen ayikladiginizdan emin olun: ayiklama bitmeden calistirdiysaniz bekleyip tekrar deneyin.
  echo Klasorde "python" klasoru hala yoksa ZIP'i yeniden ayiklayin; guvenlik yazilimi silmis olabilir.
)
echo.
pause
exit /b 1
:start
echo Netmera Otomasyonu baslatiliyor...
"%PY%" -m netmera_automation ui
if errorlevel 1 pause
