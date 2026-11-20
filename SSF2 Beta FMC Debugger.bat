@echo off
cd /d "%~dp0"
echo "Started"

rem Guarda el log de la ejecucion anterior antes de empezar
if exist ssf2_log.txt move /y ssf2_log.txt ssf2_log_anterior.txt >nul

rem Muestra la consola y a la vez la guarda en ssf2_log.txt (adl se lanza con cmd, igual que antes)
powershell -NoProfile -Command "cmd /c 'adl -runtime . .\META-INF\AIR\application.xml . 2>&1' | Tee-Object -FilePath ssf2_log.txt; exit $LASTEXITCODE"

echo.
echo ================================================
echo El juego se cerro. Codigo de salida: %errorlevel%
echo El log completo esta en ssf2_log.txt
echo ================================================
pause
