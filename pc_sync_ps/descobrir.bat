@echo off
cd /d "%~dp0"
echo Mapeando o banco do Plannix (so nomes de tabelas e colunas, nenhum dado de peca)...
echo Se quiser localizar onde fica o codigo da peca, rode:  descobrir.bat 143136850
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0pcp_sync.ps1" descobrir %1
echo.
echo Mande o arquivo plannix_esquema.txt desta pasta.
pause
