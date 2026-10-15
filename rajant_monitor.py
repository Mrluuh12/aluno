"""
RAJANT PROMETHEUS EXPORTER — v2.3
Correções completas conforme BCE User Guide v11.25 + pesquisa
"""
import ssl
if not hasattr(ssl, 'wrap_socket'):
    def _wrap(sock, keyfile=None, certfile=None, server_side=False,
               cert_reqs=ssl.CERT_NONE, ssl_version=None, ca_certs=None,
               do_handshake_on_connect=True, suppress_ragged_eofs=True, ciphers=None):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT if not server_side else ssl.PROTOCOL_TLS_SERVER)
        ctx.check_hostname = False; ctx.verify_mode = cert_reqs
        if certfile: ctx.load_cert_chain(certfile, keyfile)
        if ca_certs:  ctx.load_verify_locations(ca_certs)
        if ciphers:   ctx.set_ciphers(ciphers)
        return ctx.wrap_socket(sock, server_side=server_side,
                               do_handshake_on_connect=do_handshake_on_connect,
                               suppress_ragged_eofs=suppress_ragged_eofs)
    ssl.wrap_socket = _wrap

import re, os, json, time, logging, argparse, threading, configparser, sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from collections import deque
from datetime import datetime
from pathlib import Path

# A rajant-api so e necessaria para FALAR com os radios. O relatorio
# (Excel/PPT) le so o Prometheus, e sair com erro no import impediria de
# gerar relatorio numa maquina sem a biblioteca.
try:
    from rajant_api import Breadcrumb
except ImportError:
    Breadcrumb = None


def exigir_rajant_api():
    """Chamada nos caminhos que precisam de radio de verdade."""
    if Breadcrumb is None:
        print("ERRO: esta operacao fala com os radios e precisa da "
              "rajant-api.\n      pip install rajant-api --no-deps\n"
              "      pip install \"protobuf==4.23.4\"")
        sys.exit(1)
    return Breadcrumb
try:
    from prometheus_client import start_http_server, Gauge
except ImportError:
    print("ERRO: pip install prometheus-client"); sys.exit(1)

# ══════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════
CONFIG_FILE = "config.ini"
# Arquivo do cache antigo: não é mais lido nem gravado (a lista está no
# código, REDE_CONHECIDA). Só para avisar que pode ser apagado.
CACHE_ANTIGO = "rajant_ips_cache.json"

DEFAULT_CONFIG = """\
[rede]
seeds                   = 10.188.96.140
password                = anglomesh-view
role                    = VIEW
porta_bcapi             = 2300

[coleta]
# BCE recomenda 300s para redes grandes (< 60s gera ~5TB/ano com 147 BCs)
intervalo_segundos      = 60
max_threads             = 30
timeout_conexao         = 15
tentativas_por_bc       = 3
falhas_antes_offline    = 3
# Segundos que as séries de um BC que parou de responder continuam no
# /metrics com o último valor. 0 = saem na primeira coleta que falha, e o
# Grafana mostra o buraco em vez de uma linha reta no último valor.
segurar_serie_s         = 0
redescoberta_ciclos     = 10
# Os IPs da frota estão no código (REDE_CONHECIDA) e são coletados sempre.
# Descoberta: a partir deles, segue os vizinhos de cada BC e acha rádio
# novo. false = coleta SÓ a lista do código e os [rede] seeds.
descoberta              = true
# Só publica equipamento com TAG no nome (CA-1035, ERM-22, ERB-02, RED065,
# PCP-002...). IP sem nome, rádio de terceiro e rádio de teste ficam fora
# do /metrics e do Grafana — inclusive como vizinho de outro rádio. O que
# for descartado aparece no log e em rajant_bc_sem_tag, nunca some calado.
filtrar_tag             = true
# O formato da tag (expressão regular, sem diferenciar maiúsculas).
padrao_tag              = ^[A-Z]{2,5}[- ]?\\d{2,}
# Coleta mais frequente dos equipamentos móveis ([relatorio] padrao_movel).
# 0 = mesmo intervalo para todos.
intervalo_moveis_segundos = 20
# Janela para cálculo de disponibilidade (segundos) — BCE usa 7 dias
janela_disponibilidade_s = 86400

[servidor]
porta_metrics           = 8000

[log]
arquivo                 = rajant_monitor.log
nivel                   = INFO
"""

def carregar_config():
    cfg = configparser.ConfigParser()
    if not Path(CONFIG_FILE).exists():
        Path(CONFIG_FILE).write_text(DEFAULT_CONFIG, encoding="utf-8")
    cfg.read(CONFIG_FILE, encoding="utf-8")
    return cfg

# ══════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════
def setup_logging(arquivo, nivel):
    fmt  = "%(asctime)s [%(levelname)s] %(message)s"
    lvl  = getattr(logging, nivel.upper(), logging.INFO)
    # Sob o supervisor (--servico) não há quem leia o console: tudo vai
    # para o arquivo, e a saída padrão fica só para o que escapar do log.
    hdls = [] if os.environ.get("RAJANT_SERVICO") else [logging.StreamHandler(sys.stdout)]
    if arquivo:
        # Rotativo: rodando o ano todo em segundo plano, um FileHandler
        # simples enchia o disco sem ninguém ver. 10 MB × 5 arquivos.
        from logging.handlers import RotatingFileHandler
        try: hdls.append(RotatingFileHandler(arquivo, maxBytes=10 * 1024 * 1024,
                                             backupCount=5, encoding="utf-8"))
        except Exception as e:
            print(f"AVISO: log em arquivo indisponivel ({arquivo}): {e}")
    if not hdls:
        hdls = [logging.StreamHandler(sys.stdout)]
    logging.basicConfig(level=lvl, format=fmt, datefmt="%Y-%m-%d %H:%M:%S",
                        handlers=hdls, force=True)
    return logging.getLogger("rajant")

log = logging.getLogger("rajant")

# ══════════════════════════════════════════════════════════════
# LABELS
# ══════════════════════════════════════════════════════════════
LBC   = ["bc", "ip"]
LRAD  = ["bc", "ip", "radio", "canal", "freq"]
LPEER = ["bc", "ip", "radio", "peer"]
LETH  = ["bc", "ip", "porta"]
LAP   = ["bc", "ip", "radio", "essid", "freq"]
LSEED = ["seed"]

# ══════════════════════════════════════════════════════════════
# MÉTRICAS
# ══════════════════════════════════════════════════════════════
# Descoberta
# Nomes da época do cache, mantidos para não quebrar painel e histórico:
# hoje são os IPs monitorados (lista do código + descobertos) e quantos
# deles responderam no último ciclo.
m_cache_total  = Gauge("rajant_cache_total",  "IPs monitorados", [])
# Equipamentos que responderam mas foram descartados por nao terem
# prefixo de frota. Existe para o descarte ser AUDITAVEL: sem isto,
# um erro no filtro sumiria com equipamento sem deixar rastro.
m_bc_sem_tag   = Gauge("rajant_bc_sem_tag",  "BCs ignorados por nao ter tag")
m_cache_online = Gauge("rajant_cache_online", "IPs respondendo", [])
m_seed_status  = Gauge("rajant_seed_status",  "Seed OK=1",    LSEED)

# Sistema
m_online        = Gauge("rajant_online",             "Online",                         LBC)
m_uptime        = Gauge("rajant_uptime_s",           "Uptime s",                       LBC)
m_temp          = Gauge("rajant_temperatura_c",      "Temp C",                         LBC)
m_temp_classe   = Gauge("rajant_temperatura_classe", "0=fria..4=critico",              LBC)
# ── BATERIA ────────────────────────────────────────────────────
# REMOVIDAS: rajant_voltagem_v / _min_v / _max_v / rajant_bateria_v.
# Não existe bloco `sensors` (nem `voltage`) em NENHUM .proto do bcapi —
# o parser procurava sensors{voltage{name:"input" value{current/min/max}}},
# que é invenção. As quatro séries publicavam 0,000 V desde sempre.
# O que a BC API realmente expõe é State.Battery, e capacidade em % NÃO é
# tensão: as métricas abaixo trocam de grandeza, e o relatório mudou junto.
m_bat_pct       = Gauge("rajant_bateria_pct",        "Carga da bateria % (State.Battery.capacityPercent)", LBC)
m_bat_ma        = Gauge("rajant_bateria_ma",         "Corrente mA (State.Battery.milliamps)",              LBC)
m_bat_charge    = Gauge("rajant_bateria_carregando", "1=carregando (State.Battery.charging)",              LBC)
m_bat_temp      = Gauge("rajant_bateria_temp_c",     "Temp da bateria C (State.Battery.temperatureCelsius)", LBC)
m_bat_desc_min  = Gauge("rajant_bateria_autonomia_min", "Minutos ate descarregar (dischargeTimeMinutes)",  LBC)
m_bat_chg_min   = Gauge("rajant_bateria_recarga_min",   "Minutos ate carregar (chargeTimeMinutes)",        LBC)
m_boot          = Gauge("rajant_boot_counter",       "Reboots",                        LBC)
m_reboot_needed = Gauge("rajant_reboot_needed",      "Reboot necessario",              LBC)
m_memoria       = Gauge("rajant_memoria_livre_kb",   "Mem KB",                         LBC)
# cpuLoad/cpuUsage/cpu não existem em nenhum .proto. Derivada de
# State.System.idle vs .uptime (ver calcular_taxas) — só é publicada a
# partir da 2a coleta, quando há delta. Nunca publica 0 "por falta de dado".
m_cpu           = Gauge("rajant_cpu_load_pct",       "CPU % (derivada de System.idle)", LBC)
m_bridge        = Gauge("rajant_bridge_ativa",       "Bridge",                         LBC)
# Derivada de State.Wired.aptState == APT_STATE_MASTER (aptMaster/isMaster
# não existem no protocolo).
m_apt_master    = Gauge("rajant_apt_master",         "APT Master (Wired.aptState)",    LBC)
# REMOVIDA: rajant_session_state — sessionState/connectionState não existem
# em nenhum .proto. O único estado de sessão do protocolo é State.AdminSession
# (sessões administrativas conectadas AO BreadCrumb), que não é o que a série
# media. "Sessão OK" do ponto de vista do coletor já é rajant_online.

# Disponibilidade — metodologia BCE (janela deslizante)
m_avail_1h      = Gauge("rajant_disponibilidade_1h_pct",  "Disp % 1h",                LBC)
m_avail_24h     = Gauge("rajant_disponibilidade_24h_pct", "Disp % 24h",               LBC)
m_avail_7d      = Gauge("rajant_disponibilidade_7d_pct",  "Disp % 7d",                LBC)
m_falhas        = Gauge("rajant_falhas_consecutivas",     "Falhas consecutivas",       LBC)
m_ultima_coleta = Gauge("rajant_ultima_coleta_ts",        "Timestamp ultima coleta",   LBC)

# Rádio
m_r_canal       = Gauge("rajant_radio_canal",        "Canal",               LRAD)
m_r_ruido       = Gauge("rajant_radio_ruido_dbm",    "Noise dBm",           LRAD)
m_r_ruido_cls   = Gauge("rajant_radio_ruido_classe", "0=poor 1=fair 2=good",LRAD)
m_r_txpower     = Gauge("rajant_radio_txpower_dbm",  "TX pwr dBm",          LRAD)
# REMOVIDAS: rajant_radio_radar_detec / _radar_pulsos / _phy_erros.
# radarDetections, pulseEvents e rxPhyErrors não existem em State.Wireless
# (nem em nenhuma outra mensagem). A BC API não expõe DFS nem erros de PHY.
m_r_rx_bytes    = Gauge("rajant_radio_rx_bytes",     "RX bytes",            LRAD)
m_r_tx_bytes    = Gauge("rajant_radio_tx_bytes",     "TX bytes",            LRAD)
m_r_rx_pkts     = Gauge("rajant_radio_rx_pacotes",   "RX pkts",             LRAD)
m_r_tx_pkts     = Gauge("rajant_radio_tx_pacotes",   "TX pkts",             LRAD)
m_r_rx_mbps     = Gauge("rajant_radio_rx_mbps",      "RX Mbps",             LRAD)
m_r_tx_mbps     = Gauge("rajant_radio_tx_mbps",      "TX Mbps",             LRAD)
m_r_rx_pps      = Gauge("rajant_radio_rx_pps",       "RX pkt/s",            LRAD)
m_r_tx_pps      = Gauge("rajant_radio_tx_pps",       "TX pkt/s",            LRAD)
m_r_ch_active   = Gauge("rajant_radio_ch_active_ms", "Canal ativo ms",      LRAD)
m_r_ch_busy     = Gauge("rajant_radio_ch_busy_ms",   "Canal busy ms",       LRAD)
m_r_ch_rx       = Gauge("rajant_radio_ch_rx_ms",     "Canal RX ms",         LRAD)
m_r_ch_tx       = Gauge("rajant_radio_ch_tx_ms",     "Canal TX ms",         LRAD)
m_r_busy        = Gauge("rajant_radio_busy_pct",     "Busy %",              LRAD)
m_r_rx_pct      = Gauge("rajant_radio_rx_pct",       "RX %",                LRAD)
m_r_tx_pct      = Gauge("rajant_radio_tx_pct",       "TX %",                LRAD)
m_r_idle        = Gauge("rajant_radio_idle_pct",     "Idle %",              LRAD)
# busy - rx - tx: antena ocupada por transmissor que nao e nosso.
m_r_interf      = Gauge("rajant_radio_interf_pct",
                        "Interferencia % (busy - rx - tx)",                 LRAD)
m_r_peers_ativos= Gauge("rajant_radio_peers_ativos", "Peers ativos",        LRAD)
m_r_peers_total = Gauge("rajant_radio_peers_total",  "Peers total",         LRAD)
m_r_clients     = Gauge("rajant_radio_clientes",     "Clientes WiFi",       LRAD)
# Good peers — BCE: peers com SNR > 30dB
m_r_good_peers  = Gauge("rajant_radio_good_peers",   "Peers SNR>30dB",      LRAD)

# Peers
m_p_snr         = Gauge("rajant_peer_snr_db",       "SNR dB",              LPEER)
m_p_sinal       = Gauge("rajant_peer_sinal_dbm",    "Signal dBm",          LPEER)
m_p_rssi        = Gauge("rajant_peer_rssi",         "RSSI",                LPEER)
m_p_taxa        = Gauge("rajant_peer_taxa_mbps",    "Rate Mbps",           LPEER)
m_p_custo       = Gauge("rajant_peer_custo",        "Cost InstaMesh",      LPEER)
# REMOVIDA: rajant_peer_txpower_dbm — State.Peer não tem txpower. A potência
# de transmissão é do RÁDIO LOCAL (State.Wireless.txpower, já publicada em
# rajant_radio_txpower_dbm); a do outro lado do enlace não trafega no protocolo.
m_p_ativo       = Gauge("rajant_peer_ativo",        "Peer ativo",          LPEER)
m_first_hop     = Gauge("rajant_first_hop_cost",    "First hop cost",      LBC + ["radio"])

# Info do BC — labels descritivos p/ classificação (ERB/ERM/etc) e relatórios
m_bc_info       = Gauge("rajant_bc_info",           "Info do BC (valor sempre 1)",
                        LBC + ["modelo", "firmware", "grupos"])

# ──────────────────────────────────────────────────────────────
# DEDUPLICAÇÃO DE NÓS FÍSICOS
# Cada rádio/interface de um BreadCrumb tem IPv4 próprio, e a
# descoberta caminha pelos peers por IP — então um único BC físico
# com 3 rádios aparecia como 3 "BCs", inflando a contagem.
# rajant_bc_primario=1 marca UM IP por nó físico (o menor IP do
# mesmo nome de BC); os demais recebem 0. Relatórios contam só os
# primários, sem perder as métricas por interface.
# ──────────────────────────────────────────────────────────────
# GPS (formato NMEA convertido para graus decimais)
m_gps_lat       = Gauge("rajant_gps_lat",           "Latitude (graus decimais)",  LBC)
m_gps_lon       = Gauge("rajant_gps_lon",           "Longitude (graus decimais)", LBC)
m_gps_fix       = Gauge("rajant_gps_fix",           "1=posicao valida, 0=sem fix", LBC)
# gpsSpeedKph vem pronto em km/h no Gps.proto — o parser antigo lia um campo
# `speed` inexistente e ainda multiplicava por 1,852 (nós→km/h).
m_gps_vel       = Gauge("rajant_gps_vel_kmh",       "Velocidade km/h (gpsVel.gpsSpeedKph)", LBC)
# `course`/`heading`/`track` não existem; o campo real é gpsTrackDegreesTrue.
# O rumo já era calculado no parser mas NÃO tinha métrica — nunca foi publicado.
m_gps_rumo      = Gauge("rajant_gps_rumo_graus",    "Rumo verdadeiro (gpsTrackDegreesTrue)", LBC)
m_gps_sats      = Gauge("rajant_gps_satelites",     "Satelites a vista (gpsSatsInView)",  LBC)
m_gps_alt       = Gauge("rajant_gps_altitude_m",    "Altitude m (gpsPos.gpsAlt)",         LBC)
m_gps_hdop      = Gauge("rajant_gps_precisao_h",    "Precisao horizontal (gpsPrecisionH)",LBC)
# Indicador de fix do NMEA GGA, em gpsPos.gpsQuality: 0 invalido, 1 GPS,
# 2 DGPS, 4/5 RTK. Estava no proto e era descartado. Separa "sem GPS" de
# "GPS degradado", que no mapa e a diferenca entre ponto ausente e ponto
# no lugar errado.
m_gps_qual      = Gauge("rajant_gps_qualidade",     "Indicador de fix (gpsPos.gpsQuality)",LBC)

m_bc_primario   = Gauge("rajant_bc_primario",       "1=IP primario do no fisico, 0=interface secundaria", LBC)
m_bc_ifaces     = Gauge("rajant_bc_interfaces",     "Qtd de IPs (interfaces) do mesmo no fisico", LBC)
m_nos_fisicos   = Gauge("rajant_nos_fisicos_total", "Total de BreadCrumbs fisicos distintos")

# Ping RTT (ICMP)
m_ping_rtt      = Gauge("rajant_ping_rtt_ms",       "Ping RTT ms",         LBC)
m_ping_ok       = Gauge("rajant_ping_ok",            "Ping OK (1=sim)",     LBC)

# Estabilidade de link — mudanças de peer_ativo nas últimas coletas
m_link_changes  = Gauge("rajant_link_changes",       "Mudancas de link (instabilidade)", LBC)

# Score de qualidade da malha por BC (0-100)
m_mesh_score    = Gauge("rajant_mesh_score",         "Score qualidade mesh 0-100",        LBC)

# InstaMesh
m_im_tx         = Gauge("rajant_im_pkt_tx",         "IM TX",               LBC)
m_im_rx         = Gauge("rajant_im_pkt_rx",         "IM RX",               LBC)
m_im_drop       = Gauge("rajant_im_pkt_drop",       "IM drop",             LBC)
m_im_floods     = Gauge("rajant_im_floods_drop",    "Floods drop",         LBC)
m_im_arp        = Gauge("rajant_im_arp_total",      "ARP total",           LBC)
m_im_disc_src   = Gauge("rajant_im_disc_sourced",   "Discoveries sourced", LBC)
m_im_disc_pass  = Gauge("rajant_im_disc_passed",    "Discoveries passed",  LBC)
# REMOVIDAS: rajant_im_unicast_drop, rajant_im_overflows, rajant_im_undeliv_rx,
# rajant_im_undeliv_tx. unicastDropped, overflows, undeliverablesReceived e
# undeliverableTransmitFailures não constam de State.InstaMesh — a mensagem
# tem exatamente 19 campos e nenhum deles é esses.
# SUBSTITUTAS (existem e estavam sem uso):
m_im_src_floods = Gauge("rajant_im_source_floods_drop", "Floods originados descartados (sourceFloodsDropped)", LBC)
m_im_multicast  = Gauge("rajant_im_pkt_multicast",  "Pacotes multicast (packetsMulticast)", LBC)
m_im_tx_ps      = Gauge("rajant_im_tx_pps",         "IM TX pkt/s",         LBC)
m_im_rx_ps      = Gauge("rajant_im_rx_pps",         "IM RX pkt/s",         LBC)
m_im_drop_ps    = Gauge("rajant_im_drop_pps",       "IM drop pkt/s",       LBC)
m_im_perda      = Gauge("rajant_im_perda_pct",      "Perda %",             LBC)
m_im_floods_ps  = Gauge("rajant_im_floods_pps",     "Floods/s",            LBC)
m_im_arp_ps     = Gauge("rajant_im_arp_pps",        "ARP/s",               LBC)

# Ethernet
m_e_link        = Gauge("rajant_eth_link",          "Link",                LETH)
m_e_apt_state   = Gauge("rajant_eth_apt_state",     "APT state",           LETH)
m_e_rx_bytes    = Gauge("rajant_eth_rx_bytes",      "ETH RX bytes",        LETH)
m_e_tx_bytes    = Gauge("rajant_eth_tx_bytes",      "ETH TX bytes",        LETH)
m_e_rx_pkts     = Gauge("rajant_eth_rx_pacotes",    "ETH RX pkts",         LETH)
m_e_tx_pkts     = Gauge("rajant_eth_tx_pacotes",    "ETH TX pkts",         LETH)
m_e_rx_mbps     = Gauge("rajant_eth_rx_mbps",       "ETH RX Mbps",         LETH)
m_e_tx_mbps     = Gauge("rajant_eth_tx_mbps",       "ETH TX Mbps",         LETH)
m_e_peers       = Gauge("rajant_eth_peers",        "Peers na porta (APT)", LETH)
# REMOVIDAS: rajant_eth_rx_erros, _tx_erros, _rx_crc, _rx_drop, _tx_drop,
# _mudancas. State.Wired carrega stats do tipo CommStats, que tem SÓ quatro
# contadores: rxBytes, rxPackets, txBytes, txPackets. Não há erro, CRC,
# descarte nem contagem de mudanças de link em lugar algum do protocolo.
# Publicá-las como 0 dizia "porta perfeita" quando o dado nunca foi medido.

m_ap_clients    = Gauge("rajant_ap_clientes",       "Clientes por SSID (blocos AP.client)", LAP)

# ── ALERTAS ────────────────────────────────────────────────────
# AlertSystem.bestRadioRate existe em Common.proto e não era aproveitado.
m_best_rate     = Gauge("rajant_best_radio_rate",   "Melhor taxa de radio (AlertSystem.bestRadioRate)", LBC)
m_alertas       = Gauge("rajant_alertas_ativos",    "Qtd de alertas ativos (AlertSystem.alerts)",       LBC)

# ══════════════════════════════════════════════════════════════
# DISPONIBILIDADE — JANELA DESLIZANTE (metodologia BCE)
# BCE define: % do tempo que o BC esteve online na janela
# Usamos deque de timestamps de eventos (1=online, 0=offline)
# ══════════════════════════════════════════════════════════════
class JanelaDisponibilidade:
    """
    Mantém histórico de eventos (timestamp, online) em janelas de tempo.
    Calcula disponibilidade % em janelas de 1h, 24h e 7d.
    """
    JANELAS = {
        "1h":  3600,
        "24h": 86400,
        "7d":  604800,
    }

    def __init__(self):
        # deque de (timestamp, status) onde status = 1 ou 0
        self._eventos = deque()
        self._lock    = threading.Lock()

    def registrar(self, online: bool):
        ts = time.time()
        with self._lock:
            self._eventos.append((ts, 1 if online else 0))
            # Remove eventos mais antigos que 7d
            cutoff = ts - self.JANELAS["7d"]
            while self._eventos and self._eventos[0][0] < cutoff:
                self._eventos.popleft()

    def disponibilidade(self, janela_s: int) -> float:
        """
        Disponibilidade % ponderada por TEMPO (não por contagem de eventos).

        Princípio: disponibilidade = tempo online / tempo total da janela.
        O estado entre duas coletas é o da coleta anterior (função degrau).
        Contar eventos distorce o resultado quando o intervalo de coleta
        varia (retries, ciclos lentos): 10 amostras online em 10min e
        1 amostra offline de 50min dariam 91% por contagem, quando o
        valor real é ~17%.
        """
        ts_agora  = time.time()
        ts_inicio = ts_agora - janela_s
        with self._lock:
            eventos = list(self._eventos)
        if not eventos:
            return 0.0

        # Estado vigente no início da janela = último evento anterior a ela
        estado_inicial = None
        dentro = []
        for t, s in eventos:
            if t < ts_inicio:
                estado_inicial = s
            else:
                dentro.append((t, s))

        tempo_online = 0.0
        cursor, estado_atual = ts_inicio, estado_inicial
        if estado_atual is None:
            # Sem histórico antes da janela: só medimos a partir do 1º evento
            if not dentro:
                return 0.0
            cursor, estado_atual = dentro[0]
            dentro = dentro[1:]
            janela_efetiva = ts_agora - cursor
        else:
            janela_efetiva = janela_s

        for t, s in dentro:
            if estado_atual == 1:
                tempo_online += t - cursor
            cursor, estado_atual = t, s
        if estado_atual == 1:
            tempo_online += ts_agora - cursor

        if janela_efetiva <= 0:
            return 0.0
        return round(min(100.0, tempo_online / janela_efetiva * 100), 2)

_janelas: dict[str, JanelaDisponibilidade] = {}
_janelas_lock = threading.Lock()

def janela_bc(ip: str) -> JanelaDisponibilidade:
    with _janelas_lock:
        if ip not in _janelas:
            _janelas[ip] = JanelaDisponibilidade()
        return _janelas[ip]

# ══════════════════════════════════════════════════════════════
# VALIDAÇÃO E CLASSIFICAÇÕES (BCE pg.90)
# ══════════════════════════════════════════════════════════════
RANGES = {
    "temperatura_c": (-40.0, 120.0),
    "bateria_pct":   (0.0,  100.0),
    "ruido_dbm":     (-120.0, -10.0),
    "sinal_dbm":     (-120.0,  0.0),
    "snr_db":        (-10.0,  100.0),
    "taxa_mbps":     (0.0,   1000.0),
    "custo":         (0.0,  999999.0),
    "cpu_pct":       (0.0,   100.0),
    "busy_pct":      (0.0,   100.0),
}

def validar(v, campo, bc=""):
    if v is None: return None
    lo, hi = RANGES.get(campo, (None, None))
    if lo is not None and not (lo <= v <= hi):
        log.debug(f"[val] {bc} {campo}={v} fora [{lo},{hi}]")
        return None
    return v

def class_temp(c):
    if c is None: return None
    if c < 0:  return 0
    if c < 50: return 1
    if c < 65: return 2
    if c < 75: return 3
    return 4

def class_ruido(dbm):
    """BCE pg.90: Good<=-80 | Fair<=-40 | Poor>-40 dBm"""
    if dbm is None: return None
    if dbm <= -80: return 2
    if dbm <= -40: return 1
    return 0

# State.Wired.AptState — valores EXATOS do enum em State.proto:
#     APT_STATE_MASTER = 0, APT_STATE_SLAVE = 1,
#     APT_STATE_NONE   = 2, APT_STATE_LINK  = 3
# ATENÇÃO (mudança de semântica): o mapa antigo usava link=2 e none=3, ou
# seja, os dois estavam TROCADOS em relação ao protocolo. rajant_eth_apt_state
# passa a publicar o valor do enum oficial — dashboards que traduziam 2/3
# precisam ser reajustados.
# O state em texto pode trazer o nome completo do enum ou só o sufixo.
_APT_MAP = {
    "master": 0, "apt_state_master": 0,
    "slave":  1, "apt_state_slave":  1,
    "none":   2, "apt_state_none":   2, "": 2,
    "link":   3, "apt_state_link":   3,
}
_APT_TXT = {0: "MASTER", 1: "SLAVE", 2: "NONE", 3: "LINK"}

# ══════════════════════════════════════════════════════════════
# ESTADO DE CONFIABILIDADE
# ══════════════════════════════════════════════════════════════
class EstadoBC:
    def __init__(self, falhas_limite, manter_s):
        self.falhas_limite = falhas_limite
        self.manter_s      = manter_s
        self.falhas_consec = 0
        self.ts_ultimo_ok  = 0.0
        # Hora REAL da última coleta boa, para o /metrics: `ts_ultimo_ok` é
        # o relógio monotônico (bom para medir intervalo, sem sentido fora
        # do processo). O painel faz time() - rajant_ultima_coleta_ts, e
        # com o monotônico o resultado era um número sem significado.
        self.epoch_ultimo_ok = 0.0
        self.ultima_ts     = 0.0
        self.ultima_dados  = None

    def ok(self, dados, ts):
        self.falhas_consec = 0
        self.ts_ultimo_ok  = ts
        self.epoch_ultimo_ok = time.time()
        self.ultima_ts     = ts
        self.ultima_dados  = dados

    def falha(self): self.falhas_consec += 1

    @property
    def offline(self): return self.falhas_consec >= self.falhas_limite

    @property
    def respondendo(self):
        """A última tentativa deu certo. É o que rajant_online diz; `offline`
        espera `falhas_limite` falhas, e um IP da lista que nunca respondeu
        contaria como no ar até lá."""
        return self.falhas_consec == 0 and self.ultima_dados is not None

    def expirou(self, agora):
        if self.manter_s <= 0: return True
        return (agora - self.ultima_ts) > self.manter_s

_estados    = {}
_estados_lk = threading.Lock()

def estado(ip, fl, ms):
    with _estados_lk:
        if ip not in _estados:
            _estados[ip] = EstadoBC(fl, ms)
        return _estados[ip]

# ══════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════
def _s(t, c):
    m = re.search(rf'{c}:\s*"([^"]*)"', t)
    if m: return m.group(1).strip()
    m = re.search(rf'(?<!\w){c}:\s*([^\n\{{,}}]+)', t)
    return m.group(1).strip() if m else ""

def _i(t, c):
    try:    return int(float(_s(t, c)))
    except: return 0

def _f(t, c):
    try:    return float(_s(t, c))
    except: return 0.0

def _b(t, c): return _s(t, c).lower() == 'true'
def _pct(v, t): return round(v/t*100, 2) if t > 0 else 0.0

def _s2(t, *campos):
    """Tenta múltiplos nomes de campo (camelCase / snake_case)."""
    for c in campos:
        v = _s(t, c)
        if v: return v
    return ""

def _i2(t, *campos):
    for c in campos:
        v = _s(t, c)
        if v:
            try: return int(float(v))
            except: pass
    return 0


def extrair_blocos(txt, nome_bloco):
    """Retorna lista com o conteúdo interno de cada bloco `nome_bloco { ... }`."""
    blocos = []
    for m in re.finditer(rf'(?m)^[ \t]*{nome_bloco}\s*\{{', txt):
        i, depth = m.end(), 1
        while i < len(txt) and depth > 0:
            ch = txt[i]
            if ch == '{':   depth += 1
            elif ch == '}': depth -= 1
            i += 1
        blocos.append(txt[m.end():i-1])
    return blocos

def extrair_bloco(txt, nome_bloco):
    b = extrair_blocos(txt, nome_bloco)
    return b[0] if b else ""

def remover_bloco(txt, nome_bloco):
    """Devolve o texto sem as subárvores `nome_bloco { ... }`.

    Necessário porque State e Config têm blocos com o MESMO nome. O
    `configuration` traz `wired { name: "eth1" requestFallback: false }`
    — a configuração da porta, sem contadores. Extrair sem remover isso
    faz o parser achar a porta certa e ler zero em tudo.
    """
    saida, pos = [], 0
    for m in re.finditer(rf'(?m)^[ \t]*{nome_bloco}\s*\{{', txt):
        if m.start() < pos: continue
        i, depth = m.end(), 1
        while i < len(txt) and depth > 0:
            ch = txt[i]
            if ch == '{':   depth += 1
            elif ch == '}': depth -= 1
            i += 1
        saida.append(txt[pos:m.start()]); pos = i
    saida.append(txt[pos:])
    return "".join(saida)

def nmea_para_graus(coord):
    """'2743.8950S' -> -27.7316 | '05004.1429W' -> -50.0690
    Formato NMEA: DDMM.mmmm (os minutos SEMPRE têm 2 dígitos inteiros);
    hemisfério S/W é negativo. Tratar como decimal joga o ponto longe."""
    if not coord: return None
    coord = str(coord).strip()
    hemi = coord[-1].upper() if coord and coord[-1].isalpha() else ""
    num  = coord[:-1] if hemi else coord
    if "." not in num: return None
    try:
        corte   = num.index(".") - 2
        graus   = float(num[:corte])
        minutos = float(num[corte:])
        valor   = graus + minutos / 60.0
        return round(-valor if hemi in ("S", "W") else valor, 7)
    except (ValueError, IndexError):
        return None

