@echo off
REM ====================================================================
REM  Faz o Rajant Monitor subir sozinho quando ESTE usuario entra no
REM  Windows: exportador, Prometheus e Grafana, em segundo plano, sem
REM  janela. NAO precisa de administrador - basta dar dois cliques.
REM
REM  Diferenca da versao de administrador (instalar_inicializacao.bat):
REM  esta so sobe depois do login. Maquina ligada sem ninguem logado =
REM  nada rodando; logoff encerra tudo. Bloquear a tela (Win+L) nao para.
REM
REM  A pasta precisa aceitar gravacao pelo seu usuario (config.ini, logs):
REM  use uma pasta sua, ex. C:\Users\SEU_USUARIO\RajantMonitor - nao Program Files.
REM ====================================================================
cd /d "%~dp0"
if not exist rajant_monitor.exe (
    echo  rajant_monitor.exe nao esta nesta pasta: %CD%
    pause
    exit /b 1
)
rajant_monitor.exe --instalar-usuario
echo.
echo  Para ver o que esta no ar:   rajant_monitor.exe --status
echo  Registro do supervisor:      servico.log
echo.
pause
