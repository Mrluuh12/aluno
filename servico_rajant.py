"""
Modo serviço do rajant_monitor: sobe Prometheus, Grafana e o exportador em
segundo plano, sem janela, e os reergue se caírem.

    rajant_monitor.exe --servico            roda o supervisor (fica no ar)
    rajant_monitor.exe --iniciar            sobe o supervisor oculto e volta
    rajant_monitor.exe --parar              derruba o supervisor e os três
    rajant_monitor.exe --status             o que está no ar, e em que porta
    rajant_monitor.exe --instalar-usuario   sobe no logon deste usuário (sem admin)
    rajant_monitor.exe --remover-usuario    tira do logon
    rajant_monitor.exe --instalar-servico   tarefa agendada: sobe com o Windows (admin)
    rajant_monitor.exe --remover-servico    tira a tarefa agendada

A configuração fica em [servicos] do config.ini. Caminho vazio = procura
nos lugares de instalação comuns; o log diz o que achou e o que não achou.

Um processo que já atende a porta dele (Grafana instalado como serviço do
Windows, um exportador aberto à mão) não é iniciado de novo: o supervisor
só o observa, e assume se ele cair.
"""
import os, sys, time, json, socket, signal, logging, subprocess, glob
from logging.handlers import RotatingFileHandler
from pathlib import Path

NOME_TAREFA = "Rajant Monitor"
ARQ_PID = "servico.pid"
ARQ_LOG = "servico.log"
PASTA_LOGS = "logs"

DEFAULTS_SERVICOS = {
    # Caminho do executável. Vazio = procurar (ver `_procurar`).
    "prometheus_exe": "",
    # Argumentos do Prometheus, rodando na pasta dele.
    "prometheus_args": "--config.file=prometheus.yml",
    "prometheus_porta": "9090",
    "grafana_exe": "",
    "grafana_porta": "3000",
    # false = o supervisor não mexe naquele programa.
    "iniciar_prometheus": "true",
    "iniciar_grafana": "true",
    "iniciar_exportador": "true",
    # Segundos de espera ao ligar a máquina antes de subir tudo: a rede e
    # os serviços do Windows ainda estão subindo.
    "espera_inicial_s": "20",
}

# Acrescentada ao fim do config.ini quando falta — acrescentada, não
# regravada: regravar o arquivo apagaria os comentários do usuário.
SECAO_SERVICOS = """
[servicos]
# Modo serviço (rajant_monitor.exe --servico): sobe estes programas em
# segundo plano, sem janela, e os reergue se caírem. Caminho vazio =
# procurar nos lugares de instalação comuns (o servico.log diz o que achou).
# Ex.: prometheus_exe = C:\\prometheus\\prometheus.exe
prometheus_exe     =
prometheus_args    = --config.file=prometheus.yml
prometheus_porta   = 9090
# A pasta bin\\ do Grafana. Ex.: C:\\grafana\\bin\\grafana-server.exe
grafana_exe        =
grafana_porta      = 3000
# false = o supervisor não mexe naquele programa (ex.: Grafana instalado
# como serviço do Windows, que já sobe sozinho).
iniciar_prometheus = true
iniciar_grafana    = true
iniciar_exportador = true
# Espera ao ligar a máquina, antes de subir tudo (a rede ainda está subindo).
espera_inicial_s   = 20
"""

EH_WINDOWS = sys.platform.startswith("win")
log = logging.getLogger("servico")


# ── onde estamos ──────────────────────────────────────────────
def pasta_do_programa():
    """A pasta do .exe (ou do .py). Tudo — config, logs, pid — é relativo a
    ela: iniciado pelo Agendador como SYSTEM, a pasta corrente seria
    C:\\Windows\\System32, e o config.ini nasceria lá."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def comando_do_exportador():
    """Como chamar o exportador: o mesmo .exe, ou o .py pelo mesmo Python."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, str(pasta_do_programa() / "rajant_monitor.py")]


