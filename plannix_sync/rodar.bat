@echo off
cd /d "%~dp0"
py -3 plannix_sync.py enviar >> plannix_sync.log 2>&1
