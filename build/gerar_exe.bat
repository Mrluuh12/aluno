@echo off
REM ====================================================================
REM  Gera rajant_monitor.exe no Windows.
REM
REM  Rode a partir da pasta do projeto:   build\gerar_exe.bat
REM  Pode dar duplo clique: a janela NAO fecha sozinha, nem no erro.
REM
REM  Tudo o que acontece fica em  build\log_build.txt  - se der errado,
REM  mande esse arquivo, ele tem o erro completo.
REM
REM  PyInstaller NAO faz compilacao cruzada: .exe de Windows so sai de
REM  uma maquina Windows.
REM ====================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0.."
set "LOG=%~dp0log_build.txt"
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
    echo   Instale o Python marcando "Add Python to PATH" e abra um
    echo   Prompt de Comando NOVO depois de instalar.
    goto :erro
)
for /f "delims=" %%v in ('python --version 2^>^&1') do echo   %%v

REM ---------------------------------------------------------------- 2
set "PASSO=instalar as dependencias"
echo.
echo [2/5] Instalando dependencias... ^(alguns minutos^)
echo       saida detalhada indo para o log
python -m pip install --upgrade pip >> "%LOG%" 2>&1
python -m pip install pyinstaller prometheus_client matplotlib numpy scipy python-pptx openpyxl lxml >> "%LOG%" 2>&1
if errorlevel 1 (
    echo   ERRO ao instalar as dependencias.
    goto :erro
)
echo   ok

REM ---------------------------------------------------------------- 3
REM  A rajant-api declara grpcio==1.56.2, grpcio-tools==1.56.2 e
REM  protobuf==4.23.4, mas o codigo dela NAO importa grpc em lugar
REM  nenhum. So o protobuf e usado de verdade. Isso importa porque o
REM  grpcio 1.56.2 nao tem wheel para Python 3.12+: instalar as
REM  dependencias declaradas falharia tentando compilar C++.
REM
REM  NAO junte estas linhas com as do passo 2: o --no-deps vale para o
REM  COMANDO INTEIRO, e o matplotlib entraria sem packaging, pyparsing
REM  e pillow. O erro so apareceria depois, ao gerar a primeira imagem.
set "PASSO=instalar a rajant-api"
echo.
echo [3/5] Instalando a rajant-api e o protobuf...
python -m pip install rajant-api --no-deps >> "%LOG%" 2>&1
python -m pip install "protobuf==4.23.4" >> "%LOG%" 2>&1
if errorlevel 1 (
    echo   ERRO ao instalar o protobuf. Sem ele a rajant-api nao importa.
    goto :erro
)

REM  Confere pelo MESMO caminho que o programa usa, importando o
REM  rajant_monitor - NAO a rajant_api direto. A rajant-api 0.1.1 faz
REM  `from ssl import wrap_socket`, funcao removida no Python 3.12;
REM  o rajant_monitor instala um shim compativel antes de importa-la.
REM  Conferir com `import rajant_api` acusa uma falha que nao afeta o
REM  programa.
python -c "import sys; sys.argv=['x']; import rajant_monitor as m; print('   ok:', m.Breadcrumb)" > "%TEMP%\rjchk.txt" 2>&1
if errorlevel 1 (
    echo   ERRO ao importar o programa:
    type "%TEMP%\rjchk.txt"
    type "%TEMP%\rjchk.txt" >> "%LOG%"
    echo.
    echo   Causas comuns:
    echo     "No module named 'google'"           -^> falta o protobuf
    echo     "No module named 'prometheus_client'" -^> falhou o passo 2
    goto :erro
)
type "%TEMP%\rjchk.txt"
type "%TEMP%\rjchk.txt" >> "%LOG%"

REM ---------------------------------------------------------------- 4
set "PASSO=compilar com o PyInstaller"
echo.
echo [4/5] Compilando com o PyInstaller...
echo.
echo       ISTO DEMORA. De 3 a 10 minutos, dependendo da maquina.
echo       A tela fica parada nesse tempo - e normal, NAO travou.
echo       Para acompanhar, abra o log noutra janela.
echo.
REM  `python -m PyInstaller`, NAO `pyinstaller` direto: quando o pip cai
REM  em "user installation" (site-packages do sistema sem permissao de
REM  escrita), os executaveis vao para
REM  ...\AppData\Roaming\Python\PythonXX\Scripts, que normalmente NAO
REM  esta no PATH. O comando some e o build morre com "is not recognized",
REM  mesmo com o pyinstaller instalado. Pelo modulo, usa o mesmo Python
REM  que instalou o pacote e o PATH deixa de importar.
python -m PyInstaller --noconfirm --clean "build\rajant_monitor.spec" >> "%LOG%" 2>&1
if errorlevel 1 (
    echo   ERRO na compilacao.
    goto :erro
)
if not exist "dist\rajant_monitor\rajant_monitor.exe" (
    echo   ERRO: o PyInstaller terminou sem erro, mas o .exe nao apareceu.
    goto :erro
)
echo   ok

REM ---------------------------------------------------------------- 5
set "PASSO=copiar os arquivos externos"
echo.
echo [5/5] Copiando os arquivos que ficam FORA do exe...
REM  Editaveis sem recompilar: e por isso que estao de fora.
if exist painel                    xcopy /E /I /Y painel dist\rajant_monitor\painel >> "%LOG%" 2>&1
REM  Logos da identidade Anglo: sem eles o deck sai sem marca.
if exist marca                     xcopy /E /I /Y marca dist\rajant_monitor\marca >> "%LOG%" 2>&1
if exist Relatorio_Semanal_Rede.pptx copy /Y Relatorio_Semanal_Rede.pptx dist\rajant_monitor\ >> "%LOG%" 2>&1
if exist config.ini                copy /Y config.ini dist\rajant_monitor\ >> "%LOG%" 2>&1
echo   ok

REM  Prova de fogo: o binario responde?
echo.
echo Conferindo o executavel...
pushd dist\rajant_monitor
rajant_monitor.exe --help > "%TEMP%\rjexe.txt" 2>&1
set "RC=%ERRORLEVEL%"
popd
if not "%RC%"=="0" (
    echo   ERRO: o .exe foi gerado mas nao roda.
    type "%TEMP%\rjexe.txt"
    type "%TEMP%\rjexe.txt" >> "%LOG%"
    goto :erro
)
echo   ok: o executavel responde

echo.
echo ====================================================================
echo  PRONTO: dist\rajant_monitor\rajant_monitor.exe
echo.
echo  ATENCAO: rode o exe SEMPRE a partir da pasta dele. O config.ini, o
echo  banco surveys.db e o cache sao criados no diretorio ATUAL, nao ao
echo  lado do executavel.
echo.
echo  Sem argumento, o programa SOBE O SERVICO e fica rodando - nao e
echo  travamento. Para so testar:  rajant_monitor.exe --help
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
