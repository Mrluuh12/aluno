@echo off
REM  Derruba o supervisor, o exportador, o Prometheus e o Grafana que ele
REM  iniciou. A tarefa agendada continua: no proximo boot sobe de novo.
cd /d "%~dp0"
rajant_monitor.exe --parar
pause
