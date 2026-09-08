"""Diagnóstico do ambiente do rajant_monitor.

Chamado por `build\\diagnostico.bat`, mas roda sozinho:

    python build/diagnostico.py

Escreve `build/diagnostico.txt`. Não instala nem altera nada.

Está em Python, e não dentro do .bat, por dois motivos: dá para testar, e
uma checagem por processo (13 chamadas a `python -c`) levava dezenas de
segundos no Windows — quem fechasse antes ficava com meio relatório.
"""
import importlib
import os
import platform
import subprocess
import sys
from pathlib import Path

MODULOS = ["prometheus_client", "matplotlib", "numpy", "scipy", "pptx",
           "openpyxl", "lxml", "google.protobuf", "packaging", "pyparsing",
           "PIL", "xlsxwriter", "PyInstaller", "rajant_api"]

ARQUIVOS = [("rajant_monitor.py", True), ("build/rajant_monitor.spec", True),
            ("painel/visao-geral.html", True),
            ("Relatorio_Semanal_Rede.pptx", False), ("config.ini", False)]


def _rodar(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        return (r.stdout + r.stderr).strip() or "(sem saida)"
    except Exception as e:
        return f"({type(e).__name__}: {e})"


def coletar(raiz=None):
    raiz = Path(raiz or ".").resolve()
    L = [f"DIAGNOSTICO rajant_monitor",
         f"pasta do projeto: {raiz}",
         "=" * 68, "",
         "--- Sistema ---",
         f"  {platform.platform()}",
         f"  python  : {sys.version.split()[0]}  ({sys.executable})",
         f"  64 bits : {sys.maxsize > 2**32}", ""]

    # O py launcher pode apontar para uma versao SEM os pacotes: e a
    # armadilha de quem tem varios Python instalados.
    L += ["--- Todos os Python instalados ---", "  " +
          _rodar(["py", "-0p"]).replace("\n", "\n  "), ""]

    L += ["--- ssl.wrap_socket (removido no Python 3.12+) ---"]
    import ssl
    L += [f"  existe nativo: {hasattr(ssl, 'wrap_socket')}", ""]

    L += ["--- Modulos ---"]
    faltando = []
    for n in MODULOS:
        try:
            m = importlib.import_module(n)
            L.append(f"  OK    {n:<20} {getattr(m, '__version__', '(sem versao)')}")
        except Exception as e:
            L.append(f"  FALTA {n:<20} {type(e).__name__}: {e}")
            faltando.append(n)
    L.append("")

    # rajant_api importada DIRETO falha em 3.12+; pelo programa, funciona.
    L += ["--- Os dois caminhos de import ---"]
    try:
        importlib.import_module("rajant_api")
        L.append("  direto        : OK")
    except Exception as e:
        L.append(f"  direto        : falhou ({type(e).__name__}) "
                 f"— esperado em Python 3.12+")
    sys.argv = ["x"]
    sys.path.insert(0, str(raiz))
    try:
        m = importlib.import_module("rajant_monitor")
        L.append(f"  via programa  : OK  ({m.Breadcrumb})")
    except Exception as e:
        L.append(f"  via programa  : FALHOU  {type(e).__name__}: {e}")
        L.append("                  ^ este e o que precisa funcionar")
    L.append("")

    # pip instala em "user site" quando o site-packages do sistema nao e
    # gravavel, e a pasta Scripts dele costuma ficar fora do PATH — foi o
    # que fez o `pyinstaller` sumir no build.
    L += ["--- Comandos no PATH ---"]
    import sysconfig
    try:
        scripts = sysconfig.get_path("scripts", os.name + "_user")
    except Exception:
        scripts = "(nao determinado)"
    L.append(f"  Scripts do usuario: {scripts}")
    L.append(f"  esta no PATH?     : "
             f"{str(scripts).lower() in os.environ.get('PATH', '').lower()}")
    L.append(f"  pyinstaller direto: {_rodar(['pyinstaller', '--version'])}")
    L.append(f"  python -m PyInstaller: "
             f"{_rodar([sys.executable, '-m', 'PyInstaller', '--version'])}")
    L.append("")

    L += ["--- Arquivos do projeto ---"]
    for rel, obrig in ARQUIVOS:
        p = raiz / rel
        marca = "OK   " if p.exists() else ("FALTA" if obrig else "-    ")
        extra = "" if obrig else "  (opcional)"
        L.append(f"  {marca} {rel}{extra}")
    L.append("")

    exe = raiz / "dist" / "rajant_monitor" / (
        "rajant_monitor.exe" if os.name == "nt" else "rajant_monitor")
    L += ["--- Executavel ---"]
    if exe.exists():
        L.append(f"  existe: {exe}")
        L.append("  --help: " + _rodar([str(exe), "--help"]).split("\n")[0])
    else:
        L.append("  ainda nao gerado")
    L.append("")

    log = raiz / "build" / "log_build.txt"
    L += ["--- Fim do log de build ---"]
    if log.exists():
        L += ["  " + l for l in log.read_text(errors="replace"
                                              ).splitlines()[-40:]]
    else:
        L.append("  sem log_build.txt")

    L += ["", "=" * 68]
    if faltando:
        L.append(f"RESUMO: faltam {len(faltando)} modulo(s): "
                 f"{', '.join(faltando)}")
    else:
        L.append("RESUMO: todos os modulos presentes")
    return "\n".join(L)


def main():
    raiz = Path(__file__).resolve().parent.parent
    os.chdir(raiz)
    texto = coletar(raiz)
    saida = raiz / "build" / "diagnostico.txt"
    saida.write_text(texto, encoding="utf-8", errors="replace")
    print(texto)
    print(f"\nRelatorio salvo em: {saida}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
