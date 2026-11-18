@echo off
rem Agenda o envio a cada 10 minutos, enquanto este usuario estiver logado no PC.
schtasks /Create /TN "PCP Naves - Plannix" /TR "\"%~dp0rodar.bat\"" /SC MINUTE /MO 10 /F
echo.
echo Pronto. Para remover: schtasks /Delete /TN "PCP Naves - Plannix" /F
pause