# ── configuração ──────────────────────────────────────────────
def cfg_servicos(cfg, arquivo=None):
    if not cfg.has_section("servicos"):
        if arquivo:
            try:
                with open(arquivo, "a", encoding="utf-8") as f:
                    f.write(SECAO_SERVICOS)
            except OSError:
                pass
        cfg.read_string(SECAO_SERVICOS)
    for k, v in DEFAULTS_SERVICOS.items():
        if not cfg.has_option("servicos", k):
            cfg.set("servicos", k, v)
    return cfg


def _existe(caminho):
    return bool(caminho) and Path(caminho).is_file()


def _procurar(nomes, raizes):
    """O primeiro executável com um dos `nomes`, numa das `raizes`
    (padrões de glob). Só olha onde se costuma instalar: varrer o disco
    atrasaria a subida em minutos."""
    for raiz in raizes:
        for nome in nomes:
            for achado in sorted(glob.glob(os.path.join(raiz, nome))):
                if Path(achado).is_file():
                    return achado
    return None


def raizes_de_instalacao():
    base = pasta_do_programa()
    r = [str(base), str(base.parent)]
    if EH_WINDOWS:
        # Sem administrador, Prometheus e Grafana ficam na pasta do usuário
        # (zip extraído em Downloads, na Área de Trabalho, em Documentos) ou
        # em AppData\Local\Programs, onde instalador "só para mim" põe.
        perfil = os.environ.get("USERPROFILE")
        local = os.environ.get("LOCALAPPDATA")
        if perfil:
            r.append(perfil)
        if local:
            r.append(os.path.join(local, "Programs"))
        for drive in ("C:\\", "D:\\"):
            r += [drive, os.path.join(drive, "Program Files"),
                  os.path.join(drive, "Program Files (x86)")]
    else:
        r += ["/opt", "/usr/local", "/usr/share"]
    return r


def achar_prometheus(cfg):
    exe = cfg.get("servicos", "prometheus_exe", fallback="").strip().strip('"')
    if exe:
        return exe if _existe(exe) else None
    nome = "prometheus.exe" if EH_WINDOWS else "prometheus"
    padroes = [nome, os.path.join("prometheus*", nome),
               os.path.join("*", "prometheus*", nome)]
    return _procurar(padroes, raizes_de_instalacao())


def achar_grafana(cfg):
    """(executável, raiz) do Grafana. A raiz é a pasta com bin/ e conf/."""
    exe = cfg.get("servicos", "grafana_exe", fallback="").strip().strip('"')
    sufixo = ".exe" if EH_WINDOWS else ""
    if not exe:
        padroes = []
        for n in (f"grafana-server{sufixo}", f"grafana{sufixo}"):
            padroes += [os.path.join("GrafanaLabs", "grafana", "bin", n),
                        os.path.join("grafana*", "bin", n),
                        os.path.join("*", "grafana*", "bin", n)]
        exe = _procurar(padroes, raizes_de_instalacao())
    if not _existe(exe):
        return None, None
    return exe, str(Path(exe).resolve().parent.parent)


def comando_grafana(exe, raiz):
    # grafana-server existe em todas as versões; nas novas o mesmo trabalho
    # é `grafana server`.
    if Path(exe).stem.lower() == "grafana":
        return [exe, "server", "--homepath", raiz]
    return [exe, "--homepath", raiz]


# ── portas e processos ────────────────────────────────────────
def porta_aberta(porta, host="127.0.0.1", espera=1.0):
    try:
        with socket.create_connection((host, int(porta)), timeout=espera):
            return True
    except OSError:
        return False


