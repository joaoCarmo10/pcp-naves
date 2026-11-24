@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === PCP Naves - instalacao da sincronizacao (Autodoc + Plannix) ===
echo.
where py >nul 2>&1
if errorlevel 1 (
  echo Python nao encontrado. Instalando o Python 3.12 so para este usuario...
  winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
  where py >nul 2>&1
  if errorlevel 1 (
    echo.
    echo Nao deu para instalar sozinho. Instale o Python em https://www.python.org/downloads/ ^(marque "Add to PATH"^)
    echo e rode este instalar.bat de novo.
    pause
    exit /b 1
  )
)
if not exist pcp_sync.ini (
  copy pcp_sync.exemplo.ini pcp_sync.ini >nul
  echo Preencha o pcp_sync.ini que vai abrir ^(token do app e usuario do Autodoc^), salve e feche o Bloco de Notas.
  notepad pcp_sync.ini
)
findstr /R /C:"^modo *= *sql" pcp_sync.ini >nul && py -3 -m pip install --user --quiet pyodbc
echo.
echo Testando acessos (nada e gravado no app)...
py -3 pcp_sync.py testar
echo.
schtasks /Create /TN "PCP Naves - Sincronizar" /TR "wscript.exe \"%~dp0rodar.vbs\"" /SC HOURLY /MO 1 /ST 06:05 /F
echo.
echo Pronto. Roda de hora em hora, escondido, das 6h as 20h ^(ajuste no pcp_sync.ini^).
echo Rodar agora:  py -3 pcp_sync.py agora
echo Ver o log:    pcp_sync.log
echo Remover:      desinstalar.bat
pause
