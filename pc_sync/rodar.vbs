' Roda a sincronizacao sem abrir janela. Chamado pelo Agendador de Tarefas.
Set fso = CreateObject("Scripting.FileSystemObject")
pasta = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = pasta
sh.Run "pyw -3 """ & pasta & "\pcp_sync.py"" rodar", 0, False
