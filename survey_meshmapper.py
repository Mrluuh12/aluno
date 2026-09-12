#!/usr/bin/env python3
"""
Gerador de relatório de site survey a partir de capturas do MeshMapper.

Duplo clique no executável abre a janela: escolha os arquivos, escolha a
pasta de saída, clique em Gerar. Saem o KMZ com o rastro de calor, o PPT
na identidade Anglo e o Excel.

Também funciona por linha de comando:

    survey_meshmapper.exe captura1.kmz captura2.kmz -o relatorios

Não fala com rádio nenhum: entra arquivo, sai relatório. Roda no notebook
de quem fez a medição, sem acesso à mina e sem a rajant-api.
"""
import sys, argparse, traceback, threading, queue
from pathlib import Path

# O motor de saída (KMZ, PPT, Excel, gráficos) é o do rajant_monitor: são
# ~4 mil linhas já corrigidas contra armadilhas reais — ordem dos
# elementos no KML, escape do XML, borda de célula no PPTX, transparência
# do raster. Reescrever aqui seria refazer os mesmos bugs.
try:
    import rajant_monitor as rm
except ImportError:
    print("ERRO: rajant_monitor.py precisa estar na mesma pasta.")
    sys.exit(1)

TITULO = "Site Survey — Relatórios do MeshMapper"
EXTENSOES = [("Captura do MeshMapper", "*.kmz *.json *.csv"),
             ("KMZ do MeshMapper", "*.kmz"),
             ("CSV do MeshMapper", "*.csv"),
             ("Todos os arquivos", "*.*")]


# ══════════════════════════════════════════════════════════════
# O TRABALHO — sem nada de interface, para poder ser testado
# ══════════════════════════════════════════════════════════════
def gerar(arquivos, saida=None, nome=None, juntar=True, bandas=None,
          fazer_kmz=True, fazer_ppt=True, fazer_excel=True, aviso=print):
    """Lê as capturas e grava os relatórios. Devolve a lista de arquivos.

    `juntar=True` produz UM relatório com tudo — é o caso da campanha de
    survey, vários veículos cobrindo a mina. `juntar=False` produz um
    conjunto por arquivo, para quando cada captura é um laudo próprio.

    `aviso` recebe cada linha de progresso; a interface passa a própria
    função para escrever na janela.
    """
    arquivos = [str(a) for a in arquivos]
    if not arquivos:
        raise RuntimeError("nenhum arquivo escolhido")
    destino = Path(saida or ".").resolve()
    destino.mkdir(parents=True, exist_ok=True)
    cfg = rm.carregar_config(); rm.cfg_relatorio(cfg)

    if not juntar:
        feitos = []
        for i, arq in enumerate(arquivos, 1):
            aviso(f"[{i}/{len(arquivos)}] {Path(arq).name}")
            try:
                # Subpasta por captura: dois arquivos do MESMO veiculo na
                # MESMA hora produzem nomes de saida iguais, e um
                # sobrescrevia o outro em silencio — a contagem dizia 6
                # relatorios e o disco tinha 3.
                sub = destino / rm._slug_arquivo(Path(arq).stem)[:60]
                sub.mkdir(parents=True, exist_ok=True)
                feitos += _um(arq, sub, cfg, nome, bandas,
                              fazer_kmz, fazer_ppt, fazer_excel, aviso)
            except Exception as e:
                # Um arquivo ruim não pode levar os outros junto.
                aviso(f"    ! falhou: {e}")
        return feitos

    aviso(f"Lendo {len(arquivos)} arquivo(s)…")
    sid, sv, amostras, peers, origens = rm.importar_meshmapper_varios(
        arquivos, nome=nome)
    for o in origens:
        if o["ok"]:
            aviso(f"  ok      {o['arquivo']} — {o.get('movel')}, "
                  f"{o['amostras']} amostras")
        else:
            aviso(f"  FALHOU  {o['arquivo']} — {o.get('erro')}")
    return _saidas(sv, amostras, peers, sid, destino, cfg, bandas,
                   fazer_kmz, fazer_ppt, fazer_excel, aviso)


def _um(arq, destino, cfg, nome, bandas, kmz, ppt, xls, aviso):
    sid, sv, amostras, peers = rm.importar_meshmapper(arq, nome=nome)
    sv["origens"] = [{"arquivo": Path(arq).name, "ok": True,
                      "movel": sv.get("movel"), "amostras": len(amostras),
                      "peers": len(peers), "inicio": sv.get("inicio"),
                      "fim": sv.get("fim")}]
    return _saidas(sv, amostras, peers, sid, destino, cfg, bandas,
                   kmz, ppt, xls, aviso)