def gps_time_para_segundos(v):
    """`GPS.GPSPositionReport.gpsTime` -> segundos desde a meia-noite UTC.

    O `Gps.proto` declara `optional float gpsTime = 1;` e **não diz a
    unidade**. Por isso aqui se DISCRIMINA em vez de supor:

      • 0 a 240000  -> hora NMEA `hhmmss.ss` (o resto do bloco é NMEA:
                       gpsLat e gpsLong vêm em DDMM.mmmm). 174551.25 são
                       17h45m51,25s.
      • > 1e9       -> época Unix. Improvável num float de 32 bits — não
                       sobra precisão para segundos — mas se um firmware
                       publicar assim, é reconhecido em vez de virar hora
                       absurda.

    Devolve None quando não encaixa em nenhum dos dois: número sem
    unidade conhecida não vira medida de tempo por conveniência.

    ATENÇÃO — o uso principal de `gpsTime` NÃO depende desta conversão.
    Para saber se a posição é nova basta comparar o valor bruto com o da
    leitura anterior, e isso funciona em qualquer formato. Esta função
    serve para exibir e para medir o intervalo de atualização.
    """
    if v is None: return None
    try: f = float(v)
    except (TypeError, ValueError): return None
    if f <= 0: return None
    if f >= 1e9:                       # época Unix
        return f % 86400.0
    # UMA validação, aqui: hh/mm/ss fora de faixa cobre tudo que não é
    # hora NMEA. Havia antes um `if f >= 240000: return None` por cima
    # disto — teste de mutação mostrou que removê-lo não quebrava nada,
    # porque 240000 já sai por `hh > 23`. Guarda que não guarda nada só
    # dá a impressão de que a faixa é checada em dois lugares.
    hh = int(f // 10000)
    mm = int((f // 100) % 100)
    ss = f - hh * 10000 - mm * 100
    if hh > 23 or mm > 59 or ss >= 60.0:
        return None
    return round(hh * 3600 + mm * 60 + ss, 3)


def _mbps(delta, dt):
    if dt <= 0 or delta <= 0: return 0.0
    return round((delta * 8)/1_000_000/dt, 4)

# ══════════════════════════════════════════════════════════════
# PARSER
# ══════════════════════════════════════════════════════════════
def _varredura_ethernet(txt):
    """Último recurso: procura no state inteiro qualquer trecho que
    mencione uma interface ethernet (eth0/eth1/en0/lan...) e coleta os
    contadores numéricos ao redor. Serve para firmwares cujo layout de
    blocos não corresponde a nenhum dos formatos conhecidos."""
    achados = []
    for m in re.finditer(r'"((?:eth|en|lan|ge|fe)\d*[a-z0-9_-]*)"', txt, re.IGNORECASE):
        nome = m.group(1)
        # janela de contexto ao redor da menção
        ini = max(0, m.start() - 400)
        ctx = txt[ini:m.end() + 1500]
        rx = _i2(ctx,'rxBytes','rx_bytes','rxOctets','rx_octets','ifInOctets')
        tx = _i2(ctx,'txBytes','tx_bytes','txOctets','tx_octets','ifOutOctets')
        if rx or tx:
            _apt = _s2(ctx,'aptState','apt_state').lower()
            achados.append({
                "nome": nome,
                # `linkup`/`linkUp`/`link` não existem no protocolo; o sinal
                # de porta viva é aptState != NONE ou haver tráfego.
                "link": (_apt not in ('', 'apt_state_none', 'none')) or bool(rx or tx),
                "apt_int": _APT_MAP.get(_apt, 2),   # 2 = APT_STATE_NONE
                "rx_bytes": rx, "tx_bytes": tx,
                "rx_pkts": _i2(ctx,'rxPackets','rx_packets'),
                "tx_pkts": _i2(ctx,'txPackets','tx_packets'),
                "peers_eth": 0,
            })
    # dedupe por nome, mantendo o de maiores contadores
    porta = {}
    for a in achados:
        ant = porta.get(a["nome"])
        if not ant or (a["rx_bytes"]+a["tx_bytes"]) > (ant["rx_bytes"]+ant["tx_bytes"]):
            porta[a["nome"]] = a
    return list(porta.values())

def diagnostico_ethernet(raw):
    """Relatório legível de por que a ethernet pode estar vazia."""
    txt = json.dumps(raw) if isinstance(raw, dict) else str(raw)
    txt = txt.replace('\\n','\n').replace('\\"','"').replace('\\\\','\\')
    linhas = []
    linhas.append(f"Tamanho do state: {len(txt)} caracteres")
    rotulos = {}
    for r in ("wired","ethernet","eth","port","interface","wireless","stats",
              "statistics","counters"):
        n = len(extrair_blocos(txt, r))
        if n: rotulos[r] = n
    linhas.append(f"Blocos encontrados: {rotulos or 'NENHUM'}")
    nomes = sorted(set(re.findall(r'"((?:eth|en|lan|ge|fe)\d*[a-z0-9_-]*)"',
                                  txt, re.IGNORECASE)))
    linhas.append(f"Nomes de interface citados no state: {nomes or 'NENHUM'}")
    campos = sorted(set(re.findall(r'\b(rx[A-Za-z_]*|tx[A-Za-z_]*)\s*:', txt)))
    linhas.append(f"Campos de contador presentes: {campos[:25] or 'NENHUM'}")
    for rot in ("wired","ethernet","interface","port"):
        bs = extrair_blocos(txt, rot)
        if bs:
            linhas.append(f"\n--- Primeiro bloco '{rot}' (bruto, 800 chars) ---")
            linhas.append(bs[0][:800])
            break
    d = parse_state(raw)
    linhas.append(f"\nResultado do parser: {len(d['ethernet'])} porta(s)")
    for e in d["ethernet"]:
        linhas.append(f"   {e['nome']}: link={e['link']} apt={e['apt_int']} "
                      f"rx={e['rx_bytes']} tx={e['tx_bytes']} "
                      f"rx_pkts={e['rx_pkts']} peers={e.get('peers_eth',0)}")
    v = _varredura_ethernet(txt)
    linhas.append(f"Varredura profunda: {[(a['nome'], a['rx_bytes'], a['tx_bytes']) for a in v] or 'nada'}")
    return "\n".join(linhas)

def parse_state(raw):
    txt = json.dumps(raw) if isinstance(raw, dict) else str(raw)
    txt = txt.replace('\\n','\n').replace('\\"','"').replace('\\\\','\\')

    # A subárvore `configuration` (State.Configuration -> Config) repete
    # NOMES de mensagens do estado: wired, wireless, battery, general...
    # Config.Battery, por exemplo, só tem os limiares de alarme
    # (warningThresholdMinutes/errorThresholdMinutes) — nenhum dado medido.
    # Extrair do texto inteiro pegava o bloco de configuração e lia zero.
    # Tudo que é ESTADO sai de txt_estado; só nome/grupos vêm de `cb`.
    txt_estado = remover_bloco(txt, 'configuration')

    sb = extrair_bloco(txt_estado, 'system') or txt_estado

    uptime_s = _f(sb, 'uptime') / 1000.0

    # State.System.temperature é int32 e o .proto não documenta a unidade.
    # Firmwares publicam centésimos de grau (4500 = 45,00 °C) ou o grau
    # inteiro (45). Fixar um divisor quebra metade da frota, então decidimos
    # pela magnitude: nenhum BreadCrumb opera a 200 °C, logo |v| > 200 só
    # pode ser centi-grau. (Confirmar com --dump-state num BC da frota.)
    _temp_raw = _i(sb, 'temperature')
    temp_c    = round(_temp_raw / 100.0, 1) if abs(_temp_raw) > 200 else float(_temp_raw)

    # ── Bateria (State.Battery) ────────────────────────────────
    # Substitui o bloco `sensors`/`voltage`, que NÃO existe no protocolo.
    # Atenção: capacityPercent é carga em %, não tensão — quem lê o
    # relatório precisa saber que a grandeza mudou.
    bat_b = extrair_bloco(txt_estado, 'battery')
    bateria = {
        "pct":      _i(bat_b, 'capacityPercent')      if bat_b else None,
        "ma":       _i(bat_b, 'milliamps')            if bat_b else None,
        "carregando": _b(bat_b, 'charging')           if bat_b else None,
        "temp_c":   _i(bat_b, 'temperatureCelsius')   if bat_b else None,
        "desc_min": _i(bat_b, 'dischargeTimeMinutes') if bat_b else None,
        "chg_min":  _i(bat_b, 'chargeTimeMinutes')    if bat_b else None,
    }
    # Sem bloco battery (modelo sem bateria) → None em tudo, nada publicado.
    if not bat_b:
        bateria = {k: None for k in bateria}

    # CPU: cpuLoad/cpuUsage/cpu não existem em .proto nenhum. O par que
    # State.System oferece é `idle` + `uptime` (ambos float), exatamente o
    # par do /proc/uptime do Linux: contadores acumulados desde o boot.
    # A carga sai de 1 - Δidle/Δuptime — razão adimensional, então vale
    # tanto para segundos quanto para milissegundos. Precisa de duas
    # coletas, por isso o valor é calculado em calcular_taxas() e fica
    # None aqui (None = não publica; 0 seria "CPU ociosa", que é mentira).
    idle_raw   = _f(sb, 'idle')
    mem_kb     = round(_i(sb,'freeMemory') / 1024.0, 1)
    reboot     = 1 if (_b(sb,'reboot') or _b(sb,'rebootRequired')) else 0
    ip_m       = re.search(r'ipv4 \{[^}]*address:\s*"([\d.]+)"', sb)

    bb     = extrair_bloco(txt_estado, 'build')
    fw     = _s(bb, 'version') if bb else ""
    cb     = extrair_bloco(txt, 'configuration')
    # O nome do BC é Config.General.name — caminho
    # configuration.saved.general.name (é assim que o bc_livestats.py oficial
    # o lê). Buscar `name:` na subárvore `configuration` inteira pegava o
    # primeiro que aparecesse: nome de grupo, de VLAN ou de porta wired.
    _cfg_saved = extrair_bloco(cb, 'saved') or extrair_bloco(cb, 'active') or cb
    _geral     = extrair_bloco(_cfg_saved, 'general')
    nome       = _s(_geral, 'name') if _geral else ""
    grupos = "|".join(re.findall(r'groups \{[^}]*name:\s*"([^"]+)"', cb)) if cb else ""
    modelo = _s(sb, 'platform')

    # ── GPS do BreadCrumb ──────────────────────────────────────
    # O Rajant publica a posição em NMEA dentro de gps.gpsPos:
    #   gpsLat: "2743.8950S"   gpsLong: "05004.1234W"
    # Formato DDMM.mmmm + hemisfério — NÃO são graus decimais. Converter
    # errado joga o ponto a centenas de km. Mantemos um fallback para
    # firmwares que publiquem graus decimais direto.
    gps_b = ""
    for nome_b in ('gps', 'location', 'position', 'geo'):
        gps_b = extrair_bloco(sb, nome_b) or extrair_bloco(txt_estado, nome_b)
        if gps_b: break
    pos_b = extrair_bloco(gps_b, 'gpsPos') or extrair_bloco(gps_b, 'gps_pos') or gps_b

    def _nmea_str(bloco, *campos):
        for c in campos:
            m = re.search(rf'\b{c}\s*:\s*"([^"]+)"', bloco)
            if m: return m.group(1)
        return ""

    def _coord_decimal(bloco, *campos):
        for c in campos:
            m = re.search(rf'\b{c}\s*:\s*(-?\d+\.?\d*)', bloco)
            if m:
                try: v = float(m.group(1))
                except ValueError: continue
                if v == 0: continue
                for div in (1.0, 1e6, 1e7):     # graus | escalados
                    if abs(v/div) <= 180.0001: return round(v/div, 7)
        return None

    lat = nmea_para_graus(_nmea_str(pos_b, 'gpsLat', 'gps_lat', 'latitude'))
    lon = nmea_para_graus(_nmea_str(pos_b, 'gpsLong', 'gps_long', 'gpsLon', 'longitude'))
    if lat is None: lat = _coord_decimal(pos_b, 'latitude', 'lat')
    if lon is None: lon = _coord_decimal(pos_b, 'longitude', 'longitude', 'lon', 'lng')
    # gpsSwitch desabilitado = módulo desligado, posição não confiável
    sw = extrair_bloco(gps_b, 'gpsSwitch') or extrair_bloco(gps_b, 'gps_switch')
    if sw and re.search(r'\benabled\s*:\s*false', sw, re.I):
        lat = lon = None
    gps_fix = 1 if (lat is not None and lon is not None) else 0

    # ── Velocidade e rumo (GPS.GPSVelocityReport) ──────────────
    # O parser antigo lia `speed` e `course`, que não existem no Gps.proto,
    # e ainda convertia nós→km/h em cima do zero resultante. Os campos reais
    # são gpsSpeedKph (JÁ em km/h — não converter) e gpsSpeedKnots.
    vel_b   = extrair_bloco(gps_b, 'gpsVel') or extrair_bloco(gps_b, 'gps_vel') or gps_b
    _kph    = _s(vel_b, 'gpsSpeedKph')
    if _kph:
        vel_kmh = round(float(_kph), 1)
    else:
        _kn = _s(vel_b, 'gpsSpeedKnots')
        vel_kmh = round(float(_kn) * 1.852, 1) if _kn else None
    # Rumo verdadeiro; se o firmware só publicar o magnético, usa esse.
    _trk = _s(vel_b, 'gpsTrackDegreesTrue') or _s(vel_b, 'gpsTrackDegreesMag')
    rumo = round(float(_trk), 1) if _trk else None

    # ── Qualidade do fix (GPS.GPSPositionReport) ───────────────
    # Campos que existiam e não eram aproveitados.
    _sats = _s(pos_b, 'gpsSatsInView')
    _alt  = _s(pos_b, 'gpsAlt')
    _hdop = _s(pos_b, 'gpsPrecisionH')
    gps_sats = int(float(_sats)) if _sats else None
    gps_alt  = round(float(_alt), 1)  if _alt  else None
    gps_hdop = round(float(_hdop), 2) if _hdop else None

    # ── Hora do fix (GPS.GPSPositionReport.gpsTime) ────────────
    # É o campo que separa "medi aqui" de "medi com uma posição velha".
    # O State devolve a posição que o módulo tem NO MOMENTO da consulta;
    # se o GPS atualiza a 1 Hz e perguntamos a 5 Hz, quatro das cinco
    # respostas repetem a mesma posição. Sem `gpsTime` isso é invisível e
    # vira ponto duplicado no mapa; com ele, dá para não gravar amostra
    # cuja posição não mudou — e para medir sozinho de quanto em quanto o
    # módulo atualiza, em vez de supor.
    #
    # `gpsQuality` é o indicador de fix do NMEA GGA (0 = inválido,
    # 1 = GPS, 2 = DGPS, 4/5 = RTK). Também estava sendo descartado.
    _gtime = _s(pos_b, 'gpsTime')
    _qual  = _s(pos_b, 'gpsQuality')
    gps_time = float(_gtime) if _gtime else None
    gps_qual = float(_qual) if _qual else None

    # ── Identidade de fábrica (State.Manufacturer) ─────────────
    # `serial` aqui é uint32 — a parte NUMÉRICA do número de série. É ela
    # que fecha a identificação do vizinho: State.Peer traz `encapId`, e
    # encapId é esse mesmo número (conferido em 40 de 40 vizinhos da
    # captura real: FE1-2255B-107805 <-> encap 107805). Com o serial de
    # cada rádio coletado, o `encap` de qualquer vizinho vira nome.
    #
    # ATENÇÃO para quem for mexer: `manufacturer` é o campo 190 do State,
    # um ramo À PARTE de gps/wireless/system. CAMINHOS_ESTADO não o pede,
    # então numa coleta filtrada ele vem vazio — e é por isso que o certo
    # é ler UMA vez por rádio e guardar: serial e modelo não mudam.
    mf_b = extrair_bloco(txt_estado, 'manufacturer')
    serial_num = (_i(mf_b, 'serial') or None) if mf_b else None
    modelo_fab = (_s(mf_b, 'model') or None) if mf_b else None

    sistema = {
        "ip":         ip_m.group(1) if ip_m else "",
        "nome":       nome or (ip_m.group(1) if ip_m else ""),
        "modelo":     modelo,
        "firmware":   fw,
        "grupos":     grupos,
        "uptime_s":   int(uptime_s),
        "temp_c":     temp_c,
        "temp_cls":   class_temp(temp_c),
        "bateria":    bateria,
        "boot":       _i(sb,'bootCounter'),
        "reboot":     reboot,
        "mem_kb":     mem_kb,
        # None até haver duas coletas (ver calcular_taxas)
        "cpu_pct":    None,
        "cpu_idle":   idle_raw,
        "uptime_raw": _f(sb, 'uptime'),
        "bridge":     _b(sb,'bridgeup'),
        # preenchido após o parsing de `wired` (aptState)
        "apt_master": False,
        "gps_lat":    lat,
        "gps_lon":    lon,
        "gps_fix":    gps_fix,
        "gps_vel":    vel_kmh,
        "gps_rumo":   rumo,
        "gps_sats":   gps_sats,
        "gps_alt":    gps_alt,
        "gps_hdop":   gps_hdop,
        # Bruto, como veio: comparar com a leitura anterior é o que diz se
        # a posição é nova, e isso independe de saber a unidade.
        "gps_time":   gps_time,
        "gps_time_s": gps_time_para_segundos(gps_time),
        "gps_qual":   gps_qual,
        "serial_num": serial_num,
        "modelo_fab": modelo_fab,
    }

    # ── Alertas (Common.proto AlertSystem) ─────────────────────
    al_b = extrair_bloco(txt_estado, 'alertSystem')
    sistema["best_rate"] = _i(al_b, 'bestRadioRate') if al_b else None
    sistema["alertas"]   = len(extrair_blocos(al_b, 'alerts')) if al_b else None

    ib   = extrair_bloco(txt_estado, 'instamesh')
    instamesh = {
        "pkt_tx":    _i2(ib,'packetsSent','packets_sent'),
        "pkt_rx":    _i2(ib,'packetsReceived','packets_received'),
        "pkt_drop":  _i2(ib,'packetsDropped','packets_dropped'),
        "floods":    _i2(ib,'floodsDropped','floods_dropped'),
        "arp":       _i2(ib,'arpTotal','arp_total'),
        "disc_src":  _i2(ib,'discoveriesSourced','discoveries_sourced'),
        "disc_pass": _i2(ib,'discoveriesPassed','discoveries_passed'),
        # Existem em State.InstaMesh e não eram lidos. Substituem
        # unicastDropped / overflows / undeliverables*, que são inventados.
        "src_floods": _i2(ib,'sourceFloodsDropped','source_floods_dropped'),
        "multicast":  _i2(ib,'packetsMulticast','packets_multicast'),
    }

    radios = []
    for bloco in extrair_blocos(txt_estado, 'wireless'):
        st = extrair_bloco(bloco, 'stats')
        canal     = _i(bloco,'channel')
        ruido     = _i(bloco,'noise')
        freq      = "5GHz" if canal > 14 else "2.4GHz"
        ch_active = _i2(bloco,'channelActiveTime','channel_active_time')
        ch_busy   = _i2(bloco,'channelBusyTime','channel_busy_time')
        ch_rx     = _i2(bloco,'channelReceiveTime','channel_receive_time')
        ch_tx     = _i2(bloco,'channelTransmitTime','channel_transmit_time')

        # State.Wireless.AP = { key, action, essid, repeated Client client }.
        # Não existe clientCount/clients: o número de clientes é a CONTAGEM
        # dos blocos `client`. E não existe `enabled` no AP — exigi-lo fazia
        # com que NENHUM AP entrasse na lista (o antigo _b() devolvia False
        # sempre), zerando rajant_ap_clientes e rajant_radio_clientes juntas.
        aps, total_cli = [], 0
        for apb in extrair_blocos(bloco, 'ap'):
            esid = _s(apb,'essid')
            cli  = len(extrair_blocos(apb, 'client'))
            if esid:
                aps.append({"essid": esid, "clients": cli, "freq": freq})
                total_cli += cli

        peers = []
        for pb in extrair_blocos(bloco, 'peer'):
            ipm2 = re.search(r'ipv4[Aa]ddress:\s*"([\d.]+)"', pb)
            ip_p = ipm2.group(1) if ipm2 else ""
            # ipv4Address é OPCIONAL no State.Peer: exigir o IP descartava
            # o enlace inteiro — e com ele taxa, custo e SNR. Sem IP,
            # identifica-se pelo MAC.
            if not ip_p or ip_p == "0.0.0.0":
                macm = re.search(r'\bmac:\s*"([^"]+)"', pb)
                ip_p = f"mac:{macm.group(1)}" if macm else ""
            if not ip_p: continue
            rssi  = _i(pb,'rssi')
            sinal = _i(pb,'signal')
            # SNR = sinal - ruído, ambos em dBm (State.Peer.signal e
            # State.Wireless.noise). A conta antiga era `rssi - abs(ruido)`,
            # que mistura escala relativa (RSSI) com absoluta (dBm): com
            # rssi=30 e ruido=-95 dava 30-95 = -65 dB, sempre negativo, e
            # derrubava good_peers (SNR>30) para zero em toda a rede.
            # Sem ruído no state, cai no RSSI, que no Rajant já é medido
            # acima do piso de ruído.
            snr = (sinal - ruido) if (ruido and sinal) else rssi
            peers.append({
                "ip":    ip_p,
                "ativo": _b(pb,'enabled'),
                "sinal": sinal,
                "rssi":  rssi,
                "snr":   snr,
                # bc_livestats.py oficial: pv.rate / 10 = Mbps
                "taxa":  round(_i(pb,'rate')/10.0, 1),
                "custo": _i(pb,'cost'),
                # State.Peer não tem txpower — campo removido (era sempre 0).
                "idade": _i(pb,'age'),
                # State.Peer NÃO tem name nem serialNumber — conferido no
                # State.proto: os campos são mac, enabled, cost, rate,
                # rssi, signal, age, stats, encapId, ipv4Address. O que
                # identifica o vizinho por NOME é o encapId: ele é o
                # sufixo do número de série do rádio.
                #
                # Verificado nos 40 vizinhos da captura real da ERM-12:
                #   ES1-2450CS-113187 <-> encap 113187  (ERB-11 L1)
                #   FE1-2255B-107805  <-> encap 107805  (ERB-02)
                # 40 de 40. Não é heurística; é a chave que o próprio
                # MeshMapper usa para mostrar "ERB-07" em vez do MAC.
                "encap": _i(pb,'encapId') or None,
            })

        custos     = [p["custo"] for p in peers if p["ativo"] and p["custo"] > 0]
        # Good peers = peers ativos com SNR > 30dB (BCE threshold)
        good_peers = sum(1 for p in peers if p["ativo"] and p["snr"] > 30)

        radios.append({
            "nome":         _s(bloco,'name'),
            "canal":        canal,
            "freq":         freq,
            "ruido":        ruido,
            "ruido_cls":    class_ruido(ruido),
            "txpwr":        _i(bloco,'txpower'),
            # radar_detec / radar_pulsos / phy REMOVIDOS: radarDetections,
            # pulseEvents e rxPhyErrors não existem em State.Wireless. Ficar
            # como None ainda fazia publicar() estourar em .set(None).
            "rx_bytes":     _i2(st,'rxBytes','rx_bytes'),
            "tx_bytes":     _i2(st,'txBytes','tx_bytes'),
            "rx_pkts":      _i2(st,'rxPackets','rx_packets'),
            "tx_pkts":      _i2(st,'txPackets','tx_packets'),
            "ch_active":    ch_active,
            "ch_busy":      ch_busy,
            "ch_rx":        ch_rx,
            "ch_tx":        ch_tx,
            "busy_pct":     _pct(ch_busy, ch_active),
            "rx_pct":       _pct(ch_rx,   ch_active),
            "tx_pct":       _pct(ch_tx,   ch_active),
            "idle_pct":     _pct(max(0, ch_active-ch_busy), ch_active),
            # Interferência = meio ocupado por transmissão que NÃO é nossa.
            # Fica None aqui de propósito: os contadores são cumulativos
            # desde o boot, e a razão entre eles daria a média da vida
            # inteira do rádio. Um BC ligado há meses num ambiente sujo
            # apareceria eternamente interferido, e um recém-ligado limpo.
            # calcular_taxas() preenche com o delta entre duas coletas,
            # que é a ocupação de agora.
            "interf_pct":   None,
            "peers_ativos": sum(1 for p in peers if p["ativo"]),
            "peers_total":  len(peers),
            "good_peers":   good_peers,
            "total_cli":    total_cli,
            "first_hop":    min(custos) if custos else 0,
            "peers":        peers,
            "aps":          aps,
        })

    ethernet = []
    blocos_wired = extrair_blocos(txt_estado, 'wired')
    # Fallback: algumas versões nomeiam o bloco de outra forma
    if not blocos_wired:
        for alt in ('ethernet', 'interface', 'port', 'eth'):
            blocos_wired = extrair_blocos(txt_estado, alt)
            if blocos_wired:
                log.debug(f"[parse] blocos de ethernet encontrados como '{alt}'")
                break
    # Guarda extra: bloco com campos exclusivos de Config não é estado
    blocos_wired = [b for b in blocos_wired
                    if not re.search(r'\b(requestFallback|alternateGateway|'
                                     r'speedMbps|duplexMode|gatewayMode|portVLANConfig)\s*:', b)]
    for idx, b in enumerate(blocos_wired):
        # O bloco de contadores pode se chamar stats/statistics/counters —
        # e em algumas versões os campos ficam direto no bloco da porta.
        # Sem esse fallback, a porta aparece no Grafana com tudo zerado.
        s = ""
        for nome_stats in ('stats', 'statistics', 'counters', 'stat'):
            s = extrair_bloco(b, nome_stats)
            if s: break
        if not s or not re.search(r'(rx_?[Bb]ytes|tx_?[Bb]ytes)', s):
            s = b   # campos direto na porta
        apt = _s2(b,'aptState','apt_state').lower()
        # Fallback: label 'porta' NUNCA pode ser vazio — série com
        # porta="" fica invisível/infiltrável no Grafana
        nome_porta = _s(b,'name').strip() or f"eth{idx}"
        # O State.Wired do bcapi tem SÓ: key, action, mac, masterMac,
        # aptState, stats(CommStats: rx/txBytes, rx/txPackets), peer, name,
        # ipv4. Não existem linkup, erros, CRC nem drops — pedir esses
        # campos devolvia zero e parecia porta morta. O link é inferido:
        # aptState != NONE, ou tráfego/peers presentes.
        tem_peer = bool(extrair_blocos(b, 'peer'))
        apt_i    = _APT_MAP.get(apt, 2)   # 2 = APT_STATE_NONE
        ethernet.append({
            "nome":     nome_porta,
            # Link inferido: APT ativo (MASTER/SLAVE/LINK), peer na porta ou
            # tráfego contado. aptState ausente ("") NÃO conta como link —
            # antes contava, e toda porta sem o campo aparecia como "up".
            "link":     (apt_i in (0, 1, 3)) or tem_peer,
            "apt_int":  apt_i,
            "rx_bytes": _i2(s,'rxBytes','rx_bytes','rxOctets','rx_octets','ifInOctets'),
            "tx_bytes": _i2(s,'txBytes','tx_bytes','txOctets','tx_octets','ifOutOctets'),
            "rx_pkts":  _i2(s,'rxPackets','rx_packets','rxFrames','rx_frames'),
            "tx_pkts":  _i2(s,'txPackets','tx_packets','txFrames','tx_frames'),
            # rx_err/tx_err/rx_crc/rx_drop/tx_drop/mudancas foram REMOVIDOS
            # da estrutura junto com as métricas: CommStats só tem
            # rx/txBytes e rx/txPackets. Manter as chaves em None ainda
            # deixava publicar() chamar .set(None) e estourar TypeError.
            "peers_eth": len(extrair_blocos(b, 'peer')),
        })
    if not blocos_wired:
        log.debug("[parse] Nenhum bloco 'wired' encontrado no state "
                  "(use --diagnostico-eth IP para inspecionar o formato bruto)")

    # Último recurso: nada extraído ou tudo zerado → varredura profunda
    if not ethernet or all((e["rx_bytes"] + e["tx_bytes"]) == 0 for e in ethernet):
        achados = _varredura_ethernet(txt)
        if achados:
            log.debug(f"[parse] ethernet recuperada por varredura profunda: "
                      f"{[a['nome'] for a in achados]}")
            if not ethernet:
                ethernet = achados
            else:
                for e in ethernet:
                    for a in achados:
                        if a["nome"] == e["nome"]:
                            for k, v in a.items():
                                if v and not e.get(k): e[k] = v

    # ── APT Master ─────────────────────────────────────────────
    # aptMaster/isMaster não existem em State.System (nem em lugar nenhum).
    # O protocolo expõe State.Wired.aptState, cujo enum tem APT_STATE_MASTER
    # como valor 0. O BC é master do Auto Port Trunking se QUALQUER porta
    # wired estiver em MASTER. _APT_MAP mapeia 'master' -> 0.
    sistema["apt_master"] = any(e.get("apt_int") == 0 for e in ethernet)

    # Só ENDEREÇO IP entra aqui: é desta lista que a descoberta sai
    # tentando conectar. O identificador `mac:AA:BB:...` existe porque
    # State.Peer.ipv4Address é opcional e sem ele o enlace seria
    # descartado — mas ele não é um endereço. Ia para a fila de
    # descoberta, falhava a conexão, era marcado OFFLINE e virava
    # equipamento fantasma nas métricas.
    peers_ips = {p["ip"] for r in radios for p in r["peers"]
                 if p["ip"] and p["ip"] != "0.0.0.0"
                 and not str(p["ip"]).startswith("mac:")}

    return {"sistema": sistema, "instamesh": instamesh,
            "radios": radios, "ethernet": ethernet,
            "peers_ips": peers_ips}


# ══════════════════════════════════════════════════════════════
# TAXAS
# ══════════════════════════════════════════════════════════════
_ultimo = {}

def calcular_taxas(ip, dados, ts):
    ant = _ultimo.get(ip)
    _ultimo[ip] = {"d": dados, "ts": ts}
    if not ant or (ts - ant["ts"]) < 1: return dados
    dt = ts - ant["ts"]
    ad = ant["d"]
    im = dados["instamesh"]
    aim= ad.get("instamesh", {})

    # ── CPU derivada de State.System.idle ──────────────────────
    # Não existe cpuLoad no protocolo. `idle` e `uptime` são o par do
    # /proc/uptime: contadores acumulados desde o boot. A carga é
    #     100 * (1 - Δidle/Δuptime)
    # e a razão é adimensional — não importa se o firmware publica em
    # segundos ou milissegundos, desde que os dois usem a mesma unidade.
    # Só publica quando o resultado é plausível; nada de 0 por falta de dado.
    s_at, s_ant = dados["sistema"], ad.get("sistema", {})
    d_idle = s_at.get("cpu_idle", 0.0) - (s_ant.get("cpu_idle") or 0.0)
    d_up   = s_at.get("uptime_raw", 0.0) - (s_ant.get("uptime_raw") or 0.0)
    if d_up > 0 and d_idle >= 0:
        carga = 100.0 * (1.0 - (d_idle / d_up))
        # Fora de [0,100] = firmware com idle por núcleo (multicore) ou
        # reboot no meio da janela. Melhor não publicar do que publicar
        # um número que o operador vai tratar como real.
        if -1.0 <= carga <= 101.0:
            s_at["cpu_pct"] = round(min(100.0, max(0.0, carga)), 1)

    def ps(k, t=dt): return round(max(im.get(k,0)-aim.get(k,0), 0)/t, 3)
    im["tx_ps"]     = ps("pkt_tx")
    im["rx_ps"]     = ps("pkt_rx")
    im["drop_ps"]   = ps("pkt_drop")
    im["floods_ps"] = ps("floods")
    im["arp_ps"]    = ps("arp")
    td = max(im["pkt_tx"]  -aim.get("pkt_tx",0),   0)
    dd = max(im["pkt_drop"]-aim.get("pkt_drop",0),  0)
    im["perda_pct"] = round(dd/(td+dd)*100, 2) if (td+dd) > 0 else 0.0

    a_rad = {r["nome"]: r for r in ad.get("radios",[])}
    for r in dados["radios"]:
        ar = a_rad.get(r["nome"], {})
        def db(c): return max(r[c]-ar.get(c,r[c]), 0)
        r["rx_mbps"] = _mbps(db("rx_bytes"), dt)
        r["tx_mbps"] = _mbps(db("tx_bytes"), dt)
        r["rx_pps"]  = round(db("rx_pkts")/dt, 1)
        r["tx_pps"]  = round(db("tx_pkts")/dt, 1)
        ca = db("ch_active")
        if ca > 0:
            r["busy_pct"] = _pct(db("ch_busy"), ca)
            r["rx_pct"]   = _pct(db("ch_rx"),   ca)
            r["tx_pct"]   = _pct(db("ch_tx"),   ca)
            r["idle_pct"] = round(max(0.0, 100.0-r["busy_pct"]), 2)
            # O que sobra de `busy` depois de descontar o nosso próprio RX
            # e TX é tempo de antena consumido por outro transmissor: é o
            # indicador de interferência que a BC API permite. Mede o que
            # de fato rouba banda do enlace — não energia de RF solta,
            # como faria um analisador de espectro.
            alheio = max(0, db("ch_busy") - db("ch_rx") - db("ch_tx"))
            r["interf_pct"] = _pct(alheio, ca)

    a_eth = {e["nome"]: e for e in ad.get("ethernet",[])}
    for e in dados["ethernet"]:
        ae = a_eth.get(e["nome"], {})
        e["rx_mbps"] = _mbps(max(e["rx_bytes"]-ae.get("rx_bytes",e["rx_bytes"]),0), dt)
        e["tx_mbps"] = _mbps(max(e["tx_bytes"]-ae.get("tx_bytes",e["tx_bytes"]),0), dt)
    return dados

# ══════════════════════════════════════════════════════════════
# PING RTT (ICMP)
# ══════════════════════════════════════════════════════════════
import subprocess, platform

def ping_rtt(ip, timeout_ms=1000):
    """Retorna RTT em ms ou None se falhou. Funciona em Windows e Linux."""
    try:
        sistema = platform.system().lower()
        if sistema == "windows":
            cmd = ["ping", "-n", "1", "-w", str(timeout_ms), ip]
        else:
            cmd = ["ping", "-c", "1", "-W", "1", ip]
        
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
        out = r.stdout
        
        # Linux: "time=0.845 ms" (decimal!) | Windows: "Tempo = 12ms"
        # (\d+(?:\.\d+)?) aceita inteiros e decimais — a versão antiga
        # (\d+) falhava em todo RTT decimal, deixando ping_ok=0 no Linux
        for pat in [r"[Tt]empo[=<]\s*(\d+(?:\.\d+)?)\s*ms",
                    r"[Tt]ime[=<]\s*(\d+(?:\.\d+)?)\s*ms",
                    r"Average\s*=\s*(\d+(?:\.\d+)?)ms",
                    r"M[eé]dia\s*=\s*(\d+(?:\.\d+)?)ms"]:
            m = re.search(pat, out)
            if m:
                return float(m.group(1))
        return None
    except Exception:
        return None

# ══════════════════════════════════════════════════════════════
# ESTABILIDADE DE LINK & MESH SCORE
# ══════════════════════════════════════════════════════════════
_peer_estado_anterior = {}  # ip -> set de peers ativos

def calcular_link_changes(ip, radios):
    """Conta quantos peers mudaram de estado (entraram/saíram) desde última coleta."""
    peers_agora = set()
    for r in radios:
        for p in r.get("peers", []):
            if p.get("ativo"):
                peers_agora.add(f"{r['nome']}:{p['ip']}")
    
    anterior = _peer_estado_anterior.get(ip, peers_agora)
    changes  = len(peers_agora.symmetric_difference(anterior))
    _peer_estado_anterior[ip] = peers_agora
    return changes

def calcular_mesh_score(dados):
    """
    Score de qualidade da malha para este BC (0-100).
    Baseado em: SNR médio dos good peers, busy%, perda, disponibilidade.
    """
    try:
        radios = dados.get("radios", [])
        im     = dados.get("instamesh", {})

        # SNR médio dos peers ativos (peso 40%)
        snrs = []
        for r in radios:
            for p in r.get("peers", []):
                if p.get("ativo") and p.get("snr", 0) > -10:
                    snrs.append(min(p["snr"], 60))
        snr_score = (sum(snrs) / len(snrs) / 60 * 100) if snrs else 0

        # Good peers ratio (peso 30%)
        total_peers = sum(r.get("peers_ativos", 0) for r in radios)
        good_peers  = sum(r.get("good_peers", 0) for r in radios)
        good_score  = (good_peers / total_peers * 100) if total_peers > 0 else 0

        # Canal busy (peso 20%) — menos é melhor
        busy_vals = [r.get("busy_pct", 0) for r in radios if r.get("busy_pct") is not None]
        busy_avg  = sum(busy_vals) / len(busy_vals) if busy_vals else 100
        busy_score = max(0, 100 - busy_avg)

        # Perda de pacotes (peso 10%) — menos é melhor
        perda = im.get("perda_pct", 0)
        loss_score = max(0, 100 - perda * 5)

        score = (snr_score * 0.4 + good_score * 0.3 +
                 busy_score * 0.2 + loss_score * 0.1)
        return round(min(100, max(0, score)), 1)
    except Exception:
        return 0.0

# ══════════════════════════════════════════════════════════════
# PUBLICAR
# ══════════════════════════════════════════════════════════════
def publicar(ip, dados, est, nome_vizinho=None):
    """Publica a coleta de UM BC e retira as séries que deixaram de existir.

    `nome_vizinho(peer)` dá a tag do vizinho (ou None). Com ele, a série de
    enlace sai com o nome do vizinho no label `peer`, e vizinho sem tag não
    vira série. Sem ele (testes, uso antigo), `peer` é o IP, como antes.
    """
    s   = dados["sistema"]
    im  = dados["instamesh"]
    # CRÍTICO: bc nunca pode ser vazio
    # BCs sem nome configurado usam o IP como label 'bc'
    # Sem isso o label 'bc' some do Prometheus e a variável do Grafana fica vazia
    bc  = (s["nome"] or "").strip() or ip
    s["nome"] = bc
    lbc = {"bc": bc, "ip": ip}
    jd  = janela_bc(ip)
    serie = SeriesDoBC(ip)

    # Registra evento de disponibilidade
    jd.registrar(True)

    def put(metrica, valor, **labels):
        serie.set(metrica, valor, **(labels or lbc))

    def pub(metrica, valor, **labels):
        """Publica só quando há medição de verdade.

        None = a BC API não forneceu o dado nesta coleta. Publicar 0 no
        lugar é indistinguível de "medido e deu zero" — foi o que fazia
        painel de erro de ethernet e CPU parecerem saudáveis.
        Além disso, prometheus_client faz float(valor) e estoura
        TypeError com None: antes desta guarda, um único campo ausente
        abortava publicar() no meio, a exceção era engolida pelo
        `except Exception` de _coletar_bc e o BC inteiro era marcado
        OFFLINE — com metade das séries já publicadas.
        """
        put(metrica, valor, **labels)

    put(m_online, 1)
    put(m_bc_info, 1, bc=bc, ip=ip, modelo=s.get("modelo",""),
        firmware=s.get("firmware",""), grupos=s.get("grupos",""))
    put(m_uptime, s["uptime_s"])
    if s.get("gps_fix"):
        pub(m_gps_lat,  s["gps_lat"])
        pub(m_gps_lon,  s["gps_lon"])
        pub(m_gps_vel,  s.get("gps_vel"))
        pub(m_gps_rumo, s.get("gps_rumo"))
    # Qualidade do fix vale mesmo sem posição válida (ajuda a diagnosticar
    # por que não há fix): satélites à vista, altitude, precisão horizontal.
    pub(m_gps_sats, s.get("gps_sats"))
    pub(m_gps_alt,  s.get("gps_alt"))
    pub(m_gps_hdop, s.get("gps_hdop"))
    pub(m_gps_qual, s.get("gps_qual"))
    put(m_gps_fix, s.get("gps_fix", 0))
    put(m_boot, s["boot"])
    put(m_reboot_needed, s["reboot"])
    put(m_bridge, 1 if s["bridge"] else 0)
    put(m_apt_master, 1 if s["apt_master"] else 0)
    pub(m_best_rate, s.get("best_rate"))
    pub(m_alertas,   s.get("alertas"))
    put(m_falhas, est.falhas_consec)
    put(m_ultima_coleta, est.epoch_ultimo_ok or None)
    put(m_memoria, s["mem_kb"])
    # Disponibilidade em janelas deslizantes (metodologia BCE)
    put(m_avail_1h, jd.disponibilidade(3600))
    put(m_avail_24h, jd.disponibilidade(86400))
    put(m_avail_7d, jd.disponibilidade(604800))
    # Ping RTT (ICMP) — mede latência real até o BC
    rtt = ping_rtt(ip)
    if rtt is not None:
        put(m_ping_rtt, rtt)
        put(m_ping_ok, 1)
    else:
        put(m_ping_ok, 0)
    # Estabilidade de links — quantos peers mudaram desde última coleta
    changes = calcular_link_changes(ip, dados["radios"])
    put(m_link_changes, changes)
    # Score de qualidade da malha (0-100)
    score = calcular_mesh_score(dados)
    put(m_mesh_score, score)
    t = validar(s["temp_c"], "temperatura_c", bc)
    if t is not None:
        put(m_temp, t)
        if s["temp_cls"] is not None:
            put(m_temp_classe, s["temp_cls"])
    # Bateria (State.Battery). Modelos sem bateria não trazem o bloco →
    # tudo None → nenhuma série publicada, em vez de uma fileira de zeros.
    bat = s.get("bateria") or {}
    pub(m_bat_pct,      validar(bat.get("pct"), "bateria_pct", bc))
    pub(m_bat_ma,       bat.get("ma"))
    pub(m_bat_temp,     bat.get("temp_c"))
    pub(m_bat_desc_min, bat.get("desc_min"))
    pub(m_bat_chg_min,  bat.get("chg_min"))
    if bat.get("carregando") is not None:
        put(m_bat_charge, 1 if bat["carregando"] else 0)
    # cpu_pct só existe a partir da 2a coleta (delta de idle/uptime)
    pub(m_cpu, validar(s.get("cpu_pct"), "cpu_pct", bc))

    put(m_im_tx, im["pkt_tx"])
    put(m_im_rx, im["pkt_rx"])
    put(m_im_drop, im["pkt_drop"])
    put(m_im_floods, im["floods"])
    put(m_im_arp, im["arp"])
    put(m_im_disc_src, im["disc_src"])
    put(m_im_disc_pass, im["disc_pass"])
    put(m_im_src_floods, im["src_floods"])
    put(m_im_multicast, im["multicast"])
    put(m_im_tx_ps, im.get("tx_ps",0))
    put(m_im_rx_ps, im.get("rx_ps",0))
    put(m_im_drop_ps, im.get("drop_ps",0))
    put(m_im_perda, im.get("perda_pct",0))
    put(m_im_floods_ps, im.get("floods_ps",0))
    put(m_im_arp_ps, im.get("arp_ps",0))
    for r in dados["radios"]:
        rn   = r["nome"]
        rlbs = {"bc":bc,"ip":ip,"radio":rn,"canal":str(r["canal"]),"freq":r["freq"]}
        put(m_r_canal, r["canal"], **rlbs)
        put(m_r_txpower, r["txpwr"], **rlbs)
        put(m_r_rx_bytes, r["rx_bytes"], **rlbs)
        put(m_r_tx_bytes, r["tx_bytes"], **rlbs)
        put(m_r_rx_pkts, r["rx_pkts"], **rlbs)
        put(m_r_tx_pkts, r["tx_pkts"], **rlbs)
        put(m_r_ch_active, r["ch_active"], **rlbs)
        put(m_r_ch_busy, r["ch_busy"], **rlbs)
        put(m_r_ch_rx, r["ch_rx"], **rlbs)
        put(m_r_ch_tx, r["ch_tx"], **rlbs)
        put(m_r_busy, r.get("busy_pct",0), **rlbs)
        put(m_r_rx_pct, r.get("rx_pct",0), **rlbs)
        put(m_r_tx_pct, r.get("tx_pct",0), **rlbs)
        put(m_r_idle, r.get("idle_pct",0), **rlbs)
        # Sem delta ainda (1a coleta do rádio) fica sem série, nunca 0 —
        # zero seria lido como "medi e não há interferência".
        pub(m_r_interf, r.get("interf_pct"), **rlbs)
        put(m_r_peers_ativos, r["peers_ativos"], **rlbs)
        put(m_r_peers_total, r["peers_total"], **rlbs)
        put(m_r_good_peers, r["good_peers"], **rlbs)
        put(m_r_clients, r["total_cli"], **rlbs)
        put(m_r_rx_mbps, r.get("rx_mbps",0), **rlbs)
        put(m_r_tx_mbps, r.get("tx_mbps",0), **rlbs)
        put(m_r_rx_pps, r.get("rx_pps",0), **rlbs)
        put(m_r_tx_pps, r.get("tx_pps",0), **rlbs)
        nd = validar(r["ruido"], "ruido_dbm", bc)
        if nd is not None:
            put(m_r_ruido, nd, **rlbs)
            if r["ruido_cls"] is not None:
                put(m_r_ruido_cls, r["ruido_cls"], **rlbs)
        if r["first_hop"] > 0:
            put(m_first_hop, r["first_hop"], bc=bc, ip=ip, radio=rn)

        for p in r["peers"]:
            # O vizinho entra pela TAG. Sem tag (IP solto, rádio de
            # terceiro, vizinho só com MAC) não vira série: as contagens do
            # rádio (peers_ativos, good_peers) já o contam.
            if nome_vizinho is not None:
                nome_p = nome_vizinho(p)
                if not nome_p:
                    continue
            else:
                nome_p = p["ip"]
            plbs = {"bc":bc,"ip":ip,"radio":rn,"peer":nome_p}
            sn = validar(p["sinal"],"sinal_dbm",bc)
            sq = validar(p["snr"],  "snr_db",   bc)
            tx = validar(p["taxa"], "taxa_mbps", bc)
            cu = validar(p["custo"],"custo",     bc)
            if sn is not None: put(m_p_sinal, sn, **plbs)
            if sq is not None: put(m_p_snr, sq, **plbs)
            if tx is not None: put(m_p_taxa, tx, **plbs)
            if cu is not None: put(m_p_custo, cu, **plbs)
            put(m_p_rssi, p["rssi"], **plbs)
            put(m_p_ativo, 1 if p["ativo"] else 0, **plbs)
        for ap in r["aps"]:
            put(m_ap_clients, ap["clients"], bc=bc, ip=ip, radio=rn,
                essid=ap["essid"], freq=ap["freq"])

    for e in dados["ethernet"]:
        elbs = {"bc":bc,"ip":ip,"porta":e["nome"]}
        put(m_e_link, 1 if e["link"] else 0, **elbs)
        put(m_e_apt_state, e["apt_int"], **elbs)
        put(m_e_rx_bytes, e["rx_bytes"], **elbs)
        put(m_e_tx_bytes, e["tx_bytes"], **elbs)
        put(m_e_rx_pkts, e["rx_pkts"], **elbs)
        put(m_e_tx_pkts, e["tx_pkts"], **elbs)
        put(m_e_peers, e.get("peers_eth", 0), **elbs)
        put(m_e_rx_mbps, e.get("rx_mbps",0), **elbs)
        put(m_e_tx_mbps, e.get("tx_mbps",0), **elbs)

    # O que foi publicado antes e não veio agora (vizinho que se afastou,
    # rádio desligado, porta que sumiu, nome que mudou) sai do /metrics.
    serie.fechar()


# Formato da tag de equipamento da mina: letras, hífen opcional, número
# (CA-1035, ERM-22 PTP CAM, ERB-11 L2, RED065, PCP-002). Os 155 nomes da
# REDE_CONHECIDA casam; IP e nome vazio, não.
PADRAO_TAG_PADRAO = r"^[A-Z]{2,5}[- ]?\d{2,}"

_RX_IP = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

# ══════════════════════════════════════════════════════════════
# SÉRIES DE CADA BC
# ══════════════════════════════════════════════════════════════
# O prometheus_client guarda o último valor de cada série PARA SEMPRE: o
# que não é retirado continua no /metrics, e o Prometheus o lê a cada
# raspagem como se fosse leitura nova. Era a linha reta do Grafana — rádio
# que parou de responder, vizinho que se afastou, e o gráfico seguindo no
# último valor por horas.
#
# Cada BC lembra as séries que publicou na última coleta. A coleta nova
# publica as suas e retira as que não vieram; a falha retira todas as de
# medição. Série retirada some do /metrics, o Prometheus a marca como
# encerrada, e o Grafana mostra o buraco — que é o que aconteceu.
_series_ip = {}
_series_lk = threading.Lock()


class SeriesDoBC:
    """As séries publicadas para UM IP numa coleta."""

    def __init__(self, ip):
        self.ip = ip
        self.feitas = set()

    def set(self, metrica, valor, **labels):
        if valor is None:
            return
        vals = tuple(str(labels[n]) for n in metrica._labelnames)
        metrica.labels(*vals).set(valor)
        self.feitas.add((metrica, vals))

    def fechar(self, manter=()):
        """Retira as séries da coleta anterior que esta não publicou.

        `manter`: métricas que sobrevivem mesmo assim (ver `marcar_offline`)."""
        with _series_lk:
            antes = _series_ip.get(self.ip, set())
            for metrica, vals in antes - self.feitas:
                if metrica in manter:
                    self.feitas.add((metrica, vals))
                    continue
                try:
                    metrica.remove(*vals)
                except KeyError:
                    pass
            _series_ip[self.ip] = self.feitas


def retirar_series(ip):
    """Tira do /metrics tudo o que foi publicado para `ip`."""
    with _series_lk:
        for metrica, vals in _series_ip.pop(ip, set()):
            try:
                metrica.remove(*vals)
            except KeyError:
                pass


def marcar_offline(ip, nome, est, agora):
    """A coleta falhou. Sai tudo o que é medição; fica a identidade.

    Ficam: `rajant_bc_info` (modelo, firmware), `rajant_online` = 0, as
    falhas seguidas, a última coleta boa e a disponibilidade — é o que diz
    QUAL rádio caiu e desde quando. Sinal, peers, GPS, temperatura, tráfego
    saem: não foram medidos agora, e repetir o último era a linha reta.

    `[coleta] segurar_serie_s` > 0 segura tudo por esse tempo (para
    quem prefere a linha contínua num rádio que perde uma coleta ou outra);
    o padrão é 0, retirar na primeira falha.
    """
    lbc = {"bc": nome or ip, "ip": ip}
    jd  = janela_bc(ip)
    jd.registrar(False)
    if est.ultima_dados and not est.expirou(agora):
        # Dentro da tolerância: as séries ficam como estão.
        m_falhas.labels(**lbc).set(est.falhas_consec)
        for m, jan in ((m_avail_1h, 3600), (m_avail_24h, 86400),
                       (m_avail_7d, 604800)):
            m.labels(**lbc).set(jd.disponibilidade(jan))
        return
    serie = SeriesDoBC(ip)
    serie.set(m_online, 0, **lbc)
    serie.set(m_falhas, est.falhas_consec, **lbc)
    if est.epoch_ultimo_ok:
        serie.set(m_ultima_coleta, est.epoch_ultimo_ok, **lbc)
    for m, jan in ((m_avail_1h, 3600), (m_avail_24h, 86400), (m_avail_7d, 604800)):
        serie.set(m, jd.disponibilidade(jan), **lbc)
    # rajant_bc_info sobrevive com os labels que tinha (modelo, firmware).
    serie.fechar(manter={m_bc_info})

# ══════════════════════════════════════════════════════════════
# COLETOR
# ══════════════════════════════════════════════════════════════
class RajantCollector:
    def __init__(self, seeds, role, password, port, interval,
                 max_threads, timeout, tentativas, falhas_limite,
                 manter_s, redesc_ciclos,
                 interval_moveis=0, padrao_movel="", descoberta=True,
                 prefixos_tag=None, padrao_tag=None):
        self.seeds=seeds; self.role=role; self.password=password; self.port=port
        self.interval=interval; self.max_threads=max_threads; self.timeout=timeout
        self.tentativas=tentativas; self.falhas_limite=falhas_limite
        self.manter_s=manter_s; self.redesc_ciclos=redesc_ciclos
        self.descoberta=descoberta
        # Filtro de tag: `padrao_tag` (regex) ou, no formato antigo, a lista
        # de prefixos. Nenhum dos dois = aceita qualquer nome.
        self.prefixos_tag = list(prefixos_tag) if prefixos_tag else None
        self.re_tag = (re.compile(padrao_tag, re.IGNORECASE)
                       if isinstance(padrao_tag, str) and padrao_tag
                       else padrao_tag)
        self.sem_tag = set()          # descartados, para o log e a métrica
        self.bcs=set(); self.nomes={}
        # encapId -> nome: o encapId do vizinho é o sufixo do serial do
        # rádio (State.Peer não traz nome). Montado com o serial de cada
        # rádio coletado.
        self.nome_encap = {}
        self.lock=threading.Lock(); self.sem=threading.Semaphore(max_threads)

        # ── Coleta acelerada dos equipamentos móveis ───────────────
        # O laço passa a girar no intervalo dos móveis; os fixos entram a
        # cada `passo_fixos` voltas, preservando o intervalo original deles.
        self.re_movel = None
        self.interval_moveis = 0
        self.passo_fixos = 1
        if interval_moveis and 0 < interval_moveis < interval and padrao_movel:
            padroes = [p.strip() for p in padrao_movel.split(";") if p.strip()]
            if padroes:
                try:
                    self.re_movel = re.compile("|".join(padroes), re.IGNORECASE)
                    self.interval_moveis = interval_moveis
                    self.passo_fixos = max(1, round(interval / interval_moveis))
                    log.info(f"[coleta] moveis a cada {interval_moveis}s "
                             f"(fixos a cada {self.passo_fixos} ciclos "
                             f"= {self.passo_fixos*interval_moveis}s) | "
                             f"padrao: {padrao_movel}")
                except re.error as e:
                    log.warning(f"[coleta] padrao_movel invalido ({e}); "
                                f"intervalo unico para toda a frota")

    def _eh_movel(self, ip):
        """Classifica pelo nome do BC. Nome ainda desconhecido conta como
        móvel para não deixar equipamento novo fora do primeiro ciclo."""
        if not self.re_movel: return True
        nome = (self.nomes.get(ip) or REDE_CONHECIDA.get(ip) or "").strip()
        if not nome or nome == ip: return True
        return bool(self.re_movel.search(nome))

    @property
    def filtrando(self):
        return bool(self.re_tag or self.prefixos_tag)

    def tem_tag(self, nome):
        """O nome é uma tag de equipamento?

        Sem filtro configurado, aceita tudo — exceto nome vazio ou IP, que
        nunca é tag. Com filtro, o nome tem de casar com `padrao_tag` (ou
        começar por um dos prefixos, no formato antigo)."""
        n = (nome or "").strip()
        if not n or _RX_IP.match(n):
            return not self.filtrando
        if self.re_tag is not None:
            return bool(self.re_tag.search(n))
        if self.prefixos_tag:
            return any(n.upper().startswith(p.upper()) for p in self.prefixos_tag)
        return True

    def nome_conhecido(self, ip):
        """O nome de um IP pelo que já se sabe: o que o rádio informou nesta
        execução, ou a lista da malha no código."""
        return ((self.nomes.get(ip) or "").strip()
                or (REDE_CONHECIDA.get(ip) or "").strip() or None)

    def nome_vizinho(self, p):
        """A tag do vizinho de um enlace, ou None se não tiver."""
        ip = p.get("ip") or ""
        nome = None
        if ip and not ip.startswith("mac:"):
            nome = self.nome_conhecido(ip)
        if not nome and p.get("encap"):
            nome = self.nome_encap.get(p["encap"])
        return nome if nome and self.tem_tag(nome) else None

    def _coletar_bc(self, ip):
        est  = estado(ip, self.falhas_limite, self.manter_s)
        agora= time.monotonic()
        nome = self.nome_conhecido(ip)
        for tentativa in range(1, self.tentativas+1):
            try:
                bc = exigir_rajant_api()(host=ip, port=self.port,
                                role=self.role, password=self.password)
                try:
                    if not bc.reachable():
                        raise ConnectionRefusedError("nao alcancavel")
                    autenticar(bc)
                    raw = ler_estado(bc)
                finally:
                    fechar_sessao(bc)
                dados = parse_state(raw)
                ts    = time.monotonic()
                dados = calcular_taxas(ip, dados, ts)
                nome  = dados["sistema"]["nome"] or ip
                self.nomes[ip] = nome

                # Filtro por tag: o BC respondeu, mas o nome nao e de
                # equipamento da frota. Descarta ANTES de publicar, senao
                # a metrica ja nasceu suja. Fica registrado, nunca some
                # calado.
                if not self.tem_tag(nome):
                    if ip not in self.sem_tag:
                        self.sem_tag.add(ip)
                        log.info(f"  - {nome:<22} ({ip}) ignorado: "
                                 f"nome sem tag")
                    with self.lock:
                        self.bcs.discard(ip)
                    # Se já foi publicado com outro nome, sai do /metrics.
                    retirar_series(ip)
                    m_bc_sem_tag.set(len(self.sem_tag))
                    return set()
                self.sem_tag.discard(ip)

                est.ok(dados, ts)
                if dados["sistema"].get("serial_num"):
                    self.nome_encap[dados["sistema"]["serial_num"]] = nome
                publicar(ip, dados, est,
                         nome_vizinho=self.nome_vizinho if self.filtrando else None)
                s = dados["sistema"]
                # CPU e bateria podem ser None (1a coleta / modelo sem
                # bateria) — "n/d" deixa isso explícito no log.
                _cpu = f"{s['cpu_pct']:.0f}%" if s.get("cpu_pct") is not None else "n/d"
                _bat = s.get("bateria") or {}
                _bat_s = f"{_bat['pct']}%" if _bat.get("pct") is not None else "n/d"
                log.info(f"  V {nome:<22} ({ip}) "
                         f"{len(dados['radios'])}rad "
                         f"{sum(r['peers_ativos'] for r in dados['radios'])}peers "
                         f"T:{s['temp_c']}C Bat:{_bat_s} "
                         f"CPU:{_cpu} "
                         f"Disp1h:{janela_bc(ip).disponibilidade(3600):.0f}%")
                return dados["peers_ips"]
            except Exception as e:
                if tentativa < self.tentativas:
                    time.sleep(2**(tentativa-1))
                else:
                    est.falha()
                    # IP que nunca respondeu e não tem nome conhecido, ou
                    # cujo nome não é tag, não entra no /metrics nem como
                    # offline: viraria uma linha "10.188.99.217" no Grafana.
                    if self.tem_tag(nome):
                        marcar_offline(ip, nome, est, agora)
                    else:
                        retirar_series(ip)
                    if est.offline:
                        log.warning(f"  X {(nome or '?'):<22} ({ip}) OFFLINE "
                                    f"({est.falhas_consec}x): "
                                    f"{type(e).__name__}: {e}")
        return set()

    def _lote(self, fila, visitados):
        threads, res, lk = [], {}, threading.Lock()
        def worker(ip):
            with self.sem:
                peers = self._coletar_bc(ip)
                with lk: res[ip] = peers
        for ip in fila:
            if ip in visitados: continue
            visitados.add(ip)
            t = threading.Thread(target=worker, args=(ip,), daemon=True)
            threads.append(t); t.start()
        for t in threads: t.join(timeout=self.timeout+10)
        novos = set()
        for ip, peers in res.items():
            for peer_ip in peers:
                if peer_ip not in visitados: novos.add(peer_ip)
        with self.lock:
            antes = len(self.bcs); self.bcs.update(visitados)
            n = len(self.bcs)-antes
        if n > 0: log.info(f"  + {n} BCs novos")
        return novos

    def descobrir(self):
        log.info("="*55)
        log.info("  INICIANDO DESCOBERTA")
        seeds_ok, seeds_fail = [], []
        for seed in self.seeds:
            bc = None
            try:
                bc = exigir_rajant_api()(host=seed, port=self.port,
                                role=self.role, password=self.password)
                if bc.reachable() and autenticar(bc):
                    seeds_ok.append(seed)
                    m_seed_status.labels(seed=seed).set(1)
                    log.info(f"  Seed OK:     {seed}")
                else:
                    seeds_fail.append(seed)
                    m_seed_status.labels(seed=seed).set(0)
                    log.warning(f"  Seed OFFLINE:{seed}")
            except Exception as e:
                seeds_fail.append(seed)
                m_seed_status.labels(seed=seed).set(0)
                log.warning(f"  Seed OFFLINE:{seed} ({e})")
            finally:
                fechar_sessao(bc)

        lista = set(REDE_CONHECIDA)

        if not self.descoberta:
            # Lista fechada: coleta exatamente a lista do código e os
            # seeds, sem seguir peer nenhum. A descoberta acha tudo que
            # RESPONDE na malha — inclusive equipamento de terceiro e
            # rádio de teste —, e quem tem a lista dos proprios
            # equipamentos nao quer isso. Todos, nao so os que
            # responderam: um BC offline agora tem de continuar sendo
            # monitorado, senao ele desaparece da metrica justamente
            # quando cai.
            alvos = set(self.seeds) | lista
            with self.lock:
                self.bcs = set(alvos)
            log.info(f"  Descoberta DESLIGADA: {len(alvos)} IP(s) fixos "
                     f"(lista do codigo + seeds)")
            if seeds_fail:
                log.warning(f"  {len(seeds_fail)} sem resposta agora, "
                            f"mantidos na lista: "
                            f"{', '.join(sorted(seeds_fail)[:8])}"
                            + (" ..." if len(seeds_fail) > 8 else ""))
            vis = set()
            self._lote(alvos, vis)          # uma passada, sem expandir
            log.info(f"  Total: {len(self.bcs)} BCs")
            log.info("="*55)
            return

        # A lista do código entra inteira, responda ou não: com o seed fora
        # do ar a partida continua, e rádio offline segue monitorado.
        ponto_inicio = set(seeds_ok) | lista
        with self.lock:
            self.bcs.update(lista)
        log.info(f"  Seeds OK:{len(seeds_ok)} | Lista do codigo:{len(lista)} IPs")
        fila, vis = set(ponto_inicio), set()
        while fila: fila = self._lote(fila, vis)
        m_cache_total.set(len(self.bcs))
        fora = len(self.bcs - lista)
        log.info(f"  Total: {len(self.bcs)} BCs"
                 + (f" ({fora} achados pela descoberta, fora da lista)" if fora else ""))
        log.info("="*55)

    def ciclo(self, apenas_moveis=False):
        threads = []
        alvos = [ip for ip in list(self.bcs)
                 if not apenas_moveis or self._eh_movel(ip)]
        for ip in alvos:
            def worker(x=ip):
                with self.sem:
                    peers = self._coletar_bc(x)
                    # Com a descoberta desligada, a lista e fechada: o
                    # ciclo nao pode reintroduzir pela porta dos fundos o
                    # que a descoberta deixou de fora.
                    if peers and self.descoberta:
                        with self.lock:
                            # O sem tag volta só na redescoberta (o nome pode
                            # ter mudado); a cada ciclo seria uma consulta
                            # inteira jogada fora por rádio.
                            novos = peers - self.bcs - self.sem_tag
                            if novos:
                                log.info(f"  + {len(novos)} peers novos")
                                self.bcs.update(novos)
            t = threading.Thread(target=worker, daemon=True)
            threads.append(t); t.start()
        for t in threads: t.join(timeout=self.timeout+10)
        online = sum(1 for ip in self.bcs
                     if estado(ip, self.falhas_limite, self.manter_s).respondendo)
        m_cache_online.set(online)
        m_cache_total.set(len(self.bcs))
        self._publicar_identidade()

    def _publicar_identidade(self):
        """Agrupa os IPs descobertos por nome de BC (nó físico) e elege um
        IP primário para cada um. Sem isso, um BC com N rádios conta como N."""
        grupos = {}
        def dados_de(ip):
            est = estado(ip, self.falhas_limite, self.manter_s)
            return (getattr(est, "ultima_dados", None) or {}).get("sistema") or {}

        def nome_de(ip):
            return (dados_de(ip).get("nome") or "").strip()

        for ip in list(self.bcs):
            if not self.tem_tag(nome_de(ip) or self.nome_conhecido(ip)):
                continue
            s = dados_de(ip)
            # Identidade do nó físico, em ordem de confiabilidade:
            #  1) o IP que o próprio BC reporta em system.ipv4 (único por nó)
            #  2) o nome configurado
            #  3) o IP consultado (nó ainda não identificado)
            # Nomes repetidos na rede (config duplicada) colapsariam nós
            # distintos — por isso o IP reportado vem primeiro.
            chave = (s.get("ip") or "").strip() or nome_de(ip) or ip
            grupos.setdefault(chave, []).append(ip)

        # Guarda: um grupo com muitas interfaces sugere colisão de nome,
        # não um nó com muitos rádios. Nesse caso, desagrupa (cada IP conta).
        MAX_IFACES = 6
        for chave in [k for k, v in grupos.items() if len(v) > MAX_IFACES]:
            ips = grupos.pop(chave)
            log.warning(f"  '{chave}' com {len(ips)} IPs — provavel colisao de nome; "
                        f"cada IP sera contado como no distinto")
            for ip in ips: grupos[f"{chave}#{ip}"] = [ip]

        def ip_ordem(ip):
            try:   return tuple(int(x) for x in ip.split("."))
            except Exception: return (999, 999, 999, 999)

        vistos = set()
        for chave, ips in grupos.items():
            ips_ord = sorted(ips, key=ip_ordem)
            primario = ips_ord[0]
            for ip in ips_ord:
                rotulo = nome_de(ip) or self.nome_conhecido(ip) or ip
                lb = (rotulo, ip)
                vistos.add(lb)
                m_bc_primario.labels(*lb).set(1 if ip == primario else 0)
                m_bc_ifaces.labels(*lb).set(len(ips_ord))
        # Interface que saiu (sem tag, renomeada, removida) sai do /metrics.
        for lb in getattr(self, "_identidade_antes", set()) - vistos:
            for m in (m_bc_primario, m_bc_ifaces):
                try: m.remove(*lb)
                except KeyError: pass
        self._identidade_antes = vistos
        m_nos_fisicos.set(len(grupos))
        log.info(f"  Nos fisicos: {len(grupos)} | IPs monitorados: {len(self.bcs)}")

    def run(self):
        self.descobrir()
        n = 0
        # Com coleta acelerada dos móveis, o laço gira no intervalo menor e
        # os fixos entram a cada `passo_fixos` voltas.
        intervalo = self.interval_moveis or self.interval
        while True:
            n += 1
            t0 = time.monotonic()
            completo = (self.passo_fixos <= 1) or (n % self.passo_fixos == 0)
            if completo:
                alvo_txt = f"{len(self.bcs)} BCs"
            else:
                alvo_txt = (f"{sum(1 for ip in self.bcs if self._eh_movel(ip))} "
                            f"moveis de {len(self.bcs)}")
            log.info(f"\n-- Ciclo #{n} | {datetime.now().strftime('%H:%M:%S')} "
                     f"| {alvo_txt} --")
            self.ciclo(apenas_moveis=not completo)
            # Redescoberta conta ciclos completos, não voltas do laço.
            if completo and self.redesc_ciclos > 0 and \
               (n // max(1, self.passo_fixos)) % self.redesc_ciclos == 0:
                log.info("Re-descoberta..."); self.descobrir()
            dur    = time.monotonic()-t0
            espera = max(0, intervalo-dur)
            log.info(f"  Ciclo {dur:.1f}s | Proximo em {espera:.0f}s")
            time.sleep(espera)

# ══════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════
#   MÓDULO DE RELATÓRIOS EXCEL  (semanal / mensal / personalizado)
#   Fonte de dados: Prometheus (mesma fonte do Grafana)
#   Interface: página web simples em [servidor] porta_relatorio
# ══════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════
import urllib.request, urllib.parse, random, io
from datetime import timedelta, date

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

DEFAULTS_RELATORIO = {
    "prometheus_url":  "http://localhost:9090",
    "porta_relatorio": "8010",
    # Metas (%) — do relatório semanal
    "meta_backbone":   "99.90",
    "meta_mesh":       "99.50",
    "meta_geral":      "99.90",
    # Limiares BCE
    "limite_cpu":      "60",
    "limite_temp":     "65",
    "limite_latencia": "10",
    "limite_ruido":    "-90",
    # Classificação por nome (regex ; separadas, case-insensitive)
    "padrao_erb":      "^ERB ; -ERB",
    "padrao_erm":      "^ERM ; -ERM",
    "padrao_backbone": "BACKBONE ; ^BB- ; CORE",
    "padrao_movel":    "^CA ; ^PA ; ^PF ; ^TT ; ^EH ; CAMINH ; ESCAV ; PERFURA",
    "template_ppt":    "Relatorio_Semanal_Rede.pptx",
    # Painel HTML próprio, servido em /painel. Editável sem reiniciar.
    "painel_html":     "painel/visao-geral.html",
    # true = o semanal sai na identidade Anglo (fundo branco, azul
    # institucional, logo e regua), repintando o template do cliente no fim
    # da geracao. Nada muda de lugar: menus, botoes, tabelas, graficos e o
    # que estiver digitado no template continuam onde estao.
    # false = deck no visual original do template.
    "identidade_anglo": "true",
}

def cfg_relatorio(cfg):
    if not cfg.has_section("relatorio"):
        cfg.add_section("relatorio")
    mudou = False
    for k, v in DEFAULTS_RELATORIO.items():
        if not cfg.has_option("relatorio", k):
            cfg.set("relatorio", k, v); mudou = True
    if mudou:
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f: cfg.write(f)
        except Exception: pass
    return cfg

# ──────────────────────────────────────────────────────────────
# Canal → frequência central (MHz)
# 2.4 GHz: f = 2407 + 5·ch (ch14 = 2484, exceção do padrão)
# 5 GHz:   f = 5000 + 5·ch (ex.: ch149 → 5745 MHz)
# ──────────────────────────────────────────────────────────────
def canal_para_mhz(canal):
    try: c = int(canal)
    except: return None
    if c <= 0: return None
    if c == 14: return 2484
    if c <= 13: return 2407 + 5*c
    return 5000 + 5*c

# ──────────────────────────────────────────────────────────────
# Cliente Prometheus (HTTP API, stdlib)
# ──────────────────────────────────────────────────────────────
class Prometheus:
    def __init__(self, url): self.base = url.rstrip("/")
    def _get(self, endpoint, params):
        url = f"{self.base}/api/v1/{endpoint}?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(url, timeout=120) as r:
            data = json.loads(r.read().decode())
        if data.get("status") != "success":
            raise RuntimeError(f"Prometheus: {data}")
        return data["data"]["result"]
    def instant(self, query, ts=None):
        p = {"query": query}
        if ts: p["time"] = ts
        return self._get("query", p)
    def range(self, query, ini, fim, passo):
        return self._get("query_range",
                         {"query": query, "start": ini, "end": fim, "step": passo})

def classificador_categorias(cfg):
    padroes = [("ERB",      cfg.get("relatorio","padrao_erb")),
               ("ERM",      cfg.get("relatorio","padrao_erm")),
               ("Backbone", cfg.get("relatorio","padrao_backbone")),
               ("Móvel",    cfg.get("relatorio","padrao_movel"))]
    comp = [(cat, [re.compile(p.strip(), re.IGNORECASE)
                   for p in pats.split(";") if p.strip()])
            for cat, pats in padroes]
    def classificar(nome, grupos=""):
        alvo = f"{nome} {grupos}"
        for cat, regs in comp:
            if any(r.search(alvo) for r in regs): return cat
        return "Outros"
    return classificar

# ──────────────────────────────────────────────────────────────
# COLETA (Prometheus → estrutura do relatório)
# ──────────────────────────────────────────────────────────────
def _r_bc(res):
    return {r["metric"].get("bc", r["metric"].get("ip","?")): float(r["value"][1])
            for r in res}

def _r_labels(res):
    """[(labels_dict, valor)]"""
    return [(r["metric"], float(r["value"][1])) for r in res]

def coletar_dados(prom, cfg, ini_dt, fim_dt, dias):
    # ── Clamp ao presente: consultar o Prometheus num instante FUTURO
    # retorna vazio (lookback de 5 min). Relatório de "hoje" terminaria
    # à meia-noite de amanhã → tudo offline e quedas fantasma até 24h.
    agora  = time.time()
    ini_s  = ini_dt.timestamp()
    fim_s  = min(fim_dt.timestamp(), agora)
    dur_s  = max(60, fim_s - ini_s)
    jan    = f"{int(dur_s)}s"
    t      = fim_s
    d = {"bcs": {}, "dias": dias, "series": {}, "eventos": [],
         "radios": {}, "links": {}, "eths": {}}
    lock = threading.Lock()

    def bcinfo(bc):
        return d["bcs"].setdefault(bc, {"ip":"","modelo":"","firmware":"","grupos":""})

    # ── FILTRO DE NÓ FÍSICO ───────────────────────────────────
    # Um BreadCrumb com N rádios/sub-redes responde em N IPs, e a
    # descoberta por peers acha todos. Sem filtro, o relatório conta
    # interfaces em vez de aparelhos (a origem da contagem inflada).
    # O exporter já elege um IP primário por nó (rajant_bc_primario=1);
    # aqui restringimos TODAS as consultas a esses IPs.
    P = ""
    try:
        if prom.instant("rajant_bc_primario == 1", t):
            P = " and on(bc,ip) (rajant_bc_primario == 1)"
            n_fis = prom.instant("rajant_nos_fisicos", t)
            if n_fis:
                log.info(f"[rel] filtro de no fisico ativo "
                         f"({int(float(n_fis[0]['value'][1]))} nos)")
        else:
            log.warning("[rel] rajant_bc_primario ausente (exporter antigo): "
                        "a contagem pode incluir interfaces do mesmo BC")
    except Exception as e:
        log.warning(f"[rel] filtro de no fisico indisponivel: {e}")

    def F(sel):
        """Aplica o filtro de interface primária a um seletor."""
        return f"({sel}{P})" if P else sel

    # ── Lista canônica de nós FÍSICOS ──
    # Cada rádio de um BC tem IPv4 próprio; sem consolidar, um nó com 3
    # rádios vira 3 "BreadCrumbs". Prioridade: rajant_bc_primario (exporter
    # v2.4+) → bc_info → rajant_online, sempre agrupando pelo nome do BC.
    d["ifaces"] = {}
    canonicos = None
    try:
        res_p = prom.instant(f"max_over_time(rajant_bc_primario[{jan}])", t)
        if res_p:
            canonicos = set()
            for r in res_p:
                m = r["metric"]; nome = m.get("bc","?")
                if float(r["value"][1]) == 1:
                    canonicos.add(nome)
                    bcinfo(nome)["ip"] = m.get("ip","")
                d["ifaces"][nome] = d["ifaces"].get(nome, 0) + 1
            log.info(f"[rel] {len(canonicos)} nos fisicos (de "
                     f"{sum(d['ifaces'].values())} interfaces)")
    except Exception as e:
        log.warning(f"[rel] rajant_bc_primario indisponivel: {e}")

    # Info
    for r in prom.instant(F(f"max_over_time(rajant_bc_info[{jan}])"), t):
        m = r["metric"]; nome = m.get("bc","?")
        if canonicos is not None and nome not in canonicos:
            continue
        b = bcinfo(nome)
        b.update(ip=b.get("ip") or m.get("ip",""), modelo=m.get("modelo",""),
                 firmware=m.get("firmware",""), grupos=m.get("grupos",""))
    if canonicos is not None:
        # descarta qualquer nó que não seja primário
        for k in [k for k in d["bcs"] if k not in canonicos]:
            d["bcs"].pop(k, None)

    # ── Por BC ──
    consultas_bc = {
        "disp_pct":   F(f"avg_over_time(rajant_online[{jan}])") + " * 100",
        "cpu_med":    F(f"avg_over_time(rajant_cpu_load_pct[{jan}])"),
        "cpu_max":    F(f"max_over_time(rajant_cpu_load_pct[{jan}])"),
        "mem_min_kb": F(f"min_over_time(rajant_memoria_livre_kb[{jan}])"),
        "temp_med":   F(f"avg_over_time(rajant_temperatura_c[{jan}])"),
        "temp_max":   F(f"max_over_time(rajant_temperatura_c[{jan}])"),
        # rajant_voltagem_v não existe mais (não havia sensor de tensão na
        # BC API). O equivalente real é a carga da bateria em %.
        "bat_min":    F(f"min_over_time(rajant_bateria_pct[{jan}])"),
        "bat_med":    F(f"avg_over_time(rajant_bateria_pct[{jan}])"),
        "lat_med":    F(f"avg_over_time(rajant_ping_rtt_ms[{jan}])"),
        "lat_max":    F(f"max_over_time(rajant_ping_rtt_ms[{jan}])"),
        "uptime_d":   F(f"max_over_time(rajant_uptime_s[{jan}])") + " / 86400",
        "score_med":  F(f"avg_over_time(rajant_mesh_score[{jan}])"),
        "score_min":  F(f"min_over_time(rajant_mesh_score[{jan}])"),
        "perda_med":  F(f"avg_over_time(rajant_im_perda_pct[{jan}])"),
        "perda_max":  F(f"max_over_time(rajant_im_perda_pct[{jan}])"),
        "reboots":    F(f"max_over_time(rajant_boot_counter[{jan}])") + " - " + F(f"min_over_time(rajant_boot_counter[{jan}])"),
        "topo_chg":   F(f"sum_over_time(rajant_link_changes[{jan}])"),
        "im_drop_ps": F(f"avg_over_time(rajant_im_drop_pps[{jan}])"),
        "online_agora": F("rajant_online"),
        "peers_med":  f"avg_over_time((sum by (bc) ({F('rajant_radio_peers_ativos')}))[{jan}:5m])",
        "rf_mbps_med":f"avg_over_time((sum by (bc) ({F('rajant_radio_rx_mbps')} + {F('rajant_radio_tx_mbps')}))[{jan}:5m])",
        "rf_mbps_max":f"max_over_time((sum by (bc) ({F('rajant_radio_rx_mbps')} + {F('rajant_radio_tx_mbps')}))[{jan}:5m])",
        "eth_mbps_med":f"avg_over_time((sum by (bc) ({F('rajant_eth_rx_mbps')} + {F('rajant_eth_tx_mbps')}))[{jan}:5m])",
        "eth_mbps_max":f"max_over_time((sum by (bc) ({F('rajant_eth_rx_mbps')} + {F('rajant_eth_tx_mbps')}))[{jan}:5m])",
        "trafego_gb": f"avg_over_time((sum by (bc) ({F('rajant_radio_rx_mbps')} + {F('rajant_radio_tx_mbps')}))[{jan}:5m]) * {dur_s} / 8 / 1000",
    }
    trabalhos = [("bc", chave, q) for chave, q in consultas_bc.items()]

    # ── Por rádio (bc, radio) — canais, frequências, RF ──
    def rad(m):
        chave = (m.get("bc","?"), m.get("radio","?"))
        r = d["radios"].setdefault(chave, {"canais": set(), "freq": m.get("freq","")})
        if m.get("canal"): r["canais"].add(m["canal"])
        return r
    consultas_rad = {
        "ruido_med":  f"avg_over_time({F('rajant_radio_ruido_dbm')}[{jan}])",
        "ruido_pior": f"max_over_time({F('rajant_radio_ruido_dbm')}[{jan}])",   # dBm: maior = pior
        "ruido_melhor":f"min_over_time({F('rajant_radio_ruido_dbm')}[{jan}])",
        "txpwr":      f"max_over_time({F('rajant_radio_txpower_dbm')}[{jan}])",
        "busy_med":   f"avg_over_time({F('rajant_radio_busy_pct')}[{jan}])",
        "busy_max":   f"max_over_time({F('rajant_radio_busy_pct')}[{jan}])",
        "rxpct_med":  f"avg_over_time({F('rajant_radio_rx_pct')}[{jan}])",
        "txpct_med":  f"avg_over_time({F('rajant_radio_tx_pct')}[{jan}])",
        "rx_med":     f"avg_over_time({F('rajant_radio_rx_mbps')}[{jan}])",
        "rx_max":     f"max_over_time({F('rajant_radio_rx_mbps')}[{jan}])",
        "tx_med":     f"avg_over_time({F('rajant_radio_tx_mbps')}[{jan}])",
        "tx_max":     f"max_over_time({F('rajant_radio_tx_mbps')}[{jan}])",
        # phy_delta/radar removidos: rajant_radio_phy_erros e
        # rajant_radio_radar_detec não existem mais (sem campo no protocolo).
        "peers_med":  f"avg_over_time({F('rajant_radio_peers_ativos')}[{jan}])",
        "good_med":   f"avg_over_time({F('rajant_radio_good_peers')}[{jan}])",
        "cli_med":    f"avg_over_time({F('rajant_radio_clientes')}[{jan}])",
    }
    trabalhos += [("rad", chave, q) for chave, q in consultas_rad.items()]

    # ── Por link (bc, radio, peer) — sinal, SNR, custo ──
    def lnk(m):
        chave = (m.get("bc","?"), m.get("radio","?"), m.get("peer","?"))
        return d["links"].setdefault(chave, {})
    consultas_lnk = {
        "snr_med":   f"avg_over_time({F('rajant_peer_snr_db')}[{jan}])",
        "snr_min":   f"min_over_time({F('rajant_peer_snr_db')}[{jan}])",
        "snr_max":   f"max_over_time({F('rajant_peer_snr_db')}[{jan}])",
        "sinal_med": f"avg_over_time({F('rajant_peer_sinal_dbm')}[{jan}])",
        "rssi_med":  f"avg_over_time({F('rajant_peer_rssi')}[{jan}])",
        "taxa_med":  f"avg_over_time({F('rajant_peer_taxa_mbps')}[{jan}])",
        "taxa_min":  f"min_over_time({F('rajant_peer_taxa_mbps')}[{jan}])",
        "custo_med": f"avg_over_time({F('rajant_peer_custo')}[{jan}])",
        "custo_min": f"min_over_time({F('rajant_peer_custo')}[{jan}])",
        "ativo_pct": f"avg_over_time({F('rajant_peer_ativo')}[{jan}]) * 100",
    }
    trabalhos += [("lnk", chave, q) for chave, q in consultas_lnk.items()]

    # ── Por porta ethernet (bc, porta) ──
    def eth(m):
        chave = (m.get("bc","?"), m.get("porta","?"))
        return d["eths"].setdefault(chave, {})
    consultas_eth = {
        "link_pct":  f"avg_over_time({F('rajant_eth_link')}[{jan}]) * 100",
        "rx_med":    f"avg_over_time({F('rajant_eth_rx_mbps')}[{jan}])",
        "rx_max":    f"max_over_time({F('rajant_eth_rx_mbps')}[{jan}])",
        "tx_med":    f"avg_over_time({F('rajant_eth_tx_mbps')}[{jan}])",
        "tx_max":    f"max_over_time({F('rajant_eth_tx_mbps')}[{jan}])",
        # rx_err/tx_err/crc/rx_drop/tx_drop/mudancas removidos: State.Wired
        # usa CommStats, que só tem rx/txBytes e rx/txPackets. Não há
        # contador de erro, CRC ou descarte por porta na BC API.
        "apt":       f"max_over_time({F('rajant_eth_apt_state')}[{jan}])",
        "peers":     f"max_over_time({F('rajant_eth_peers')}[{jan}])",
        "rx_pkts":   f"max_over_time({F('rajant_eth_rx_pacotes')}[{jan}])",
        "tx_pkts":   f"max_over_time({F('rajant_eth_tx_pacotes')}[{jan}])",
    }
    trabalhos += [("eth", chave, q) for chave, q in consultas_eth.items()]

    # ── Executa TODAS as consultas instantâneas em paralelo ──
    # Antes eram ~60 requisições HTTP em série (a razão da lentidão);
    # o Prometheus lida bem com consultas concorrentes.
    from concurrent.futures import ThreadPoolExecutor, as_completed
    t0 = time.monotonic()
    def aplica_bc(bc):
        """Não cria nós novos quando já há lista canônica — evita que
        métricas por rádio/interface reintroduzam duplicatas."""
        if canonicos is not None and bc not in canonicos:
            return None
        return bcinfo(bc)
    aplicadores = {"bc": aplica_bc, "rad": rad, "lnk": lnk, "eth": eth}
    def executa(job):
        tipo, chave, q = job
        return tipo, chave, prom.instant(q, t)
    with ThreadPoolExecutor(max_workers=10) as ex:
        futuros = {ex.submit(executa, j): j for j in trabalhos}
        for fut in as_completed(futuros):
            tipo, chave, _ = futuros[fut]
            try:
                tipo, chave, res = fut.result()
                with lock:
                    if tipo == "bc":
                        for bc, v in _r_bc(res).items():
                            alvo = aplica_bc(bc)
                            if alvo is not None:
                                alvo[chave] = round(v, 2)
                    else:
                        alvo = aplicadores[tipo]
                        for m, v in _r_labels(res):
                            alvo(m)[chave] = round(v, 2)
            except Exception as e:
                log.warning(f"[rel] consulta '{chave}' falhou: {e}")
    log.info(f"[rel] {len(trabalhos)} consultas em {time.monotonic()-t0:.1f}s (paralelo)")

    # ── Séries diárias ──
    for chave, q in {"disp_dia": f"avg({F('rajant_online')}) * 100",
                     "thr_dia":  f"sum({F('rajant_radio_rx_mbps')} + {F('rajant_radio_tx_mbps')})",
                     "lat_dia":  f"avg({F('rajant_ping_rtt_ms')})",
                     "on_dia":   f"sum({F('rajant_online')})"}.items():
        try:
            res = prom.range(q, ini_s, fim_s, "1h")
            por_dia = {}
            for ts, v in (res[0]["values"] if res else []):
                por_dia.setdefault(date.fromtimestamp(float(ts)), []).append(float(v))
            d["series"][chave] = {k: round(sum(vs)/len(vs), 2) for k, vs in por_dia.items()}
        except Exception as e:
            log.warning(f"[rel] série '{chave}' falhou: {e}")
            d["series"][chave] = {}

    # ── Eventos de queda (transições 1→0 de rajant_online) ──
    # Passo adaptativo: janelas longas com passo fino geram milhões de
    # pontos (301 BCs × 8640 amostras/mês) e derrubam o desempenho.
    # Trade-off explícito: quedas mais curtas que o passo podem não aparecer.
    if   dur_s <= 2*86400:  passo_ev = "5m"
    elif dur_s <= 10*86400: passo_ev = "15m"
    else:                   passo_ev = "30m"
    try:
        for r in prom.range(F("rajant_online"), ini_s, fim_s, passo_ev):
            bc = r["metric"].get("bc","?")
            ant, ini_q = None, None
            for ts, v in r["values"]:
                v, ts = float(v), float(ts)
                if ant == 1 and v == 0: ini_q = ts
                if ant == 0 and v == 1 and ini_q:
                    d["eventos"].append({"bc": bc,
                        "inicio": datetime.fromtimestamp(ini_q),
                        "fim": datetime.fromtimestamp(ts),
                        "dur_min": round((ts-ini_q)/60, 1)})
                    ini_q = None
                ant = v
            if ini_q:
                # Queda ainda aberta NO MOMENTO DA GERAÇÃO (fim_s já
                # limitado ao presente — nada de "futuro offline")
                d["eventos"].append({"bc": bc,
                    "inicio": datetime.fromtimestamp(ini_q), "fim": None,
                    "dur_min": round((fim_s-ini_q)/60, 1)})
    except Exception as e:
        log.warning(f"[rel] eventos falharam: {e}")
    return d

# ──────────────────────────────────────────────────────────────
# DADOS SINTÉTICOS (?demo=1) — validar layout sem Prometheus
# ──────────────────────────────────────────────────────────────
def dados_demo(dias):
    random.seed(42)
    nomes = (
        # Infraestrutura fixa
        [f"ERB-{i:02d}" for i in range(1, 13)] +
        [f"ERM-{i:03d}" for i in range(1, 41)] +          # repetidoras móveis
        ["BB-CORE-01","BB-CORE-02","BACKBONE-ANEL-N","BACKBONE-ANEL-S"] +
        # Frota (prefixos reais: CA caminhão, PA pá, PF perfuratriz,
        # TT trator, EH escavadeira)
        [f"CA-{i:04d}" for i in range(4001, 4061)] +
        [f"PA-{i:04d}" for i in range(1201, 1209)] +
        [f"PF-{i:04d}" for i in range(1801, 1809)] +
        [f"TT-{i:04d}" for i in range(5601, 5609)] +
        [f"EH-{i:04d}" for i in range(3001, 3011)] +
        ["BC-PORTARIA-01","BC-OFICINA-02","BC-BRITADOR"]
    )
    canais5 = [36, 44, 149, 153, 157, 161]
    d = {"bcs": {}, "dias": dias, "series": {}, "eventos": [],
         "radios": {}, "links": {}, "eths": {}, "ifaces": {}}
    ips = {}
    for n in nomes:
        ip = f"10.188.{random.randint(96,99)}.{random.randint(2,254)}"
        ips[n] = ip
        disp = round(random.uniform(97.5, 100.0), 2)
        if n in ("CA-4007","ERM-005","PF-1803"): disp = round(random.uniform(92,98),2)
        d["bcs"][n] = {"ip": ip,
            "modelo": random.choice(["ME4-2450R","LX5-2255B","Peregrine","Hawk","ES1"]),
            "firmware": random.choice(["11.25.0","11.24.2","11.23.1"]), "grupos": "",
            "disp_pct": disp,
            "cpu_med": round(random.uniform(8,45),1),  "cpu_max": round(random.uniform(45,95),1),
            "mem_min_kb": round(random.uniform(4e4,2.2e5)),
            "temp_med": round(random.uniform(35,55),1),"temp_max": round(random.uniform(55,78),1),
            "bat_min": round(random.uniform(45,80),1), "bat_med": round(random.uniform(80,100),1),
            "lat_med": round(random.uniform(1,9),1),   "lat_max": round(random.uniform(9,40),1),
            "uptime_d": round(random.uniform(2,120),1),
            "score_med": round(random.uniform(55,98),1),"score_min": round(random.uniform(30,55),1),
            "perda_med": round(random.uniform(0,1.2),2),"perda_max": round(random.uniform(1.2,6),2),
            "reboots": random.choice([0,0,0,0,1,2]),   "topo_chg": random.randint(0,120),
            "im_drop_ps": round(random.uniform(0,4),2),
            "peers_med": round(random.uniform(2,9),1),
            "rf_mbps_med": round(random.uniform(5,90),1),"rf_mbps_max": round(random.uniform(90,320),1),
            "eth_mbps_med": round(random.uniform(1,60),1),"eth_mbps_max": round(random.uniform(60,250),1),
            "trafego_gb": round(random.uniform(20,900),1),
            "online_agora": 0 if n in ("CA-4011","PF-1803") else 1}
        d["ifaces"][n] = random.choice([2, 3, 3])
        for rn, freq in [("wlan0","5GHz"), ("wlan1","2.4GHz")]:
            canal = random.choice(canais5) if freq=="5GHz" else random.choice([1,6,11])
            d["radios"][(n, rn)] = {"canais": {str(canal)}, "freq": freq,
                "ruido_med": round(random.uniform(-102,-88),1),
                "ruido_pior": round(random.uniform(-88,-72),1),
                "ruido_melhor": round(random.uniform(-108,-102),1),
                "txpwr": random.choice([22,25,27]),
                "busy_med": round(random.uniform(5,45),1),"busy_max": round(random.uniform(45,92),1),
                "rxpct_med": round(random.uniform(3,25),1),"txpct_med": round(random.uniform(3,25),1),
                "rx_med": round(random.uniform(2,45),1),"rx_max": round(random.uniform(45,180),1),
                "tx_med": round(random.uniform(2,45),1),"tx_max": round(random.uniform(45,180),1),
                "peers_med": round(random.uniform(1,5),1),"good_med": round(random.uniform(0.5,4),1),
                "cli_med": round(random.uniform(0,6),1)}
    for n in nomes:
        for _ in range(random.randint(2,4)):
            peer = ips[random.choice([x for x in nomes if x != n])]
            snr  = round(random.uniform(12, 48),1)
            d["links"][(n, "wlan0", peer)] = {
                "snr_med": snr, "snr_min": round(snr-random.uniform(3,10),1),
                "snr_max": round(snr+random.uniform(2,8),1),
                "sinal_med": round(-random.uniform(45,80),1),
                "rssi_med": round(random.uniform(15,50),1),
                "taxa_med": round(random.uniform(50,780),1),
                "taxa_min": round(random.uniform(6,50),1),
                "custo_med": round(random.uniform(80,900)),
                "custo_min": round(random.uniform(40,80)),
                "ativo_pct": round(random.uniform(85,100),1)}
        d["eths"][(n, "eth0")] = {"link_pct": round(random.uniform(95,100),1),
            "rx_med": round(random.uniform(1,40),1),"rx_max": round(random.uniform(40,200),1),
            "tx_med": round(random.uniform(1,40),1),"tx_max": round(random.uniform(40,200),1),
            "apt": random.choice([0,1,3]), "peers": random.randint(0,3),
            "rx_pkts": random.randint(1e5,9e6), "tx_pkts": random.randint(1e5,9e6)}
        if random.random() < 0.3:
            d["eths"][(n, "eth1")] = dict(d["eths"][(n, "eth0")], link_pct=100.0)
    for chave, base, var in [("disp_dia",99.6,0.5),("thr_dia",850,250),
                             ("lat_dia",4.5,2.0),("on_dia",len(nomes)-1,1.5)]:
        d["series"][chave] = {dia: round(base+random.uniform(-var,var),2) for dia in dias}
    for bc, dur in [("CA-4007",95),("ERM-005",240),("CA-4011",55),("BC-BRITADOR",12),
                ("PF-1803",180),("TT-5602",25),("EH-3004",42)]:
        t0 = datetime.combine(random.choice(dias), datetime.min.time()) + timedelta(hours=random.randint(6,20))
        d["eventos"].append({"bc": bc, "inicio": t0,
                             "fim": None if bc in ("CA-4011","PF-1803") else t0+timedelta(minutes=dur),
                             "dur_min": dur})
    return d

# ──────────────────────────────────────────────────────────────
# ESTILOS DO EXCEL
# ──────────────────────────────────────────────────────────────
F_TIT  = Font(name="Arial", size=15, bold=True, color="FFFFFF")
F_SUB  = Font(name="Arial", size=9,  color="D9E2F3")
F_H    = Font(name="Arial", size=9,  bold=True, color="FFFFFF")
F_TXT  = Font(name="Arial", size=9)
F_TXTB = Font(name="Arial", size=9,  bold=True)
F_PEQ  = Font(name="Arial", size=8,  italic=True, color="808080")
C_VERDE="C6EFCE"; C_VERDE_F="006100"; C_AMAR="FFEB9C"; C_AMAR_F="9C6500"
C_VERM ="FFC7CE"; C_VERM_F ="9C0006"
FILL_TIT = PatternFill("solid", fgColor="1F4E79")
FILL_H   = PatternFill("solid", fgColor="2E75B6")
FILL_ZEB = PatternFill("solid", fgColor="F2F2F2")
FILL_OK  = PatternFill("solid", fgColor=C_VERDE)
FILL_WARN= PatternFill("solid", fgColor=C_AMAR)
FILL_CRIT= PatternFill("solid", fgColor=C_VERM)
BORDA = Border(*[Side(style="thin", color="BFBFBF")]*4)

def _cab(ws, titulo, subtitulo, ncols):
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncols)
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncols)
    c = ws.cell(1,1,titulo); c.font=F_TIT; c.fill=FILL_TIT
    c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    c2 = ws.cell(2,1,subtitulo); c2.font=F_SUB; c2.fill=FILL_TIT
    c2.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    for col in range(1, ncols+1):
        ws.cell(1,col).fill = FILL_TIT; ws.cell(2,col).fill = FILL_TIT
    ws.row_dimensions[1].height = 24; ws.row_dimensions[2].height = 13

def _th(ws, linha, textos, larguras=None):
    for j, txt in enumerate(textos, start=1):
        c = ws.cell(linha, j, txt)
        c.font=F_H; c.fill=FILL_H; c.border=BORDA
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    if larguras:
        for j, w in enumerate(larguras, start=1):
            ws.column_dimensions[get_column_letter(j)].width = w
    ws.row_dimensions[linha].height = 24

def _td(ws, linha, valores, zebra=False, estilo=True):
    if not estilo:
        # Modo rápido p/ abas com milhares de linhas: só valor + fonte.
        # Borda/zebra/alinhamento por célula custam caro no openpyxl.
        for j, v in enumerate(valores, start=1):
            c = ws.cell(linha, j, v); c.font = F_TXT
        return linha + 1
    for j, v in enumerate(valores, start=1):
        c = ws.cell(linha, j, v)
        c.font=F_TXT; c.border=BORDA
        if zebra: c.fill = FILL_ZEB
        c.alignment = Alignment(horizontal="center" if j > 1 else "left",
                                vertical="center")
    return linha + 1

def _semaforo(cel, valor, limite, invertido=False, tol_pct=5):
    """Verde=dentro | Amarelo=perto do limite | Vermelho=fora.
    invertido=True: menor é melhor (CPU, temp, latência, ruído)."""
    if valor is None: return
    folga = abs(limite) * tol_pct / 100.0
    ok    = (valor <= limite) if invertido else (valor >= limite)
    quase = (valor <= limite + folga) if invertido else (valor >= limite - folga)
    if ok:      cel.fill=FILL_OK;   cel.font=Font(name="Arial",size=9,bold=True,color=C_VERDE_F)
    elif quase: cel.fill=FILL_WARN; cel.font=Font(name="Arial",size=9,bold=True,color=C_AMAR_F)
    else:       cel.fill=FILL_CRIT; cel.font=Font(name="Arial",size=9,bold=True,color=C_VERM_F)

def _rodape_formulas(ws, lin, lin0, colunas, rotulo="MÉDIA / TOTAL"):
    """colunas: {idx: 'AVG'|'SUM'|'MAX'|'MIN'}"""
    vals = [rotulo] + [""]*(max(colunas)-1)
    for j, op in colunas.items():
        L = get_column_letter(j)
        if op == "AVG": vals[j-1] = f"=ROUND(AVERAGE({L}{lin0}:{L}{lin-1}),2)"
        else:           vals[j-1] = f"={op}({L}{lin0}:{L}{lin-1})"
    _td(ws, lin, vals)
    for j in range(1, max(colunas)+1): ws.cell(lin, j).font = F_TXTB
    return lin + 1

# ──────────────────────────────────────────────────────────────
# ABAS
# ──────────────────────────────────────────────────────────────
def aba_resumo(wb, d, cfg, periodo_txt):
    ws = wb.active; ws.title = "Resumo Executivo"
    _cab(ws, "RELATÓRIO DE REDE — RESUMO EXECUTIVO",
         f"Período: {periodo_txt} | Gerado em {datetime.now():%d/%m/%Y %H:%M} | Fonte: Prometheus", 7)
    meta_g = cfg.getfloat("relatorio","meta_geral")
    meta_m = cfg.getfloat("relatorio","meta_mesh")
    meta_b = cfg.getfloat("relatorio","meta_backbone")
    bcs = d["bcs"]
    def media(cat=None):
        sel = [b["disp_pct"] for b in bcs.values()
               if b.get("disp_pct") is not None and (cat is None or b.get("cat")==cat)]
        return round(sum(sel)/len(sel), 2) if sel else None
    online = sum(1 for b in bcs.values() if b.get("online_agora")==1)
    total  = len(bcs)
    quedas = len(d["eventos"])
    t_ind  = round(sum(e["dur_min"] for e in d["eventos"])/60, 1)
    lin = 4
    _th(ws, lin, ["Indicador","Valor","Meta/Ref","Status","Unid.","",""],
        [34,13,11,9,9,4,30]); lin += 1
    for nome, val, meta, unid, inv in [
            ("Disponibilidade Geral da Malha", media(),           meta_g, "%", False),
            ("Disponibilidade Backbone",       media("Backbone"), meta_b, "%", False),
            ("Disponibilidade ERBs",           media("ERB"),      meta_m, "%", False),
            ("Disponibilidade ERMs",           media("ERM"),      meta_m, "%", False),
            ("Disponibilidade Móveis",         media("Móvel"),    meta_m, "%", False),
            ("BreadCrumbs Online (na geração)",online,            total,  f"de {total}", False),
            ("Quedas registradas",             quedas,            0,      "eventos", True),
            ("Tempo indisponível acumulado",   t_ind,             0,      "horas",   True)]:
        _td(ws, lin, [nome, val if val is not None else "s/ dados", meta, "", unid])
        ws.cell(lin,1).font = F_TXTB
        ws.cell(lin,1).alignment = Alignment(horizontal="left", vertical="center", indent=1)
        if val is not None and isinstance(meta,(int,float)):
            ok = (val <= meta) if inv else (val >= meta)
            st = ws.cell(lin,4); st.value = "●"
            st.fill = FILL_OK if ok else FILL_CRIT
            st.font = Font(name="Arial", size=11, bold=True,
                           color=C_VERDE_F if ok else C_VERM_F)
            st.alignment = Alignment(horizontal="center")
        lin += 1
    lin += 1
    ws.cell(lin,1,"PIORES DISPONIBILIDADES DO PERÍODO").font = F_TXTB; lin += 1
    _th(ws, lin, ["BreadCrumb","Categoria","Disponib. %","Quedas","T. indisp (min)","",""]); lin += 1
    q_bc = {}
    for e in d["eventos"]: q_bc.setdefault(e["bc"],[]).append(e["dur_min"])
    piores = sorted([(n,b) for n,b in bcs.items() if b.get("disp_pct") is not None],
                    key=lambda x: x[1]["disp_pct"])[:10]
    for n, b in piores:
        qs = q_bc.get(n,[])
        _td(ws, lin, [n, b.get("cat","—"), b["disp_pct"], len(qs), round(sum(qs),1)])
        _semaforo(ws.cell(lin,3), b["disp_pct"], meta_m, tol_pct=0.5)
        lin += 1
    lin += 1
    ws.cell(lin,1,"Legenda: ● verde = meta atingida | vermelho = fora. "
                  "Metas em config.ini [relatorio].").font = F_PEQ
    ws.freeze_panes = "A5"; ws.sheet_view.showGridLines = False

def aba_disponibilidade(wb, d, cfg):
    ws = wb.create_sheet("Disponibilidade")
    meta = cfg.getfloat("relatorio","meta_mesh")
    _cab(ws, "DISPONIBILIDADE POR BREADCRUMB",
         "% do tempo online no período | quedas por transição online→offline (5 min)", 9)
    lin = 4
    _th(ws, lin, ["BreadCrumb","Categoria","IP","Disponib. %","Quedas","T. indisp (min)",
                  "Maior queda (min)","Uptime (dias)","Reboots"],
        [24,11,14,11,8,13,14,12,9]); lin += 1
    q_bc = {}
    for e in d["eventos"]: q_bc.setdefault(e["bc"],[]).append(e["dur_min"])
    lin0 = lin
    for i,(n,b) in enumerate(sorted(d["bcs"].items(), key=lambda x: x[1].get("disp_pct") or 0)):
        qs = q_bc.get(n,[])
        _td(ws, lin, [n, b.get("cat","—"), b.get("ip",""), b.get("disp_pct"),
                      len(qs), round(sum(qs),1), max(qs) if qs else 0,
                      b.get("uptime_d"), b.get("reboots")], zebra=(i%2==1))
        if b.get("disp_pct") is not None:
            _semaforo(ws.cell(lin,4), b["disp_pct"], meta, tol_pct=0.5)
        if (b.get("reboots") or 0) > 0: ws.cell(lin,9).fill = FILL_WARN
        lin += 1
    _rodape_formulas(ws, lin, lin0, {4:"AVG",5:"SUM",6:"SUM"}, "MÉDIA GERAL")
    ws.freeze_panes = "A5"; ws.sheet_view.showGridLines = False

def aba_serie_diaria(wb, d, cfg):
    ws = wb.create_sheet("Série Diária")
    meta = cfg.getfloat("relatorio","meta_geral")
    _cab(ws, "EVOLUÇÃO DIÁRIA", "Médias por dia — disponibilidade, throughput, latência", 6)
    lin = 4
    _th(ws, lin, ["Dia","Disponib. %","Meta %","BCs online (méd)",
                  "Throughput malha (Mbps)","Latência méd (ms)"],
        [12,12,9,14,18,14]); lin += 1
    lin0 = lin
    for dia in d["dias"]:
        _td(ws, lin, [dia.strftime("%d/%m/%Y"),
                      d["series"].get("disp_dia",{}).get(dia), meta,
                      d["series"].get("on_dia",{}).get(dia),
                      d["series"].get("thr_dia",{}).get(dia),
                      d["series"].get("lat_dia",{}).get(dia)])
        v = d["series"].get("disp_dia",{}).get(dia)
        if v is not None: _semaforo(ws.cell(lin,2), v, meta, tol_pct=0.5)
        lin += 1
    _rodape_formulas(ws, lin, lin0, {2:"AVG",4:"AVG",5:"AVG",6:"AVG"})
    ws.freeze_panes = "A5"; ws.sheet_view.showGridLines = False

def aba_throughput(wb, d, cfg):
    ws = wb.create_sheet("Throughput")
    _cab(ws, "THROUGHPUT E TRÁFEGO POR BREADCRUMB",
         "RF = soma dos rádios | ETH = portas ethernet | Tráfego ≈ média Mbps × duração", 9)
    lin = 4
    _th(ws, lin, ["BreadCrumb","Categoria","RF méd (Mbps)","RF máx (Mbps)","ETH méd (Mbps)",
                  "ETH máx (Mbps)","Tráfego total (GB)","Perda méd %","Perda máx %"],
        [24,11,12,12,12,12,14,11,11]); lin += 1
    lin0 = lin
    for i,(n,b) in enumerate(sorted(d["bcs"].items(),
                                    key=lambda x: -(x[1].get("trafego_gb") or 0))):
        _td(ws, lin, [n, b.get("cat","—"), b.get("rf_mbps_med"), b.get("rf_mbps_max"),
                      b.get("eth_mbps_med"), b.get("eth_mbps_max"), b.get("trafego_gb"),
                      b.get("perda_med"), b.get("perda_max")], zebra=(i%2==1))
        if (b.get("perda_med") or 0) > 1: ws.cell(lin,8).fill = FILL_WARN
        if (b.get("perda_max") or 0) > 5: ws.cell(lin,9).fill = FILL_CRIT
        lin += 1
    _rodape_formulas(ws, lin, lin0,
                     {3:"AVG",4:"MAX",5:"AVG",6:"MAX",7:"SUM",8:"AVG",9:"MAX"})
    ws.freeze_panes = "A5"; ws.sheet_view.showGridLines = False

def aba_canais_rf(wb, d, cfg):
    ws = wb.create_sheet("Canais & RF")
    lim_ruido = cfg.getfloat("relatorio","limite_ruido")
    _cab(ws, "CANAIS, FREQUÊNCIAS E OCUPAÇÃO DE RF — POR RÁDIO",
         f"Ruído: bom ≤ -100 dBm | degradado > {lim_ruido:.0f} dBm | "
         "Busy% = tempo de canal ocupado", 19)
    lin = 4
    _th(ws, lin, ["BreadCrumb","Rádio","Banda","Canal(is)","Freq. central (MHz)",
                  "TX power (dBm)","Ruído méd (dBm)","Ruído pior (dBm)","Ruído melhor (dBm)",
                  "Busy méd %","Busy máx %","RX canal %","TX canal %",
                  "RX méd (Mbps)","TX méd (Mbps)","Pico RX/TX (Mbps)",
                  "Peers ativos (méd)","Clientes WiFi (méd)"],
        [22,9,8,9,13,10,11,11,11,9,9,9,9,11,11,13,12,12]); lin += 1
    lin0 = lin
    for i, ((bc, rn), r) in enumerate(sorted(d["radios"].items())):
        canais = sorted(r.get("canais", set()), key=lambda x: int(x) if str(x).isdigit() else 0)
        mhz = " / ".join(str(canal_para_mhz(c)) for c in canais if canal_para_mhz(c))
        pico = max(r.get("rx_max") or 0, r.get("tx_max") or 0)
        _td(ws, lin, [bc, rn, r.get("freq",""), " / ".join(canais), mhz,
                      r.get("txpwr"), r.get("ruido_med"), r.get("ruido_pior"),
                      r.get("ruido_melhor"), r.get("busy_med"), r.get("busy_max"),
                      r.get("rxpct_med"), r.get("txpct_med"),
                      r.get("rx_med"), r.get("tx_med"), pico or None,
                      r.get("peers_med"), r.get("cli_med")],
            zebra=(i%2==1))
        if r.get("ruido_med")  is not None: _semaforo(ws.cell(lin,7), r["ruido_med"],  lim_ruido, invertido=True)
        if r.get("ruido_pior") is not None: _semaforo(ws.cell(lin,8), r["ruido_pior"], lim_ruido, invertido=True)
        if r.get("busy_max")   is not None: _semaforo(ws.cell(lin,11), r["busy_max"],  70, invertido=True, tol_pct=15)
        lin += 1
    _rodape_formulas(ws, lin, lin0, {7:"AVG",10:"AVG",11:"MAX",14:"AVG",15:"AVG",17:"AVG"})
    lin += 1
    ws.cell(lin,1,"Freq. central: 2,4 GHz → 2407+5·canal (ch14=2484) | 5 GHz → 5000+5·canal. "
                  "As colunas 'Erros PHY' e 'Radar' foram removidas: a BC API não "
                  "expõe contadores de PHY nem eventos de DFS/radar (não há campo "
                  "correspondente em State.Wireless).").font = F_PEQ
    ws.freeze_panes = "C5"; ws.sheet_view.showGridLines = False

def aba_espectro(wb, d, cfg):
    ws = wb.create_sheet("Espectro por Canal")
    lim_ruido = cfg.getfloat("relatorio","limite_ruido")
    _cab(ws, "OCUPAÇÃO DO ESPECTRO — VISÃO POR CANAL",
         "Agregado de todos os rádios em cada canal — identifica o canal mais poluído "
         "(alimenta o slide Mapa de Calor)", 9)
    # Agrega rádios por canal
    por_canal = {}
    for (bc, rn), r in d["radios"].items():
        for c in r.get("canais", set()):
            e = por_canal.setdefault(str(c), {"freq": r.get("freq",""), "radios": 0,
                                              "ruidos": [], "piores": [], "busys": [],
                                              "bcs": set()})
            e["radios"] += 1; e["bcs"].add(bc)
            if r.get("ruido_med")  is not None: e["ruidos"].append(r["ruido_med"])
            if r.get("ruido_pior") is not None: e["piores"].append(r["ruido_pior"])
            if r.get("busy_med")   is not None: e["busys"].append(r["busy_med"])
    lin = 4
    _th(ws, lin, ["Canal","Freq. central (MHz)","Banda","Rádios no canal","BCs distintos",
                  "Ruído méd (dBm)","Pior ruído (dBm)","Busy méd %","Situação"],
        [8,14,8,12,11,12,12,10,16]); lin += 1
    lin0 = lin
    def chave_ord(c):
        try: return int(c)
        except: return 999
    for i, c in enumerate(sorted(por_canal, key=chave_ord)):
        e = por_canal[c]
        r_med  = round(sum(e["ruidos"])/len(e["ruidos"]),1) if e["ruidos"] else None
        r_pior = max(e["piores"]) if e["piores"] else None
        b_med  = round(sum(e["busys"])/len(e["busys"]),1)   if e["busys"] else None
        if   r_pior is None:        sit = "s/ dados"
        elif r_pior > lim_ruido:    sit = "CRÍTICO"
        elif r_pior > -100:         sit = "Moderado"
        else:                       sit = "Limpo"
        _td(ws, lin, [c, canal_para_mhz(c), e["freq"], e["radios"], len(e["bcs"]),
                      r_med, r_pior, b_med, sit], zebra=(i%2==1))
        if r_med  is not None: _semaforo(ws.cell(lin,6), r_med,  lim_ruido, invertido=True)
        if r_pior is not None: _semaforo(ws.cell(lin,7), r_pior, lim_ruido, invertido=True)
        cs = ws.cell(lin,9)
        if sit == "CRÍTICO":  cs.fill=FILL_CRIT; cs.font=Font(name="Arial",size=9,bold=True,color=C_VERM_F)
        elif sit == "Moderado": cs.fill=FILL_WARN
        elif sit == "Limpo":  cs.fill=FILL_OK
        lin += 1
    lin += 1
    ws.cell(lin,1,"Leitura: limpo ≤ -100 dBm | moderado -99 a -90 | "
                  "crítico > -90 dBm. 'Pior ruído' = máximo observado no período.").font = F_PEQ
    ws.freeze_panes = "A5"; ws.sheet_view.showGridLines = False

def aba_links(wb, d):
    ws = wb.create_sheet("Links (Peers)")
    _cab(ws, "LINKS DA MALHA — SINAL, SNR, TAXA E CUSTO INSTAMESH",
         "Um registro por enlace rádio↔peer | SNR: bom > 30 dB (BCE) | custo menor = rota melhor", 13)
    ip_para_nome = {b.get("ip"): n for n, b in d["bcs"].items() if b.get("ip")}
    lin = 4
    _th(ws, lin, ["BreadCrumb","Rádio","Peer (IP)","Peer (nome)","SNR méd (dB)","SNR mín (dB)",
                  "SNR máx (dB)","Sinal méd (dBm)","RSSI méd","Taxa méd (Mbps)","Taxa mín (Mbps)",
                  "Custo méd","% tempo ativo"],
        [22,8,14,20,10,10,10,12,9,12,12,10,11]); lin += 1
    lin0 = lin
    ordenado = sorted(d["links"].items(),
                      key=lambda x: (x[1].get("snr_med") if x[1].get("snr_med") is not None else 999))
    rapido = len(ordenado) > 1500
    for i, ((bc, rn, peer), L) in enumerate(ordenado):
        _td(ws, lin, [bc, rn, peer, ip_para_nome.get(peer, "—"),
                      L.get("snr_med"), L.get("snr_min"), L.get("snr_max"),
                      L.get("sinal_med"), L.get("rssi_med"),
                      L.get("taxa_med"), L.get("taxa_min"),
                      L.get("custo_med"), L.get("ativo_pct")],
            zebra=(not rapido and i % 2 == 1), estilo=not rapido)
        if (not rapido or i < 800):
            if L.get("snr_med") is not None: _semaforo(ws.cell(lin,5), L["snr_med"], 30, tol_pct=20)
            if L.get("snr_min") is not None: _semaforo(ws.cell(lin,6), L["snr_min"], 15, tol_pct=30)
            if (L.get("ativo_pct") or 100) < 90: ws.cell(lin,13).fill = FILL_WARN
        lin += 1
    _rodape_formulas(ws, lin, lin0, {5:"AVG",6:"MIN",8:"AVG",10:"AVG",12:"AVG",13:"AVG"})
    lin += 1
    ws.cell(lin,1,"Ordenado do pior SNR para o melhor — os primeiros enlaces da lista "
                  "são os candidatos a realinhamento/site survey.").font = F_PEQ
    ws.freeze_panes = "A5"; ws.sheet_view.showGridLines = False

def aba_ethernet(wb, d):
    ws = wb.create_sheet("Ethernet")
    dur_h = max(len(d.get("dias") or [1]), 1) * 24
    _cab(ws, "PORTAS ETHERNET — LINK E THROUGHPUT",
         "Campos conforme State.Wired do bcapi | link% = tempo com APT ativo/peer", 13)
    lin = 4
    _th(ws, lin, ["BreadCrumb","Porta","Link %","RX méd (Mbps)","RX máx (Mbps)",
                  "TX méd (Mbps)","TX máx (Mbps)","RX pacotes","TX pacotes",
                  "APT","Peers","Tráfego (GB)","Obs."],
        [22,8,8,11,11,11,11,12,12,9,7,11,13]); lin += 1
    lin0 = lin
    for i, ((bc, porta), e) in enumerate(sorted(d["eths"].items())):
        gb = round(((e.get("rx_med") or 0)+(e.get("tx_med") or 0))*dur_h*3600/8/1000, 1) \
             if (e.get("rx_med") is not None) else None
        _td(ws, lin, [bc, porta, e.get("link_pct"), e.get("rx_med"), e.get("rx_max"),
                      e.get("tx_med"), e.get("tx_max"), e.get("rx_pkts"), e.get("tx_pkts"),
                      e.get("apt"), e.get("peers"), gb, ""],
            zebra=(i%2==1))
        if e.get("link_pct") is not None: _semaforo(ws.cell(lin,3), e["link_pct"], 99, tol_pct=2)
        # A BC API não expõe erros/CRC/drops por porta — nada a sinalizar aqui.
        lin += 1
    _rodape_formulas(ws, lin, lin0,
                     {3:"AVG",4:"AVG",5:"MAX",6:"AVG",7:"MAX",8:"MAX",9:"MAX",
                      11:"SUM",12:"SUM"})
    lin += 1
    ws.cell(lin,1,"A BC API (State.Wired) fornece apenas rx/txBytes, rx/txPackets, "
                  "aptState e peers por porta — não há contadores de erro, CRC ou "
                  "descarte no protocolo.").font = F_PEQ
    ws.freeze_panes = "A5"; ws.sheet_view.showGridLines = False

def aba_saude(wb, d, cfg):
    ws = wb.create_sheet("Saúde BreadCrumbs")
    lim_cpu, lim_temp = cfg.getfloat("relatorio","limite_cpu"), cfg.getfloat("relatorio","limite_temp")
    lim_lat = cfg.getfloat("relatorio","limite_latencia")
    _cab(ws, "SAÚDE DOS BREADCRUMBS",
         f"Limiares BCE: CPU < {lim_cpu:.0f}% | Temp < {lim_temp:.0f}°C | Latência < {lim_lat:.0f} ms", 14)
    lin = 4
    _th(ws, lin, ["BreadCrumb","CPU méd %","CPU máx %","Mem mín (MB)","Temp méd °C","Temp máx °C",
                  "Bateria mín %","Bateria méd %","Lat méd (ms)","Lat máx (ms)",
                  "Mesh Score méd","Mesh Score mín","Drop IM (pkt/s)","Mud. topologia"],
        [22,9,9,11,10,10,10,10,10,10,11,11,11,12]); lin += 1
    lin0 = lin
    for i,(n,b) in enumerate(sorted(d["bcs"].items())):
        mem_mb = round((b.get("mem_min_kb") or 0)/1024) or None
        _td(ws, lin, [n, b.get("cpu_med"), b.get("cpu_max"), mem_mb,
                      b.get("temp_med"), b.get("temp_max"),
                      b.get("bat_min"), b.get("bat_med"),
                      b.get("lat_med"), b.get("lat_max"),
                      b.get("score_med"), b.get("score_min"),
                      b.get("im_drop_ps"), b.get("topo_chg")], zebra=(i%2==1))
        if b.get("cpu_max")  is not None: _semaforo(ws.cell(lin,3),  b["cpu_max"],  lim_cpu,  invertido=True, tol_pct=15)
        if b.get("temp_max") is not None: _semaforo(ws.cell(lin,6),  b["temp_max"], lim_temp, invertido=True, tol_pct=10)
        if b.get("lat_med")  is not None: _semaforo(ws.cell(lin,9),  b["lat_med"],  lim_lat,  invertido=True, tol_pct=50)
        if b.get("score_med") is not None: _semaforo(ws.cell(lin,11), b["score_med"], 70, tol_pct=15)
        # Bateria: menos carga é pior, então o semáforo NÃO é invertido.
        if b.get("bat_min")  is not None: _semaforo(ws.cell(lin,7),  b["bat_min"], 30, tol_pct=30)
        lin += 1
    _rodape_formulas(ws, lin, lin0,
                     {2:"AVG",3:"MAX",4:"MIN",5:"AVG",6:"MAX",7:"MIN",8:"AVG",
                      9:"AVG",10:"MAX",11:"AVG",12:"MIN",13:"AVG",14:"SUM"})
    lin += 1
    ws.cell(lin,1,"As colunas de tensão (V) foram substituídas por carga de bateria (%): "
                  "a BC API não expõe tensão de entrada — não existe bloco de sensores "
                  "no protocolo. O dado real é State.Battery.capacityPercent, e só "
                  "aparece em modelos com bateria. CPU é derivada de System.idle.").font = F_PEQ
    ws.freeze_panes = "A5"; ws.sheet_view.showGridLines = False

def aba_rankings(wb, d):
    ws = wb.create_sheet("Rankings")
    _cab(ws, "RANKINGS DO PERÍODO", "Top 10 em cada dimensão", 12)
    bcs = d["bcs"]
    def top(chave, inverso=False, n=10):
        itens = [(k, v.get(chave)) for k, v in bcs.items() if v.get(chave) is not None]
        return sorted(itens, key=lambda x: (x[1] if inverso else -x[1]))[:n]
    pior_snr = sorted([(f"{bc} → {peer}", L.get("snr_med"))
                       for (bc,_,peer), L in d["links"].items()
                       if L.get("snr_med") is not None], key=lambda x: x[1])[:10]
    grupos = [
        ("TOP 10 — MAIOR TRÁFEGO (GB)",          top("trafego_gb")),
        ("TOP 10 — MAIS VIZINHOS (peers méd)",   top("peers_med")),
        ("TOP 10 — INSTABILIDADE (mud. topo)",   top("topo_chg")),
        ("TOP 10 — PIOR MESH SCORE",             top("score_med", inverso=True)),
        ("TOP 10 — MAIOR LATÊNCIA (ms méd)",     top("lat_med")),
        ("PIORES LINKS — SNR MÉDIO (dB)",        pior_snr),
    ]
    col0, lin_base = 1, 4
    for gi, (titulo, itens) in enumerate(grupos):
        col  = col0 + (gi % 3) * 4
        lin  = lin_base + (gi // 3) * 14
        ws.cell(lin, col, titulo).font = F_TXTB
        lin += 1
        for j, txt in enumerate(["#","Item","Valor"]):
            c = ws.cell(lin, col+j, txt)
            c.font=F_H; c.fill=FILL_H; c.border=BORDA
            c.alignment = Alignment(horizontal="center")
        ws.column_dimensions[get_column_letter(col)].width   = 5
        ws.column_dimensions[get_column_letter(col+1)].width = 30
        ws.column_dimensions[get_column_letter(col+2)].width = 11
        lin += 1
        for i, (nome, val) in enumerate(itens):
            for j, v in enumerate([i+1, nome, val]):
                c = ws.cell(lin, col+j, v)
                c.font=F_TXT; c.border=BORDA
                c.alignment = Alignment(horizontal="center" if j != 1 else "left")
            lin += 1
    ws.sheet_view.showGridLines = False

def aba_categorias(wb, d, cfg):
    ws = wb.create_sheet("Categorias")
    meta = cfg.getfloat("relatorio","meta_mesh")
    _cab(ws, "VISÃO POR CATEGORIA — ERB / ERM / BACKBONE / MÓVEL",
         "Classificação por padrão de nome + grupos do BC Commander (config.ini)", 9)
    cats = {}
    for n, b in d["bcs"].items(): cats.setdefault(b.get("cat","Outros"), []).append(b)
    lin = 4
    _th(ws, lin, ["Categoria","Qtd","Online","Disponib. média %","Pior disponib. %",
                  "Tráfego total (GB)","CPU máx %","Temp máx °C","Pior latência (ms)"],
        [15,7,8,15,14,15,10,11,13]); lin += 1
    ordem = ["Backbone","ERB","ERM","Móvel","Outros"]
    for cat in [c for c in ordem if c in cats] + [c for c in cats if c not in ordem]:
        bs = cats[cat]
        disp = [b["disp_pct"] for b in bs if b.get("disp_pct") is not None]
        _td(ws, lin, [cat, len(bs), sum(1 for b in bs if b.get("online_agora")==1),
                      round(sum(disp)/len(disp),2) if disp else None,
                      min(disp) if disp else None,
                      round(sum(b.get("trafego_gb") or 0 for b in bs),1),
                      max((b.get("cpu_max") or 0) for b in bs),
                      max((b.get("temp_max") or 0) for b in bs),
                      max((b.get("lat_max") or 0) for b in bs)])
        ws.cell(lin,1).font = F_TXTB
        if disp:
            _semaforo(ws.cell(lin,4), round(sum(disp)/len(disp),2), meta, tol_pct=0.5)
            _semaforo(ws.cell(lin,5), min(disp), meta, tol_pct=0.5)
        lin += 1
    ws.sheet_view.showGridLines = False

def aba_eventos(wb, d):
    ws = wb.create_sheet("Eventos")
    _cab(ws, "EVENTOS DE INDISPONIBILIDADE",
         "Quedas por transição online→offline (resolução 5 min)", 6)
    lin = 4
    _th(ws, lin, ["BreadCrumb","Início","Fim","Duração (min)","Duração (h)","Situação"],
        [24,17,17,12,10,13]); lin += 1
    lin0 = lin
    for i, e in enumerate(sorted(d["eventos"], key=lambda x: -x["dur_min"])):
        aberto = e["fim"] is None
        _td(ws, lin, [e["bc"], e["inicio"].strftime("%d/%m %H:%M"),
                      e["fim"].strftime("%d/%m %H:%M") if e["fim"] else "—",
                      e["dur_min"], f"=ROUND(D{lin}/60,2)",
                      "EM ABERTO" if aberto else "Normalizado"], zebra=(i%2==1))
        if aberto:
            ws.cell(lin,6).fill = FILL_CRIT
            ws.cell(lin,6).font = Font(name="Arial",size=9,bold=True,color=C_VERM_F)
        elif e["dur_min"] >= 60: ws.cell(lin,4).fill = FILL_WARN
        lin += 1
    if d["eventos"]:
        _td(ws, lin, ["TOTAL","","",f"=SUM(D{lin0}:D{lin-1})",f"=ROUND(D{lin}/60,2)",""])
        for j in range(1,7): ws.cell(lin,j).font = F_TXTB
    else:
        _td(ws, lin, ["Nenhuma queda registrada no período","","","","",""])
    ws.freeze_panes = "A5"; ws.sheet_view.showGridLines = False


def aba_inventario(wb, d):
    ws = wb.create_sheet("Inventário")
    _cab(ws, "INVENTÁRIO DA MALHA", "Ativos descobertos no período", 6)
    bcs = d["bcs"]; lin = 4
    total  = len(bcs)
    online = sum(1 for b in bcs.values() if b.get("online_agora")==1)
    _th(ws, lin, ["Resumo","Valor","","Modelo / Firmware","Qtd",""],
        [26,10,4,22,8,14]); lin += 1
    resumo = [("Total de BreadCrumbs", total), ("Online (na geração)", online),
              ("Offline (na geração)", total-online),
              ("IPs/interfaces monitorados", sum(d.get("ifaces", {}).values()) or total),
              ("Portas ethernet monitoradas", len(d["eths"])),
              ("Rádios monitorados", len(d["radios"])),
              ("Enlaces (links) monitorados", len(d["links"]))]
    modelos, firmwares = {}, {}
    for b in bcs.values():
        modelos[b.get("modelo") or "?"] = modelos.get(b.get("modelo") or "?",0)+1
        firmwares[f'fw {b.get("firmware") or "?"}'] = firmwares.get(f'fw {b.get("firmware") or "?"}',0)+1
    lista_mf = (sorted(modelos.items(), key=lambda x:-x[1]) +
                sorted(firmwares.items(), key=lambda x:-x[1]))
    for i in range(max(len(resumo), len(lista_mf))):
        vals = ["","","","","",""]
        if i < len(resumo):   vals[0], vals[1] = resumo[i]
        if i < len(lista_mf): vals[3], vals[4] = lista_mf[i]
        _td(ws, lin, vals)
        if i < len(resumo):
            ws.cell(lin,1).font = F_TXTB
            if resumo[i][0].startswith("Offline") and resumo[i][1] > 0:
                ws.cell(lin,2).fill = FILL_WARN
        lin += 1
    lin += 1
    if all((b.get("modelo") or "?") == "?" for b in bcs.values()):
        c = ws.cell(lin, 1, "Modelo/firmware/IP indisponíveis: a métrica rajant_bc_info "
                            "só existe a partir do exporter v2.3 — verifique se o Prometheus "
                            "está coletando do exporter novo.")
        c.font = Font(name="Arial", size=9, bold=True, color=C_AMAR_F)
        ws.cell(lin, 1).fill = FILL_WARN
        lin += 1
    ws.cell(lin,1,"LISTA COMPLETA").font = F_TXTB; lin += 1
    _th(ws, lin, ["BreadCrumb","IP","Categoria","Modelo","Firmware","Status"]); lin += 1
    for i,(n,b) in enumerate(sorted(bcs.items())):
        on = b.get("online_agora")
        _td(ws, lin, [n, b.get("ip",""), b.get("cat","—"), b.get("modelo",""),
                      b.get("firmware",""),
                      "ONLINE" if on==1 else ("OFFLINE" if on==0 else "—")], zebra=(i%2==1))
        if on == 0:
            ws.cell(lin,6).fill = FILL_CRIT
            ws.cell(lin,6).font = Font(name="Arial",size=9,bold=True,color=C_VERM_F)
        lin += 1
    ws.freeze_panes = "A5"; ws.sheet_view.showGridLines = False

# ──────────────────────────────────────────────────────────────
# MONTAGEM DO WORKBOOK
# ──────────────────────────────────────────────────────────────
def _config_impressao(ws, repetir_ate_linha=4):
    """Paisagem + ajuste à largura da página + repete cabeçalho ao imprimir.
    Afeta só a exportação para PDF/impressão; não muda o uso em planilha."""
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = f"1:{repetir_ate_linha}"

def obter_dados(cfg, ini_d, fim_d, demo=False):
    """Coleta + classificação + filtros — base comum do Excel e do PPT."""
    dias   = [ini_d + timedelta(days=i) for i in range((fim_d - ini_d).days + 1)]
    ini_dt = datetime.combine(ini_d, datetime.min.time())
    fim_dt = datetime.combine(fim_d + timedelta(days=1), datetime.min.time())

    if demo:
        d = dados_demo(dias)
    else:
        prom = Prometheus(cfg.get("relatorio","prometheus_url"))
        d = coletar_dados(prom, cfg, ini_dt, fim_dt, dias)
    if not d["bcs"]:
        raise RuntimeError("Nenhum BC encontrado no período — verifique o Prometheus "
                           "e a retenção de dados (--storage.tsdb.retention.time).")

    cls = classificador_categorias(cfg)
    for n, b in d["bcs"].items():
        b["cat"] = cls(n, b.get("grupos",""))

    # ── Filtro de rádios/links inativos — COM GUARDA ──
    # Descartar "rádio sem ruído/tráfego" só faz sentido quando essas
    # métricas existem na base. Se o exporter não as publica (ou o
    # Prometheus não as tem no período), o filtro apagaria a rede inteira.
    # Regra: só filtra se ao menos 40% dos rádios tiverem algum sinal de
    # vida — aí o silêncio dos demais é informativo, não ausência de dado.
    def _vivo_radio(r):
        return (r.get("ruido_med") is not None
                or (r.get("rx_med") or 0) > 0 or (r.get("tx_med") or 0) > 0
                or (r.get("busy_med") or 0) > 0
                or (r.get("peers_med") or 0) > 0
                or r.get("txpwr") is not None)
    if d["radios"]:
        vivos = sum(1 for r in d["radios"].values() if _vivo_radio(r))
        cobertura = vivos / len(d["radios"])
        if cobertura >= 0.40:
            antes = len(d["radios"])
            d["radios"] = {k: r for k, r in d["radios"].items() if _vivo_radio(r)}
            if antes != len(d["radios"]):
                log.info(f"[rel] {antes - len(d['radios'])} radios inativos omitidos "
                         f"(de {antes})")
        else:
            log.warning(f"[rel] filtro de radios DESATIVADO: so {cobertura:.0%} tem "
                        f"metricas de RF — mantendo todos os {len(d['radios'])} radios. "
                        f"Verifique se o Prometheus coleta rajant_radio_*.")

    def _vivo_link(L):
        return ((L.get("ativo_pct") or 0) > 0 or L.get("snr_med") is not None
                or (L.get("taxa_med") or 0) > 0 or L.get("custo_med") is not None)
    if d["links"]:
        vivos_l = sum(1 for L in d["links"].values() if _vivo_link(L))
        if vivos_l / len(d["links"]) >= 0.40:
            d["links"] = {k: L for k, L in d["links"].items() if _vivo_link(L)}
        else:
            log.warning(f"[rel] filtro de links DESATIVADO: cobertura baixa de "
                        f"metricas rajant_peer_* — mantendo todos os {len(d['links'])}.")
    return d


def montar_relatorio(cfg, ini_d, fim_d, demo=False):
    """Gera o workbook e retorna (bytes, nome_arquivo)."""
    periodo_txt = f"{ini_d:%d/%m/%Y} a {fim_d:%d/%m/%Y}"
    d = obter_dados(cfg, ini_d, fim_d, demo)

    wb = Workbook()
    aba_resumo(wb, d, cfg, periodo_txt)
    aba_disponibilidade(wb, d, cfg)
    aba_serie_diaria(wb, d, cfg)
    aba_throughput(wb, d, cfg)
    aba_canais_rf(wb, d, cfg)
    aba_espectro(wb, d, cfg)
    aba_links(wb, d)
    aba_ethernet(wb, d)
    aba_saude(wb, d, cfg)
    aba_rankings(wb, d)
    aba_categorias(wb, d, cfg)
    aba_eventos(wb, d)
    aba_inventario(wb, d)

    for ws in wb.worksheets:
        try:
            from openpyxl.worksheet.properties import PageSetupProperties
            if ws.sheet_properties.pageSetUpPr is None:
                ws.sheet_properties.pageSetUpPr = PageSetupProperties()
            _config_impressao(ws)
        except Exception:
            pass

    buf = io.BytesIO(); wb.save(buf)
    sufixo = "_DEMO" if demo else ""
    nome = f"Relatorio_Rede_{ini_d:%Y%m%d}_{fim_d:%Y%m%d}{sufixo}.xlsx"
    return buf.getvalue(), nome

# ──────────────────────────────────────────────────────────────
# PREENCHIMENTO DO RELATÓRIO PPT (template do usuário)
# Preserva design, menus e hyperlinks: só troca texto de valores.
# Template: [relatorio] template_ppt (padrão: Relatorio_Semanal_Rede.pptx
# na pasta do executável).
# ──────────────────────────────────────────────────────────────
PPT_VERDE, PPT_AMBAR, PPT_VERM = "4CAF50", "FFC107", "F44336"

def _ppt_set(tf, novo):
    """Troca o texto preservando a formatação: escreve no 1º run do
    parágrafo que contém '_' e esvazia os demais runs desse parágrafo."""
    for p in tf.paragraphs:
        if "_" in p.text:
            if not p.runs: continue
            p.runs[0].text = str(novo)
            for r in p.runs[1:]: r.text = ""
            return True
    # fallback: primeiro parágrafo com texto
    for p in tf.paragraphs:
        if p.runs:
            p.runs[0].text = str(novo)
            for r in p.runs[1:]: r.text = ""
            return True
    return False


def _ppt_fmt(v, casas=2, sufixo=""):
    if v is None: return "n/d"
    s = f"{v:.{casas}f}".replace(".", ",")
    return s + sufixo

def _ppt_status(run, valor, meta, invertido=False):
    """Colore o ● conforme a meta."""
    from pptx.dml.color import RGBColor
    if valor is None: return
    ok    = (valor <= meta) if invertido else (valor >= meta)
    quase = (valor <= meta*1.05) if invertido else (valor >= meta - 0.5)
    cor = PPT_VERDE if ok else (PPT_AMBAR if quase else PPT_VERM)
    run.font.color.rgb = RGBColor.from_string(cor)

def _tabela_por_cabecalho(slide, texto_1a_celula):
    for sh in slide.shapes:
        if sh.has_table and sh.table.cell(0,0).text.strip().lower().startswith(texto_1a_celula.lower()):
            return sh.table
    return None

def _txt_por_conteudo(slide, contem):
    for sh in slide.shapes:
        if sh.has_text_frame and contem in sh.text_frame.text:
            return sh
    return None

def _txt_valor_do_cartao(slide, rotulo):
    """Acha a caixa de VALOR de um cartão: a caixa com '_' mais próxima
    abaixo/ao lado da caixa do rótulo (layout de cartões do slide 6/13)."""
    alvo = None
    for sh in slide.shapes:
        if sh.has_text_frame and sh.text_frame.text.strip().upper().startswith(rotulo.upper()):
            alvo = sh; break
    if not alvo: return None
    melhor, melhor_d = None, None
    for sh in slide.shapes:
        if sh is alvo or not sh.has_text_frame: continue
        if "_" not in sh.text_frame.text: continue
        dx = abs(sh.left - alvo.left); dy = sh.top - alvo.top
        if dy < 0 or dx > 914400:  # mesmo cartão: abaixo, mesma coluna (±1")
            continue
        dist = dy + dx
        if melhor_d is None or dist < melhor_d:
            melhor, melhor_d = sh, dist
    return melhor

def _titulo_do_slide(s):
    """Título do slide: a caixa de texto mais alta que não é botão.

    Existe porque olhar "qualquer texto do slide" confunde título com
    prosa. O botão ◂ MENU fica em 0,28" e o título em 0,32" — mais alto
    que o título —, então ele precisa sair da disputa pelo prefixo.
    """
    cands = []
    for sh in s.shapes:
        if not sh.has_text_frame or sh.top is None: continue
        t = (sh.text_frame.text or "").strip()
        if not t or t[0] in "◂▤": continue
        cands.append((sh.top, t))
    return min(cands)[1].replace("\n", " ") if cands else ""


def _slide_por_titulo(p, contem):
    """Localiza um slide pelo texto do título (robusto a reordenações).
    Ignora o slide de Navegação: os botões do menu contêm os títulos de
    TODAS as seções e causariam falsos positivos."""
    alvo = contem.lower()
    for i, s in enumerate(p.slides):
        textos = [sh.text_frame.text for sh in s.shapes if sh.has_text_frame]
        if any(t.strip().lower().startswith("navega") for t in textos):
            continue
        if any(alvo in t.lower() for t in textos):
            return i, s
    return None, None


# Título ("5. Site Survey — Rota Percorrida — 5.8 GHz") e subtítulo
# ("Banda: ___ | Data: ___ | ...", "Base: survey passivo ...", "Requisito:
# ..."). Nenhum dos dois pode ser sobrescrito ao preencher os números.
_RE_TITULO_SURVEY = re.compile(r"^\s*\d+\.\s*site\s+survey\s*[—–-]", re.I)
_PREFIXOS_SUBTITULO = ("banda:", "base:", "requisito:", "requisitos:",
                       "piso de ruído:", "piso de ruido:", "rssi previsto",
                       "sobreposição co-channel", "sobreposicao co-channel",
                       "posição de todos os breadcrumbs",
                       "posicao de todos os breadcrumbs")


def _eh_titulo_survey(txt):
    t = (txt or "").strip()
    if _RE_TITULO_SURVEY.match(t):
        return True
    return any(t.lower().startswith(p) for p in _PREFIXOS_SUBTITULO)


def remover_slides_survey(p):
    """Tira do deck TODOS os slides da secao de Site Survey.

    O survey foi descontinuado, mas o template do cliente ainda traz a
    secao inteira, e ela ficaria no arquivo em branco, parecendo
    relatorio malfeito.

    O slide de Navegacao NAO e removido — ele lista todas as secoes e
    seria confundido com um slide de survey. Os botoes que apontavam para
    a secao removida sao neutralizados.

    Devolve quantos slides sairam.
    """
    # O que decide é o TÍTULO do slide, não "qualquer texto nele". Casar
    # com o corpo levava junto slide que só CITA survey em prosa: o de
    # Links Críticos tem no subtítulo "candidatos a realinhamento / site
    # survey", e sumia do semanal inteiro por causa dessa frase.
    alvos = []
    for i, s in enumerate(p.slides):
        tit = _titulo_do_slide(s)
        if tit.strip().lower().startswith("navega"):
            continue
        if _eh_titulo_survey(tit) or "site survey" in tit.lower():
            alvos.append(i)
    if not alvos:
        return 0

    lst = p.slides._sldIdLst
    ids = list(lst)
    # De tras para frente: remover pelo indice bagunçaria os seguintes.
    for i in reversed(alvos):
        el = ids[i]
        rid = el.get("{http://schemas.openxmlformats.org/officeDocument/"
                     "2006/relationships}id")
        lst.remove(el)
        try:
            p.part.drop_rel(rid)      # senao o arquivo carrega a parte orfa
        except Exception:
            pass

    # Botao do menu apontando para slide que nao existe mais vira link
    # morto: o PowerPoint nao acusa, so nao vai a lugar nenhum.
    for s in p.slides:
        for sh in s.shapes:
            if not sh.has_text_frame: continue
            txt = (sh.text_frame.text or "").strip()
            if not txt.lower().startswith("5."): continue
            if "site survey" not in txt.lower(): continue
            _limpar_link_de_texto(sh)
            try:
                sh.click_action.target_slide = None
            except Exception:
                pass
            _ppt_set(sh.text_frame, "5. Site Survey (descontinuado)")
    log.info(f"[ppt] {len(alvos)} slide(s) da secao de survey do template "
             f"removidos")
    return len(alvos)


def gerar_ppt(cfg, ini_d, fim_d, demo=False):
    """Preenche o template PPT com os dados do período. Retorna (bytes, nome)."""
    try:
        from pptx import Presentation
    except ImportError:
        raise RuntimeError("python-pptx nao instalado: pip install python-pptx "
                           "(e recompile o exe com --collect-all pptx)")
    template = cfg.get("relatorio", "template_ppt",
                       fallback="Relatorio_Semanal_Rede.pptx")
    if not Path(template).exists():
        raise RuntimeError(f"Template PPT nao encontrado: {template} — coloque o "
                           "arquivo na pasta do programa ou ajuste [relatorio] template_ppt")

    d    = obter_dados(cfg, ini_d, fim_d, demo)
    bcs  = d["bcs"]
    meta_g = cfg.getfloat("relatorio","meta_geral")
    meta_m = cfg.getfloat("relatorio","meta_mesh")

    # ── Agregados ──
    disps    = [b["disp_pct"] for b in bcs.values() if b.get("disp_pct") is not None]
    disp_g   = round(sum(disps)/len(disps), 2) if disps else None
    disp_bb_l= [b["disp_pct"] for b in bcs.values()
                if b.get("cat")=="Backbone" and b.get("disp_pct") is not None]
    disp_bb  = round(sum(disp_bb_l)/len(disp_bb_l), 2) if disp_bb_l else None
    online   = sum(1 for b in bcs.values() if b.get("online_agora")==1)
    total    = len(bcs)
    scores   = [b["score_med"] for b in bcs.values() if b.get("score_med") is not None]
    score_m  = round(sum(scores)/len(scores), 1) if scores else None
    lats     = [b["lat_med"] for b in bcs.values() if b.get("lat_med") is not None]
    lat_m    = round(sum(lats)/len(lats), 1) if lats else None
    topo     = int(sum(b.get("topo_chg") or 0 for b in bcs.values()))
    abertas  = sum(1 for e in d["eventos"] if e["fim"] is None)
    thr_vals = list(d["series"].get("thr_dia", {}).values())
    thr_m    = round(sum(thr_vals)/len(thr_vals), 0) if thr_vals else None
    perdas   = [b["perda_med"] for b in bcs.values() if b.get("perda_med") is not None]
    perda_m  = round(sum(perdas)/len(perdas), 2) if perdas else None
    links_deg= sum(1 for L in d["links"].values()
                   if L.get("snr_med") is not None and L["snr_med"] < 15)
    cpus     = [b["cpu_med"] for b in bcs.values() if b.get("cpu_med") is not None]
    cpu_m    = round(sum(cpus)/len(cpus), 1) if cpus else None
    cpu_pior = max(((b.get("cpu_max") or 0, n) for n, b in bcs.items()), default=(None,""))
    temp_pior= max(((b.get("temp_max") or 0, n) for n, b in bcs.items()), default=(None,""))
    # Antes: contagem de BCs com tensão fora de 18–30 V. Não existe tensão na
    # BC API — o critério agora é bateria abaixo de 30% nos modelos que a têm.
    volt_fora= sum(1 for b in bcs.values()
                   if b.get("bat_min") is not None and b["bat_min"] < 30)
    ups      = [b["uptime_d"] for b in bcs.values() if b.get("uptime_d") is not None]
    up_m     = round(sum(ups)/len(ups), 1) if ups else None
    busy_max = [r.get("busy_max") or 0 for r in d["radios"].values()]
    util_m   = round(sum(busy_max)/len(busy_max), 0) if busy_max else None

    # Disponibilidade da mesh nos últimos 30 dias (coluna "Mês" do KPI)
    disp_mes = None
    if not demo:
        try:
            prom = Prometheus(cfg.get("relatorio","prometheus_url"))
            r = prom.instant("avg(avg_over_time(rajant_online[30d])) * 100",
                             min(time.time(),
                                 datetime.combine(fim_d + timedelta(days=1),
                                                  datetime.min.time()).timestamp()))
            if r: disp_mes = round(float(r[0]["value"][1]), 2)
        except Exception as e:
            log.warning(f"[ppt] disp mensal falhou: {e}")
    else:
        disp_mes = disp_g

    # Espectro
    radios_crit = [(r.get("ruido_pior"), bc, rn) for (bc, rn), r in d["radios"].items()
                   if r.get("ruido_pior") is not None and r["ruido_pior"] > -90]
    pior = max(((r.get("ruido_pior"), bc) for (bc, rn), r in d["radios"].items()
                if r.get("ruido_pior") is not None), default=(None,""))
    canais_ruido = {}
    for (bc, rn), r in d["radios"].items():
        for c in r.get("canais", set()):
            if r.get("ruido_med") is not None:
                canais_ruido.setdefault(str(c), []).append(r["ruido_med"])
    canal_poluido = max(((sum(v)/len(v), c) for c, v in canais_ruido.items()
                         if len(v) >= 3), default=(None,""))[1]

    p = Presentation(template)
    S = p.slides
    def slide(t):
        _, s = _slide_por_titulo(p, t)
        return s

    # ── Slide 1: período ──
    sh = _txt_por_conteudo(p.slides[0], "SEMANA")
    if sh: _ppt_set(sh.text_frame, f"{ini_d:%d/%m/%Y}  a  {fim_d:%d/%m/%Y}")

    # ── Slide 3: dashboard executivo ──
    t = _tabela_por_cabecalho(slide("Dashboard Executivo") or p.slides[2], "Indicador")
    if t:
        def linha_dash(rotulo, valor, meta, fmt="%"):
            for r_i in range(1, len(t.rows)):
                if t.cell(r_i, 0).text.strip().lower().startswith(rotulo.lower()):
                    _ppt_set(t.cell(r_i, 2).text_frame,
                             _ppt_fmt(valor, 2, "%") if fmt == "%" else
                             (str(valor) if valor is not None else "n/d"))
                    cel_st = t.cell(r_i, 1)
                    if cel_st.text_frame.paragraphs and cel_st.text_frame.paragraphs[0].runs:
                        _ppt_status(cel_st.text_frame.paragraphs[0].runs[0],
                                    valor, meta, invertido=(fmt=="inv"))
                    return
        linha_dash("Disponibilidade Geral", disp_g,  meta_g)
        linha_dash("Rede Mesh Rajant",      disp_g,  meta_m)
        if disp_bb is not None:
            linha_dash("Backbone",          disp_bb, meta_g)

    # ── Slide 4: KPIs ──
    _s4 = slide("KPIs da Semana") or p.slides[3]
    t = _tabela_por_cabecalho(_s4, "Sistema")
    if t:
        for r_i in range(1, len(t.rows)):
            if "mesh rajant" in t.cell(r_i, 0).text.lower():
                _ppt_set(t.cell(r_i, 2).text_frame, _ppt_fmt(disp_g, 2, "%"))
                _ppt_set(t.cell(r_i, 3).text_frame, _ppt_fmt(disp_mes, 2, "%"))
    t = _tabela_por_cabecalho(_s4, "Métrica")
    if t:
        valores_kpi = {
            "throughput":  _ppt_fmt(thr_m, 0, " Mbps"),
            "latência":    _ppt_fmt(lat_m, 1, " ms"),
            "utilização":  _ppt_fmt(util_m, 0, " %"),
            "cpu":         _ppt_fmt(cpu_m, 1, " %"),
            "memória":     "n/d",
            "temperatura": _ppt_fmt(temp_pior[0], 1, " °C") if temp_pior[0] else "n/d",
            "uptime":      _ppt_fmt(up_m, 1, " d"),
        }
        for r_i in range(1, len(t.rows)):
            rot = t.cell(r_i, 0).text.lower()
            for chave, val in valores_kpi.items():
                if chave in rot:
                    _ppt_set(t.cell(r_i, 1).text_frame, val); break

    # ── Slide 6: saúde da mesh (cartões + tabela) ──
    s6 = slide("Saúde da Rede Mesh") or p.slides[5]
    cartoes = [("DISPONIBILIDADE DA MALHA", _ppt_fmt(disp_g, 2, "%")),
               ("MESH SCORE",               _ppt_fmt(score_m, 1)),
               ("BREADCRUMBS ONLINE",       f"{online} / {total}"),
               ("LATÊNCIA MÉDIA",           _ppt_fmt(lat_m, 1, " ms")),
               ("MUDANÇAS DE TOPOLOGIA",    str(topo)),
               ("ALARMES ATIVOS",           str(abertas))]
    for rotulo, valor in cartoes:
        sh = _txt_valor_do_cartao(s6, rotulo)
        if sh: _ppt_set(sh.text_frame, valor)
    sh = _txt_por_conteudo(s6, "throughput médio")
    if sh and sh.text_frame.paragraphs:
        pr = sh.text_frame.paragraphs[0]
        if pr.runs:
            pr.runs[0].text = (f"Throughput médio {_ppt_fmt(thr_m,0)} Mbps  •  "
                               f"perda de pacotes {_ppt_fmt(perda_m,2)} %  •  "
                               f"links degradados (SNR<15dB): {links_deg}")
            for r in pr.runs[1:]: r.text = ""
    t = _tabela_por_cabecalho(s6, "CPU média")
    if t and len(t.rows) > 1:
        vals6 = [_ppt_fmt(cpu_m,1,"%"),
                 f"{_ppt_fmt(cpu_pior[0],0,'%')} ({cpu_pior[1][:12]})" if cpu_pior[0] else "n/d",
                 "n/d",
                 f"{_ppt_fmt(temp_pior[0],0,'°C')} ({temp_pior[1][:12]})" if temp_pior[0] else "n/d",
                 f"{volt_fora} nós",
                 _ppt_fmt(up_m,1," d")]
        for c_i, v in enumerate(vals6[:len(t.columns)]):
            _ppt_set(t.cell(1, c_i).text_frame, v)

    # ── Slide 7: rankings ──
    def preencher_ranking(tabela, itens, casas=1):
        if not tabela: return
        for i in range(1, len(tabela.rows)):
            if i-1 < len(itens):
                nome, val = itens[i-1]
                _ppt_set(tabela.cell(i, 1).text_frame, nome[:24])
                _ppt_set(tabela.cell(i, 2).text_frame, _ppt_fmt(val, casas))
            else:
                _ppt_set(tabela.cell(i, 1).text_frame, "—")
                _ppt_set(tabela.cell(i, 2).text_frame, "—")
    top_traf  = sorted(((n, b.get("trafego_gb")) for n, b in bcs.items()
                        if b.get("trafego_gb") is not None),
                       key=lambda x: -x[1])[:10]
    top_peers = sorted(((n, b.get("peers_med")) for n, b in bcs.items()
                        if b.get("peers_med") is not None),
                       key=lambda x: -x[1])[:10]
    _s7 = slide("Rankings") or p.slides[6]
    tabelas7 = [sh.table for sh in _s7.shapes if sh.has_table]
    if len(tabelas7) >= 1: preencher_ranking(tabelas7[0], top_traf)
    if len(tabelas7) >= 2: preencher_ranking(tabelas7[1], top_peers)

    # ── Slide 8: ruído e canal (Mapa de Calor) ──
    t = _tabela_por_cabecalho(slide("Mapa de Calor") or p.slides[7], "Semana")
    if t and len(t.rows) > 1:
        _ppt_set(t.cell(1, 1).text_frame, str(len(radios_crit)))
        _ppt_set(t.cell(1, 2).text_frame,
                 f"{_ppt_fmt(pior[0],0)} dBm ({pior[1][:10]})" if pior[0] is not None else "n/d")
        _ppt_set(t.cell(1, 3).text_frame,
                 f"CH {canal_poluido}" if canal_poluido else "n/d")

    # ── Slide 13: inventário Rajant ──
    s13 = slide("Inventário da Infraestrutura") or p.slides[12]
    for rotulo, valor in [("Total de BreadCrumbs", str(total)),
                          ("Online",  str(online)),
                          ("Offline", str(total - online))]:
        sh = _txt_valor_do_cartao(s13, rotulo)
        if sh: _ppt_set(sh.text_frame, valor)

    # ── Slides técnicos gerados (gráficos + tabelas na estética do template) ──
    if cfg.getboolean("relatorio", "slides_tecnicos", fallback=True):
        try:
            _slides_tecnicos(p, d, cfg, ini_d, fim_d,
                             disp_g, meta_g, canal_poluido)
        except Exception as e:
            log.warning(f"[ppt] slides técnicos falharam: {e}")

    # O survey foi descontinuado; o template do cliente ainda traz a seção
    # de Site Survey, que sairia em branco no meio do semanal.
    remover_slides_survey(p)

    # Identidade Anglo por último: repinta o que o template trouxe, o que o
    # relatório preencheu e o que os slides técnicos geraram, tudo de uma
    # vez. Falhar aqui é problema de estética, não de conteúdo — o deck sai
    # no visual antigo e o motivo fica no log.
    if cfg.getboolean("relatorio", "identidade_anglo", fallback=True):
        try:
            r = aplicar_identidade_anglo(p)
            log.info(f"[ppt] identidade Anglo: {r['slides']} slides, "
                     f"{r['formas']} formas, {r['runs']} textos, "
                     f"{r['tabelas']} celulas, {r['graficos']} cores de grafico")
        except Exception as e:
            log.warning(f"[ppt] identidade Anglo nao aplicada: {e}")

    buf = io.BytesIO(); p.save(buf)
    nome = (f"Relatorio_Semanal_Rede_{ini_d:%Y%m%d}_{fim_d:%Y%m%d}"
            f"{'_DEMO' if demo else ''}.pptx")
    return buf.getvalue(), nome

# ──────────────────────────────────────────────────────────────
# SLIDES TÉCNICOS — construídos na identidade visual do template
# (fundo 0D2052, cartões 1A2744, cabeçalho 1A3A7A, Calibri,
#  verde 27AE60 / azul 2980B9 / laranja E67E22 / vermelho C0392B)
# ──────────────────────────────────────────────────────────────
def _slides_tecnicos(p, d, cfg, ini_d, fim_d, disp_g, meta_g, canal_poluido):
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION

    C_BG   = RGBColor.from_string("0D2052")
    C_CARD = RGBColor.from_string("1A2744")
    C_HDR  = RGBColor.from_string("1A3A7A")
    C_ZEB  = RGBColor.from_string("152238")
    C_TXT  = RGBColor.from_string("C8D0E0")
    C_SUB  = RGBColor.from_string("A0B0CC")
    C_BRANCO = RGBColor.from_string("FFFFFF")
    C_VERDE  = RGBColor.from_string("27AE60")
    C_AZUL   = RGBColor.from_string("2980B9")
    C_LARANJA= RGBColor.from_string("E67E22")
    C_VERM   = RGBColor.from_string("C0392B")

    layout = p.slide_masters[0].slide_layouts[0]

    def novo_slide(titulo, subtitulo):
        s = p.slides.add_slide(layout)
        s.background.fill.solid()
        s.background.fill.fore_color.rgb = C_BG
        # remove placeholders herdados do layout
        for ph in list(s.placeholders):
            ph._element.getparent().remove(ph._element)
        tb = s.shapes.add_textbox(Inches(0.55), Inches(0.25), Inches(10.5), Inches(0.6))
        r = tb.text_frame.paragraphs[0].add_run(); r.text = titulo
        r.font.name = "Calibri"; r.font.size = Pt(26); r.font.bold = True
        r.font.color.rgb = C_BRANCO
        st = s.shapes.add_textbox(Inches(0.55), Inches(0.85), Inches(10.5), Inches(0.35))
        r2 = st.text_frame.paragraphs[0].add_run(); r2.text = subtitulo
        r2.font.name = "Calibri"; r2.font.size = Pt(12); r2.font.color.rgb = C_SUB
        # botão ◂ MENU com hyperlink para o slide de navegação
        b = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                               Inches(12.0), Inches(0.28), Inches(0.95), Inches(0.32))
        b.fill.solid(); b.fill.fore_color.rgb = C_HDR; b.line.fill.background()
        rb = b.text_frame.paragraphs[0].add_run(); rb.text = "◂ MENU"
        rb.font.name = "Calibri"; rb.font.size = Pt(10); rb.font.bold = True
        rb.font.color.rgb = C_BRANCO
        b.text_frame.paragraphs[0].alignment = PP_ALIGN.CENTER
        try: b.click_action.target_slide = p.slides[1]
        except Exception: pass
        return s

    def cartao(s, x, y, w, h):
        c = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                               Inches(x), Inches(y), Inches(w), Inches(h))
        c.fill.solid(); c.fill.fore_color.rgb = C_CARD
        c.line.color.rgb = RGBColor.from_string("2A3A5C"); c.line.width = Pt(0.75)
        c.shadow.inherit = False
        return c

    def tabela(s, x, y, w, h, cab, linhas, cores_txt=None, tam=10):
        """cab: lista de títulos | linhas: lista de listas |
        cores_txt: {(r,c): RGBColor} opcional"""
        gf = s.shapes.add_table(len(linhas)+1, len(cab),
                                Inches(x), Inches(y), Inches(w), Inches(h))
        t = gf.table
        t.first_row = False; t.horz_banding = False  # sem estilo padrão
        for j, txt in enumerate(cab):
            cel = t.cell(0, j)
            cel.fill.solid(); cel.fill.fore_color.rgb = C_HDR
            pr = cel.text_frame.paragraphs[0]; pr.alignment = PP_ALIGN.CENTER
            r = pr.add_run(); r.text = str(txt)
            r.font.name="Calibri"; r.font.size=Pt(tam); r.font.bold=True
            r.font.color.rgb = C_BRANCO
        for i, lin in enumerate(linhas, start=1):
            for j, v in enumerate(lin):
                cel = t.cell(i, j)
                cel.fill.solid()
                cel.fill.fore_color.rgb = C_ZEB if i % 2 == 0 else C_CARD
                pr = cel.text_frame.paragraphs[0]
                pr.alignment = PP_ALIGN.LEFT if j == 0 else PP_ALIGN.CENTER
                r = pr.add_run(); r.text = "" if v is None else str(v)
                r.font.name="Calibri"; r.font.size=Pt(tam)
                cor = (cores_txt or {}).get((i, j))
                r.font.color.rgb = cor if cor else C_TXT
                if cor: r.font.bold = True
        return t

    def _chart_escuro(chart, cor_serie=None):
        """Fundo transparente + textos claros no gráfico."""
        from pptx.oxml.ns import qn
        chart.font.name = "Calibri"; chart.font.size = Pt(10)
        chart.font.color.rgb = C_TXT
        cs = chart._chartSpace
        spPr = cs.find(qn('c:spPr'))
        if spPr is None:
            from lxml import etree
            spPr = etree.SubElement(cs, qn('c:spPr'))
        else:
            for ch in list(spPr): spPr.remove(ch)
        from lxml import etree
        etree.SubElement(spPr, qn('a:noFill'))
        etree.SubElement(spPr, qn('a:ln')).append(
            etree.fromstring('<a:noFill xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"/>'))
        try:
            chart.value_axis.has_major_gridlines = True
            gl = chart.value_axis.major_gridlines
            gl.format.line.color.rgb = RGBColor.from_string("2A3A5C")
            gl.format.line.width = Pt(0.5)
        except Exception: pass
        for eixo in ("category_axis", "value_axis"):
            try:
                ax = getattr(chart, eixo)
                ax.tick_labels.font.size = Pt(9)
                ax.tick_labels.font.color.rgb = C_SUB
                ax.format.line.color.rgb = RGBColor.from_string("2A3A5C")
            except Exception: pass

    def fmt(v, casas=1):
        return "n/d" if v is None else f"{v:.{casas}f}".replace(".", ",")

    def mover_apos_titulo(*titulos):
        """Move o slide recém-criado (último) para depois do primeiro
        slide encontrado entre os títulos dados (ordem de preferência)."""
        idx = None
        for t in titulos:
            i, _ = _slide_por_titulo(p, t)
            if i is not None:
                idx = i
                if idx == len(p.slides) - 1:  # o novo slide é o último
                    idx -= 1
                break
        lst = p.slides._sldIdLst
        el = list(lst)[-1]
        if idx is None:
            return  # deixa no fim
        lst.remove(el)
        lst.insert(idx + 1, el)

    bcs = d["bcs"]
    novos = 0

    # ═══ SLIDE A: Evolução Diária + Categorias (após Rankings, idx 6) ═══
    s = novo_slide("4. Saúde da Mesh Rajant — Evolução Diária",
                   f"Disponibilidade e throughput por dia  |  {ini_d:%d/%m} a {fim_d:%d/%m}  |  gerado automaticamente")
    dias  = d["dias"]
    disp_s= d["series"].get("disp_dia", {})
    thr_s = d["series"].get("thr_dia", {})
    cats_com_dado = [dia for dia in dias if dia in disp_s or dia in thr_s] or dias
    rotulos = [dia.strftime("%d/%m") for dia in cats_com_dado]

    cartao(s, 0.55, 1.35, 6.1, 3.5)
    cd1 = CategoryChartData(); cd1.categories = rotulos
    cd1.add_series("Disponibilidade %", [disp_s.get(dia) for dia in cats_com_dado])
    cd1.add_series("Meta", [meta_g]*len(cats_com_dado))
    gf1 = s.shapes.add_chart(XL_CHART_TYPE.LINE_MARKERS,
                             Inches(0.7), Inches(1.45), Inches(5.8), Inches(3.3), cd1)
    ch1 = gf1.chart; _chart_escuro(ch1)
    ch1.has_legend = True; ch1.legend.position = XL_LEGEND_POSITION.BOTTOM
    ch1.legend.include_in_layout = False
    ch1.series[0].format.line.color.rgb = C_VERDE
    ch1.series[0].format.line.width = Pt(2.25)
    ch1.series[1].format.line.color.rgb = C_LARANJA
    ch1.series[1].format.line.width = Pt(1.25)
    try:
        vals = [v for v in (disp_s.get(dia) for dia in cats_com_dado) if v is not None]
        if vals:
            ch1.value_axis.minimum_scale = max(0, min(min(vals), meta_g) - 2)
            ch1.value_axis.maximum_scale = 100
    except Exception: pass

    cartao(s, 6.85, 1.35, 6.1, 3.5)
    cd2 = CategoryChartData(); cd2.categories = rotulos
    cd2.add_series("Throughput (Mbps)", [thr_s.get(dia) for dia in cats_com_dado])
    gf2 = s.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED,
                             Inches(7.0), Inches(1.45), Inches(5.8), Inches(3.3), cd2)
    ch2 = gf2.chart; _chart_escuro(ch2)
    ch2.has_legend = False
    ch2.plots[0].gap_width = 60
    ch2.series[0].format.fill.solid()
    ch2.series[0].format.fill.fore_color.rgb = C_AZUL

    # Mini tabela por categoria
    cats = {}
    for n, b in bcs.items(): cats.setdefault(b.get("cat","Outros"), []).append(b)
    linhas_cat, cores_cat = [], {}
    ordem = [c for c in ["Backbone","ERB","ERM","Móvel","Outros"] if c in cats]
    for i, cat in enumerate(ordem, start=1):
        bs = cats[cat]
        ds = [b["disp_pct"] for b in bs if b.get("disp_pct") is not None]
        med = round(sum(ds)/len(ds), 2) if ds else None
        pior= min(ds) if ds else None
        linhas_cat.append([cat, len(bs),
                           sum(1 for b in bs if b.get("online_agora")==1),
                           fmt(med,2)+"%" if med is not None else "n/d",
                           fmt(pior,2)+"%" if pior is not None else "n/d"])
        for j, v in [(3, med), (4, pior)]:
            if v is not None:
                cores_cat[(i, j)] = (C_VERDE if v >= cfg.getfloat("relatorio","meta_mesh")
                                     else C_LARANJA if v >= cfg.getfloat("relatorio","meta_mesh")-1
                                     else C_VERM)
    tabela(s, 0.55, 5.1, 12.4, 0.4+0.32*len(linhas_cat),
           ["Categoria","Qtd","Online","Disponib. média","Pior disponib."],
           linhas_cat, cores_cat, tam=10)
    mover_apos_titulo("Rankings"); novos += 1

    # ═══ SLIDE B: Links Críticos por SNR ═══
    s = novo_slide("4. Saúde da Mesh Rajant — Links Críticos (SNR)",
                   "10 piores enlaces do período — candidatos a realinhamento / site survey  |  gerado automaticamente")
    ip_nome = {b.get("ip"): n for n, b in bcs.items() if b.get("ip")}
    piores = sorted(((k, L) for k, L in d["links"].items()
                     if L.get("snr_med") is not None),
                    key=lambda x: x[1]["snr_med"])[:10]
    linhas_l, cores_l = [], {}
    for i, ((bc, rn, peer), L) in enumerate(piores, start=1):
        snr = L.get("snr_med")
        linhas_l.append([bc[:20], rn, ip_nome.get(peer, peer)[:20],
                         fmt(snr), fmt(L.get("snr_min")),
                         fmt(L.get("sinal_med")), fmt(L.get("taxa_med"),0),
                         fmt(L.get("custo_med"),0), fmt(L.get("ativo_pct"),0)+"%"])
        if snr is not None:
            cores_l[(i,3)] = C_VERM if snr < 15 else (C_LARANJA if snr < 25 else C_VERDE)
    if not linhas_l:
        linhas_l = [["Sem dados de SNR no período","","","","","","","",""]]
    tabela(s, 0.55, 1.5, 12.4, 0.45+0.38*len(linhas_l),
           ["BreadCrumb","Rádio","Peer","SNR méd (dB)","SNR mín","Sinal (dBm)",
            "Taxa (Mbps)","Custo IM","Ativo"],
           linhas_l, cores_l, tam=10)
    nota = s.shapes.add_textbox(Inches(0.55), Inches(6.6), Inches(12), Inches(0.3))
    rn2 = nota.text_frame.paragraphs[0].add_run()
    rn2.text = ("Leitura BCE: SNR > 30 dB bom  |  15–30 dB atenção  |  < 15 dB crítico. "
                "Custo InstaMesh menor = rota preferida.")
    rn2.font.name="Calibri"; rn2.font.size=Pt(10); rn2.font.italic=True
    rn2.font.color.rgb = C_SUB
    mover_apos_titulo("Evolução Diária"); novos += 1

    # ═══ SLIDE C: Ethernet + Quedas ═══
    s = novo_slide("4. Saúde da Mesh Rajant — Ethernet e Quedas",
                   "Portas com anomalia e maiores indisponibilidades do período  |  gerado automaticamente")
    t1 = s.shapes.add_textbox(Inches(0.55), Inches(1.3), Inches(6), Inches(0.3))
    r3 = t1.text_frame.paragraphs[0].add_run(); r3.text = "PORTAS ETHERNET COM ANOMALIA"
    r3.font.name="Calibri"; r3.font.size=Pt(11); r3.font.bold=True; r3.font.color.rgb=C_SUB
    # A BC API não tem erro/CRC/drop/state-changes por porta (State.Wired usa
    # CommStats, só bytes e pacotes). O critério de anomalia passa a ser o
    # que é medível: link% abaixo de 99 ou porta APT sem peer.
    anom = sorted(((k, e) for k, e in d["eths"].items()
                   if (e.get("link_pct") is not None and e["link_pct"] < 99)
                   or (e.get("apt") in (0, 1) and not (e.get("peers") or 0))),
                  key=lambda x: (x[1].get("link_pct") if x[1].get("link_pct")
                                 is not None else 100))[:8]
    linhas_e, cores_e = [], {}
    for i, ((bc, porta), e) in enumerate(anom, start=1):
        lk = e.get("link_pct")
        linhas_e.append([bc[:18], porta, fmt(lk,1)+"%" if lk is not None else "n/d",
                         fmt(e.get("rx_med"),1), fmt(e.get("tx_med"),1),
                         _APT_TXT.get(e.get("apt"), "n/d"),
                         int(e.get("peers") or 0)])
        if lk is not None and lk < 99: cores_e[(i,2)] = C_VERM
        if e.get("apt") in (0, 1) and not (e.get("peers") or 0):
            cores_e[(i,6)] = C_LARANJA
    if not linhas_e:
        linhas_e = [["Nenhuma anomalia ethernet no período","","","","","",""]]
    tabela(s, 0.55, 1.65, 6.2, 0.4+0.3*len(linhas_e),
           ["BC","Porta","Link","RX Mb","TX Mb","APT","Peers"],
           linhas_e, cores_e, tam=9)
    # Nota de rodapé: o slide prometia erros/CRC/drops que a BC API não mede.
    nt = s.shapes.add_textbox(Inches(0.55), Inches(1.65)+Inches(0.4+0.3*len(linhas_e)),
                              Inches(6.2), Inches(0.5))
    rn3 = nt.text_frame.paragraphs[0].add_run()
    rn3.text = ("Erros / CRC / drops por porta não são publicados pela BC API "
                "(State.Wired expõe apenas bytes, pacotes, aptState e peers).")
    rn3.font.name="Calibri"; rn3.font.size=Pt(8); rn3.font.italic=True
    rn3.font.color.rgb = C_SUB

    t2 = s.shapes.add_textbox(Inches(7.0), Inches(1.3), Inches(6), Inches(0.3))
    r4 = t2.text_frame.paragraphs[0].add_run(); r4.text = "MAIORES QUEDAS DO PERÍODO"
    r4.font.name="Calibri"; r4.font.size=Pt(11); r4.font.bold=True; r4.font.color.rgb=C_SUB
    qs = sorted(d["eventos"], key=lambda e: -e["dur_min"])[:8]
    linhas_q, cores_q = [], {}
    for i, e in enumerate(qs, start=1):
        aberto = e["fim"] is None
        linhas_q.append([e["bc"][:18], e["inicio"].strftime("%d/%m %H:%M"),
                         "—" if aberto else e["fim"].strftime("%d/%m %H:%M"),
                         fmt(e["dur_min"],0),
                         "EM ABERTO" if aberto else "OK"])
        if aberto: cores_q[(i,4)] = C_VERM
    if not linhas_q:
        linhas_q = [["Nenhuma queda no período","","","",""]]
    tabela(s, 7.0, 1.65, 5.95, 0.4+0.3*len(linhas_q),
           ["BC","Início","Fim","Min","Status"], linhas_q, cores_q, tam=9)
    tot_q = len(d["eventos"]); tot_h = round(sum(e["dur_min"] for e in d["eventos"])/60, 1)
    resumo = s.shapes.add_textbox(Inches(0.55), Inches(6.6), Inches(12), Inches(0.3))
    r5 = resumo.text_frame.paragraphs[0].add_run()
    r5.text = (f"Total do período: {tot_q} quedas  •  {fmt(tot_h,1)} h de indisponibilidade acumulada  •  "
               f"{sum(1 for e in d['eventos'] if e['fim'] is None)} em aberto na geração")
    r5.font.name="Calibri"; r5.font.size=Pt(11); r5.font.color.rgb=C_TXT; r5.font.bold=True
    mover_apos_titulo("Links Críticos"); novos += 1

    # ═══ SLIDE D: Espectro por Canal ═══
    # Seção 4: estes números vêm dos contadores do exporter (todos os
    # rádios ativos, o período inteiro). Com título "5. Site Survey — ..."
    # ele sairia do deck junto com a seção de survey do template.
    s = novo_slide("4. Saúde da Mesh Rajant — Espectro por Canal",
                   "Ocupação e ruído agregados por canal — todos os rádios ativos  |  gerado automaticamente")
    por_canal = {}
    for (bc, rn), r in d["radios"].items():
        for c in r.get("canais", set()):
            e = por_canal.setdefault(str(c), {"freq": r.get("freq",""), "n": 0,
                                              "ruidos": [], "piores": [], "busys": []})
            e["n"] += 1
            if r.get("ruido_med")  is not None: e["ruidos"].append(r["ruido_med"])
            if r.get("ruido_pior") is not None: e["piores"].append(r["ruido_pior"])
            if r.get("busy_med")   is not None: e["busys"].append(r["busy_med"])
    def orden(c):
        try: return int(c)
        except: return 999
    canais = sorted(por_canal, key=orden)
    lim_ruido = cfg.getfloat("relatorio","limite_ruido")

    cartao(s, 0.55, 1.35, 6.1, 3.6)
    cd3 = CategoryChartData(); cd3.categories = [f"CH {c}" for c in canais]
    cd3.add_series("Busy % médio",
                   [round(sum(e["busys"])/len(e["busys"]),1) if e["busys"] else 0
                    for e in (por_canal[c] for c in canais)])
    gf3 = s.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED,
                             Inches(0.7), Inches(1.45), Inches(5.8), Inches(3.4), cd3)
    ch3 = gf3.chart; _chart_escuro(ch3); ch3.has_legend = False
    ch3.plots[0].gap_width = 50
    ch3.series[0].format.fill.solid()
    ch3.series[0].format.fill.fore_color.rgb = C_AZUL

    linhas_c, cores_c = [], {}
    for i, c in enumerate(canais[:10], start=1):
        e = por_canal[c]
        r_med  = round(sum(e["ruidos"])/len(e["ruidos"]),1) if e["ruidos"] else None
        r_pior = max(e["piores"]) if e["piores"] else None
        if   r_pior is None:     sit, cor = "s/ dados", None
        elif r_pior > lim_ruido: sit, cor = "CRÍTICO",  C_VERM
        elif r_pior > -100:      sit, cor = "Moderado", C_LARANJA
        else:                    sit, cor = "Limpo",    C_VERDE
        linhas_c.append([f"CH {c}", canal_para_mhz(c) or "—", e["n"],
                         fmt(r_med), fmt(r_pior), sit])
        if cor: cores_c[(i,5)] = cor
        if r_med is not None:
            cores_c[(i,3)] = C_VERM if r_med > lim_ruido else (
                             C_LARANJA if r_med > -100 else C_VERDE)
    tabela(s, 6.85, 1.35, 6.1, 0.45+0.34*len(linhas_c),
           ["Canal","MHz","Rádios","Ruído méd","Pior","Situação"],
           linhas_c, cores_c, tam=10)
    if canal_poluido:
        alerta = s.shapes.add_textbox(Inches(0.55), Inches(6.55), Inches(12), Inches(0.35))
        r6 = alerta.text_frame.paragraphs[0].add_run()
        r6.text = (f"Canal mais poluído do período: CH {canal_poluido}"
                   f"  ({canal_para_mhz(canal_poluido) or '—'} MHz)"
                   " — avaliar replanejamento de canal nos rádios afetados.")
        r6.font.name="Calibri"; r6.font.size=Pt(11); r6.font.bold=True
        r6.font.color.rgb = C_LARANJA
    # Ancorado nos slides da própria seção 4; sem âncora o slide ficava
    # solto no fim.
    mover_apos_titulo("Ethernet e Quedas", "Links Críticos",
                      "Análise, Recomendações e Conclusão", "No Talk",
                      "Mapa de Calor"); novos += 1
    log.info(f"[ppt] {novos} slides técnicos adicionados")


