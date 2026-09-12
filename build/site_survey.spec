# PyInstaller spec do site_survey — a janela única: coleta ao vivo dos
# rádios e relatórios a partir de arquivos do MeshMapper.
#
# Use:  python -m PyInstaller --noconfirm --clean build/site_survey.spec
#
# Diferenças em relação aos outros dois specs, e o motivo de cada uma:
#
#  * rajant_api ENTRA (hiddenimports). Este exe fala com rádio — é o que
#    o survey_meshmapper.exe deliberadamente não faz. A importação é
#    dentro de try/except, então o PyInstaller não a acha sozinho.
#  * tkinter NÃO entra nos excludes: é a interface do programa. Herdar o
#    exclude do spec do rajant_monitor geraria um exe que abre e fecha na
#    hora, com o erro só no console que ninguém vê.
#  * prometheus_client ENTRA, mesmo este exe não expondo /metrics: o
#    rajant_monitor o importa no topo e sai se faltar. Excluí-lo uma vez
#    e o exe passou a fechar sozinho — ver o bloco de excludes.
#  * Sem servidor web em uso. O rajant_monitor entra como
#    BIBLIOTECA (parser, KMZ, PPT, Excel), nunca executado — o coletor de
#    métricas e a página web não têm por que vir junto.
#  * console=True mesmo tendo janela: os módulos também rodam por linha de
#    comando, e com console=False a saída some — o erro junto.
#
# A máquina que roda ESTE exe precisa de rede até a malha. Quem só gera
# relatório de arquivo continua tendo o survey_meshmapper.exe, que não
# carrega a rajant-api nem exige rede.
import os

datas = []
for pasta in ("marca", "exemplos"):
    if os.path.isdir(os.path.join("..", pasta)):
        datas.append((os.path.join("..", pasta), pasta))

a = Analysis(
    ["../site_survey.py"],
    pathex=[".."],
    binaries=[],
    datas=datas,
    # A rajant_api é importada dentro de try/except (para o módulo
    # carregar sem ela nas máquinas que só geram relatório), e o
    # PyInstaller não segue import condicional.
    #
    # tkinter entra EXPLÍCITO: ele é importado dentro de main(), e import
    # em corpo de função é justamente o que a análise estática às vezes
    # não alcança. Sem ele o exe abre, falha no import e fecha o console
    # antes de alguém ler o motivo — que foi o "não abre" relatado.
    hiddenimports=["rajant_api", "coleta_rajant", "survey_meshmapper",
                   "rajant_monitor",
                   "tkinter", "tkinter.ttk", "tkinter.filedialog",
                   "tkinter.messagebox", "tkinter.constants"],
    hookspath=[],
    runtime_hooks=[],
    # ATENÇÃO ao mexer aqui: excluir um módulo que o rajant_monitor importa
    # NO TOPO quebra o executável inteiro, e o sintoma não parece um erro
    # de empacotamento — o exe abre e fecha sozinho.
    #
    # Foi o que aconteceu com `prometheus_client`: excluí por raciocinar
    # que este exe não expõe /metrics, mas o rajant_monitor o importa no
    # import do módulo e SAI se faltar. Resultado no cliente:
    #   ERRO: pip install prometheus-client
    # com o pacote instalado na máquina — ele só não estava DENTRO do exe.
    #
    # Há teste que bloqueia cada nome desta lista e confere que o
    # site_survey ainda importa.
    excludes=["PyQt5", "PyQt6", "PySide2", "PySide6",
              "IPython", "jupyter", "notebook", "pytest", "sphinx",
              # A rajant-api DECLARA grpcio e NUNCA o importa. Sem este
              # exclude o PyInstaller tenta empacotar uma dependência que
              # nem sequer instala em Python 3.12+.
              "grpc", "grpcio", "grpcio_tools"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="site_survey",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False,
    upx=False,
    name="site_survey",
)
