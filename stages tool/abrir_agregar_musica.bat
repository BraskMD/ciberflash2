@echo off
cd /d "%~dp0"
where py >nul 2>nul && (py agregar_musica.py) || (python agregar_musica.py)
if errorlevel 1 pause