# ══════════════════════════════════════════════════════════════
# A MALHA CONHECIDA — os IPs da frota, no código
# ══════════════════════════════════════════════════════════════
# Os rádios da mina, por IP e nome: a lista do rajant_ips_cache.json da
# operação (15/10/2026), agora fixa aqui — o programa não lê nem grava
# cache nenhum. Cinco IPs estavam no arquivo com o próprio IP no lugar do
# nome (leitura que falhou, ver ler_estado); o nome deles veio do cache
# anterior da operação: ERB-01 PLATO 17, ERB-14, ERM-07, CA-1038, CA-1044.
#
# TODOS são coletados sempre, respondam ou não: rádio fora do ar aparece
# offline no Grafana em vez de sumir. É também o ponto de partida da
# descoberta, que segue os vizinhos de cada um e acha rádio novo — que,
# para seguir monitorado depois de um reinício, entra nesta lista.
#
# O nome que o próprio rádio informa tem prioridade sobre o daqui.
REDE_CONHECIDA = {
    "10.188.96.30": "ERB-01 PLATO 17",
    "10.188.96.40": "ERB-02",
    "10.188.96.50": "ERB-03",
    "10.188.96.60": "ERB-04",
    "10.188.96.70": "ERB-05 TALUDE",
    "10.188.96.90": "ERB-07",
    "10.188.96.100": "ERB-08",
    "10.188.96.130": "ERB-11 L2",
    "10.188.96.131": "ERB-11 L1",
    "10.188.96.140": "ERB-12 Slipstream",
    "10.188.96.150": "ERB-13",
    "10.188.96.160": "ERB-14",
    "10.188.97.10": "ERM-01 PTP CAM",
    "10.188.97.20": "ERM-02",
    "10.188.97.30": "ERM-03",
    "10.188.97.40": "ERM-04 PTP",
    "10.188.97.50": "ERM-05 PTP CAM",
    "10.188.97.60": "ERM-06 PTP",
    "10.188.97.70": "ERM-07",
    "10.188.97.80": "ERM-08 PTP CAM",
    "10.188.97.90": "ERM-09 PTP CAM",
    "10.188.97.100": "ERM-10 PTP CAM",
    "10.188.97.110": "ERM-11",
    "10.188.97.120": "ERM-12 PTP CAM",
    "10.188.97.127": "ERM-27 PTP",
    "10.188.97.128": "ERM-28 PTP",
    "10.188.97.129": "ERM-29 PTP",
    "10.188.97.130": "ERM-13",
    "10.188.97.139": "ERM-30 PTP",
    "10.188.97.140": "ERM-14",
    "10.188.97.148": "ERM-31 PTP CAM",
    "10.188.97.150": "ERM-15 PTP",
    "10.188.97.160": "ERM-16",
    "10.188.97.164": "ERM-33 PTP CAM",
    "10.188.97.170": "ERM-17",
    "10.188.97.180": "ERM-18",
    "10.188.97.184": "ERM-35 PTP CAM",
    "10.188.97.190": "ERM-19",
    "10.188.97.200": "ERM-20",
    "10.188.97.210": "ERM-21 PTP CAM",
    "10.188.97.220": "ERM-22 PTP CAM",
    "10.188.97.230": "ERM-23 PTP",
    "10.188.97.240": "ERM-24 PTP",
    "10.188.99.1": "CA-1001",
    "10.188.99.2": "CA-1002",
    "10.188.99.3": "CA-1003",
    "10.188.99.4": "CA-1004",
    "10.188.99.6": "CA-1006",
    "10.188.99.7": "CA-1007",
    "10.188.99.10": "CA-1010",
    "10.188.99.11": "CA-1011",
    "10.188.99.13": "CA-1013",
    "10.188.99.14": "CA-1014",
    "10.188.99.16": "CA-1016",
    "10.188.99.17": "CA-1017",
    "10.188.99.18": "CA-1018",
    "10.188.99.19": "CA-1019",
    "10.188.99.20": "CA-1020",
    "10.188.99.21": "CA-1021",
    "10.188.99.22": "CA-1022",
    "10.188.99.23": "CA-1023",
    "10.188.99.24": "CA-1024",
    "10.188.99.25": "CA-1025",
    "10.188.99.26": "CA-1026",
    "10.188.99.27": "CA-1027",
    "10.188.99.28": "CA-1028",
    "10.188.99.29": "CA-1029",
    "10.188.99.30": "CA-1030",
    "10.188.99.31": "CA-1031",
    "10.188.99.32": "CA-1032",
    "10.188.99.33": "CA-1033",
    "10.188.99.34": "CA-1034",
    "10.188.99.35": "CA-1035",
    "10.188.99.36": "CA-1036",
    "10.188.99.37": "CA-1037",
    "10.188.99.38": "CA-1038",
    "10.188.99.40": "CA-1040",
    "10.188.99.41": "CA-1041",
    "10.188.99.42": "CA-1042",
    "10.188.99.43": "CA-1043",
    "10.188.99.44": "CA-1044",
    "10.188.99.45": "CA-1045",
    "10.188.99.46": "CA-1046",
    "10.188.99.49": "CA-1049",
    "10.188.99.50": "CA-1050",
    "10.188.99.91": "RED070",
    "10.188.99.92": "RED071",
    "10.188.99.114": "PA-5504",
    "10.188.99.115": "PA-5505",
    "10.188.99.116": "PA-5506",
    "10.188.99.118": "TT-3707",
    "10.188.99.119": "TT-3708",
    "10.188.99.120": "TT-3709",
    "10.188.99.121": "TT-3710",
    "10.188.99.122": "TT-3711",
    "10.188.99.131": "PA-5501",
    "10.188.99.132": "PA-5502",
    "10.188.99.133": "PA-5503",
    "10.188.99.134": "EH-6001",
    "10.188.99.135": "EH-6002",
    "10.188.99.136": "EH-6101",
    "10.188.99.137": "EH-6102",
    "10.188.99.138": "RED002",
    "10.188.99.140": "EH-6003",
    "10.188.99.146": "EH-6550",
    "10.188.99.147": "EH-6551",
    "10.188.99.151": "PF-4601",
    "10.188.99.155": "PF-4701",
    "10.188.99.156": "PF-4702 FLANDERS",
    "10.188.99.157": "PF-4703",
    "10.188.99.158": "PF-4704",
    "10.188.99.159": "PF-4705",
    "10.188.99.169": "TT-3505 TECWISE",
    "10.188.99.170": "TT-3506",
    "10.188.99.171": "TT-3507",
    "10.188.99.172": "TT-3508 TECWISE",
    "10.188.99.173": "TT-3509 TECWISE",
    "10.188.99.174": "TT-3510 TECWISE",
    "10.188.99.176": "TT-3512 TECWISE",
    "10.188.99.189": "TT-3705",
    "10.188.99.190": "TT-3706",
    "10.188.99.192": "TT-3802",
    "10.188.99.195": "PA-5802",
    "10.188.99.199": "TN-4001",
    "10.188.99.200": "TN-4002",
    "10.188.99.210": "EH-6402",
    "10.188.99.212": "EH-6404",
    "10.188.99.213": "EH-6405",
    "10.188.99.216": "CP-7001",
    "10.188.99.219": "CP-7004",
    "10.188.99.227": "MA-5101",
    "10.188.99.229": "MA-5103",
    "10.188.99.230": "MA-5104",
    "10.188.99.238": "EH-6553",
    "10.188.99.240": "MA-5201",
    "10.188.99.241": "MA-5202",
    "10.188.99.242": "MA-5203",
    "10.188.100.155": "PF-4701 FLANDERS",
    "10.188.100.157": "PF-4703 FLANDERS",
    "10.188.100.158": "PF-4704 FLANDERS",
    "10.188.105.170": "TT-3506 KOMATSU",
    "10.188.111.46": "PCP-002 Sensing POD",
}


