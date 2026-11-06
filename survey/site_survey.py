#!/usr/bin/env python3
"""
Site Survey — a janela única.

Duas abas, os mesmos relatórios:

  • Coleta          fala com os rádios, descobre a malha, você marca os
                    equipamentos e coleta ao vivo
  • Arquivos        lê capturas do MeshMapper que alguém já trouxe

Saem KMZ com o rastro de calor, PPT na identidade Anglo e Excel.

A lógica não mora aqui: coleta em `coleta_rajant.py`, arquivos em
`survey_meshmapper.py`, relatórios em `motor_survey.py`. Este arquivo é
só a janela — assim cada pedaço continua testável sem abrir tela.

Pela linha de comando, use os módulos diretamente:

    coleta_rajant --seeds 10.188.96.140 --minutos 30 -o saida
    survey_meshmapper captura.kmz -o saida
"""
import sys, os, threading, queue, traceback
from pathlib import Path


# ──────────────────────────────────────────────────────────────
# Erro na partida TEM de ficar visível.
#
# Com o exe em modo console, um duplo clique abre a janela preta e ela
# FECHA no instante em que o programa termina. Qualquer falha de import
# some antes de alguém conseguir ler — e o que chega ao usuário é "não
# abre", sem nenhuma pista.
#
# Então todo erro de partida vai para um arquivo AO LADO DO EXE e o
# console espera uma tecla. Custa duas dúzias de linhas e transforma
# "não abre" em "abriu e disse o motivo".
# ──────────────────────────────────────────────────────────────
def _pasta_do_exe():
    # Congelado pelo PyInstaller, __file__ aponta para dentro do pacote
    # temporário; o que o usuário enxerga é a pasta do executável.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _morrer(titulo, detalhe):
    txt = f"{titulo}\n\n{detalhe}\n"
    try:
        alvo = _pasta_do_exe() / "site_survey_erro.txt"
        alvo.write_text(txt, encoding="utf-8")
        onde = f"\nGravado em: {alvo}"
    except Exception:
        onde = ""
    print("=" * 68)
    print(txt + onde)
    print("=" * 68)
    # A pausa é para o duplo clique, onde o console fecharia sozinho. Ela
    # NÃO pode acontecer quando ninguém está lá para apertar a tecla: o
    # passo de verificação do build roda `--verificar` de dentro do .bat,
    # e um input() ali penduraria a compilação para sempre.
    if "--verificar" not in sys.argv[1:] and sys.stdin and sys.stdin.isatty():
        try:
            input("\nPressione ENTER para fechar...")
        except Exception:
            pass
    sys.exit(1)


# BaseException, não Exception: os módulos chamam `sys.exit(1)` quando
# falta uma dependência, e SystemExit NÃO é Exception. Com `except
# Exception` o processo morria antes de gravar o arquivo de erro — foi
# assim que um `prometheus_client` faltando no exe virou "não abre".
try:
    import motor_survey as rm
    import survey_meshmapper as smm
    import coleta_rajant as col
except BaseException as e:
    _morrer("O programa não conseguiu carregar um módulo de que precisa.",
            f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}\n"
            "Se a mensagem acima pede um `pip install`, o pacote não está "
            "DENTRO do executável — instalá-lo na máquina não resolve.\n"
            "Gere o executável de novo com build\\gerar_exe_site_survey.bat.")

TITULO = "Site Survey — Rajant"
EXTENSOES = [("Captura do MeshMapper", "*.kmz *.json *.csv"),
             ("KMZ do MeshMapper", "*.kmz"),
             ("CSV do MeshMapper", "*.csv"),
             ("Todos os arquivos", "*.*")]


def abrir_pasta(caminho):
    import subprocess
    try:
        if sys.platform.startswith("win"): os.startfile(str(caminho))
        elif sys.platform == "darwin":     subprocess.Popen(["open", str(caminho)])
        else:                              subprocess.Popen(["xdg-open", str(caminho)])
        return True
    except Exception:
        return False


def verificar():
    """Confere tudo que a janela precisa, SEM abrir janela. Devolve 0/1.

    Existe para o build poder provar que o executável funciona. Antes,
    a única forma de saber era dar duplo clique — e se falhasse, o
    console fechava antes de qualquer mensagem.
    """
    problemas = []
    print("Verificação do site_survey\n")
    for rot, mod in (("motor de relatórios", "motor_survey"),
                     ("leitura de arquivos", "survey_meshmapper"),
                     ("coleta ao vivo", "coleta_rajant")):
        try:
            __import__(mod); print(f"  ok      {rot} ({mod})")
        except Exception as e:
            print(f"  FALHOU  {rot} ({mod}): {e}"); problemas.append(mod)

    try:
        import tkinter
        # Importar não basta: o tcl/tk pode faltar no disco e só quebrar
        # ao criar a primeira janela. Cria e destrói uma, sem mostrar.
        r = tkinter.Tk(); r.withdraw(); r.destroy()
        print("  ok      interface gráfica (tkinter + tcl/tk)")
    except Exception as e:
        print(f"  FALHOU  interface gráfica: {e}")
        problemas.append("tkinter")

    try:
        import sv_ttk  # noqa: F401
        print("  ok      tema da janela (sv_ttk)")
    except Exception:
        print("  aviso   sv_ttk ausente: a janela abre com o tema de reserva")

    try:
        import motor_survey as _rm
        print("  ok      rajant-api presente" if _rm.Breadcrumb is not None
              else "  aviso   rajant-api ausente: a aba Coleta não funciona, "
                   "a de Arquivos sim")
    except Exception:
        pass

    for pacote in ("matplotlib", "numpy", "scipy", "pptx", "openpyxl", "lxml"):
        try:
            __import__(pacote); print(f"  ok      {pacote}")
        except Exception as e:
            print(f"  FALHOU  {pacote}: {e}"); problemas.append(pacote)

    try:
        import laspy, lazrs  # noqa: F401
        print("  ok      terreno LAS/LAZ (laspy)")
    except Exception as e:
        print(f"  FALHOU  terreno LAS/LAZ (laspy): {e}"); problemas.append("laspy")

    # GeoTIFF: importar não basta. No exe, o que costuma faltar são os
    # dados de projeção (proj.db), e aí só a conversão de coordenadas
    # quebra. Converte um ponto de UTM 23S para graus.
    try:
        from rasterio import warp
        lon, lat = warp.transform("EPSG:31983", "EPSG:4326", [600000], [7900000])
        assert -45 < lon[0] < -43 and -20 < lat[0] < -18
        print("  ok      GeoTIFF e coordenadas (rasterio)")
    except Exception as e:
        print(f"  FALHOU  GeoTIFF (rasterio): {e}"); problemas.append("rasterio")

    print()
    if problemas:
        print(f"{len(problemas)} problema(s): {', '.join(problemas)}")
        return 1
    print("Tudo certo. A janela vai abrir.")
    return 0