def pid_vivo(pid):
    if not pid:
        return False
    if EH_WINDOWS:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            cod = ctypes.c_ulong()
            k.GetExitCodeProcess(h, ctypes.byref(cod))
            return cod.value == 259                  # STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def matar(pid):
    """Derruba o processo e os filhos dele (o Grafana abre plugins)."""
    if not pid_vivo(pid) or int(pid) == os.getpid():
        return
    if EH_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, creationflags=0x08000000)
        return
    try:
        grupo = os.getpgid(int(pid))
        if grupo == os.getpgid(0):        # mesmo grupo que o nosso: só ele
            raise OSError
        os.killpg(grupo, signal.SIGTERM)
    except OSError:
        try: os.kill(int(pid), signal.SIGTERM)
        except OSError: pass


def pasta_gravavel(pasta):
    """Dá para criar arquivo aqui? `os.access` não serve no Windows: ele
    olha só o atributo somente-leitura, não a permissão da pasta — e
    Program Files sem administrador passa no `os.access` e falha na hora
    de gravar."""
    try:
        Path(pasta).mkdir(parents=True, exist_ok=True)
        teste = Path(pasta) / f".teste_gravacao_{os.getpid()}"
        teste.write_bytes(b"")
        teste.unlink()
        return True
    except OSError:
        return False


def _abrir_oculto():
    """(creationflags, startupinfo) para abrir um programa sem janela.

    Console NOVO e ESCONDIDO — o que o `WshShell.Run cmd, 0` de um .vbs
    faz —, e não processo sem console (CREATE_NO_WINDOW): o exportador
    chama o `ping` do sistema, e programa de console aberto por um processo
    sem console ganha janela própria — uma piscando a cada ping. Com o
    console escondido, o ping o herda e nada aparece."""
    if not EH_WINDOWS:
        return 0, None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0                                   # SW_HIDE
    return subprocess.CREATE_NEW_CONSOLE, si


class Servico:
    """Um programa sob supervisão."""

    ESPERAS = (5, 10, 30, 60)          # entre tentativas, em segundos

    def __init__(self, nome, cmd, pasta, porta=None, log_arq=None, env=None):
        self.nome, self.cmd, self.pasta = nome, cmd, pasta
        self.env = env
        self.porta = int(porta) if porta else None
        self.log_arq = log_arq
        self.proc = None
        self.falhas = 0
        self.proxima = 0.0
        self.externo = False

    @property
    def pid(self):
        return self.proc.pid if self.proc and self.proc.poll() is None else None

    def _abrir_log(self):
        if not self.log_arq:
            return subprocess.DEVNULL
        p = Path(self.log_arq)
        p.parent.mkdir(parents=True, exist_ok=True)
        # Recomeça o arquivo se passou de 20 MB: o Prometheus e o Grafana
        # escrevem bastante, e ninguém lê o log de um mês atrás.
        if p.exists() and p.stat().st_size > 20 * 1024 * 1024:
            p.replace(p.with_suffix(p.suffix + ".1"))
        return open(p, "ab")

    def verificar(self, agora=None):
        """Uma volta de supervisão. Devolve o que aconteceu, para o log."""
        agora = time.monotonic() if agora is None else agora
        if self.proc is not None:
            cod = self.proc.poll()
            if cod is None:
                if self.porta and porta_aberta(self.porta):
                    self.falhas = 0          # está no ar e atendendo
                return None
            self.proc = None
            if self.porta and porta_aberta(self.porta):
                # Saiu, mas a porta é atendida por outro: há um segundo
                # Grafana (serviço do Windows) ou exportador aberto à mão.
                self.externo = True
                return f"{self.nome} saiu (código {cod}); a porta {self.porta} " \
                       f"já é atendida por outro processo — usando esse"
            espera = self.ESPERAS[min(self.falhas, len(self.ESPERAS) - 1)]
            self.falhas += 1
            self.proxima = agora + espera
            return f"{self.nome} parou (código {cod}); nova tentativa em {espera} s"
        if self.porta and porta_aberta(self.porta):
            if not self.externo:
                self.externo = True
                return f"{self.nome} já está no ar na porta {self.porta}; não inicio outro"
            return None
        if self.externo:
            self.externo = False
            msg_ext = f"{self.nome} externo saiu do ar; assumindo"
        else:
            msg_ext = None
        if agora < self.proxima:
            return msg_ext
        flags, si = _abrir_oculto()
        try:
            self.proc = subprocess.Popen(
                self.cmd, cwd=self.pasta, stdin=subprocess.DEVNULL,
                stdout=self._abrir_log(), stderr=subprocess.STDOUT,
                creationflags=flags, startupinfo=si,
                env=(dict(os.environ, **self.env) if self.env else None),
                start_new_session=not EH_WINDOWS)
        except OSError as e:
            espera = self.ESPERAS[min(self.falhas, len(self.ESPERAS) - 1)]
            self.falhas += 1
            self.proxima = agora + espera
            return f"{self.nome} não abriu ({type(e).__name__}: {e}); " \
                   f"nova tentativa em {espera} s"
        return (msg_ext + "; " if msg_ext else "") + \
            f"{self.nome} iniciado (pid {self.proc.pid})"

    def parar(self):
        if self.proc is not None and self.proc.poll() is None:
            matar(self.proc.pid)
        self.proc = None