def _kmz_por_banda(sv, amostras, peers, destino, cfg, campos, aviso):
    """Um KMZ por banda. Devolve os arquivos gravados.

    2,4 e 5,8 GHz são malhas diferentes no mesmo terreno: num arquivo só,
    a banda boa tapa a ruim e o mapa deixa de dizer qual das duas está
    servindo. Separadas, dá para ligar uma de cada vez no Google Earth.

    A cor de cada ponto continua saindo da MELHOR ERB/ERM visível ali —
    `sinal_cob`, a primeira aba de cada arquivo.

    `peers` só vai adiante quando [relatorio] vizinhanca está ligado; sem
    isso o arquivo não ganha a pasta por repetidora.
    """
    com_viz = rm._cfg_bool(cfg, "relatorio", "vizinhanca", False)
    bandas_pres = sorted({rm._norm_banda(a.get("banda")) for a in amostras
                          if a.get("banda")}) or [None]
    feitos = []
    for b in bandas_pres:
        try:
            dados, nome = rm.gerar_kml_survey(
                sv, amostras, cfg=cfg, campos=campos, banda=b,
                peers=(peers if com_viz else None))
        except Exception as e:
            aviso(f"  ! KMZ de {b or 'todas as bandas'} não gerado: {e}")
            continue
        alvo = destino / nome
        alvo.write_bytes(dados); feitos.append(alvo)
    return feitos


def _saidas(sv, amostras, peers, sid, destino, cfg, bandas,
            fazer_kmz, fazer_ppt, fazer_excel, aviso):
    campos = rm.campos_com_medicao(amostras)
    faltando = [c for c in rm.CAMPOS_KMZ if c not in campos]
    dur = ((sv["fim"] - sv["inicio"]) / 60.0) if sv.get("fim") and sv.get("inicio") else 0
    # Uma captura feita de um ponto fixo — o MeshMapper ligado numa
    # repetidora — não é um trajeto. Rastro de calor dela sairia como uma
    # mancha de um pixel pintada com a escala de área, que parece mapa e
    # não é. O produto dela é outro: quem fala com aquele rádio e como.
    sitios = rm.sitios_parados(amostras, peers)
    parados = {s["nome"] for s in sitios}
    andando = sorted({a.get("radio") for a in amostras} - parados)

    aviso(f"  equipamento(s) ..... {sv.get('movel')}")
    aviso(f"  amostras ........... {len(amostras)}")
    aviso(f"  vizinhos lidos ..... {len(peers)}")
    aviso(f"  duração ............ {dur:.1f} min")
    aviso(f"  intervalo real ..... {rm._mm_intervalo_real(amostras)} s")
    for s in sitios:
        r = s["resumo"]
        aviso(f"  captura parada ..... {s['nome']} (raio {s['raio_m']:g} m) — "
              f"{r['vizinhos']} vizinhos, {r['infra']} de infraestrutura")
    aviso(f"  grandezas medidas .. {', '.join(campos) or 'nenhuma'}")
    if faltando:
        # Dizer o que NÃO veio evita a leitura errada de "mediu e deu
        # ruim" onde o certo é "o MeshMapper não mede isso".
        aviso(f"  sem medição ........ {', '.join(faltando)}")

    feitos = []
    if fazer_kmz:
        if campos and andando:
            feitos += _kmz_por_banda(sv, amostras, peers, destino, cfg,
                                     campos, aviso)
        elif not campos:
            aviso("  ! sem grandeza medida: KMZ do trajeto não gerado")
        else:
            aviso("  · nenhum equipamento em deslocamento: "
                  "sem rastro de calor")
        if sitios and rm._cfg_bool(cfg, "relatorio", "vizinhanca", False):
            try:
                dados, nome_f = rm.gerar_kml_pontos_fixos(sitios, cfg=cfg)
                alvo = destino / nome_f
                alvo.write_bytes(dados); feitos.append(alvo)
            except Exception as e:
                aviso(f"  ! KMZ da vizinhança não gerado: {e}")
    if fazer_ppt:
        try:
            dados, nome_ppt = rm.ppt_survey_anglo(sid, cfg, bandas,
                                                  sitios=sitios)
            alvo = destino / nome_ppt
            alvo.write_bytes(dados); feitos.append(alvo)
        except Exception as e:
            aviso(f"  ! PPT não gerado: {e}")
    if fazer_excel:
        try:
            dados, nome_xls = rm.excel_do_meshmapper(sv, amostras, peers, cfg)
            alvo = destino / nome_xls
            alvo.write_bytes(dados); feitos.append(alvo)
        except Exception as e:
            aviso(f"  ! Excel não gerado: {e}")
    for f in feitos:
        aviso(f"  → {f.name}")
    return feitos


