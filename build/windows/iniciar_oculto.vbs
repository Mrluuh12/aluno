' Sobe o Rajant Monitor em segundo plano, sem janela, no login do usuario.
'
' Alternativa a tarefa agendada (instalar_inicializacao.bat), para quando
' nao ha acesso de administrador: crie um atalho para este arquivo em
'     Win+R  ->  shell:startup
' A diferenca: a tarefa agendada sobe com o Windows, mesmo sem login; este
' arquivo so sobe quando alguem entra na maquina.
Set fso = CreateObject("Scripting.FileSystemObject")
pasta = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = pasta
' 0 = janela escondida; False = nao espera terminar.
sh.Run """" & pasta & "\rajant_monitor.exe"" --servico", 0, False