# ── montagem ─────────────────────────────────────────────────
def montar_servicos(cfg, base=None):
    """[Servico] a partir do config. Devolve (servicos, avisos)."""
    base = Path(base or pasta_do_programa())
    logs = base / PASTA_LOGS
    sv, avisos = [], []
    sim = lambda k: cfg.getboolean("servicos", k, fallback=True)

    if sim("iniciar_prometheus"):
        exe = achar_prometheus(cfg)
        if exe:
            args = cfg.get("servicos", "prometheus_args",
                           fallback=DEFAULTS_SERVICOS["prometheus_args"]).split()
            sv.append(Servico("Prometheus", [exe] + args, str(Path(exe).parent),
                              cfg.get("servicos", "prometheus_porta", fallback="9090"),
                              logs / "prometheus.log"))
            # Ele grava as séries em data\ dentro da própria pasta.
            if not pasta_gravavel(Path(exe).parent / "data"):
                avisos.append(f"Prometheus em pasta sem permissão de gravação "
                              f"({Path(exe).parent}): ele grava em data\\ e não "
                              f"vai subir. Extraia o zip numa pasta sua")
        else:
            avisos.append("Prometheus não encontrado: informe o caminho em "
                          "[servicos] prometheus_exe do config.ini")
    if sim("iniciar_grafana"):
        exe, raiz = achar_grafana(cfg)
        if exe:
            sv.append(Servico("Grafana", comando_grafana(exe, raiz), raiz,
                              cfg.get("servicos", "grafana_porta", fallback="3000"),
                              logs / "grafana.log"))
            if not pasta_gravavel(Path(raiz) / "data"):
                avisos.append(f"Grafana em pasta sem permissão de gravação "
                              f"({raiz}): se ele não for serviço do Windows, "
                              f"não vai subir. Use o zip (standalone) numa "
                              f"pasta sua")
        else:
            avisos.append("Grafana não encontrado: informe o caminho em "
                          "[servicos] grafana_exe do config.ini")
    if sim("iniciar_exportador"):
        porta = cfg.get("servidor", "porta_metrics", fallback="8000")
        # O exportador tem o próprio log (rajant_monitor.log); aqui vai só o
        # que escapar dele — um erro antes de o log abrir, um traceback.
        sv.append(Servico("Exportador", comando_do_exportador(), str(base),
                          porta, logs / "exportador_erros.log",
                          env={"RAJANT_SERVICO": "1"}))
    return sv, avisos


def _gravar_pid(servicos, base):
    dados = {"supervisor": os.getpid(),
             "filhos": {s.nome: s.pid for s in servicos if s.pid}}
    try:
        (Path(base) / ARQ_PID).write_text(json.dumps(dados), encoding="utf-8")
    except OSError:
        pass


