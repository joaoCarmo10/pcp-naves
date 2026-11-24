@echo off
cd /d "%~dp0"
echo === PCP Naves - instalacao da sincronizacao (Autodoc + Plannix) ===
echo Usa so o PowerShell que ja vem no Windows. Nao instala nada.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -LiteralPath '%~dp0' | Unblock-File"
if not exist pcp_sync.ini (
  copy pcp_sync.exemplo.ini pcp_sync.ini >nul
  echo Preencha o pcp_sync.ini que vai abrir: token do app e usuario do Autodoc. Salve e feche o Bloco de Notas.
  notepad pcp_sync.ini
)
echo.
echo Testando acessos (nada e gravado no app)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0pcp_sync.ps1" testar
echo.
schtasks /Create /TN "PCP Naves - Sincronizar" /TR "wscript.exe \"%~dp0rodar.vbs\"" /SC HOURLY /MO 1 /ST 06:05 /F
echo.
echo Pronto. Roda de hora em hora, sem abrir janela, das 6h as 20h (ajuste no pcp_sync.ini).
echo Rodar agora:  rodar_agora.bat
echo Ver o log:    pcp_sync.log
echo Remover:      desinstalar.bat
pause
