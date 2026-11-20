@echo off
cd /d "%~dp0"
python paletas_tool.py
if errorlevel 1 pause
