' Roda a sincronizacao sem abrir janela. Chamado pelo Agendador de Tarefas.
Set fso = CreateObject("Scripting.FileSystemObject")
pasta = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = pasta
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & pasta & "\pcp_sync.ps1"" rodar", 0, False
