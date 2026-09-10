@echo off
REM ====================================================================
REM  Gera survey_meshmapper.exe no Windows.
REM
REM  Este e o gerador de RELATORIO a partir do arquivo do MeshMapper.
REM  Nao fala com radio: entra o .kmz do MeshMapper, saem o KMZ com o
REM  rastro de calor, o PPT e o Excel.
REM
REM  Rode a partir da pasta do projeto:   build\gerar_exe_survey.bat
REM  Pode dar duplo clique: a janela NAO fecha sozinha, nem no erro.
REM
REM  Log completo em  build\log_build_survey.txt
REM ====================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0.."
set "LOG=%~dp0log_build_survey.txt"
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
echo [1/4] Conferindo o Python...
python --version >> "%LOG%" 2>&1
if errorlevel 1 (
    echo   ERRO: 'python' nao encontrado no PATH.
    echo   Instale marcando "Add Python to PATH" e abra um Prompt NOVO.
    goto :erro
)
for /f "delims=" %%v in ('python --version 2^>^&1') do echo   %%v

REM ---------------------------------------------------------------- 2
REM  NAO precisa da rajant-api nem do protobuf: este exe nao fala com
REM  radio. E uma dependencia a menos para dar errado na maquina de quem
REM  so quer gerar o relatorio.
set "PASSO=instalar as dependencias"
echo.
echo [2/4] Instalando dependencias... ^(alguns minutos^)
python -m pip install --upgrade pip >> "%LOG%" 2>&1
python -m pip install pyinstaller matplotlib numpy scipy python-pptx openpyxl lxml >> "%LOG%" 2>&1
if errorlevel 1 (
    echo   ERRO ao instalar as dependencias.
    goto :erro
)
echo   ok

REM  Confere que o modulo importa SEM a rajant-api instalada — que e
REM  justamente o cenario da maquina de quem gera o relatorio.
python -c "import sys; sys.argv=['x']; import rajant_monitor as m; print('   ok: motor de relatorio carregado (rajant-api: ' + ('ausente, tudo bem' if m.Breadcrumb is None else 'presente') + ')')" > "%TEMP%\smchk.txt" 2>&1
if errorlevel 1 (
    echo   ERRO ao importar o motor de relatorio:
    type "%TEMP%\smchk.txt"
    type "%TEMP%\smchk.txt" >> "%LOG%"
    goto :erro
)
type "%TEMP%\smchk.txt"
type "%TEMP%\smchk.txt" >> "%LOG%"

REM ---------------------------------------------------------------- 3
set "PASSO=compilar com o PyInstaller"
echo.
echo [3/4] Compilando com o PyInstaller...
echo.
echo       ISTO DEMORA. De 3 a 10 minutos, dependendo da maquina.
echo       A tela fica parada nesse tempo - e normal, NAO travou.
echo.
REM  `python -m PyInstaller`, NAO `pyinstaller` direto: quando o pip cai
REM  em "user installation", os executaveis vao para
REM  ...\AppData\Roaming\Python\PythonXX\Scripts, que normalmente NAO
REM  esta no PATH — e o build morre com "is not recognized".
python -m PyInstaller --noconfirm --clean "build\survey_meshmapper.spec" >> "%LOG%" 2>&1
if errorlevel 1 (
    echo   ERRO na compilacao.
    goto :erro
)
if not exist "dist\survey_meshmapper\survey_meshmapper.exe" (
    echo   ERRO: o PyInstaller terminou sem erro, mas o .exe nao apareceu.
    goto :erro
)
echo   ok

REM ---------------------------------------------------------------- 4
set "PASSO=copiar os arquivos externos"
echo.
echo [4/4] Copiando os arquivos que ficam FORA do exe...
REM  Logos da identidade Anglo: sem eles o deck sai sem marca.
if exist marca      xcopy /E /I /Y marca dist\survey_meshmapper\marca >> "%LOG%" 2>&1
if exist config.ini copy /Y config.ini dist\survey_meshmapper\ >> "%LOG%" 2>&1
echo   ok

echo.
echo Conferindo o executavel...
pushd dist\survey_meshmapper
survey_meshmapper.exe --help > "%TEMP%\smexe.txt" 2>&1
set "RC=%ERRORLEVEL%"
popd
if not "%RC%"=="0" (
    echo   ERRO: o .exe foi gerado mas nao roda.
    type "%TEMP%\smexe.txt"
    type "%TEMP%\smexe.txt" >> "%LOG%"
    goto :erro
)
echo   ok: o executavel responde

echo.
echo ====================================================================
echo  PRONTO: dist\survey_meshmapper\survey_meshmapper.exe
echo.
echo  Como usar:
echo     survey_meshmapper.exe Meshmapper_2026-09-10_12-59-11.kmz
echo.
echo  Saem tres arquivos na pasta atual: o KMZ com o rastro de calor,
echo  o PPT na identidade Anglo e o Excel com a aba de vizinhos.
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