def _ler_pid(base):
    try:
        return json.loads((Path(base) / ARQ_PID).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _log_em_arquivo(base):
    h = RotatingFileHandler(Path(base) / ARQ_LOG, maxBytes=5 * 1024 * 1024,
                            backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.handlers[:] = [h]
    if sys.stdout and sys.stdout.isatty():
        log.addHandler(logging.StreamHandler(sys.stdout))
    log.setLevel(logging.INFO)


# ── as ações ─────────────────────────────────────────────────
def rodar(cfg, base=None, voltas=None, intervalo=5.0, espera_inicial=None):
    """O supervisor. `voltas` limita o laço (testes); None = para sempre."""
    base = Path(base or pasta_do_programa())
    os.chdir(base)
    _log_em_arquivo(base)
    ant = _ler_pid(base).get("supervisor")
    if ant and ant != os.getpid() and pid_vivo(ant):
        log.info(f"já há um supervisor no ar (pid {ant}); saindo")
        return 1
    # Já, e não só depois da espera inicial: nesses 20 s um segundo
    # --iniciar não veria ninguém e abriria outro supervisor, e o --parar
    # não acharia o que parar.
    _gravar_pid([], base)
    servicos, avisos = montar_servicos(cfg, base)
    log.info("=" * 60)
    log.info(f"supervisor no ar (pid {os.getpid()}) em {base}")
    for s in servicos:
        log.info(f"  {s.nome:<11} {' '.join(s.cmd)}  [pasta {s.pasta}]")
    for a in avisos:
        log.info(f"  AVISO: {a}")
    if espera_inicial is None:
        espera_inicial = cfg.getint("servicos", "espera_inicial_s", fallback=20)
    if espera_inicial > 0:
        log.info(f"  aguardando {espera_inicial} s antes de subir")
        time.sleep(espera_inicial)

    parar = {"pedido": False}

    def _sinal(*_):
        parar["pedido"] = True
    for sig in (signal.SIGTERM, signal.SIGINT):
        try: signal.signal(sig, _sinal)
        except (ValueError, OSError): pass

    n = 0
    try:
        while not parar["pedido"] and (voltas is None or n < voltas):
            n += 1
            for s in servicos:
                msg = s.verificar()
                if msg:
                    log.info(msg)
            _gravar_pid(servicos, base)
            if voltas is None or n < voltas:
                time.sleep(intervalo)
    finally:
        if parar["pedido"]:
            log.info("pedido de parada: derrubando os serviços")
            for s in servicos:
                s.parar()
            try: (base / ARQ_PID).unlink()
            except OSError: pass
    return 0


def parar_tudo(base=None):
    base = Path(base or pasta_do_programa())
    d = _ler_pid(base)
    if not d:
        print("Nada a parar: nenhum supervisor registrado.")
        return 0
    if d.get("supervisor") and d["supervisor"] != os.getpid():
        matar(d["supervisor"])            # primeiro ele, senão reergue os filhos
        print(f"  supervisor (pid {d['supervisor']}) parado")
    for nome, pid in (d.get("filhos") or {}).items():
        matar(pid)
        print(f"  {nome} (pid {pid}) parado")
    try: (base / ARQ_PID).unlink()
    except OSError: pass
    return 0


def status(cfg, base=None):
    base = Path(base or pasta_do_programa())
    d = _ler_pid(base)
    sup = d.get("supervisor")
    print(f"Supervisor: {'no ar (pid %s)' % sup if sup and pid_vivo(sup) else 'parado'}")
    for nome, porta in (("Prometheus", cfg.get("servicos", "prometheus_porta", fallback="9090")),
                        ("Grafana", cfg.get("servicos", "grafana_porta", fallback="3000")),
                        ("Exportador", cfg.get("servidor", "porta_metrics", fallback="8000"))):
        print(f"  {nome:<11} porta {porta:<5} "
              f"{'respondendo' if porta_aberta(porta) else 'SEM RESPOSTA'}")
    servicos, avisos = montar_servicos(cfg, base)
    for a in avisos:
        print(f"  aviso: {a}")
    ini = inicializacao()
    if ini is not None:
        print("Inicialização: " + ("; ".join(ini) if ini else
              "NENHUMA — depois de reiniciar, nada sobe sozinho"))
    return 0


def xml_da_tarefa(cmd, pasta):
    """Definição da tarefa agendada. Pelo XML, e não pelas opções do
    schtasks: tarefa criada pela linha de comando vem com "parar se rodar
    mais de 3 dias" — o supervisor seria derrubado na quinta-feira."""
    from xml.sax.saxutils import escape
    exe, args = cmd[0], " ".join(cmd[1:])
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Rajant Monitor: exportador, Prometheus e Grafana em segundo plano.</Description>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger><Enabled>true</Enabled></BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>S-1-5-18</UserId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(exe)}</Command>
      <Arguments>{escape(args)}</Arguments>
      <WorkingDirectory>{escape(str(pasta))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def instalar_tarefa(base=None):
    if not EH_WINDOWS:
        print("A tarefa agendada é do Windows. No Linux, use um serviço do "
              "systemd chamando:  rajant_monitor.py --servico")
        return 1
    base = Path(base or pasta_do_programa())
    cmd = comando_do_exportador() + ["--servico"]
    xml = base / "tarefa_rajant_monitor.xml"
    xml.write_text(xml_da_tarefa(cmd, base), encoding="utf-16")
    r = subprocess.run(["schtasks", "/Create", "/TN", NOME_TAREFA, "/XML",
                        str(xml), "/F"], capture_output=True, text=True)
    print((r.stdout or "") + (r.stderr or ""))
    if r.returncode != 0:
        print("Não foi possível criar a tarefa. Rode como administrador "
              "(botão direito > Executar como administrador).\n"
              "Sem administrador nesta máquina: instalar_inicializacao_usuario.bat "
              "(sobe no logon do seu usuário).")
        return r.returncode
    subprocess.run(["schtasks", "/Run", "/TN", NOME_TAREFA], capture_output=True)
    print(f"Tarefa '{NOME_TAREFA}' criada e iniciada. Ela sobe sozinha a cada "
          f"vez que o Windows liga, mesmo sem ninguém fazer login.")
    return 0


def remover_tarefa(base=None):
    parar_tudo(base)
    if not EH_WINDOWS:
        return 0
    r = subprocess.run(["schtasks", "/Delete", "/TN", NOME_TAREFA, "/F"],
                       capture_output=True, text=True)
    print((r.stdout or "") + (r.stderr or ""))
    return r.returncode


# ── inicialização sem administrador ──────────────────────────
# HKCU\...\Run: o Windows roda o comando no logon DESTE usuário. Não pede
# administrador — é a chave que Teams e OneDrive usam para abrir com o
# Windows. Limite: só roda depois do login, e o logoff encerra tudo.
CHAVE_RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"


def linha_de_inicio():
    """O comando gravado no Run. Entre aspas: a pasta pode ter espaço."""
    return subprocess.list2cmdline(comando_do_exportador() + ["--iniciar"])


def _run_ler():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CHAVE_RUN) as k:
            return winreg.QueryValueEx(k, NOME_TAREFA)[0]
    except OSError:
        return None


def _run_gravar(valor):
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, CHAVE_RUN, 0,
                            winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, NOME_TAREFA, 0, winreg.REG_SZ, valor)


