#!/usr/bin/env python3
"""
Site Survey — a janela única.

Duas abas, os mesmos relatórios:

  • Coleta          fala com os rádios, descobre a malha, você marca os
                    equipamentos e coleta ao vivo
  • Arquivos        lê capturas do MeshMapper que alguém já trouxe

Saem KMZ com o rastro de calor, PPT na identidade Anglo e Excel.

A lógica não mora aqui: coleta em `coleta_rajant.py`, arquivos em
`survey_meshmapper.py`, relatórios em `rajant_monitor.py`. Este arquivo é
só a janela — assim cada pedaço continua testável sem abrir tela.

Pela linha de comando, use os módulos diretamente:

    coleta_rajant --seeds 10.188.96.140 --minutos 30 -o saida
    survey_meshmapper captura.kmz -o saida
"""
import sys, os, threading, queue, traceback
from pathlib import Path

try:
    import rajant_monitor as rm
    import survey_meshmapper as smm
    import coleta_rajant as col
except ImportError as e:
    print(f"ERRO: falta um módulo ao lado deste arquivo: {e}")
    sys.exit(1)

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


def main():
    try:
        import tkinter as tk
        from tkinter import ttk, filedialog, messagebox
    except ImportError:
        print("ERRO: este Python não tem tkinter (a interface gráfica).\n"
              "      No Windows, reinstale o Python marcando 'tcl/tk'.\n"
              "      Enquanto isso, use pela linha de comando:\n"
              "      survey_meshmapper captura.kmz -o saida")
        return 1

    AZUL   = "#" + rm.ANGLO["azul"]
    AZUL2  = "#" + rm.ANGLO["azul2"]
    FUNDO  = "#" + rm.ANGLO["fundo"]
    CARTAO = "#" + rm.ANGLO["cartao"]
    TEXTO  = "#" + rm.ANGLO["texto"]
    SUAVE  = "#" + rm.ANGLO["suave"]
    LINHA  = "#" + rm.ANGLO["linha"]

    cfg = rm.carregar_config()
    jan = tk.Tk()
    jan.title(TITULO)
    jan.geometry("1200x820")
    jan.configure(bg=FUNDO)
    fila = queue.Queue()
    est = {"coleta": None, "achados": {}, "arquivos": []}

    st = ttk.Style()
    try: st.theme_use("clam")
    except Exception: pass
    st.configure("TFrame", background=FUNDO)
    st.configure("TNotebook", background=FUNDO, borderwidth=0)
    st.configure("TNotebook.Tab", padding=(20, 9), font=("Segoe UI", 10))
    st.map("TNotebook.Tab", background=[("selected", FUNDO)],
           foreground=[("selected", AZUL)])
    st.configure("TLabel", background=FUNDO, foreground=TEXTO,
                 font=("Segoe UI", 10))
    st.configure("Sec.TLabel", font=("Segoe UI", 10, "bold"))
    st.configure("Fraco.TLabel", foreground=SUAVE, font=("Segoe UI", 9))
    st.configure("TCheckbutton", background=FUNDO, foreground=TEXTO)
    st.configure("Azul.TButton", background=AZUL, foreground="white",
                 font=("Segoe UI", 10, "bold"), padding=(16, 9), borderwidth=0)
    st.map("Azul.TButton", background=[("active", AZUL2),
                                       ("disabled", "#9AA6C4")])
    st.configure("TButton", padding=(12, 7))
    st.configure("Tr.Treeview", rowheight=23, fieldbackground=CARTAO,
                 background=CARTAO, foreground=TEXTO)
    st.configure("Tr.Treeview.Heading", font=("Segoe UI", 9, "bold"))

    topo = tk.Frame(jan, bg=AZUL, height=58)
    topo.pack(fill="x"); topo.pack_propagate(False)
    tk.Label(topo, text="Site Survey", bg=AZUL, fg="white",
             font=("Segoe UI", 17, "bold")).pack(side="left", padx=20)
    tk.Label(topo, text="Rajant", bg=AZUL, fg="#C7D2F0",
             font=("Segoe UI", 10)).pack(side="left", pady=(6, 0))

    nb = ttk.Notebook(jan); nb.pack(fill="both", expand=True, padx=12, pady=10)
    ab_col = ttk.Frame(nb, padding=12); nb.add(ab_col, text="Coleta")
    ab_arq = ttk.Frame(nb, padding=12); nb.add(ab_arq, text="Arquivos do MeshMapper")

    # ═══════════ comum às duas abas: destino e o que gerar ═══════════
    rod = ttk.Frame(jan, padding=(14, 0, 14, 12)); rod.pack(fill="x")
    ttk.Label(rod, text="Salvar em").pack(side="left")
    v_saida = tk.StringVar(value=str(Path.home() / "Documents"))
    ttk.Entry(rod, textvariable=v_saida, width=46).pack(side="left", padx=8)

    def escolher_pasta():
        d = filedialog.askdirectory(title="Onde salvar")
        if d: v_saida.set(d)
    ttk.Button(rod, text="Procurar...", command=escolher_pasta).pack(side="left")
    v_kmz = tk.BooleanVar(value=True)
    v_ppt = tk.BooleanVar(value=True)
    v_xls = tk.BooleanVar(value=True)
    for txt, var in (("KMZ", v_kmz), ("PPT", v_ppt), ("Excel", v_xls)):
        ttk.Checkbutton(rod, text=txt, variable=var).pack(side="left", padx=(14, 0))

    # ═══════════════════════ ABA COLETA ══════════════════════════
    if rm.Breadcrumb is None:
        # Sem a rajant-api a coleta não roda, mas a aba de arquivos sim.
        # Dizer isso aqui evita o usuário marcar equipamento e descobrir
        # o problema depois de esperar a descoberta.
        ttk.Label(ab_col, style="Sec.TLabel",
                  text="A biblioteca rajant-api não está instalada nesta "
                       "máquina.").pack(anchor="w", pady=(40, 6))
        ttk.Label(ab_col, style="Fraco.TLabel",
                  text="A coleta ao vivo precisa dela e de rede até a malha. "
                       "A aba de arquivos funciona sem.").pack(anchor="w")
        ttk.Label(ab_col, style="Fraco.TLabel", text=(
            "    pip install rajant-api --no-deps\n"
            "    pip install \"protobuf==4.23.4\"")).pack(anchor="w", pady=10)
    else:
        ttk.Label(ab_col, text="1. Rede", style="Sec.TLabel").pack(anchor="w")
        lr = ttk.Frame(ab_col); lr.pack(fill="x", pady=(4, 8))
        ttk.Label(lr, text="Seeds").pack(side="left")
        v_seeds = tk.StringVar(value=cfg.get("rede", "seeds",
                                             fallback="10.188.96.140"))
        ttk.Entry(lr, textvariable=v_seeds, width=30).pack(side="left", padx=(6, 12))
        ttk.Label(lr, text="Usuário").pack(side="left")
        v_role = tk.StringVar(value=cfg.get("rede", "role", fallback="co"))
        ttk.Entry(lr, textvariable=v_role, width=8).pack(side="left", padx=(6, 12))
        ttk.Label(lr, text="Senha").pack(side="left")
        v_senha = tk.StringVar(value=cfg.get("rede", "password", fallback=""))
        ttk.Entry(lr, textvariable=v_senha, width=15,
                  show="•").pack(side="left", padx=(6, 12))
        ttk.Label(lr, text="Porta").pack(side="left")
        v_porta = tk.StringVar(value=cfg.get("rede", "port", fallback="2300"))
        ttk.Entry(lr, textvariable=v_porta, width=7).pack(side="left", padx=(6, 12))
        b_desc = ttk.Button(lr, text="Procurar equipamentos", style="Azul.TButton")
        b_desc.pack(side="left")

        ttk.Label(ab_col, text="2. Equipamentos",
                  style="Sec.TLabel").pack(anchor="w", pady=(8, 0))
        lbl_sel = ttk.Label(ab_col, style="Fraco.TLabel",
                            text="nenhum equipamento encontrado ainda")
        lbl_sel.pack(anchor="w")

        qd = ttk.Frame(ab_col); qd.pack(fill="both", expand=True, pady=(4, 6))
        cols = ("sel", "nome", "ip", "gps", "vizinhos", "obs")
        arv = ttk.Treeview(qd, columns=cols, show="headings", height=12,
                           style="Tr.Treeview", selectmode="extended")
        for c, t, w, an in (("sel", "", 34, "center"),
                            ("nome", "Equipamento", 230, "w"),
                            ("ip", "IP", 130, "w"), ("gps", "GPS", 60, "center"),
                            ("vizinhos", "Vizinhos", 75, "center"),
                            ("obs", "Observação", 320, "w")):
            arv.heading(c, text=t); arv.column(c, width=w, anchor=an)
        rol = ttk.Scrollbar(qd, orient="vertical", command=arv.yview)
        arv.configure(yscrollcommand=rol.set)
        arv.pack(side="left", fill="both", expand=True)
        rol.pack(side="right", fill="y")
        arv.tag_configure("semgps", foreground=SUAVE)
        arv.tag_configure("erro", foreground="#C0392B")

        marcados = set()

        def _conta():
            com = sum(1 for ip in marcados
                      if est["achados"].get(ip, {}).get("tem_gps"))
            lbl_sel.configure(
                text=f"{len(marcados)} marcado(s) de {len(est['achados'])}  ·  "
                     f"{com} com GPS — só esses entram no mapa")

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

        lb = ttk.Frame(ab_col); lb.pack(fill="x")
        for txt, q in (("Marcar todos", "todos"), ("Só com GPS", "gps"),
                       ("Só veículos", "moveis"), ("Desmarcar", "nenhum")):
            ttk.Button(lb, text=txt,
                       command=lambda q=q: marcar(q)).pack(side="left", padx=(0, 6))

        ttk.Label(ab_col, text="3. Coleta",
                  style="Sec.TLabel").pack(anchor="w", pady=(10, 0))
        lc = ttk.Frame(ab_col); lc.pack(fill="x", pady=(4, 6))
        ttk.Label(lc, text="Uma amostra a cada").pack(side="left")
        v_passo = tk.StringVar(value=str(int(col.PASSO_M_PADRAO)))
        ttk.Entry(lc, textvariable=v_passo, width=6).pack(side="left", padx=6)
        ttk.Label(lc, text="metros").pack(side="left", padx=(0, 18))
        ttk.Label(lc, text="Parar após").pack(side="left")
        v_min = tk.StringVar(value="0")
        ttk.Entry(lc, textvariable=v_min, width=6).pack(side="left", padx=6)
        ttk.Label(lc, text="minutos  (0 = até mandar parar)",
                  style="Fraco.TLabel").pack(side="left")

        pn = tk.Frame(ab_col, bg=CARTAO, highlightbackground=LINHA,
                      highlightthickness=1)
        pn.pack(fill="x", pady=(4, 6))
        v_stat = tk.StringVar(value="parado")
        tk.Label(pn, textvariable=v_stat, bg=CARTAO, fg=TEXTO, anchor="w",
                 font=("Consolas", 10)).pack(fill="x", padx=10, pady=7)

        lf = ttk.Frame(ab_col); lf.pack(fill="x")
        b_ini = ttk.Button(lf, text="Iniciar coleta", style="Azul.TButton")
        b_ini.pack(side="left")
        b_par = ttk.Button(lf, text="Parar e gerar relatórios", state="disabled")
        b_par.pack(side="left", padx=8)

    # ══════════════════════ ABA ARQUIVOS ═════════════════════════
    ttk.Label(ab_arq, text="1. Arquivos da captura",
              style="Sec.TLabel").pack(anchor="w")
    ttk.Label(ab_arq, text=".kmz, data.json ou os .csv",
              style="Fraco.TLabel").pack(anchor="w")
    qa = ttk.Frame(ab_arq); qa.pack(fill="both", expand=True, pady=(4, 6))
    lst = tk.Listbox(qa, height=14, bg=CARTAO, fg=TEXTO, relief="flat",
                     highlightbackground=LINHA, highlightthickness=1,
                     selectmode="extended", font=("Consolas", 9))
    rl2 = ttk.Scrollbar(qa, orient="vertical", command=lst.yview)
    lst.configure(yscrollcommand=rl2.set)
    lst.pack(side="left", fill="both", expand=True); rl2.pack(side="right", fill="y")

    lbl_arq = ttk.Label(ab_arq, text="nenhum arquivo", style="Fraco.TLabel")

    def _lista():
        lst.delete(0, "end")
        for a in est["arquivos"]: lst.insert("end", a)
        n = len(est["arquivos"])
        lbl_arq.configure(text="nenhum arquivo" if not n
                          else f"{n} arquivo(s)")

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

    la = ttk.Frame(ab_arq); la.pack(fill="x")
    ttk.Button(la, text="Escolher arquivos...", style="Azul.TButton",
               command=escolher).pack(side="left")
    ttk.Button(la, text="Remover selecionado",
               command=remover).pack(side="left", padx=6)
    ttk.Button(la, text="Limpar",
               command=lambda: (est["arquivos"].clear(), _lista())).pack(side="left")
    lbl_arq.pack(side="right")

    v_juntar = tk.BooleanVar(value=True)
    ttk.Checkbutton(ab_arq, text="Juntar tudo num relatório só",
                    variable=v_juntar).pack(anchor="w", pady=(8, 0))
    b_ger = ttk.Button(ab_arq, text="Gerar relatórios", style="Azul.TButton")
    b_ger.pack(anchor="w", pady=(10, 0))

    # ═══════════════════════ andamento ═══════════════════════════
    ttk.Label(jan, text="Andamento", style="Sec.TLabel").pack(anchor="w", padx=14)
    txt = tk.Text(jan, height=8, bg=CARTAO, fg=TEXTO, relief="flat",
                  font=("Consolas", 9), wrap="word")
    txt.pack(fill="both", expand=False, padx=14, pady=(2, 6))

    def escreve(t):
        txt.insert("end", str(t) + "\n"); txt.see("end")

    # ═══════════════════════ ações ═══════════════════════════════
    def gerar_de_arquivos():
        if not est["arquivos"]:
            messagebox.showwarning(TITULO, "Escolha ao menos um arquivo.")
            return
        b_ger.configure(state="disabled", text="Gerando...")
        txt.delete("1.0", "end")
        arqs = list(est["arquivos"]); juntar = v_juntar.get()
        alvo = v_saida.get()
        kmz, ppt, xls = v_kmz.get(), v_ppt.get(), v_xls.get()

        def trab():
            try:
                feitos = smm.gerar(arqs, alvo, juntar=juntar, fazer_kmz=kmz,
                                   fazer_ppt=ppt, fazer_excel=xls,
                                   aviso=lambda t: fila.put(("log", t)))
                fila.put(("pronto_arq", feitos))
            except Exception as e:
                fila.put(("log", f"ERRO: {e}"))
                fila.put(("log", traceback.format_exc()))
                fila.put(("pronto_arq", []))
        threading.Thread(target=trab, daemon=True).start()
    b_ger.configure(command=gerar_de_arquivos)

    if rm.Breadcrumb is not None:
        def descobrir():
            b_desc.configure(state="disabled", text="Procurando...")
            txt.delete("1.0", "end")
            seeds = [s.strip() for s in
                     v_seeds.get().replace(";", ",").split(",") if s.strip()]
            role, senha = v_role.get(), v_senha.get()
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

        def iniciar():
            alvos = {ip: est["achados"][ip]["nome"] for ip in marcados}
            if not alvos:
                messagebox.showwarning(TITULO, "Marque ao menos um equipamento.")
                return
            try:
                passo = float(v_passo.get().replace(",", "."))
                if passo <= 0: raise ValueError
            except ValueError:
                messagebox.showwarning(
                    TITULO, "Metros deve ser um número maior que zero.")
                return
            try:
                minutos = float(v_min.get().replace(",", ".") or 0)
            except ValueError:
                minutos = 0.0
            c = col.Coleta(alvos, role=v_role.get(), senha=v_senha.get(),
                           porta=int(v_porta.get() or 2300), passo_m=passo,
                           aviso=lambda t: fila.put(("log", t)))
            est["coleta"] = c
            b_ini.configure(state="disabled")
            b_par.configure(state="normal")
            b_desc.configure(state="disabled")
            alvo = v_saida.get()
            kmz, ppt, xls = v_kmz.get(), v_ppt.get(), v_xls.get()

            def trab():
                try:
                    c.rodar(minutos)
                    fila.put(("log", "gerando relatórios..."))
                    feitos = col.gravar_e_gerar(
                        c, alvo, fazer_kmz=kmz, fazer_ppt=ppt, fazer_excel=xls,
                        aviso=lambda t: fila.put(("log", t)))
                    fila.put(("pronto_col", feitos))
                except Exception as e:
                    fila.put(("log", f"ERRO: {e}"))
                    fila.put(("log", traceback.format_exc()))
                    fila.put(("pronto_col", []))
            threading.Thread(target=trab, daemon=True).start()

        def parar():
            c = est.get("coleta")
            if c:
                b_par.configure(state="disabled", text="Gerando...")
                c.parar()

        b_desc.configure(command=descobrir)
        b_ini.configure(command=iniciar)
        b_par.configure(command=parar)

    def bombear():
        try:
            while True:
                tipo, val = fila.get_nowait()
                if tipo == "log":
                    escreve(val)
                elif tipo == "achados":
                    est["achados"] = val
                    for i in arv.get_children(): arv.delete(i)
                    for ip, v in sorted(val.items(),
                                        key=lambda kv: kv[1]["nome"]):
                        if v.get("erro"):
                            tag, gps, obs = "erro", "—", v["erro"][:70]
                        elif not v.get("tem_gps"):
                            tag, gps = "semgps", "não"
                            obs = "sem posição: entra no censo, não no mapa"
                        else:
                            tag, gps, obs = "", "sim", ""
                        arv.insert("", "end", iid=ip, tags=(tag,),
                                   values=("☐", v["nome"], ip, gps,
                                           v.get("vizinhos") or "", obs))
                    marcar("gps")
                    b_desc.configure(state="normal",
                                     text="Procurar equipamentos")
                elif tipo in ("pronto_arq", "pronto_col"):
                    if tipo == "pronto_arq":
                        b_ger.configure(state="normal", text="Gerar relatórios")
                    else:
                        b_ini.configure(state="normal")
                        b_par.configure(state="disabled",
                                        text="Parar e gerar relatórios")
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
            passo = f"{s['passo_m']:g} m" if s["passo_m"] else "—"
            v_stat.set(
                f"{int(s['duracao_s'])//60:02d}:{int(s['duracao_s'])%60:02d}"
                f"   {s['amostras']} amostras   {s['equipamentos']} equipamentos"
                f"   {s['leituras_s']:.1f} leituras/s   passo real {passo}"
                f"   {s['repetidos']} posições repetidas   {s['falhas']} falhas")
        jan.after(300, bombear)

    _lista()
    jan.after(300, bombear)
    jan.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
