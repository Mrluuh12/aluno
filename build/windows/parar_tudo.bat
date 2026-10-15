@echo off
REM  Derruba o supervisor, o exportador, o Prometheus e o Grafana que ele
REM  iniciou. A inicializacao continua configurada: no proximo boot (ou
REM  login, na versao sem administrador) sobe de novo.
cd /d "%~dp0"
rajant_monitor.exe --parar
pause