# ══════════════════════════════════════════════════════════════
# SESSÃO NA BCAPI — login e leitura do State
# ══════════════════════════════════════════════════════════════
# O get_message() da rajant-api 0.1.1 faz UM recv(65535): pelo TLS isso
# devolve no máximo um registro (~16 KB), e uma mensagem maior chega pela
# metade. O State de um rádio com muitos vizinhos passa disso.
# Aqui se usa a mesma sessão da biblioteca (connection, build_message,
# send_message, seq_number) e o MESMO enquadramento do código dela:
# cabeçalho de 8 bytes `>ibbbb` (tamanho, compressão 2 = deflate cru,
# wbits -15) e o corpo, lido até o tamanho que o cabeçalho declara.
import struct as _struct, zlib as _zlib

# Quantas vezes o login daqui falhou e o da biblioteca entrou.
_LOGIN_RESERVA = {"vezes": 0}


def _bc_ler_exato(conn, n):
    partes, falta = [], n
    while falta > 0:
        b = conn.recv(min(falta, 65536))
        if not b:
            raise ConnectionError("o rádio fechou a conexão no meio da mensagem")
        partes.append(b); falta -= len(b)
    return b"".join(partes)


def bc_receber(bc):
    """Uma mensagem INTEIRA do rádio, do tamanho que o cabeçalho declara."""
    cab = _bc_ler_exato(bc.connection, 8)
    n, flag = _struct.unpack(">ibbbb", cab)[:2]
    if n < 0 or n > 64 * 1024 * 1024:
        raise ValueError(f"cabeçalho de mensagem inválido ({n} bytes)")
    corpo = _bc_ler_exato(bc.connection, n)
    bruto = len(corpo)
    if flag == 2:
        corpo = _zlib.decompress(corpo, -15)
    m = type(bc.build_message())()
    m.ParseFromString(corpo)
    bc.seq_number += 1            # como o get_message da biblioteca
    bc._ultimo_tamanho = (bruto, len(corpo))
    return m


