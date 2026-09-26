#!/usr/bin/env python3
"""
Coleta de site survey direto dos rádios Rajant.

Duplo clique no executável abre a janela: ele descobre a malha, lista os
equipamentos, você marca quais quer, aperta Iniciar. Ao parar, saem o KMZ
com o rastro de calor, o PPT na identidade Anglo e o Excel.

Diferente do `survey_meshmapper`, que lê arquivo do MeshMapper, este
FALA COM OS RÁDIOS. Precisa de rede até a malha e da biblioteca
rajant-api.

Também funciona por linha de comando:

    coleta_rajant.exe --seeds 10.188.96.140 --minutos 30 -o relatorios

A diferença para o MeshMapper: ele lê UM rádio por notebook; aqui um
processo só lê todos, e cada veículo reporta a própria posição junto com
a própria vizinhança — na mesma leitura de State.
"""
import sys, os, time, threading, queue, argparse, traceback
from pathlib import Path

try:
    import rajant_monitor as rm
except ImportError:
    print("ERRO: rajant_monitor.py precisa estar na mesma pasta.")
    sys.exit(1)

TITULO = "Coleta Rajant — Site Survey"

# Alvo de espaçamento, em metros, entre amostras do MESMO equipamento.
# É o que o usuário controla, em vez de segundos: o que o mapa precisa é
# densidade no espaço. 10 m é a densidade do MeshMapper — uma leitura por
# segundo a ~40 km/h. Parado, não gasta leitura nenhuma.
PASSO_M_PADRAO = 10.0

# Rádio parado ainda é lido de vez em quando: para confirmar que continua
# parado e, principalmente, para perceber quando SAIU. Com 30 s, um
# caminhão que arrancava rodava até 300 m antes da primeira leitura em
# movimento — um buraco no começo de cada viagem.
INTERVALO_PARADO_S = 10.0

# O GPS do rádio atualiza ~1 vez por segundo. Ler de novo antes disso
# devolve a mesma posição, que é descartada — leitura gasta à toa.
MIN_RELEITURA_S = 1.0

# Leituras simultâneas. A consulta é enxuta (só gps, wireless e system) e
# cada uma vai para um rádio diferente; o que limita a densidade do mapa
# é quantas cabem por segundo.
LEITURAS_SIMULTANEAS = 24


