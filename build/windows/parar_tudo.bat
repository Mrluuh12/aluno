@echo off
REM  Derruba o supervisor, o exportador, o Prometheus e o Grafana que ele
REM  iniciou. Para subir de novo: dois cliques no rajant_monitor.exe.
cd /d "%~dp0"
rajant_monitor.exe --parar
pause