def _sessao_da_biblioteca(bc):
    """A sessão é da rajant-api de verdade (e não um objeto de teste)?"""
    return (getattr(bc, "connection", None) is not None
            and callable(getattr(bc, "build_message", None))
            and callable(getattr(bc, "send_message", None)))


def ler_estado(bc):
    """O State do rádio, INTEIRO, ou uma exceção que diz por que não veio.

    O get_state() da biblioteca devolve False em qualquer falha — inclusive
    o State que chegou pela metade —, e o False passava pelo parser como um
    State vazio: rádio sem nome, sem rádio e sem vizinho, tratado como
    leitura boa. Com o filtro de tag ligado, o rádio grande saía do Grafana
    como "sem tag".

    O pedido é o mesmo do get_state() dela; sem o ping que ela faz antes,
    porque quem chama já testou o alcance.
    """
    if _sessao_da_biblioteca(bc):
        m = bc.build_message()
        m.state.Clear()               # exatamente o pedido da biblioteca
        bc.send_message(m)
        for _ in range(6):
            r = bc_receber(bc)
            if r.HasField("state"):
                return r.state
        raise ConnectionError("o rádio respondeu, mas sem o State")
    st = bc.get_state()
    if st is False or st is None or (isinstance(st, (str, bytes, dict))
                                     and not st):
        raise ConnectionError("o rádio não devolveu o State")
    return st