def _run_apagar():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CHAVE_RUN, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, NOME_TAREFA)
        return True
    except OSError:
        return False


def _tarefa_existe():
    r = subprocess.run(["schtasks", "/Query", "/TN", NOME_TAREFA],
                       capture_output=True, creationflags=0x08000000)
    return r.returncode == 0


def inicializacao():
    """O que faz o monitor subir sozinho, em texto. None fora do Windows."""
    if not EH_WINDOWS:
        return None
    achados = []
    try:
        if _tarefa_existe():
            achados.append(f"tarefa '{NOME_TAREFA}' (sobe com a máquina)")
    except OSError:
        pass
    valor = _run_ler()
    if valor:
        txt = f"logon de {os.environ.get('USERNAME', 'este usuário')}"
        if valor != linha_de_inicio():
            # Pasta movida ou copiada: o logon abriria a versão antiga.
            txt += f" — ATENÇÃO, aponta para outro lugar: {valor}"
        achados.append(txt)
    return achados


def iniciar(base=None):
    """Sobe o supervisor oculto e volta na hora. É o que o logon chama: a
    janela deste processo abre e fecha, e o supervisor fica num console
    escondido."""
    base = Path(base or pasta_do_programa())
    ant = _ler_pid(base).get("supervisor")
    if ant and pid_vivo(ant):
        print(f"O supervisor já está no ar (pid {ant}).")
        return 0
    flags, si = _abrir_oculto()
    p = subprocess.Popen(comando_do_exportador() + ["--servico"], cwd=str(base),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags,
                         startupinfo=si, start_new_session=not EH_WINDOWS)
    print(f"Supervisor iniciado em segundo plano (pid {p.pid}). "
          f"Registro: {base / ARQ_LOG}")
    return 0


