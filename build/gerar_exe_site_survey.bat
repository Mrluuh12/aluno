@echo off
REM ====================================================================
REM  Gera site_survey.exe no Windows — a janela unica.
REM
REM  Duas abas:
REM    Coleta    fala com os radios, descobre a malha, voce marca os
REM              equipamentos e coleta ao vivo
REM    Arquivos  le capturas do MeshMapper que alguem ja trouxe
REM
REM  Este exe PRECISA da rajant-api e de rede ate a malha. Quem so gera
REM  relatorio de arquivo continua tendo o survey_meshmapper.exe, que nao
REM  carrega a biblioteca nem exige rede.
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
echo [1/5] Conferindo o Python...
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
echo [2/5] Instalando dependencias... ^(alguns minutos^)
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
echo [3/5] Instalando a rajant-api ^(este exe fala com radio^)...
python -m pip install rajant-api --no-deps >> "%LOG%" 2>&1
python -m pip install "protobuf==4.23.4" >> "%LOG%" 2>&1
python -c "import sys; sys.argv=['x']; import rajant_monitor as m; print('   ok: rajant-api ' + ('AUSENTE - a aba de coleta nao vai funcionar' if m.Breadcrumb is None else 'presente'))" > "%TEMP%\ssapi.txt" 2>&1
type "%TEMP%\ssapi.txt"
type "%TEMP%\ssapi.txt" >> "%LOG%"

REM ---------------------------------------------------------------- 4
set "PASSO=compilar com o PyInstaller"
echo.
echo [4/5] Compilando com o PyInstaller...
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
echo [5/5] Copiando os arquivos que ficam FORA do exe...
if exist marca      xcopy /E /I /Y marca dist\site_survey\marca >> "%LOG%" 2>&1
if exist config.ini copy /Y config.ini dist\site_survey\ >> "%LOG%" 2>&1
echo   ok

echo.
echo ====================================================================
echo  PRONTO: dist\site_survey\site_survey.exe
echo.
echo  DE DUPLO CLIQUE no exe. Abre a janela com as duas abas:
echo    Coleta    - Procurar equipamentos, marcar, Iniciar coleta
echo    Arquivos  - Escolher arquivos do MeshMapper, Gerar relatorios
echo.
echo  Pela linha de comando os modulos tambem funcionam:
echo     coleta_rajant --seeds 10.188.96.140 --minutos 30 -o saida
echo     survey_meshmapper captura.kmz -o saida
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
