@echo off
REM ====================================================================
REM  Gera site_survey.exe no Windows — a janela unica.
REM
REM  Duas abas:
REM    Coleta    fala com os radios, descobre a malha, voce marca os
REM              equipamentos e coleta ao vivo
REM    Arquivos  le capturas do MeshMapper que alguem ja trouxe
REM
REM  E O UNICO EXECUTAVEL DO PROJETO. Existia antes um survey_meshmapper
REM  .exe so para arquivos; ele virou a aba "Arquivos" daqui. Dois exes
REM  na mesma pasta so geravam duvida sobre qual abrir.
REM
REM  A aba Coleta precisa da rajant-api e de rede ate a malha. Sem elas
REM  ela se explica em vez de falhar, e a de Arquivos segue funcionando —
REM  o mesmo exe serve as duas maquinas.
REM
REM  Rode a partir da pasta do projeto:  build\gerar_exe_site_survey.bat
REM  Pode dar duplo clique: a janela NAO fecha sozinha, nem no erro.
REM
REM  Log completo em  build\log_build_site_survey.txt
REM ====================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0.."
set "LOG=%~dp0log_build_site_survey.txt"
set "PASSO=inicio"

echo ==================================================== > "%LOG%"
echo Build iniciado em %DATE% %TIME% >> "%LOG%"
echo Pasta: %CD% >> "%LOG%"
echo ==================================================== >> "%LOG%"

echo.
echo  Log completo em: %LOG%
echo.

REM ---------------------------------------------------------------- 1
set "PASSO=conferir o Python"
echo [1/6] Conferindo o Python...
python --version >> "%LOG%" 2>&1
if errorlevel 1 (
    echo   ERRO: 'python' nao encontrado no PATH.
    echo   Instale marcando "Add Python to PATH" e abra um Prompt NOVO.
    goto :erro
)
for /f "delims=" %%v in ('python --version 2^>^&1') do echo   %%v

REM ---------------------------------------------------------------- 2
set "PASSO=instalar as dependencias"
echo.
echo [2/6] Instalando dependencias... ^(alguns minutos^)
python -m pip install --upgrade pip >> "%LOG%" 2>&1
python -m pip install pyinstaller matplotlib numpy scipy python-pptx openpyxl lxml >> "%LOG%" 2>&1
if errorlevel 1 (
    echo   ERRO ao instalar as dependencias.
    goto :erro
)
echo   ok

REM ---------------------------------------------------------------- 3
REM  A rajant-api DECLARA grpcio e grpcio-tools e NUNCA os importa.
REM  grpcio 1.56.2 nao tem wheel para Python 3.12+ e a instalacao morre
REM  tentando compilar C++. Por isso --no-deps, e o protobuf a parte.
set "PASSO=instalar a rajant-api"
echo.
echo [3/6] Instalando a rajant-api ^(este exe fala com radio^)...
python -m pip install rajant-api --no-deps >> "%LOG%" 2>&1
python -m pip install "protobuf==4.23.4" >> "%LOG%" 2>&1
python -c "import sys; sys.argv=['x']; import rajant_monitor as m; print('   ok: rajant-api ' + ('AUSENTE - a aba de coleta nao vai funcionar' if m.Breadcrumb is None else 'presente'))" > "%TEMP%\ssapi.txt" 2>&1
type "%TEMP%\ssapi.txt"
type "%TEMP%\ssapi.txt" >> "%LOG%"

REM ---------------------------------------------------------------- 4
set "PASSO=compilar com o PyInstaller"
echo.
echo [4/6] Compilando com o PyInstaller...
echo.
echo       ISTO DEMORA. De 3 a 10 minutos, dependendo da maquina.
echo       A tela fica parada nesse tempo - e normal, NAO travou.
echo.
REM  `python -m PyInstaller`, NAO `pyinstaller` direto: quando o pip cai
REM  em "user installation", os executaveis vao para
REM  ...\AppData\Roaming\Python\PythonXX\Scripts, que normalmente NAO
REM  esta no PATH — e o build morre com "is not recognized".
python -m PyInstaller --noconfirm --clean "build\site_survey.spec" >> "%LOG%" 2>&1
if errorlevel 1 (
    echo   ERRO na compilacao.
    goto :erro
)
if not exist "dist\site_survey\site_survey.exe" (
    echo   ERRO: o PyInstaller terminou sem erro, mas o .exe nao apareceu.
    goto :erro
)
echo   ok

REM ---------------------------------------------------------------- 5
set "PASSO=copiar os arquivos externos"
echo.
echo [5/6] Copiando os arquivos que ficam FORA do exe...
if exist marca      xcopy /E /I /Y marca dist\site_survey\marca >> "%LOG%" 2>&1
if exist config.ini copy /Y config.ini dist\site_survey\ >> "%LOG%" 2>&1
echo   ok

REM ---------------------------------------------------------------- 6
REM  Prova que o exe funciona ANTES de entregar. Sem isto, a unica forma
REM  de saber era dar duplo clique — e se falhasse, o console fechava no
REM  mesmo instante e o que sobrava era "nao abre", sem pista nenhuma.
REM  --verificar confere os modulos, o tcl/tk e as dependencias, e SAI.
set "PASSO=verificar o executavel"
echo.
echo [6/6] Conferindo o executavel...
pushd dist\site_survey
site_survey.exe --verificar > "%TEMP%\ssver.txt" 2>&1
set "RC=%ERRORLEVEL%"
popd
type "%TEMP%\ssver.txt"
type "%TEMP%\ssver.txt" >> "%LOG%"
if not "%RC%"=="0" (
    echo.
    echo   ERRO: o .exe foi gerado mas NAO vai abrir a janela.
    echo   O motivo esta logo acima.
    goto :erro
)

echo.
echo ====================================================================
echo  PRONTO: dist\site_survey\site_survey.exe
echo.
echo  E ESTE O UNICO PROGRAMA. De duplo clique nele.
echo  Abre a janela com as duas abas:
echo    Coleta    - Procurar equipamentos, marcar, Iniciar coleta
echo    Arquivos  - Escolher arquivos do MeshMapper, Gerar relatorios
echo.
echo  Se em algum momento ele nao abrir, rode no Prompt:
echo     dist\site_survey\site_survey.exe --verificar
echo  e mande a saida. O erro tambem fica em site_survey_erro.txt,
echo  ao lado do exe.
echo ====================================================================
echo.
echo Build concluido em %DATE% %TIME% >> "%LOG%"
goto :fim

:erro
echo.
echo ====================================================================
echo  FALHOU ao %PASSO%.
echo.
echo  As ultimas linhas do log:
echo ====================================================================
powershell -NoProfile -Command "Get-Content -Tail 25 '%LOG%'" 2>nul || (
    echo   ^(log completo em %LOG%^)
)
echo ====================================================================
echo  Mande o arquivo %LOG% para analise.
echo ====================================================================

:fim
echo.
echo Pressione qualquer tecla para fechar...
pause >nul
endlocal
