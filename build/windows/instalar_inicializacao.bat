@echo off
REM ====================================================================
REM  Faz o Rajant Monitor subir sozinho com o Windows, em segundo plano:
REM  exportador, Prometheus e Grafana, sem janela, mesmo sem ninguem
REM  fazer login. Se algum cair, o supervisor reergue.
REM
REM  Botao direito neste arquivo > Executar como administrador.
REM  Rode da pasta do rajant_monitor.exe.
REM ====================================================================
cd /d "%~dp0"
net session >nul 2>&1
if errorlevel 1 (
    echo.
    echo  Precisa de administrador: botao direito ^> Executar como administrador.
    echo.
    pause
    exit /b 1
)
if not exist rajant_monitor.exe (
    echo  rajant_monitor.exe nao esta nesta pasta: %CD%
    pause
    exit /b 1
)
rajant_monitor.exe --instalar-servico
echo.
echo  Para ver o que esta no ar:   rajant_monitor.exe --status
echo  Registro do supervisor:      servico.log
echo.
pause
