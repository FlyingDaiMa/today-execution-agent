@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -u main.py 1>>bot-output-current.log 2>>bot-error-current.log