def abrir_pasta(caminho):
    """Abre a pasta no explorador do sistema. Falhar aqui não é erro:
    os arquivos já estão gravados."""
    import subprocess, os
    try:
        if sys.platform.startswith("win"): os.startfile(str(caminho))
        elif sys.platform == "darwin":     subprocess.Popen(["open", str(caminho)])
        else:                              subprocess.Popen(["xdg-open", str(caminho)])
        return True
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════
# INTERFACE
# ══════════════════════════════════════════════════════════════
# tkinter, não Qt: vem junto com o Python do Windows, então não há mais
# uma dependência para instalar na máquina de quem só quer o relatório.
#
# O trabalho roda em thread separada e conversa com a janela por uma
# fila. Rodando na thread da interface, a janela congelaria durante a
# geração — e uma janela congelada parece travada, o usuário fecha e
# perde o trabalho no meio.
def interface():
    try:
        import tkinter as tk
        from tkinter import ttk, filedialog, messagebox
    except ImportError:
        print("ERRO: este Python não tem tkinter (a interface gráfica).\n"
              "      No Windows, reinstale o Python marcando 'tcl/tk'.\n"
              "      Enquanto isso, use pela linha de comando:\n"
              "      survey_meshmapper arquivo.kmz -o pasta_de_saida")
        return 1

    ANGLO_AZUL = "#" + rm.ANGLO["azul"]
    FUNDO      = "#" + rm.ANGLO["fundo"]
    CARTAO     = "#" + rm.ANGLO["cartao"]
    TEXTO      = "#" + rm.ANGLO["texto"]
    SUAVE      = "#" + rm.ANGLO["suave"]

    jan = tk.Tk()
    jan.title(TITULO)
    jan.geometry("880x640")
    jan.minsize(760, 560)
    jan.configure(bg=FUNDO)

    estado = {"arquivos": [], "saida": str(Path.home() / "Documents"),
              "rodando": False}
    fila = queue.Queue()

    # ── cabeçalho ──
    topo = tk.Frame(jan, bg=ANGLO_AZUL, height=64)
    topo.pack(fill="x"); topo.pack_propagate(False)
    tk.Label(topo, text="Site Survey", bg=ANGLO_AZUL, fg="white",
             font=("Segoe UI", 17, "bold")).pack(side="left", padx=18)
    tk.Label(topo, text="MeshMapper", bg=ANGLO_AZUL, fg="#C7CEDD",
             font=("Segoe UI", 10)).pack(side="left", pady=(6, 0))

    corpo = tk.Frame(jan, bg=FUNDO, padx=16, pady=12)
    corpo.pack(fill="both", expand=True)

    # ── arquivos ──
    tk.Label(corpo, text="1. Arquivos da captura", bg=FUNDO, fg=TEXTO,
             font=("Segoe UI", 10, "bold")).pack(anchor="w")
    tk.Label(corpo, text=".kmz, data.json ou os .csv",
             bg=FUNDO, fg=SUAVE, font=("Segoe UI", 9)).pack(anchor="w")

    cx = tk.Frame(corpo, bg=FUNDO); cx.pack(fill="both", expand=True, pady=(6, 0))
    barra = tk.Scrollbar(cx)
    lista = tk.Listbox(cx, selectmode="extended", bg=CARTAO, fg=TEXTO,
                       font=("Consolas", 9), relief="flat",
                       highlightthickness=1, highlightbackground="#D6DAE3",
                       yscrollcommand=barra.set)
    barra.config(command=lista.yview)
    lista.pack(side="left", fill="both", expand=True)
    barra.pack(side="right", fill="y")

    def redesenhar():
        lista.delete(0, "end")
        for a in estado["arquivos"]:
            lista.insert("end", f"  {Path(a).name}")
        bt_gerar.config(state=("normal" if estado["arquivos"]
                               and not estado["rodando"] else "disabled"))
        lbl_conta.config(text=(f"{len(estado['arquivos'])} arquivo(s)"
                               if estado["arquivos"] else "nenhum arquivo"))

    def escolher():
        novos = filedialog.askopenfilenames(title="Escolher capturas do MeshMapper",
                                            filetypes=EXTENSOES)
        for n in novos:
            if n not in estado["arquivos"]:
                estado["arquivos"].append(n)
        redesenhar()

    def remover():
        for i in sorted(lista.curselection(), reverse=True):
            estado["arquivos"].pop(i)
        redesenhar()

    def limpar():
        estado["arquivos"].clear(); redesenhar()

    linha = tk.Frame(corpo, bg=FUNDO); linha.pack(fill="x", pady=(8, 0))

    def botao(pai, texto, cmd, principal=False):
        b = tk.Button(pai, text=texto, command=cmd, relief="flat",
                      cursor="hand2", padx=14, pady=6,
                      font=("Segoe UI", 9, "bold" if principal else "normal"),
                      bg=(ANGLO_AZUL if principal else CARTAO),
                      fg=("white" if principal else TEXTO),
                      activebackground=(ANGLO_AZUL if principal else "#E4E8F0"),
                      activeforeground=("white" if principal else TEXTO))
        b.pack(side="left", padx=(0, 8))
        return b

    botao(linha, "Escolher arquivos…", escolher, principal=True)
    botao(linha, "Remover selecionado", remover)
    botao(linha, "Limpar", limpar)
    lbl_conta = tk.Label(linha, text="nenhum arquivo", bg=FUNDO, fg=SUAVE,
                         font=("Segoe UI", 9))
    lbl_conta.pack(side="right")

    # ── saída e opções ──
    tk.Label(corpo, text="2. Onde salvar", bg=FUNDO, fg=TEXTO,
             font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(14, 0))
    ls = tk.Frame(corpo, bg=FUNDO); ls.pack(fill="x", pady=(4, 0))
    var_saida = tk.StringVar(value=estado["saida"])
    tk.Entry(ls, textvariable=var_saida, bg=CARTAO, fg=TEXTO, relief="flat",
             font=("Segoe UI", 9), highlightthickness=1,
             highlightbackground="#D6DAE3").pack(side="left", fill="x",
                                                 expand=True, ipady=5)
    tk.Button(ls, text="Procurar…", relief="flat", bg=CARTAO, fg=TEXTO,
              cursor="hand2", padx=12, pady=4, font=("Segoe UI", 9),
              command=lambda: var_saida.set(
                  filedialog.askdirectory(title="Pasta de saída")
                  or var_saida.get())).pack(side="left", padx=(8, 0))

    op = tk.Frame(corpo, bg=FUNDO); op.pack(fill="x", pady=(10, 0))
    var_juntar = tk.BooleanVar(value=True)
    var_kmz = tk.BooleanVar(value=True)
    var_ppt = tk.BooleanVar(value=True)
    var_xls = tk.BooleanVar(value=True)

    def check(texto, var, dica=None):
        c = tk.Checkbutton(op, text=texto, variable=var, bg=FUNDO, fg=TEXTO,
                           activebackground=FUNDO, selectcolor=FUNDO,
                           font=("Segoe UI", 9), cursor="hand2")
        c.pack(side="left", padx=(0, 14))
        return c

    check("Juntar tudo num relatório só", var_juntar)
    check("KMZ", var_kmz); check("PPT", var_ppt); check("Excel", var_xls)

    # ── log ──
    tk.Label(corpo, text="3. Andamento", bg=FUNDO, fg=TEXTO,
             font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(14, 0))
    cl = tk.Frame(corpo, bg=FUNDO); cl.pack(fill="both", expand=True, pady=(4, 0))
    barra2 = tk.Scrollbar(cl)
    log = tk.Text(cl, height=9, bg=CARTAO, fg=TEXTO, relief="flat",
                  font=("Consolas", 9), wrap="none", state="disabled",
                  highlightthickness=1, highlightbackground="#D6DAE3",
                  yscrollcommand=barra2.set)
    barra2.config(command=log.yview)
    log.pack(side="left", fill="both", expand=True)
    barra2.pack(side="right", fill="y")

    def escreve(txt):
        log.config(state="normal"); log.insert("end", str(txt) + "\n")
        log.see("end"); log.config(state="disabled")

    # ── ação ──
    rodape = tk.Frame(corpo, bg=FUNDO); rodape.pack(fill="x", pady=(12, 0))
    prog = ttk.Progressbar(rodape, mode="indeterminate", length=200)

    def trabalhar():
        try:
            feitos = gerar(estado["arquivos"], var_saida.get(),
                           juntar=var_juntar.get(),
                           fazer_kmz=var_kmz.get(), fazer_ppt=var_ppt.get(),
                           fazer_excel=var_xls.get(),
                           aviso=lambda t: fila.put(("log", t)))
            fila.put(("fim", feitos))
        except Exception as e:
            fila.put(("erro", str(e)))

    def gerar_clicado():
        if estado["rodando"]: return
        estado["rodando"] = True
        bt_gerar.config(state="disabled", text="Gerando…")
        prog.pack(side="right"); prog.start(12)
        log.config(state="normal"); log.delete("1.0", "end")
        log.config(state="disabled")
        threading.Thread(target=trabalhar, daemon=True).start()

    def bombear():
        # A interface só é tocada por AQUI. tkinter não é seguro entre
        # threads: escrever na janela de dentro do worker trava ou some
        # com o texto, e o sintoma não parece com a causa.
        try:
            while True:
                tipo, dado = fila.get_nowait()
                if tipo == "log":
                    escreve(dado)
                elif tipo == "fim":
                    prog.stop(); prog.pack_forget()
                    estado["rodando"] = False
                    bt_gerar.config(state="normal", text="Gerar relatórios")
                    escreve("")
                    escreve(f"Pronto: {len(dado)} arquivo(s) em {var_saida.get()}")
                    if dado and messagebox.askyesno(
                            TITULO, f"{len(dado)} arquivo(s).  Abrir a pasta?"):
                        abrir_pasta(var_saida.get())
                elif tipo == "erro":
                    prog.stop(); prog.pack_forget()
                    estado["rodando"] = False
                    bt_gerar.config(state="normal", text="Gerar relatórios")
                    escreve(f"ERRO: {dado}")
                    messagebox.showerror(TITULO, str(dado))
        except queue.Empty:
            pass
        jan.after(120, bombear)

    bt_gerar = tk.Button(rodape, text="Gerar relatórios", command=gerar_clicado,
                         relief="flat", cursor="hand2", padx=22, pady=9,
                         font=("Segoe UI", 10, "bold"),
                         bg=ANGLO_AZUL, fg="white", activebackground=ANGLO_AZUL,
                         activeforeground="white", state="disabled")
    bt_gerar.pack(side="left")

    redesenhar()
    jan.after(120, bombear)
    jan.mainloop()
    return 0


