@echo off
REM  Para tudo e tira o Rajant Monitor do logon deste usuario.
REM  Nao precisa de administrador.
cd /d "%~dp0"
rajant_monitor.exe --remover-usuario
pause
