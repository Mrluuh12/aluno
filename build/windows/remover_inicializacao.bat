@echo off
REM  Para tudo e tira o Rajant Monitor da inicializacao do Windows.
REM  Botao direito > Executar como administrador.
cd /d "%~dp0"
net session >nul 2>&1
if errorlevel 1 (
    echo  Precisa de administrador: botao direito ^> Executar como administrador.
    pause
    exit /b 1
)
rajant_monitor.exe --remover-servico
pause