def main():
    if "--verificar" in sys.argv[1:]:
        return verificar()
    if "--testar-trace" in sys.argv[1:] or "--testar-iperf" in sys.argv[1:]:
        # Diagnóstico de campo pela linha de comando, no mesmo executável:
        #   site_survey.exe --testar-trace 10.188.99.6 --senha ...
        import coleta_rajant as _col
        return _col.main(sys.argv[1:])
    try:
        import tkinter as tk
        from tkinter import ttk, filedialog, messagebox
    except Exception as e:
        # Aqui morava o "não abre": a mensagem ia para um console que
        # fechava no mesmo instante.
        _morrer(
            "Este programa não achou o tkinter (a interface gráfica).",
            f"{type(e).__name__}: {e}\n\n"
            "No Windows, reinstale o Python marcando 'tcl/tk' e gere o "
            "executável de novo.\n"
            "Enquanto isso, pela linha de comando funciona:\n"
            "   survey_meshmapper captura.kmz -o saida\n"
            "   coleta_rajant --seeds 10.188.96.140 --minutos 30 -o saida")

    AZUL   = "#" + rm.ANGLO["azul"]
    AZUL2  = "#" + rm.ANGLO["azul2"]
    SUAVE  = "#6B7280"
    VERM   = "#C0392B"

    cfg = rm.carregar_config()
    jan = tk.Tk()
    jan.title(TITULO)
    # A janela nasce cabendo na tela (768 px de altura, com a barra de
    # tarefas). Largura até 1600: com a escala do Windows em 125–150%, as
    # letras crescem e 1200 px cortava os botões da direita.
    larg = max(900, min(1600, jan.winfo_screenwidth() - 80))
    alt = max(600, min(860, jan.winfo_screenheight() - 90))
    jan.geometry(f"{larg}x{alt}")
    jan.minsize(900, 560)
    if sys.platform.startswith("win"):
        try:
            jan.state("zoomed")
        except Exception:
            pass
    fila = queue.Queue()
    est = {"coleta": None, "achados": {}, "arquivos": [], "lista": {}}

    # ── tema ──
    # Sun Valley: os mesmos controles do ttk com a cara do Windows 11. Sem
    # ele (exe antigo, pacote faltando), o "clam" com as cores da Anglo —
    # a janela abre do mesmo jeito.
    st = ttk.Style()
    try:
        import sv_ttk
        sv_ttk.set_theme("light")
        FUNDO = st.lookup("TFrame", "background") or "#FAFAFA"
        CARTAO = "#FFFFFF"
    except Exception:
        FUNDO, CARTAO = "#F3F4F6", "#FFFFFF"
        try: st.theme_use("clam")
        except Exception: pass
        # Fundo branco nos controles: os cartões são brancos e um bloco
        # cinza dentro deles parecia outra caixa.
        st.configure(".", background=CARTAO, font=("Segoe UI", 10))
        st.configure("Card.TFrame", background=CARTAO, relief="solid",
                     borderwidth=1)
        st.configure("Accent.TButton", background=AZUL, foreground="white",
                     font=("Segoe UI", 10, "bold"), padding=(16, 7),
                     borderwidth=0)
        st.map("Accent.TButton", background=[("active", AZUL2),
                                             ("disabled", "#9AA6C4")])
        st.configure("TButton", padding=(12, 5))
        st.configure("Treeview", rowheight=24, fieldbackground=CARTAO,
                     background=CARTAO)
        st.configure("TNotebook.Tab", padding=(18, 6))
    st.configure("Titulo.TLabel", font=("Segoe UI", 11, "bold"))
    st.configure("Fraco.TLabel", foreground=SUAVE, font=("Segoe UI", 9))
    st.configure("Metrica.TLabel", font=("Segoe UI", 12, "bold"))
    st.configure("Rotulo.TLabel", foreground=SUAVE, font=("Segoe UI", 8))
    jan.configure(bg=FUNDO)

    topo = tk.Frame(jan, bg=AZUL, height=42)
    topo.pack(fill="x"); topo.pack_propagate(False)
    tk.Label(topo, text="Site Survey", bg=AZUL, fg="white",
             font=("Segoe UI", 14, "bold")).pack(side="left", padx=(18, 8))
    tk.Label(topo, text="Rajant · Anglo American", bg=AZUL, fg="#C7D2F0",
             font=("Segoe UI", 9)).pack(side="left", pady=(4, 0))

    def cartao(pai, **kw):
        return ttk.Frame(pai, style="Card.TFrame", padding=(12, 8), **kw)

    # ORDEM DE EMPACOTAMENTO, e não é detalhe: o `pack` serve quem chega
    # primeiro. Um Notebook com expand=True empacotado antes do rodapé
    # toma a janela inteira e espreme o resto até sumir — foi assim que
    # os botões de Iniciar e Parar saíram da tela. Rodapé e registro são
    # reservados AGORA, presos ao fundo; o Notebook fica com a sobra.
    nb = ttk.Notebook(jan)
    ab_col = ttk.Frame(nb, padding=(10, 8)); nb.add(ab_col, text="Coleta")
    ab_arq = ttk.Frame(nb, padding=(10, 8)); nb.add(ab_arq, text="Arquivos do MeshMapper")

    # ═══════════ comum às duas abas: destino e o que gerar ═══════════
    rod = ttk.Frame(jan, padding=(14, 2, 14, 8))
    rod.pack(side="bottom", fill="x")
    ttk.Label(rod, text="Salvar em").pack(side="left")
    v_saida = tk.StringVar(value=str(Path.home() / "Documents"))
    ttk.Entry(rod, textvariable=v_saida, width=46).pack(side="left", padx=8)

    def escolher_pasta():
        d = filedialog.askdirectory(title="Onde salvar")
        if d: v_saida.set(d)
    ttk.Button(rod, text="Procurar…", command=escolher_pasta).pack(side="left")
    v_kmz = tk.BooleanVar(value=True)
    v_ppt = tk.BooleanVar(value=True)
    v_xls = tk.BooleanVar(value=True)
    for txt_, var in (("KMZ", v_kmz), ("PPT", v_ppt), ("Excel", v_xls)):
        ttk.Checkbutton(rod, text=txt_, variable=var).pack(side="left", padx=(14, 0))

    # Áreas: a imagem de cada área (GeoTIFF; o nome do arquivo é o nome da
    # área) e/ou polígonos do Google Earth (KMZ/KML). Valem para as duas
    # abas. Sem nenhuma, laudo sem áreas. A leitura vai para uma thread:
    # GeoTIFF de drone tem gigabytes, e a janela não pode congelar.
    v_tif = tk.BooleanVar(value=True)
    ttk.Checkbutton(rod, text="Mapas GeoTIFF", variable=v_tif).pack(
        side="left", padx=(14, 0))
    rod_ar = ttk.Frame(jan, padding=(14, 2, 14, 0))
    rod_ar.pack(side="bottom", fill="x")
    ttk.Label(rod_ar, text="Áreas").pack(side="left")
    lbl_areas = ttk.Label(rod_ar, text="", style="Fraco.TLabel")
    est["areas_arqs"] = [x.strip() for x in cfg.get(
        "survey", "areas", fallback="").split(";") if x.strip()]
    est["areas_arqs_ok"] = []
    est["areas"] = []
    est["areas_lendo"] = False

    def _resumo_areas():
        ar = est["areas"]
        if est["areas_lendo"]:
            t = "lendo as áreas…"
        elif not ar:
            t = "laudo sem áreas"
        else:
            nomes = ", ".join(a["nome"] for a in ar)
            img = sum(1 for a in ar if a.get("imagem"))
            ter = sum(1 for a in ar if a.get("terreno"))
            t = (f"{len(ar)} área(s)" + (f", {img} com imagem" if img else "")
                 + (f", {ter} com terreno" if ter else "")
                 + ": " + (nomes if len(nomes) < 60 else nomes[:57] + "…"))
        lbl_areas.configure(text=t)

    def _ler_areas(arqs):
        est["areas_lendo"] = True
        _resumo_areas()
        arqs = list(arqs)

        def trab():
            try:
                ar = rm.carregar_areas(arqs) or []
                fila.put(("areas", (arqs, ar, None)))
            except Exception as e:
                fila.put(("areas", (arqs, None, str(e))))
        threading.Thread(target=trab, daemon=True).start()

    def adicionar_areas():
        fs = filedialog.askopenfilenames(
            title="Áreas: a imagem, o terreno (LAS) ou os polígonos de cada área",
            filetypes=[("Áreas", "*.tif *.tiff *.las *.laz *.kmz *.kml"),
                       ("Imagem georreferenciada (GeoTIFF)", "*.tif *.tiff"),
                       ("Terreno (LAS/LAZ)", "*.las *.laz"),
                       ("Google Earth", "*.kmz *.kml"),
                       ("Todos os arquivos", "*.*")])
        novos = [f for f in fs if f not in est["areas_arqs"]]
        if novos:
            est["areas_arqs"] = est["areas_arqs"] + novos
            _ler_areas(est["areas_arqs"])

    def limpar_areas():
        est["areas_arqs"], est["areas_arqs_ok"], est["areas"] = [], [], []
        est["areas_lendo"] = False
        _resumo_areas()

    def _areas_prontas():
        """As áreas lidas ([] = sem áreas), ou None se ainda lendo."""
        if est["areas_lendo"]:
            messagebox.showinfo(TITULO, "Aguarde: as áreas ainda estão "
                                        "sendo lidas.")
            return None
        return est["areas"]

    ttk.Button(rod_ar, text="Adicionar áreas…",
               command=adicionar_areas).pack(side="left", padx=(25, 0))
    ttk.Button(rod_ar, text="Limpar", command=limpar_areas).pack(
        side="left", padx=(6, 0))

    def abrir_infra():
        """A planilha das ERB/ERM da cobertura estimada (posição, altura
        do mastro, potência, setor). Criada com o cabeçalho se não
        existir; a busca e o survey acrescentam as que acharem."""
        import cobertura_estimada as ce
        cam = Path(cfg.get("cobertura", "infraestrutura",
                           fallback=ce.INFRA_PADRAO) or ce.INFRA_PADRAO).resolve()
        try:
            if not cam.exists():
                ce.completar_infraestrutura(cam, est.get("achados"))
            if not cam.exists():
                cam.write_text(";".join(ce.COLUNAS_INFRA) + "\n",
                               encoding="utf-8-sig")
            elif est.get("achados"):
                n = ce.completar_infraestrutura(cam, est["achados"])
                if n:
                    escreve(f"{cam.name}: {n} ERB/ERM acrescentada(s) pela busca")
        except Exception as e:
            messagebox.showerror(TITULO, f"Planilha das ERB/ERM:\n{e}")
            return
        if not abrir_pasta(cam):
            messagebox.showinfo(TITULO, f"Abra a planilha:\n{cam}")

    ttk.Button(rod_ar, text="ERB/ERM…", command=abrir_infra).pack(
        side="left", padx=(6, 0))
    lbl_areas.pack(side="left", padx=(12, 0))
    if est["areas_arqs"]:
        _ler_areas(est["areas_arqs"])
    else:
        _resumo_areas()

    # ═══════════════════════ ABA COLETA ══════════════════════════
    if rm.Breadcrumb is None:
        ttk.Label(ab_col, style="Titulo.TLabel",
                  text="rajant-api não instalada — coleta indisponível"
                  ).pack(anchor="w", pady=(40, 6))
        ttk.Label(ab_col, style="Fraco.TLabel", text=(
            "pip install rajant-api --no-deps\n"
            "pip install \"protobuf==4.23.4\"")).pack(anchor="w")
    else:
        # ── Rede ──
        # Em cada linha, os botões são empacotados ANTES dos campos: se
        # faltar largura (escala do Windows alta), quem encolhe é o campo,
        # não o botão.
        lr = cartao(ab_col); lr.pack(fill="x", pady=(0, 8))
        b_desc = ttk.Button(lr, text="Procurar equipamentos", style="Accent.TButton")
        b_desc.pack(side="right")
        ttk.Label(lr, text="Rede", style="Titulo.TLabel").pack(side="left", padx=(0, 14))
        ttk.Label(lr, text="Seeds").pack(side="left")
        v_seeds = tk.StringVar(value=cfg.get("rede", "seeds",
                                             fallback="10.188.96.140"))
        ttk.Entry(lr, textvariable=v_seeds, width=26).pack(side="left", padx=(6, 12))
        ttk.Label(lr, text="Usuário").pack(side="left")
        v_role = tk.StringVar(value=rm.papel_bcapi(
            cfg.get("rede", "role", fallback="CO")))
        ttk.Entry(lr, textvariable=v_role, width=7).pack(side="left", padx=(6, 12))
        ttk.Label(lr, text="Senha").pack(side="left")
        v_senha = tk.StringVar(value=cfg.get("rede", "password", fallback=""))
        ttk.Entry(lr, textvariable=v_senha, width=16,
                  show="•").pack(side="left", padx=(6, 12))
        ttk.Label(lr, text="Porta").pack(side="left")
        v_porta = tk.StringVar(value=cfg.get("rede", "port", fallback="2300"))
        ttk.Entry(lr, textvariable=v_porta, width=6).pack(side="left", padx=(6, 12))

        # ── Coleta (reservada ANTES da lista: ela cresce, isto não some) ──
        baixo = cartao(ab_col)
        baixo.pack(side="bottom", fill="x", pady=(8, 0))
        l3 = ttk.Frame(baixo); l3.pack(fill="x")
        b_par = ttk.Button(l3, text="Parar e gerar", state="disabled")
        b_par.pack(side="right")
        b_ini = ttk.Button(l3, text="Iniciar coleta", style="Accent.TButton")
        b_ini.pack(side="right", padx=8)
        lc = ttk.Frame(l3); lc.pack(side="left", fill="x", expand=True)
        ttk.Label(lc, text="Coleta", style="Titulo.TLabel").pack(side="left", padx=(0, 14))
        ttk.Label(lc, text="Passo (m)").pack(side="left")
        v_passo = tk.StringVar(value=str(int(col.PASSO_M_PADRAO)))
        ttk.Entry(lc, textvariable=v_passo, width=5).pack(side="left", padx=(6, 12))
        ttk.Label(lc, text="Duração").pack(side="left")
        v_min = tk.StringVar(value="Sem limite")
        ttk.Combobox(lc, textvariable=v_min, width=10,
                     values=("Sem limite", "15 min", "30 min", "60 min",
                             "120 min")).pack(side="left", padx=(6, 12))
        v_trace = tk.BooleanVar(value=True)
        ttk.Checkbutton(lc, text="Trace", variable=v_trace).pack(side="left")
        v_dest = tk.StringVar(value=rm.DESTINO_TRACE_PADRAO)
        ttk.Entry(lc, textvariable=v_dest, width=13).pack(side="left", padx=(4, 6))
        b_tt = ttk.Button(lc, text="Testar trace")
        b_tt.pack(side="left")

        # ── Testes ativos: latência/perda por ping e throughput pelo
        # iperf3 do próprio rádio. O throughput pesa na malha, e por isso
        # nasce desligado: um teste por vez, com intervalo, e pausa quando
        # a latência do ping sobe.
        l4 = ttk.Frame(baixo); l4.pack(fill="x", pady=(6, 0))
        b_ti = ttk.Button(l4, text="Testar iperf")
        b_ti.pack(side="right")
        ttk.Label(l4, text="Testes ativos", style="Titulo.TLabel").pack(
            side="left", padx=(0, 14))
        v_ping = tk.BooleanVar(value=True)
        ttk.Checkbutton(l4, text="Latência e perda (ping)",
                        variable=v_ping).pack(side="left")
        v_vaz = tk.BooleanVar(value=False)
        ttk.Checkbutton(l4, text="Throughput (iperf3) até",
                        variable=v_vaz).pack(side="left", padx=(14, 4))
        v_srv = tk.StringVar(value=cfg.get("teste_ativo", "iperf_servidor",
                                           fallback=""))
        cb_srv = ttk.Combobox(l4, textvariable=v_srv, width=26)
        cb_srv.pack(side="left", padx=(0, 12))
        ttk.Label(l4, text="Sentido").pack(side="left")
        v_sent = tk.StringVar(value="Os dois")
        ttk.Combobox(l4, textvariable=v_sent, width=9, state="readonly",
                     values=("Subida", "Descida", "Os dois")).pack(
            side="left", padx=(6, 12))
        ttk.Label(l4, text="Duração (s)").pack(side="left")
        v_iseg = tk.StringVar(value="5")
        ttk.Entry(l4, textvariable=v_iseg, width=4).pack(side="left", padx=(6, 12))
        ttk.Label(l4, text="Intervalo (s)").pack(side="left")
        v_iint = tk.StringVar(value="60")
        ttk.Entry(l4, textvariable=v_iint, width=5).pack(side="left", padx=(6, 0))

        def _ip_servidor():
            """O IP do servidor do iperf3: "ERB-01 (10.188.96.30)" ou o IP."""
            import re as _re
            t = v_srv.get().strip()
            m = _re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", t)
            return m.group(1) if m else t

        # Métricas da coleta em blocos, um número por bloco.
        lm = ttk.Frame(baixo); lm.pack(fill="x", pady=(8, 0))
        metricas = {}
        for chave, rot in (("tempo", "Tempo"), ("amostras", "Amostras"),
                           ("equip", "Equipamentos"), ("ritmo", "Leituras/s"),
                           ("passo", "Passo real"), ("parados", "Parados"),
                           ("falhas", "Falhas"), ("trace", "Trace"),
                           ("latencia", "Latência"), ("vazao", "Throughput")):
            f = ttk.Frame(lm); f.pack(side="left", padx=(0, 22))
            ttk.Label(f, text=rot, style="Rotulo.TLabel").pack(anchor="w")
            v = tk.StringVar(value="—")
            ttk.Label(f, textvariable=v, style="Metrica.TLabel").pack(anchor="w")
            metricas[chave] = v

        # ── Equipamentos ──
        qd_c = cartao(ab_col)
        l2 = ttk.Frame(qd_c); l2.pack(fill="x", pady=(0, 6))
        lb = ttk.Frame(l2); lb.pack(side="right")
        ttk.Label(l2, text="Equipamentos", style="Titulo.TLabel").pack(side="left")
        lbl_sel = ttk.Label(l2, style="Fraco.TLabel", text="")
        lbl_sel.pack(side="left", padx=(10, 0))
        qd = ttk.Frame(qd_c)
        cols = ("sel", "nome", "ip", "gps", "vizinhos", "obs")
        arv = ttk.Treeview(qd, columns=cols, show="headings", height=6,
                           selectmode="extended")
        for c, t, w, an in (("sel", "", 36, "center"),
                            ("nome", "Equipamento", 230, "w"),
                            ("ip", "IP", 130, "w"), ("gps", "GPS", 60, "center"),
                            ("vizinhos", "Vizinhos", 80, "center"),
                            ("obs", "Observação", 320, "w")):
            arv.heading(c, text=t); arv.column(c, width=w, anchor=an)
        rol = ttk.Scrollbar(qd, orient="vertical", command=arv.yview)
        arv.configure(yscrollcommand=rol.set)
        arv.pack(side="left", fill="both", expand=True)
        rol.pack(side="right", fill="y")
        arv.tag_configure("semgps", foreground=SUAVE)
        arv.tag_configure("erro", foreground=VERM)
        qd.pack(fill="both", expand=True)
        qd_c.pack(fill="both", expand=True)

        marcados = set()

        def _conta():
            com = sum(1 for ip in marcados
                      if est["achados"].get(ip, {}).get("tem_gps"))
            lbl_sel.configure(
                text=f"{len(marcados)} de {len(est['achados'])} marcados  ·  "
                     f"{com} com GPS")

        def _pinta(ip):
            if arv.exists(ip):
                arv.set(ip, "sel", "☑" if ip in marcados else "☐")

        def alternar(ip):
            if ip in marcados: marcados.discard(ip)
            else: marcados.add(ip)
            _pinta(ip); _conta()

        def clique(ev):
            ip = arv.identify_row(ev.y)
            if ip and arv.identify_column(ev.x) == "#1":
                alternar(ip)
        arv.bind("<Button-1>", clique)
        arv.bind("<space>", lambda e: [alternar(i) for i in arv.selection()])

        def marcar(quais):
            import re as _re
            marcados.clear()
            for ip, v in est["achados"].items():
                if v.get("erro"): continue
                if quais == "todos": marcados.add(ip)
                elif quais == "gps" and v.get("tem_gps"): marcados.add(ip)
                elif quais == "moveis" and v.get("tem_gps") and not _re.match(
                        rm.PADRAO_INFRA, v.get("nome") or "", _re.I):
                    marcados.add(ip)
            for ip in est["achados"]: _pinta(ip)
            _conta()

        for txt_, q in (("Todos", "todos"), ("Com GPS", "gps"),
                        ("Veículos", "moveis"), ("Nenhum", "nenhum")):
            ttk.Button(lb, text=txt_,
                       command=lambda q=q: marcar(q)).pack(side="left", padx=(6, 0))

    # ══════════════════════ ABA ARQUIVOS ═════════════════════════
    ca = cartao(ab_arq)
    la = ttk.Frame(ca); la.pack(side="bottom", fill="x", pady=(8, 0))
    b_ger = ttk.Button(la, text="Gerar relatórios", style="Accent.TButton")
    b_ger.pack(side="right")
    v_juntar = tk.BooleanVar(value=True)
    ttk.Checkbutton(la, text="Relatório único",
                    variable=v_juntar).pack(side="right", padx=12)
    la_h = ttk.Frame(ca); la_h.pack(fill="x", pady=(0, 6))
    ttk.Label(la_h, text="Arquivos", style="Titulo.TLabel").pack(side="left")
    lbl_arq = ttk.Label(la_h, text="", style="Fraco.TLabel")
    lbl_arq.pack(side="left", padx=(10, 0))
    qa = ttk.Frame(ca); qa.pack(fill="both", expand=True)
    lst = tk.Listbox(qa, height=12, bg=CARTAO, relief="flat",
                     highlightthickness=1, highlightbackground="#D6DAE3",
                     selectmode="extended", font=("Consolas", 9),
                     activestyle="none", selectbackground=AZUL)
    rl2 = ttk.Scrollbar(qa, orient="vertical", command=lst.yview)
    lst.configure(yscrollcommand=rl2.set)
    lst.pack(side="left", fill="both", expand=True); rl2.pack(side="right", fill="y")
    ca.pack(fill="both", expand=True)

    def _lista():
        lst.delete(0, "end")
        for a in est["arquivos"]: lst.insert("end", a)
        n = len(est["arquivos"])
        lbl_arq.configure(text=f"{n} arquivo(s)" if n else "")

    def escolher():
        novos = filedialog.askopenfilenames(title="Capturas do MeshMapper",
                                            filetypes=EXTENSOES)
        for n in novos:
            if n not in est["arquivos"]: est["arquivos"].append(n)
        _lista()

    def remover():
        for i in sorted(lst.curselection(), reverse=True):
            del est["arquivos"][i]
        _lista()

    for txt_, cmd in (("Adicionar arquivos…", escolher), ("Remover", remover),
                      ("Limpar", lambda: (est["arquivos"].clear(), _lista()))):
        ttk.Button(la, text=txt_, command=cmd).pack(side="left", padx=(0, 6))

    # ═══════════════════════ andamento ═══════════════════════════
    txt = tk.Text(jan, height=4, bg=CARTAO, relief="flat", font=("Consolas", 9),
                  wrap="word", highlightthickness=1,
                  highlightbackground="#D6DAE3", padx=8, pady=4)
    txt.pack(side="bottom", fill="x", expand=False, padx=14, pady=(2, 4))
    # Só agora, com o fundo já reservado: o Notebook fica com a sobra.
    nb.pack(fill="both", expand=True, padx=12, pady=(8, 2))

    def escreve(t):
        txt.insert("end", str(t) + "\n"); txt.see("end")

    # ═══════════════════════ ações ═══════════════════════════════
    def gerar_de_arquivos():
        if not est["arquivos"]:
            messagebox.showwarning(TITULO, "Escolha ao menos um arquivo.")
            return
        areas = _areas_prontas()
        if areas is None:
            return
        b_ger.configure(state="disabled", text="Gerando…")
        txt.delete("1.0", "end")
        arqs = list(est["arquivos"]); juntar = v_juntar.get()
        alvo = v_saida.get()
        kmz, ppt, xls = v_kmz.get(), v_ppt.get(), v_xls.get()
        tif = v_tif.get()

        def trab():
            try:
                feitos = smm.gerar(arqs, alvo, juntar=juntar, fazer_kmz=kmz,
                                   fazer_ppt=ppt, fazer_excel=xls,
                                   aviso=lambda t: fila.put(("log", t)),
                                   areas=areas, fazer_mapas=tif)
                fila.put(("pronto_arq", feitos))
            except Exception as e:
                fila.put(("log", f"ERRO: {e}"))
                fila.put(("log", traceback.format_exc()))
                fila.put(("pronto_arq", []))
        threading.Thread(target=trab, daemon=True).start()
    b_ger.configure(command=gerar_de_arquivos)

    if rm.Breadcrumb is not None:
        def descobrir():
            b_desc.configure(state="disabled", text="Procurando…")
            txt.delete("1.0", "end")
            seeds = [s.strip() for s in
                     v_seeds.get().replace(";", ",").split(",") if s.strip()]
            try:
                role = rm.conferir_papel(v_role.get())
            except ValueError as e:
                b_desc.configure(state="normal", text="Procurar equipamentos")
                messagebox.showwarning(TITULO, str(e))
                return
            v_role.set(role)
            senha = v_senha.get()
            porta = int(v_porta.get() or 2300)

            def trab():
                try:
                    r = rm.descobrir_malha(seeds, role=role, senha=senha,
                                           porta=porta,
                                           aviso=lambda t: fila.put(("log", t)))
                    fila.put(("achados", r))
                except Exception as e:
                    fila.put(("log", f"ERRO: {e}"))
                    fila.put(("log", traceback.format_exc()))
                    fila.put(("achados", {}))
            threading.Thread(target=trab, daemon=True).start()

        def _minutos():
            import re as _re
            m = _re.search(r"\d+([.,]\d+)?", v_min.get() or "")
            return float(m.group(0).replace(",", ".")) if m else 0.0

        def iniciar():
            alvos = {ip: est["achados"][ip]["nome"] for ip in marcados}
            if not alvos:
                messagebox.showwarning(TITULO, "Marque ao menos um equipamento.")
                return
            try:
                passo = float(v_passo.get().replace(",", "."))
                if passo <= 0: raise ValueError
            except ValueError:
                messagebox.showwarning(TITULO, "Passo: um número maior que zero.")
                return
            minutos = _minutos()
            # Lidas AGORA: arquivo errado aparece antes da coleta, não
            # depois dela.
            areas = _areas_prontas()
            if areas is None:
                return
            srv = _ip_servidor() if v_vaz.get() else None
            if v_vaz.get() and not srv:
                messagebox.showwarning(TITULO, "Throughput: escolha o "
                                       "servidor do iperf3 (um BreadCrumb da "
                                       "lista ou o IP de um servidor iperf3).")
                return
            try:
                iseg = int(float(v_iseg.get().replace(",", ".")))
                iint = float(v_iint.get().replace(",", "."))
                if iseg <= 0 or iint < 10: raise ValueError
            except ValueError:
                messagebox.showwarning(TITULO, "Throughput: duração maior que "
                                       "zero e intervalo de no mínimo 10 s.")
                return
            c = col.Coleta(alvos, role=v_role.get(), senha=v_senha.get(),
                           porta=int(v_porta.get() or 2300), passo_m=passo,
                           aviso=lambda t: fila.put(("log", t)),
                           trace_destino=(v_dest.get().strip()
                                          if v_trace.get() else None),
                           malha=est["achados"], ping=v_ping.get(),
                           iperf_servidor=srv, iperf_segundos=iseg,
                           iperf_intervalo_s=iint,
                           iperf_sentido={"Subida": "envio",
                                          "Descida": "recebimento"}.get(
                                              v_sent.get(), "ambos"))
            est["coleta"] = c
            b_ini.configure(state="disabled")
            b_par.configure(state="normal")
            b_desc.configure(state="disabled")
            alvo = v_saida.get()
            kmz, ppt, xls = v_kmz.get(), v_ppt.get(), v_xls.get()
            tif = v_tif.get()

            def trab():
                try:
                    c.rodar(minutos)
                    fila.put(("log", "gerando relatórios…"))
                    feitos = col.gravar_e_gerar(
                        c, alvo, fazer_kmz=kmz, fazer_ppt=ppt, fazer_excel=xls,
                        aviso=lambda t: fila.put(("log", t)), areas=areas,
                        fazer_mapas=tif)
                    fila.put(("pronto_col", feitos))
                except Exception as e:
                    fila.put(("log", f"ERRO: {e}"))
                    fila.put(("log", traceback.format_exc()))
                    fila.put(("pronto_col", []))
            threading.Thread(target=trab, daemon=True).start()

        def parar():
            c = est.get("coleta")
            if c:
                b_par.configure(state="disabled", text="Gerando…")
                c.parar()

        def testar_trace():
            sel = [i for i in arv.selection() if i in est["achados"]]
            if not sel:
                messagebox.showwarning(TITULO, "Selecione um rádio na lista.")
                return
            ip = sel[0]
            b_tt.configure(state="disabled", text="Testando…")
            role, senha = v_role.get(), v_senha.get()
            porta = int(v_porta.get() or 2300)
            dest, pasta = v_dest.get().strip(), v_saida.get()

            def trab():
                try:
                    alvo = col.testar_trace(
                        ip, dest, role=role, senha=senha, porta=porta,
                        pasta=pasta, aviso=lambda t: fila.put(("log", t)))
                    fila.put(("log", f"\nArquivo do teste: {alvo}"))
                except Exception as e:
                    fila.put(("log", f"ERRO no teste: {e}"))
                    fila.put(("log", traceback.format_exc()))
                fila.put(("teste_fim", None))
            threading.Thread(target=trab, daemon=True).start()

        def testar_iperf():
            sel = [i for i in arv.selection() if i in est["achados"]]
            if not sel:
                messagebox.showwarning(TITULO, "Selecione um rádio na lista.")
                return
            srv = _ip_servidor()
            if not srv:
                messagebox.showwarning(TITULO, "Escolha o servidor do iperf3.")
                return
            ip = sel[0]
            b_ti.configure(state="disabled", text="Testando…")
            role, senha = v_role.get(), v_senha.get()
            porta = int(v_porta.get() or 2300)
            pasta = v_saida.get()
            try:
                iseg = max(1, int(float(v_iseg.get().replace(",", "."))))
            except ValueError:
                iseg = 5
            reverso = v_sent.get() == "Descida"

            def trab():
                try:
                    alvo = col.testar_iperf(ip, srv, role=role, senha=senha,
                                            porta=porta, pasta=pasta,
                                            segundos=iseg, reverso=reverso,
                                            aviso=lambda t: fila.put(("log", t)))
                    fila.put(("log", f"\nArquivo do teste: {alvo}"))
                except Exception as e:
                    fila.put(("log", f"ERRO no teste: {e}"))
                    fila.put(("log", traceback.format_exc()))
                fila.put(("teste_iperf_fim", None))
            threading.Thread(target=trab, daemon=True).start()

        b_ti.configure(command=testar_iperf)
        b_desc.configure(command=descobrir)
        b_ini.configure(command=iniciar)
        b_par.configure(command=parar)
        b_tt.configure(command=testar_trace)

    def bombear():
        try:
            while True:
                tipo, val = fila.get_nowait()
                if tipo == "log":
                    escreve(val)
                elif tipo == "areas":
                    arqs, ar, erro = val
                    if arqs != est["areas_arqs"]:
                        continue            # leitura de uma lista antiga
                    est["areas_lendo"] = False
                    if erro:
                        # Fica a última lista que leu sem erro.
                        est["areas_arqs"] = list(est["areas_arqs_ok"])
                        escreve(f"ERRO nas áreas: {erro}")
                        messagebox.showerror(TITULO, f"Áreas:\n{erro}")
                    else:
                        est["areas"], est["areas_arqs_ok"] = ar, list(arqs)
                        for a in ar:
                            escreve(f"área {a['nome']}"
                                    + (f" — imagem {Path(a['imagem']).name}"
                                       if a.get("imagem") else ""))
                    _resumo_areas()
                elif tipo == "teste_fim":
                    b_tt.configure(state="normal", text="Testar trace")
                elif tipo == "teste_iperf_fim":
                    b_ti.configure(state="normal", text="Testar iperf")
                elif tipo == "achados":
                    est["achados"] = val
                    for i in arv.get_children(): arv.delete(i)
                    resp = sum(1 for v in val.values() if not v.get("erro"))
                    escreve(f"{resp} de {len(val)} responderam")
                    # Quem respondeu primeiro, e dentro disso por nome.
                    for ip, v in sorted(
                            val.items(),
                            key=lambda kv: (bool(kv[1].get("erro")),
                                            not kv[1].get("tem_gps"),
                                            kv[1]["nome"])):
                        if v.get("erro"):
                            tag, gps, obs = "erro", "—", v["erro"][:70]
                        elif not v.get("tem_gps"):
                            tag, gps, obs = "semgps", "não", "sem GPS"
                        else:
                            tag, gps, obs = "", "sim", ""
                        arv.insert("", "end", iid=ip, tags=(tag,),
                                   values=("☐", v["nome"], ip, gps,
                                           v.get("vizinhos") or "", obs))
                    marcar("gps")
                    # Servidor do iperf3: os BreadCrumbs que responderam,
                    # ERB/ERM primeiro (o lado fixo da malha).
                    import re as _re
                    vivos = [(ip, v) for ip, v in val.items() if not v.get("erro")]
                    vivos.sort(key=lambda kv: (not _re.match(rm.PADRAO_INFRA,
                                                             kv[1]["nome"] or "", _re.I),
                                               kv[1]["nome"]))
                    cb_srv.configure(values=[f"{v['nome']} ({ip})" for ip, v in vivos])
                    b_desc.configure(state="normal",
                                     text="Procurar equipamentos")
                elif tipo in ("pronto_arq", "pronto_col"):
                    if tipo == "pronto_arq":
                        b_ger.configure(state="normal", text="Gerar relatórios")
                    else:
                        b_ini.configure(state="normal")
                        b_par.configure(state="disabled", text="Parar e gerar")
                        b_desc.configure(state="normal")
                        est["coleta"] = None
                    if val:
                        escreve(f"pronto: {len(val)} arquivo(s) em {v_saida.get()}")
                        if messagebox.askyesno(TITULO, "Abrir a pasta?"):
                            abrir_pasta(v_saida.get())
        except queue.Empty:
            pass
        c = est.get("coleta")
        if c and c.inicio:
            s = c.status()
            if s.get("trace_parado"):
                tr = "desligado"
            elif s.get("trace_ok") or s.get("trace_sem") or s.get("trace_ocupado"):
                tr = (f"{s['trace_ok']} ok"
                      + (f" · {s['trace_ocupado']} ocup." if s.get("trace_ocupado") else "")
                      + (f" · {s['trace_sem']} sem" if s.get("trace_sem") else ""))
            else:
                tr = "—"
            d = int(s["duracao_s"])
            for chave, valor in (
                    ("tempo", f"{d // 60:02d}:{d % 60:02d}"),
                    ("amostras", f"{s['amostras']}"),
                    ("equip", f"{s['equipamentos']}"),
                    ("ritmo", f"{s['leituras_s']:.1f}"),
                    ("passo", f"{s['passo_m']:g} m" if s["passo_m"] else "—"),
                    ("parados", f"{s.get('parados', 0)}"),
                    ("falhas", f"{s['falhas']}"),
                    ("trace", tr),
                    ("latencia", f"{s['ping_ms']:g} ms"
                     if s.get("ping_ms") is not None else "—"),
                    ("vazao", ("desligado" if s.get("iperf_parado")
                               else "em pausa" if s.get("iperf_pausado")
                               else f"testando {s['iperf_em_teste']}"
                               if s.get("iperf_em_teste")
                               else f"{s['iperf_ultimo']:g} Mbps · {s['iperf_ok']} ok"
                               if s.get("iperf_ultimo") is not None else "—"))):
                metricas[chave].set(valor)
        jan.after(300, bombear)

    _lista()
    jan.after(300, bombear)
    jan.mainloop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        _morrer("O programa parou com um erro.",
                f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}")
