@echo off
schtasks /Delete /TN "PCP Naves - Sincronizar" /F
echo Agendamento removido. Pode apagar esta pasta (o pcp_sync.ini tem a senha do Autodoc).
pause