def fechar_sessao(bc):
    """A Breadcrumb não tem close(): sem isto a sessão fica aberta no rádio
    até o coletor de lixo passar."""
    try:
        c = getattr(bc, "connection", None)
        if c is not None:
            c.close()
    except Exception:
        pass


def papel_bcapi(role):
    """O usuário (papel) do jeito que a rajant-api o procura: maiúsculo.

    A biblioteca faz `roles[role]` num dicionário que só tem VIEW, LOCAL,
    ADMIN, CO e JOIN. Com "co" em minúsculo dá KeyError — e o `except:`
    do authenticate() dela engole o erro e devolve só False. Na tela saía
    "autenticacao falhou" nos 156 rádios, com a senha certa.
    """
    return str(role or "").strip().upper()


AGENTE_BCAPI = "rajant_monitor"

# Prazo de cada etapa do login e, depois dele, de cada leitura da sessão.
# A biblioteca usa 2 s fixos, com o aperto de mão TLS dentro: pouco para
# rádio a vários saltos numa malha carregada.
PRAZO_BCAPI_S = 5.0

_ETAPAS_LOGIN = {
    "conexão": "a conexão TLS não completou",
    "desafio": "conectou, mas o rádio não mandou o desafio",
    "resultado": "o rádio não respondeu à senha",
}


