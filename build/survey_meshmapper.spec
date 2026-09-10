# PyInstaller spec do survey_meshmapper — o gerador de relatório a partir
# de uma captura do MeshMapper.
#
# Use:  python -m PyInstaller --noconfirm --clean build/survey_meshmapper.spec
#
# Diferenças em relação ao spec do rajant_monitor, e o motivo de cada uma:
#
#  * SEM rajant_api nos hiddenimports. Este executável não fala com rádio
#    nenhum: entra arquivo, sai KMZ/PPT/Excel. Embutir a biblioteca traria
#    junto a exigência de protobuf 4.23.4 e o shim de ssl.wrap_socket,
#    tudo para nada.
#  * SEM prometheus_client, sem http.server em uso. O rajant_monitor é
#    importado como BIBLIOTECA (pelo motor de KMZ/PPT), não executado —
#    por isso o import da rajant_api foi tornado opcional lá.
#  * O rajant_monitor.py entra como módulo, não como script: o ponto de
#    entrada é o survey_meshmapper.py.
#
# Igual ao outro spec, e pelas mesmas razões:
#  * onedir, não onefile (scipy/matplotlib extraídos a cada subida, e
#    antivírus corporativo implica com o extrator).
#  * collect_data('pptx'): o python-pptx lê templates/*.xml do disco. Sem
#    isso, Presentation() só estoura na hora de gerar o PPT.
#  * collect_data('matplotlib'): matplotlibrc e as fontes.
#  * excludes de GUI: tkinter/Qt entram de carona por matplotlib e somam
#    centenas de MB sem uso.

from PyInstaller.utils.hooks import collect_data_files

datas = collect_data_files("pptx") + collect_data_files("matplotlib")

# A pasta marca/ (logos Anglo) fica FORA do executável, ao lado dele: sem
# ela o deck sai sem logo, em vez de estourar — e trocar o logo não deve
# exigir recompilar.

a = Analysis(
    ["../survey_meshmapper.py"],
    pathex=[".."],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6",
              "IPython", "jupyter", "notebook", "pytest", "sphinx",
              # Não fala com rádio: a lib e o gRPC que ela declara não
              # têm por que entrar.
              "rajant_api", "grpc", "grpcio"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="survey_meshmapper",
    debug=False,
    strip=False,
    upx=False,          # UPX dispara heurística de antivírus
    console=True,       # é ferramenta de linha de comando
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False,
    name="survey_meshmapper",
)