# ══════════════════════════════════════════════════════════════
# AGENDA POR DESLOCAMENTO
# ══════════════════════════════════════════════════════════════
# Ler todo mundo a cada N segundos desperdiça o orçamento em quem está
# parado: na captura real da ERM-12 foram 1.095 leituras e ZERO metro
# coberto, e no CA-1006 mais da metade das leituras saiu com o veículo
# imóvel.
#
# Aqui a pergunta é outra: quem já andou o suficiente para merecer uma
# leitura nova? Com alvo de 15 m e 40 veículos a 40 km/h dá ~30 leituras
# por segundo — que cabem em meia dúzia de threads. Os parados custam uma
# leitura a cada 30 s cada um.
class Agenda:
    """Decide quem ler agora, por distância percorrida e não por relógio."""

    def __init__(self, passo_m=PASSO_M_PADRAO, parado_s=INTERVALO_PARADO_S,
                 min_releitura_s=MIN_RELEITURA_S):
        self.passo_m  = float(passo_m)
        self.parado_s = float(parado_s)
        self.min_releitura_s = float(min_releitura_s)
        self.ultimo   = {}      # ip -> {"ts", "lat", "lon", "vel"}: última posição NOVA
        self.tentativa = {}     # ip -> quando foi lido, com ou sem posição nova
        self._lock    = threading.Lock()

    def registrar(self, ip, ts, lat, lon, vel_kmh=None):
        """Leitura com posição nova (ou falha, com lat/lon None).

        `gpsVel` é opcional no Gps.proto. Sem ele, a agenda caía no ritmo
        do rádio parado — um caminhão a 40 km/h lido a cada 30 s, amostras
        a 300 m uma da outra. A velocidade sai então do próprio
        deslocamento entre as duas últimas posições.
        """
        with self._lock:
            self.tentativa[ip] = ts
            u = self.ultimo.get(ip)
            if (vel_kmh is None and lat is not None and lon is not None
                    and u and u.get("lat") is not None):
                dt = ts - u["ts"]
                if dt >= 0.5:
                    vel_kmh = (rm._dist_m(u["lat"], u["lon"], lat, lon)
                               / dt * 3.6)
            self.ultimo[ip] = {"ts": ts, "lat": lat, "lon": lon,
                               "vel": vel_kmh}

    def adiar(self, ip, ts):
        """Leu, mas o GPS não tinha posição nova. Não mexe na base do
        deslocamento — só segura a releitura até o próximo fix."""
        with self._lock:
            self.tentativa[ip] = ts

    def prioridade(self, ip, agora):
        """Quanto este rádio está 'devendo'. Maior = ler antes.

        Sem posição conhecida ainda, prioridade máxima: é a primeira
        leitura dele. Com posição, estima o quanto andou desde a última
        pela velocidade — a reportada pelo rádio ou, sem ela, a do
        deslocamento. É o que permite ler o caminhão rápido mais vezes
        sem ler o parado à toa.
        """
        t = self.tentativa.get(ip)
        if t is not None and agora - t < self.min_releitura_s:
            return 0.0
        u = self.ultimo.get(ip)
        if u is None:
            return float("inf")
        dt = max(0.0, agora - u["ts"])
        vel = u.get("vel")
        if not vel:
            # Sem velocidade, cai no tempo: o rádio parado (ou sem GPS)
            # entra a cada `parado_s`. Zero não serve — nunca seria lido.
            return dt / self.parado_s
        andou = (float(vel) / 3.6) * dt
        # Empata com o do tempo para o rádio lento não ficar eternamente
        # atrás do rápido e nunca ser lido.
        return max(andou / self.passo_m, dt / self.parado_s)

    def eleger(self, ips, agora, quantos):
        """Os `quantos` que mais estão devendo, e só os que já venceram."""
        cands = [(self.prioridade(ip, agora), ip) for ip in ips]
        cands = [c for c in cands if c[0] >= 1.0]
        cands.sort(key=lambda c: -c[0])
        return [ip for _, ip in cands[:max(1, int(quantos))]]


