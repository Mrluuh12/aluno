# PyInstaller spec do rajant_monitor.
#
# Use:  pyinstaller --noconfirm --clean build/rajant_monitor.spec
#
# Escolhas que não são óbvias, todas verificadas construindo e RODANDO o
# binário (não só compilando):
#
#  * onedir, não onefile. Em onefile os ~200 MB de scipy/matplotlib são
#    extraídos para o temp a cada inicialização, o que atrasa a subida do
#    serviço; e antivírus corporativo costuma implicar com o extrator.
#  * collect_data('pptx'): python-pptx carrega templates/*.xml e .emf do
#    disco. Sem isso, Presentation() estoura só na hora de gerar o PPT —
#    ou seja, semanas depois, na frente do cliente.
#  * collect_data('matplotlib'): matplotlibrc e as fontes.
#  * rajant_api em hiddenimports: é importada dentro de try/except, então
#    a análise estática do PyInstaller não a enxerga.
#  * excludes de GUI: tkinter/Qt entram de carona por matplotlib e somam
#    centenas de MB sem nenhum uso — o programa não tem interface gráfica.

from PyInstaller.utils.hooks import collect_data_files

datas = collect_data_files("pptx") + collect_data_files("matplotlib")

# O painel HTML fica FORA do executável de propósito: é editável sem
# recompilar, que é a razão de ele existir. Copie a pasta painel/ para o
# lado do .exe.

a = Analysis(
    ["../rajant_monitor.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=["rajant_api"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6",
              "IPython", "jupyter", "notebook", "pytest", "sphinx"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="rajant_monitor",
    debug=False,
    strip=False,
    upx=False,          # UPX dispara heurística de antivírus; não vale a pena
    console=True,       # é serviço de linha de comando: precisa do console
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False,
    name="rajant_monitor",
)