def _conectar(bc, prazo_s):
    """A conexão TLS da biblioteca, com o prazo daqui. Se a classe trocou
    a conexão (teste, outra versão), usa a dela."""
    f = getattr(type(bc), "setup_connection_socket", None)
    mod = sys.modules.get(getattr(f, "__module__", "") or "")
    wrap = getattr(mod, "wrap_socket", None)
    if getattr(f, "__module__", "") == "rajant_api" and callable(wrap):
        import socket as _socket
        soc = _socket.socket(_socket.AF_INET)
        soc.settimeout(prazo_s)
        bc.connection = wrap(soc)
        bc.connection.connect((bc.host, bc.port))
    else:
        bc.setup_connection_socket()
    try:
        bc.connection.settimeout(prazo_s)
    except Exception:
        pass


def autenticar(bc, etapas=None, prazo_s=None, reserva=True):
    """Login no rádio: True, ou uma exceção com o MOTIVO.

    O authenticate() da biblioteca devolve False para tudo — usuário que
    não existe, senha errada, TLS, tempo esgotado —, e a tela só podia
    dizer "autenticacao falhou". Aqui são os mesmos passos dela (o rádio
    manda um desafio; volta sha384 da senha + desafio), com cada falha
    dita pelo nome, a etapa em que parou e a descrição que o próprio rádio
    mandar. Sem o ping que ela faz antes: quem chama já testou o alcance.

    `etapas` (lista) recebe (etapa, ms) — é o que o diagnóstico grava.
    `reserva`: se este caminho falhar por rede, tenta o authenticate() da
    biblioteca antes de desistir. Se ela entrar onde este não entrou, o
    defeito é daqui — fica no log, e a coleta não para por causa dele.
    """
    prazo_s = PRAZO_BCAPI_S if prazo_s is None else prazo_s
    bc.role = papel_bcapi(getattr(bc, "role", ""))
    papeis = getattr(bc, "roles", None)
    if isinstance(papeis, dict) and papeis and bc.role not in papeis:
        raise PermissionError(f"usuário '{bc.role}' não existe na BCAPI — use "
                              + ", ".join(sorted(papeis, key=papeis.get)))
    if not (callable(getattr(bc, "setup_connection_socket", None))
            and callable(getattr(bc, "prepare_login_message", None))
            and isinstance(getattr(bc, "statuses", None), dict)):
        # Objeto de teste, ou outra versão da biblioteca: o caminho dela.
        if not bc.authenticate():
            raise PermissionError("autenticacao falhou")
        return True
    t0 = time.monotonic()

    def marca(nome):
        if etapas is not None:
            etapas.append((nome, round((time.monotonic() - t0) * 1000)))

    etapa = "conexão"
    try:
        _conectar(bc, prazo_s)
        marca("conexão TLS")
        etapa = "desafio"
        ini = bc_receber(bc)
        marca("desafio recebido")
        etapa = "resultado"
        bc.serial = str(ini.auth.serial)
        pedido = bc.prepare_login_message(ini)
        # Campo do Auth "reportado pelo cliente no login" (Message.proto):
        # o rádio o mostra em State.adminSessions, e o diagnóstico separa
        # a sessão desta ferramenta da de um MeshMapper ou BC|Commander.
        pedido.auth.userAgent = AGENTE_BCAPI
        bc.send_message(pedido)
        res = None
        for _ in range(3):
            r = bc_receber(bc)
            if r.HasField("authResult"):
                res = r.authResult
                break
        marca("resultado recebido")
    except OSError as e:          # tempo esgotado, TLS, conexão recusada
        try: bc.connection.close()
        except Exception: pass
        if isinstance(e, TimeoutError):
            erro = TimeoutError(f"sem resposta no login: {_ETAPAS_LOGIN[etapa]} "
                                f"em {prazo_s:g} s (porta {bc.port})")
        else:
            erro = ConnectionError(f"login: {_ETAPAS_LOGIN[etapa]} — "
                                   f"{type(e).__name__}: {e}")
        marca(f"parou: {etapa}")
        if reserva and callable(getattr(bc, "authenticate", None)):
            t1 = time.monotonic()
            try:
                ok = bool(bc.authenticate())
            except Exception:
                ok = False
            if etapas is not None:
                etapas.append(("authenticate() da biblioteca: "
                               + ("entrou" if ok else "também falhou"),
                               round((time.monotonic() - t1) * 1000)))
            if ok:
                _LOGIN_RESERVA["vezes"] += 1
                if _LOGIN_RESERVA["vezes"] == 1:
                    log.warning(f"[bcapi] login próprio falhou em {bc.host} "
                                f"({erro}); o da biblioteca entrou")
                try: bc.connection.settimeout(prazo_s)
                except Exception: pass
                return True
        raise erro
    if res is None:
        raise PermissionError("o rádio não devolveu o resultado do login")
    st = bc.statuses.get(res.status, str(res.status))
    if st != "SUCCESS":
        try: bc.connection.close()
        except Exception: pass
        desc = (res.description or "").strip()
        msg = (f"o rádio recusou o login de {bc.role}"
               + (f" ({desc})" if desc else "") + " — confira usuário e senha")
        senha = getattr(bc, "password", None) or ""
        if senha != senha.strip():
            msg += "; a senha digitada tem espaço no começo ou no fim"
        raise PermissionError(msg)
    bc.authenticated = True
    return True


# ══════════════════════════════════════════════════════════════
# IDENTIDADE ANGLO AMERICAN
# Extraída do Dashboard_Transporte que o cliente forneceu: as medidas
# abaixo são as DELE, não aproximações — logo, título e régua caem nos
# mesmos pontos, senão os decks não parecem da mesma família.
# ══════════════════════════════════════════════════════════════
ANGLO = {
    "azul":      "031795",   # institucional: título, régua, fundo da capa
    "azul2":     "19328F",   # preenchimento secundário
    "texto":     "1A1A1A",
    "suave":     "5A6478",
    "linha":     "D6DAE3",
    "fundo":     "FFFFFF",
    "cartao":    "F4F6FA",
    "fonte":     "Calibri",
    # geometria do template do cliente, em polegadas
    "logo":      (0.30, 0.20, 1.50, 0.59),
    "titulo":    (1.80, 0.20, 9.73, 0.70),
    "regua_y":   1.00,
    "regua":     (0.30, 12.73, 0.028),
    "capa_logo": (0.70, 0.55, 2.30, 0.91),
    "capa_tit":  (1.00, 2.70, 9.60, 2.00),
}
DIR_MARCA = "marca"


def _logo_anglo(branco=False):
    """Caminho do logo. None quando a pasta `marca/` não veio junto —
    aí o slide sai sem logo em vez de estourar."""
    nome = "anglo_branco.png" if branco else "anglo_azul.png"
    for base in (Path(DIR_MARCA), Path(__file__).resolve().parent / DIR_MARCA):
        p = base / nome
        if p.exists(): return str(p)
    return None


def _rgb(hexa):
    from pptx.dml.color import RGBColor
    return RGBColor.from_string(hexa)


_A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _limpar_link_de_texto(shape):
    """Remove o hyperlink do TEXTO da forma.

    Um botão do template carrega DOIS links: um na forma e outro dentro do
    run. Clicando no texto — que é o que a pessoa faz — o PowerPoint usa o
    do run. Retargetar só a forma deixava o botão indo para o alvo antigo,
    e não havia como perceber pelo python-pptx: os dois existem em paralelo
    e a API só mostra o da forma.
    """
    if not shape.has_text_frame:
        return 0
    n = 0
    for par in shape.text_frame.paragraphs:
        for r in par.runs:
            rPr = r._r.find(f"{_A_NS}rPr")
            if rPr is None: continue
            for hl in rPr.findall(f"{_A_NS}hlinkClick"):
                rPr.remove(hl); n += 1
    return n


# ──────────────────────────────────────────────────────────────
# IDENTIDADE ANGLO NO DECK SEMANAL
#
# O semanal nasce de um template do cliente (Relatorio_Semanal_Rede.pptx),
# em azul-escuro, com menus, botões, cartões e campos preenchidos à mão.
# Trocar o arquivo por um template Anglo criaria dois templates para manter
# em sincronia e jogaria fora o que já está digitado nele. Em vez disso o
# deck é REPINTADO no fim da geração, depois de preenchido: nada muda de
# lugar, nada se perde — só a pele.
#
# Repintar no fim, e não no começo, tem um motivo: assim a passagem pega
# também o que o relatório acabou de escrever (o verde/âmbar/vermelho do
# status) e os slides técnicos gerados por código, que usam a MESMA paleta
# do template. Uma tabela de cores, dois produtores.
#
# Isso só é seguro porque o template não deixa nada para herdar. Conferido
# no arquivo do cliente: 874 runs de texto e TODOS com cor explícita; o
# master e o layout não têm forma nenhuma; a fonte já é Calibri, a mesma da
# identidade. Se um dia aparecer run sem cor, ele herda o texto escuro do
# tema e continua legível no fundo branco — o pior caso é perder ênfase,
# não sumir.
# ──────────────────────────────────────────────────────────────

# Preenchimentos, bordas e grades. A paleta do template é fechada: estas
# sete cores cobrem fundo, cartão, zebra, cabeçalho e linha em todo o deck.
ANGLO_FUNDOS = {
    "0D2052": ANGLO["fundo"],    # fundo do slide
    "16223C": ANGLO["fundo"],    # variação do fundo
    "1A2744": ANGLO["cartao"],   # cartão
    "152238": "ECEFF5",          # zebra da tabela
    "1A3A7A": ANGLO["azul"],     # cabeçalho de tabela / botão
    "2A3A5C": ANGLO["linha"],    # bordas e grade do gráfico
    "5A6A88": ANGLO["linha"],
}

# Texto. Os tons de status mudam de valor, não de sentido: verde, âmbar e
# vermelho do template foram escolhidos para fundo escuro e clareiam demais
# no branco — âmbar puro (FFC107) fica ilegível.
ANGLO_TEXTOS = {
    "C8D0E0": ANGLO["texto"],
    "A0B0CC": ANGLO["suave"],
    "5A6A88": ANGLO["suave"],
    "7FB3D3": ANGLO["azul2"],
    "2980B9": ANGLO["azul2"],
    "27AE60": "1E7A3E",
    "4CAF50": "1E7A3E",
    "E67E22": "B26A00",
    "FFC107": "B26A00",
    "C0392B": "B3261E",
    "F44336": "B3261E",
}

# O que sobrou escuro DEPOIS da conversão precisa de um tom claro quando o
# fundo continuou escuro — a capa e os botões de voltar. Para os tons de
# status a volta é literalmente a cor original do template: ela já tinha
# sido escolhida para fundo escuro.
ANGLO_SOBRE_ESCURO = {
    ANGLO["texto"]: ANGLO["fundo"],
    ANGLO["suave"]: ANGLO["fundo"],
    ANGLO["azul"]:  ANGLO["fundo"],
    ANGLO["azul2"]: "7FB3D3",
    "1E7A3E": "27AE60",
    "B26A00": "E67E22",
    "B3261E": "E74C3C",
}

# Na capa o fundo NÃO vira branco: vira o azul institucional. O texto já
# é branco e continua branco.
ANGLO_CAPA = {
    "0D2052": ANGLO["azul"],
    "16223C": ANGLO["azul"],
    "1A2744": ANGLO["azul2"],
    "1A3A7A": ANGLO["azul2"],
}

# Geometria do cabeçalho no semanal: logo à esquerda, título azul à direita
# dele, régua azul de ponta a ponta.
#
# O cabeçalho é mais compacto que o do template (que punha o título em
# 0,32" e o subtítulo em 0,92") porque a régua tem de caber ACIMA do
# conteúdo, e o slide de "Próxima Semana" começa em 1,20" — não em 1,35"
# como os outros. Com a régua em 1,08" ela passa em todos sem que nenhuma
# tabela ou cartão do template precise sair do lugar.
ANGLO_SEMANAL = {
    "logo":    (0.30, 0.22, 1.50, 0.59),
    "titulo":  (1.80, 0.22, 9.73, 0.52),
    "sub":     (1.80, 0.74, 9.73, 0.28),
    "regua_y": 1.08,
}


