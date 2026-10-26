# PyInstaller spec do site_survey — a janela única: coleta ao vivo dos
# rádios e relatórios a partir de arquivos do MeshMapper.
#
# Use:  python -m PyInstaller --noconfirm --clean build/site_survey.spec
#
# O motivo de cada escolha:
#
#  * rajant_api ENTRA (hiddenimports). A importação é dentro de
#    try/except, então o PyInstaller não a acha sozinho.
#  * tkinter NÃO entra nos excludes: é a interface do programa. Sem ele o
#    exe abre e fecha na hora, com o erro só no console que ninguém vê.
#  * motor_survey é o motor (parser, KMZ, PPT, Excel). Ele não tem coletor
#    de métricas nem página web: é um projeto separado do rajant_monitor.
#  * console=True mesmo tendo janela: os módulos também rodam por linha de
#    comando, e com console=False a saída some — o erro junto.
#
# A aba Coleta precisa de rede até a malha; a aba Arquivos não.
import os

datas = []
for pasta in ("marca", "exemplos"):
    if os.path.isdir(os.path.join("..", pasta)):
        datas.append((os.path.join("..", pasta), pasta))
# Tema da janela (Sun Valley): o .tcl e as imagens vão como dados. Sem o
# pacote na máquina do build, a janela usa o tema de reserva.
try:
    from PyInstaller.utils.hooks import collect_data_files
    datas += collect_data_files("sv_ttk")
except Exception:
    pass

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
    # Common_pb2 (a mensagem Trace) e text_format são importados dentro
    # de função, no TRACE da coleta: declarados aqui para não dependerem
    # de o PyInstaller achá-los sozinho.
    hiddenimports=["rajant_api", "rajant_api.Common_pb2",
                   "rajant_api.Message_pb2", "rajant_api.State_pb2",
                   "google.protobuf.text_format",
                   "coleta_rajant", "survey_meshmapper",
                   "motor_survey",
                   "tkinter", "tkinter.ttk", "tkinter.filedialog",
                   "tkinter.messagebox", "tkinter.constants",
                   "sv_ttk"],
    hookspath=[],
    runtime_hooks=[],
    # ATENÇÃO ao mexer aqui: excluir um módulo que o motor_survey importa
    # NO TOPO quebra o executável inteiro, e o sintoma não parece um erro
    # de empacotamento — o exe abre e fecha sozinho.
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
