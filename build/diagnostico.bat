@echo off
REM ====================================================================
REM  Coleta o estado da maquina num arquivo unico, para diagnostico.
REM  Nao instala nem altera nada - so le e escreve o relatorio.
REM
REM  Duplo clique. Ao terminar, mande  build\diagnostico.txt
REM
REM  O trabalho de verdade esta em diagnostico.py: da para testar, e
REM  roda tudo num processo so. A versao anterior abria um Python por
REM  modulo, levava dezenas de segundos e quem fechasse antes ficava
REM  com meio relatorio - foi o que aconteceu no primeiro diagnostico.
REM ====================================================================
setlocal
cd /d "%~dp0.."

echo.
echo Coletando... isto leva alguns segundos.
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ====================================================================
    echo  ERRO: 'python' nao encontrado no PATH.
    echo.
    echo  Sem Python nao da nem para diagnosticar. Instale marcando
    echo  "Add Python to PATH" e abra um Prompt de Comando NOVO.
    echo ====================================================================
    goto :fim
)

python "%~dp0diagnostico.py"
if errorlevel 1 (
    echo.
    echo  O diagnostico falhou. A saida acima diz o motivo.
)

:fim
echo.
echo Pressione qualquer tecla para fechar...
pause >nul
endlocal