def _luz(hexa):
    """Luminância relativa (0 escuro … 1 claro).

    É o que decide se texto branco continua branco: sobre o azul da capa,
    sim; sobre o fundo branco novo, ele sumiria.
    """
    r, g, b = (int(hexa[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _formas_planas(shapes):
    """Achata grupos. Repintar só o primeiro nível deixaria forma escura
    dentro de grupo, e ela apareceria como um retângulo preto no meio do
    slide branco."""
    for sh in shapes:
        if sh.shape_type is not None and str(sh.shape_type).startswith("GROUP"):
            yield from _formas_planas(sh.shapes)
        else:
            yield sh


def _hex_do_fill(obj):
    """Cor sólida da forma ou da célula; None quando não é sólida."""
    try:
        f = obj.fill
        if f.type is None or not str(f.type).startswith("SOLID"):
            return None
        return str(f.fore_color.rgb).upper()
    except Exception:
        return None      # cor de tema, gradiente, imagem: não se mexe


def _pintar(obj, hexa):
    try:
        obj.fill.solid(); obj.fill.fore_color.rgb = _rgb(hexa)
        return True
    except Exception:
        return False


def _tem_link(sh):
    """Botão = forma com link, na forma ou dentro do run.

    Os dois casos existem no template e é por isso que se olha os dois:
    o menu principal liga a forma, os itens ligam o texto.
    """
    try:
        if sh.click_action.target_slide is not None:
            return True
    except Exception:
        pass
    if sh.has_text_frame:
        for par in sh.text_frame.paragraphs:
            for r in par.runs:
                rPr = r._r.find(f"{_A_NS}rPr")
                if rPr is not None and rPr.find(f"{_A_NS}hlinkClick") is not None:
                    return True
    return False


def _cartao_atras(sh, formas):
    """Forma preenchida que contém a caixa de texto — o cartão do botão.

    No template o texto do botão mora numa caixa SEM preenchimento em cima
    de um retângulo colorido. Quem precisa mudar de cor é o retângulo.
    """
    if _hex_do_fill(sh) is not None or sh.left is None or sh.top is None:
        return None
    cx = sh.left + (sh.width or 0) / 2
    cy = sh.top + (sh.height or 0) / 2
    melhor, area = None, None
    for o in formas:
        if o is sh or o.left is None or o.top is None: continue
        if _hex_do_fill(o) is None: continue
        if not (o.left <= cx <= o.left + (o.width or 0)): continue
        if not (o.top <= cy <= o.top + (o.height or 0)): continue
        a = (o.width or 0) * (o.height or 0)
        if area is None or a < area:      # o menor que contém: o cartão
            melhor, area = o, a
    return melhor


def _repintar_texto(tf, fundo, forcar=None):
    """Repinta os runs sabendo o que ficou ATRÁS deles."""
    n = 0
    escuro = _luz(fundo or ANGLO["fundo"]) < 0.5
    for par in tf.paragraphs:
        for r in par.runs:
            try:
                if r.font.color.type is None: continue
                hx = str(r.font.color.rgb).upper()
            except Exception:
                continue                  # cor de tema: deixa como está
            if forcar is not None:
                novo = forcar
            elif hx == "FFFFFF":
                # Branco só continua branco sobre fundo escuro.
                novo = None if escuro else ANGLO["texto"]
            else:
                novo = ANGLO_TEXTOS.get(hx)
                if novo and escuro:
                    novo = ANGLO_SOBRE_ESCURO.get(novo, novo)
            if novo and novo != hx:
                try:
                    r.font.color.rgb = _rgb(novo)
                    r.font.name = ANGLO["fonte"]
                    n += 1
                except Exception:
                    pass
    return n


def _repintar_linha(sh, cor=None):
    try:
        if sh.line.color.type is None: return 0
        hx = str(sh.line.color.rgb).upper()
    except Exception:
        return 0
    novo = cor or ANGLO_FUNDOS.get(hx) or ANGLO_TEXTOS.get(hx)
    if not novo or novo == hx: return 0
    try:
        sh.line.color.rgb = _rgb(novo); return 1
    except Exception:
        return 0


def _repintar_tabela(tbl):
    n = 0
    for row in tbl.rows:
        for cel in row.cells:
            hx = _hex_do_fill(cel)
            novo = ANGLO_FUNDOS.get(hx) if hx else None
            if novo and _pintar(cel, novo): n += 1
            _repintar_texto(cel.text_frame, novo or hx)
    # As bordas da célula não passam pelo python-pptx: moram em lnL/lnR/
    # lnT/lnB dentro do tcPr. Sem tratá-las aqui, sobra a grade azul-escura
    # do template desenhada por cima do fundo branco — foi o que apareceu
    # no primeiro teste, 1248 traços que a repintura das formas não pegava.
    for borda in ("lnL", "lnR", "lnT", "lnB"):
        for ln in tbl._tbl.iter(f"{_A_NS}{borda}"):
            for cor in ln.iter(f"{_A_NS}srgbClr"):
                hx = (cor.get("val") or "").upper()
                alvo = ANGLO_FUNDOS.get(hx) or ANGLO_TEXTOS.get(hx)
                if alvo and alvo != hx:
                    cor.set("val", alvo); n += 1
    return n


def _repintar_grafico(grafico):
    """Gráfico nativo: as cores vivem no XML da parte do gráfico, fora do
    alcance das formas do slide. Sem isto o gráfico continua com texto
    claro e grade escura sobre o cartão branco."""
    from pptx.oxml.ns import qn
    n = 0
    for el in grafico._chartSpace.iter():
        if el.tag != qn("a:srgbClr"): continue
        hx = (el.get("val") or "").upper()
        novo = ANGLO_FUNDOS.get(hx) or ANGLO_TEXTOS.get(hx)
        if novo and novo != hx:
            el.set("val", novo); n += 1
    return n


def _ja_tem_marca(s):
    """Slide que já tem a marca não leva um segundo logo em cima do
    primeiro."""
    from pptx.util import Emu
    for sh in s.shapes:
        if sh.name in ("anglo_marca", "anglo_regua"):
            return True
        if "PICTURE" in str(sh.shape_type) and sh.left is not None \
           and Emu(sh.left).inches < 1.75 and Emu(sh.top).inches < 1.0:
            return True
    return False


def _cabecalho_anglo(s, capa):
    """Abre espaço para o logo e pinta título e subtítulo."""
    from pptx.util import Inches, Emu
    if capa:
        return 0
    cands = []
    for sh in s.shapes:
        if not sh.has_text_frame or not sh.text_frame.text.strip(): continue
        if sh.left is None or sh.top is None or sh.width is None: continue
        if Emu(sh.left).inches > 1.0 or Emu(sh.width).inches < 6.0: continue
        if Emu(sh.top).inches >= 1.4: continue
        cands.append(sh)
    if not cands:
        return 0
    cands.sort(key=lambda sh: sh.top)
    tit = cands[0]
    sub = cands[1] if len(cands) > 1 and Emu(cands[1].top).inches >= 0.8 else None
    # A altura é imposta, não herdada: as caixas do template têm 0,60"/0,32"
    # e as dos slides técnicos 0,60"/0,35". Com o texto centrado na vertical,
    # herdar altura deixaria os dois cabeçalhos em linhas diferentes.
    x, y, w, h = ANGLO_SEMANAL["titulo"]
    tit.left, tit.top, tit.width, tit.height = (Inches(x), Inches(y),
                                                Inches(w), Inches(h))
    _repintar_texto(tit.text_frame, ANGLO["fundo"], forcar=ANGLO["azul"])
    if sub is not None:
        x, y, w, h = ANGLO_SEMANAL["sub"]
        sub.left, sub.top, sub.width, sub.height = (Inches(x), Inches(y),
                                                    Inches(w), Inches(h))
        _repintar_texto(sub.text_frame, ANGLO["fundo"], forcar=ANGLO["suave"])
    return 1


def _cabe_regua(s):
    """A régua só entra se não cruzar nada.

    O template é do cliente e ele edita: se alguém subir uma tabela para
    dentro da faixa, é melhor o slide sair sem régua do que com uma barra
    azul riscando o conteúdo.
    """
    from pptx.util import Emu
    y0 = ANGLO_SEMANAL["regua_y"]
    y1 = y0 + ANGLO["regua"][2]
    for sh in s.shapes:
        if sh.name in ("anglo_marca", "anglo_regua"): continue
        if sh.top is None: continue
        t = Emu(sh.top).inches
        if t + Emu(sh.height or 0).inches > y0 and t < y1:
            return False
    return True


def _marca_no_slide(s, capa):
    from pptx.util import Inches
    from pptx.enum.shapes import MSO_SHAPE
    if _ja_tem_marca(s):
        return 0
    n = 0
    if capa:
        lg, (x, y, w, h) = _logo_anglo(branco=True), ANGLO["capa_logo"]
    else:
        lg, (x, y, w, h) = _logo_anglo(), ANGLO_SEMANAL["logo"]
    if lg:
        pic = s.shapes.add_picture(lg, Inches(x), Inches(y), Inches(w), Inches(h))
        pic.name = "anglo_marca"; n += 1
    if not capa and _cabe_regua(s):
        rx, rw, rh = ANGLO["regua"]
        rg = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(rx),
                                Inches(ANGLO_SEMANAL["regua_y"]),
                                Inches(rw), Inches(rh))
        rg.fill.solid(); rg.fill.fore_color.rgb = _rgb(ANGLO["azul"])
        rg.line.fill.background(); rg.shadow.inherit = False
        rg.name = "anglo_regua"; n += 1
    return n


def aplicar_identidade_anglo(p):
    """Repinta o deck inteiro na identidade Anglo, no lugar.

    Devolve um resumo do que mudou — serve de prova nos testes e no log:
    "não mexeu em nada" e "mexeu em tudo" têm de ser distinguíveis.
    """
    n = {"slides": 0, "formas": 0, "runs": 0, "tabelas": 0,
         "graficos": 0, "marcas": 0}
    for i, s in enumerate(p.slides):
        capa = (i == 0)          # mesma premissa do preenchimento do período
        fundo_novo = ANGLO["azul"] if capa else ANGLO["fundo"]
        try:
            s.background.fill.solid()
            s.background.fill.fore_color.rgb = _rgb(fundo_novo)
        except Exception:
            pass

        formas = list(_formas_planas(s.shapes))

        # Botões. O de voltar fica azul com texto branco; item de menu fica
        # cartão claro com texto azul.
        voltar, menu = set(), set()
        for sh in formas:
            if not _tem_link(sh): continue
            txt = sh.text_frame.text.strip() if sh.has_text_frame else ""
            grupo = voltar if txt[:1] in "◂▤" else menu
            grupo.add(id(sh))
            atras = _cartao_atras(sh, formas)
            if atras is not None:
                grupo.add(id(atras))

        for sh in formas:
            hx = _hex_do_fill(sh)
            if hx is not None:
                if   id(sh) in voltar: novo = ANGLO["azul"]
                elif id(sh) in menu:   novo = ANGLO["cartao"]
                elif capa:             novo = ANGLO_CAPA.get(hx)
                else: novo = ANGLO_FUNDOS.get(hx) or ANGLO_TEXTOS.get(hx)
                if novo and novo != hx and _pintar(sh, novo):
                    n["formas"] += 1
            n["formas"] += _repintar_linha(
                sh, ANGLO["linha"] if id(sh) in menu else None)

        for sh in formas:
            if sh.has_table:
                n["tabelas"] += _repintar_tabela(sh.table)
            if getattr(sh, "has_chart", False):
                n["graficos"] += _repintar_grafico(sh.chart)
            if not sh.has_text_frame: continue
            fundo = _hex_do_fill(sh)
            if fundo is None:
                atras = _cartao_atras(sh, formas)
                fundo = _hex_do_fill(atras) if atras is not None else fundo_novo
            forcar = (ANGLO["azul"] if id(sh) in menu else
                      ANGLO["fundo"] if id(sh) in voltar else None)
            n["runs"] += _repintar_texto(sh.text_frame, fundo, forcar)

        n["marcas"] += _cabecalho_anglo(s, capa)
        n["marcas"] += _marca_no_slide(s, capa)
        n["slides"] += 1
    return n


# ──────────────────────────────────────────────────────────────
# SERVIDOR WEB — página simples com seletor de período
# ──────────────────────────────────────────────────────────────
def painel_html(cfg):
    """Serve o painel HTML próprio (painel/visao-geral.html).

    Lido do disco a cada requisição de propósito: assim dá para editar o
    CSS/JS e recarregar o navegador sem reiniciar o exporter — que é o ponto
    de ter o painel num arquivo separado em vez de embutido no Python.
    """
    caminho = Path(cfg.get("relatorio", "painel_html",
                           fallback="painel/visao-geral.html"))
    if not caminho.exists():
        return ("<!DOCTYPE html><meta charset='utf-8'>"
                "<body style='font-family:system-ui;background:#0d2052;"
                "color:#e8edf7;padding:40px;line-height:1.6'>"
                f"<h2>Painel não encontrado</h2><p><code>{caminho}</code></p>"
                "<p>Coloque o arquivo nesse caminho ou ajuste "
                "<code>[relatorio] painel_html</code> no config.ini.</p>"
                "<p>O painel faz parte da entrega, na pasta "
                "<code>painel/</code>.</p></body>")
    try:
        return caminho.read_text(encoding="utf-8")
    except Exception as e:
        return (f"<!DOCTYPE html><meta charset='utf-8'><body>"
                f"<pre>Erro ao ler {caminho}: {e}</pre></body>")


PAGINA_HTML = r"""<!DOCTYPE html>
<html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rede Rajant — Relatórios</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: Arial, Helvetica, sans-serif; background:#eef1f5;
         margin:0; padding:20px 14px 40px; color:#1f2d3d; }
  .card { max-width:560px; margin:22px auto; background:#fff; border-radius:10px;
          box-shadow:0 2px 12px rgba(0,0,0,.08); overflow:hidden; }
  .top { background:#1F4E79; color:#fff; padding:18px 22px;
         display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; }
  .top h1 { margin:0; font-size:19px; flex:1; }
  .top a { color:#cfe0f2; font-size:13px; text-decoration:none; }
  .top a:hover { color:#fff; text-decoration:underline; }
  .body { padding:20px 22px; }
  label { display:block; font-size:13px; font-weight:bold; margin:0 0 6px; }
  .fraco { font-size:12px; color:#66748a; font-weight:normal; }
  input[type=text], input[type=date] {
          width:100%; padding:9px 10px; border:1px solid #c7d0da;
          border-radius:6px; font-size:14px; font-family:inherit; background:#fff; }
  .linha { display:flex; gap:12px; flex-wrap:wrap; }
  .linha > div { flex:1; min-width:130px; }
  hr { border:0; border-top:1px solid #e3e8ee; margin:22px 0 18px; }
  button.b { border:0; border-radius:7px; font:inherit; font-weight:bold;
             cursor:pointer; padding:11px 14px; font-size:14px; width:100%; }
  .ger  { background:#1F4E79; color:#fff; } .ger:hover { background:#163a5c; }
  .ppt  { background:#2E75B6; color:#fff; } .ppt:hover { background:#245e92; }
  .neut { background:#e3e8ee; color:#33404f; width:auto !important; }
  .neut:hover { background:#d5dde6; }
  button.b:disabled { opacity:.55; cursor:wait; }
  .presets { display:flex; gap:7px; flex-wrap:wrap; margin-bottom:16px; }
  .presets button { border:1px solid #c7d0da; background:#fff; color:#33404f;
          border-radius:7px; padding:8px 12px; font-size:12.5px; font-weight:bold;
          cursor:pointer; font-family:inherit; }
  .presets button.on { background:#2E75B6; color:#fff; border-color:#2E75B6; }
  .caixa-erro { background:#FFC7CE; color:#9C0006; padding:10px 12px;
          border-radius:6px; font-size:13px; margin:0 0 14px; display:none; }
  .nota { font-size:12px; color:#66748a; margin:10px 0 0; }
  @media (max-width:420px) {
    .linha > div { min-width:100%; }
    .body { padding:16px 14px; }
  }
</style>
</head>
<body>
<div class="card">
  <div class="top">
    <h1>Rede Rajant — Relatórios</h1>
    <a href="/painel">Painel</a>
  </div>
  <div class="body">
    <div class="caixa-erro" id="erro"></div>

    <label>Período</label>
    <div class="presets" id="presets">
      <button type="button" data-d="7" class="on">Últimos 7 dias</button>
      <button type="button" data-d="30">Últimos 30 dias</button>
    </div>
    <div class="linha">
      <div><label class="fraco">De</label><input type="date" id="ini"></div>
      <div><label class="fraco">Até</label><input type="date" id="fim"></div>
    </div>
    <p class="nota"><label style="display:inline;font-weight:normal">
      <input type="checkbox" id="demo"> Dados de demonstração (sem Prometheus)
    </label></p>
    <button class="b ger" id="btnXls" onclick="gerar('xlsx')"
            style="margin-top:14px">Gerar e baixar Excel</button>
    <button class="b ppt" id="btnPpt" onclick="gerar('pptx')"
            style="margin-top:10px">Gerar e baixar PPT</button>

    <hr>
    <label>Diagnóstico de BreadCrumb</label>
    <div class="linha">
      <div><input type="text" id="ipDiag" placeholder="10.188.99.10"></div>
      <div style="flex:0 0 auto"><button class="b neut" type="button"
              onclick="diagnostico()">Abrir</button></div>
    </div>
    <p class="nota">Lê o estado do rádio e mostra portas ethernet e o trecho
      bruto. Somente leitura.</p>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
const q = (o) => Object.entries(o).map(([k,v]) => k+'='+encodeURIComponent(v)).join('&');

function erro(msg) {
  const e = $('#erro');
  if (!msg) { e.style.display = 'none'; return; }
  e.textContent = msg; e.style.display = 'block';
}

const iso = d => new Date(d.getTime() - d.getTimezoneOffset()*60000)
                   .toISOString().slice(0,10);
function periodo(dias) {
  $('#ini').value = iso(new Date(Date.now() - dias*864e5));
  $('#fim').value = iso(new Date());
  document.querySelectorAll('#presets button').forEach(b =>
    b.classList.toggle('on', Number(b.dataset.d) === dias));
}
$('#presets').addEventListener('click', ev => {
  const b = ev.target.closest('button'); if (b) periodo(Number(b.dataset.d));
});
['#ini', '#fim'].forEach(id => $(id).addEventListener('change', () =>
  document.querySelectorAll('#presets button').forEach(b => b.classList.remove('on'))));
periodo(7);

async function gerar(fmt) {
  erro('');
  const ini = $('#ini').value, fim = $('#fim').value;
  if (!ini || !fim) return erro('Selecione as duas datas.');
  if (ini > fim)    return erro('A data inicial não pode ser depois da final.');
  const btn = fmt === 'pptx' ? $('#btnPpt') : $('#btnXls');
  const orig = btn.textContent;
  btn.disabled = true; btn.textContent = 'Gerando…';
  try {
    const p = {ini: ini, fim: fim, demo: $('#demo').checked ? '1' : '0', fmt: fmt};
    const r = await fetch('/gerar?' + q(p));
    if (!r.ok) throw new Error(await r.text());
    await baixar(r, 'relatorio.' + fmt);
  } catch (e) {
    erro(e instanceof TypeError
      ? 'Sem resposta do servidor. Verifique se o serviço está rodando.'
      : 'Erro ao gerar: ' + e.message);
  } finally { btn.disabled = false; btn.textContent = orig; }
}
async function baixar(r, padrao) {
  const cd = r.headers.get('Content-Disposition') || '';
  const m = cd.match(/filename="?([^"]+)"?/);
  const blob = await r.blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = m ? m[1] : padrao;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(a.href);
}
function diagnostico() {
  const ip = ($('#ipDiag').value || '').trim();
  if (!/^\d{1,3}(\.\d{1,3}){3}$/.test(ip)) return erro('IP inválido.');
  erro('');
  window.open('/diagnostico?' + q({ip: ip}), '_blank');
}
$('#ipDiag').addEventListener('keydown', ev => { if (ev.key === 'Enter') diagnostico(); });
</script>
</body></html>
"""

def criar_handler(cfg):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a): pass  # silencia log padrão

        def _envia(self, codigo, corpo, ctype="text/html; charset=utf-8", extra=None):
            self.send_response(codigo)
            self.send_header("Content-Type", ctype)
            if extra:
                for k, v in extra.items(): self.send_header(k, v)
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                self._envia(200, PAGINA_HTML.encode("utf-8")); return
            # ── Painel HTML próprio ──
            if parsed.path in ("/painel", "/painel.html"):
                self._envia(200, painel_html(cfg).encode("utf-8")); return

            # ── Proxy do Prometheus para o painel ──
            # Passar pelo exporter evita CORS no navegador e mantém a URL do
            # Prometheus no servidor — o painel pode rodar em qualquer lugar
            # sem expor a infraestrutura nem exigir configuração no cliente.
            if parsed.path == "/api/q":
                q = urllib.parse.parse_qs(parsed.query)
                consulta = (q.get("q", [""])[0] or "").strip()
                if not consulta:
                    self._envia(400, b'{"erro":"consulta vazia"}',
                                "application/json; charset=utf-8"); return
                try:
                    prom = Prometheus(cfg.get("relatorio", "prometheus_url"))
                    if q.get("t", ["instant"])[0] == "range":
                        agora = time.time()
                        try:    janela = float(q.get("janela", ["3600"])[0])
                        except ValueError: janela = 3600.0
                        janela = max(60.0, min(janela, 90*86400))
                        try:    passo = int(float(q.get("passo", ["60"])[0]))
                        except ValueError: passo = 60
                        passo = max(15, min(passo, 3600))
                        res = prom.range(consulta, agora-janela, agora, f"{passo}s")
                    else:
                        res = prom.instant(consulta)
                    corpo = json.dumps({"ok": True, "res": res})
                except Exception as e:
                    corpo = json.dumps({"ok": False, "erro": str(e)})
                self._envia(200, corpo.encode("utf-8"),
                            "application/json; charset=utf-8")
                return

            if parsed.path == "/diagnostico":
                q = urllib.parse.parse_qs(parsed.query)
                ip = (q.get("ip") or [""])[0].strip()
                if not ip:
                    self._envia(400, b"Informe ?ip=10.x.x.x", "text/plain; charset=utf-8"); return
                try:
                    porta_api = cfg.getint("rede","porta_bcapi", fallback=2300)
                    role  = papel_bcapi(cfg.get("rede","role", fallback="VIEW"))
                    senha = cfg.get("rede","password", fallback="")
                    bc = exigir_rajant_api()(host=ip, port=porta_api, role=role, password=senha)
                    if not bc.reachable():    raise RuntimeError("BC nao alcancavel")
                    try:
                        autenticar(bc)
                        raw = ler_estado(bc)
                    finally:
                        fechar_sessao(bc)
                    dados = parse_state(raw)
                    txt = json.dumps(raw, indent=2) if isinstance(raw, dict) else str(raw)
                    txt = txt.replace('\\n','\n').replace('\\"','"')
                    # recorta os blocos wired/ethernet do texto bruto
                    trechos = []
                    for nome in ('wired','ethernet','interface','port'):
                        for b in extrair_blocos(txt, nome)[:4]:
                            trechos.append(f"===== bloco '{nome}' =====\n{b[:1500]}")
                    eths = dados.get("ethernet", [])
                    resumo = [
                        f"BC: {dados['sistema'].get('nome','?')}  ({ip})",
                        f"Radios: {len(dados.get('radios',[]))}",
                        f"Portas ethernet detectadas: {len(eths)}", ""]
                    for e in eths:
                        resumo.append(
                            f"  porta={e['nome']:8s} link={e['link']} "
                            f"rx_bytes={e['rx_bytes']} tx_bytes={e['tx_bytes']} "
                            f"rx_pkts={e['rx_pkts']} peers={e.get('peers_eth',0)}")
                    if not eths:
                        resumo.append("  NENHUMA porta encontrada no state.")
                    resumo += ["", "─"*60,
                               "TRECHO BRUTO (envie este conteudo p/ analise se os "
                               "contadores estiverem zerados):", ""]
                    corpo = "\n".join(resumo + trechos)
                    self._envia(200, corpo.encode("utf-8"), "text/plain; charset=utf-8")
                except Exception as e:
                    self._envia(500, f"Falha no diagnostico: {e}".encode("utf-8"),
                                "text/plain; charset=utf-8")
                return
            if parsed.path == "/gerar":
                q = urllib.parse.parse_qs(parsed.query)
                try:
                    ini = datetime.strptime(q["ini"][0], "%Y-%m-%d").date()
                    fim = datetime.strptime(q["fim"][0], "%Y-%m-%d").date()
                    demo = q.get("demo", ["0"])[0] == "1"
                    fmt  = q.get("fmt",  ["xlsx"])[0]
                    if ini > fim:
                        raise ValueError("data inicial após a final")
                    if (fim - ini).days > 366:
                        raise ValueError("período máximo de 366 dias")

                    log.info(f"[web] Relatório {fmt} {ini}..{fim} demo={demo}")
                    if fmt == "pptx":
                        dados, nome = gerar_ppt(cfg, ini, fim, demo=demo)
                        ctype = ("application/vnd.openxmlformats-officedocument"
                                 ".presentationml.presentation")
                    else:
                        dados, nome = montar_relatorio(cfg, ini, fim, demo=demo)
                        ctype = ("application/vnd.openxmlformats-officedocument"
                                 ".spreadsheetml.sheet")
                    cab = {"Content-Disposition": f'attachment; filename="{nome}"'}
                    self._envia(200, dados, ctype, cab)
                except Exception as e:
                    log.error(f"[web] Falha: {e}")
                    self._envia(500, str(e).encode("utf-8"), "text/plain; charset=utf-8")
                return
            self._envia(404, b"Nao encontrado", "text/plain")
    return Handler

def iniciar_servidor_relatorios(cfg):
    porta = cfg.getint("relatorio", "porta_relatorio")
    srv = HTTPServer(("0.0.0.0", porta), criar_handler(cfg))
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    log.info(f"Servidor de relatorios em http://0.0.0.0:{porta}/  "
             f"(Prometheus: {cfg.get('relatorio','prometheus_url')})")
    return srv

# ══════════════════════════════════════════════════════════════
# MAIN — inicia exporter + servidor de relatórios juntos
# ══════════════════════════════════════════════════════════════
def main():
    import servico_rajant
    # Executável: config.ini, logs e relatórios ficam AO LADO dele,
    # de onde quer que tenha sido chamado (atalho, outra pasta). Antes iam
    # para a pasta corrente.
    if getattr(sys, "frozen", False):
        os.chdir(servico_rajant.pasta_do_programa())
    # Dois cliques (nenhum argumento): exportador, Prometheus e Grafana em
    # segundo plano, e a janela fecha. Qualquer argumento é linha de
    # comando, como sempre foi.
    acoes = ([a for a in sys.argv[1:] if a in servico_rajant.ACOES]
             or ([] if sys.argv[1:] else ["--iniciar"]))
    if acoes:
        sys.exit(servico_rajant.executar(acoes[0], carregar_config))
    ap = argparse.ArgumentParser(
        description="Rajant Monitor — exporter Prometheus + relatorios Excel/PPT",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="sem argumento (dois cliques): exportador + Prometheus + Grafana\n"
               "em segundo plano, e a janela fecha.\n"
               "  --status             o que esta rodando\n"
               "  --parar              para tudo\n"
               "  --servico            o supervisor dos tres, nesta janela")
    ap.add_argument("--exportador", action="store_true",
                    help="So o exportador e a pagina web, nesta janela (o que o "
                         "segundo plano roda).")
    ap.add_argument("--seed"); ap.add_argument("--password")
    # Maiúsculas ou minúsculas: a rajant-api só conhece VIEW/CO/ADMIN e
    # `papel_bcapi` normaliza; a lista antiga (VIEW, admin, co) recusava "CO".
    ap.add_argument("--role", default=None, type=str.upper,
                    choices=["VIEW", "CO", "ADMIN", "LOCAL"])
    ap.add_argument("--port", type=int)
    ap.add_argument("--interval", type=int)
    ap.add_argument("--metrics-port", type=int)
    ap.add_argument("--porta-relatorio", type=int,
                    help="Porta da pagina web de relatorios (padrao config: 8010)")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--dump-state", metavar="IP",
                    help="Conecta no IP, salva o state bruto e sai (diagnostico de parsing).")
    ap.add_argument("--diagnostico-eth", metavar="IP",
                    help="Diagnostico focado na porta ethernet: mostra blocos, campos e "
                         "o que o parser extraiu. Salva diagnostico_eth_<ip>.txt")
    ap.add_argument("--gerar-relatorio", nargs=2, metavar=("INI","FIM"),
                    help="Gera um relatorio .xlsx (AAAA-MM-DD AAAA-MM-DD) e sai.")
    ap.add_argument("--demo", action="store_true",
                    help="Usa dados sinteticos ao gerar relatorio via --gerar-relatorio.")
    ap.add_argument("--sem-relatorios", action="store_true",
                    help="Sobe apenas o exporter, sem o servidor web de relatorios.")
    args = ap.parse_args()

    cfg = carregar_config()
    cfg = cfg_relatorio(cfg)  # garante a secao [relatorio] com defaults

    # ── Configuracao do exporter (secoes [rede] / [coleta] / [servidor]) ──
    seeds_str = args.seed      or cfg.get("rede","seeds",       fallback="")
    password  = args.password  or cfg.get("rede","password",    fallback="")
    role      = args.role      or cfg.get("rede","role",        fallback="VIEW")
    port      = args.port      or cfg.getint("rede","porta_bcapi",          fallback=2300)
    interval  = args.interval  or cfg.getint("coleta","intervalo_segundos", fallback=60)
    max_thr   = cfg.getint("coleta","max_threads",          fallback=30)
    timeout   = cfg.getint("coleta","timeout_conexao",      fallback=15)
    tentativas= cfg.getint("coleta","tentativas_por_bc",    fallback=3)
    falhas_lim= cfg.getint("coleta","falhas_antes_offline", fallback=3)
    manter_s  = cfg.getint("coleta","segurar_serie_s",      fallback=0)
    redesc    = cfg.getint("coleta","redescoberta_ciclos",  fallback=10)
    # Coleta acelerada dos móveis. O padrão de nome
    # vem de [relatorio] padrao_movel — mesma fonte que classifica os
    # equipamentos nos relatórios, para não divergirem.
    int_moveis= cfg.getint("coleta","intervalo_moveis_segundos", fallback=0)
    padr_movel= cfg.get("relatorio","padrao_movel", fallback="")
    mport     = args.metrics_port or cfg.getint("servidor","porta_metrics", fallback=8000)
    log_arq   = cfg.get("log","arquivo",  fallback="rajant_monitor.log")
    log_niv   = "DEBUG" if args.debug else cfg.get("log","nivel", fallback="INFO")

    if args.porta_relatorio:
        cfg.set("relatorio", "porta_relatorio", str(args.porta_relatorio))

    global log
    log = setup_logging(log_arq, log_niv)

    if args.gerar_relatorio:
        ini = datetime.strptime(args.gerar_relatorio[0], "%Y-%m-%d").date()
        fim = datetime.strptime(args.gerar_relatorio[1], "%Y-%m-%d").date()
        dados, nome = montar_relatorio(cfg, ini, fim, demo=args.demo)
        outdir = Path("relatorios"); outdir.mkdir(exist_ok=True)
        (outdir / nome).write_bytes(dados)
        log.info(f"Relatorio gerado: {outdir / nome}")
        return

    seeds = [s.strip() for s in seeds_str.split(",") if s.strip()]

    # ── Diagnostico de state bruto (nao exige seeds) ──
    # ── Diagnostico de state bruto ──
    if args.diagnostico_eth:
        ipd = args.diagnostico_eth
        log.info(f"Diagnostico de ethernet em {ipd}...")
        bc = exigir_rajant_api()(host=ipd, port=port, role=role, password=password)
        if not bc.reachable():    log.error("Nao alcancavel"); sys.exit(2)
        try: autenticar(bc)
        except Exception as e: log.error(f"Autenticacao falhou: {e}"); sys.exit(3)
        try:
            raw = ler_estado(bc)
        except Exception as e:
            log.error(f"Leitura do State falhou: {type(e).__name__}: {e}"); sys.exit(4)
        finally:
            fechar_sessao(bc)
        rel = diagnostico_ethernet(raw)
        arq = f"diagnostico_eth_{ipd.replace('.','_')}.txt"
        Path(arq).write_text(rel, encoding="utf-8")
        print("\n" + rel + "\n")
        log.info(f"Salvo em {arq} — envie este arquivo para analise.")
        return

    if args.dump_state:
        ipd = args.dump_state
        log.info(f"Coletando state bruto de {ipd}...")
        bc = exigir_rajant_api()(host=ipd, port=port, role=role, password=password)
        if not bc.reachable():    log.error("Nao alcancavel"); sys.exit(2)
        try: autenticar(bc)
        except Exception as e: log.error(f"Autenticacao falhou: {e}"); sys.exit(3)
        try:
            raw = ler_estado(bc)
        except Exception as e:
            log.error(f"Leitura do State falhou: {type(e).__name__}: {e}"); sys.exit(4)
        finally:
            fechar_sessao(bc)
        txt = json.dumps(raw, indent=2) if isinstance(raw, dict) else str(raw)
        arq = f"state_{ipd.replace('.','_')}.txt"
        Path(arq).write_text(txt, encoding="utf-8")
        dados = parse_state(raw)
        log.info(f"State salvo em {arq}")
        log.info(f"Parse: {len(dados['radios'])} radios | "
                 f"{len(dados['ethernet'])} portas eth "
                 f"({', '.join(e['nome'] for e in dados['ethernet']) or 'NENHUMA'})")
        return

    if not password: log.error("Senha nao configurada (secao [rede], chave password)."); sys.exit(1)

    descoberta_on = cfg.getboolean("coleta", "descoberta", fallback=True)
    padrao_tag = None
    if cfg.getboolean("coleta", "filtrar_tag", fallback=True):
        padrao_tag = cfg.get("coleta", "padrao_tag",
                             fallback=PADRAO_TAG_PADRAO).strip() or PADRAO_TAG_PADRAO
        try:
            re.compile(padrao_tag)
        except re.error as e:
            log.error(f"[coleta] padrao_tag invalido ({e}); usando o padrao "
                      f"{PADRAO_TAG_PADRAO}")
            padrao_tag = PADRAO_TAG_PADRAO
    for velha, nova in (("manter_ultimo_valor_s", "segurar_serie_s"),
                        ("somente_com_tag", "filtrar_tag")):
        if cfg.has_option("coleta", velha):
            log.info(f"[coleta] '{velha}' nao e mais usado (agora: '{nova}', "
                     f"padrao {cfg.get('coleta', nova, fallback='o do programa')})")
    for velha in ("usar_cache", "falhas_para_remover"):
        if cfg.has_option("coleta", velha):
            log.info(f"[coleta] '{velha}' nao e mais usado: os IPs estao no "
                     f"codigo (REDE_CONHECIDA), sem cache")
    if Path(CACHE_ANTIGO).exists():
        log.info(f"[coleta] {CACHE_ANTIGO} nao e mais lido nem gravado; "
                 f"pode apagar")

    start_http_server(mport)

    # ── Servidor web de relatorios (thread separada) ──
    if not args.sem_relatorios:
        try:
            iniciar_servidor_relatorios(cfg)
        except Exception as e:
            log.error(f"Nao foi possivel iniciar o servidor de relatorios: {e}")

    log.info("")
    log.info("="*60)
    log.info("  RAJANT MONITOR — EXPORTER + RELATORIOS")
    log.info("="*60)
    log.info(f"  Lista:     {len(REDE_CONHECIDA)} IPs no codigo"
             + (f" + {len(set(seeds) - set(REDE_CONHECIDA))} seed(s) fora dela"
                if set(seeds) - set(REDE_CONHECIDA) else ""))
    log.info(f"  Descoberta:{'ligada' if descoberta_on else 'DESLIGADA (so a lista e os seeds)'}")
    log.info("  Tag:       " + (f"so nomes no formato {padrao_tag}"
                                 if padrao_tag else "SEM filtro (filtrar_tag = false)"))
    log.info("  Series:    " + (f"seguram {manter_s}s apos falha" if manter_s > 0
                                 else "saem na primeira falha"))
    log.info(f"  Metricas:  http://localhost:{mport}/metrics")
    if not args.sem_relatorios:
        log.info(f"  Relatorios: http://localhost:{cfg.getint('relatorio','porta_relatorio')}/")
    log.info(f"  Intervalo: {interval}s | Threads: {max_thr}")
    log.info("="*60)

    coletor = RajantCollector(
        seeds=seeds, role=role, password=password, port=port,
        interval=interval, max_threads=max_thr, timeout=timeout,
        tentativas=tentativas, falhas_limite=falhas_lim,
        manter_s=manter_s, redesc_ciclos=redesc,
        interval_moveis=int_moveis, padrao_movel=padr_movel,
        descoberta=descoberta_on, padrao_tag=padrao_tag,
    )
    coletor.run()

if __name__ == "__main__":
    main()