def instalar_usuario(base=None):
    base = Path(base or pasta_do_programa())
    if not EH_WINDOWS:
        print("A inicialização no logon é do Windows. No Linux, use um "
              "serviço do systemd --user chamando:  rajant_monitor.py --servico")
        return 1
    if not pasta_gravavel(base):
        print(f"Esta pasta não aceita gravação pelo seu usuário: {base}\n"
              f"O monitor grava config.ini, logs e cache ao lado do .exe. Copie "
              f"a pasta inteira para um lugar seu, por exemplo "
              f"{Path(os.environ.get('USERPROFILE', 'C:/Users/voce')) / 'RajantMonitor'}, "
              f"e rode de lá.")
        return 1
    linha = linha_de_inicio()
    try:
        _run_gravar(linha)
    except OSError as e:
        print(f"Não foi possível gravar a inicialização do usuário: {e}")
        return 1
    print(f"Pronto: o Rajant Monitor sobe sozinho quando "
          f"{os.environ.get('USERNAME', 'este usuário')} entrar no Windows, "
          f"sem administrador.\n"
          f"  Comando: {linha}\n"
          f"  Atenção: com a máquina ligada e ninguém logado, nada roda; "
          f"logoff encerra tudo. Bloquear a tela (Win+L) não para nada.")
    return iniciar(base)


def remover_usuario(base=None):
    parar_tudo(base)
    if not EH_WINDOWS:
        return 0
    print("Inicialização no logon removida." if _run_apagar()
          else "Não havia inicialização no logon.")
    return 0


ACOES = {"--servico", "--iniciar", "--parar", "--status",
         "--instalar-usuario", "--remover-usuario",
         "--instalar-servico", "--remover-servico"}


def executar(acao, cfg_loader):
    """Ponto de entrada a partir do rajant_monitor.main."""
    base = pasta_do_programa()
    os.chdir(base)
    if acao == "--parar":
        return parar_tudo(base)
    if acao == "--iniciar":
        return iniciar(base)
    if acao == "--instalar-usuario":
        return instalar_usuario(base)
    if acao == "--remover-usuario":
        return remover_usuario(base)
    if acao == "--instalar-servico":
        return instalar_tarefa(base)
    if acao == "--remover-servico":
        return remover_tarefa(base)
    cfg = cfg_servicos(cfg_loader(), base / "config.ini")
    if acao == "--status":
        return status(cfg, base)
    return rodar(cfg, base)
