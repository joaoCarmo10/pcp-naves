@echo off
cd /d "%~dp0"
echo Compara o que o script le do banco do Plannix com a ultima exportacao "all_data" salva em Downloads.
echo Exporte o all data do Plannix (de preferencia completo: fabricadas e a fabricar) antes de rodar.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0pcp_sync.ps1" comparar
echo.
echo Mande o arquivo plannix_comparacao.txt desta pasta.
pause