def main():
    ap = argparse.ArgumentParser(
        prog="survey_meshmapper", add_help=True,
        description="Relatório de site survey a partir de capturas do "
                    "MeshMapper. Sem argumentos, abre a janela.")
    ap.add_argument("arquivos", nargs="*",
                    help="capturas: .kmz, data.json ou os CSVs")
    ap.add_argument("-o", "--saida", help="pasta de saída (padrão: atual)")
    ap.add_argument("-n", "--nome", help="nome do survey no relatório")
    ap.add_argument("--separado", action="store_true",
                    help="um conjunto de relatórios por arquivo, em vez de "
                         "juntar tudo num só")
    ap.add_argument("--bandas", help="bandas a detalhar no PPT")
    ap.add_argument("--sem-ppt", action="store_true")
    ap.add_argument("--sem-excel", action="store_true")
    ap.add_argument("--sem-kmz", action="store_true")
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()

    # Sem argumento nenhum, abre a janela. Antes, o duplo clique no .exe
    # caía no erro de uso do argparse e a janela do console fechava na
    # hora — parecia que o programa não funcionava.
    if not a.arquivos:
        sys.exit(interface() or 0)

    bandas = [b.strip() for b in a.bandas.split(",")] if a.bandas else None
    try:
        feitos = gerar(a.arquivos, a.saida, a.nome, not a.separado, bandas,
                       not a.sem_kmz, not a.sem_ppt, not a.sem_excel)
        print(f"\n{len(feitos)} arquivo(s) gerado(s).")
    except Exception as e:
        print(f"\nERRO: {e}")
        if a.debug: traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
