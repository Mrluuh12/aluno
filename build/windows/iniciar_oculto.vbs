' Sobe o Rajant Monitor agora, em segundo plano, sem janela.
'
' Para subir sozinho no login sem administrador, o caminho normal e o
' instalar_inicializacao_usuario.bat. Este arquivo e a reserva para quando
' a TI bloquear aquela forma: crie um atalho para ele em
'     Win+R  ->  shell:startup
Set fso = CreateObject("Scripting.FileSystemObject")
pasta = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = pasta
' 0 = janela escondida; False = nao espera terminar.
sh.Run """" & pasta & "\rajant_monitor.exe"" --servico", 0, False