# ══════════════════════════════════════════════════════════════
# A COLETA — sem nada de interface, para poder ser testada
# ══════════════════════════════════════════════════════════════
class Coleta:
    """Lê os rádios escolhidos e grava amostras e vizinhos.

    `parar()` encerra no fim do ciclo em andamento; o relatório sai do
    que já foi gravado, então interromper no meio não perde a coleta.
    """

    def __init__(self, alvos, role="co", senha="", porta=2300,
                 passo_m=PASSO_M_PADRAO, max_threads=LEITURAS_SIMULTANEAS,
                 timeout_s=6,
                 cfg=None, nome=None, reencontro_s=60.0,
                 parado_s=INTERVALO_PARADO_S, aviso=print):
        self.alvos    = {ip: (nomes or ip) for ip, nomes in dict(alvos).items()}
        self.role     = role
        self.senha    = senha
        self.porta    = porta
        self.agenda   = Agenda(passo_m, parado_s)
        self.max_thr  = max(1, int(max_threads))
        self.timeout  = timeout_s
        # Quanto esperar antes de tentar de novo um radio descartado.
        # Curto demais gasta o orcamento em quem esta fora; longo demais
        # perde o caminhao que voltou.
        self.reencontro_s = float(reencontro_s)
        self.cfg      = rm.cfg_survey(cfg)
        self.nome     = nome or f"Coleta {time.strftime('%d/%m %H:%M')}"
        self.aviso    = aviso
        self._parar   = threading.Event()
        self.sessoes  = {}
        self.amostras = []
        self.peers    = []
        self.lock     = threading.Lock()
        # gpsTime da última amostra GRAVADA de cada rádio. É o que impede
        # o ponto repetido: se o módulo não atualizou a posição, a leitura
        # nova traz a posição velha, e gravá-la empilharia amostras no
        # mesmo lugar — que foi o que encheu a captura da ERM-12.
        self.ultimo_fix = {}
        self.motivos    = {}   # motivo da falha -> quantas vezes
        self.desistencias = {}  # ip -> quando a sessao desistiu dele
        self.n_repetidos = 0
        self.n_lidos     = 0
        self.n_falhas    = 0
        self.inicio      = None
        self.fim         = None
        self.em_voo      = {}   # ip -> quando a leitura em curso começou
        self.duracoes    = []   # segundos das últimas leituras, para a tela

    def parar(self):
        self._parar.set()

    # ── uma leitura ──────────────────────────────────────────
    def _ler(self, ip):
        ses = self.sessoes.get(ip)
        # `SessaoRadio` DESISTE do rádio após 3 falhas seguidas e nunca
        # mais tenta. Para o survey curto de onde ela veio isso está
        # certo; para uma coleta de turno, não: um caminhão que passa
        # vinte segundos atrás de uma bancada sairia do levantamento para
        # o resto do dia. Aqui ele volta a ser tentado depois de uma
        # pausa — a política de reencontro é do coletor, não da sessão.
        if ses is not None and ses.desistiu:
            quando = self.desistencias.get(ip)
            agora = time.monotonic()
            if quando is None:
                self.desistencias[ip] = agora
                return None
            if agora - quando < self.reencontro_s:
                return None
            self.desistencias.pop(ip, None)
            try: ses.fechar()
            except Exception: pass
            ses = None
            self.sessoes.pop(ip, None)
            self.aviso(f"tentando {self.alvos.get(ip, ip)} de novo")
        if ses is None:
            # POR NOME, nunca por posição. A assinatura é
            # SessaoRadio(ip, porta, role, senha, ...) e eu passei
            # (ip, role, senha, porta): a porta virou "VIEW", o usuário
            # virou a senha e a senha virou 2300. Toda conexão falhou —
            # 139 falhas e zero amostra —, e como os argumentos eram
            # posicionais, nada acusou.
            ses = rm.SessaoRadio(ip, porta=self.porta, role=self.role,
                                 senha=self.senha, timeout=self.timeout)
            self.sessoes[ip] = ses
        txt = ses.estado_bruto()
        if not txt:
            # A sessão guarda o motivo; sem trazê-lo para cá o usuário vê
            # só um contador de falhas subindo, sem nenhuma pista.
            raise RuntimeError(ses.ultimo_erro or "sem resposta")
        d = rm.parse_state(txt)
        # Interferência e CPU não saem de UMA leitura: os contadores de
        # canal são cumulativos desde o boot, e a razão entre eles daria a
        # média da vida inteira do rádio. `calcular_taxas` guarda a
        # leitura anterior por IP e devolve a taxa do intervalo. Sem esta
        # chamada, `interf_pct` fica None para sempre.
        return rm.calcular_taxas(ip, d, time.monotonic())

    def _amostra(self, ip, d, agora):
        s = d["sistema"]
        nome = s.get("nome") or self.alvos.get(ip) or ip
        lat, lon = s.get("gps_lat"), s.get("gps_lon")
        fix = s.get("gps_time")

        if lat is None or lon is None:
            # Sem posição não vai para o mapa. Não é erro: parte da frota
            # não tem módulo de GPS, e o rádio ainda serve ao censo.
            self.agenda.registrar(ip, agora, None, None, None)
            return None
        # Posição repetida: o módulo não atualizou desde a última gravada.
        # Comparar o gpsTime BRUTO funciona em qualquer formato — não
        # depende de saber a unidade, que o Gps.proto não declara.
        if fix is not None and self.ultimo_fix.get(ip) == fix:
            # Não vira base de deslocamento: registrar a posição velha com
            # o relógio de agora faria a velocidade estimada dar zero, e o
            # caminhão em movimento cairia no ritmo de parado.
            self.agenda.adiar(ip, agora)
            with self.lock:
                self.n_repetidos += 1
            return None
        if fix is not None:
            self.ultimo_fix[ip] = fix
        self.agenda.registrar(ip, agora, lat, lon, s.get("gps_vel"))

        # O melhor enlace do ponto, e TUDO que ele mede junto. Guardar só
        # o RSSI deixava o laudo sem SNR e sem ruído — as duas grandezas
        # sumiam do KMZ e do PPT por falta de dado, não por não existirem.
        bom = None
        vizinhos = []
        for r in d.get("radios") or []:
            # Piso de ruído é do RÁDIO, não do vizinho: é a mesma antena
            # ouvindo. 0 aqui é "não medido", como em todo campo em dBm.
            ruido_r = r.get("ruido") or None
            banda_r = rm._norm_banda(r.get("freq")) if r.get("freq") else None
            for p in (r.get("peers") or []):
                # MESMA regra do arquivo: `_i()` devolve 0 para campo
                # ausente, e 0 dBm ganharia a eleição do melhor sinal.
                sig, snr, custo, _ru = rm.limpar_enlace(
                    p.get("sinal"), p.get("snr"), p.get("custo"))
                vizinhos.append({
                    "nome": p.get("nome") or p.get("ip"),
                    "ip": p.get("ip"), "encap": p.get("encap"),
                    "sinal": sig, "snr": snr,
                    "custo": custo, "taxa": p.get("taxa") or None,
                    "banda": banda_r, "canal": r.get("canal"),
                })
                if sig is not None and (bom is None or sig > bom["sinal"]):
                    bom = {"sinal": sig, "snr": snr, "custo": custo,
                           "taxa": p.get("taxa") or None,
                           "ruido": ruido_r, "banda": banda_r,
                           "canal": r.get("canal"),
                           "servidor": vizinhos[-1]["nome"],
                           "interf": r.get("interf_pct")}
        bom = bom or {}

        # SNR preferido do próprio enlace; sem ele, recuperado de
        # sinal − ruído, que é a definição. Nunca inventado.
        snr_f = bom.get("snr")
        if snr_f is None and bom.get("sinal") is not None and bom.get("ruido"):
            snr_f = round(bom["sinal"] - bom["ruido"], 1)

        return {
            "radio": nome, "ts": time.time(),
            "lat": lat, "lon": lon, "alt": s.get("gps_alt"),
            "vel": s.get("gps_vel"), "sats": s.get("gps_sats"),
            "hdop": s.get("gps_hdop"),
            "sinal": bom.get("sinal"), "snr": snr_f, "ruido": bom.get("ruido"),
            # Latência e perda o rádio não mede — ficam None, e sem
            # medição a grandeza não vira aba nem slide.
            "rtt": None, "perda": None,
            # Interferência a coleta ao vivo TEM, e o MeshMapper nunca deu:
            # é a fração do meio ocupada por transmissor alheio. Só aparece
            # a partir da segunda leitura do mesmo rádio (os contadores são
            # cumulativos desde o boot; ver calcular_taxas).
            "interf": bom.get("interf"), "vazao": None,
            "custo": bom.get("custo"), "taxa": bom.get("taxa"),
            "peers": len(vizinhos),
            "banda": bom.get("banda"), "canal": bom.get("canal"),
            "servidor": bom.get("servidor"),
            "fonte": "direto",
        }, vizinhos

    # ── o laço ───────────────────────────────────────────────
    def _proximo(self):
        """O rádio que mais está devendo leitura e não está sendo lido."""
        with self.lock:
            livres = [ip for ip in self.alvos if ip not in self.em_voo]
            eleitos = self.agenda.eleger(livres, time.monotonic(), 1)
            if not eleitos:
                return None
            self.em_voo[eleitos[0]] = time.monotonic()
            return eleitos[0]

    def _tratar(self, ip, d, dur):
        """Resultado de UMA leitura: amostra, falha ou posição repetida."""
        if isinstance(d, Exception) or d is None:
            with self.lock:
                self.n_falhas += 1
                motivo = (f"{type(d).__name__}: {d}"
                          if isinstance(d, Exception) else "sem resposta")
                # Um contador subindo não diz nada. Cada motivo NOVO vai
                # para a tela uma vez; repetido só conta. Foi um "139
                # falhas" mudo que escondeu uma troca de argumentos por
                # quase uma hora.
                novo = motivo not in self.motivos
                self.motivos[motivo] = self.motivos.get(motivo, 0) + 1
            if novo:
                nome = self.alvos.get(ip, ip)
                self.aviso(f"falha em {nome} ({ip}): {motivo}")
            self.agenda.registrar(ip, time.monotonic(), None, None, None)
            return
        with self.lock:
            self.n_lidos += 1
            self.duracoes.append(dur)
            del self.duracoes[:-200]
        saida = self._amostra(ip, d, time.monotonic())
        if not saida:
            return
        am, viz = saida
        with self.lock:
            # `ponto` é a POSIÇÃO da amostra na lista: `cobertura_disponivel`
            # casa vizinho e amostra por ela. Com várias leituras
            # terminando ao mesmo tempo, o número tem de sair da própria
            # lista, dentro da trava, e não de um contador à parte.
            self.amostras.append(am)
            ponto = len(self.amostras)
            for v in viz:
                v["ponto"] = ponto
                v["ts"] = am["ts"]
                v["lat"] = am["lat"]; v["lon"] = am["lon"]
                v["movel"] = am["radio"]
                v["ruido"] = ((v["sinal"] - v["snr"])
                              if v.get("sinal") is not None
                              and v.get("snr") is not None else None)
                self.peers.append(v)

    def rodar(self, minutos=0):
        """Coleta até `parar()` ou até `minutos` (0 = sem limite).

        Cada trabalhador pega o rádio mais atrasado, lê, grava e já pega o
        próximo. Antes era em LOTES: elegia 12, esperava os 12 terminarem
        e só então elegia de novo. Um rádio lento ou fora de alcance
        segurava o lote inteiro até o timeout de 6 s — e um caminhão a
        40 km/h anda 67 m em 6 s. Era esse o espaçamento de 60–80 m entre
        as bolhas do mapa, com o alvo pedindo 15 m.
        """
        if not self.alvos:
            raise RuntimeError("nenhum equipamento selecionado")
        self.inicio = time.time()
        limite = (self.inicio + minutos * 60.0) if minutos else None
        self.aviso(f"coletando {len(self.alvos)} equipamento(s)")
        fim = threading.Event()
        trabalhadores = []

        def _trabalhar():
            while not fim.is_set():
                ip = self._proximo()
                if ip is None:
                    # Ninguém venceu o alvo de metros ainda. O caminhão
                    # rápido vence em pouco mais de um segundo.
                    fim.wait(0.1)
                    continue
                t0 = time.monotonic()
                try:
                    d = self._ler(ip)
                except Exception as e:
                    d = e
                try:
                    self._tratar(ip, d, time.monotonic() - t0)
                except Exception as e:
                    self.aviso(f"falha interna ao gravar {ip}: {e}")
                finally:
                    with self.lock:
                        self.em_voo.pop(ip, None)

        def _novo():
            t = threading.Thread(target=_trabalhar, daemon=True)
            t.start(); trabalhadores.append(t)

        for _ in range(self.max_thr):
            _novo()
        repostos = 0
        try:
            while not self._parar.is_set():
                if limite and time.time() >= limite:
                    break
                # A rajant-api não recebe timeout: uma consulta pendurada
                # prende o trabalhador para sempre. Cada leitura presa além
                # do prazo ganha um substituto, com teto, para a coleta
                # não ir perdendo fôlego ao longo do turno.
                agora = time.monotonic()
                with self.lock:
                    presos = sum(1 for t0 in self.em_voo.values()
                                 if agora - t0 > self.timeout + 5)
                if presos > repostos and len(trabalhadores) < 2 * self.max_thr:
                    repostos += 1
                    _novo()
                self._parar.wait(0.2)
        finally:
            fim.set()
            prazo = time.monotonic() + self.timeout + 5
            for t in trabalhadores:
                t.join(max(0.0, prazo - time.monotonic()))

        self.fim = time.time()
        for ses in self.sessoes.values():
            try: ses.fechar()
            except Exception: pass
        if self.motivos:
            # Ordenado do motivo mais frequente para o menos: com dezenas
            # de radios, e o primeiro que explica a coleta inteira.
            for m, n in sorted(self.motivos.items(), key=lambda kv: -kv[1])[:5]:
                self.aviso(f"  {n}x  {m}")
        self.aviso(f"coleta encerrada: {len(self.amostras)} amostras, "
                   f"{len(self.peers)} leituras de vizinho")
        return self.amostras, self.peers

    # ── estado, para a janela mostrar ────────────────────────
    def status(self):
        with self.lock:
            n_am = len(self.amostras)
            moveis = len({a["radio"] for a in self.amostras})
            dur = (time.time() - self.inicio) if self.inicio else 0.0
            ds = sorted(self.duracoes)
            return {
                "amostras": n_am, "vizinhos": len(self.peers),
                "equipamentos": moveis, "lidos": self.n_lidos,
                "falhas": self.n_falhas, "repetidos": self.n_repetidos,
                "duracao_s": dur,
                "leituras_s": (self.n_lidos / dur) if dur > 0 else 0.0,
                # Quanto demora UMA leitura. Com o passo real acima do
                # pedido, é este número que diz por quê: leitura lenta
                # pede mais leituras simultâneas.
                "leitura_ms": (round(ds[len(ds) // 2] * 1000) if ds else None),
                "passo_m": self._passo_real(),
            }

    def _passo_real(self):
        """Espaçamento que de fato saiu, por equipamento. É o número que
        diz se o alvo está sendo cumprido — o pedido é intenção."""
        por = {}
        for a in self.amostras:
            por.setdefault(a["radio"], []).append(a)
        ds = []
        for pts in por.values():
            pts = sorted(pts, key=lambda x: x["ts"])
            for p, q in zip(pts, pts[1:]):
                ds.append(rm._dist_m(p["lat"], p["lon"], q["lat"], q["lon"]))
        if not ds:
            return None
        ds.sort()
        return round(ds[len(ds) // 2], 1)


def gravar_e_gerar(coleta, saida, fazer_kmz=True, fazer_ppt=True,
                   fazer_excel=True, aviso=print):
    """Grava o survey no banco e emite os relatórios. Devolve os arquivos."""
    am, pr = coleta.amostras, coleta.peers
    if not am:
        raise RuntimeError("nenhuma amostra com posição foi coletada")
    destino = Path(saida or ".").resolve()
    destino.mkdir(parents=True, exist_ok=True)

    sv = {"nome": coleta.nome, "movel": ", ".join(sorted({a["radio"] for a in am})),
          "inicio": coleta.inicio, "fim": coleta.fim,
          "intervalo_s": None, "arquivo": "coleta ao vivo",
          "versao_bcc": None, "limiares_mm": {}}
    sv["cobertura"] = rm.cobertura_disponivel(am, pr)

    con = rm.banco()
    try:
        radios = sorted({a["radio"] for a in am})
        sid = rm.survey_criar(con, sv["nome"], sv["inicio"], None, radios)
        rm.amostras_gravar(con, sid, am)
        rm.survey_fechar(con, sid, sv["fim"], rm.survey_resumo(am),
                         n_moveis=len(radios), n_fixos=0,
                         intervalo_efetivo_s=rm._mm_intervalo_real(am))
    finally:
        con.close()

    cfg = rm.cfg_relatorio(rm.carregar_config())
    campos = rm.campos_com_medicao(am)
    com_viz = rm._cfg_bool(cfg, "relatorio", "vizinhanca", False)
    sitios = rm.sitios_parados(am, pr) if com_viz else []
    feitos = []
    if fazer_kmz and campos:
        # Um arquivo por banda — ver `_kmz_por_banda` no survey_meshmapper,
        # que é a mesma regra: 2,4 e 5,8 GHz são malhas diferentes no
        # mesmo terreno e num arquivo só uma tapa a outra.
        import survey_meshmapper as _smm
        feitos += _smm._kmz_por_banda(sv, am, pr, destino, cfg, campos, aviso)
    if fazer_kmz and sitios:
        try:
            dados, nome = rm.gerar_kml_pontos_fixos(sitios, cfg=cfg)
            alvo = destino / nome; alvo.write_bytes(dados); feitos.append(alvo)
        except Exception as e:
            aviso(f"  ! KMZ da vizinhança não gerado: {e}")
    if fazer_ppt:
        try:
            dados, nome = rm.ppt_survey_anglo(sid, cfg, sitios=sitios)
            alvo = destino / nome; alvo.write_bytes(dados); feitos.append(alvo)
        except Exception as e:
            aviso(f"  ! PPT não gerado: {e}")
    if fazer_excel:
        try:
            dados, nome = rm.excel_do_meshmapper(sv, am, pr, cfg)
            alvo = destino / nome; alvo.write_bytes(dados); feitos.append(alvo)
        except Exception as e:
            aviso(f"  ! Excel não gerado: {e}")
    for f in feitos:
        aviso(f"  → {f.name}")
    return feitos


def abrir_pasta(caminho):
    import subprocess
    try:
        if sys.platform.startswith("win"): os.startfile(str(caminho))
        elif sys.platform == "darwin":     subprocess.Popen(["open", str(caminho)])
        else:                              subprocess.Popen(["xdg-open", str(caminho)])
        return True
    except Exception:
        return False


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        # A janela vive em site_survey.py, que junta coleta e arquivos.
        print("Sem argumentos: abra o site_survey (a janela).\n"
              "Pela linha de comando:\n"
              "  coleta_rajant --seeds 10.188.96.140 --minutos 30 -o saida")
        return 2

    ap = argparse.ArgumentParser(
        prog="coleta_rajant", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seeds", default="",
                    help="IPs de partida, separados por vírgula. A busca "
                         "já parte da malha conhecida no código; isto "
                         "acrescenta endereços.")
    ap.add_argument("--lista", metavar="ARQ",
                    help="arquivo com IPs (o rajant_ips_cache.json, ou um "
                         "IP por linha) para usar como partida")
    ap.add_argument("--role", default="co")
    ap.add_argument("--senha", default="")
    ap.add_argument("--porta", type=int, default=2300)
    ap.add_argument("--minutos", type=float, default=10.0)
    ap.add_argument("--passo-m", type=float, default=PASSO_M_PADRAO,
                    help="alvo de espaçamento entre amostras, em metros")
    ap.add_argument("--so-com-gps", action="store_true",
                    help="coleta só os rádios que reportam posição")
    ap.add_argument("-o", "--saida", default=".")
    a = ap.parse_args(argv)

    seeds = [s.strip() for s in a.seeds.replace(";", ",").split(",") if s.strip()]
    if a.lista:
        seeds += list(rm.ler_lista_de_ips(a.lista))
    achados = rm.descobrir_malha(seeds, role=a.role, senha=a.senha,
                                 porta=a.porta, aviso=print)
    alvos = {ip: v["nome"] for ip, v in achados.items()
             if not v.get("erro") and (v.get("tem_gps") or not a.so_com_gps)}
    if not alvos:
        print("nenhum equipamento respondeu"); return 1
    print(f"{len(alvos)} equipamento(s); coletando por {a.minutos:g} min")
    c = Coleta(alvos, role=a.role, senha=a.senha, porta=a.porta,
               passo_m=a.passo_m, aviso=print)
    c.rodar(a.minutos)
    feitos = gravar_e_gerar(c, a.saida, aviso=print)
    print(f"\n{len(feitos)} arquivo(s) gerado(s).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrompido"); sys.exit(130)
