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

import re, json, time, logging, argparse, threading, configparser, sys, math
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

# A rajant-api so e necessaria para FALAR com os radios. O gerador de
# relatorios a partir de arquivo do MeshMapper nao toca na rede, e sair
# com erro no import impediria de rodar quem so quer gerar KMZ e PPT numa
# maquina sem a biblioteca.
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
CACHE_FILE  = "rajant_ips_cache.json"

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
manter_ultimo_valor_s   = 300
redescoberta_ciclos     = 10
falhas_para_remover     = 20
# Descoberta pela malha: parte dos seeds e segue os peers de cada BC.
# false = coleta SÓ os IPs listados em [rede] seeds, sem seguir peer
# nenhum. Use quando a lista de equipamentos é conhecida e fixa: a
# descoberta acha tudo que responde na malha, inclusive o que não é da
# frota.
descoberta              = true
# Cache de IPs em disco (rajant_ips_cache.json). false = não lê nem
# escreve; a partida depende só dos seeds.
usar_cache              = true
# Só publica equipamento cujo nome case com um prefixo de
# [relatorio] prefixos_frota (CA, PA, PF, TT, EH, ERM, ERB...).
# Descarta o que a malha devolve sem nome reconhecível. O que for
# descartado aparece no log e em rajant_bc_sem_tag — nunca some calado.
somente_com_tag         = false
# Equipamentos móveis funcionam como sondas de site survey: a resolução
# espacial do mapa é velocidade × intervalo. A 25 km/h, 60 s dá ~420 m entre
# amostras — mais que o raio de interpolação padrão (300 m), o que abre
# buracos no heatmap. Coletar só os móveis mais rápido melhora o survey sem
# multiplicar o volume da frota inteira. 0 = mesmo intervalo para todos.
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
# CACHE DE IPs EM DISCO
# ══════════════════════════════════════════════════════════════
class CacheIPs:
    def __init__(self, caminho=CACHE_FILE, ativo=True):
        """`ativo=False` deixa o cache inerte: não lê nem escreve o disco.

        Quem tem a lista fixa dos próprios equipamentos não quer o cache —
        ele guarda o que a descoberta já encontrou um dia e ressuscita
        endereço que saiu da rede, mantendo equipamento fantasma na
        métrica muito depois de ele ter ido embora.
        """
        self.ativo   = ativo
        self.caminho = Path(caminho)
        self.lock    = threading.Lock()
        self._dados  = {}
        if ativo:
            self._carregar()
        else:
            log.info("[cache] desligado: partida so pelos seeds")

    def _carregar(self):
        if self.caminho.exists():
            try:
                self._dados = json.loads(self.caminho.read_text(encoding="utf-8"))
                log.info(f"[cache] {len(self._dados)} IPs carregados de {self.caminho}")
            except Exception as e:
                log.warning(f"[cache] Erro ao carregar: {e}")
                self._dados = {}

    def _salvar(self):
        # Desligado: guarda em memoria para o ciclo, nao toca no disco.
        if not self.ativo: return
        try:
            self.caminho.write_text(
                json.dumps(self._dados, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception: pass

    def adicionar(self, ip, nome=""):
        with self.lock:
            if ip not in self._dados:
                log.debug(f"[cache] Novo: {ip} ({nome})")
            self._dados[ip] = {"nome": nome or ip, "ultima_vez": time.time(), "falhas": 0}
            self._salvar()

    def registrar_falha(self, ip, limite=20):
        with self.lock:
            if ip not in self._dados: return
            self._dados[ip]["falhas"] = self._dados[ip].get("falhas", 0) + 1
            if limite > 0 and self._dados[ip]["falhas"] >= limite:
                log.warning(f"[cache] {ip} removido ({self._dados[ip]['falhas']} falhas)")
                del self._dados[ip]
            self._salvar()

    def registrar_sucesso(self, ip, nome=""):
        with self.lock:
            if ip in self._dados:
                self._dados[ip]["falhas"] = 0
                self._dados[ip]["ultima_vez"] = time.time()
                if nome: self._dados[ip]["nome"] = nome
            self._salvar()

    def todos_ips(self):
        with self.lock: return set(self._dados.keys())
    def nome(self, ip):
        with self.lock: return self._dados.get(ip, {}).get("nome", ip)
    def total(self):
        with self.lock: return len(self._dados)

# ══════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════
def setup_logging(arquivo, nivel):
    fmt  = "%(asctime)s [%(levelname)s] %(message)s"
    lvl  = getattr(logging, nivel.upper(), logging.INFO)
    hdls = [logging.StreamHandler(sys.stdout)]
    if arquivo:
        try: hdls.append(logging.FileHandler(arquivo, encoding="utf-8"))
        except Exception: pass
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
m_cache_total  = Gauge("rajant_cache_total",  "IPs no cache", [])
# Equipamentos que responderam mas foram descartados por nao terem
# prefixo de frota. Existe para o descarte ser AUDITAVEL: sem isto,
# um erro no filtro sumiria com equipamento sem deixar rastro.
m_bc_sem_tag   = Gauge("rajant_bc_sem_tag",  "BCs ignorados por nao ter tag")
m_cache_online = Gauge("rajant_cache_online", "IPs online",   [])
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
        self.ultima_ts     = 0.0
        self.ultima_dados  = None

    def ok(self, dados, ts):
        self.falhas_consec = 0
        self.ts_ultimo_ok  = ts
        self.ultima_ts     = ts
        self.ultima_dados  = dados

    def falha(self): self.falhas_consec += 1

    @property
    def offline(self): return self.falhas_consec >= self.falhas_limite

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

# ──────────────────────────────────────────────────────────────
# EXTRATOR DE BLOCOS COM BALANCEAMENTO DE CHAVES
# Regex não conta chaves aninhadas nem lida com indentação
# variável do protobuf text format. Este extrator percorre o
# texto contando '{' e '}' — funciona para qualquer indentação
# e qualquer nível de aninhamento (stats, sensors, apt...).
# ──────────────────────────────────────────────────────────────
def _parece_ip(txt):
    """True se o texto é apenas um IPv4 (nome de configuração ausente)."""
    return bool(re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", (txt or "").strip()))

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

def ping_qualidade(ip, n=4, timeout_s=1):
    """RTT médio (ms) e perda (%) numa única chamada de `ping`.

    Um `ping -c N` custa uma execução; N chamadas de ping_rtt() custam N
    processos. Com 150 rádios por ciclo de survey a diferença decide se a
    coleta cabe no intervalo.

    A perda só existe porque a BC API não a fornece: latência e perda vêm
    do ICMP do servidor, medindo o caminho centro→equipamento, que é o que
    a aplicação Dispatch enfrenta.

    Devolve (rtt_ms, perda_pct). Ambos None quando o ping nem rodou —
    None é "não medido"; perda 100 % é "medido e não respondeu".
    """
    try:
        if platform.system().lower() == "windows":
            cmd = ["ping", "-n", str(n), "-w", str(int(timeout_s*1000)), ip]
        else:
            cmd = ["ping", "-c", str(n), "-W", str(timeout_s), "-i", "0.25", ip]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=n*timeout_s + 5)
        out = (r.stdout or "") + (r.stderr or "")
    except Exception:
        return None, None

    perda = None
    for pat in (r"(\d+(?:\.\d+)?)%\s*packet loss",
                r"(\d+(?:\.\d+)?)%\s*de perda",
                r"Perdidos\s*=\s*\d+\s*\((\d+(?:\.\d+)?)%",
                r"Lost\s*=\s*\d+\s*\((\d+(?:\.\d+)?)%"):
        m = re.search(pat, out, re.I)
        if m:
            perda = float(m.group(1)); break

    rtt = None
    for pat in (r"=\s*[\d.]+/([\d.]+)/",          # rtt min/avg/max
                r"M[eé]dia\s*=\s*(\d+(?:\.\d+)?)\s*ms",
                r"Average\s*=\s*(\d+(?:\.\d+)?)\s*ms",
                r"[Tt]ime[=<]\s*(\d+(?:\.\d+)?)\s*ms",
                r"[Tt]empo[=<]\s*(\d+(?:\.\d+)?)\s*ms"):
        m = re.search(pat, out)
        if m:
            rtt = float(m.group(1)); break

    # Sem nenhuma resposta o `ping` pode não imprimir linha de estatística.
    if perda is None and rtt is None:
        perda = 100.0
    return rtt, perda


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
def publicar(ip, dados, est):
    s   = dados["sistema"]
    im  = dados["instamesh"]
    # CRÍTICO: bc nunca pode ser vazio
    # BCs sem nome configurado usam o IP como label 'bc'
    # Sem isso o label 'bc' some do Prometheus e a variável do Grafana fica vazia
    bc  = (s["nome"] or "").strip() or ip
    s["nome"] = bc
    lbc = {"bc": bc, "ip": ip}
    jd  = janela_bc(ip)

    # Registra evento de disponibilidade
    jd.registrar(True)

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
        if valor is None: return
        metrica.labels(**(labels or lbc)).set(valor)

    m_online.labels(**lbc).set(1)
    m_bc_info.labels(bc=bc, ip=ip, modelo=s.get("modelo",""),
                     firmware=s.get("firmware",""),
                     grupos=s.get("grupos","")).set(1)
    m_uptime.labels(**lbc).set(s["uptime_s"])
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
    m_gps_fix.labels(**lbc).set(s.get("gps_fix", 0))
    m_boot.labels(**lbc).set(s["boot"])
    m_reboot_needed.labels(**lbc).set(s["reboot"])
    m_bridge.labels(**lbc).set(1 if s["bridge"] else 0)
    m_apt_master.labels(**lbc).set(1 if s["apt_master"] else 0)
    pub(m_best_rate, s.get("best_rate"))
    pub(m_alertas,   s.get("alertas"))
    m_falhas.labels(**lbc).set(est.falhas_consec)
    m_ultima_coleta.labels(**lbc).set(est.ts_ultimo_ok)
    m_memoria.labels(**lbc).set(s["mem_kb"])

    # Disponibilidade em janelas deslizantes (metodologia BCE)
    m_avail_1h.labels(**lbc).set(jd.disponibilidade(3600))
    m_avail_24h.labels(**lbc).set(jd.disponibilidade(86400))
    m_avail_7d.labels(**lbc).set(jd.disponibilidade(604800))

    # Ping RTT (ICMP) — mede latência real até o BC
    rtt = ping_rtt(ip)
    if rtt is not None:
        m_ping_rtt.labels(**lbc).set(rtt)
        m_ping_ok.labels(**lbc).set(1)
    else:
        m_ping_ok.labels(**lbc).set(0)

    # Estabilidade de links — quantos peers mudaram desde última coleta
    changes = calcular_link_changes(ip, dados["radios"])
    m_link_changes.labels(**lbc).set(changes)

    # Score de qualidade da malha (0-100)
    score = calcular_mesh_score(dados)
    m_mesh_score.labels(**lbc).set(score)

    t = validar(s["temp_c"], "temperatura_c", bc)
    if t is not None:
        m_temp.labels(**lbc).set(t)
        if s["temp_cls"] is not None:
            m_temp_classe.labels(**lbc).set(s["temp_cls"])

    # Bateria (State.Battery). Modelos sem bateria não trazem o bloco →
    # tudo None → nenhuma série publicada, em vez de uma fileira de zeros.
    bat = s.get("bateria") or {}
    pub(m_bat_pct,      validar(bat.get("pct"), "bateria_pct", bc))
    pub(m_bat_ma,       bat.get("ma"))
    pub(m_bat_temp,     bat.get("temp_c"))
    pub(m_bat_desc_min, bat.get("desc_min"))
    pub(m_bat_chg_min,  bat.get("chg_min"))
    if bat.get("carregando") is not None:
        m_bat_charge.labels(**lbc).set(1 if bat["carregando"] else 0)

    # cpu_pct só existe a partir da 2a coleta (delta de idle/uptime)
    pub(m_cpu, validar(s.get("cpu_pct"), "cpu_pct", bc))

    m_im_tx.labels(**lbc).set(im["pkt_tx"])
    m_im_rx.labels(**lbc).set(im["pkt_rx"])
    m_im_drop.labels(**lbc).set(im["pkt_drop"])
    m_im_floods.labels(**lbc).set(im["floods"])
    m_im_arp.labels(**lbc).set(im["arp"])
    m_im_disc_src.labels(**lbc).set(im["disc_src"])
    m_im_disc_pass.labels(**lbc).set(im["disc_pass"])
    m_im_src_floods.labels(**lbc).set(im["src_floods"])
    m_im_multicast.labels(**lbc).set(im["multicast"])
    m_im_tx_ps.labels(**lbc).set(im.get("tx_ps",0))
    m_im_rx_ps.labels(**lbc).set(im.get("rx_ps",0))
    m_im_drop_ps.labels(**lbc).set(im.get("drop_ps",0))
    m_im_perda.labels(**lbc).set(im.get("perda_pct",0))
    m_im_floods_ps.labels(**lbc).set(im.get("floods_ps",0))
    m_im_arp_ps.labels(**lbc).set(im.get("arp_ps",0))

    for r in dados["radios"]:
        rn   = r["nome"]
        rlbs = {"bc":bc,"ip":ip,"radio":rn,"canal":str(r["canal"]),"freq":r["freq"]}
        m_r_canal.labels(**rlbs).set(r["canal"])
        m_r_txpower.labels(**rlbs).set(r["txpwr"])
        m_r_rx_bytes.labels(**rlbs).set(r["rx_bytes"])
        m_r_tx_bytes.labels(**rlbs).set(r["tx_bytes"])
        m_r_rx_pkts.labels(**rlbs).set(r["rx_pkts"])
        m_r_tx_pkts.labels(**rlbs).set(r["tx_pkts"])
        m_r_ch_active.labels(**rlbs).set(r["ch_active"])
        m_r_ch_busy.labels(**rlbs).set(r["ch_busy"])
        m_r_ch_rx.labels(**rlbs).set(r["ch_rx"])
        m_r_ch_tx.labels(**rlbs).set(r["ch_tx"])
        m_r_busy.labels(**rlbs).set(r.get("busy_pct",0))
        m_r_rx_pct.labels(**rlbs).set(r.get("rx_pct",0))
        m_r_tx_pct.labels(**rlbs).set(r.get("tx_pct",0))
        m_r_idle.labels(**rlbs).set(r.get("idle_pct",0))
        # Sem delta ainda (1a coleta do rádio) fica sem série, nunca 0 —
        # zero seria lido como "medi e não há interferência".
        pub(m_r_interf, r.get("interf_pct"), **rlbs)
        m_r_peers_ativos.labels(**rlbs).set(r["peers_ativos"])
        m_r_peers_total.labels(**rlbs).set(r["peers_total"])
        m_r_good_peers.labels(**rlbs).set(r["good_peers"])
        m_r_clients.labels(**rlbs).set(r["total_cli"])
        m_r_rx_mbps.labels(**rlbs).set(r.get("rx_mbps",0))
        m_r_tx_mbps.labels(**rlbs).set(r.get("tx_mbps",0))
        m_r_rx_pps.labels(**rlbs).set(r.get("rx_pps",0))
        m_r_tx_pps.labels(**rlbs).set(r.get("tx_pps",0))
        nd = validar(r["ruido"], "ruido_dbm", bc)
        if nd is not None:
            m_r_ruido.labels(**rlbs).set(nd)
            if r["ruido_cls"] is not None:
                m_r_ruido_cls.labels(**rlbs).set(r["ruido_cls"])
        if r["first_hop"] > 0:
            m_first_hop.labels(bc=bc,ip=ip,radio=rn).set(r["first_hop"])

        for p in r["peers"]:
            plbs = {"bc":bc,"ip":ip,"radio":rn,"peer":p["ip"]}
            sn = validar(p["sinal"],"sinal_dbm",bc)
            sq = validar(p["snr"],  "snr_db",   bc)
            tx = validar(p["taxa"], "taxa_mbps", bc)
            cu = validar(p["custo"],"custo",     bc)
            if sn is not None: m_p_sinal.labels(**plbs).set(sn)
            if sq is not None: m_p_snr.labels(**plbs).set(sq)
            if tx is not None: m_p_taxa.labels(**plbs).set(tx)
            if cu is not None: m_p_custo.labels(**plbs).set(cu)
            m_p_rssi.labels(**plbs).set(p["rssi"])
            m_p_ativo.labels(**plbs).set(1 if p["ativo"] else 0)

        for ap in r["aps"]:
            m_ap_clients.labels(bc=bc,ip=ip,radio=rn,
                                 essid=ap["essid"],freq=ap["freq"]).set(ap["clients"])

    for e in dados["ethernet"]:
        elbs = {"bc":bc,"ip":ip,"porta":e["nome"]}
        m_e_link.labels(**elbs).set(1 if e["link"] else 0)
        m_e_apt_state.labels(**elbs).set(e["apt_int"])
        m_e_rx_bytes.labels(**elbs).set(e["rx_bytes"])
        m_e_tx_bytes.labels(**elbs).set(e["tx_bytes"])
        m_e_rx_pkts.labels(**elbs).set(e["rx_pkts"])
        m_e_tx_pkts.labels(**elbs).set(e["tx_pkts"])
        m_e_peers.labels(**elbs).set(e.get("peers_eth", 0))
        m_e_rx_mbps.labels(**elbs).set(e.get("rx_mbps",0))
        m_e_tx_mbps.labels(**elbs).set(e.get("tx_mbps",0))


def marcar_offline(ip, nome, est, agora):
    lbc = {"bc": nome or ip, "ip": ip}
    jd  = janela_bc(ip)
    jd.registrar(False)
    if est.ultima_dados and not est.expirou(agora):
        m_falhas.labels(**lbc).set(est.falhas_consec)
        # Atualiza disponibilidade mesmo offline
        m_avail_1h.labels(**lbc).set(jd.disponibilidade(3600))
        m_avail_24h.labels(**lbc).set(jd.disponibilidade(86400))
        m_avail_7d.labels(**lbc).set(jd.disponibilidade(604800))
    else:
        m_online.labels(**lbc).set(0)
        m_falhas.labels(**lbc).set(est.falhas_consec)
        m_avail_1h.labels(**lbc).set(jd.disponibilidade(3600))
        m_avail_24h.labels(**lbc).set(jd.disponibilidade(86400))
        m_avail_7d.labels(**lbc).set(jd.disponibilidade(604800))

# ══════════════════════════════════════════════════════════════
# COLETOR
# ══════════════════════════════════════════════════════════════
class RajantCollector:
    def __init__(self, seeds, role, password, port, interval,
                 max_threads, timeout, tentativas, falhas_limite,
                 manter_s, redesc_ciclos, falhas_remover, cache,
                 interval_moveis=0, padrao_movel="", descoberta=True,
                 prefixos_tag=None):
        self.seeds=seeds; self.role=role; self.password=password; self.port=port
        self.interval=interval; self.max_threads=max_threads; self.timeout=timeout
        self.tentativas=tentativas; self.falhas_limite=falhas_limite
        self.manter_s=manter_s; self.redesc_ciclos=redesc_ciclos
        self.falhas_remover=falhas_remover; self.cache=cache
        self.descoberta=descoberta
        # None = aceita qualquer nome. Lista = só publica quem casa com um
        # dos prefixos da frota.
        self.prefixos_tag = list(prefixos_tag) if prefixos_tag else None
        self.sem_tag = set()          # descartados, para o log e a métrica
        self.bcs=set(); self.nomes={}
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
        nome = (self.nomes.get(ip) or self.cache.nome(ip) or "").strip()
        if not nome or nome == ip: return True
        return bool(self.re_movel.search(nome))

    def tem_tag(self, nome):
        """O nome casa com algum prefixo da frota?

        Sem prefixos configurados, aceita tudo — o filtro e opcional e nao
        pode passar a descartar por omissao.
        """
        if not self.prefixos_tag:
            return True
        n = (nome or "").strip().upper()
        if not n:
            return False
        return any(n.startswith(p.upper()) for p in self.prefixos_tag)

    def _coletar_bc(self, ip):
        est  = estado(ip, self.falhas_limite, self.manter_s)
        agora= time.monotonic()
        nome = self.nomes.get(ip, self.cache.nome(ip))
        for tentativa in range(1, self.tentativas+1):
            try:
                bc = exigir_rajant_api()(host=ip, port=self.port,
                                role=self.role, password=self.password)
                if not bc.reachable():   raise ConnectionRefusedError("nao alcancavel")
                if not bc.authenticate(): raise PermissionError("autenticacao falhou")
                dados = parse_state(bc.get_state())
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
                                 f"nome sem prefixo de frota")
                    with self.lock:
                        self.bcs.discard(ip)
                    m_bc_sem_tag.set(len(self.sem_tag))
                    return set()

                est.ok(dados, ts)
                self.cache.adicionar(ip, nome)
                self.cache.registrar_sucesso(ip, nome)
                publicar(ip, dados, est)
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
                    self.cache.registrar_falha(ip, self.falhas_remover)
                    marcar_offline(ip, nome, est, agora)
                    if est.offline:
                        log.warning(f"  X {nome:<22} ({ip}) OFFLINE ({est.falhas_consec}x): {e}")
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
            try:
                bc = exigir_rajant_api()(host=seed, port=self.port,
                                role=self.role, password=self.password)
                if bc.reachable() and bc.authenticate():
                    seeds_ok.append(seed)
                    m_seed_status.labels(seed=seed).set(1)
                    log.info(f"  Seed OK:     {seed}")
                else:
                    seeds_fail.append(seed)
                    m_seed_status.labels(seed=seed).set(0)
                    log.warning(f"  Seed OFFLINE:{seed}")
            except Exception:
                seeds_fail.append(seed)
                m_seed_status.labels(seed=seed).set(0)
                log.warning(f"  Seed OFFLINE:{seed}")

        cache_ips    = self.cache.todos_ips()

        if not self.descoberta:
            # Lista fechada: coleta exatamente os seeds, sem seguir peer
            # nenhum. A descoberta acha tudo que RESPONDE na malha —
            # inclusive equipamento de terceiro e rádio de teste —, e
            # quem tem a lista dos proprios equipamentos nao quer isso.
            # Usa os seeds CONFIGURADOS, nao so os que responderam: um
            # BC offline agora tem de continuar sendo monitorado, senao
            # ele desaparece da metrica justamente quando cai.
            alvos = set(self.seeds)
            with self.lock:
                self.bcs = set(alvos)
            log.info(f"  Descoberta DESLIGADA: {len(alvos)} IP(s) fixos "
                     f"de [rede] seeds")
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

        ponto_inicio = set(seeds_ok) | cache_ips
        if not seeds_ok and cache_ips:
            log.warning(f"  Seeds offline! Usando {len(cache_ips)} IPs do cache.")
        elif not seeds_ok and not cache_ips:
            log.error("  Seeds offline e cache vazio! Aguardando..."); return

        log.info(f"  Seeds OK:{len(seeds_ok)} | Cache:{len(cache_ips)} IPs")
        fila, vis = set(ponto_inicio), set()
        while fila: fila = self._lote(fila, vis)
        m_cache_total.set(self.cache.total())
        log.info(f"  Total: {len(self.bcs)} BCs | Cache: {self.cache.total()} IPs")
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
                            novos = peers - self.bcs
                            if novos:
                                log.info(f"  + {len(novos)} peers novos")
                                self.bcs.update(novos)
            t = threading.Thread(target=worker, daemon=True)
            threads.append(t); t.start()
        for t in threads: t.join(timeout=self.timeout+10)
        online = sum(1 for ip in self.bcs if not estado(ip, self.falhas_limite, self.manter_s).offline)
        m_cache_online.set(online)
        m_cache_total.set(self.cache.total())
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

        for chave, ips in grupos.items():
            ips_ord = sorted(ips, key=ip_ordem)
            primario = ips_ord[0]
            for ip in ips_ord:
                rotulo = nome_de(ip) or ip
                lb = {"bc": rotulo, "ip": ip}
                m_bc_primario.labels(**lb).set(1 if ip == primario else 0)
                m_bc_ifaces.labels(**lb).set(len(ips_ord))
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
    # KMZ da mina usado como fundo georreferenciado dos mapas de survey
    # gerados pela página web. Vazio = mapas sem fundo (só eixos lat/lon).
    "survey_kmz":      "",
    # Painel HTML próprio, servido em /painel. Editável sem reiniciar.
    "painel_html":     "painel/visao-geral.html",
    # ── Survey ────────────────────────────────────────────────
    # Prefixos da frota: rótulo:PREFIXO. Viram os botões de seleção em
    # massa da página — com 150 nós, checkbox individual não escala.
    "prefixos_frota":  ("Caminhão:CA ; Pá:PA ; Perfuratriz:PF ; Trator:TT ; "
                        "Escavadeira:EH ; Repetidora móvel:ERM ; Torre:ERB"),
    # Perfis salvos: nome = prefixos ou nomes separados por vírgula.
    "perfil_frota_completa": "CA,PA,PF,TT,EH,ERM,ERB",
    "perfil_cava_principal": "CA,PF,EH,ERM",
    "perfil_so_repetidoras": "ERM,ERB",
    # false = o survey NAO entra no relatorio semanal: ele e entrega
    # propria, gerada por --ppt-survey. Os slides de survey que vem no
    # template tambem sao removidos, senao ficariam em branco no arquivo.
    "survey_no_semanal": "false",
    # true = o semanal sai na identidade Anglo (fundo branco, azul
    # institucional, logo e regua), repintando o template do cliente no fim
    # da geracao. Nada muda de lugar: menus, botoes, tabelas, graficos e o
    # que estiver digitado no template continuam onde estao.
    # false = deck no visual original do template.
    "identidade_anglo": "true",
    # false = o KMZ do survey NAO leva marcador de equipamento. Com uma
    # dezena de BCs a camada de alfinetes cobre a medicao, que e o assunto
    # do arquivo. true devolve a pasta "BreadCrumbs".
    "kmz_com_equipamentos": "false",
    # false = a rota sai como MAPA DE CALOR (nucleo por medicao, so onde o
    # radio passou). true devolve tambem a linha ligando as amostras — e a
    # linha inventa aresta reta entre pontos distantes.
    "kmz_com_rotas": "false",
    # false = o PPT sai com MOLDURAS VAZIAS, cada uma dizendo qual KMZ
    # abrir e qual camada ligar para tirar o print no Google Earth. É o
    # caminho de quem quer o satélite real no slide, que o PNG não tem
    # enquanto a rede da mina bloquear os tiles.
    # true = volta a inserir as imagens geradas automaticamente.
    "imagens_no_ppt":  "false",
    # Lado da célula (m) na agregação do survey. Mediana por célula tira o
    # viés do equipamento parado e o ruído da leitura instantânea; 50 m é
    # da ordem do espaçamento dos pontos a 10 s.
    "zonas_grade_m":   "50",
    # Ortofoto local usada quando o satélite não é alcançável. Só entra no
    # mapa acompanhada de fundo_bbox: sem saber a que retângulo do mundo a
    # imagem corresponde, ela seria esticada até os dados e o mapa sairia
    # bonito e errado — pior que mapa sem fundo.
    "fundo_local":     "",
    # Cantos da ortofoto acima, em graus: norte,sul,leste,oeste.
    # Ex.: -27.7250,-27.7400,-50.0580,-50.0760
    "fundo_bbox":      "",
}

def cfg_relatorio(cfg):
    if not cfg.has_section("relatorio"):
        cfg.add_section("relatorio")
    mudou = False
    for k, v in DEFAULTS_RELATORIO.items():
        if not cfg.has_option("relatorio", k):
            cfg.set("relatorio", k, v); mudou = True
    # A seção [survey] entra na MESMA gravação: em arquivo separado o
    # operador nunca descobriria que as opções existem.
    faltava = not cfg.has_section("survey") or any(
        not cfg.has_option("survey", k) for k in DEFAULTS_SURVEY)
    if faltava:
        cfg_survey(cfg); mudou = True
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
         f"Ruído: bom ≤ -100 dBm | degradado > {lim_ruido:.0f} dBm (heatmap do site survey) | "
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
         "(alimenta o slide de Site Survey)", 9)
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
    ws.cell(lin,1,"Leitura (mesma do heatmap): limpo ≤ -100 dBm | moderado -99 a -90 | "
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

def aba_survey(wb, sv, resumo, calib, manuais, comp, amostras_n=0):
    """Aba Survey: requisitos Modular, calibração, medições manuais e
    comparação com o survey anterior."""
    ws = wb.create_sheet("Survey")
    quando = datetime.fromtimestamp(sv.get("inicio") or time.time())
    dur = ""
    if sv.get("fim"):
        dur = f" · duração {int((sv['fim']-sv['inicio'])/60)} min"
    _cab(ws, f"SITE SURVEY — {sv.get('nome','(sem nome)')}",
         f"{quando:%d/%m/%Y %H:%M}{dur} · {resumo.get('radios',0)} equipamentos "
         f"· {amostras_n} amostras medidas", 7)

    lin = 4
    ws.cell(lin,1,"REQUISITOS MODULAR MINING").font = Font(
        name="Arial", size=10, bold=True, color="1F4E79")
    lin += 1
    _th(ws, lin, ["Indicador","Requisito","% dentro","Média","Pior (P05)",
                  "Semana anterior","Tendência"],
        [20,14,10,10,12,15,12]); lin += 1
    lin0 = lin
    for campo in ("sinal","snr","rtt","perda"):
        r = (resumo or {}).get(campo)
        c = (comp or {}).get(campo, {})
        if not r:
            # Sem medição: linha em branco, nunca zero.
            _td(ws, lin, [rm_rotulo(campo), "—", None, None, None, None, "—"])
            lin += 1; continue
        op = ">" if campo in ("sinal","snr") else "<"
        _td(ws, lin, [r["rotulo"], f"{op} {r['limite']} {r['unidade']}",
                      r["pct_ok"], r["med"], r["p05"],
                      c.get("antes"), SETA.get(c.get("direcao"), "—")],
            zebra=(lin % 2 == 0))
        _semaforo(ws.cell(lin,3), r["pct_ok"], 95, tol_pct=5)
        cel = ws.cell(lin,7)
        if c.get("direcao") == "melhorou": cel.fill = FILL_OK
        elif c.get("direcao") == "piorou": cel.fill = FILL_CRIT
        lin += 1
    lin += 1

    ws.cell(lin,1,"CALIBRAÇÃO DO MODELO DE PROPAGAÇÃO").font = Font(
        name="Arial", size=10, bold=True, color="1F4E79")
    lin += 1
    _th(ws, lin, ["Banda","Expoente n","Erro RMS (dB)","Amostras",
                  "Alcance (m)","Situação"], [12,12,14,10,12,34]); lin += 1
    for banda, c in (calib or {}).items():
        sit = ("calibrado com os dados medidos" if c.get("ok")
               else f"PADRÃO — {c.get('motivo','sem dados')}")
        _td(ws, lin, [banda, c.get("n"), c.get("rms"), c.get("amostras"),
                      c.get("alcance_m"), sit], zebra=(lin % 2 == 0))
        if not c.get("ok"): ws.cell(lin,6).fill = FILL_WARN
        lin += 1
    lin += 1

    ips = [m for m in (manuais or []) if m["tipo"] == "iperf"]
    ws.cell(lin,1,"THROUGHPUT POR PONTO DE TESTE (iperf — lançamento manual)"
            ).font = Font(name="Arial", size=10, bold=True, color="1F4E79")
    lin += 1
    _th(ws, lin, ["Data","Local","Banda","Mbps","Requisito","Observação"],
        [12,26,10,10,12,32]); lin += 1
    if ips:
        for m in ips:
            _td(ws, lin, [m.get("data"), m.get("local"), m.get("banda"),
                          m.get("mbps"), "> 1 Mbps", m.get("obs")],
                zebra=(lin % 2 == 0))
            if m.get("mbps") is not None:
                _semaforo(ws.cell(lin,4), m["mbps"], 1.0, tol_pct=50)
            lin += 1
    else:
        ws.cell(lin,1,"Sem lançamento no período — a BC API não mede "
                      "throughput; use a importação de CSV na página."
                ).font = F_PEQ
        lin += 1
    lin += 1

    trs = [m for m in (manuais or []) if m["tipo"] == "trace"]
    ws.cell(lin,1,"SALTOS E GARGALO (trace — lançamento manual)").font = Font(
        name="Arial", size=10, bold=True, color="1F4E79")
    lin += 1
    _th(ws, lin, ["Data","Origem","Destino","Saltos","Custo total",
                  "Salto gargalo","Observação"], [12,16,16,9,12,16,26]); lin += 1
    if trs:
        for m in trs:
            _td(ws, lin, [m.get("data"), m.get("origem"), m.get("destino"),
                          m.get("saltos"), m.get("custo_total"),
                          m.get("gargalo"), m.get("obs")],
                zebra=(lin % 2 == 0))
            lin += 1
    else:
        ws.cell(lin,1,"Sem lançamento no período.").font = F_PEQ
        lin += 1

    lin += 2
    ws.cell(lin,1,"As três camadas do mapa são distintas: MEDIDO pela frota "
                  "(verdade de campo), COBERTURA ESTIMADA (modelo de "
                  "planejamento) e INFRAESTRUTURA. Indicador sem medição fica "
                  "em branco — nunca zero, que se confundiria com 'medido e "
                  "reprovado'.").font = F_PEQ
    ws.freeze_panes = "A5"; ws.sheet_view.showGridLines = False


def rm_rotulo(campo):
    return {"sinal":"RSSI","snr":"SNR","rtt":"Latência",
            "perda":"Perda"}.get(campo, campo)


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

def dados_survey_para_relatorio(survey_id=None, banco_path=None):
    """Carrega o survey (o indicado, ou o último fechado) para os relatórios.

    Devolve None quando não há survey — as seções então saem em branco, e
    não com número inventado.
    """
    try:
        con = banco(banco_path)
    except Exception as e:
        log.warning(f"[survey] banco indisponível: {e}")
        return None
    try:
        if survey_id:
            sv = survey_obter(con, survey_id)
        else:
            lst = [s for s in survey_listar(con, 20) if s.get("fim")]
            sv = lst[0] if lst else None
        if not sv: return None
        am = survey_amostras(con, sv["id"])
        resumo = json.loads(sv.get("resumo") or "{}") or survey_resumo(am)
        calib = json.loads(sv.get("calibracao") or "{}")
        ant = survey_anterior(con, sv["id"])
        resumo_ant = json.loads(ant.get("resumo") or "{}") if ant else None
        return {
            "survey": sv, "amostras": am, "resumo": resumo, "calib": calib,
            "manuais": manual_listar(con, sv["id"]),
            "comparacao": comparar_surveys(resumo, resumo_ant),
            "anterior": ant,
        }
    finally:
        con.close()


def montar_relatorio(cfg, ini_d, fim_d, demo=False, survey_id=None):
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
    # Aba Survey só existe quando há survey persistido — sem ele, criar
    # uma aba de campos vazios só confundiria quem abre o arquivo.
    sv = dados_survey_para_relatorio(survey_id)
    if sv:
        try:
            aba_survey(wb, sv["survey"], sv["resumo"], sv["calib"],
                       sv["manuais"], sv["comparacao"], len(sv["amostras"]))
        except Exception as e:
            log.error(f"[relatorio] aba Survey falhou: {e}")
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

def _ppt_set_bloco(tf, novo):
    """Substitui TODO o conteúdo do quadro, preservando a fonte.

    Diferente de _ppt_set(), que preenche uma lacuna num parágrafo. Num
    bloco de vários marcadores, aquele trocava só o parágrafo com '____' e
    os outros continuavam contando a versão antiga da história.
    """
    pars = tf.paragraphs
    if not pars: return False
    modelo = next((r for p in pars for r in p.runs), None)
    fonte = {}
    if modelo is not None:
        fonte = {"name": modelo.font.name, "size": modelo.font.size,
                 "bold": modelo.font.bold}
        try: fonte["cor"] = modelo.font.color.rgb
        except Exception: fonte["cor"] = None
    for p in list(pars[1:]):
        p._p.getparent().remove(p._p)
    p0 = tf.paragraphs[0]
    for r in list(p0.runs):
        r._r.getparent().remove(r._r)
    for i, linha in enumerate(str(novo).split("\n")):
        par = p0 if i == 0 else tf.add_paragraph()
        r = par.add_run(); r.text = linha
        if fonte:
            if fonte.get("name"): r.font.name = fonte["name"]
            if fonte.get("size"): r.font.size = fonte["size"]
            if fonte.get("bold") is not None: r.font.bold = fonte["bold"]
            if fonte.get("cor") is not None: r.font.color.rgb = fonte["cor"]
    return True

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

def _tabela_ppt(slide, x, y, w, cab, linhas, destaques=None, tam=10,
                altura_linha=0.32):
    """Tabela na identidade do template. Versão de módulo — as outras duas
    são aninhadas dentro dos geradores de slide e não dá para reusar.

    `destaques` = {(linha, coluna): cor} para marcar valor fora do requisito.
    """
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN
    C_HDR_ = RGBColor.from_string("1A3A7A"); C_BR_ = RGBColor.from_string("FFFFFF")
    C_ZEB_ = RGBColor.from_string("152238"); C_CRD = RGBColor.from_string("1A2744")
    C_TXT_ = RGBColor.from_string("C8D0E0")
    gf = slide.shapes.add_table(len(linhas) + 1, len(cab), Inches(x), Inches(y),
                                Inches(w),
                                Inches(0.4 + altura_linha * len(linhas)))
    t = gf.table; t.first_row = False; t.horz_banding = False
    for j, txt in enumerate(cab):
        cel = t.cell(0, j); cel.fill.solid(); cel.fill.fore_color.rgb = C_HDR_
        pr = cel.text_frame.paragraphs[0]; pr.alignment = PP_ALIGN.CENTER
        r = pr.add_run(); r.text = str(txt)
        r.font.name = "Calibri"; r.font.size = Pt(tam)
        r.font.bold = True; r.font.color.rgb = C_BR_
    for i, lin in enumerate(linhas, start=1):
        for j, v in enumerate(lin):
            cel = t.cell(i, j); cel.fill.solid()
            cel.fill.fore_color.rgb = C_ZEB_ if i % 2 == 0 else C_CRD
            pr = cel.text_frame.paragraphs[0]
            pr.alignment = PP_ALIGN.LEFT if j == 1 else PP_ALIGN.CENTER
            r = pr.add_run(); r.text = "—" if v in (None, "") else str(v)
            r.font.name = "Calibri"; r.font.size = Pt(tam)
            cor = (destaques or {}).get((i, j))
            r.font.color.rgb = RGBColor.from_string(cor) if cor else C_TXT_
            if cor: r.font.bold = True
    return t


def _remover_forma(sh):
    """Tira a forma do slide. python-pptx não expõe delete de shape."""
    sh._element.getparent().remove(sh._element)


def marcar_ppt_demo(p, texto=None):
    """Carimba os slides de survey quando os dados são de demonstração.

    A marca vai nas imagens, mas o slide de zonas é só tabela — e é
    justamente o que mais parece um laudo. Sem carimbo, um deck de exemplo
    circula e vira "o survey da mina".
    """
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    txt = texto or MARCA_DEMO["texto"]
    n = 0
    for s in p.slides:
        textos = [sh.text_frame.text for sh in s.shapes if sh.has_text_frame]
        if not any("site survey" in (t or "").lower() for t in textos):
            continue
        if any((t or "").strip().lower().startswith("navega") for t in textos):
            continue
        if any(txt in (t or "") for t in textos):
            continue
        tb = s.shapes.add_textbox(Inches(0.55), Inches(7.02), Inches(12.25),
                                  Inches(0.3))
        r = tb.text_frame.paragraphs[0].add_run(); r.text = txt
        r.font.name = "Calibri"; r.font.size = Pt(11); r.font.bold = True
        r.font.color.rgb = RGBColor.from_string("FF3B30")
        n += 1
    return n


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


def _eh_rotulo_secao(txt):
    """Rótulo de coluna ('METODOLOGIA', 'LEITURA'): caixa-alta e curto.
    Escrever o número aqui deixava o parágrafo real intocado logo abaixo."""
    t = (txt or "").strip()
    return bool(t) and len(t) <= 40 and "\n" not in t and t == t.upper()


# Campos manuais de localidade, que o cliente não usa. Casados por texto
# inteiro ou por prefixo — nunca por substring solta: "% da área dentro do
# requerido" e "Área ≥ -75 dBm" falam de COBERTURA, não de lugar, e apagar
# esses tiraria número medido do relatório.
_ROTULOS_LOCALIDADE = ("áreas percorridas", "areas percorridas",
                       "localidades", "áreas de operação",
                       "areas de operacao", "local do survey")

# Metodologia sem números — quando a banda entra no relatório,
# preencher_slides_survey() troca por esta mesma frase já quantificada.
METODOLOGIA_PADRAO = (
    "Survey contínuo pelos rádios da própria frota, sem veículo dedicado "
    "nem kit de medição: cada rádio é consultado diretamente pela BC API "
    "no intervalo escolhido e o GPS do próprio equipamento georreferencia "
    "a amostra.\n"
    "Medidos por amostra: RSSI, SNR, ruído, custo e taxa do melhor enlace, "
    "latência e perda (ICMP).")


def _eh_bloco_de_localidade(txt):
    t = (txt or "").strip().lower()
    if not t:
        return False
    if any(t.startswith(r) for r in _ROTULOS_LOCALIDADE):
        return True
    # A lista de bolinhas em branco que acompanha o rótulo: só linhas
    # "●  ______". Uma linha com conteúdo real salva o bloco.
    linhas = [l.strip() for l in t.splitlines() if l.strip()]
    if linhas and all(re.fullmatch(r"[●•]\s*_{3,}", l) for l in linhas):
        return True
    return False


# Linhas de localidade que vivem DENTRO de um bloco maior — o marcador da
# metodologia é uma linha entre outras sete que continuam valendo, então
# aqui se remove a linha, não a caixa.
_RE_LINHA_LOCALIDADE = re.compile(
    r"(percurso pelas|áreas de operaç|areas de operac|kit fixado ao teto"
    r"|local(idade)?s?\s*:\s*_+)", re.I)


def _remover_linhas_localidade(tf):
    """Apaga do quadro de texto só as linhas de localidade.

    A metodologia é um bloco de vários marcadores; apagar a caixa inteira
    levaria junto o método de medição. Devolve quantos parágrafos saíram.
    """
    fora = 0
    for par in list(tf.paragraphs):
        if not _RE_LINHA_LOCALIDADE.search(par.text or ""):
            continue
        # A continuação indentada do marcador ("    áreas de operação: ___")
        # é outro parágrafo; o regex pega os dois porque casa em ambos.
        par._p.getparent().remove(par._p); fora += 1
    return fora


def limpar_localidade_survey(p):
    """Apaga os campos manuais de local/cava/áreas dos slides de survey e
    alarga o mapa no espaço liberado.

    Roda sobre o template JÁ EXISTENTE: só reescrever o gerador não
    adiantaria, porque o deck do cliente já tem esses slides prontos.

    Devolve (formas_removidas, mapas_alargados).
    """
    removidas = alargados = 0
    LARG_CHEIA = 12.25          # margem de 0,55" dos dois lados em 13,33"
    for s in p.slides:
        textos = [sh.text_frame.text for sh in s.shapes if sh.has_text_frame]
        if not any("site survey" in (t or "").lower() for t in textos):
            continue
        if any((t or "").strip().lower().startswith("navega") for t in textos):
            continue

        # Linhas soltas dentro de blocos que continuam valendo (metodologia).
        for sh in s.shapes:
            if sh.has_text_frame and not _eh_titulo_survey(sh.text_frame.text):
                removidas += _remover_linhas_localidade(sh.text_frame)

        # O subtítulo do template descreve o método antigo (kit passivo +
        # ativo em veículo dedicado). Continua no slide mesmo depois de o
        # corpo ser reescrito, e aí o slide se contradiz.
        for sh in s.shapes:
            if not sh.has_text_frame: continue
            t = (sh.text_frame.text or "").strip()
            if t.lower().startswith("base: survey passivo"):
                _ppt_set(sh.text_frame,
                         "Base: survey contínuo pelos rádios da própria "
                         "frota — consulta direta pela BC API + GPS do "
                         "equipamento")
                removidas += 1
            elif "exportado do google earth" in t.lower():
                _ppt_set(sh.text_frame,
                         "Trajeto medido pelos rádios da frota durante o "
                         "período do survey")
                removidas += 1
            elif "survey passivo: adaptador wireless" in t.lower():
                # Corpo da metodologia do método antigo. Trocado aqui, e
                # não só em preencher_slides_survey(), porque aquele roda
                # POR BANDA: um survey de uma banda só deixaria os slides
                # da outra descrevendo kit e veículo dedicado.
                _ppt_set_bloco(sh.text_frame, METODOLOGIA_PADRAO)
                removidas += 1

        alvos = [sh for sh in s.shapes
                 if sh.has_text_frame and _eh_bloco_de_localidade(sh.text_frame.text)]
        if not alvos:
            continue
        for sh in alvos:
            _remover_forma(sh); removidas += 1

        # Alarga a imagem/moldura do mapa para a largura cheia do slide.
        from pptx.util import Inches
        for sh in s.shapes:
            eh_mapa = (sh.shape_type is not None
                       and "PICTURE" in str(sh.shape_type)) or (
                       sh.has_text_frame
                       and "mapa" in (sh.text_frame.text or "").lower()
                       and "colar" in (sh.text_frame.text or "").lower())
            if not eh_mapa:
                continue
            if sh.width >= Inches(LARG_CHEIA):
                continue
            # Mantém a proporção geográfica: cresce em largura e altura
            # até o teto disponível, para o mapa não achatar.
            fator = Inches(LARG_CHEIA) / float(sh.width)
            nova_alt = min(int(sh.height * fator), Inches(5.3))
            sh.width, sh.height = Inches(LARG_CHEIA), nova_alt
            alargados += 1
    if removidas:
        log.info(f"[survey/ppt] localidade: {removidas} campos removidos, "
                 f"{alargados} mapas alargados")
    return removidas, alargados


def preencher_analise_survey(p, sv, resumo, zonas, srv, amostras, banda=None,
                             campo="sinal"):
    """Preenche ANÁLISE / RECOMENDAÇÕES / CONCLUSÃO do slide de fecho.

    Era o único slide da seção que continuava em branco — "preencher após
    consolidação dos dados" —, e é justamente o que o leitor procura. Tudo
    que ele pede já está calculado: percentuais por requisito, zonas com
    ação, cobertura por BC e ping-pong.
    """
    suf = f" — {banda}" if banda else ""
    _, s = _slide_por_titulo(p, "Análise, Recomendações e Conclusão" + suf)
    if s is None:
        _, s = _slide_por_titulo(p, "Análise, Recomendações e Conclusão")
    if s is None: return 0

    def _bloco(rotulo, texto):
        """Escreve na caixa ABAIXO do rótulo da coluna."""
        alvo = None
        for sh in s.shapes:
            if not sh.has_text_frame: continue
            if (sh.text_frame.text or "").strip().upper() != rotulo:
                continue
            # A caixa de conteúdo é a mais próxima logo abaixo, na mesma
            # coluna: comparar só o topo pegaria a coluna vizinha.
            cands = [c for c in s.shapes
                     if c.has_text_frame and c is not sh
                     and (c.top or 0) > (sh.top or 0)
                     and abs((c.left or 0) - (sh.left or 0)) < 457200]
            if cands: alvo = min(cands, key=lambda c: c.top)
            break
        if alvo is None: return False
        _ppt_set_bloco(alvo.text_frame, texto)
        return True

    n_am = len(amostras)
    linhas_a = []
    for c in ("sinal", "snr", "rtt", "perda"):
        r = (resumo or {}).get(c)
        if not r: continue
        rot_c = (limite_de(c) or (None, None, None, c.upper()))[3]
        linhas_a.append(f"• {rot_c}: {r['pct_ok']:.1f}% dentro do requisito "
                        f"(mediana {r['med']:g} {r['unidade']}, "
                        f"pior 5% em {r['p05']:g} {r['unidade']}).")
    cob = list((srv or {}).get("cobertura", {}).items())[:3]
    if cob:
        linhas_a.append("• Cobertura: " + ", ".join(
            f"{bc} {d['pct']:.0f}%" for bc, d in cob) + ".")
    if srv and srv.get("n_handovers"):
        linhas_a.append(f"• {srv['n_handovers']} handover(s) no período"
                        + (f", {srv['n_pingpong']} em ping-pong"
                           if srv.get("n_pingpong") else "") + ".")
    if not linhas_a:
        linhas_a = ["Sem medição suficiente no período para consolidar."]
    linhas_a.insert(0, f"{n_am} amostras" + (f" em {banda}" if banda else "")
                    + ".")

    rec = []
    for i, z in enumerate(zonas or [], start=1):
        rec.append(f"{i}. {texto_zona(z, campo)}")
    if srv and srv.get("n_pingpong"):
        alvo = {tuple(x["entre"]) for x in srv["pingpong"]}
        rec.append(f"{len(rec)+1}. Ping-pong entre "
                   + "; ".join(" e ".join(a) for a in list(alvo)[:3])
                   + ". Dois BCs disputando a mesma área: avaliar potência "
                     "ou reposicionamento — não é falha de rádio.")
    if not rec:
        rec = ["Nenhuma zona contígua fora do requisito no período. "
               "Manter o plano de manutenção atual."]

    r_sinal = (resumo or {}).get("sinal") or {}
    pct = r_sinal.get("pct_ok")
    n_z = len(zonas or [])
    if pct is None:
        con = "Sem RSSI medido no período; conclusão pendente de nova coleta."
    elif n_z == 0:
        con = (f"Cobertura dentro do requisito em {pct:.1f}% das amostras, "
               f"sem zona contígua reprovada. Rede adequada à operação no "
               f"período medido.")
    else:
        sist = sum(1 for z in zonas if z.get("sistemico"))
        con = (f"Cobertura dentro do requisito em {pct:.1f}% das amostras. "
               f"{n_z} zona(s) exigem ação"
               + (f", sendo {sist} de deficiência sistêmica (replanejamento, "
                  f"não ponto cego)" if sist else "")
               + ". Ver as recomendações ao lado.")

    n = 0
    n += _bloco("ANÁLISE DOS RESULTADOS", "\n".join(linhas_a))
    n += _bloco("RECOMENDAÇÕES", "\n".join(rec[:6]))
    n += _bloco("CONCLUSÃO", con)
    return n


def preencher_slides_survey(p, sv, resumo, calib, manuais, comp, amostras,
                            banda=None):
    """Preenche os slides de Site Survey com os números do survey.

    Regra que atravessa tudo: número que não existe deixa o campo EM
    BRANCO com o traço do template. Inventar é pior que vazio.
    """
    from pptx.util import Pt
    suf = f" — {banda}" if banda else ""
    n_am = len(amostras)
    radios = sorted({a["radio"] for a in amostras})
    quando = datetime.fromtimestamp(sv.get("inicio") or time.time())
    dur_min = int(((sv.get("fim") or time.time()) - sv["inicio"]) / 60)

    def _slide(titulo):
        _, s = _slide_por_titulo(p, titulo + suf)
        if s is None: _, s = _slide_por_titulo(p, titulo)
        return s

    def _texto(slide, contem, novo):
        """Troca o texto de uma caixa que contenha `contem`.

        Pula título e subtítulo: o título de um slide de survey SEMPRE
        contém a palavra procurada ("… — Metodologia e Requisitos"), então
        a busca ingênua acertava o título primeiro e o apagava. O slide
        ficava sem cabeçalho e o número medido não aparecia em lugar
        nenhum.
        """
        if slide is None: return False
        cands = []
        for sh in slide.shapes:
            if not sh.has_text_frame: continue
            t = sh.text_frame.text or ""
            if _eh_titulo_survey(t) or _eh_rotulo_secao(t): continue
            if contem.lower() in t.lower():
                cands.append((len(t), sh))
        if not cands: return False
        # O corpo é o candidato mais longo. Sem isso o texto caía num
        # rótulo curto ("METODOLOGIA") e o parágrafo real, que contradizia
        # o novo método, continuava no slide logo abaixo.
        tf = max(cands, key=lambda c: c[0])[1].text_frame
        # Bloco de vários parágrafos precisa ser trocado inteiro; _ppt_set
        # só preencheria a lacuna de um deles e o resto ficaria.
        if len(tf.paragraphs) > 1 or "\n" in str(novo):
            _ppt_set_bloco(tf, novo)
        else:
            _ppt_set(tf, novo)
        return True

    preenchidos = 0

    # ── Metodologia e Requisitos ──
    s = _slide("Metodologia e Requisitos")
    if s is not None:
        # O intervalo que vale é o EFETIVO: o pedido é só a intenção, e um
        # ciclo lento pode tê-lo estourado. Quando divergem, mostra os dois
        # — quem lê o relatório precisa saber a resolução real do trajeto.
        ped = sv.get("intervalo_s")
        efe = sv.get("intervalo_efetivo_s")
        if efe and ped and abs(efe - ped) >= 1:
            txt_int = f"intervalo efetivo de {efe:g} s (pedido: {ped:g} s)"
        else:
            txt_int = f"intervalo de {efe or ped or '?'} s"
        n_cache = sum(1 for a in amostras if a.get("fonte") == "cache")
        obs_cache = (f" {n_cache} amostra(s) vieram do cache do exporter, "
                     f"por indisponibilidade momentânea do rádio."
                     if n_cache else "")
        met = (f"Survey contínuo pelos rádios da própria frota — "
               f"{len(radios)} rádios consultados diretamente pela BC API, "
               f"{txt_int}, {quando:%d/%m/%Y %H:%M} por {dur_min} min "
               f"({n_am} amostras).{obs_cache}\n"
               f"Requisitos Modular Mining: RSSI > -75 dBm · SNR > 20 dB · "
               f"latência < 100 ms · perda < 2% · throughput > 1 Mbps.")
        # Âncoras no CONTEÚDO do corpo, não na palavra "metodologia": o
        # rótulo da coluna tem essa palavra, o corpo não. Buscar pelo
        # rótulo caía no fallback "requisito" e o texto ia parar na caixa
        # de consequências ("Fora dos requisitos, as aplicações…").
        if (_texto(s, "survey contínuo pelos rádios", met)
                or _texto(s, "survey passivo", met)
                or _texto(s, "período da coleta", met)):
            preenchidos += 1

    # ── Percentuais por grandeza ──
    # "____% da área dentro do requerido" vira o número medido.
    mapa_slides = {
        "Intensidade de Sinal (RSSI)": "sinal",
        "Relação Sinal/Ruído (SNR)":   "snr",
        "Latência (RTT)":              "rtt",
        "Packet Loss":                 "perda",
    }
    for titulo, campo in mapa_slides.items():
        s = _slide(titulo)
        if s is None: continue
        r = (resumo or {}).get(campo)
        if not r:
            # Sem medição: deixa o traço do template.
            continue
        frase = (f"{r['pct_ok']:.1f}% das amostras dentro do requerido "
                 f"({'>' if campo in ('sinal','snr') else '<'} "
                 f"{r['limite']:g} {r['unidade']}) — média {r['med']:g} "
                 f"{r['unidade']}, pior 5% em {r['p05']:g} {r['unidade']}, "
                 f"{r['n']} amostras.")
        if (_texto(s, "% da área", frase) or _texto(s, "requerido", frase)
                or _texto(s, "requisito", frase)):
            preenchidos += 1

    # ── Cobertura estimada: % ≥ -75 dBm + alcance calibrado ──
    s = _slide("Cobertura Estimada da Mina")
    if s is not None:
        cal = (calib or {}).get(banda or "5.8 GHz") or {}
        r = (resumo or {}).get("sinal")
        pct = f"{r['pct_ok']:.1f}%" if r else "—"
        alc = f"{cal.get('alcance_m'):.0f} m" if cal.get("alcance_m") else "—"
        est = ("modelo calibrado com os dados medidos "
               f"(n={cal.get('n')}, RMS {cal.get('rms')} dB, "
               f"{cal.get('amostras')} amostras)"
               if cal.get("ok") else
               f"modelo NÃO calibrado — {cal.get('motivo','sem dados')}; "
               f"usando padrão")
        if _texto(s, "cobertura",
                  f"{pct} das amostras com RSSI ≥ -75 dBm. "
                  f"Alcance do modelo: {alc}. {est}."):
            preenchidos += 1

    # ── Throughput por ponto (medições manuais de iperf) ──
    ips = [m for m in (manuais or []) if m["tipo"] == "iperf"]
    s = _slide("Throughput por Ponto")
    if s is not None:
        linhas = [[str(i+1), m.get("local") or "—",
                   f"{m['mbps']:.1f}" if m.get("mbps") is not None else "—",
                   m.get("banda") or "—"]
                  for i, m in enumerate(ips)]
        if not linhas:
            # Sem lançamento: a BC API não mede throughput.
            _texto(s, "throughput",
                   "Sem lançamento de iperf no período — a BC API não mede "
                   "throughput; lance pela página de relatórios.")
        else:
            # Abaixo de 1 Mbps é reprovado no requisito Modular.
            destaques = {(i, 2): "C0392B"
                         for i, m in enumerate(ips, start=1)
                         if (m.get("mbps") or 0) < 1.0}
            try:
                _tabela_ppt(s, 0.6, 1.7, 11.0,
                            ["#", "Área / ponto", "Mbps", "Banda"],
                            linhas, destaques)
                preenchidos += 1
            except Exception as e:
                log.debug(f"[ppt] tabela de throughput: {e}")

    # ── Saltos e gargalo (medições manuais de trace) ──
    trs = [m for m in (manuais or []) if m["tipo"] == "trace"]
    s = _slide("Saltos")
    if s is None: s = _slide("Gargalo")
    if s is not None:
        linhas = [[m.get("origem") or "—", m.get("destino") or "—",
                   str(m.get("saltos") or "—"), str(m.get("custo_total") or "—"),
                   m.get("gargalo") or "—"] for m in trs]
        if not linhas:
            _texto(s, "salto", "Sem lançamento de trace no período.")
        else:
            try:
                _tabela_ppt(s, 0.6, 1.7, 11.0,
                            ["Origem", "Destino", "Saltos", "Custo", "Gargalo"],
                            linhas)
                preenchidos += 1
            except Exception as e:
                log.debug(f"[ppt] tabela de saltos: {e}")

    log.info(f"[ppt] survey: {preenchidos} bloco(s) preenchido(s){suf}")
    return preenchidos


def remover_slides_survey(p):
    """Tira do deck TODOS os slides da secao de Site Survey.

    O relatorio semanal e o survey viraram entregas separadas, com
    publicos diferentes. So deixar de ANEXAR nao basta: o template do
    cliente ja traz a secao inteira, e ela ficaria no arquivo em branco,
    parecendo relatorio malfeito.

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
            _ppt_set(sh.text_frame, "5. Site Survey (relatório separado)")
    log.info(f"[ppt] {len(alvos)} slide(s) de survey removidos: "
             f"o survey e entrega propria")
    return len(alvos)


def anexar_survey_ao_ppt(p, cfg, survey):
    """Gera o survey do Prometheus e cola os slides prontos no PPT.

    O período e o rádio do survey são INDEPENDENTES do período do relatório:
    o relatório costuma cobrir a semana/mês, enquanto o survey interessa num
    turno específico e num rádio específico. Forçar o mesmo recorte tornaria
    a análise de cobertura inútil.

    Devolve um resumo para o log e para a nota de rodapé do slide.
    """
    # ── Caminho 1: survey PERSISTIDO (botão "Usar no relatório") ──
    # Aqui as amostras já existem no banco; não há o que consultar no
    # Prometheus. É o caminho que o histórico usa.
    sid = survey.get("survey_id")
    if sid:
        con = banco()
        try:
            sv = survey_obter(con, sid)
            if not sv: raise RuntimeError(f"survey {sid} não encontrado")
            am = survey_amostras(con, sid)
            mn = manual_listar(con, sid)
            resumo = json.loads(sv.get("resumo") or "{}")
            calib  = json.loads(sv.get("calibracao") or "{}")
            ant = survey_anterior(con, sid)
            comp = comparar_surveys(
                resumo, json.loads(ant.get("resumo") or "{}") if ant else None)
        finally:
            con.close()
        if not am:
            raise RuntimeError(f"survey {sid} sem amostras")

        fixos = {}
        por_radio = {}
        for a in am:
            if a.get("lat") is None: continue
            por_radio.setdefault(a["radio"], []).append(a)
        for radio, pts in por_radio.items():
            if len(pts) < 2:
                fixos[radio] = (pts[0]["lat"], pts[0]["lon"]); continue
            desl = max(abs(pts[0]["lat"]-x["lat"]) + abs(pts[0]["lon"]-x["lon"])
                       for x in pts)
            if desl <= 0.0003:
                fixos[radio] = (pts[0]["lat"], pts[0]["lon"])

        bandas = survey.get("bandas") or ["2.4 GHz", "5.8 GHz"]
        slides = imagens = 0

        # Antes de qualquer imagem: tirar os campos de localidade e alargar
        # as molduras. inserir_imagens_survey() herda a geometria da
        # moldura, então na ordem inversa a imagem entraria estreita.
        limpar_localidade_survey(p)

        # Imagem gerada ou moldura para colar o print do Google Earth. O
        # PNG não tem satélite quando a rede da mina bloqueia os tiles, e
        # aí montar o slide à mão a partir do KMZ dá um mapa melhor.
        modo_img = ("imagem"
                    if cfg.getboolean("relatorio", "imagens_no_ppt",
                                      fallback=False)
                    else "moldura")

        # ── Mapa da Rede: um só, antes dos slides por banda ──
        try:
            radios_sv = json.loads(sv.get("radios") or "[]")
        except (ValueError, TypeError):
            radios_sv = []
        img_rede, resumo_rede = None, {}
        try:
            if modo_img == "imagem":
                img_rede, resumo_rede = gerar_mapa_rede(
                    sv, am, fixos, "survey_imgs", cfg=cfg,
                    radios_survey=radios_sv)
            else:
                # A tabela do slide precisa das contagens, mas o PNG vai
                # ser substituído pelo print do Google Earth — renderizar
                # matplotlib para descartar é só gasto de CPU.
                resumo_rede = contar_rede(am, radios_sv, cfg, fixos=fixos)
        except Exception as e:
            log.warning(f"[survey/ppt] mapa da rede: {e}")
            img_rede, resumo_rede = None, {}
        construir_slide_mapa_rede(p)
        if img_rede or modo_img == "moldura":
            imagens += inserir_imagens_survey(
                p, {"mapa_rede": img_rede} if img_rede else {},
                modo=modo_img)
        preencher_mapa_rede(p, sv, resumo_rede, am)
        slides += 1

        # ── Zonas-problema: onde está ruim e o que fazer ──
        try:
            grade = float(cfg.get("relatorio", "zonas_grade_m", fallback="50"))
        except (ValueError, TypeError):
            grade = 50.0
        try:
            zonas = zonas_problema(am, "sinal", grade)
        except Exception as e:
            log.warning(f"[survey/ppt] zonas-problema: {e}"); zonas = []
        preencher_slide_zonas(p, zonas, "sinal")
        slides += 1
        srv = analisar_servidores(am)
        if srv["n_pingpong"]:
            log.info(f"[survey/ppt] {srv['n_pingpong']} evento(s) de "
                     f"ping-pong entre BCs — disputa de área")
        log.info(f"[survey/ppt] {len(zonas)} zona(s) fora do requisito "
                 f"(grade de {grade:.0f} m)")

        for b in bandas:
            # O template do cliente JÁ traz os slides de survey das duas
            # bandas. Construir de novo gerava um deck com tudo duplicado —
            # e as imagens caíam na primeira ocorrência (a do template),
            # deixando as cópias novas vazias. Só monta o que faltar.
            _, ja_existe = _slide_por_titulo(p, f"Rota Percorrida — {b}")
            if ja_existe is None:
                slides += construir_slides_survey(p, banda=b)
            else:
                log.info(f"[survey/ppt] banda {b}: slides já existem no "
                         f"template, apenas preenchendo")
            imgs = {}
            if modo_img == "imagem":
                try:
                    imgs = gerar_mapas_survey(sv, am, fixos, "survey_imgs",
                                              banda=b, cfg=cfg, calib=calib)
                except Exception as e:
                    log.warning(f"[survey/ppt] banda {b}: {e}"); continue
            if imgs or modo_img == "moldura":
                imagens += inserir_imagens_survey(p, imgs, banda=b,
                                                  modo=modo_img)
            preencher_slides_survey(p, sv, resumo, calib, mn, comp, am, banda=b)
            # Fecho da seção: era o único slide que ficava em branco, e é
            # o que o leitor procura. Tudo que ele pede já foi calculado.
            am_b = [a for a in am if _norm_banda(a.get("banda")) == b] or am
            try:
                zonas_b = zonas_problema(am_b, "sinal", grade)
            except Exception:
                zonas_b = []
            preencher_analise_survey(p, sv, survey_resumo(am_b), zonas_b,
                                     analisar_servidores(am_b), am_b, banda=b)

        # Navegação: o índice precisa dos slides já criados para linkar.
        construir_indice_survey(p, bandas)
        atualizar_navegacao(p)
        barra_navegacao_survey(p, bandas)
        slides += 1
        log.info(f"[survey/ppt] survey #{sid}: {slides} slides, "
                 f"{imagens} imagens, {len(am)} amostras")
        return {"linhas": len(am), "equipamentos": len({a['radio'] for a in am}),
                "bandas": bandas, "slides": slides, "imagens": imagens,
                "survey_id": sid, "comparacao": comp,
                "ini": datetime.fromtimestamp(sv["inicio"]),
                "fim": datetime.fromtimestamp(sv["fim"] or sv["inicio"]),
                "radios": [], "servidores": [], "descartados": {},
                "radio": "todos"}

    # ── Caminho 2: survey derivado do Prometheus (recorte por período) ──
    ini = survey.get("ini"); fim = survey.get("fim")
    if not (ini and fim):
        raise RuntimeError("survey sem periodo definido")

    prom = Prometheus(cfg.get("relatorio", "prometheus_url"))
    destino = Path("survey_imgs")
    info = gerar_csv_survey_prometheus(
        prom, ini.timestamp(), fim.timestamp(),
        survey.get("passo", 60), str(destino / "survey_ppt.csv"),
        vel_min=survey.get("vel_min", 1.0),
        grade_m=survey.get("grade_m", 0.0),
        radio=survey.get("radio", "") or "")

    # Bandas: as pedidas, ou as que realmente apareceram nos dados.
    bandas = survey.get("bandas") or info["bandas"] or [None]
    total_slides, total_imgs = 0, 0
    for b in bandas:
        # 1) cria os slides-modelo (molduras "COLAR IMAGEM AQUI")
        total_slides += construir_slides_survey(p, banda=b)
        # 2) gera os PNGs e 3) troca as molduras pelas imagens
        try:
            imgs = gerar_imagens_survey(
                info["caminho"], str(destino), banda=b,
                fundo=survey.get("fundo"), kmz=survey.get("kmz"),
                kmz_fundo=survey.get("kmz_fundo", "auto"),
                fundo_bbox=survey.get("fundo_bbox"),
                raio_m=survey.get("raio_m", 300.0))
        except Exception as e:
            log.warning(f"[survey/ppt] banda {b}: imagens nao geradas ({e})")
            continue
        total_imgs += inserir_imagens_survey(p, imgs, banda=b)
        # Números nos slides: percentuais reais, calibração e as tabelas de
        # iperf/trace. Sem lançamento, os campos ficam com o traço do
        # template — nunca com número inventado.
        try:
            con_ = banco()
            try:
                sv_ = survey_obter(con_, survey.get("survey_id")) or {}
                am_ = survey_amostras(con_, survey.get("survey_id")) \
                      if survey.get("survey_id") else []
                mn_ = manual_listar(con_, survey.get("survey_id")) \
                      if survey.get("survey_id") else []
            finally:
                con_.close()
            if sv_:
                preencher_slides_survey(
                    p, sv_, json.loads(sv_.get("resumo") or "{}"),
                    json.loads(sv_.get("calibracao") or "{}"), mn_, None,
                    am_, banda=b)
        except Exception as e:
            log.debug(f"[ppt] preenchimento do survey: {e}")
        # 4) preenche os gráficos nativos do PPT com as faixas medidas
        try:
            atualizar_graficos_survey(p, info["caminho"], banda=b)
        except Exception as e:
            log.debug(f"[survey/ppt] graficos da banda {b}: {e}")

    resumo = {
        "linhas": info["linhas"], "equipamentos": info["bcs"],
        "bandas": bandas, "radios": info.get("radios") or [],
        "servidores": info.get("servidores") or [],
        "descartados": info["descartados"],
        "slides": total_slides, "imagens": total_imgs,
        "ini": ini, "fim": fim, "radio": survey.get("radio") or "todos",
    }
    log.info(f"[survey/ppt] {total_slides} slides, {total_imgs} imagens, "
             f"{info['linhas']} amostras de {info['bcs']} equipamento(s)")
    return resumo


def gerar_ppt(cfg, ini_d, fim_d, demo=False, survey=None):
    """Preenche o template PPT com os dados do período. Retorna (bytes, nome).

    `survey` (opcional): dict com o recorte do site survey a anexar —
    período próprio, rádio, banda e filtros. Ver anexar_survey_ao_ppt().
    """
    try:
        from pptx import Presentation
        from pptx.dml.color import RGBColor
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

    # Site survey / espectro
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

    # ── Slide 8: site survey ──
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

    # ── Site survey (opcional) ─────────────────────────────────
    # Vai por último para os slides entrarem depois dos técnicos. Falha
    # aqui não derruba o relatório inteiro: o PPT sai sem os slides de
    # survey e o motivo fica no log e no nome do arquivo.
    sufixo_sv = ""
    aviso_sv = ""
    # Survey e entrega propria (--ppt-survey / botao PPT do historico).
    # Ligue survey_no_semanal para voltar ao deck unico.
    if not cfg.getboolean("relatorio", "survey_no_semanal", fallback=False):
        remover_slides_survey(p)
        survey = None
    if survey:
        try:
            r = anexar_survey_ao_ppt(p, cfg, survey)
            if r["slides"]:
                sufixo_sv = "_com_Survey"
            else:
                aviso_sv = ("survey pedido, mas nenhum slide foi gerado "
                            "(sem amostra no periodo?)")
                log.warning(f"[survey/ppt] {aviso_sv}")
        except Exception as e:
            # Falha aqui nao derruba o relatorio, mas TEM de aparecer:
            # antes so ia para o log, e quem pediu o survey recebia um PPT
            # sem ele sem nenhuma explicacao.
            aviso_sv = f"survey nao anexado: {e}"
            log.error(f"[survey/ppt] {aviso_sv}")
    gerar_ppt.ultimo_aviso = aviso_sv

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
            f"{sufixo_sv}{'_DEMO' if demo else ''}.pptx")
    return buf.getvalue(), nome

# ──────────────────────────────────────────────────────────────
# SLIDES TÉCNICOS — construídos na identidade visual do template
# (fundo 0D2052, cartões 1A2744, cabeçalho 1A3A7A, Calibri,
#  verde 27AE60 / azul 2980B9 / laranja E67E22 / vermelho C0392B)
# ──────────────────────────────────────────────────────────────
def _slides_tecnicos(p, d, cfg, ini_d, fim_d, disp_g, meta_g, canal_poluido):
    from pptx.util import Inches, Pt, Emu
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
    # Seção 4, não 5: estes números vêm dos contadores do EXPORTER (todos
    # os rádios ativos, o período inteiro), não de uma captura de survey.
    # Enquanto se chamou "5. Site Survey — ...", a separação dos dois
    # relatórios o apagava do semanal junto com a seção de survey — e era
    # o único gráfico de espectro que o semanal tinha.
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
    # Ancorado nos slides da própria seção 4: os de survey podem não
    # existir (é o caso do semanal) e aí o slide ficava solto no fim.
    mover_apos_titulo("Ethernet e Quedas", "Links Críticos",
                      "Análise, Recomendações e Conclusão", "No Talk",
                      "Mapa de Calor"); novos += 1
    log.info(f"[ppt] {novos} slides técnicos adicionados")



# ──────────────────────────────────────────────────────────────
# GERAÇÃO DE HEATMAPS DE SITE SURVEY (a partir de CSV georreferenciado)
# Entrada: CSV com colunas de latitude/longitude + métricas medidas
# (RSSI, SNR, ruído, perda, latência). Nomes de coluna são detectados
# automaticamente entre as variantes comuns (Ekahau/TamoGraph/coleta
# própria). Saída: PNGs de heatmap, rota e histograma p/ colar no PPT.
# Requer: matplotlib, numpy, scipy (opcional, melhora a interpolação).
# ──────────────────────────────────────────────────────────────
# métrica -> (rótulo, unidade, limiar, maior_melhor, faixas do histograma, escala fixa)
# A escala fixa (vmin, vmax) reproduz as legendas do relatório de survey e é
# ABSOLUTA de propósito: normalizar pela faixa dos dados pintaria de vermelho
# o pior ponto mesmo com a área inteira dentro da especificação.
METRICAS_SURVEY = {
    "rssi":    ("Intensidade de Sinal (RSSI)", "dBm", -75, True,
                [(-200,-75),(-75,-70),(-70,-65),(-65,-60),(-60,-55),(-55,-50),(-50,-45),(-45,0)],
                (-85, -45)),
    "snr":     ("Relação Sinal/Ruído (SNR)",   "dB",   20, True,
                [(0,20),(20,25),(25,30),(30,35),(35,40),(40,200)],
                (5, 40)),
    "ruido":   ("Noise Floor",                 "dBm", -90, False,
                [(-200,-100),(-100,-95),(-95,-90),(-90,-85),(-85,0)],
                (-100, -60)),
    "perda":   ("Packet Loss",                 "%",     2, False,
                [(0,2),(2,4),(4,6),(6,8),(8,10),(10,30),(30,100)],
                (0, 100)),
    "latencia":("Latência (RTT)",              "ms",  100, False,
                [(0,10),(10,20),(20,30),(30,50),(50,70),(70,90),(90,100),
                 (100,200),(200,500),(500,100000)],
                (0, 1000)),
}

# Sinônimos aceitos nos cabeçalhos do CSV (comparação sem acento/caixa)
COLUNAS_SURVEY = {
    "lat":     ["lat","latitude","gps_lat","y"],
    "lon":     ["lon","lng","long","longitude","gps_lon","x"],
    "rssi":    ["rssi","signal","signal_strength","sinal","potencia","dbm","signalstrength"],
    "snr":     ["snr","signaltonoise","signal_to_noise","sinal_ruido","relacaosinalruido"],
    "ruido":   ["noise","ruido","noisefloor","noise_floor","piso_ruido"],
    "perda":   ["loss","packetloss","packet_loss","perda","perdapacote","perda_pacotes"],
    "latencia":["latency","rtt","latencia","roundtrip","round_trip_time","ping"],
    "banda":   ["band","banda","frequency","frequencia","freq"],
}

# ──────────────────────────────────────────────────────────────
# LEITURA DE KMZ/KML (Google Earth)
# O KMZ é um ZIP com um KML dentro. O que interessa:
#  • GroundOverlay: imagem + LatLonBox (norte/sul/leste/oeste) — a imagem
#    JÁ VEM georreferenciada, então o heatmap encaixa por coordenada, sem
#    alinhamento manual;
#  • Placemark/Point: repetidoras, torres, pontos de interesse;
#  • LineString: rotas desenhadas no Earth.
# ──────────────────────────────────────────────────────────────
KML_NS = "{http://www.opengis.net/kml/2.2}"

def ler_kmz(caminho, extrair_para="kmz_extraido"):
    """Devolve {'overlays':[{img, norte,sul,leste,oeste, rot}],
               'pontos':[{nome, lat, lon}], 'rotas':[[(lat,lon)..]]}"""
    import zipfile, xml.etree.ElementTree as ET
    caminho = Path(caminho)
    destino = Path(extrair_para); destino.mkdir(parents=True, exist_ok=True)
    kml_txt, base = None, destino

    if caminho.suffix.lower() == ".kmz" or zipfile.is_zipfile(caminho):
        with zipfile.ZipFile(caminho) as z:
            z.extractall(destino)
            nomes = z.namelist()
        kmls = [n for n in nomes if n.lower().endswith(".kml")]
        if not kmls: raise RuntimeError("KMZ sem arquivo .kml dentro")
        kml_txt = (destino / kmls[0]).read_text(encoding="utf-8", errors="replace")
    else:
        kml_txt = caminho.read_text(encoding="utf-8", errors="replace")
        base = caminho.parent

    # tolera KML sem namespace declarado
    raiz = ET.fromstring(kml_txt.encode("utf-8"))
    def achar(el, tag):
        return el.find(f"{KML_NS}{tag}") if el.find(f"{KML_NS}{tag}") is not None \
               else el.find(tag)
    def iterar(tag):
        yield from raiz.iter(f"{KML_NS}{tag}")
        yield from raiz.iter(tag)

    def num(el, tag, padrao=None):
        e = achar(el, tag)
        try: return float(e.text)
        except Exception: return padrao

    saida = {"overlays": [], "pontos": [], "rotas": [], "poligonos": [], "pastas": []}
    for f in iterar("Folder"):
        n = achar(f, "name")
        if n is not None and (n.text or "").strip():
            saida["pastas"].append(n.text.strip())

    for go in iterar("GroundOverlay"):
        icone = achar(go, "Icon")
        href = ""
        if icone is not None:
            h = achar(icone, "href")
            href = (h.text or "").strip() if h is not None else ""
        cx = achar(go, "LatLonBox") or achar(go, "LatLonAltBox")
        if cx is None or not href: continue
        img = base / href
        if not img.exists():   # href pode vir com caminho relativo diferente
            cand = list(base.rglob(Path(href).name))
            if not cand: continue
            img = cand[0]
        saida["overlays"].append({
            "img": str(img),
            "norte": num(cx,"north"), "sul": num(cx,"south"),
            "leste": num(cx,"east"),  "oeste": num(cx,"west"),
            "rot":   num(cx,"rotation", 0.0) or 0.0,
            "nome":  (achar(go,"name").text if achar(go,"name") is not None else "overlay")})

    # mapeia cada Placemark -> nome da pasta (canal/banda no BC Commander)
    pasta_de = {}
    for f in iterar("Folder"):
        nf = achar(f, "name")
        rot = (nf.text or "").strip() if nf is not None else ""
        for pm in f.iter():
            if pm.tag.endswith("Placemark"): pasta_de[id(pm)] = rot

    for pm in iterar("Placemark"):
        nome_el = achar(pm, "name")
        nome = (nome_el.text or "").strip() if nome_el is not None else ""
        if not nome:
            pt_id = pm.find(f".//{KML_NS}Point")
            if pt_id is not None: nome = pt_id.get("id") or ""
        _pasta = pasta_de.get(id(pm), "")
        for pt in pm.iter():
            if not pt.tag.endswith("coordinates") or not (pt.text or "").strip():
                continue
            bruto = pt.text.strip()
            pares = [p for p in re.split(r"\s+", bruto) if p.strip()]
            coords = []
            for p in pares:
                partes = p.split(",")
                if len(partes) >= 2:
                    try: coords.append((float(partes[1]), float(partes[0])))  # lat, lon
                    except ValueError: pass
            if len(coords) == 1:
                saida["pontos"].append({"nome": nome or "ponto",
                                        "lat": coords[0][0], "lon": coords[0][1],
                                        "pasta": _pasta})
            elif len(coords) > 1:
                # anel fechado (1º == último) = polígono; senão, rota
                fechado = (abs(coords[0][0]-coords[-1][0]) < 1e-9 and
                           abs(coords[0][1]-coords[-1][1]) < 1e-9)
                if fechado or pm.find(f".//{KML_NS}Polygon") is not None \
                           or pm.find(".//Polygon") is not None:
                    saida["poligonos"].append({"nome": nome, "pts": coords,
                                               "pasta": _pasta})
                else:
                    saida["rotas"].append(coords)
            break
    saida["bbox"] = bbox_robusto(saida)
    return saida

def bbox_robusto(kmz_dados, margem=0.02):
    """BBOX ignorando coordenadas com sinal invertido (erro comum de
    exportação: longitude sem o '-' joga o ponto para o outro hemisfério,
    esticando o mapa por meio planeta). Filtra pelo hemisfério mediano
    e depois por percentil, para nao deixar um vertice solto mandar."""
    lats, lons = [], []
    for p in kmz_dados.get("pontos", []):
        lats.append(p["lat"]); lons.append(p["lon"])
    for grupo in ("rotas",):
        for r in kmz_dados.get(grupo, []):
            for la, lo in r: lats.append(la); lons.append(lo)
    for pol in kmz_dados.get("poligonos", []):
        for la, lo in pol["pts"]: lats.append(la); lons.append(lo)
    for ov in kmz_dados.get("overlays", []):
        if None not in (ov["norte"], ov["sul"], ov["leste"], ov["oeste"]):
            lats += [ov["norte"], ov["sul"]]; lons += [ov["leste"], ov["oeste"]]
    if not lats: return None
    def limpa(vals):
        neg = sum(1 for v in vals if v < 0)
        sinal = -1 if neg >= len(vals)/2 else 1     # hemisfério dominante
        bons = [v for v in vals if (v < 0) == (sinal < 0)]
        bons.sort()
        n = len(bons)
        if n >= 20:                                  # corta 0,5% de cada ponta
            k = max(1, int(n*0.005))
            bons = bons[k:n-k]
        return bons
    la_ok, lo_ok = limpa(lats), limpa(lons)
    if not la_ok or not lo_ok: return None
    dla = (max(la_ok)-min(la_ok)) * margem
    dlo = (max(lo_ok)-min(lo_ok)) * margem
    descartados = (len(lats)-len(la_ok)) + (len(lons)-len(lo_ok))
    return {"sul": min(la_ok)-dla, "norte": max(la_ok)+dla,
            "oeste": min(lo_ok)-dlo, "leste": max(lo_ok)+dlo,
            "descartados": descartados, "total": len(lats)+len(lons)}

def _norm_col(s):
    import unicodedata
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii","ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())

def ler_csv_survey(caminho):
    """Lê o CSV e devolve {coluna_canonica: [valores]} + linhas válidas."""
    import csv
    with open(caminho, newline="", encoding="utf-8-sig", errors="replace") as f:
        amostra = f.read(4096); f.seek(0)
        try:    dialeto = csv.Sniffer().sniff(amostra, delimiters=",;\t")
        except Exception: dialeto = csv.excel
        leitor = csv.DictReader(f, dialect=dialeto)
        linhas = list(leitor)
        cabec  = leitor.fieldnames or []
    if not linhas:
        raise RuntimeError("CSV vazio")
    mapa = {}
    normalizados = {_norm_col(c): c for c in cabec}
    for canonica, sinonimos in COLUNAS_SURVEY.items():
        for s in sinonimos:
            if s in normalizados:
                mapa[canonica] = normalizados[s]; break
    if "lat" not in mapa or "lon" not in mapa:
        raise RuntimeError(
            f"CSV sem colunas de latitude/longitude reconheciveis. "
            f"Cabecalhos lidos: {cabec[:12]}")
    def num(v):
        if v is None: return None
        v = str(v).strip().replace(",", ".")
        v = re.sub(r"[^0-9.\-eE]", "", v)
        try:    return float(v)
        except Exception: return None
    dados = {"_n": 0}
    for canonica, col in mapa.items():
        dados[canonica] = [num(l.get(col)) if canonica != "banda" else (l.get(col) or "")
                           for l in linhas]
    dados["_n"] = len(linhas)
    dados["_metricas"] = [k for k in METRICAS_SURVEY if k in dados
                          and any(v is not None for v in dados[k])]
    return dados

def gerar_csv_survey_prometheus(prom, ini_ts, fim_ts, passo_s, caminho,
                                vel_min=1.0, grade_m=0.0, radio=""):
    """Monta o CSV de survey a partir das séries do Prometheus.

    A frota móvel já é um enxame de sondas: cada equipamento reporta posição
    e qualidade de RF a cada ciclo de coleta. Isto transforma esse histórico
    no mesmo CSV que `ler_csv_survey` espera de uma campanha manual, então
    todo o pipeline de heatmap / histograma / KMZ funciona sem alteração.

    Só é possível porque a auditoria contra os .proto do bcapi consertou
    três coisas: o SNR (era `rssi - abs(ruido)`, sempre negativo), a
    velocidade (lia um campo `speed` inexistente e devolvia 0) e o rumo
    (era calculado e nunca publicado).

    LIMITE DE RESOLUÇÃO — a distância entre amostras é
    velocidade × intervalo_de_coleta. A 25 km/h com coleta de 60 s dá ~420 m,
    maior que o raio de interpolação padrão (300 m). Serve para achar buraco
    de cobertura; não substitui survey fino de posicionamento de antena.
    Ver `intervalo_moveis_segundos` no config.ini.
    """
    def faixa(q):
        try:
            return prom.range(q, ini_ts, fim_ts, f"{int(passo_s)}s")
        except Exception as e:
            log.warning(f"[survey] consulta falhou ({q[:48]}...): {e}")
            return []

    def por_bc(res):
        """[{metric,values}] -> {(bc, ts): valor}"""
        saida = {}
        for s in res:
            bc = s["metric"].get("bc")
            if not bc: continue
            for ts, v in s["values"]:
                try: saida[(bc, int(float(ts)))] = float(v)
                except (TypeError, ValueError): pass
        return saida

    def por_bc_banda(res):
        """[{metric,values}] -> {(bc, freq, ts): valor}"""
        saida = {}
        for s in res:
            bc, fq = s["metric"].get("bc"), s["metric"].get("freq")
            if not bc or not fq: continue
            for ts, v in s["values"]:
                try: saida[(bc, fq, int(float(ts)))] = float(v)
                except (TypeError, ValueError): pass
        return saida

    def melhor_enlace(res, filtro_radio=None):
        """Escolhe o MELHOR enlace de cada (bc, freq, ts) preservando quem
        estava do outro lado.

        Agregar com `max by (bc, freq)` no PromQL descartava os labels
        `peer` e `radio` — e é justamente o peer que diz qual BreadCrumb
        cobria aquele ponto. Sem isso não há mapa de células nem como ver
        onde acontece handover.
        """
        saida = {}
        for s in res:
            m = s["metric"]
            bc, fq = m.get("bc"), m.get("freq")
            radio, peer = m.get("radio", ""), m.get("peer", "")
            if not bc or not fq: continue
            if filtro_radio and radio != filtro_radio: continue
            for ts, v in s["values"]:
                try: val = float(v)
                except (TypeError, ValueError): continue
                ch = (bc, fq, int(float(ts)))
                ant = saida.get(ch)
                if ant is None or val > ant[0]:
                    saida[ch] = (val, peer, radio)
        return saida

    # `rajant_peer_*` não carrega o label `freq` (LPEER = bc,ip,radio,peer).
    # O `* 0 + 1` puxa `freq` de rajant_radio_canal sem alterar o valor —
    # é o jeito de fazer join preservando a métrica original.
    # Sem `max by`: a escolha do melhor enlace é feita em Python para não
    # perder `peer` e `radio` no caminho.
    def com_banda(metrica):
        return (f'{metrica} * on (bc, ip, radio) group_left(freq) '
                f'(rajant_radio_canal * 0 + 1)')

    log.info(f"[survey] consultando Prometheus "
             f"(passo {int(passo_s)}s, vel_min {vel_min} km/h)...")
    lat  = por_bc(faixa('rajant_gps_lat'))
    lon  = por_bc(faixa('rajant_gps_lon'))
    fix  = por_bc(faixa('rajant_gps_fix'))
    vel  = por_bc(faixa('rajant_gps_vel_kmh'))
    rumo = por_bc(faixa('rajant_gps_rumo_graus'))
    perda = por_bc(faixa('avg by (bc) (rajant_im_perda_pct)'))
    lat_ms = por_bc(faixa('avg by (bc) (rajant_ping_rtt_ms)'))
    # Melhor enlace disponível naquele ponto = qualidade da cobertura ali,
    # e o `peer` desse enlace é o BreadCrumb que estava cobrindo o local.
    snr   = melhor_enlace(faixa(com_banda('rajant_peer_snr_db')), radio)
    rssi  = melhor_enlace(faixa(com_banda('rajant_peer_sinal_dbm')), radio)
    _rq = 'avg by (bc, freq) (rajant_radio_ruido_dbm'
    ruido = por_bc_banda(faixa(
        f'{_rq}{{radio="{radio}"}})' if radio else f'{_rq})'))

    if not lat or not lon:
        raise RuntimeError(
            "Prometheus não devolveu rajant_gps_lat/_lon no período. "
            "Sem GPS não há survey — confira se os equipamentos móveis têm "
            "o módulo habilitado (gpsSwitch) e se há fix.")

    bandas_vistas = sorted({k[1] for k in snr} | {k[1] for k in ruido})
    if not bandas_vistas:
        bandas_vistas = [""]

    descartados = {"sem_fix": 0, "parado": 0, "sem_rf": 0}
    linhas = []
    for (bc, ts), la in sorted(lat.items()):
        lo = lon.get((bc, ts))
        if lo is None: continue
        # gpsSwitch desabilitado / sem fix -> posição não confiável
        if fix.get((bc, ts), 1) < 1:
            descartados["sem_fix"] += 1; continue
        v = vel.get((bc, ts))
        # Equipamento parado despeja centenas de amostras na mesma coordenada
        # e enviesa a interpolação. Descartar exige a velocidade funcionando,
        # o que só passou a ser verdade depois da auditoria.
        if vel_min > 0 and v is not None and v < vel_min:
            descartados["parado"] += 1; continue
        teve_rf = False
        for fq in bandas_vistas:
            e_s = snr.get((bc, fq, ts))
            e_r = rssi.get((bc, fq, ts))
            n_  = ruido.get((bc, fq, ts))
            if e_s is None and e_r is None and n_ is None:
                continue
            teve_rf = True
            # `servidor` = peer do melhor enlace; é o BC que cobria o ponto.
            melhor = e_s or e_r
            linhas.append({
                "bc": bc, "ts": ts, "lat": round(la, 7), "lon": round(lo, 7),
                "banda": fq,
                "snr": e_s[0] if e_s else None,
                "rssi": e_r[0] if e_r else None,
                "ruido": n_,
                "servidor": melhor[1] if melhor else "",
                "radio": melhor[2] if melhor else "",
                "perda": perda.get((bc, ts)), "latencia": lat_ms.get((bc, ts)),
                "vel_kmh": v, "rumo": rumo.get((bc, ts)),
            })
        if not teve_rf:
            descartados["sem_rf"] += 1

    if not linhas:
        raise RuntimeError(
            f"Nenhuma amostra utilizável. Descartes: {descartados}. "
            f"Se 'parado' dominou, baixe --survey-vel-min; se 'sem_rf', "
            f"não há métrica de rádio casando com os instantes de GPS.")

    # Agregação por célula da grade: reduz o peso de trechos onde o
    # equipamento passou muitas vezes. Opcional — o filtro de velocidade
    # já resolve o caso do equipamento parado.
    if grade_m and grade_m > 0:
        graus = grade_m / 111_320.0          # ~metros por grau de latitude
        celulas = {}
        for r in linhas:
            ch = (round(r["lat"]/graus), round(r["lon"]/graus), r["banda"])
            celulas.setdefault(ch, []).append(r)
        agrupadas = []
        for (_, _, banda), grupo in celulas.items():
            def med(c):
                vs = [g[c] for g in grupo if g.get(c) is not None]
                return round(sum(vs)/len(vs), 2) if vs else None
            # Servidor da célula = quem serviu mais vezes ali. Média não faz
            # sentido para identificador.
            servidores = [g["servidor"] for g in grupo if g.get("servidor")]
            dominante = (max(set(servidores), key=servidores.count)
                         if servidores else "")
            agrupadas.append({
                "bc": grupo[0]["bc"], "ts": grupo[0]["ts"],
                "lat": med("lat"), "lon": med("lon"), "banda": banda,
                "snr": med("snr"), "rssi": med("rssi"), "ruido": med("ruido"),
                "servidor": dominante, "radio": grupo[0].get("radio", ""),
                "perda": med("perda"), "latencia": med("latencia"),
                "vel_kmh": med("vel_kmh"), "rumo": med("rumo"),
                "_amostras": len(grupo),
                "_servidores": len(set(servidores)),
            })
        log.info(f"[survey] grade de {grade_m:.0f} m: "
                 f"{len(linhas)} amostras -> {len(agrupadas)} células")
        linhas = agrupadas

    import csv as _csv
    colunas = ["bc", "ts", "lat", "lon", "banda", "radio", "servidor",
               "snr", "rssi", "ruido", "perda", "latencia", "vel_kmh", "rumo"]
    if any("_amostras" in r for r in linhas):
        colunas += ["_amostras", "_servidores"]
    Path(caminho).parent.mkdir(parents=True, exist_ok=True)
    with open(caminho, "w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=colunas, extrasaction="ignore")
        w.writeheader()
        for r in linhas: w.writerow(r)

    bcs = len({r["bc"] for r in linhas})
    servidores = sorted({r["servidor"] for r in linhas if r.get("servidor")})
    radios = sorted({r["radio"] for r in linhas if r.get("radio")})
    log.info(f"[survey] {caminho}: {len(linhas)} linhas | {bcs} equipamentos "
             f"| bandas {bandas_vistas} | radios {radios or '-'} "
             f"| {len(servidores)} BCs servindo | descartes {descartados}")
    return {"linhas": len(linhas), "bcs": bcs, "bandas": bandas_vistas,
            "radios": radios, "servidores": servidores,
            "descartados": descartados, "caminho": str(caminho)}


def _filtra_banda(dados, banda):
    """Filtra os pontos por banda (2.4 / 5). Aceita 'banda' textual ou
    frequência numérica em MHz/GHz."""
    if not banda or "banda" not in dados:
        return dados
    alvo = "2.4" if "2" in str(banda)[:3] else "5"
    idx = []
    for i, v in enumerate(dados["banda"]):
        s = str(v)
        try:
            f = float(str(v).replace(",", "."))
            ghz = f/1000.0 if f > 100 else f
            marca = "2.4" if ghz < 3 else "5"
        except Exception:
            marca = "2.4" if ("2.4" in s or "2,4" in s or "2400" in s) else (
                    "5" if ("5" in s) else "")
        if marca == alvo: idx.append(i)
    if not idx: return dados
    saida = {"_n": len(idx), "_metricas": dados["_metricas"]}
    for k, v in dados.items():
        if k.startswith("_"): continue
        saida[k] = [v[i] for i in idx]
    return saida

def gerar_imagens_survey(csv_path, saida_dir, banda=None, fundo=None,
                         kmz=None, bcs_gps=None, kmz_fundo='auto',
                         kmz_pontos=False, fundo_bbox=None, raio_m=300.0):
    """Gera PNGs (rota, heatmaps e histogramas) e devolve {chave: caminho}."""
    import math
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import LinearSegmentedColormap, BoundaryNorm

    dados = _filtra_banda(ler_csv_survey(csv_path), banda)
    saida = Path(saida_dir); saida.mkdir(parents=True, exist_ok=True)

    # ── Fundo georreferenciado vindo do KMZ ──
    kmz_dados = None
    if kmz:
        try:
            kmz_dados = ler_kmz(kmz, str(Path(saida_dir) / "kmz"))
            log.info(f"[survey] KMZ: {len(kmz_dados['overlays'])} overlay(s), "
                     f"{len(kmz_dados['pontos'])} ponto(s), "
                     f"{len(kmz_dados.get('poligonos', []))} poligono(s)")
            if not kmz_dados["overlays"] and not fundo:
                log.warning("[survey] KMZ sem GroundOverlay e sem --survey-fundo: "
                            "usando o contorno vetorial como base.")
            bbk = kmz_dados.get("bbox")
            if bbk and bbk.get("descartados"):
                log.warning(f"[survey] {bbk['descartados']} coordenadas com sinal "
                            f"invertido descartadas do enquadramento")
        except Exception as e:
            log.error(f"[survey] falha ao ler KMZ: {e}")
    sufixo = ("_" + str(banda).replace(".", "").replace(" ", "")) if banda else ""
    imgs = {}

    bb = (kmz_dados or {}).get("bbox")
    lat = np.array([v if v is not None else np.nan for v in dados["lat"]], dtype=float)
    lon = np.array([v if v is not None else np.nan for v in dados["lon"]], dtype=float)
    val_ok = ~(np.isnan(lat) | np.isnan(lon))
    if val_ok.sum() < 3:
        raise RuntimeError("Menos de 3 pontos com coordenadas validas no CSV")

    # Paleta do survey: vermelho (ruim) → verde (bom)
    CORES = ["#C0392B","#E74C3C","#E67E22","#F1C40F","#9ACD32","#27AE60","#1E8449"]
    cmap_bom_alto  = LinearSegmentedColormap.from_list("survey", CORES)
    cmap_bom_baixo = LinearSegmentedColormap.from_list("survey_i", list(reversed(CORES)))

    # ── Proporção geográfica correta ──
    # 1 grau de longitude "encolhe" com cos(latitude). Sem corrigir, uma
    # mina alta e estreita aparece achatada e larga — leitura enganosa.
    _ref = fundo_bbox or bb or {
        "norte": float(np.nanmax(lat)), "sul": float(np.nanmin(lat)),
        "leste": float(np.nanmax(lon)), "oeste": float(np.nanmin(lon))}
    _lat_med = (_ref["norte"] + _ref["sul"]) / 2.0
    _fator   = 1.0 / max(math.cos(math.radians(_lat_med)), 1e-6)   # aspecto y/x
    _gw = abs(_ref["leste"] - _ref["oeste"]) / _fator
    _gh = abs(_ref["norte"] - _ref["sul"])
    _r  = (_gw / _gh) if _gh else 1.0
    # figura acompanha o formato do terreno (altura fixa de 7,2")
    FIG_H = 7.2
    FIG_W = max(4.2, min(12.0, FIG_H * _r + 2.6))   # +2.6" p/ colorbar/eixos

    def enquadrar(ax):
        """Fixa limites e proporção na área de referência (bbox do KMZ/foto).
        DEPOIS dos imshow: cada imshow(aspect="auto") reseta o aspecto dos
        eixos, então definir a proporção antes não tem efeito."""
        ax.set_xlim(_ref["oeste"], _ref["leste"])
        ax.set_ylim(_ref["sul"],   _ref["norte"])
        ax.set_aspect(_fator)

    def moldura(ax):
        ax.set_aspect(_fator)   # eixos em proporção geográfica real
        ax.set_facecolor("#0D2052")
        for s in ax.spines.values(): s.set_color("#2A3A5C")
        ax.tick_params(colors="#A0B0CC", labelsize=7)
        ax.set_xlabel("Longitude", color="#A0B0CC", fontsize=8)
        ax.set_ylabel("Latitude",  color="#A0B0CC", fontsize=8)

    def fundo_img(ax):
        """Desenha o fundo. Com KMZ, usa o LatLonBox do GroundOverlay —
        a imagem fica na posição geográfica correta, sem ajuste manual."""
        usou = False
        if kmz_dados:
            for ov in kmz_dados["overlays"]:
                if None in (ov["norte"], ov["sul"], ov["leste"], ov["oeste"]):
                    continue
                try:
                    img = plt.imread(ov["img"])
                    ax.imshow(img, extent=[ov["oeste"], ov["leste"],
                                           ov["sul"],   ov["norte"]],
                              aspect="auto", zorder=0, alpha=0.95,
                              interpolation="bilinear")
                    usou = True
                except Exception as e:
                    log.warning(f"[survey] overlay '{ov['img']}' nao carregado: {e}")
        # 2º) Imagem passada em --survey-fundo. O enquadramento correto é o
        # BBOX do KMZ (foi com ele que a foto foi exportada); usar os limites
        # dos pontos medidos esticaria a imagem e deslocaria tudo.
        if not usou and fundo and Path(fundo).exists():
            try:
                img = plt.imread(fundo)
                if fundo_bbox:
                    ext = [fundo_bbox["oeste"], fundo_bbox["leste"],
                           fundo_bbox["sul"],   fundo_bbox["norte"]]
                elif bb:
                    ext = [bb["oeste"], bb["leste"], bb["sul"], bb["norte"]]
                else:
                    ext = [lon[val_ok].min(), lon[val_ok].max(),
                           lat[val_ok].min(), lat[val_ok].max()]
                ax.imshow(img, extent=ext, aspect="auto", zorder=0,
                          alpha=0.95, interpolation="bilinear")
                usou = True
            except Exception as e:
                log.warning(f"[survey] fundo nao carregado: {e}")
        # 3º) Contorno vetorial do KMZ (quando não há imagem alguma)
        if not usou and kmz_dados and kmz_fundo in ("auto", "vetorial"):
            for pol in kmz_dados.get("poligonos", []):
                pts = [(lo, la) for la, lo in pol["pts"]
                       if bb is None or (bb["oeste"] <= lo <= bb["leste"]
                                         and bb["sul"] <= la <= bb["norte"])]
                if len(pts) < 3: continue
                xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
                ax.plot(xs, ys, "-", color="#4A5A7C", lw=0.7, alpha=0.85, zorder=1)
            usou = bool(kmz_dados.get("poligonos"))

    def marcadores(ax, rotular=True):
        """Repetidoras/torres do KMZ + BCs com GPS (como no BC Commander)."""
        pontos = list(kmz_dados["pontos"]) if (kmz_dados and kmz_pontos) else []
        for nome, la_, lo_ in (bcs_gps or []):
            pontos.append({"nome": nome, "lat": la_, "lon": lo_})
        if not pontos: return
        px = [p["lon"] for p in pontos]; py = [p["lat"] for p in pontos]
        ax.scatter(px, py, marker="^", s=95, c="#F1C40F",
                   edgecolor="#0D2052", linewidths=1.1, zorder=6,
                   label="Repetidoras / BCs")
        if rotular:
            for p in pontos[:40]:
                ax.annotate(p["nome"][:16], (p["lon"], p["lat"]),
                            textcoords="offset points", xytext=(5, 4),
                            fontsize=6, color="#F1C40F", zorder=7)

    # ── 1. Rota percorrida ──
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H), dpi=140)
    fig.patch.set_facecolor("#0D2052"); moldura(ax); fundo_img(ax)
    # ── Traçado da rota ──
    # Linha única e grossa com 1.100 pontos sobrepostos vira um emaranhado
    # sem sentido de percurso. Aqui: segmentos coloridos pela PROGRESSÃO
    # (início → fim), setas esparsas indicando o sentido e marcadores só
    # nas pontas. Fica claro por onde o veículo passou e em que ordem.
    from matplotlib.collections import LineCollection
    rx, ry = lon[val_ok], lat[val_ok]
    segs = np.stack([np.c_[rx[:-1], ry[:-1]], np.c_[rx[1:], ry[1:]]], axis=1)
    prog = np.linspace(0, 1, len(segs))
    lc = LineCollection(segs, cmap="viridis", array=prog, linewidths=2.0,
                        alpha=0.95, zorder=3, capstyle="round")
    ax.add_collection(lc)
    cbp = fig.colorbar(lc, ax=ax, shrink=0.7, pad=0.02)
    cbp.set_label("progressão do percurso", color="#C8D0E0", fontsize=8)
    cbp.ax.tick_params(colors="#A0B0CC", labelsize=7)
    cbp.set_ticks([0, 1]); cbp.set_ticklabels(["início", "fim"])
    cbp.outline.set_edgecolor("#2A3A5C")
    # setas de sentido a cada ~8% do trajeto
    passo = max(1, len(rx)//12)
    for i in range(passo, len(rx)-1, passo):
        dx, dy = rx[i+1]-rx[i], ry[i+1]-ry[i]
        if dx == 0 and dy == 0: continue
        ax.annotate("", xy=(rx[i]+dx*4, ry[i]+dy*4), xytext=(rx[i], ry[i]),
                    arrowprops=dict(arrowstyle="-|>", color="#FFFFFF",
                                    lw=1.0, alpha=0.85), zorder=5)
    ax.scatter(rx[0], ry[0], s=110, marker="o", facecolor="#27AE60",
               edgecolor="white", lw=1.6, zorder=6, label="Início")
    ax.scatter(rx[-1], ry[-1], s=130, marker="s", facecolor="#C0392B",
               edgecolor="white", lw=1.6, zorder=6, label="Fim")
    marcadores(ax)
    ax.set_title(f"Rota Percorrida{(' — ' + str(banda)) if banda else ''}  "
                 f"({int(val_ok.sum())} pontos)", color="white", fontsize=11, fontweight="bold")
    leg = ax.legend(loc="upper right", fontsize=8, facecolor="#1A2744", edgecolor="#2A3A5C")
    for t in leg.get_texts(): t.set_color("#C8D0E0")
    enquadrar(ax)
    p_rota = saida / f"rota{sufixo}.png"
    fig.tight_layout(); fig.savefig(p_rota, facecolor="#0D2052"); plt.close(fig)
    imgs["rota"] = str(p_rota)

    # ── 2. Heatmap + 3. Histograma por métrica ──
    for met in dados.get("_metricas", []):
        rotulo, unid, limiar, maior_melhor, faixas, escala = METRICAS_SURVEY[met]
        vmin, vmax = escala
        v = np.array([x if x is not None else np.nan for x in dados[met]], dtype=float)
        ok = val_ok & ~np.isnan(v)
        if ok.sum() < 3: continue
        x, y, z = lon[ok], lat[ok], v[ok]

        # Interpolação em grade (griddata; fallback = dispersão densa)
        fig, ax = plt.subplots(figsize=(FIG_W, FIG_H), dpi=140)
        fig.patch.set_facecolor("#0D2052"); moldura(ax); fundo_img(ax)
        cmap = cmap_bom_alto if maior_melhor else cmap_bom_baixo
        try:
            from scipy.spatial import cKDTree
            # ── Cobertura de TODA a área da mina ──
            # A rota é um corredor estreito; interpolar só nela deixa 90% do
            # mapa vazio. Aqui a grade cobre o bbox inteiro e o valor de cada
            # célula vem por IDW (média ponderada pelo inverso da distância)
            # dos k pontos medidos mais próximos, até um raio de influência.
            # Além desse raio não há base para afirmar nada: fica sem cor.
            gx = np.linspace(_ref["oeste"], _ref["leste"], 420)
            gy = np.linspace(_ref["sul"],   _ref["norte"], 420)
            GX, GY = np.meshgrid(gx, gy)
            # graus -> metros aproximados (lon encolhe por cos(lat))
            mlat = 111320.0
            mlon = 111320.0 * math.cos(math.radians(_lat_med))
            P  = np.c_[x*mlon, y*mlat]
            Q  = np.c_[GX.ravel()*mlon, GY.ravel()*mlat]
            arv = cKDTree(P)
            k = min(12, len(P))
            dist, idx = arv.query(Q, k=k)
            if k == 1: dist, idx = dist[:, None], idx[:, None]
            with np.errstate(divide="ignore", invalid="ignore"):
                w = 1.0 / np.maximum(dist, 1.0)**2
                w[dist > raio_m] = 0.0
                soma = w.sum(axis=1)
                GZ = np.where(soma > 0, (w * z[idx]).sum(axis=1) / np.maximum(soma, 1e-12), np.nan)
            GZ = GZ.reshape(GX.shape)
            im = ax.imshow(GZ, extent=[_ref["oeste"], _ref["leste"],
                                       _ref["sul"], _ref["norte"]],
                           origin="lower", cmap=cmap, aspect="auto",
                           zorder=3, alpha=0.72, interpolation="bilinear",
                           vmin=vmin, vmax=vmax)
            cobertos = np.isfinite(GZ).mean()
        except Exception as e:
            log.warning(f"[survey] interpolacao por area falhou ({e}); usando pontos")
            im = ax.scatter(x, y, c=z, cmap=cmap, s=26, zorder=3,
                            vmin=vmin, vmax=vmax)
            cobertos = None
        marcadores(ax, rotular=False)
        cb = fig.colorbar(im, ax=ax, shrink=0.85)
        cb.set_label(f"{rotulo} ({unid})", color="#C8D0E0", fontsize=9)
        cb.ax.tick_params(colors="#A0B0CC", labelsize=7)
        cb.outline.set_edgecolor("#2A3A5C")
        # linha de limiar na barra de cores
        try:
            cb.ax.axhline(limiar, color="white", lw=1.6, ls="--")
        except Exception: pass
        fora = (z < limiar).sum() if maior_melhor else (z > limiar).sum()
        pct_ok = 100.0 * (1 - fora/len(z))
        extra_cob = (f"  •  área estimada: {cobertos*100:.0f}% da mina"
                     if cobertos else "")
        ax.set_title(f"{rotulo}{(' — ' + str(banda)) if banda else ''}\n"
                     f"{pct_ok:.1f}% dos pontos dentro do requerido "
                     f"({'>' if maior_melhor else '<'} {limiar} {unid}){extra_cob}",
                     color="white", fontsize=10.5, fontweight="bold")
        enquadrar(ax)
        p_hm = saida / f"heatmap_{met}{sufixo}.png"
        fig.tight_layout(); fig.savefig(p_hm, facecolor="#0D2052"); plt.close(fig)
        imgs[f"heatmap_{met}"] = str(p_hm)

        # Histograma por faixa (mesmo padrão do relatório de survey)
        rotulos, pcts = [], []
        for a, b in faixas:
            sel = (z >= a) & (z < b)
            pcts.append(100.0 * sel.sum() / len(z))
            if   a <= -200: rotulos.append(f"< {b:g}")
            elif b >= 100000 or b == 0 or b >= 200: rotulos.append(f"≥ {a:g}")
            else: rotulos.append(f"{a:g}–{b:g}")
        fig, ax = plt.subplots(figsize=(8, 5), dpi=140)
        fig.patch.set_facecolor("#0D2052"); ax.set_facecolor("#0D2052")
        cores_barras = [cmap(i/(max(len(faixas)-1,1))) for i in range(len(faixas))]
        barras = ax.bar(rotulos, pcts, color=cores_barras, edgecolor="#0D2052")
        for b_, p_ in zip(barras, pcts):
            if p_ > 0.05:
                ax.text(b_.get_x()+b_.get_width()/2, p_+1.2, f"{p_:.1f}%",
                        ha="center", color="#C8D0E0", fontsize=8)
        ax.set_ylim(0, max(100, max(pcts)*1.15))
        ax.set_ylabel("% dos pontos medidos", color="#A0B0CC", fontsize=9)
        ax.set_xlabel(f"{rotulo} ({unid})", color="#A0B0CC", fontsize=9)
        ax.tick_params(colors="#A0B0CC", labelsize=8)
        ax.grid(axis="y", color="#2A3A5C", lw=0.5)
        ax.set_axisbelow(True)
        for s in ax.spines.values(): s.set_color("#2A3A5C")
        ax.set_title(f"Distribuição — {rotulo}{(' — ' + str(banda)) if banda else ''}",
                     color="white", fontsize=11, fontweight="bold")
        p_hi = saida / f"hist_{met}{sufixo}.png"
        fig.tight_layout(); fig.savefig(p_hi, facecolor="#0D2052"); plt.close(fig)
        imgs[f"hist_{met}"] = str(p_hi)

    imgs["_pontos"] = int(val_ok.sum())
    imgs["_metricas"] = dados.get("_metricas", [])
    return imgs

def _pcts_por_faixa(valores, faixas):
    vals = [v for v in valores if v is not None]
    if not vals: return None
    n = len(vals)
    return [round(100.0*sum(1 for v in vals if a <= v < b)/n, 2) for a, b in faixas]

def atualizar_graficos_survey(p, csv_path, banda=None):
    """Preenche os histogramas NATIVOS dos slides com os percentuais reais
    do CSV (mantendo-os editáveis no PowerPoint)."""
    from pptx.chart.data import CategoryChartData
    dados = _filtra_banda(ler_csv_survey(csv_path), banda)
    alvos = {"rssi": "Intensidade de Sinal (RSSI)", "snr": "Relação Sinal/Ruído (SNR)",
             "ruido": "Noise Floor", "perda": "Packet Loss", "latencia": "Latência (RTT)"}
    atualizados = 0
    for met, titulo in alvos.items():
        if met not in dados: continue
        faixas = METRICAS_SURVEY[met][4]
        pcts = _pcts_por_faixa(dados[met], faixas)
        if not pcts: continue
        alvo = f"{titulo} — {banda}" if banda else titulo
        _, s = _slide_por_titulo(p, alvo)
        if s is None: continue
        for sh in s.shapes:
            if not sh.has_chart: continue
            ch = sh.chart
            cats = [c for c in ch.plots[0].categories]
            if len(cats) != len(pcts): continue
            cd = CategoryChartData(); cd.categories = cats
            cd.add_series("% da área", pcts)
            ch.replace_data(cd)
            atualizados += 1
            break
    return atualizados

def mapa_cobertura(kmz_dados, saida_dir, banda="2.4 GHz", fundo=None,
                   fundo_bbox=None, ptx_dbm=25.0, ganho_dbi=8.0,
                   expoente=2.3, rotular=True):
    """Cobertura estimada de RF em TODA a área da mina, a partir das posições
    reais dos BreadCrumbs (KML do BC Commander).

    Modelo log-distância:
        RSSI(d) = Ptx + Gtx + Grx - [FSPL(1m) + 10*n*log10(d)]
        FSPL(1m) = 20*log10(f_MHz) - 27.55

    Parâmetros calibrados para cava a céu aberto: Ptx 25 dBm, ganho de
    8 dBi em cada ponta (antenas típicas Rajant) e n = 2,3 — entre o espaço
    livre (2,0) e o ambiente obstruído (3,0+). Ignorar os ganhos e usar
    n alto subestima o alcance em mais de 30 dB e faz a mina inteira
    parecer sem cobertura.

    É uma ESTIMATIVA de planejamento, não medição: serve para achar
    buracos de cobertura onde o survey não passou. Ajuste com
    --cobertura-expoente / --cobertura-ptx / --cobertura-ganho.
    """
    import math
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import LinearSegmentedColormap
    from pathlib import Path as _P

    pontos = [p for p in (kmz_dados or {}).get("pontos", [])]
    if len(pontos) < 2:
        raise RuntimeError("KML sem posicoes de BreadCrumbs suficientes")
    bb = fundo_bbox or kmz_dados.get("bbox")
    if not bb: raise RuntimeError("sem area de referencia (bbox)")

    f_mhz = 2437.0 if str(banda).startswith("2") else 5745.0
    lat_med = (bb["norte"] + bb["sul"]) / 2.0
    mlat = 111320.0; mlon = 111320.0 * math.cos(math.radians(lat_med))

    px = np.array([p["lon"] for p in pontos]) * mlon
    py = np.array([p["lat"] for p in pontos]) * mlat

    N = 460
    gx = np.linspace(bb["oeste"], bb["leste"], N)
    gy = np.linspace(bb["sul"],   bb["norte"], N)
    GX, GY = np.meshgrid(gx, gy)
    QX, QY = GX*mlon, GY*mlat

    fspl1 = 20*math.log10(f_mhz) - 27.55
    melhor = np.full(QX.shape, -200.0)
    for bx, by in zip(px, py):
        d = np.hypot(QX-bx, QY-by)
        np.maximum(d, 1.0, out=d)
        rssi = (ptx_dbm + 2*ganho_dbi) - (fspl1 + 10*expoente*np.log10(d))
        np.maximum(melhor, rssi, out=melhor)

    CORES = ["#C0392B","#E74C3C","#E67E22","#F1C40F","#9ACD32","#27AE60","#1E8449"]
    cmap = LinearSegmentedColormap.from_list("cob", CORES)
    fator = 1.0/max(math.cos(math.radians(lat_med)), 1e-6)
    gwid = abs(bb["leste"]-bb["oeste"])/fator; ghei = abs(bb["norte"]-bb["sul"])
    FIG_H = 7.2; FIG_W = max(4.2, min(12.0, FIG_H*(gwid/ghei) + 2.6))

    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H), dpi=140)
    fig.patch.set_facecolor("#0D2052"); ax.set_facecolor("#0D2052")
    if fundo and _P(fundo).exists():
        try:
            ax.imshow(plt.imread(fundo), extent=[bb["oeste"], bb["leste"],
                                                 bb["sul"], bb["norte"]],
                      aspect="auto", zorder=0, alpha=0.95, interpolation="bilinear")
        except Exception as e:
            log.warning(f"[cobertura] fundo: {e}")
    im = ax.imshow(melhor, extent=[bb["oeste"], bb["leste"], bb["sul"], bb["norte"]],
                   origin="lower", cmap=cmap, aspect="auto", zorder=2,
                   alpha=0.62, vmin=-85, vmax=-45, interpolation="bilinear")
    # curva do limiar de -75 dBm: a fronteira da cobertura aceitável
    try:
        cs = ax.contour(GX, GY, melhor, levels=[-75], colors="#FFFFFF",
                        linewidths=1.4, zorder=4)
        ax.clabel(cs, fmt="-75 dBm", fontsize=7, colors="#FFFFFF")
    except Exception: pass

    ax.scatter([p["lon"] for p in pontos], [p["lat"] for p in pontos],
               marker="^", s=70, c="#F1C40F", edgecolor="#0D2052",
               linewidths=1.0, zorder=6)
    if rotular:
        for p in pontos:
            ax.annotate(p["nome"][:12], (p["lon"], p["lat"]),
                        textcoords="offset points", xytext=(4, 3),
                        fontsize=5.5, color="#F1C40F", zorder=7)
    cb = fig.colorbar(im, ax=ax, shrink=0.85)
    cb.set_label("RSSI estimado (dBm)", color="#C8D0E0", fontsize=9)
    cb.ax.tick_params(colors="#A0B0CC", labelsize=7)
    cb.outline.set_edgecolor("#2A3A5C")
    dentro = 100.0*(melhor >= -75).mean()
    ax.set_title(f"Cobertura estimada — {banda}\n"
                 f"{len(pontos)} BreadCrumbs  •  {dentro:.0f}% da área ≥ -75 dBm  "
                 f"(modelo log-distância n={expoente}, Ptx {ptx_dbm:.0f} dBm, "
                 f"ganho {ganho_dbi:.0f} dBi)",
                 color="white", fontsize=10.5, fontweight="bold")
    ax.set_xlabel("Longitude", color="#A0B0CC", fontsize=8)
    ax.set_ylabel("Latitude",  color="#A0B0CC", fontsize=8)
    ax.tick_params(colors="#A0B0CC", labelsize=7)
    for s in ax.spines.values(): s.set_color("#2A3A5C")
    ax.set_xlim(bb["oeste"], bb["leste"]); ax.set_ylim(bb["sul"], bb["norte"])
    ax.set_aspect(fator)
    saida = _P(saida_dir); saida.mkdir(parents=True, exist_ok=True)
    arq = saida / f"cobertura_{str(banda).replace('.','').replace(' ','')}.png"
    fig.tight_layout(); fig.savefig(arq, facecolor="#0D2052"); plt.close(fig)
    return str(arq), dentro

# ──────────────────────────────────────────────────────────────
# GERAÇÃO DE KMZ PARA O GOOGLE EARTH
# Em vez de desenhar o mapa (e competir com a imagem do Earth), o
# programa exporta os DADOS georreferenciados: heatmaps como overlays
# transparentes, rotas dos equipamentos, posições dos BreadCrumbs e
# legenda. O Earth entrega satélite, relevo e zoom — e o print sai
# com a qualidade que a apresentação precisa.
#
# Detalhes de KML que moldam o código:
#  • cor é AABBGGRR (alfa primeiro, RGB invertido) — não RGB;
#  • GroundOverlay precisa de LatLonBox para georreferenciar a imagem;
#  • pastas com <open>0</open> nascem recolhidas; cada uma vira um
#    checkbox no Earth (é assim que se liga/desliga 2,4 e 5,8 GHz).
# ──────────────────────────────────────────────────────────────
def _kml_cor(rgb_hex, alfa=255):
    """'27AE60' -> 'ff60ae27' (AABBGGRR)."""
    r, g, b = rgb_hex[0:2], rgb_hex[2:4], rgb_hex[4:6]
    return f"{alfa:02x}{b}{g}{r}".lower()

def _esc(t):
    from xml.sax.saxutils import escape
    return escape(str(t or ""))

def grade_idw_m(x, y, z, bb, lat_med, raio_m=300.0, n=520, k_viz=12):
    """Interpolação IDW numa grade regular. NaN onde nenhuma amostra está
    dentro de `raio_m` — é isso que impede o mapa de inventar cobertura
    onde a frota não passou.

    Versão de módulo: a mesma conta estava aninhada dentro do gerador de
    KMZ antigo e não dava para reusar no KML novo.
    """
    import numpy as np
    from scipy.spatial import cKDTree
    mlat = 111320.0; mlon = 111320.0*math.cos(math.radians(lat_med))
    gx = np.linspace(bb["oeste"], bb["leste"], n)
    gy = np.linspace(bb["sul"],   bb["norte"], n)
    GX, GY = np.meshgrid(gx, gy)
    arv = cKDTree(np.c_[np.asarray(x)*mlon, np.asarray(y)*mlat])
    k = min(k_viz, len(x))
    d, idx = arv.query(np.c_[GX.ravel()*mlon, GY.ravel()*mlat], k=k)
    if k == 1: d, idx = d[:, None], idx[:, None]
    z = np.asarray(z, float)
    with np.errstate(divide="ignore", invalid="ignore"):
        w = 1.0/np.maximum(d, 1.0)**2
        w[d > raio_m] = 0.0
        s = w.sum(axis=1)
        G = np.where(s > 0, (w*z[idx]).sum(axis=1)/np.maximum(s, 1e-12), np.nan)
    return G.reshape(GX.shape)


def _png_overlay(GZ, bbox, cmap, vmin, vmax, caminho, alfa=0.72):
    """Raster puro (sem eixos/moldura/fundo) para servir de GroundOverlay:
    o pixel tem que corresponder exatamente ao LatLonBox, senão o mapa
    'escorrega' sobre o terreno."""
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    norm = np.clip((GZ - vmin) / max(vmax - vmin, 1e-9), 0, 1)
    rgba = cmap(norm)
    rgba[..., 3] = np.where(np.isfinite(GZ), alfa, 0.0)   # sem dado = transparente
    plt.imsave(caminho, np.flipud(rgba))                   # KML espera norte no topo
    return caminho

def _legenda_png(caminho, titulo, faixas_rotulos, cores, largura=340, altura=None):
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    n = len(faixas_rotulos)
    altura = altura or (28 + 22*n)
    fig = plt.figure(figsize=(largura/100, altura/100), dpi=100)
    fig.patch.set_facecolor("#0D2052"); fig.patch.set_alpha(0.88)
    ax = fig.add_axes([0, 0, 1, 1]); ax.axis("off")
    ax.text(0.04, 1 - 14/altura, titulo, color="white", fontsize=9,
            fontweight="bold", va="top")
    for i, (rot, cor) in enumerate(zip(faixas_rotulos, cores)):
        y = 1 - (34 + 22*i)/altura
        ax.add_patch(plt.Rectangle((0.04, y-0.035), 0.10, 0.055,
                                   facecolor="#"+cor, edgecolor="none",
                                   transform=ax.transAxes))
        ax.text(0.18, y-0.008, rot, color="#C8D0E0", fontsize=8, va="center")
    fig.savefig(caminho, facecolor=fig.get_facecolor(), transparent=False)
    plt.close(fig)
    return caminho

def calibrar_propagacao(medidas, pontos_bc, banda="2.4 GHz"):
    """Ajusta o modelo de propagação AOS DADOS MEDIDOS, em vez de usar
    valores de catálogo.

    Para cada medição (lat, lon, sinal) calcula a distância ao BC mais
    próximo e resolve por mínimos quadrados:
        RSSI = A - 10*n*log10(d)
    devolvendo (A, n, alcance_m, qtd, erro_db). O alcance é a distância
    onde o modelo cruza -85 dBm — é o raio real das manchas, não um chute.

    Sem medições suficientes, cai no padrão de catálogo (A=57, n=2.3).
    """
    import math
    import numpy as np
    pontos_bc, _ = limpar_coords(pontos_bc)
    padrao = (57.0, 2.3, 900.0, 0, None)
    if not pontos_bc or not medidas: return padrao
    lat_med = sum(p[1] for p in pontos_bc)/len(pontos_bc)
    mlat = 111320.0; mlon = 111320.0*math.cos(math.radians(lat_med))
    bx = np.array([p[2]*mlon for p in pontos_bc])
    by = np.array([p[1]*mlat for p in pontos_bc])
    D, S = [], []
    for m in medidas:
        la, lo, sinal = m[0], m[1], m[2]
        if sinal is None or la is None or lo is None: continue
        if not (-100 <= sinal <= -20): continue
        d = np.hypot(bx - lo*mlon, by - la*mlat).min()
        if d < 20 or d > 6000: continue      # perto demais/longe demais distorce
        D.append(d); S.append(sinal)
    if len(D) < 25: return padrao
    D = np.array(D); S = np.array(S)
    X = np.c_[np.ones(len(D)), -10*np.log10(D)]
    coef, *_ = np.linalg.lstsq(X, S, rcond=None)
    A, n = float(coef[0]), float(coef[1])
    if not (1.6 <= n <= 4.5) or not (20 <= A <= 90):   # ajuste implausível
        return padrao
    erro = float(np.sqrt(np.mean((X @ coef - S)**2)))
    alcance = 10 ** ((A + 85.0) / (10*n))
    alcance = max(150.0, min(alcance, 3000.0))
    return A, n, alcance, len(D), erro

def heat_infra_png(pontos_bc, bbox, caminho, banda="2.4 GHz", alcance_m=1000.0,
                   ptx_dbm=25.0, ganho_dbi=8.0, expoente=2.3,
                   alfa_max=0.62, piso_dbm=-90.0, teto_dbm=-55.0, n=760):
    """Mapa de calor no estilo do relatório: manchas de cobertura em volta de
    CADA ERM/ERB, não a mina inteira pintada.

    Duas escolhas fazem o visual:
      • a cor vem do melhor RSSI estimado no ponto (verde bom → vermelho ruim),
        na escala piso_dbm..teto_dbm (padrão -90..-55: o requisito Modular
        é -75 dBm, então a faixa útil fica no meio e a leitura não vira um
        tapete laranja);
      • a OPACIDADE cai com a distância ao rádio mais próximo e zera em
        `alcance_m`. É isso que produz as ilhas de cobertura e deixa o
        terreno do Earth aparecer entre elas, em vez de um tapete opaco.
    """
    import math
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import LinearSegmentedColormap

    if not pontos_bc:
        raise RuntimeError("sem posicoes de ERM/ERB para o mapa de calor")
    CORES = ["#C0392B","#E74C3C","#E67E22","#F1C40F","#9ACD32","#27AE60","#1E8449"]
    cmap = LinearSegmentedColormap.from_list("rf", CORES)

    f_mhz  = 2437.0 if str(banda).startswith("2") else 5745.0
    lat_med = (bbox["norte"] + bbox["sul"]) / 2.0
    mlat = 111320.0; mlon = 111320.0 * math.cos(math.radians(lat_med))

    # grade proporcional à área (evita pixel esticado)
    gw = abs(bbox["leste"]-bbox["oeste"]) * mlon
    gh = abs(bbox["norte"]-bbox["sul"])  * mlat
    nx = int(n * (gw/max(gh, 1e-9))) if gh > gw else n
    ny = n if gh >= gw else int(n * (gh/max(gw, 1e-9)))
    nx, ny = max(nx, 120), max(ny, 120)

    gx = np.linspace(bbox["oeste"], bbox["leste"], nx)
    gy = np.linspace(bbox["sul"],   bbox["norte"], ny)
    GX, GY = np.meshgrid(gx, gy)
    QX, QY = GX*mlon, GY*mlat

    fspl1 = 20*math.log10(f_mhz) - 27.55
    melhor = np.full(QX.shape, -200.0)
    dmin   = np.full(QX.shape,  1e12)
    for _, la, lo in pontos_bc:
        d = np.hypot(QX - lo*mlon, QY - la*mlat)
        np.minimum(dmin, d, out=dmin)
        np.maximum(d, 1.0, out=d)
        np.maximum(melhor, (ptx_dbm + 2*ganho_dbi) - (fspl1 + 10*expoente*np.log10(d)),
                   out=melhor)

    val = np.clip((melhor - piso_dbm) / max(teto_dbm - piso_dbm, 1e-9), 0, 1)
    rgba = cmap(val)
    # opacidade some com a distância (queda suave, borda limpa)
    fade = np.clip(1.0 - (dmin/alcance_m)**1.6, 0, 1)
    rgba[..., 3] = alfa_max * fade
    plt.imsave(caminho, np.flipud(rgba))
    cobertura_pct = 100.0 * (fade > 0.02).mean()
    dentro_pct    = 100.0 * ((melhor >= -75) & (fade > 0.02)).sum() / max((fade > 0.02).sum(), 1)
    return caminho, cobertura_pct, dentro_pct

def limpar_coords(pontos):
    """Remove pontos com coordenada no hemisfério errado (longitude exportada
    sem o '-'). Um único ponto desses estica a área do mapa por milhares de
    km e faz a grade do heatmap degenerar."""
    if not pontos: return pontos, 0
    lats = [p[1] for p in pontos]; lons = [p[2] for p in pontos]
    def sinal(vs):
        return -1 if sum(1 for v in vs if v < 0) >= len(vs)/2 else 1
    sla, slo = sinal(lats), sinal(lons)
    bons = [p for p in pontos
            if (p[1] < 0) == (sla < 0) and (p[2] < 0) == (slo < 0)
            and abs(p[1]) <= 90 and abs(p[2]) <= 180]
    return bons, len(pontos) - len(bons)

def _bucket_cor(v, faixas):
    """faixas: [(limite, 'RRGGBB')] em ordem crescente de qualidade."""
    for lim, cor in faixas:
        if v is None: return "808080"
        if v < lim: return cor
    return faixas[-1][1]

# ──────────────────────────────────────────────────────────────
# KML DO SURVEY PARA O GOOGLE EARTH
#
# Existe porque o PNG depende de uma imagem de fundo que a rede da mina
# não deixa baixar — sem ela o mapa sai com eixos lat/lon e ninguém
# reconhece onde é. O Google Earth já traz o satélite, e ainda deixa
# ligar/desligar camada, clicar no ponto e ler a medição.
#
# Fonte de dados: o survey PERSISTIDO, o mesmo que alimenta o PPT. Duas
# fontes divergiriam e o relatório contradiria o mapa.
# ──────────────────────────────────────────────────────────────

# Faixas de cor por grandeza: (limite_superior, RRGGBB). O corte do meio
# é sempre o requisito Modular, para a leitura ser imediata.
FAIXAS_KML = {
    # RSSI segue a convenção dos survey comerciais (Ekahau, NetSpot):
    # escala útil de -90 a -45 dBm, gradiente vermelho→verde, e os cortes
    # de qualidade em -50 / -60 / -67 / -70 / -80 dBm. O -75 do requisito
    # Modular cai entre -70 e -80, então fica como faixa própria: assim
    # aprovado e reprovado não dividem a mesma cor.
    "sinal": [(-85, "8B1A1A"), (-80, "C0392B"), (-75, "E74C3C"),
              (-70, "E67E22"), (-67, "F1C40F"), (-60, "9ACD32"),
              (-50, "27AE60"), (999, "1E8449")],
    # Cobertura usa as MESMAS faixas do RSSI: e a mesma grandeza, lida de
    # outra fonte. Faixas proprias fariam duas reguas para o mesmo dBm.
    "sinal_cob": [(-85, "8B1A1A"), (-80, "C0392B"), (-75, "E74C3C"),
                  (-70, "E67E22"), (-67, "F1C40F"), (-60, "9ACD32"),
                  (-50, "27AE60"), (999, "1E8449")],
    "snr":   [(10, "8B1A1A"), (15, "C0392B"), (20, "E74C3C"),
              (25, "E67E22"), (30, "F1C40F"), (40, "9ACD32"),
              (999, "27AE60")],
    "rtt":   [(20, "27AE60"), (50, "9ACD32"), (100, "F1C40F"),
              (200, "E67E22"), (400, "E74C3C"), (99999, "C0392B")],
    "perda": [(0.5, "27AE60"), (1, "9ACD32"), (2, "F1C40F"),
              (5, "E67E22"), (10, "E74C3C"), (101, "C0392B")],
    "interf": [(5, "27AE60"), (10, "9ACD32"), (20, "F1C40F"),
               (35, "E67E22"), (50, "E74C3C"), (101, "C0392B")],
    # Piso de ruído: quanto mais negativo, mais limpo. O corte de -85 dBm
    # é o mesmo de ESCALAS, e sem esta entrada o slide de Noise Floor
    # ficava sem KMZ para o print.
    "ruido": [(-95, "27AE60"), (-90, "9ACD32"), (-85, "F1C40F"),
              (-80, "E67E22"), (-75, "E74C3C"), (999, "C0392B")],
}

_ICONES_KML = {
    "ERB":   ("http://maps.google.com/mapfiles/kml/shapes/triangle.png", "F1C40F"),
    "ERM":   ("http://maps.google.com/mapfiles/kml/shapes/triangle.png", "4FA3F7"),
    "Móvel": ("http://maps.google.com/mapfiles/kml/shapes/donut.png",    "2ECC71"),
}


def _balao_amostra(a):
    """Conteúdo do balão do ponto: só o que foi medido.

    Campo sem medição fica FORA da tabela em vez de aparecer como 0 —
    zero seria lido como 'mediu e deu zero'.
    """
    linhas = []
    campos = (("sinal", "RSSI", "dBm", 1), ("snr", "SNR", "dB", 1),
              ("ruido", "Ruído", "dBm", 1), ("rtt", "Latência", "ms", 1),
              ("perda", "Perda", "%", 2), ("vazao", "Vazão", "Mbps", 2),
              ("taxa", "Taxa do enlace", "Mbps", 0),
              ("custo", "Custo do enlace", "", 0),
              ("peers", "Enlaces ativos", "", 0),
              ("vel", "Velocidade", "m/s", 1),
              ("sats", "Satélites", "", 0), ("hdop", "HDOP", "", 1))
    for c, rot, un, casas in campos:
        v = a.get(c)
        if v is None: continue
        try: txt = f"{float(v):.{casas}f}".rstrip("0").rstrip(".") if casas else f"{v}"
        except (TypeError, ValueError): txt = str(v)
        fora = ""
        if c in REQUISITOS:
            op, lim, _, _ = REQUISITOS[c]
            try:
                ruim = (float(v) <= lim) if op == ">" else (float(v) >= lim)
                fora = " <b style='color:#C0392B'>(fora)</b>" if ruim else ""
            except (TypeError, ValueError): pass
        linhas.append(f"<tr><td>{rot}</td><td><b>{_esc(txt)} {un}</b>{fora}</td></tr>")
    if a.get("servidor"):
        linhas.append(f"<tr><td>Servido por</td>"
                      f"<td><b>{_esc(a['servidor'])}</b></td></tr>")
    if a.get("banda"):
        linhas.append(f"<tr><td>Banda</td><td>{_esc(a['banda'])}</td></tr>")
    if a.get("fonte") == "cache":
        # Ponto defasado precisa vir identificado no balão também.
        linhas.append("<tr><td>Origem</td><td><i>cache do exporter "
                      "(rádio indisponível no instante)</i></td></tr>")
    quando = ""
    if a.get("ts"):
        quando = f"<p><small>{datetime.fromtimestamp(a['ts']):%d/%m/%Y %H:%M:%S}</small></p>"
    return (f"<![CDATA[<h3>{_esc(a.get('radio',''))}</h3>{quando}"
            f"<table border='0' cellpadding='3'>{''.join(linhas)}</table>]]>")


# Passos do gradiente. 40 dá transição suave sem estourar o número de
# estilos no KML — cada cor distinta vira um <Style>.
PASSOS_COR = 40


def cor_continua(valor, esc, passos=PASSOS_COR):
    """Cor da AMOSTRA na escala, num gradiente contínuo.

    As oito faixas fixas serviam para classificar (dentro/fora do
    requisito), mas como cor de traçado davam degrau: duas amostras de
    -74,9 e -75,1 dBm saíam vermelho e laranja, e a rota virava confete.
    O gradiente dá a cor exata de cada medição e ainda fica legível.

    Devolve 'RRGGBB', ou None quando não houve medição.
    """
    if valor is None or not esc:
        return None
    from matplotlib.colors import to_hex
    lo, hi = float(esc["lo"]), float(esc["hi"])
    x = (float(valor) - lo) / (hi - lo) if hi != lo else 0.5
    x = max(0.0, min(1.0, x))
    # Quantiza para limitar a quantidade de estilos.
    x = round(x * (passos - 1)) / (passos - 1)
    cm = _cmap_rf(invertido=(esc.get("melhor") == "baixo"))
    return to_hex(cm(x))[1:].upper()


def _placemark(nome, descricao, estilo, ponto=None, linha=None, anel=None,
               quando=None):
    """Monta um Placemark na ORDEM que o esquema KML 2.2 exige.

    O esquema define a sequência de AbstractFeatureType como
    `name → description → TimeStamp → styleUrl` e só depois a geometria.
    Emitir `styleUrl` antes de `description` faz o Google Earth descartar
    o estilo e cair no padrão — que desenha a linha BRANCA/preta. Era por
    isso que as rotas saíam sem cor: as cores estavam certas, a ordem dos
    elementos é que não estava.
    """
    p = ["<Placemark>"]
    if nome:      p.append(f"<name>{_esc(nome)}</name>")
    if descricao: p.append(f"<description>{descricao}</description>")
    if quando:    p.append(f"<TimeStamp><when>{quando}</when></TimeStamp>")
    if estilo:    p.append(f"<styleUrl>#{_esc(estilo)}</styleUrl>")
    if ponto is not None:
        p.append(f"<Point><altitudeMode>clampToGround</altitudeMode>"
                 f"<coordinates>{ponto[1]:.7f},{ponto[0]:.7f},0</coordinates>"
                 f"</Point>")
    elif linha is not None:
        c = " ".join(f"{lo:.7f},{la:.7f},0" for la, lo in linha)
        p.append(f"<LineString><tessellate>1</tessellate>"
                 f"<altitudeMode>clampToGround</altitudeMode>"
                 f"<coordinates>{c}</coordinates></LineString>")
    elif anel is not None:
        c = " ".join(f"{lo:.7f},{la:.7f},0" for la, lo in anel)
        p.append(f"<Polygon><altitudeMode>clampToGround</altitudeMode>"
                 f"<outerBoundaryIs><LinearRing><coordinates>{c}"
                 f"</coordinates></LinearRing></outerBoundaryIs></Polygon>")
    p.append("</Placemark>")
    return "".join(p)


def _decimar(amostras, teto):
    """Reduz uniformemente preservando a ordem temporal.

    Um survey de 8 h com 150 rádios a 10 s dá ~430 mil pontos: o Google
    Earth engasga e o arquivo fica inutilizável. Decimar mantém a forma
    do trajeto; o passo real vai escrito na descrição do KML.
    """
    if teto <= 0 or len(amostras) <= teto:
        return amostras, 1
    passo = -(-len(amostras) // teto)      # teto da divisão, sem importar math
    return amostras[::passo], passo


def _calor_da_rota(am, campo, bb, raio_m, n=760, esc=None):
    """Mapa de calor SÓ por onde o rádio passou.

    Não é interpolação de cobertura: cada amostra pinta um núcleo de raio
    `raio_m` à sua volta e nada além disso. Onde ninguém passou fica
    transparente — o mapa não afirma sinal em lugar que não foi medido.
    É a diferença entre "medi aqui e deu isto" e "eu acho que lá deve dar
    aquilo", e só a primeira cabe num laudo.

    CADA PIXEL MOSTRA UMA LEITURA REAL, a da amostra mais próxima — não
    uma média. Medido contra o arquivo do cliente: no mesmo trajeto, as
    amostras cruas davam 81% fora do requisito, o vizinho mais próximo
    dava 88% e a média ponderada dizia 100%. A média não escondia
    problema: ela APAGAVA o que era bom, porque as poucas leituras de
    -45 dBm sumiam ao serem promediadas com as vizinhas ruins.

    Quando duas amostras estão praticamente à mesma distância do pixel —
    o caso de passar duas vezes no mesmo lugar —, vale a PIOR. Empate
    entre duas leituras reais se resolve para o lado conservador: a
    operação enfrenta as duas, e é a ruim que para o caminhão.

    Devolve (valor, alfa), ambos n×n, com NaN e 0 fora do rastro.
    """
    import numpy as np, math
    pts = [(a["lat"], a["lon"], float(a[campo])) for a in am
           if a.get("lat") is not None and a.get("lon") is not None
           and a.get(campo) is not None]
    if len(pts) < 3:
        return None, None

    lat_med = (bb["norte"] + bb["sul"]) / 2.0
    mlat = 111320.0
    mlon = 111320.0 * math.cos(math.radians(lat_med))
    gx = np.linspace(bb["oeste"], bb["leste"], n)
    gy = np.linspace(bb["sul"],   bb["norte"], n)
    if gx[-1] <= gx[0] or gy[-1] <= gy[0]:
        return None, None
    px = (gx[1] - gx[0]) * mlon          # metros por pixel em x
    py = (gy[1] - gy[0]) * mlat
    rx = max(1, int(raio_m / max(px, 1e-6)))
    ry = max(1, int(raio_m / max(py, 1e-6)))
    # Sigma mais largo que raio/2: com raio/2 o peso no meio do vão entre
    # duas amostras cai a ~0,25 e o rastro se parte visualmente mesmo com
    # os círculos se tocando.
    sigma = max(raio_m / 1.5, 1.0)

    # "Pior" depende da grandeza: em RSSI e SNR o pior é o menor; em
    # ruído, latência, perda e interferência é o maior.
    maior_melhor = (esc or {}).get("melhor", "alto") == "alto"
    # Tolerância de empate: UM PIXEL, não uma fração do raio.
    #
    # Com 25% do raio, o empate disparava entre amostras CONSECUTIVAS: um
    # pixel no meio do caminho entre duas leituras fica à mesma distância
    # das duas, e o mapa inteiro pendia para o lado ruim (95,8% da área
    # fora do requisito contra 91,9% pelo vizinho puro, no arquivo do
    # cliente). Isso não é ser conservador, é distorcer.
    #
    # Na resolução da grade, só empata o que está de fato no mesmo lugar:
    # veículo parado, ou segunda passagem pelo mesmo ponto. Aí sim vale a
    # pior — a operação enfrenta as duas leituras.
    tol = max(px, py)

    dmin  = np.full((n, n), np.inf)
    valor = np.full((n, n), np.nan)
    # Alfa sai do núcleo MAIS FORTE que cobre o pixel, não da soma deles.
    # Com a soma, um equipamento parado — 40 amostras no mesmo ponto —
    # vira uma bola sólida e ainda puxa a referência de opacidade para
    # cima, apagando o rastro do veículo que andou. Pelo máximo, a faixa
    # tem largura uniforme independentemente de quantas amostras caíram
    # ali, que é o que se espera de um rastro.
    wmax = np.zeros((n, n))
    # Só a janela de cada amostra é tocada: varrer a grade inteira por
    # ponto seria O(pontos x n²) e um survey de horas não terminaria.
    for la, lo, v in pts:
        j = int(round((lo - gx[0]) / (gx[-1] - gx[0]) * (n - 1)))
        i = int(round((la - gy[0]) / (gy[-1] - gy[0]) * (n - 1)))
        i0, i1 = max(0, i - ry), min(n, i + ry + 1)
        j0, j1 = max(0, j - rx), min(n, j + rx + 1)
        if i0 >= i1 or j0 >= j1: continue
        dy = (np.arange(i0, i1) - i)[:, None] * py
        dx = (np.arange(j0, j1) - j)[None, :] * px
        d2 = dx * dx + dy * dy
        w = np.exp(-d2 / (2.0 * sigma * sigma))
        dentro = d2 <= raio_m * raio_m
        w = np.where(dentro, w, 0.0)      # corte duro: fora do raio, nada
        np.maximum(wmax[i0:i1, j0:j1], w, out=wmax[i0:i1, j0:j1])

        d  = np.sqrt(d2)
        jd = dmin[i0:i1, j0:j1]
        jv = valor[i0:i1, j0:j1]
        manda  = dentro & (d < jd - tol)                 # mais perto: manda
        empata = dentro & (np.abs(d - jd) <= tol)        # mesmo lugar
        with np.errstate(invalid="ignore"):
            pior = (v < jv) if maior_melhor else (v > jv)
        jv[manda | (empata & (np.isnan(jv) | pior))] = v
        np.minimum(jd, np.where(dentro, d, np.inf), out=jd)

    vivo = wmax > 1e-6
    if not vivo.any():
        return None, None
    valor[~vivo] = np.nan
    # O miolo do rastro fica sólido e a borda esvanece — aspecto de calor
    # em vez de fita. A raiz alarga a parte opaca: com o gaussiano cru a
    # faixa só ficava cheia bem no centro e o rastro parecia um colar de
    # contas.
    alfa = np.sqrt(np.clip(wmax, 0.0, 1.0)) * 0.9
    alfa[~vivo] = 0.0
    return valor, alfa


def _png_calor(valor, alfa, cmap, vmin, vmax, caminho):
    """Raster RGBA com transparência POR PIXEL.

    O `_png_overlay` usa alfa constante, que serve a heatmap de área
    inteira. Aqui a borda precisa esvanecer, senão o rastro vira uma
    salsicha de contorno duro.
    """
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    norm = np.clip((valor - vmin) / max(vmax - vmin, 1e-9), 0, 1)
    rgba = cmap(np.nan_to_num(norm, nan=0.0))
    rgba[..., 3] = np.where(np.isfinite(valor), alfa, 0.0)
    plt.imsave(caminho, np.flipud(rgba))     # KML espera norte no topo
    return caminho


def _cfg_bool(cfg, secao, chave, padrao=False):
    """Lê um booleano do config tolerando cfg=None e seção ausente.

    O KML é gerado tanto pelo serviço (com config) quanto por teste e por
    linha de comando (sem), e quebrar por falta de seção seria trocar um
    detalhe de aparência por um survey perdido.
    """
    try:
        return cfg.getboolean(secao, chave, fallback=padrao)
    except Exception:
        return padrao


def _trechos_continuos(pts, fator=3.0, piso_s=30.0, salto_m=250.0):
    """Parte o trajeto onde houve BURACO na medição.

    Ligar dois pontos consecutivos é afirmar que o veículo passou pela
    reta entre eles. Com a amostragem espaçada isso vira aresta reta
    cortando a cava: o traçado mente sobre por onde se andou e sobre onde
    o sinal foi medido. Melhor um trecho interrompido — que mostra que ali
    não se mediu — do que uma reta inventada.

    O limiar sai do PRÓPRIO survey: a mediana do intervalo entre amostras
    vezes `fator`. Assim uma captura de 5 s tolera vãos de ~15 s e uma de
    60 s tolera ~180 s, sem número mágico que só serve para um caso. O
    `piso_s` evita que uma captura muito rápida quebre o trajeto ao menor
    engasgo da malha, e `salto_m` pega o caso em que o tempo está normal
    mas a posição pulou (perda de fix, GPS voltando).
    """
    if len(pts) < 2:
        return [pts] if pts else []
    dts = []
    for a, b in zip(pts, pts[1:]):
        ta, tb = a.get("ts"), b.get("ts")
        if ta is not None and tb is not None and tb > ta:
            dts.append(tb - ta)
    if dts:
        ordenados = sorted(dts)
        mediana = ordenados[len(ordenados) // 2]
        limite_t = max(piso_s, mediana * fator)
    else:
        limite_t = piso_s

    trechos, atual = [], [pts[0]]
    for a, b in zip(pts, pts[1:]):
        ta, tb = a.get("ts"), b.get("ts")
        quebra = ta is not None and tb is not None and (tb - ta) > limite_t
        if not quebra:
            quebra = _dist_m(a["lat"], a["lon"], b["lat"], b["lon"]) > salto_m
        if quebra:
            trechos.append(atual); atual = [b]
        else:
            atual.append(b)
    trechos.append(atual)
    return [t for t in trechos if len(t) >= 2]


def gerar_kml_survey(sv, amostras, fixos=None, manuais=None, cfg=None,
                     campo="sinal", max_pontos=8000, comprimir=True,
                     grade_zonas=50.0, banda=None, campos=None,
                     peers=None, max_repetidoras=12):
    """KML/KMZ do survey para abrir no Google Earth.

    `peers` (a lista de vizinhos da captura) acrescenta uma pasta por
    ERB/ERM, com a pegada MEDIDA de cada uma. Sem ele o arquivo sai como
    antes — a sondagem por API não devolve a vizinhança inteira, só o
    enlace que atendeu, e aí não há o que desenhar.

    `campos` (lista) põe UMA ABA por grandeza no mesmo arquivo, que é o
    jeito de comparar RSSI e interferência sem abrir seis janelas. Só a
    primeira nasce visível: ligadas juntas, os pontos se empilham no mesmo
    lugar e o mapa não diz nada. `campo` (singular) segue valendo para um
    arquivo de uma grandeza só.

    `banda` recorta as amostras de uma malha só — misturar 2.4 e 5.8 GHz
    no mesmo mapa esconde justamente a banda que está ruim.

    Devolve (bytes, nome_arquivo).
    """
    import zipfile, io as _io, tempfile

    campos = list(campos) if campos else [campo]
    ruins_ = [c for c in campos if c not in FAIXAS_KML]
    if ruins_:
        raise ValueError(f"campo(s) {ruins_} sem escala de cor; "
                         f"use um de {sorted(FAIXAS_KML)}")
    campo = campos[0]
    am = [a for a in (amostras or [])
          if a.get("lat") is not None and a.get("lon") is not None]
    if banda:
        am = [a for a in am if _norm_banda(a.get("banda")) == banda]
    if not am and not fixos:
        raise RuntimeError(
            f"survey sem ponto georreferenciado"
            + (f" na banda {banda}" if banda else ""))

    cfgr = cfg_relatorio(cfg or configparser.ConfigParser())
    classificar = classificador_categorias(cfgr)
    faixas = FAIXAS_KML[campo]
    op, limite, unid, rot_req = (limite_de(campo)
                                 or (">", None, "", campo.upper()))

    nome_sv = sv.get("nome") or "Survey"
    ini = sv.get("inicio"); fim = sv.get("fim") or ini
    quando = datetime.fromtimestamp(ini) if ini else datetime.now()

    # ── estilos ──
    # Um estilo por cor de CADA grandeza pedida: com várias abas no mesmo
    # documento, gerar só as cores da primeira deixaria as outras sem
    # estilo — e styleUrl apontando para estilo inexistente cai no padrão
    # do Google Earth, que desenha linha branca.
    # Cores do gradiente efetivamente usadas: cada uma vira um <Style>.
    # Coletadas ao montar as abas e emitidas antes do documento.
    cores_linha, cores_ponto = set(), set()
    cores_todas = {c for cp in campos for _, c in FAIXAS_KML[cp]}
    cores_todas.add("808080")          # trecho sem medição
    estilos = []
    for cor in sorted(cores_todas):
        estilos.append(
            f'<Style id="p{cor}"><IconStyle><color>{_kml_cor(cor)}</color>'
            f'<scale>0.4</scale><Icon><href>http://maps.google.com/mapfiles/'
            f'kml/shapes/placemark_circle.png</href></Icon></IconStyle>'
            f'<LabelStyle><scale>0</scale></LabelStyle></Style>')
        # Rota e CONTEXTO, nao a medida. Linha grossa e opaca virava
        # rastro de GPS cobrindo o terreno e competindo com o heatmap,
        # que e onde a informacao esta.
        # Largura e opacidade de survey de verdade: a fita colorida E o
        # dado. Com 2,6 px e 170 de alfa a rota sumia sobre o satelite da
        # cava, que ja e claro e cheio de textura — parecia um risco de
        # GPS, nao uma medicao.
        estilos.append(
            f'<Style id="l{cor}"><LineStyle><color>{_kml_cor(cor, 235)}</color>'
            f'<width>7</width></LineStyle></Style>')
    # Contorno da rota: mais grosso, escuro e por baixo.
    # Contorno discreto: com uma dezena de equipamentos passando pela
    # mesma pista, contorno grosso e opaco de um veiculo cobre a COR do
    # outro no cruzamento — vira uma malha escura por cima da medicao.
    # O contorno acompanha a fita: mais largo que ela, para virar borda, e
    # discreto no alfa para nao empastar cruzamento de dois veiculos.
    estilos.append(
        '<Style id="lcontorno"><LineStyle><color>60201510</color>'
        '<width>10</width></LineStyle></Style>')
    estilos.append(
        '<Style id="pFora"><IconStyle><color>ff0000ff</color><scale>0.55</scale>'
        '<Icon><href>http://maps.google.com/mapfiles/kml/shapes/caution.png'
        '</href></Icon></IconStyle><LabelStyle><scale>0</scale></LabelStyle></Style>')
    for tipo, (href, cor) in _ICONES_KML.items():
        estilos.append(
            f'<Style id="bc{_esc(tipo)}"><IconStyle><color>{_kml_cor(cor)}</color>'
            f'<scale>1.15</scale><Icon><href>{href}</href></Icon></IconStyle>'
            f'<LabelStyle><scale>0.85</scale></LabelStyle></Style>')
    estilos.append(
        '<Style id="manual"><IconStyle><color>ffff00ff</color><scale>1.0</scale>'
        '<Icon><href>http://maps.google.com/mapfiles/kml/shapes/ruler.png'
        '</href></Icon></IconStyle></Style>')
    # Zona: preenchimento vermelho translúcido para não esconder o terreno.
    estilos.append(
        '<Style id="zona"><LineStyle><color>ff2b39c0</color><width>3</width>'
        '</LineStyle><PolyStyle><color>552b39c0</color></PolyStyle></Style>')
    estilos.append(
        '<Style id="sugestao"><IconStyle><color>ff00ffff</color>'
        '<scale>1.3</scale><Icon><href>http://maps.google.com/mapfiles/kml/'
        'shapes/target.png</href></Icon></IconStyle></Style>')

    pastas = []

    # ── 1. BreadCrumbs, separados por tipo ──
    posicoes = {}
    for nome, c in (fixos or {}).items():
        if c: posicoes[nome] = (c[0], c[1])
    por_radio_geral = {}
    for a in am:
        por_radio_geral.setdefault(a["radio"], []).append(a)
    for radio, pts in por_radio_geral.items():
        if radio not in posicoes:
            u = max(pts, key=lambda x: x.get("ts") or 0)
            posicoes[radio] = (u["lat"], u["lon"])

    # As posições continuam sendo calculadas — o traçado depende delas —,
    # mas por padrão NÃO viram marcador. Com uma dezena de equipamentos a
    # camada de alfinetes cobre justamente a medição, que é o assunto do
    # arquivo. Quem quiser o inventário liga kmz_com_equipamentos.
    por_tipo = {}
    if _cfg_bool(cfg, "relatorio", "kmz_com_equipamentos", False):
        for nome, (la, lo) in sorted(posicoes.items()):
            cat = classificar(nome)
            tipo = cat if cat in _ICONES_KML else "Móvel"
            por_tipo.setdefault(tipo, []).append(
                _placemark(nome, f"<![CDATA[{len(por_radio_geral.get(nome, []))} "
                                 f"amostras]]>", f"bc{tipo}",
                           ponto=(la, lo)))
    if por_tipo:
        sub = "".join(
            f"<Folder><name>{_esc(t)} ({len(v)})</name><open>0</open>"
            f"{''.join(v)}</Folder>"
            for t, v in sorted(por_tipo.items()))
        pastas.append(f"<Folder><name>BreadCrumbs ({len(posicoes)})</name>"
                      f"<open>1</open>{sub}</Folder>")

    # ── 2. Uma ABA por grandeza ──
    # Todas no mesmo arquivo, só a primeira visível: ligadas juntas, os
    # pontos de seis grandezas se empilham no mesmo lugar e o mapa não diz
    # nada. O operador liga a que quer no painel de camadas.
    # A rota agora e CALOR. A linha continua disponivel para quem quiser
    # o traco cru, mas desligada: era ela que produzia as arestas retas
    # ligando pontos por onde ninguem passou.
    com_rotas = _cfg_bool(cfg, "relatorio", "kmz_com_rotas", False)

    # PNGs do calor, embutidos no KMZ. Em KML solto nao ha onde guardar a
    # imagem, e overlay apontando para arquivo ausente nao desenha nada —
    # entao o calor so sai no KMZ.
    extras = []

    def _calor_no_kmz(amostras_aba, campo, visivel, sufixo="", rotulo=None):
        if not comprimir:
            return ""
        # SÓ quem andou. O rádio parado dá dezenas de amostras no mesmo
        # ponto: vira uma bola isolada no mapa, e como BC fixo enxerga o
        # vizinho de perto, ela sai verde. Eram essas as "bolas espalhadas
        # e desconectadas" — e boa parte do verde que não batia com a mina.
        # O que se quer é a rota, então o calor usa quem se deslocou.
        parados = set(fixos or {})
        moveis = [a for a in amostras_aba if a.get("radio") not in parados]
        if not parados:
            # Sem a lista de fixos (chamada solta, teste), separa pelo
            # próprio dado: quem não mudou de lugar não é rota.
            por_r = {}
            for a in amostras_aba:
                if a.get("lat") is None: continue
                por_r.setdefault(a["radio"], []).append(a)
            andou = set()
            for r, ps in por_r.items():
                if len(ps) < 2: continue
                d = max(_dist_m(ps[0]["lat"], ps[0]["lon"], q["lat"], q["lon"])
                        for q in ps)
                if d > 30.0: andou.add(r)
            if not andou:
                # NINGUEM andou — captura feita parada, tipicamente do
                # MeshMapper ligado numa repetidora. Antes caia no `or`
                # abaixo e pintava tudo: 115 leituras empilhadas em 0,5 m
                # viravam uma mancha de um pixel com a escala de AREA. E
                # a leitura errada mais cara que existe, porque parece um
                # mapa. Sem rastro, o laudo da captura parada e o censo de
                # vizinhos (ver `censo_vizinhos`) e o KMZ de pontos fixos.
                log.info("[kml] nenhuma amostra em deslocamento: "
                         "sem rastro de calor nesta aba")
                return ""
            moveis = [a for a in amostras_aba if a.get("radio") in andou]
        amostras_aba = moveis or amostras_aba
        pts = [(a["lat"], a["lon"]) for a in amostras_aba
               if a.get("lat") is not None and a.get("lon") is not None
               and a.get(campo) is not None]
        if len(pts) < 3:
            return ""
        esc = ESCALAS.get(campo)
        if not esc:
            return ""
        # Raio a partir do espacamento REAL das amostras: com captura
        # rapida o rastro fica fino e fiel; com captura espacada ele
        # engrossa o suficiente para nao virar bolinha solta. O teto
        # impede que um survey ralo pinte meia cava.
        # O raio TEM de passar do espaçamento, senão os núcleos não se
        # encontram e o rastro vira colar de contas — foi o que apareceu
        # numa captura de 20 s, com ~200 m entre amostras contra um raio
        # limitado a 120 m. O teto sobe junto, mas segue existindo: sem
        # ele, uma captura muito rala pintaria meia cava a partir de
        # meia dúzia de leituras.
        # Aqui há um limite físico, não de desenho: com amostras a 200 m
        # não existe faixa estreita E contínua. Ou saem contas separadas,
        # ou sai um borrão largo que afirma medição a centenas de metros
        # da estrada. O raio acompanha o espaçamento para ligar os
        # núcleos, e o teto impede o borrão de virar "meia cava medida".
        # Quem quiser rastro fino e contínuo baixa o intervalo da captura
        # — a 1 s são ~11 m entre amostras e o raio cai para o piso.
        esp = espacamento_tipico(amostras_aba) or 40.0
        raio = float(max(25.0, min(esp * 0.9, 150.0)))
        las = [p[0] for p in pts]; los = [p[1] for p in pts]
        # Margem = raio, em graus: e exatamente o quanto o nucleo pode
        # transbordar da nuvem de pontos. Menos que isso corta o rastro na
        # borda da imagem.
        import math as _m
        dlat = raio / 111320.0
        dlon = raio / (111320.0 * max(0.2, _m.cos(_m.radians(sum(las)/len(las)))))
        bb = {"sul": min(las) - dlat, "norte": max(las) + dlat,
              "oeste": min(los) - dlon, "leste": max(los) + dlon}
        try:
            valor, alfa = _calor_da_rota(amostras_aba, campo, bb, raio,
                                         esc=esc)
            if valor is None:
                return ""
            # O sufixo não é enfeite: com uma pasta por repetidora, todos
            # os rasters se chamariam calor_sinal.png e um sobrescreveria
            # o outro dentro do zip — sobraria um mapa só, repetido em
            # todas as pastas.
            nome_png = f"calor_{campo}{sufixo}.png"
            import tempfile as _tf, os as _os
            cam = _os.path.join(_tf.mkdtemp(), nome_png)
            _png_calor(valor, alfa, _cmap_rf(invertido=(esc.get("melhor") == "baixo")),
                       float(esc["lo"]), float(esc["hi"]), cam)
            with open(cam, "rb") as fh:
                extras.append((f"files/{nome_png}", fh.read()))
        except Exception as e:
            log.warning(f"[kml] calor de {campo} falhou: {e}")
            return ""
        rot = rotulo or esc.get("rot", campo)
        return (f"<GroundOverlay><name>Calor — {_esc(rot)}</name>"
                f"<visibility>{1 if visivel else 0}</visibility>"
                f"<description>{_esc(f'Medido pelo radio, raio de {raio:.0f} m em volta de cada amostra. Transparente onde nao se passou.')}</description>"
                f"<Icon><href>files/{nome_png}</href></Icon>"
                f"<LatLonBox><north>{bb['norte']:.7f}</north>"
                f"<south>{bb['sul']:.7f}</south>"
                f"<east>{bb['leste']:.7f}</east>"
                f"<west>{bb['oeste']:.7f}</west></LatLonBox></GroundOverlay>")

    def _aba(campo_a, visivel):
        faixas_a = FAIXAS_KML[campo_a]
        op_a, lim_a, un_a, rot_a = (limite_de(campo_a)
                                    or (">", None, "", campo_a.upper()))
        dentro = []

        # ── mapa de calor do rastro ──
        # A rota vira CALOR: um núcleo por medição, só onde o rádio
        # passou. Substitui a fita de segmentos porque a linha, além de
        # fina sobre o satélite da cava, ligava pontos distantes por retas
        # que ninguém percorreu. O calor não tem aresta para inventar.
        png = _calor_no_kmz(am, campo_a, visivel)
        if png:
            dentro.append(png)

        # ── rotas (opcional) ──
        # UM segmento por medição, com a cor exata daquela amostra na
        # escala contínua. Antes eram oito faixas fixas: duas leituras de
        # -74,9 e -75,1 dBm caíam em cores diferentes e o traçado virava
        # confete. Com o gradiente, a rota vira uma fita que muda de tom
        # junto com o sinal.
        esc_a = ESCALAS.get(campo_a)
        blocos = []
        for radio, pts in (sorted(por_radio_geral.items()) if com_rotas else []):
            pts = sorted(pts, key=lambda x: x.get("ts") or 0)
            if len(pts) < 2: continue

            # Trajeto partido nos buracos de medição: o que não foi medido
            # não vira linha. Sem isto, um vão de vários minutos aparecia
            # como uma reta atravessando a cava, com cor de uma leitura que
            # não vale para nada naquele caminho.
            continuos = _trechos_continuos(pts)
            if not continuos: continue
            trechos, n_pts = [], 0
            for corrida in continuos:
                n_pts += len(corrida)
                # O contorno escuro é moldura: uma linha por TRECHO — não
                # por trajeto —, senão ele mesmo redesenha a reta que a
                # quebra acabou de tirar. Vai antes, para ficar por baixo.
                trechos.append(_placemark(
                    None, None, "lcontorno",
                    linha=[(q["lat"], q["lon"]) for q in corrida]))
                for a_, b_ in zip(corrida, corrida[1:]):
                    v = a_.get(campo_a)
                    if v is None: v = b_.get(campo_a)
                    cor = cor_continua(v, esc_a) or "9E9E9E"
                    cores_linha.add(cor)
                    rot = (f"{v:.1f} {un_a}" if isinstance(v, (int, float))
                           else "sem medição")
                    trechos.append(_placemark(
                        rot, None, f"r{cor}",
                        linha=((a_["lat"], a_["lon"]), (b_["lat"], b_["lon"]))))

            corte = (f" · {len(continuos)} trechos" if len(continuos) > 1
                     else "")
            blocos.append(
                f"<Folder><name>{_esc(radio)} ({n_pts} pontos{corte})</name>"
                f"<open>0</open>{''.join(trechos)}</Folder>")
        if blocos and com_rotas:
            dentro.append(
                f"<Folder><name>Rotas ({len(blocos)})</name>"
                f"<open>0</open>{''.join(blocos)}</Folder>")

        # pontos de medição, com balão e linha do tempo
        usados, passo = _decimar(sorted(am, key=lambda x: x.get("ts") or 0),
                                 max_pontos)
        marcas = []
        for a in usados:
            v = a.get(campo_a)
            # Mesmo gradiente da rota: ponto e linha discordarem de cor no
            # mesmo lugar seria confuso.
            cor = cor_continua(v, esc_a) or "9E9E9E"
            cores_ponto.add(cor)
            quando_a = (datetime.fromtimestamp(a["ts"]).strftime(
                "%Y-%m-%dT%H:%M:%S") if a.get("ts") else None)
            rotulo = (f"{v:.0f} {un_a}" if isinstance(v, (int, float))
                      else a.get("radio", ""))
            marcas.append(_placemark(rotulo, _balao_amostra(a), f"q{cor}",
                                     ponto=(a["lat"], a["lon"]),
                                     quando=quando_a))
        nota = (f" — 1 a cada {passo} amostras" if passo > 1 else "")
        dentro.append(
            f"<Folder><name>Medições ({len(marcas)})</name><open>0</open>"
            f"<description><![CDATA[Clique num ponto para ver todas as "
            f"grandezas medidas ali.{nota}]]></description>"
            f"{''.join(marcas)}</Folder>")

        # fora do requisito — desligada: ligada, cobre os pontos bons
        if lim_a is not None:
            ruins = [a for a in am if a.get(campo_a) is not None
                     and ((a[campo_a] <= lim_a) if op_a == ">"
                          else (a[campo_a] >= lim_a))]
            ruins, _ = _decimar(ruins, max_pontos // 2)
            if ruins:
                itens = [_placemark(f"{a[campo_a]:.0f} {un_a}",
                                    _balao_amostra(a), "pFora",
                                    ponto=(a["lat"], a["lon"]))
                         for a in ruins]
                dentro.append(
                    f"<Folder><name>Fora do requisito — {_esc(op_a)} {lim_a:g} "
                    f"{un_a} ({len(ruins)})</name><open>0</open>"
                    f"<visibility>0</visibility>{''.join(itens)}</Folder>")

        # zonas-problema: a camada que responde "o que fazer"
        if lim_a is not None:
            try:
                zonas = zonas_problema(am, campo_a, grade_zonas)
            except Exception as e:
                log.warning(f"[kml] zonas-problema ({campo_a}): {e}"); zonas = []
            if zonas:
                itens_z = []
                for i, z in enumerate(zonas, start=1):
                    # Círculo aproximado, para a zona ter área no mapa em
                    # vez de virar um alfinete solto.
                    raio = max(40.0, z["extensao_m"] / 2.0)
                    g_lat = raio / 111_320.0
                    g_lon = g_lat / max(0.1, math.cos(math.radians(z["lat"])))
                    anel = [(z["lat"] + g_lat*math.sin(k*math.pi/18),
                             z["lon"] + g_lon*math.cos(k*math.pi/18))
                            for k in range(37)]
                    texto = _esc(texto_zona(z, campo_a))
                    itens_z.append(_placemark(
                        f"Zona {i} — {z['valor_mediano']:g} {un_a}",
                        f"<![CDATA[<h3>Zona {i}</h3><p>{texto}</p>"
                        f"<p><small>Equipamentos: "
                        f"{_esc(', '.join(z['radios'][:12]))}</small></p>]]>",
                        "zona", anel=anel))
                    itens_z.append(_placemark(
                        f"Zona {i} — avaliar rádio aqui",
                        f"<![CDATA[{texto}]]>", "sugestao",
                        ponto=(z["sugestao_lat"], z["sugestao_lon"])))
                dentro.append(
                    f"<Folder><name>ZONAS-PROBLEMA ({len(zonas)})</name>"
                    f"<open>1</open><description><![CDATA[Regiões contíguas "
                    f"fora do requisito. O alfinete marca o pior ponto — "
                    f"sugestão de local para avaliar rádio, não veredito."
                    f"]]></description>{''.join(itens_z)}</Folder>")

        req = (f" · requisito {op_a} {lim_a:g} {un_a}" if lim_a is not None
               else "")
        return (f"<Folder><name>{_esc(rot_a)}</name><open>0</open>"
                f"<visibility>{1 if visivel else 0}</visibility>"
                f"<description><![CDATA[Cor por {_esc(rot_a)}{_esc(req)}"
                f"]]></description>{''.join(dentro)}</Folder>")

    for i, cp in enumerate(campos):
        pastas.append(_aba(cp, visivel=(i == 0)))

    # ── Pegada de cada repetidora ──
    # As abas acima misturam as repetidoras: a cobertura disponível é o
    # MELHOR vizinho de cada ponto, então não dá para perguntar "até onde
    # a ERM-28 alcança". Aqui cada ERB/ERM ganha o mapa dela, com o sinal
    # que os veículos mediram para ela — posição e sinal da mesma leitura.
    #
    # Nasce recolhida e desligada: dezoito rastros ligados juntos se
    # empilham e o mapa não diz nada.
    if peers:
        try:
            por_rep = amostras_por_repetidora(am, peers)
        except Exception as e:
            log.warning(f"[kml] pegada por repetidora: {e}"); por_rep = {}
        itens_r = []
        # Da que mais foi ouvida para a que menos: a ordem do arquivo é a
        # ordem em que alguém vai querer abrir.
        for nome_r, am_r in sorted(por_rep.items(),
                                   key=lambda kv: -len(kv[1]))[:max_repetidoras]:
            png = _calor_no_kmz(am_r, "sinal", False,
                                sufixo="_" + _slug_arquivo(nome_r)[:40],
                                rotulo=f"RSSI de {nome_r}")
            if not png:
                continue
            # Duas repetidoras inteiramente abaixo do piso da escala
            # (-90 dBm) geram rasters IDÊNTICOS, porque toda a faixa
            # satura na mesma cor. Não é defeito do desenho: no mapa as
            # duas são "não serve aqui", e é verdade. Os números que as
            # separam ficam na descrição — é por isso que ela traz
            # mediana, melhor e pior, e não só a cor.
            vs = sorted(a["sinal"] for a in am_r)
            med = vs[len(vs) // 2]
            req_r = float(ESCALAS["sinal"]["req"])
            ok = sum(1 for v in vs if v > req_r)
            itens_r.append(
                f"<Folder><name>{_esc(nome_r)} — {len(am_r)} pontos</name>"
                f"<open>0</open><visibility>0</visibility>"
                f"<description><![CDATA[Mediana {med:g} dBm &middot; "
                f"{ok*100.0/len(vs):.0f}% acima de {req_r:g} dBm &middot; "
                f"melhor {vs[-1]:g} &middot; pior {vs[0]:g} dBm"
                f"]]></description>{png}</Folder>")
        if itens_r:
            pastas.append(
                f"<Folder><name>Por repetidora ({len(itens_r)})</name>"
                f"<open>0</open><visibility>0</visibility>"
                f"<description><![CDATA[RSSI medido pelos veículos para "
                f"cada ERB/ERM, na posição do veículo.]]></description>"
                f"{''.join(itens_r)}</Folder>")

    # Agora que se sabe QUAIS cores apareceram, emite so essas: gerar as
    # 40 do gradiente vezes seis grandezas encheria o arquivo de estilo
    # morto.
    # Linha e ponto tem conjuntos SEPARADOS: uma cor que so aparece em
    # ponto nao precisa de estilo de linha, e vice-versa.
    for cor in sorted(cores_linha):
        estilos.append(
            f'<Style id="r{cor}"><LineStyle><color>{_kml_cor(cor, 255)}</color>'
            f'<width>3.2</width></LineStyle></Style>')
    for cor in sorted(cores_ponto):
        estilos.append(
            f'<Style id="q{cor}"><IconStyle><color>{_kml_cor(cor)}</color>'
            f'<scale>0.42</scale><Icon><href>http://maps.google.com/mapfiles/'
            f'kml/shapes/placemark_circle.png</href></Icon></IconStyle>'
            f'<LabelStyle><scale>0</scale></LabelStyle></Style>')

    # ── 5. Medições manuais (iperf / trace) ──
    itens_m = []
    for m in (manuais or []):
        if m.get("lat") is None or m.get("lon") is None: continue
        if m.get("tipo") == "iperf":
            titulo = f"iperf {m.get('mbps','?')} Mbps"
            corpo = (f"<b>{_esc(m.get('mbps'))} Mbps</b><br>"
                     f"{_esc(m.get('local') or '')}<br>"
                     f"{_esc(m.get('banda') or '')}")
        else:
            titulo = f"trace {m.get('origem','?')} → {m.get('destino','?')}"
            corpo = (f"{_esc(m.get('saltos'))} saltos · custo "
                     f"{_esc(m.get('custo_total'))}<br>gargalo: "
                     f"{_esc(m.get('gargalo') or '—')}")
        itens_m.append(_placemark(
            titulo, f"<![CDATA[{corpo}<p>{_esc(m.get('obs') or '')}</p>]]>",
            "manual", ponto=(m["lat"], m["lon"])))
    if itens_m:
        pastas.append(f"<Folder><name>Medições manuais ({len(itens_m)})</name>"
                      f"<open>1</open>{''.join(itens_m)}</Folder>")

    # ── descrição do documento: legenda e procedência ──
    legenda = []
    ant = None
    for lim, cor in faixas:
        if ant is None:      rng = f"até {lim:g}"
        elif lim > 900:      rng = f"acima de {ant:g}"
        else:                rng = f"{ant:g} a {lim:g}"
        legenda.append(f"<tr><td bgcolor='#{cor}' width='26'>&nbsp;</td>"
                       f"<td>{rng} {unid}</td></tr>")
        ant = lim
    ef = sv.get("intervalo_efetivo_s") or sv.get("intervalo_s")
    n_cache = sum(1 for a in am if a.get("fonte") == "cache")
    # Título e descrição carregam grandeza e banda: com seis arquivos
    # abertos ao mesmo tempo no Google Earth, "Survey" em todos não diz
    # qual é qual no painel de camadas.
    rot_doc = f"{_esc(nome_sv)} — {_esc(rot_req)}" + (
        f" — {_esc(banda)}" if banda else "")
    desc = (
        f"<![CDATA[<h3>{_esc(nome_sv)}</h3>"
        f"<p><b>{_esc(rot_req)}</b>"
        + (f" · {_esc(banda)}" if banda else "") + "<br>"
        f"{quando:%d/%m/%Y %H:%M}"
        + (f" – {datetime.fromtimestamp(fim):%H:%M}" if fim else "") +
        f"<br>{len(am)} amostras · {len(por_radio_geral)} rádios"
        + (f" · intervalo efetivo {ef:g} s" if ef else "") + "</p>"
        + (f"<p><i>{n_cache} amostra(s) do cache do exporter, marcadas no "
           f"balão.</i></p>" if n_cache else "")
        + f"<h4>Cor dos pontos — {_esc(rot_req)}</h4>"
          f"<table border='0' cellpadding='2'>{''.join(legenda)}</table>"
        + (f"<p>Requisito Modular: <b>{_esc(rot_req)} {_esc(op)} {limite:g} "
           f"{unid}</b></p>" if limite is not None else "")
        + f"<p><small>Gerado por rajant_monitor em "
          f"{datetime.now():%d/%m/%Y %H:%M}</small></p>]]>")

    doc = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
           f'<name>{rot_doc} — {quando:%d/%m/%Y}</name>'
           f'<description>{desc}</description>'
           f'{"".join(estilos)}{"".join(pastas)}</Document></kml>')

    # A banda entra no nome: os arquivos vão para a mesma pasta e, sem
    # isso, o de 5.8 GHz sobrescreveria o de 2.4 GHz silenciosamente.
    suf_b = f"_{banda.replace(' ', '').replace('.', '')}" if banda else ""
    # Com várias abas o nome não cabe todas: só marca "todas" para não
    # sugerir que o arquivo tem uma grandeza só.
    suf_c = campo if len(campos) == 1 else "todas"
    base = f"Survey_{quando:%Y%m%d_%H%M}_{suf_c}{suf_b}"
    if comprimir:
        buf = _io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("doc.kml", doc)
            for nome_i, dados_i in extras:
                z.writestr(nome_i, dados_i)
        log.info(f"[kml] {base}.kmz: {len(campos)} aba(s), "
                 f"{len(am)} amostras, {len(posicoes)} BCs")
        return buf.getvalue(), f"{base}.kmz"
    return doc.encode("utf-8"), f"{base}.kml"


def gerar_kml_pontos_fixos(sitios, cfg=None, comprimir=True):
    """KMZ de capturas feitas PARADAS — uma ou varias repetidoras.

    `sitios`: [{nome, lat, lon, alt, censo, resumo, inicio, fim, pontos}].

    Desenha o que tem posicao de verdade e so isso:

      • um pino por radio capturado, na coordenada que ele proprio
        reportou, com o censo da vizinhanca no balao;
      • uma linha por enlace entre DOIS radios cujas posicoes vieram de
        capturas — colorida pelo RSSI do enlace.

    O vizinho que nao tem captura propria NAO vira ponto no mapa. Ele
    aparece no balao e na planilha, como texto. Um BreadCrumb nao reporta
    a posicao dos vizinhos (conferido nos arquivos reais: os campos sao
    channel, cost, encap, filtered, frequency, ipaddr, mac, name, rssi,
    serialNumber, signal), e desenhar um vizinho num lugar arbitrario —
    ao redor da repetidora, ou no proprio pino dela — seria inventar
    geometria que ninguem mediu. O mapa mostra o que existe; o que falta
    fica declarado.

    Devolve (bytes, nome_arquivo).
    """
    import zipfile, io as _io

    sitios = [s for s in (sitios or [])
              if s.get("lat") is not None and s.get("lon") is not None]
    if not sitios:
        raise RuntimeError("nenhuma captura parada com posicao")

    faixas = FAIXAS_KML["sinal"]
    req = float(ESCALAS["sinal"]["req"])
    estilos = []
    for cor in sorted({c for _, c in faixas} | {"808080"}):
        estilos.append(
            f'<Style id="e{cor}"><LineStyle><color>{_kml_cor(cor, 235)}</color>'
            f'<width>5</width></LineStyle></Style>')
    estilos.append(
        '<Style id="sitio"><IconStyle><scale>1.3</scale>'
        f'<color>{_kml_cor("F1C40F")}</color><Icon><href>http://maps.google.com/'
        'mapfiles/kml/shapes/target.png</href></Icon></IconStyle>'
        '<LabelStyle><scale>0.95</scale></LabelStyle></Style>')

    # Posicao por nome: so de quem foi capturado. E esta a chave do
    # cruzamento — e o motivo de duas capturas valerem muito mais que
    # duas vezes uma.
    pos = {s["nome"]: (s["lat"], s["lon"]) for s in sitios}

    pins, linhas = [], {}
    for s in sitios:
        censo = s.get("censo") or []
        r = s.get("resumo") or {}
        com = [c for c in censo if c["sinal"] is not None]
        tab = "".join(
            f"<tr><td>{_esc(c['nome'])}</td>"
            f"<td align='right'>{c['sinal']:g}</td>"
            f"<td align='right'>{'' if c['snr'] is None else format(c['snr'], 'g')}</td>"
            f"<td align='right'>{'' if c['presenca'] is None else format(c['presenca'], 'g')}%</td>"
            f"<td>{_esc(', '.join(c['bandas']))}</td></tr>"
            for c in com[:60])
        dur = ((s.get("fim") or 0) - (s.get("inicio") or 0)) / 60.0
        bal = ("<![CDATA["
               f"<h3>{_esc(s['nome'])}</h3>"
               f"<p>{s.get('pontos', 0)} leituras em {dur:.1f} min &middot; "
               f"{r.get('vizinhos', len(censo))} vizinhos "
               f"({r.get('infra', 0)} de infraestrutura, "
               f"{r.get('moveis', 0)} moveis)<br>"
               f"{r.get('acima_req', 0)} acima de {req:g} dBm, "
               f"{r.get('abaixo_req', 0)} abaixo</p>"
               "<table border='1' cellpadding='3' cellspacing='0'>"
               "<tr><th>Vizinho</th><th>RSSI</th><th>SNR</th>"
               "<th>Presenca</th><th>Banda</th></tr>"
               f"{tab}</table>"
               + (f"<p><small>{len(com) - 60} vizinho(s) a mais na "
                  f"planilha.</small></p>" if len(com) > 60 else "")
               + "]]>")
        pins.append(
            f"<Placemark><name>{_esc(s['nome'])}</name>"
            f"<description>{bal}</description><styleUrl>#sitio</styleUrl>"
            f"<Point><altitudeMode>clampToGround</altitudeMode>"
            f"<coordinates>{s['lon']:.7f},{s['lat']:.7f},0</coordinates>"
            f"</Point></Placemark>")

        for c in censo:
            outro = c["nome"]
            if outro not in pos or outro == s["nome"]:
                continue
            # O enlace A-B aparece nas duas capturas, com leituras
            # proprias de cada ponta. Desenhar as duas empilharia linha
            # sobre linha; fica a PIOR das duas, que e a que limita o
            # enlace — e a que decide se precisa de mais radio ali.
            par = tuple(sorted((s["nome"], outro)))
            v = c["sinal"]
            ant = linhas.get(par)
            if ant is None or (v is not None and
                               (ant[0] is None or v < ant[0])):
                linhas[par] = (v, c)

    itens_l = []
    for par, (v, c) in sorted(linhas.items()):
        (la1, lo1), (la2, lo2) = pos[par[0]], pos[par[1]]
        cor = _bucket_cor(v, faixas)
        d = _dist_m(la1, lo1, la2, lo2)
        txt_rssi = "sem leitura" if v is None else f"{v:g} dBm"
        txt_snr = "—" if c.get("snr") is None else f"{c['snr']:g} dB"
        itens_l.append(
            # Caractere literal, nao "&harr;": KML e XML, e XML so conhece
            # as cinco entidades predefinidas. `&harr;` derruba o
            # documento inteiro com "undefined entity" — e o Google Earth
            # nao abre nada. Dentro de CDATA (os baloes) entidade HTML
            # passa, porque ali nao ha parsing.
            f"<Placemark><name>{_esc(par[0])} ↔ {_esc(par[1])}</name>"
            f"<description><![CDATA[RSSI {txt_rssi} &middot; SNR {txt_snr}"
            f" &middot; {_milhar(round(d))} m]]></description>"
            f"<styleUrl>#e{cor}</styleUrl>"
            f"<LineString><tessellate>1</tessellate>"
            f"<altitudeMode>clampToGround</altitudeMode>"
            f"<coordinates>{lo1:.7f},{la1:.7f},0 {lo2:.7f},{la2:.7f},0"
            f"</coordinates></LineString></Placemark>")

    pastas = [f"<Folder><name>Radios capturados ({len(pins)})</name>"
              f"<open>1</open>{''.join(pins)}</Folder>"]
    if itens_l:
        pastas.append(f"<Folder><name>Enlaces medidos ({len(itens_l)})</name>"
                      f"<open>1</open>{''.join(itens_l)}</Folder>")

    legenda = "".join(
        f"<tr><td bgcolor='#{cor}' width='26'>&nbsp;</td>"
        f"<td>&lt; {lim:g} dBm</td></tr>"
        for lim, cor in faixas if lim < 900)
    sem_pos = sorted({c["nome"] for s in sitios for c in (s.get("censo") or [])
                      if c["nome"] not in pos})
    desc = ("<![CDATA["
            f"<p><b>{len(pins)}</b> radio(s) capturado(s) parado(s), "
            f"<b>{len(itens_l)}</b> enlace(s) desenhado(s).</p>"
            f"<h4>Cor do enlace — RSSI</h4>"
            f"<table border='0' cellpadding='2'>{legenda}</table>"
            f"<p>Requisito Modular: RSSI &gt; {req:g} dBm</p>"
            + (f"<p><b>{len(sem_pos)}</b> vizinho(s) aparecem no censo mas "
               f"nao no mapa: so ha posicao de quem tem captura propria. "
               f"O BreadCrumb nao informa onde estao os vizinhos dele.</p>"
               if sem_pos else "")
            + f"<p><small>Gerado por rajant_monitor em "
              f"{datetime.now():%d/%m/%Y %H:%M}</small></p>]]>")

    quando = (datetime.fromtimestamp(sitios[0]["inicio"])
              if sitios[0].get("inicio") else datetime.now())
    doc = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
           f'<name>Vizinhanca medida — {quando:%d/%m/%Y}</name>'
           f'<description>{desc}</description>'
           f'{"".join(estilos)}{"".join(pastas)}</Document></kml>')

    base = (f"Vizinhanca_{_slug_arquivo(sitios[0]['nome'])}_{quando:%Y%m%d_%H%M}"
            if len(sitios) == 1
            else f"Vizinhanca_{len(sitios)}_radios_{quando:%Y%m%d_%H%M}")
    log.info(f"[kml] {base}: {len(pins)} radio(s), {len(itens_l)} enlace(s), "
             f"{len(sem_pos)} vizinho(s) sem posicao")
    if comprimir:
        buf = _io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("doc.kml", doc)
        return buf.getvalue(), f"{base}.kmz"
    return doc.encode("utf-8"), f"{base}.kml"


def gerar_kmz_survey(saida_kmz, csv_path=None, kml_bcs=None, bandas=("2.4 GHz","5.8 GHz"),
                     rotas_gps=None, bcs_gps=None, raio_m=300.0, cobertura=True,
                     cfg=None, alcance_m=1000.0):
    """Monta o KMZ do survey. Retorna (bytes, nome_arquivo).

    csv_path : medições georreferenciadas (heatmaps por métrica/banda)
    kml_bcs  : KML/KMZ do BC Commander (posições dos BreadCrumbs)
    rotas_gps: {equipamento: [(lat,lon,ts), ...]} — trajetos do GPS dos rádios
    bcs_gps  : [(nome, lat, lon)] — BCs vindos do Prometheus
    """
    import math, zipfile, io as _io, tempfile
    import numpy as np
    from matplotlib.colors import LinearSegmentedColormap

    CORES = ["#C0392B","#E74C3C","#E67E22","#F1C40F","#9ACD32","#27AE60","#1E8449"]
    cmap_alto  = LinearSegmentedColormap.from_list("s", CORES)
    cmap_baixo = LinearSegmentedColormap.from_list("si", list(reversed(CORES)))

    ESTILOS_ROTA = ""
    tmp = Path(tempfile.mkdtemp(prefix="kmz_"))
    arquivos, pastas_kml = [], []

    # ── Posições dos BreadCrumbs ──
    pontos_bc = list(bcs_gps or [])
    kd = None
    if kml_bcs and Path(kml_bcs).exists():
        try:
            kd = ler_kmz(kml_bcs, str(tmp / "kml_in"))
            for p in kd["pontos"]:
                pontos_bc.append((p["nome"], p["lat"], p["lon"]))
        except Exception as e:
            log.warning(f"[kmz] KML de BCs nao lido: {e}")

    pontos_bc, descartados_bc = limpar_coords(pontos_bc)
    if descartados_bc:
        log.warning(f"[kmz] {descartados_bc} BC(s) com coordenada invertida ignorados")
    if pontos_bc:
        itens = []
        for nome_bc, la, lo in pontos_bc:
            n_up = str(nome_bc).upper()
            estilo = ("#bcERB" if n_up.startswith("ERB") else
                      "#bcERM" if n_up.startswith("ERM") else "#bcMovel")
            itens.append(
                f"<Placemark><name>{_esc(nome_bc)}</name><styleUrl>{estilo}</styleUrl>"
                f"<Point><altitudeMode>clampToGround</altitudeMode>"
                f"<coordinates>{lo:.7f},{la:.7f},0</coordinates></Point></Placemark>")
        pastas_kml.append(
            f"<Folder><name>BreadCrumbs ({len(pontos_bc)})</name><open>0</open>"
            f"{''.join(itens)}</Folder>")

    # ── Survey pela frota: rota COLORIDA pela medição do próprio rádio ──
    # Cada trecho entre dois pontos recebe a cor do SNR (ou do sinal) medido
    # ali. O equipamento em operação vira o medidor — é o survey contínuo,
    # sem veículo dedicado. Trechos sem medição ficam cinza.
    FAIXAS_SNR   = [(10,"C0392B"), (15,"E74C3C"), (20,"E67E22"),
                    (25,"F1C40F"), (30,"9ACD32"), (999,"27AE60")]
    FAIXAS_SINAL = [(-80,"C0392B"), (-75,"E74C3C"), (-70,"E67E22"),
                    (-65,"F1C40F"), (-58,"9ACD32"), (999,"27AE60")]
    if rotas_gps:
        rotas_gps = {k: [p for p in v if abs(p[0]) <= 90 and abs(p[1]) <= 180]
                     for k, v in rotas_gps.items()}
        rotas_gps = {k: v for k, v in rotas_gps.items() if len(v) >= 2}
        blocos, estilos_rota = [], set()
        for equip, pts in sorted(rotas_gps.items()):
            if len(pts) < 2: continue
            trechos = []
            for i in range(len(pts)-1):
                a_, b_ = pts[i], pts[i+1]
                metr = a_[3] if len(a_) > 3 else None          # snr
                usa_snr = metr is not None
                if not usa_snr and len(a_) > 6: metr = a_[6]   # sinal
                cor = _bucket_cor(metr, FAIXAS_SNR if usa_snr else FAIXAS_SINAL)
                estilos_rota.add(cor)
                rot = (f"{metr:.0f} dB" if usa_snr and metr is not None
                       else (f"{metr:.0f} dBm" if metr is not None else "sem medição"))
                trechos.append(
                    f"<Placemark><name>{_esc(rot)}</name><styleUrl>#t{cor}</styleUrl>"
                    f"<LineString><tessellate>1</tessellate>"
                    f"<altitudeMode>clampToGround</altitudeMode><coordinates>"
                    f"{b_[1]:.7f},{b_[0]:.7f},0 {a_[1]:.7f},{a_[0]:.7f},0"
                    f"</coordinates></LineString></Placemark>")
            blocos.append(
                f"<Folder><name>{_esc(equip)} ({len(pts)} pontos)</name><open>0</open>"
                f"<Placemark><name>{_esc(equip)} — início</name><styleUrl>#inicio</styleUrl>"
                f"<Point><coordinates>{pts[0][1]:.7f},{pts[0][0]:.7f},0</coordinates></Point></Placemark>"
                f"{''.join(trechos)}"
                f"<Placemark><name>{_esc(equip)} — fim</name><styleUrl>#fim</styleUrl>"
                f"<Point><coordinates>{pts[-1][1]:.7f},{pts[-1][0]:.7f},0</coordinates></Point></Placemark>"
                f"</Folder>")
        if blocos:
            pastas_kml.append(
                f"<Folder><name>Survey pela frota — rotas medidas ({len(rotas_gps)})</name>"
                f"<open>1</open><description>Cor do trecho = qualidade medida pelo "
                f"radio do proprio equipamento</description>{''.join(blocos)}</Folder>")
        ESTILOS_ROTA = "".join(
            f'<Style id="t{cor}"><LineStyle><color>{_kml_cor(cor)}</color>'
            f'<width>4</width></LineStyle></Style>' for cor in sorted(estilos_rota))
    else:
        ESTILOS_ROTA = ""

    # ── Heatmaps por banda/métrica (GroundOverlay transparente) ──
    def grade_idw(x, y, z, bb, lat_med, n=520):
        from scipy.spatial import cKDTree
        mlat = 111320.0; mlon = 111320.0*math.cos(math.radians(lat_med))
        gx = np.linspace(bb["oeste"], bb["leste"], n)
        gy = np.linspace(bb["sul"],   bb["norte"], n)
        GX, GY = np.meshgrid(gx, gy)
        arv = cKDTree(np.c_[x*mlon, y*mlat])
        k = min(12, len(x))
        d, idx = arv.query(np.c_[GX.ravel()*mlon, GY.ravel()*mlat], k=k)
        if k == 1: d, idx = d[:, None], idx[:, None]
        with np.errstate(divide="ignore", invalid="ignore"):
            w = 1.0/np.maximum(d, 1.0)**2
            w[d > raio_m] = 0.0
            s = w.sum(axis=1)
            G = np.where(s > 0, (w*z[idx]).sum(axis=1)/np.maximum(s, 1e-12), np.nan)
        return G.reshape(GX.shape)

    base = ler_csv_survey(csv_path) if (csv_path and Path(csv_path).exists()) else None
    if base or pontos_bc:
        for banda in bandas:
            d = _filtra_banda(base, banda) if base else {"_metricas": []}
            if base:
                lat = np.array([v if v is not None else np.nan for v in d["lat"]], float)
                lon = np.array([v if v is not None else np.nan for v in d["lon"]], float)
                ok0 = ~(np.isnan(lat) | np.isnan(lon))
            else:
                lat = lon = np.array([]); ok0 = np.array([], dtype=bool)
            # área de referência: medições + posições dos BCs + rotas
            cand_la = list(lat[ok0]) if ok0.any() else []
            cand_lo = list(lon[ok0]) if ok0.any() else []
            for _, la_, lo_ in pontos_bc: cand_la.append(la_); cand_lo.append(lo_)
            for pts in (rotas_gps or {}).values():
                for p in pts: cand_la.append(p[0]); cand_lo.append(p[1])
            if len(cand_la) < 3: continue
            marg_la = (max(cand_la)-min(cand_la))*0.08 + 0.004
            marg_lo = (max(cand_lo)-min(cand_lo))*0.08 + 0.004
            bb = {"sul": min(cand_la)-marg_la, "norte": max(cand_la)+marg_la,
                  "oeste": min(cand_lo)-marg_lo, "leste": max(cand_lo)+marg_lo}
            lat_med = (bb["norte"]+bb["sul"])/2
            sufb = str(banda).replace(".","").replace(" ","")
            blocos = []
            for met in d.get("_metricas", []):
                rotulo, unid, limiar, maior, faixas, escala = METRICAS_SURVEY[met]
                v = np.array([x if x is not None else np.nan for x in d[met]], float)
                ok = ok0 & ~np.isnan(v)
                if ok.sum() < 3: continue
                GZ = grade_idw(lon[ok], lat[ok], v[ok], bb, lat_med)
                cmap = cmap_alto if maior else cmap_baixo
                nome_png = f"heat_{met}_{sufb}.png"
                _png_overlay(GZ, bb, cmap, escala[0], escala[1], tmp/nome_png)
                arquivos.append((tmp/nome_png, f"files/{nome_png}"))
                dentro = (v[ok] >= limiar).mean() if maior else (v[ok] <= limiar).mean()
                blocos.append(
                    f"<GroundOverlay><name>{_esc(rotulo)} — {dentro*100:.1f}% dentro do requerido</name>"
                    f"<visibility>0</visibility>"
                    f"<description>{_esc(f'{rotulo} ({unid}) | requisito {chr(62) if maior else chr(60)} {limiar} | escala {escala[0]}..{escala[1]}')}</description>"
                    f"<Icon><href>files/{nome_png}</href></Icon>"
                    f"<LatLonBox><north>{bb['norte']:.7f}</north><south>{bb['sul']:.7f}</south>"
                    f"<east>{bb['leste']:.7f}</east><west>{bb['oeste']:.7f}</west></LatLonBox>"
                    f"</GroundOverlay>")
            # ── Mapa de calor de cobertura em volta de ERM/ERB ──
            if cobertura and pontos_bc:
                try:
                    infra = [p for p in pontos_bc
                             if str(p[0]).upper().startswith(("ERB","ERM"))] or pontos_bc
                    nome_png = f"calor_rf_{sufb}.png"
                    _, cob_pct, dentro_pct = heat_infra_png(
                        infra, bb, tmp/nome_png, banda=banda, alcance_m=alcance_m)
                    arquivos.append((tmp/nome_png, f"files/{nome_png}"))
                    blocos.append(
                        f"<GroundOverlay><name>Mapa de calor RF — cobertura ERM/ERB "
                        f"({dentro_pct:.0f}% da área coberta ≥ -75 dBm)</name>"
                        f"<visibility>1</visibility><color>ffffffff</color>"
                        f"<description>{_esc(f'{len(infra)} pontos de cobertura | alcance {alcance_m:.0f} m | estimativa de planejamento')}</description>"
                        f"<Icon><href>files/{nome_png}</href></Icon>"
                        f"<LatLonBox><north>{bb['norte']:.7f}</north><south>{bb['sul']:.7f}</south>"
                        f"<east>{bb['leste']:.7f}</east><west>{bb['oeste']:.7f}</west></LatLonBox>"
                        f"</GroundOverlay>")
                except Exception as e:
                    log.warning(f"[kmz] mapa de calor RF: {e}")
            if False and cobertura and pontos_bc:
                f_mhz = 2437.0 if str(banda).startswith("2") else 5745.0
                mlat = 111320.0; mlon = 111320.0*math.cos(math.radians(lat_med))
                gx = np.linspace(bb["oeste"], bb["leste"], 520)
                gy = np.linspace(bb["sul"],   bb["norte"], 520)
                GX, GY = np.meshgrid(gx, gy)
                fspl1 = 20*math.log10(f_mhz) - 27.55
                melhor = np.full(GX.shape, -200.0)
                for _, la, lo in pontos_bc:
                    dd = np.hypot(GX*mlon - lo*mlon, GY*mlat - la*mlat)
                    np.maximum(dd, 1.0, out=dd)
                    np.maximum(melhor, (25.0+16.0) - (fspl1 + 23.0*np.log10(dd)), out=melhor)
                nome_png = f"cobertura_{sufb}.png"
                _png_overlay(melhor, bb, cmap_alto, -85, -45, tmp/nome_png, alfa=0.62)
                arquivos.append((tmp/nome_png, f"files/{nome_png}"))
                blocos.append(
                    f"<GroundOverlay><name>Cobertura estimada — {(melhor>=-75).mean()*100:.0f}% ≥ -75 dBm</name>"
                    f"<visibility>0</visibility>"
                    f"<description>Modelo log-distancia (n=2.3) a partir de "
                    f"{len(pontos_bc)} BreadCrumbs — estimativa de planejamento</description>"
                    f"<Icon><href>files/{nome_png}</href></Icon>"
                    f"<LatLonBox><north>{bb['norte']:.7f}</north><south>{bb['sul']:.7f}</south>"
                    f"<east>{bb['leste']:.7f}</east><west>{bb['oeste']:.7f}</west></LatLonBox>"
                    f"</GroundOverlay>")
            if blocos:
                pastas_kml.append(
                    f"<Folder><name>{_esc(banda)}</name><open>0</open>"
                    f"<description>Marque UMA camada por vez para leitura correta"
                    f"</description>{''.join(blocos)}</Folder>")

    # ── Legenda (ScreenOverlay, canto inferior esquerdo) ──
    _legenda_png(tmp/"legenda.png", "Escala — pior (vermelho) a melhor (verde)",
                 ["fora do requisito", "limítrofe", "dentro do requisito",
                  "BreadCrumb ERB", "BreadCrumb ERM", "rota do equipamento"],
                 ["C0392B", "F1C40F", "27AE60", "F1C40F", "2980B9", "9B59B6"])
    arquivos.append((tmp/"legenda.png", "files/legenda.png"))
    pastas_kml.append(
        '<ScreenOverlay><name>Legenda</name><Icon><href>files/legenda.png</href></Icon>'
        '<overlayXY x="0" y="0" xunits="fraction" yunits="fraction"/>'
        '<screenXY x="12" y="12" xunits="pixels" yunits="pixels"/>'
        '<size x="0" y="0" xunits="pixels" yunits="pixels"/></ScreenOverlay>')

    estilos = f"""
<Style id="bcERB"><IconStyle><scale>1.2</scale><color>{_kml_cor('F1C40F')}</color>
<Icon><href>http://maps.google.com/mapfiles/kml/shapes/triangle.png</href></Icon></IconStyle>
<LabelStyle><scale>0.8</scale></LabelStyle></Style>
<Style id="bcERM"><IconStyle><scale>1.1</scale><color>{_kml_cor('2980B9')}</color>
<Icon><href>http://maps.google.com/mapfiles/kml/shapes/triangle.png</href></Icon></IconStyle>
<LabelStyle><scale>0.75</scale></LabelStyle></Style>
<Style id="bcMovel"><IconStyle><scale>1.0</scale><color>{_kml_cor('27AE60')}</color>
<Icon><href>http://maps.google.com/mapfiles/kml/shapes/donut.png</href></Icon></IconStyle>
<LabelStyle><scale>0.7</scale></LabelStyle></Style>
<Style id="rota"><LineStyle><color>{_kml_cor('9B59B6')}</color><width>3</width></LineStyle></Style>
<Style id="inicio"><IconStyle><scale>0.9</scale><color>{_kml_cor('27AE60')}</color>
<Icon><href>http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png</href></Icon></IconStyle></Style>
<Style id="fim"><IconStyle><scale>0.9</scale><color>{_kml_cor('C0392B')}</color>
<Icon><href>http://maps.google.com/mapfiles/kml/shapes/placemark_square.png</href></Icon></IconStyle></Style>
"""
    doc = (f'<?xml version="1.0" encoding="UTF-8"?>\n'
           f'<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
           f'<name>Site Survey — Rede Rajant</name>'
           f'<description>Gerado por RajantMonitor em '
           f'{datetime.now():%d/%m/%Y %H:%M}. Ligue UMA camada de heatmap por vez.'
           f'</description>{estilos}{ESTILOS_ROTA}{"".join(pastas_kml)}</Document></kml>')

    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", doc)
        for origem, destino in arquivos:
            z.write(origem, destino)
    nome = Path(saida_kmz).name if saida_kmz else "Survey_Rajant.kmz"
    return buf.getvalue(), nome

# ══════════════════════════════════════════════════════════════
# LEITURA DE CAPTURA DO MESHMAPPER (Rajant BC Commander)
# ══════════════════════════════════════════════════════════════
# O MeshMapper é a ferramenta da própria Rajant: roda no notebook dentro
# do veículo e grava, a cada segundo, a posição e TODOS os vizinhos
# visíveis. Ler o arquivo dele em vez de sondar a malha resolve três
# coisas de uma vez:
#
#   1. a captura é contínua de verdade (interval = 1 s no arquivo), sem
#      o custo de um segundo sondador competindo com o Dispatch;
#   2. traz TODOS os peers visíveis por ponto, não só o que atendeu —
#      dá para dizer "estava ligado no X e havia um Y melhor ao lado";
#   3. quem gera o relatório não precisa de rede nenhuma.
#
# Fonte preferida: o data.json de dentro do .kmz. Os dois CSVs têm as
# mesmas colunas, porém sem altitude e sem numActivePeers; servem quando
# só eles sobraram.
#
# ARMADILHAS DE CAMPO, conferidas no arquivo real:
#
#   • "RSSI (SNR)" é SNR em dB; "Signal" é o RSSI em dBm. Os nomes das
#     colunas trocam os dois. Trocá-los inverteria a escala inteira.
#   • O ruído NÃO vem no arquivo, mas é recuperável: ruido = signal - snr.
#     Não é estimativa — em 5450 amostras deu só 6 valores distintos,
#     agrupados em -109 dBm (5,8 GHz) e -94 dBm (2,4 GHz), que é o piso
#     de ruído que o rádio usou para calcular o SNR.
#   • "Rate (Kb/s)" traz 65, 130, 195, 260 — as taxas MCS de 802.11n em
#     Mbps. O rótulo da coluna está errado; 65 Kb/s num enlace de malha
#     não existe. Tratado como Mbps.
#   • Ponto sem enlace vem com Type "N/A" e custo 2147483647 (INT_MAX).
#     Vira amostra SEM sinal, não amostra com sinal ruim: são coisas
#     diferentes no relatório e no mapa.
# ══════════════════════════════════════════════════════════════

# Custo que o InstaMesh usa para "sem rota". Aparece cru no CSV.
CUSTO_SEM_ROTA = 2147483647


def _mm_num(v):
    """Número do CSV/JSON, ou None. Campo vazio, '0' de placeholder e
    'N/A' significam ausência — e ausência é None, nunca 0."""
    if v is None: return None
    s = str(v).strip().strip('"')
    if not s or s.upper() in ("N/A", "NA", "-"): return None
    try:
        f = float(s)
    except ValueError:
        return None
    return f


def _mm_enlace(sig, snr, custo):
    """Limpa um trio (sinal, SNR, custo) vindo do MeshMapper.

    O arquivo usa 0 como "ainda nao medi este enlace", e 0 dBm nao existe
    num radio de malha — seria uma potencia recebida igual a 1 mW, colada
    na antena. Deixar passar nao e detalhe: `cobertura_disponivel()`
    escolhe o vizinho por `max(sinal)`, e o 0 ganha de todos. Medido no
    arquivo real do CA-1006: tres pontos do trajeto saiam pintados de
    verde maximo com "87 dB disponiveis e nao usados", em cima de um 0 do
    ERB-11 L2. Numero inventado entrando no laudo como prova.

    Vale para o enlace servidor E para cada vizinho da lista — o furo
    estava justamente na lista, que nao passava por aqui.
    """
    if sig == 0: sig = None
    if snr == 0 and sig is None: snr = None
    if custo is not None and custo >= CUSTO_SEM_ROTA:
        custo = None                          # INT_MAX = sem rota
    ruido = (sig - snr) if (sig is not None and snr is not None) else None
    return sig, snr, custo, ruido


def _mm_amostra(nome_movel, ts, lat, lon, alt, path, n_peers):
    """Uma amostra no formato interno, a partir do enlace servidor."""
    sig, snr, custo, ruido = _mm_enlace(
        _mm_num(path.get("signal")),
        _mm_num(path.get("rssi")),            # sim: 'rssi' do arquivo é SNR
        _mm_num(path.get("cost")))
    freq = _mm_num(path.get("freq")) or _mm_num(path.get("frequency"))
    return {
        "radio": nome_movel,
        "ts": ts,
        "lat": lat, "lon": lon, "alt": alt,
        "vel": None, "sats": None, "hdop": None,
        "sinal": sig,
        "snr": snr,
        # Recuperado, não estimado — ver o cabeçalho da seção.
        "ruido": ruido,
        "rtt": None, "perda": None, "interf": None, "vazao": None,
        "custo": custo,
        "taxa": _mm_num(path.get("rate")),    # MCS em Mbps, apesar do rótulo
        "peers": n_peers,
        "banda": _norm_banda(freq) if freq else None,
        "canal": _mm_num(path.get("channel")),
        "servidor": (path.get("name") or path.get("mac") or "").strip() or None,
        "fonte": "meshmapper",
    }


def ler_meshmapper(caminho):
    """Lê uma captura do MeshMapper e devolve (sv, amostras, peers).

    `caminho` pode ser o .kmz, o data.json solto, ou um dos dois CSVs —
    neste caso o par é localizado pelo prefixo do nome.

    `sv`      metadados da captura (nome, início, fim, intervalo, móvel)
    `amostras` no formato interno, uma por ponto
    `peers`   todos os vizinhos visíveis por ponto: [{ponto, ts, lat, lon,
              nome, ip, mac, wlan, banda, canal, sinal, snr, custo}]
    """
    p = Path(caminho)
    if not p.exists():
        raise RuntimeError(f"arquivo nao encontrado: {caminho}")
    if p.suffix.lower() == ".kmz":
        import zipfile
        with zipfile.ZipFile(p) as z:
            nomes = [n for n in z.namelist() if n.endswith("data.json")]
            if not nomes:
                raise RuntimeError(
                    f"{p.name}: KMZ sem data.json. Este KMZ nao parece ser "
                    f"do MeshMapper — se for, mande os CSVs.")
            return _mm_do_json(json.loads(z.read(nomes[0])), p.stem)
    if p.suffix.lower() == ".json":
        return _mm_do_json(json.loads(p.read_text(encoding="utf-8")), p.stem)
    if p.suffix.lower() == ".csv":
        return _mm_dos_csv(p)
    raise RuntimeError(f"{p.name}: esperado .kmz, .json ou .csv do MeshMapper")


def _mm_do_json(d, rotulo):
    pontos = d.get("points") or []
    if not pontos:
        raise RuntimeError("captura do MeshMapper sem pontos")
    cfg_mm = d.get("configuration") or {}
    meta = cfg_mm.get("crumbMeta") or {}
    movel = (meta.get("name") or meta.get("serialStr") or "movel").strip()

    amostras, peers = [], []
    for i, pt in enumerate(pontos, start=1):
        ts = (pt.get("unixTimeStamp") or 0) / 1000.0 or None
        lat, lon = pt.get("gpsLat"), pt.get("gpsLong")
        if lat is None or lon is None:
            continue                       # sem posição não vai para o mapa
        path = ((pt.get("traceInfo") or {}).get("path")) or {}
        amostras.append(_mm_amostra(movel, ts, lat, lon, pt.get("gpsAlt"),
                                    path, pt.get("numActivePeers")))
        for wlan, lista in (pt.get("wlanPeers") or {}).items():
            for q in (lista or []):
                sig, snr, custo, ruido = _mm_enlace(
                    _mm_num(q.get("signal")), _mm_num(q.get("rssi")),
                    _mm_num(q.get("cost")))
                freq = _mm_num(q.get("frequency"))
                peers.append({
                    # `movel` e QUEM CAPTUROU, nao o vizinho. Sem ele, ao
                    # juntar varios arquivos nao da para saber de qual
                    # captura veio a leitura: os numeros de ponto se
                    # repetem entre arquivos.
                    "movel": movel,
                    "ponto": i, "ts": ts, "lat": lat, "lon": lon,
                    "nome": (q.get("name") or "").strip() or q.get("serialNumber"),
                    "ip": q.get("ipaddr"), "mac": q.get("mac"),
                    "serie": q.get("serialNumber"), "wlan": wlan,
                    "banda": _norm_banda(freq) if freq else None,
                    "canal": _mm_num(q.get("channel")),
                    "freq": freq,
                    "sinal": sig, "snr": snr, "ruido": ruido, "custo": custo,
                })

    ts_v = [a["ts"] for a in amostras if a["ts"]]
    sv = {
        "nome": f"MeshMapper — {movel}",
        "movel": movel,
        "inicio": min(ts_v) if ts_v else None,
        "fim": max(ts_v) if ts_v else None,
        "intervalo_s": cfg_mm.get("interval"),
        "arquivo": rotulo,
        "versao_bcc": d.get("bcc_version"),
        # Limiares que o PRÓPRIO MeshMapper usou. Ficam registrados para o
        # relatório poder dizer com que régua a captura foi classificada,
        # em vez de impor a nossa em cima e chamar de "o que o MeshMapper
        # mostrou".
        "limiares_mm": {k: cfg_mm.get(k) for k in
                        ("goodCost", "greatCost", "goodRSSI", "greatRSSI",
                         "goodPath", "greatPath") if cfg_mm.get(k) is not None},
    }
    return sv, amostras, peers


def _mm_dos_csv(p):
    """Par de CSVs do MeshMapper. Menos rico que o data.json — sem
    altitude e sem numActivePeers —, mas é o que sobra quando só os CSVs
    foram guardados."""
    import csv as _csv
    base = re.sub(r"_(trace_path|peer_info)$", "", p.stem)
    trace = p.with_name(base + "_trace_path.csv")
    pinfo = p.with_name(base + "_peer_info.csv")
    if not trace.exists():
        raise RuntimeError(f"nao achei {trace.name} ao lado de {p.name}")

    movel = "movel"
    linhas = trace.read_text(encoding="utf-8", errors="replace").splitlines()
    if linhas and "Trace to" in linhas[0]:
        # 1a linha: "Trace to:","Serial:",...,"Name:","CA-1006","IP:",...
        campos = next(_csv.reader([linhas[0]]))
        if "Name:" in campos:
            movel = campos[campos.index("Name:") + 1].strip() or movel
        linhas = linhas[1:]

    amostras = []
    for r in _csv.DictReader(linhas):
        lat, lon = _mm_num(r.get("Latitude")), _mm_num(r.get("Longitude"))
        if lat is None or lon is None: continue
        amostras.append(_mm_amostra(
            movel, _mm_ts(r.get("Timestamp")), lat, lon, None,
            {"signal": r.get("Signal"), "rssi": r.get("RSSI (SNR)"),
             "cost": r.get("Cost"), "rate": r.get("Rate (Kb/s)"),
             "channel": r.get("Channel"), "freq": r.get("Frequency"),
             "name": r.get("IP/MAC")}, None))

    peers = []
    if pinfo.exists():
        for r in _csv.DictReader(open(pinfo, encoding="utf-8", errors="replace")):
            sig, snr, custo, ruido = _mm_enlace(
                _mm_num(r.get("Signal")), _mm_num(r.get("RSSI (SNR)")),
                _mm_num(r.get("Cost")))
            freq = _mm_num(r.get("Frequency"))
            peers.append({
                "movel": movel,          # quem capturou — ver _mm_do_json
                "ponto": int(_mm_num(r.get("Point Num")) or 0),
                "ts": _mm_ts(r.get("Timestamp")),
                "lat": _mm_num(r.get("Latitude")), "lon": _mm_num(r.get("Longitude")),
                "nome": (r.get("Name") or "").strip() or r.get("Serial"),
                "ip": r.get("IP"), "mac": r.get("MAC Address"),
                "serie": r.get("Serial"), "wlan": r.get("Wlan"),
                "banda": _norm_banda(freq) if freq else None,
                "canal": _mm_num(r.get("Channel")),
                "freq": freq,
                "sinal": sig, "snr": snr, "ruido": ruido, "custo": custo,
            })

    ts_v = [a["ts"] for a in amostras if a["ts"]]
    sv = {"nome": f"MeshMapper — {movel}", "movel": movel,
          "inicio": min(ts_v) if ts_v else None,
          "fim": max(ts_v) if ts_v else None,
          "intervalo_s": None, "arquivo": base, "versao_bcc": None,
          "limiares_mm": {}}
    return sv, amostras, peers


def importar_meshmapper(caminho, con=None, nome=None):
    """Importa uma captura do MeshMapper como um survey no banco.

    Passar pelo banco em vez de gerar direto do arquivo nao e burocracia:
    e o que faz a captura importada aparecer no historico, poder ser
    comparada com outra e alimentar os MESMOS geradores de KMZ, PPT e
    Excel que o survey proprio usa. Um caminho de saida, duas origens.

    Devolve (sid, sv, amostras, peers).
    """
    sv, amostras, peers = ler_meshmapper(caminho)
    if not amostras:
        raise RuntimeError(f"{Path(caminho).name}: nenhuma amostra com posicao")
    sv["cobertura"] = cobertura_disponivel(amostras, peers)
    fechar = con is None
    con = con or banco()
    try:
        radios = sorted({a["radio"] for a in amostras})
        sid = survey_criar(con, nome or sv["nome"], sv["inicio"],
                           sv.get("intervalo_s"), radios)
        amostras_gravar(con, sid, amostras)
        resumo = survey_resumo(amostras)
        # n_moveis/n_fixos: no MeshMapper quem anda e o veiculo com o
        # notebook — um so. Os vizinhos aparecem como peers, nao como
        # equipamentos medindo, entao nao entram na contagem de moveis.
        survey_fechar(con, sid, sv["fim"], resumo,
                      n_moveis=len(radios), n_fixos=0,
                      intervalo_efetivo_s=_mm_intervalo_real(amostras))
        log.info(f"[meshmapper] survey {sid}: {len(amostras)} amostras, "
                 f"{len(peers)} leituras de vizinho, movel {sv['movel']}")
        return sid, sv, amostras, peers
    finally:
        if fechar: con.close()


def importar_meshmapper_varios(caminhos, con=None, nome=None):
    """Junta VARIAS capturas do MeshMapper num survey so.

    Uma campanha de survey costuma ser varios veiculos, ou o mesmo
    veiculo em turnos diferentes, cobrindo a mina. O laudo e um: um KMZ
    com todos os rastros, um PPT, um Excel.

    Cada amostra mantem o nome do movel que a produziu — e o que faz o
    mapa de calor tratar cada trajeto separadamente e a rota nao ser
    ligada de um veiculo ao outro.

    Devolve (sid, sv, amostras, peers, origens), onde `origens` lista o
    que veio de cada arquivo, inclusive os que falharam: relatorio que
    engole arquivo ilegivel em silencio e pior que relatorio que falta.
    """
    amostras, peers, origens = [], [], []
    for c in caminhos:
        try:
            sv_i, am_i, pr_i = ler_meshmapper(c)
        except Exception as e:
            origens.append({"arquivo": Path(c).name, "ok": False,
                            "erro": str(e), "amostras": 0})
            log.warning(f"[meshmapper] {Path(c).name}: {e}")
            continue
        # A cobertura e por ARQUIVO: os numeros de ponto se repetem entre
        # capturas, e cruza-los ligaria o vizinho de uma ao trajeto de
        # outra.
        cob_i = cobertura_disponivel(am_i, pr_i)
        for a in am_i:
            a["arquivo"] = Path(c).name
        amostras.extend(am_i); peers.extend(pr_i)
        origens.append({"arquivo": Path(c).name, "ok": True,
                        "movel": sv_i.get("movel"), "cobertura": cob_i,
                        "amostras": len(am_i), "peers": len(pr_i),
                        "inicio": sv_i.get("inicio"), "fim": sv_i.get("fim"),
                        "versao_bcc": sv_i.get("versao_bcc"),
                        "limiares_mm": sv_i.get("limiares_mm") or {}})
    if not amostras:
        raise RuntimeError("nenhuma amostra com posicao nos arquivos lidos")

    ok = [o for o in origens if o["ok"]]
    moveis = sorted({o.get("movel") for o in ok if o.get("movel")})
    ts_v = [a["ts"] for a in amostras if a.get("ts")]
    sv = {
        "nome": nome or (f"MeshMapper — {moveis[0]}" if len(moveis) == 1
                         else f"MeshMapper — {len(moveis)} equipamentos"),
        "movel": ", ".join(moveis) or "movel",
        "inicio": min(ts_v) if ts_v else None,
        "fim": max(ts_v) if ts_v else None,
        "intervalo_s": None,
        "arquivo": f"{len(ok)} arquivo(s)",
        "versao_bcc": next((o.get("versao_bcc") for o in ok
                            if o.get("versao_bcc")), None),
        # A regua e a mesma em todos os arquivos do mesmo MeshMapper;
        # havendo divergencia, a do primeiro vale e a diferenca aparece
        # na aba de origens em vez de sumir numa media.
        "limiares_mm": next((o.get("limiares_mm") for o in ok
                             if o.get("limiares_mm")), {}),
        "cobertura": {
            "com_infra": sum((o.get("cobertura") or {}).get("com_infra", 0) for o in ok),
            "sem_infra": sum((o.get("cobertura") or {}).get("sem_infra", 0) for o in ok),
            "padrao": PADRAO_INFRA,
        },
        "origens": origens,
    }

    fechar = con is None
    con = con or banco()
    try:
        radios = sorted({a["radio"] for a in amostras})
        sid = survey_criar(con, sv["nome"], sv["inicio"], None, radios)
        amostras_gravar(con, sid, amostras)
        survey_fechar(con, sid, sv["fim"], survey_resumo(amostras),
                      n_moveis=len(radios), n_fixos=0,
                      intervalo_efetivo_s=_mm_intervalo_real(amostras))
        log.info(f"[meshmapper] survey {sid}: {len(ok)}/{len(origens)} "
                 f"arquivo(s), {len(amostras)} amostras, {len(radios)} movel(is)")
        return sid, sv, amostras, peers, origens
    finally:
        if fechar: con.close()


def _mm_intervalo_real(amostras):
    """Mediana do intervalo entre pontos. O MeshMapper declara o pedido
    em `interval`; o que descreve a resolucao e o que aconteceu."""
    ts = sorted(a["ts"] for a in amostras if a.get("ts"))
    difs = [b - a for a, b in zip(ts, ts[1:]) if b > a]
    if not difs: return None
    difs.sort()
    return round(difs[len(difs) // 2], 2)


def excel_do_meshmapper(sv, amostras, peers, cfg=None):
    """Excel da captura do MeshMapper: quatro abas.

    Nao reaproveita o workbook semanal porque aquele e um relatorio de
    PERIODO, montado a partir do Prometheus. Aqui a unidade e uma
    captura, e ha uma aba que so existe com este dado: a de vizinhos.
    """
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter as _gcl
    import io as _io

    movel = sv.get("movel") or "movel"
    wb = Workbook(); wb.remove(wb.active)

    def dt(ts):
        return (datetime.utcfromtimestamp(ts).strftime("%d/%m/%Y %H:%M:%S")
                if ts else "")

    # ── 0. Origens (so quando veio de mais de um arquivo) ──
    origens = sv.get("origens") or []
    if origens:
        ws = wb.create_sheet("Origens")
        _cab(ws, "Arquivos lidos",
             "Inclusive os que falharam — relatório que engole arquivo "
             "ilegível em silêncio é pior que relatório que falta", 7)
        lin = 4
        _th(ws, lin, ["Arquivo", "Situação", "Móvel", "Amostras",
                      "Vizinhos", "Início (UTC)", "Fim (UTC)"],
            [42, 12, 20, 11, 11, 20, 20])
        lin += 1
        for o in origens:
            lin = _td(ws, lin, [
                o.get("arquivo"),
                "ok" if o.get("ok") else "FALHOU",
                o.get("movel") or (o.get("erro") or "")[:40],
                o.get("amostras"), o.get("peers"),
                dt(o.get("inicio")), dt(o.get("fim"))],
                zebra=(lin % 2 == 0))

    # ── 1. Resumo ──
    ws = wb.create_sheet("Resumo")
    _cab(ws, f"Site Survey — {movel}",
         f"Captura do MeshMapper {sv.get('versao_bcc') or ''} · "
         f"{dt(sv.get('inicio'))} a {dt(sv.get('fim'))} UTC", 6)
    lin = 4
    _th(ws, lin, ["Item", "Valor"], [34, 46]); lin += 1
    dur = ((sv["fim"] - sv["inicio"]) / 60.0) if sv.get("fim") and sv.get("inicio") else None
    campos = campos_com_medicao(amostras)
    faltam = [c for c in CAMPOS_KMZ if c not in campos]
    ext = extensao_da_captura(amostras)
    parada = bool(ext) and ext["raio_m"] <= RAIO_PARADO_M
    itens = [
        ("Equipamento móvel", movel),
        # Parado x andando muda o que a captura significa, e por isso
        # abre o resumo: as mesmas 115 linhas sao um trajeto ou sao uma
        # janela de tempo num lugar so, e nao da para ler o resto sem
        # saber qual das duas.
        ("Tipo de captura",
         (f"PARADA — tudo dentro de {ext['raio_m']:g} m. Vale como laudo "
          f"do rádio, não como mapa de área"
          if parada else
          f"em movimento — trajeto de até {ext['raio_m']:g} m do centro")
         if ext else "sem posição"),
        ("Arquivo", sv.get("arquivo") or ""),
        ("Versão do BC Commander", sv.get("versao_bcc") or "n/d"),
        ("Duração (min)", round(dur, 1) if dur else "n/d"),
        ("Arquivos lidos", f"{sum(1 for o in (sv.get('origens') or []) if o.get('ok'))}"
                           f" de {len(sv.get('origens') or [])}"
                           if sv.get("origens") else "1"),
        ("Amostras", len(amostras)),
        ("Intervalo pedido (s)", sv.get("intervalo_s") or "n/d"),
        ("Intervalo real (s)", _mm_intervalo_real(amostras) or "n/d"),
        ("Leituras de vizinho", len(peers)),
        ("Vizinhos distintos", len({p["nome"] for p in peers if p.get("nome")})),
        ("Pontos sem enlace", sum(1 for a in amostras if a.get("sinal") is None)),
        ("Grandezas medidas", ", ".join(campos) or "nenhuma"),
        # Registrar o que NAO veio evita a leitura de "mediu e deu ruim".
        ("Não fornecido pelo MeshMapper", ", ".join(faltam) or "—"),
    ]
    for k, v in itens:
        lin = _td(ws, lin, [k, v], zebra=(lin % 2 == 0))

    # ── Cobertura x serviço entregue ──
    # O bloco que muda a recomendação do laudo. Cobertura boa com enlace
    # ruim nao e falta de radio: e escolha de caminho, e repetidora nova
    # nao resolveria.
    cob = [a["sinal_cob"] for a in amostras if a.get("sinal_cob") is not None]
    dlt = [a["delta_cob"] for a in amostras if a.get("delta_cob") is not None]
    if cob:
        srv = [a["sinal"] for a in amostras if a.get("sinal") is not None]
        req = float(ESCALAS["sinal"]["req"])
        def _fora(v): return round(sum(1 for x in v if x < req) * 100.0 / len(v), 1)
        def _med(v):
            o = sorted(v); m = len(o) // 2
            return round(o[m] if len(o) % 2 else (o[m-1] + o[m]) / 2, 1)
        lin += 1
        ws.cell(lin, 1, "Cobertura disponível x serviço entregue").font = F_TXTB
        lin += 1
        _th(ws, lin, ["O que se mede", "Mediana (dBm)",
                      f"Fora do requisito (> {req:g} dBm)"], [34, 16, 30])
        lin += 1
        lin = _td(ws, lin, ["Cobertura disponível (melhor infra)",
                            _med(cob), f"{_fora(cob)}%"], zebra=True)
        if srv:
            lin = _td(ws, lin, ["Enlace que atendeu", _med(srv),
                                f"{_fora(srv)}%"])
        if dlt:
            lin = _td(ws, lin, ["RSSI disponível e não usado (mediana)",
                                _med(dlt), "—"], zebra=True)
        info = sv.get("cobertura") or {}
        if info.get("sem_infra"):
            # Ponto sem nenhum ERB/ERM visivel caiu para o melhor vizinho
            # qualquer — que pode ser outro caminhao. Dizer isso evita
            # apresentar veiculo de passagem como cobertura da area.
            lin = _td(ws, lin, [
                "Pontos sem infra visível (usou o melhor vizinho)",
                info["sem_infra"], "—"])
        lin = _td(ws, lin, ["Critério de infraestrutura",
                            info.get("padrao", PADRAO_INFRA), "—"], zebra=True)

    lim = sv.get("limiares_mm") or {}
    if lim:
        lin += 1
        ws.cell(lin, 1, "Régua usada pelo próprio MeshMapper").font = F_TXTB
        lin += 1
        _th(ws, lin, ["Limiar", "Valor"], [34, 46]); lin += 1
        for k, v in lim.items():
            lin = _td(ws, lin, [k, v], zebra=(lin % 2 == 0))

    # ── 2. Por grandeza ──
    ws = wb.create_sheet("Por Grandeza")
    _cab(ws, "Indicadores por grandeza", "Requisito Modular Mining", 7)
    lin = 4
    _th(ws, lin, ["Grandeza", "Unid.", "Amostras", "Mediana", "Pior 5%",
                  "Requisito", "Dentro (%)"], [18, 8, 11, 12, 12, 14, 12])
    lin += 1
    resumo = survey_resumo(amostras) or {}
    for campo in CAMPOS_KMZ:
        e = ESCALAS.get(campo) or {}
        r = resumo.get(campo)
        if not r:
            lin = _td(ws, lin, [e.get("rot", campo), e.get("un", ""), 0,
                                "não medido", "", "", ""],
                      zebra=(lin % 2 == 0))
            continue
        op = ">" if e.get("melhor") == "alto" else "<"
        lin = _td(ws, lin, [e.get("rot", campo), e.get("un", ""),
                            r.get("n"), r.get("mediana"), r.get("p05"),
                            f"{op} {e.get('req')}", r.get("pct_ok")],
                  zebra=(lin % 2 == 0))

    # ── 3. Amostras ──
    ws = wb.create_sheet("Amostras")
    _cab(ws, "Trajeto medido", f"{len(amostras)} pontos", 14)
    lin = 4
    _th(ws, lin, ["#", "Hora (UTC)", "Latitude", "Longitude", "Alt (m)",
                  "Banda", "Canal", "RSSI (dBm)", "SNR (dB)", "Ruído (dBm)",
                  "Custo", "Taxa (Mbps)", "Vizinhos", "Servidor",
                  # As tres ultimas sao a outra pergunta: o que HAVIA
                  # disponivel ali, e quanto disso nao foi usado.
                  "Cobertura (dBm)", "Melhor infra", "Δ não usado (dB)"],
        [6, 19, 12, 12, 9, 10, 8, 11, 10, 12, 10, 12, 10, 22, 14, 22, 15])
    lin += 1
    for i, a in enumerate(sorted(amostras, key=lambda x: x.get("ts") or 0), 1):
        lin = _td(ws, lin, [i, dt(a.get("ts")), a.get("lat"), a.get("lon"),
                            a.get("alt"), a.get("banda"), a.get("canal"),
                            a.get("sinal"), a.get("snr"), a.get("ruido"),
                            a.get("custo"), a.get("taxa"), a.get("peers"),
                            a.get("servidor"),
                            a.get("sinal_cob"), a.get("servidor_cob"),
                            a.get("delta_cob")], estilo=False)
    ws.freeze_panes = "A5"

    # ── 4. Censo de vizinhos ──
    # Uma linha por VIZINHO, nao por leitura. E a aba que responde "quem
    # fala com este radio e como" — a pergunta de uma captura parada, e
    # tambem util na de trajeto para saber quem apareceu no percurso.
    censo = censo_vizinhos(peers, len(amostras))
    if censo:
        rc = resumo_do_censo(censo)
        ws = wb.create_sheet("Censo de Vizinhos")
        _cab(ws, f"Vizinhança de {movel}",
             f"{rc['vizinhos']} rádios distintos · {rc['infra']} de "
             f"infraestrutura, {rc['moveis']} móveis · {rc['acima_req']} "
             f"acima de {rc['requisito']:g} dBm", 13)
        lin = 4
        _th(ws, lin, ["Vizinho", "Tipo", "RSSI mediano (dBm)",
                      "Pior 10% (dBm)", "Melhor 10% (dBm)", "SNR (dB)",
                      # Presenca separa o radio estavel do que so passou:
                      # -58 dBm em 35% dos pontos e um veiculo passando,
                      # -93 dBm em 100% e um vizinho permanente e fraco.
                      # A mediana sozinha nao distingue os dois.
                      "Presença (%)", "Leituras", f"Dentro do requisito (%)",
                      "Custo mediano", "Banda", "Canal", "Interface"],
            [26, 10, 18, 15, 17, 10, 13, 10, 20, 14, 18, 12, 12])
        lin += 1
        for c in censo:
            lin = _td(ws, lin, [
                c["nome"], "infra" if c["infra"] else "móvel",
                c["sinal"], c["sinal_p10"], c["sinal_p90"], c["snr"],
                c["presenca"], c["leituras"], c["pct_ok"], c["custo"],
                ", ".join(c["bandas"]),
                ", ".join(str(x) for x in c["canais"]),
                ", ".join(c["interfaces"])], zebra=(lin % 2 == 0))
        ws.freeze_panes = "A5"

    # ── 4b. Pegada por repetidora ──
    # Responde "até onde a ERM-28 alcança", que o censo e a cobertura
    # disponível não respondem: o censo agrega por vizinho sem lugar, e a
    # cobertura mistura todas as repetidoras num valor por ponto.
    pegada = amostras_por_repetidora(amostras, peers)
    if pegada:
        ws = wb.create_sheet("Por Repetidora")
        req_r = float(ESCALAS["sinal"]["req"])
        _cab(ws, "Alcance medido de cada ERB/ERM",
             f"Posição do veículo, RSSI dele para aquela repetidora — "
             f"da mesma leitura", 8)
        lin = 4
        _th(ws, lin, ["Repetidora", "Pontos", "Mediana (dBm)", "Melhor",
                      "Pior", f"Acima de {req_r:g} dBm (%)", "Bandas",
                      "Equipamentos que a ouviram"],
            [26, 9, 15, 10, 10, 20, 18, 30])
        lin += 1
        for nome_r, am_r in sorted(pegada.items(),
                                   key=lambda kv: -len(kv[1])):
            vs = sorted(a["sinal"] for a in am_r)
            bandas_r = sorted({a["banda"] for a in am_r if a.get("banda")})
            eq = sorted({a["radio"] for a in am_r if a.get("radio")})
            lin = _td(ws, lin, [
                nome_r, len(am_r), vs[len(vs) // 2], vs[-1], vs[0],
                round(sum(1 for v in vs if v > req_r) * 100.0 / len(vs), 1),
                ", ".join(bandas_r),
                ", ".join(eq[:4]) + (f" +{len(eq)-4}" if len(eq) > 4 else "")],
                zebra=(lin % 2 == 0))
        ws.freeze_panes = "A5"

    # ── 5. Vizinhos, leitura a leitura ──
    # A aba que so existe com dado do MeshMapper: a sondagem pela BC API
    # devolve o enlace que atendeu, nao a vizinhanca inteira.
    ws = wb.create_sheet("Vizinhos")
    _cab(ws, "Todos os vizinhos visíveis, ponto a ponto",
         "O que a sondagem por API não mostra: quem mais estava ao alcance "
         "em cada posição", 11)
    lin = 4
    _th(ws, lin, ["Ponto", "Hora (UTC)", "Vizinho", "IP", "MAC", "Interface",
                  "Banda", "Canal", "RSSI (dBm)", "SNR (dB)", "Custo"],
        [7, 19, 26, 15, 19, 10, 10, 8, 11, 10, 11])
    lin += 1
    for p in sorted(peers, key=lambda x: (x.get("ponto") or 0,
                                          -(x.get("snr") or -999))):
        lin = _td(ws, lin, [p.get("ponto"), dt(p.get("ts")), p.get("nome"),
                            p.get("ip"), p.get("mac"), p.get("wlan"),
                            p.get("banda"), p.get("canal"), p.get("sinal"),
                            p.get("snr"), p.get("custo")], estilo=False)
    ws.freeze_panes = "A5"

    buf = _io.BytesIO(); wb.save(buf)
    quando = datetime.utcfromtimestamp(sv["inicio"]) if sv.get("inicio") else datetime.now()
    nome = f"Survey_{_slug_arquivo(movel)}_{quando:%Y%m%d_%H%M}.xlsx"
    return buf.getvalue(), nome


def _slug_arquivo(txt):
    """Nome de equipamento vira parte de nome de arquivo: barra e dois
    pontos derrubariam a gravacao no Windows."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(txt or "")).strip("-") or "survey"


# Quem conta como INFRAESTRUTURA. Repetidora fixa (ERB) e movel (ERM)
# caracterizam a cobertura da area; um caminhao encostado, nao — ele tem
# sinal otimo e vai embora no minuto seguinte.
PADRAO_INFRA = r"^\s*(ERB|ERM)\b"


def cobertura_disponivel(amostras, peers, padrao=None):
    """Anexa a cada amostra o MELHOR vizinho de infraestrutura do ponto.

    Duas perguntas diferentes vivem no mesmo arquivo:

      "existe sinal servivel aqui?"   -> o melhor vizinho de infra
      "a aplicacao funcionou aqui?"   -> o enlace que o InstaMesh usou

    Medido no arquivo do cliente, as duas divergem muito: o enlace
    entregue tinha mediana -88 dBm e 81% fora do requisito, enquanto o
    melhor vizinho de infra dava -66 dBm e 100% dentro. Reportar so a
    primeira leva a conclusao errada de que falta radio na area.

    Os campos anexados sao `sinal_cob`, `snr_cob`, `servidor_cob` e
    `delta_cob` (quanto de RSSI ficou na mesa). Devolve um resumo com a
    contagem, para o relatorio poder dizer com que criterio contou.
    """
    rx = re.compile(padrao or PADRAO_INFRA, re.I)
    por_ponto = {}
    for p in peers:
        if p.get("sinal") is None: continue
        nome = (p.get("nome") or "")
        por_ponto.setdefault(p.get("ponto"), []).append((rx.search(nome) is not None, p))

    n_infra = n_fallback = 0
    for i, a in enumerate(amostras, start=1):
        lista = por_ponto.get(i) or []
        infra = [p for eh, p in lista if eh]
        if infra:
            n_infra += 1
        elif lista:
            # Sem nenhum vizinho de infra visivel: cai para o melhor
            # vizinho qualquer, MARCADO como tal. Silenciar isso faria o
            # relatorio apresentar um caminhao encostado como cobertura.
            infra = [p for _, p in lista]
            n_fallback += 1
        if not infra:
            continue
        melhor = max(infra, key=lambda p: p["sinal"])
        a["sinal_cob"] = melhor["sinal"]
        a["snr_cob"] = melhor.get("snr")
        a["servidor_cob"] = melhor.get("nome")
        a["cob_e_infra"] = bool([p for eh, p in lista if eh])
        if a.get("sinal") is not None:
            # Quanto de RSSI havia disponivel e nao foi usado. E aqui que
            # mora o diagnostico: delta grande com cobertura boa nao e
            # falta de radio, e escolha de caminho.
            a["delta_cob"] = round(melhor["sinal"] - a["sinal"], 1)
    return {"com_infra": n_infra, "sem_infra": n_fallback,
            "padrao": padrao or PADRAO_INFRA}


# ══════════════════════════════════════════════════════════════
# CAPTURA DE UM PONTO FIXO — o laudo da repetidora
# ══════════════════════════════════════════════════════════════
# O MeshMapper rodando ligado a uma repetidora produz um arquivo com a
# MESMA estrutura do arquivo de um veiculo — conferido campo a campo na
# captura da ERM-12: `points`, `wlanPeers`, `traceInfo`, nada a mais.
#
# A diferenca esta no que ele significa. O arquivo do veiculo e um
# TRAJETO: cada ponto e um lugar. O arquivo da repetidora e uma JANELA DE
# TEMPO num lugar so: 115 pontos empilhados dentro de 0,4 m.
#
# Duas consequencias praticas:
#
#   • Rastro de calor nao se aplica. Sairia uma mancha de um pixel,
#     pintada com a escala de area — a leitura errada mais cara possivel,
#     porque PARECE um mapa.
#   • O que o arquivo tem de valioso e a VIZINHANCA: 40 radios distintos,
#     24 deles moveis, vistos em 114 segundos. Quem fala com esta
#     repetidora, com que sinal, em que banda, e com que constancia.
#
# O que o arquivo NAO tem, e nenhum ajuste de codigo cria: a POSICAO dos
# vizinhos. Um BreadCrumb reporta do vizinho apenas
#   channel, cost, encap, filtered, frequency, ipaddr, mac, name,
#   rssi, serialNumber, signal
# — conferido nos dois arquivos reais, do veiculo e da repetidora. As
# coordenadas do peer_info.csv sao as de QUEM CAPTUROU, repetidas para
# cada vizinho. Por isso o censo e uma tabela, nao um mapa.

# Raio abaixo do qual a captura conta como parada. O GPS de um radio
# imovel oscila poucos metros; 15 m cobre a oscilacao com folga e fica
# duas ordens de grandeza abaixo de qualquer trajeto. Medido: a captura
# da ERM-12 deu raio 0,4 m em 115 pontos; a do CA-1006 rodando, 890 m.
RAIO_PARADO_M = 15.0


def extensao_da_captura(amostras):
    """Ate onde a captura foi: {raio_m, lat, lon, n} do centro, ou None.

    `raio_m` e a MAIOR distancia de um ponto ao centro, nao o desvio
    padrao: um trajeto de ida e volta tem desvio pequeno e mesmo assim
    cobriu quilometros.
    """
    pts = [(a["lat"], a["lon"]) for a in amostras
           if a.get("lat") is not None and a.get("lon") is not None]
    if not pts:
        return None
    la = sum(p[0] for p in pts) / len(pts)
    lo = sum(p[1] for p in pts) / len(pts)
    return {"raio_m": round(max(_dist_m(la, lo, p[0], p[1]) for p in pts), 1),
            "lat": la, "lon": lo, "n": len(pts)}


def captura_parada(amostras, limite_m=None):
    """A captura saiu de um ponto fixo? Decide qual produto faz sentido."""
    ext = extensao_da_captura(amostras)
    if not ext:
        return False
    return ext["raio_m"] <= (RAIO_PARADO_M if limite_m is None else limite_m)


def censo_vizinhos(peers, n_pontos=None, padrao=None):
    """Um registro por vizinho, juntando todas as leituras dele.

    Ordenado do sinal mais forte para o mais fraco. Cada registro traz o
    que decide instalacao: quem e, em que banda, quao forte, e — o que
    uma media esconderia — com que CONSTANCIA apareceu.

    `presenca` e a fracao dos pontos da captura em que o vizinho estava
    visivel. Separa o radio estavel do intermitente: na ERM-12, o CA-1027
    aparece em 100% dos pontos a -93 dBm e o CA-1022 em 35% a -58 dBm.
    Os dois sao verdade, e significam coisas opostas — um esta sempre la
    e fraco, o outro passou perto e foi embora. Mediana sozinha nao
    distingue os dois casos.

    Vizinho sem NENHUMA leitura valida entra com sinal None, nunca 0: o
    MeshMapper usa 0 para "conheco este vizinho mas ainda nao medi".
    """
    rx = re.compile(padrao or PADRAO_INFRA, re.I)
    por_nome = {}
    for p in peers:
        chave = (p.get("nome") or p.get("serie") or p.get("mac") or "?")
        por_nome.setdefault(chave, []).append(p)

    total = n_pontos or len({p.get("ponto") for p in peers if p.get("ponto")})
    req = float(ESCALAS["sinal"]["req"])
    out = []
    for nome, lst in por_nome.items():
        sig = sorted(p["sinal"] for p in lst if p.get("sinal") is not None)
        snr = sorted(p["snr"] for p in lst if p.get("snr") is not None)
        cst = sorted(p["custo"] for p in lst if p.get("custo") is not None)
        pontos = {p.get("ponto") for p in lst if p.get("ponto")}
        ts = [p["ts"] for p in lst if p.get("ts")]
        reg = {
            "nome": nome,
            "infra": bool(rx.search(nome)),
            "ip": next((p.get("ip") for p in lst if p.get("ip")), None),
            "mac": next((p.get("mac") for p in lst if p.get("mac")), None),
            "serie": next((p.get("serie") for p in lst if p.get("serie")), None),
            "leituras": len(lst),
            "pontos": len(pontos),
            "presenca": (round(len(pontos) * 100.0 / total, 1)
                         if total else None),
            "bandas": sorted({p["banda"] for p in lst if p.get("banda")}),
            "canais": sorted({int(p["canal"]) for p in lst
                              if p.get("canal") is not None}),
            "interfaces": sorted({p["wlan"] for p in lst if p.get("wlan")}),
            "sinal": _mm_mediana(sig), "sinal_p10": _mm_pct(sig, .10),
            "sinal_p90": _mm_pct(sig, .90),
            "sinal_min": sig[0] if sig else None,
            "sinal_max": sig[-1] if sig else None,
            "snr": _mm_mediana(snr),
            "custo": _mm_mediana(cst),
            "n_sinal": len(sig),
            # Sem leitura valida nao ha percentual: 0% se leria como
            # "medi e reprovou".
            "pct_ok": (round(sum(1 for v in sig if v > req) * 100.0 / len(sig), 1)
                       if sig else None),
            "inicio": min(ts) if ts else None,
            "fim": max(ts) if ts else None,
        }
        out.append(reg)
    # Sem sinal vai para o fim: e ausencia de medida, nao medida pessima.
    out.sort(key=lambda r: (r["sinal"] is None, -(r["sinal"] or 0)))
    return out


def _mm_mediana(ordenados):
    n = len(ordenados)
    if not n: return None
    m = (ordenados[n // 2] if n % 2
         else (ordenados[n // 2 - 1] + ordenados[n // 2]) / 2.0)
    return round(m, 1)


def _mm_pct(ordenados, q):
    if not ordenados: return None
    return round(ordenados[min(len(ordenados) - 1,
                               max(0, int(q * (len(ordenados) - 1))))], 1)


def amostras_por_repetidora(amostras, peers, padrao=None, min_pontos=8):
    """A pegada MEDIDA de cada ERB/ERM: onde ela foi ouvida, e com quanto.

    Vira `{nome_da_repetidora: [amostras]}`, onde cada amostra tem a
    posição do VEÍCULO e, em `sinal`, o RSSI daquele veículo para aquela
    repetidora. Posição e sinal saíram da mesma leitura, no mesmo
    instante — nada é estimado, nada é cruzado por distância.

    É o que responde "até onde a ERM-28 alcança de verdade?", que a
    camada de cobertura disponível não responde: ela mistura todas as
    repetidoras num valor só (o melhor de cada ponto).

    A economia aqui é grande e não é óbvia: uma única leitura de um
    veículo traz o sinal para TODAS as repetidoras que ele ouve — 18 na
    mediana, medido no trajeto do CA-1006. Uma passagem de um caminhão
    alimenta 18 mapas de cobertura ao mesmo tempo.

    `min_pontos` descarta a repetidora ouvida em meia dúzia de posições:
    com poucos pontos o rastro vira bolha solta, que se lê como "medi
    esta área" quando o certo é "passei perto uma vez".
    """
    rx = re.compile(padrao or PADRAO_INFRA, re.I)
    # Posição e hora vêm da amostra do ponto, não do registro do vizinho:
    # no par de CSVs o vizinho repete a coordenada de quem capturou, e
    # depender dela ligaria o dado à fonte errada se o formato mudar.
    por_ponto = {}
    for i, a in enumerate(amostras, start=1):
        if a.get("lat") is None or a.get("lon") is None: continue
        por_ponto[i] = a

    out = {}
    for p in peers:
        if p.get("sinal") is None: continue
        nome = (p.get("nome") or "").strip()
        if not nome or not rx.search(nome): continue
        a = por_ponto.get(p.get("ponto"))
        if a is None: continue
        # Um vizinho aparece uma vez por RÁDIO (2,4 e 5,8 GHz são dois
        # enlaces com a mesma repetidora). Fica o melhor do ponto: é o
        # que ela consegue entregar ali, e somar os dois seria contar o
        # mesmo equipamento duas vezes.
        ant = out.setdefault(nome, {}).get(p["ponto"])
        if ant is not None and ant["sinal"] >= p["sinal"]:
            continue
        out[nome][p["ponto"]] = {
            "radio": a.get("radio"), "ts": a.get("ts"),
            "lat": a["lat"], "lon": a["lon"], "alt": a.get("alt"),
            "sinal": p["sinal"], "snr": p.get("snr"),
            "ruido": p.get("ruido"), "custo": p.get("custo"),
            "banda": p.get("banda"), "canal": p.get("canal"),
            "servidor": nome, "fonte": a.get("fonte"),
            # Marcado para ninguém confundir com o enlace que atendeu: é
            # o que ESTA repetidora oferecia no ponto, tenha ela sido
            # usada ou não.
            "repetidora": nome,
        }

    return {nome: [d[k] for k in sorted(d)]
            for nome, d in sorted(out.items())
            if len(d) >= min_pontos}


def sitios_parados(amostras, peers, limite_m=None):
    """As capturas PARADAS que ha neste conjunto, prontas para o mapa.

    Trabalha por equipamento que capturou, nao por arquivo: uma campanha
    pode misturar veiculos andando e repetidoras paradas no mesmo lote, e
    cada um vira o produto que faz sentido para ele. O veiculo alimenta o
    rastro de calor; a repetidora, o censo e o pino.

    Devolve a lista no formato que `gerar_kml_pontos_fixos()` espera.
    """
    por_radio = {}
    for a in amostras:
        if a.get("lat") is None or a.get("lon") is None: continue
        por_radio.setdefault(a.get("radio"), []).append(a)

    sitios = []
    for nome, am in por_radio.items():
        if not captura_parada(am, limite_m):
            continue
        ext = extensao_da_captura(am)
        # Peers da MESMA captura. Sem este filtro, juntar dois arquivos
        # daria a cada repetidora a vizinhanca da outra.
        pr = [p for p in peers if p.get("movel") == nome]
        if not pr:
            continue
        censo = censo_vizinhos(pr, len(am))
        ts = [a["ts"] for a in am if a.get("ts")]
        sitios.append({
            "nome": nome, "lat": ext["lat"], "lon": ext["lon"],
            "raio_m": ext["raio_m"], "pontos": len(am),
            "censo": censo, "resumo": resumo_do_censo(censo),
            "inicio": min(ts) if ts else None,
            "fim": max(ts) if ts else None,
        })
    sitios.sort(key=lambda s: s["nome"] or "")
    return sitios


def resumo_do_censo(censo):
    """Os numeros de capa do laudo da repetidora."""
    com = [c for c in censo if c["sinal"] is not None]
    infra = [c for c in com if c["infra"]]
    movel = [c for c in com if not c["infra"]]
    req = float(ESCALAS["sinal"]["req"])
    forte = [c for c in com if c["sinal"] > req]
    return {
        "vizinhos": len(censo),
        "com_leitura": len(com),
        "infra": len(infra),
        "moveis": len(movel),
        "acima_req": len(forte),
        "abaixo_req": len(com) - len(forte),
        "requisito": req,
        # Constantes = presentes em todos os pontos. Sao os que sustentam
        # a malha; os intermitentes passaram.
        "constantes": sum(1 for c in com if (c["presenca"] or 0) >= 99.0),
        "bandas": sorted({b for c in censo for b in c["bandas"]}),
    }


def campos_com_medicao(amostras, candidatos=None):
    """Quais grandezas tem medicao de verdade nestas amostras.

    O MeshMapper nao traz latencia, perda nem interferencia. Gerar aba
    para elas produziria pagina vazia com escala e requisito, que se le
    como "medi e deu tudo fora" em vez de "nao medi".
    """
    campos = list(candidatos or CAMPOS_KMZ)
    return [c for c in campos
            if any(a.get(c) is not None for a in amostras)]


def _mm_ts(txt):
    """'2026-09-10 15:59:27 UTC' → epoch. O sufixo UTC é literal e o
    strptime nao o interpreta como fuso: tratar como hora local mudaria o
    trajeto de lugar no tempo."""
    if not txt: return None
    s = str(txt).strip().strip('"').replace(" UTC", "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    return None


# ══════════════════════════════════════════════════════════════
# PERSISTÊNCIA DOS SURVEYS (SQLite ao lado do executável)
# ══════════════════════════════════════════════════════════════
# Sem persistência, cada survey morre quando o processo reinicia — e a
# comparação com a semana anterior (colunas "Semana anterior" e
# "Tendência" do PPT) fica impossível. Guarda-se tudo por padrão; a
# exclusão é manual, pela página de histórico.
import sqlite3

BANCO_SURVEY = "surveys.db"

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS survey (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    nome         TEXT NOT NULL,
    inicio       REAL NOT NULL,
    fim          REAL,
    intervalo_s  INTEGER,
    radios       TEXT,          -- JSON: nomes selecionados
    alcance      REAL,          -- raio inicial das manchas, em metros
    n_amostras   INTEGER,       -- contadores fechados junto com o survey,
    n_moveis     INTEGER,       -- para o histórico não reprocessar as
    n_fixos      INTEGER,       -- amostras a cada abertura da página
    intervalo_efetivo_s REAL,   -- resolução que DE FATO ocorreu; o pedido
                                -- é só a intenção, e o ciclo pode estourá-lo
    resumo       TEXT,          -- JSON: indicadores agregados
    calibracao   TEXT,          -- JSON: {banda: {A, n, rms, amostras, alcance_m}}
    criado_em    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS amostra (
    survey_id  INTEGER NOT NULL REFERENCES survey(id) ON DELETE CASCADE,
    radio      TEXT NOT NULL,
    ts         REAL NOT NULL,
    lat        REAL, lon   REAL, vel   REAL,
    snr        REAL, sinal REAL, ruido REAL,
    rtt        REAL, perda REAL,
    custo      REAL, taxa  REAL, vazao REAL,
    peers      INTEGER, sats INTEGER, hdop REAL,
    banda      TEXT,
    fonte      TEXT,          -- 'direto' (consultado agora) | 'cache' (exporter)
    servidor   TEXT,          -- BC que atendeu neste ponto (melhor enlace)
    interf     REAL,          -- % de antena ocupada por transmissor alheio
    canal      INTEGER        -- canal do rádio que atendeu
);
CREATE INDEX IF NOT EXISTS ix_amostra_survey ON amostra(survey_id);
CREATE INDEX IF NOT EXISTS ix_amostra_radio  ON amostra(survey_id, radio);
CREATE TABLE IF NOT EXISTS medicao_manual (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    survey_id INTEGER REFERENCES survey(id) ON DELETE CASCADE,
    tipo      TEXT NOT NULL,        -- 'iperf' | 'trace'
    data      TEXT,
    local     TEXT,
    lat       REAL, lon REAL,
    banda     TEXT,
    mbps      REAL,                 -- iperf
    origem    TEXT, destino TEXT,   -- trace
    saltos    INTEGER, custo_total REAL, gargalo TEXT,
    obs       TEXT
);
CREATE INDEX IF NOT EXISTS ix_manual_survey ON medicao_manual(survey_id);
"""


def banco(caminho=None):
    """Conexão SQLite com o esquema garantido.

    `check_same_thread=False` porque a captura roda numa thread e a página
    web noutra; o lock do próprio SQLite dá conta da concorrência aqui,
    que é baixa (uma escrita por ciclo de survey).
    """
    con = sqlite3.connect(caminho or BANCO_SURVEY, check_same_thread=False,
                          timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(_ESQUEMA)
    # Migração de bancos criados por versões anteriores: CREATE TABLE IF NOT
    # EXISTS não acrescenta coluna em tabela que já existe.
    tem = {r["name"] for r in con.execute("PRAGMA table_info(survey)")}
    for col, tipo in (("alcance", "REAL"), ("n_amostras", "INTEGER"),
                      ("n_moveis", "INTEGER"), ("n_fixos", "INTEGER"),
                      ("intervalo_efetivo_s", "REAL")):
        if col not in tem:
            con.execute(f"ALTER TABLE survey ADD COLUMN {col} {tipo}")
    tem_am = {r["name"] for r in con.execute("PRAGMA table_info(amostra)")}
    for col, tipo in (("fonte", "TEXT"), ("servidor", "TEXT"),
                      ("interf", "REAL"), ("canal", "INTEGER"),
                      # Cobertura disponivel no ponto: o melhor vizinho de
                      # infraestrutura, que e outra grandeza do enlace que
                      # atendeu. Ver cobertura_disponivel().
                      ("sinal_cob", "REAL"), ("snr_cob", "REAL"),
                      ("servidor_cob", "TEXT"), ("delta_cob", "REAL")):
        if col not in tem_am:
            con.execute(f"ALTER TABLE amostra ADD COLUMN {col} {tipo}")
    con.commit()
    return con


def survey_criar(con, nome, inicio, intervalo_s, radios, alcance=None):
    cur = con.execute(
        "INSERT INTO survey (nome, inicio, intervalo_s, radios, alcance, "
        "criado_em) VALUES (?,?,?,?,?,?)",
        (nome, inicio, intervalo_s, json.dumps(sorted(radios)), alcance,
         time.time()))
    con.commit()
    return cur.lastrowid


def survey_fechar(con, sid, fim, resumo=None, calibracao=None,
                  n_moveis=None, n_fixos=None, intervalo_efetivo_s=None):
    n_am = con.execute("SELECT COUNT(*) FROM amostra WHERE survey_id=?",
                       (sid,)).fetchone()[0]
    con.execute("UPDATE survey SET fim=?, resumo=?, calibracao=?, "
                "n_amostras=?, n_moveis=?, n_fixos=?, intervalo_efetivo_s=? "
                "WHERE id=?",
                (fim, json.dumps(resumo or {}), json.dumps(calibracao or {}),
                 n_am, n_moveis, n_fixos, intervalo_efetivo_s, sid))
    con.commit()


def amostras_gravar(con, sid, linhas):
    """`linhas` = lista de dicts. Campo ausente vira NULL, nunca 0 — zero
    seria lido como 'medido e deu zero'."""
    if not linhas: return 0
    cols = ["radio","ts","lat","lon","vel","snr","sinal","ruido","rtt","perda",
            "custo","taxa","vazao","peers","sats","hdop","banda","fonte",
            "servidor","interf","canal",
            "sinal_cob","snr_cob","servidor_cob","delta_cob"]
    con.executemany(
        f"INSERT INTO amostra (survey_id,{','.join(cols)}) "
        f"VALUES (?,{','.join('?'*len(cols))})",
        [tuple([sid] + [l.get(c) for c in cols]) for l in linhas])
    con.commit()
    return len(linhas)


def survey_listar(con, limite=100):
    """Histórico. Usa os contadores gravados no fechamento quando existem —
    é para isso que eles são guardados; recontar as amostras a cada abertura
    da página seria caro com surveys de 8 h.

    O alias da subconsulta tem nome PRÓPRIO (`_conta_amostras`): com
    `s.*` a coluna homônima da tabela vinha junto, o dict ficava com duas
    chaves iguais e a NULL do survey em andamento vencia.
    """
    linhas = []
    for r in con.execute(
            "SELECT s.*, "
            "  (SELECT COUNT(*) FROM amostra a WHERE a.survey_id=s.id) "
            "     AS _conta_amostras, "
            "  (SELECT COUNT(DISTINCT a.radio) FROM amostra a "
            "     WHERE a.survey_id=s.id) AS n_radios "
            "FROM survey s ORDER BY s.inicio DESC LIMIT ?", (limite,)):
        d = dict(r)
        # Gravado no fechamento tem prioridade; survey em andamento cai na
        # contagem ao vivo.
        d["n_amostras"] = d.get("n_amostras") or d.pop("_conta_amostras", 0)
        d.pop("_conta_amostras", None)
        linhas.append(d)
    return linhas


def survey_obter(con, sid):
    r = con.execute("SELECT * FROM survey WHERE id=?", (sid,)).fetchone()
    return dict(r) if r else None


def survey_amostras(con, sid, banda=None):
    q = "SELECT * FROM amostra WHERE survey_id=?"
    p = [sid]
    if banda:
        q += " AND banda=?"; p.append(banda)
    return [dict(r) for r in con.execute(q + " ORDER BY ts", p)]


def survey_excluir(con, sid):
    con.execute("DELETE FROM amostra WHERE survey_id=?", (sid,))
    con.execute("DELETE FROM medicao_manual WHERE survey_id=?", (sid,))
    con.execute("DELETE FROM survey WHERE id=?", (sid,))
    con.commit()


def survey_anterior(con, sid):
    """Survey imediatamente anterior a este, para a coluna de tendência."""
    s = survey_obter(con, sid)
    if not s: return None
    r = con.execute("SELECT * FROM survey WHERE inicio < ? AND fim IS NOT NULL "
                    "ORDER BY inicio DESC LIMIT 1", (s["inicio"],)).fetchone()
    return dict(r) if r else None


def manual_gravar(con, sid, tipo, campos):
    cols = ["data","local","lat","lon","banda","mbps","origem","destino",
            "saltos","custo_total","gargalo","obs"]
    con.execute(
        f"INSERT INTO medicao_manual (survey_id,tipo,{','.join(cols)}) "
        f"VALUES (?,?,{','.join('?'*len(cols))})",
        tuple([sid, tipo] + [campos.get(c) for c in cols]))
    con.commit()


def manual_listar(con, sid, tipo=None):
    q = "SELECT * FROM medicao_manual WHERE survey_id=?"
    p = [sid]
    if tipo: q += " AND tipo=?"; p.append(tipo)
    return [dict(r) for r in con.execute(q + " ORDER BY id", p)]


# ── Indicadores agregados, no padrão Modular Mining ───────────
# RSSI > -75 dBm · SNR > 20 dB · latência < 100 ms · perda < 2 %
REQUISITOS = {
    "sinal": (">", -75.0, "dBm", "RSSI"),
    "snr":   (">",  20.0, "dB",  "SNR"),
    "rtt":   ("<", 100.0, "ms",  "Latência"),
    "perda": ("<",   2.0, "%",   "Perda"),
}


def survey_resumo(amostras):
    """Percentual de amostras dentro de cada requisito, mais estatística.

    Campo sem nenhuma amostra devolve None em vez de 0 % — não medimos,
    não é 0 % de aprovação.
    """
    out = {"amostras": len(amostras),
           "radios": len({a["radio"] for a in amostras})}
    # Os cinco requisitos contratuais MAIS a cobertura disponivel. Ela
    # nao e requisito do cliente — e o mesmo RSSI lido de outra fonte —,
    # mas sem ela no resumo o relatorio so sabe dizer como foi o enlace
    # entregue, e nao se havia sinal servivel no lugar.
    alvos = dict(REQUISITOS)
    if any(a.get("sinal_cob") is not None for a in amostras):
        e = ESCALAS["sinal_cob"]
        alvos["sinal_cob"] = (">", float(e["req"]), e["un"], e["rot"])
    for campo, (op, lim, un, rot) in alvos.items():
        vs = [a[campo] for a in amostras if a.get(campo) is not None]
        if not vs:
            out[campo] = None
            continue
        dentro = sum(1 for v in vs if (v > lim if op == ">" else v < lim))
        vs_ord = sorted(vs)
        out[campo] = {
            "n": len(vs), "pct_ok": round(dentro / len(vs) * 100, 1),
            "med": round(sum(vs) / len(vs), 2),
            "min": round(vs_ord[0], 2), "max": round(vs_ord[-1], 2),
            "p05": round(vs_ord[int(len(vs_ord) * .05)], 2),
            "limite": lim, "unidade": un, "rotulo": rot,
        }
    for campo in ("vazao", "taxa", "custo"):
        vs = [a[campo] for a in amostras if a.get(campo) is not None]
        out[campo] = round(sum(vs) / len(vs), 2) if vs else None
    return out


# ══════════════════════════════════════════════════════════════
# ANÁLISE DE SURVEY — servidor, margem, grade e zonas-problema
#
# Percentual de aprovação diz QUANTO está ruim; nada disso diz ONDE nem o
# que fazer. Um survey de consultoria entrega a lista de zonas com a
# recomendação, e é isso que estas funções produzem.
# ══════════════════════════════════════════════════════════════

# Folga até o limite abaixo da qual o ponto passa, mas por pouco. -73 dBm
# aprova contra -75 e cai na primeira chuva ou quando o caminhão vira a
# carroceria para a antena — chamar isso de "OK" engana a operação.
MARGEM_MARGINAL = {"sinal": 5.0, "snr": 5.0, "rtt": 25.0, "perda": 0.5,
                   "interf": 5.0}


def limite_de(campo):
    """(op, limite, unidade, rótulo) de qualquer grandeza analisável.

    REQUISITOS guarda os CINCO da Modular Mining e nada mais — pôr
    interferência lá faria o resumo reportá-la como exigência do cliente,
    o que ela não é. Grandezas que só têm escala (ESCALAS) caem aqui e
    são analisáveis do mesmo jeito, sem virar requisito contratual.
    """
    if campo in REQUISITOS:
        return REQUISITOS[campo]
    e = ESCALAS.get(campo)
    if not e or e.get("req") is None:
        return None
    return ((">" if e["melhor"] == "alto" else "<"),
            float(e["req"]), e["un"], e["rot"])


def margem_requisito(valor, campo):
    """Folga com sinal: positiva = dentro, negativa = fora.

    Devolve (margem, classe) com classe em 'ok' | 'marginal' | 'fora'.
    Valor ausente devolve (None, None) — não medimos, não reprova.
    """
    lim_ = limite_de(campo)
    if valor is None or lim_ is None:
        return None, None
    op, lim, _, _ = lim_
    folga = (valor - lim) if op == ">" else (lim - valor)
    if folga < 0:
        return round(folga, 2), "fora"
    limiar = MARGEM_MARGINAL.get(campo, 0.0)
    return round(folga, 2), ("marginal" if folga < limiar else "ok")


def analisar_servidores(amostras):
    """Quem serviu o quê, e onde a malha troca de servidor.

    Handover é troca de BC servidor entre amostras consecutivas do mesmo
    rádio. Ping-pong é A→B→A numa janela curta: sintoma de dois BCs
    disputando a mesma área, e é problema de projeto, não de rádio.
    """
    por_radio = {}
    for a in amostras:
        if a.get("servidor"):
            por_radio.setdefault(a["radio"], []).append(a)

    cobertura, handovers, pingpong = {}, [], []
    for radio, pts in por_radio.items():
        pts = sorted(pts, key=lambda x: x.get("ts") or 0)
        for a in pts:
            cobertura[a["servidor"]] = cobertura.get(a["servidor"], 0) + 1
        for i in range(1, len(pts)):
            if pts[i]["servidor"] == pts[i-1]["servidor"]:
                continue
            handovers.append({
                "radio": radio, "de": pts[i-1]["servidor"],
                "para": pts[i]["servidor"], "ts": pts[i].get("ts"),
                "lat": pts[i].get("lat"), "lon": pts[i].get("lon")})
            # A→B→A: compara com o servidor de duas trocas atrás.
            if (i >= 2 and pts[i]["servidor"] == pts[i-2]["servidor"]
                    and pts[i-1]["servidor"] != pts[i]["servidor"]):
                pingpong.append({
                    "radio": radio, "entre": sorted({pts[i]["servidor"],
                                                     pts[i-1]["servidor"]}),
                    "lat": pts[i].get("lat"), "lon": pts[i].get("lon"),
                    "ts": pts[i].get("ts")})
    total = sum(cobertura.values()) or 1
    return {
        "cobertura": {k: {"amostras": v, "pct": round(100.0*v/total, 1)}
                      for k, v in sorted(cobertura.items(),
                                         key=lambda kv: -kv[1])},
        "handovers": handovers,
        "n_handovers": len(handovers),
        "pingpong": pingpong,
        "n_pingpong": len(pingpong),
        "radios_sem_servidor": sorted(
            {a["radio"] for a in amostras if not a.get("servidor")}),
    }


def agregar_em_grade(amostras, campo="sinal", grade_m=50.0):
    """Mediana por célula de grade, em vez do ponto cru.

    Duas razões, ambas medidas neste banco:
      1. equipamento PARADO despeja dezenas de amostras no mesmo lugar (a
         fila da britagem vira a média da cava);
      2. leitura instantânea varia ±5-10 dB por multipercurso, então dois
         pontos vizinhos caem em lados opostos do requisito sem que a
         cobertura ali seja diferente.

    A mediana da célula resolve os dois. Devolve lista de células com
    valor, contagem, servidor dominante e classe de margem.
    """
    if grade_m <= 0:
        raise ValueError("grade_m tem de ser positivo")
    graus_lat = grade_m / 111_320.0
    celulas = {}
    for a in amostras:
        v = a.get(campo)
        if v is None or a.get("lat") is None or a.get("lon") is None:
            continue
        # O passo em longitude encolhe por cos(lat), senão a célula fica
        # retangular no terreno e a agregação vira anisotrópica.
        graus_lon = graus_lat / max(0.1, math.cos(math.radians(a["lat"])))
        ch = (int(a["lat"] // graus_lat), int(a["lon"] // graus_lon))
        celulas.setdefault(ch, []).append(a)

    saida = []
    for (ci, cj), pts in celulas.items():
        vals = sorted(p[campo] for p in pts)
        n = len(vals)
        mediana = (vals[n//2] if n % 2 else (vals[n//2 - 1] + vals[n//2]) / 2)
        servs = {}
        for p in pts:
            if p.get("servidor"):
                servs[p["servidor"]] = servs.get(p["servidor"], 0) + 1
        dom = max(servs.items(), key=lambda kv: kv[1])[0] if servs else None
        folga, classe = margem_requisito(mediana, campo)
        saida.append({
            "i": ci, "j": cj,
            "lat": sum(p["lat"] for p in pts) / n,
            "lon": sum(p["lon"] for p in pts) / n,
            "valor": round(mediana, 2), "n": n,
            "pior": round(vals[0] if (limite_de(campo) or (">",))[0] == ">"
                          else vals[-1], 2),
            "servidor": dom,
            "radios": sorted({p["radio"] for p in pts}),
            "margem": folga, "classe": classe})
    return sorted(saida, key=lambda c: (c["i"], c["j"]))


def _vizinhas(celulas):
    """Índice de adjacência 8-conectada entre células da grade."""
    ix = {(c["i"], c["j"]): c for c in celulas}
    viz = {}
    for (i, j), c in ix.items():
        viz[(i, j)] = [ix[(i+di, j+dj)]
                       for di in (-1, 0, 1) for dj in (-1, 0, 1)
                       if (di or dj) and (i+di, j+dj) in ix]
    return viz


def espacamento_tipico(amostras):
    """Distância mediana entre amostras consecutivas do mesmo rádio.

    É o que define a menor grade que faz sentido: a 10 s e 40 km/h os
    pontos ficam a ~110 m um do outro.
    """
    por_radio = {}
    for a in amostras:
        if a.get("lat") is None or a.get("lon") is None: continue
        por_radio.setdefault(a["radio"], []).append(a)
    ds = []
    for pts in por_radio.values():
        pts = sorted(pts, key=lambda x: x.get("ts") or 0)
        for i in range(1, len(pts)):
            d = _dist_m(pts[i-1]["lat"], pts[i-1]["lon"],
                        pts[i]["lat"], pts[i]["lon"])
            if d > 0: ds.append(d)
    if not ds: return None
    ds.sort()
    return ds[len(ds)//2]


def zonas_problema(amostras, campo="sinal", grade_m=50.0, min_celulas=2,
                   incluir_marginal=False):
    """Agrupa as células reprovadas em zonas contíguas e sugere o que fazer.

    É a entrega que separa "34% dentro do requisito" de um survey de
    verdade: onde, qual a extensão, quem servia, quantos equipamentos
    passaram e onde instalar rádio.

    `min_celulas` descarta célula isolada — uma leitura ruim num ponto é
    ruído, não zona de sombra.

    A grade nunca fica mais fina que o espaçamento real das amostras: se
    ficasse, pontos consecutivos cairiam em células NÃO adjacentes, a
    mancha fragmentaria em pedaços de uma célula e o filtro acima
    descartaria todos — o survey diria "nenhuma zona" com meia cava fora
    do requisito.
    """
    esp = espacamento_tipico(amostras)
    if esp and grade_m < esp:
        log.info(f"[zonas] grade de {grade_m:.0f} m e menor que o "
                 f"espacamento das amostras ({esp:.0f} m); usando "
                 f"{esp:.0f} m para a mancha nao fragmentar")
        grade_m = esp
    celulas = agregar_em_grade(amostras, campo, grade_m)
    ruins = [c for c in celulas
             if c["classe"] == "fora"
             or (incluir_marginal and c["classe"] == "marginal")]
    if not ruins:
        return []

    # Área efetivamente percorrida, para dizer o PESO de cada zona: uma
    # mancha de 265.000 m² pode ser um bolsão ou pode ser a cava inteira,
    # e a recomendação é completamente diferente nos dois casos.
    area_total = max(1, len(celulas)) * grade_m * grade_m
    viz = _vizinhas(ruins)
    vistos, zonas = set(), []
    for c in ruins:
        ch = (c["i"], c["j"])
        if ch in vistos: continue
        # Busca em largura: a zona é o conjunto conectado de células ruins.
        fila, grupo = [c], []
        vistos.add(ch)
        while fila:
            atual = fila.pop()
            grupo.append(atual)
            for v in viz[(atual["i"], atual["j"])]:
                if (v["i"], v["j"]) not in vistos:
                    vistos.add((v["i"], v["j"])); fila.append(v)
        if len(grupo) < min_celulas:
            continue

        op = (limite_de(campo) or (">",))[0]
        pior = (min(grupo, key=lambda x: x["valor"]) if op == ">"
                else max(grupo, key=lambda x: x["valor"]))
        servs = {}
        for g in grupo:
            if g["servidor"]:
                servs[g["servidor"]] = servs.get(g["servidor"], 0) + 1
        dom = max(servs.items(), key=lambda kv: kv[1])[0] if servs else None
        radios = sorted({r for g in grupo for r in g["radios"]})
        lats = [g["lat"] for g in grupo]; lons = [g["lon"] for g in grupo]
        # Extensão real da mancha, não a contagem de células: é o número
        # que a operação entende ("240 m de rampa sem cobertura").
        ext = _dist_m(min(lats), min(lons), max(lats), max(lons))
        area = len(grupo) * grade_m * grade_m
        zonas.append({
            "celulas": len(grupo),
            "area_m2": round(area),
            "pct_area": round(100.0 * area / area_total, 1),
            # Acima disto não é bolsão de sombra, é deficiência geral de
            # cobertura — e aí a ação não é "um rádio aqui".
            "sistemico": (area / area_total) >= 0.35,
            "extensao_m": round(ext),
            "lat": sum(lats)/len(lats), "lon": sum(lons)/len(lons),
            "valor_mediano": round(
                sorted(g["valor"] for g in grupo)[len(grupo)//2], 1),
            "pior_valor": pior["valor"],
            "pior_lat": pior["lat"], "pior_lon": pior["lon"],
            "servidor": dom, "radios": radios, "n_radios": len(radios),
            "amostras": sum(g["n"] for g in grupo),
            # Onde pôr rádio: o ponto pior da zona é o que mais precisa de
            # cobertura. Sugestão de partida para o projeto, não veredito.
            "sugestao_lat": pior["lat"], "sugestao_lon": pior["lon"],
        })
    return sorted(zonas, key=lambda z: -z["area_m2"])


def _milhar(n):
    """1234567 -> '1.234.567'. Só o separador de milhar: um replace cego
    na frase inteira levaria junto as vírgulas do texto e das coordenadas."""
    return f"{int(n):,}".replace(",", ".")


# A acao depende da GRANDEZA. Recomendar "mais um radio" para uma zona de
# interferencia esta errado: adensar a malha nao tira do ar quem esta
# ocupando o canal — e ainda soma mais um transmissor na mesma faixa.
_ACAO_ZONA = {
    "sinal": ("avaliar rádio em",
              "indica densidade de malha insuficiente na região, não um "
              "obstáculo pontual. Recomenda-se replanejamento de cobertura "
              "(mais de um rádio), começando pelo pior ponto em"),
    "snr":   ("avaliar rádio em",
              "cobertura insuficiente ou piso de ruído alto em toda a "
              "região. Verificar o ruído antes de acrescentar rádio — se "
              "for ruído, mais rádio não resolve. Pior ponto em"),
    "interf": ("identificar a fonte e avaliar troca de canal; ponto de "
               "maior ocupação em",
               "ocupação alheia em toda a região: trocar o canal dos rádios "
               "afetados e localizar o emissor (enlace de terceiro, forno, "
               "radar). Acrescentar rádio NÃO resolve — soma mais um "
               "transmissor na mesma faixa. Ponto de maior ocupação em"),
    "rtt":   ("investigar o caminho até o servidor a partir de",
              "latência alta em toda a região: verificar número de saltos, "
              "congestionamento do backhaul e custo dos enlaces. Ponto pior em"),
    "perda": ("investigar o enlace em",
              "perda generalizada: verificar retransmissões, interferência e "
              "saturação antes de mexer na cobertura. Ponto pior em"),
}


def texto_zona(z, campo="sinal"):
    """Uma frase acionável por zona, do jeito que vai para o relatório.

    Zona sistêmica recebe outra recomendação de propósito: sugerir "um
    rádio aqui" para uma mancha que cobre metade da cava seria enganoso —
    o problema é de densidade da malha, não um ponto cego.
    """
    _, lim, un, rot = limite_de(campo)
    serv = f", servida por {z['servidor']}" if z["servidor"] else ""
    cabeca = (f"{_milhar(z['extensao_m'])} m de extensão "
              f"({_milhar(z['area_m2'])} m², {z['pct_area']:g}% da área "
              f"percorrida){serv}. "
              f"{rot}: mediana {z['valor_mediano']:g} {un} "
              f"(limite {lim:g} {un}), pior ponto {z['pior_valor']:g} {un}. "
              f"{z['n_radios']} equipamento(s) afetado(s), "
              f"{_milhar(z['amostras'])} amostras. ")
    pontual, sistemico = _ACAO_ZONA.get(campo, _ACAO_ZONA["sinal"])
    onde = f"{z['sugestao_lat']:.5f}, {z['sugestao_lon']:.5f}."
    if z.get("sistemico"):
        return cabeca + f"Extensão grande demais para ponto cego: {sistemico} {onde}"
    return cabeca + f"Sugestão: {pontual} {onde}"


# ══════════════════════════════════════════════════════════════
# COMPARAÇÃO ENTRE SURVEYS — colunas "Semana anterior" e "Tendência"
# ══════════════════════════════════════════════════════════════

def comparar_surveys(atual, anterior):
    """Delta de cada indicador entre dois resumos de survey.

    Devolve {campo: {agora, antes, delta, direcao}}. `direcao` já leva em
    conta se maior é melhor: 'melhorou' / 'piorou' / 'estável'. Sem o
    anterior, `antes` e `delta` ficam None — e a coluna sai em branco no
    relatório, não com zero.
    """
    out = {}
    ant = anterior or {}
    for campo in ("sinal", "snr", "rtt", "perda"):
        a = (atual or {}).get(campo) or {}
        b = ant.get(campo) or {}
        agora = a.get("pct_ok")
        antes = b.get("pct_ok")
        if agora is None:
            out[campo] = {"agora": None, "antes": antes, "delta": None,
                          "direcao": None, "rotulo": (a or b).get("rotulo", campo)}
            continue
        delta = None if antes is None else round(agora - antes, 1)
        if delta is None:      direcao = None
        elif abs(delta) < 0.5: direcao = "estável"
        else:                  direcao = "melhorou" if delta > 0 else "piorou"
        out[campo] = {"agora": agora, "antes": antes, "delta": delta,
                      "direcao": direcao,
                      "rotulo": a.get("rotulo", campo),
                      "unidade": a.get("unidade", ""),
                      "limite": a.get("limite")}
    for campo in ("amostras", "radios"):
        out[campo] = {"agora": (atual or {}).get(campo),
                      "antes": ant.get(campo)}
    return out


SETA = {"melhorou": "▲", "piorou": "▼", "estável": "=", None: "—"}


# ══════════════════════════════════════════════════════════════
# MEDIÇÕES MANUAIS — iperf e trace
# ══════════════════════════════════════════════════════════════
# Teste de banda não existe na BC API: as tarefas possíveis são só
# REBOOT, INSTALL, SNAPSHOT, ZEROIZE, FCC, CTM, TRACE, CLEAR e KICK.
# O throughput por iperf e o resultado do trace são lançados à mão; o
# programa recebe, guarda e usa nos relatórios.
#
# Sem lançamento, as seções saem com os campos EM BRANCO — nunca com
# número inventado.

COLUNAS_IPERF = ["data", "local", "lat", "lon", "banda", "mbps", "obs"]
COLUNAS_TRACE = ["data", "origem", "destino", "saltos", "custo_total",
                 "gargalo", "obs"]


def template_csv_manual(tipo):
    """CSV modelo, com uma linha de exemplo para o formato ficar claro."""
    import io as _io, csv as _csv
    buf = _io.StringIO()
    w = _csv.writer(buf, delimiter=";")
    if tipo == "iperf":
        w.writerow(COLUNAS_IPERF)
        # Coordenada é opcional; as duas colunas ficam vazias quando não há.
        w.writerow(["2026-05-15", "Praça 3 - nível 820", "-27.7312",
                    "-50.0688", "5.8 GHz", "42.5",
                    "medido do caminhão parado"])
        w.writerow(["2026-05-15", "Rampa principal", "", "",
                    "2.4 GHz", "18.2", ""])
    else:
        w.writerow(COLUNAS_TRACE)
        w.writerow(["2026-05-15", "CA-1010", "CORE-01", "4", "480",
                    "ERM-20", "gargalo no salto 3"])
    return buf.getvalue()


def _num(v):
    if v is None: return None
    s = str(v).strip().replace(",", ".")
    if not s: return None
    try: return float(s)
    except ValueError: return None


def importar_csv_manual(con, sid, tipo, texto):
    """Importa o CSV em lote. Devolve (importadas, erros[])."""
    import io as _io, csv as _csv
    if tipo not in ("iperf", "trace"):
        return 0, [f"tipo desconhecido: {tipo}"]
    amostra = texto[:4096]
    try:
        dialeto = _csv.Sniffer().sniff(amostra, delimiters=";,\t")
    except Exception:
        dialeto = _csv.excel
        dialeto.delimiter = ";"
    leitor = _csv.DictReader(_io.StringIO(texto), dialect=dialeto)
    cols = {(_norm_col(c) if c else ""): c for c in (leitor.fieldnames or [])}
    esperadas = COLUNAS_IPERF if tipo == "iperf" else COLUNAS_TRACE
    faltando = [c for c in ("data",) if _norm_col(c) not in cols]
    if faltando:
        return 0, [f"CSV sem a coluna obrigatória: {', '.join(faltando)}. "
                   f"Cabeçalhos lidos: {leitor.fieldnames}"]

    def pega(linha, nome):
        c = cols.get(_norm_col(nome))
        return (linha.get(c) or "").strip() if c else None

    n, erros = 0, []
    for i, linha in enumerate(leitor, start=2):
        if not any((v or "").strip() for v in linha.values()):
            continue
        campos = {c: pega(linha, c) for c in esperadas}
        for c in ("lat", "lon", "mbps", "custo_total"):
            if c in campos: campos[c] = _num(campos[c])
        if "saltos" in campos:
            s = _num(campos["saltos"])
            campos["saltos"] = int(s) if s is not None else None
        if tipo == "iperf" and campos.get("mbps") is None:
            erros.append(f"linha {i}: sem throughput (Mbps) — ignorada")
            continue
        if tipo == "trace" and not campos.get("destino"):
            erros.append(f"linha {i}: sem destino — ignorada")
            continue
        manual_gravar(con, sid, tipo, campos)
        n += 1
    return n, erros


# ══════════════════════════════════════════════════════════════
# SELEÇÃO DE EQUIPAMENTOS PELA TAG DO RÁDIO
# ══════════════════════════════════════════════════════════════
# A seleção é pelo NOME do BC (a tag do rádio), não por EQMPTID do
# Dispatch: é o nome que o operador de rede reconhece e o único
# identificador que a BC API expõe.

def prefixos_frota(cfg):
    """[(rótulo, prefixo)] a partir do config."""
    bruto = cfg.get("relatorio", "prefixos_frota", fallback="") if cfg else ""
    out = []
    for parte in bruto.split(";"):
        parte = parte.strip()
        if not parte: continue
        if ":" in parte:
            rot, pre = parte.split(":", 1)
        else:
            rot = pre = parte
        pre = pre.strip().upper()
        if pre: out.append((rot.strip() or pre, pre))
    return out


def perfis_frota(cfg):
    """{nome do perfil: [tokens]} — tokens podem ser prefixo ou nome exato."""
    out = {}
    if not cfg or not cfg.has_section("relatorio"): return out
    for chave, valor in cfg.items("relatorio"):
        if not chave.startswith("perfil_"): continue
        toks = [t.strip().upper() for t in valor.split(",") if t.strip()]
        if toks:
            out[chave[len("perfil_"):].replace("_", " ").title()] = toks
    return out


def casar_tags(nomes, tokens):
    """Casa nomes de BC contra tokens (prefixo ou nome exato).

    Devolve (casados, nao_encontrados). Reportar o que NÃO casou importa:
    o usuário cola uma lista de 40 tags e precisa saber quais o sistema
    não achou, em vez de silenciosamente medir 37.
    """
    if not tokens: return [], []
    nomes_u = {n.upper(): n for n in nomes}
    casados, faltando = set(), []
    for t in tokens:
        t = t.strip().upper()
        if not t: continue
        if t in nomes_u:                       # nome exato
            casados.add(nomes_u[t]); continue
        achou = [orig for up, orig in nomes_u.items() if up.startswith(t)]
        if achou: casados.update(achou)
        else:     faltando.append(t)
    return sorted(casados), faltando


def tag_do_nome(nome, prefixos):
    """'ERM-20' -> 'ERM'. Casa o prefixo mais longo primeiro, senão 'ER'
    engoliria 'ERM' e 'ERB'."""
    n = (nome or "").strip().upper()
    for p in sorted(prefixos, key=len, reverse=True):
        if n.startswith(p): return p
    m = re.match(r"([A-Z]+)", n)
    return m.group(1) if m else ""


def perfis_salvos(cfg):
    """{nome: [ips]} da seção [perfis] do config.ini.

    Perfis do usuário ficam em seção própria — os `perfil_*` de
    [relatorio] são os padrões de fábrica, por PREFIXO; estes guardam a
    seleção concreta de IPs que o operador montou.

    O configparser rebaixa o nome das opções, então "Cava Norte" é gravado
    como "cava norte". Devolvemos o nome já capitalizado para exibição, e
    a busca (perfil_ips) é insensível a maiúsculas.
    """
    out = {}
    if cfg and cfg.has_section("perfis"):
        for nome, valor in cfg.items("perfis"):
            ips = [i.strip() for i in (valor or "").split(",") if i.strip()]
            if ips: out[nome.strip().title()] = ips
    return out


def perfil_ips(cfg, nome):
    """Busca o perfil sem depender de maiúsculas."""
    alvo = (nome or "").strip().lower()
    for n, ips in perfis_salvos(cfg).items():
        if n.lower() == alvo: return ips
    return []


def _salvar_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            cfg.write(f)
    except Exception as e:
        log.warning(f"[perfis] não consegui gravar {CONFIG_FILE}: {e}")


def perfil_gravar(cfg, nome, ips):
    if not cfg.has_section("perfis"): cfg.add_section("perfis")
    cfg.set("perfis", nome.strip(), ",".join(sorted(set(ips))))
    _salvar_config(cfg)


def perfil_excluir(cfg, nome):
    if cfg.has_section("perfis"):
        cfg.remove_option("perfis", (nome or "").strip())
        _salvar_config(cfg)


def zip_imagens_survey(sid, banda=None, cfg=None):
    """Regera os PNGs do survey a partir do histórico e devolve um ZIP.

    Regerar em vez de guardar imagem: o modelo de propagação e o estilo do
    mapa mudam, e as amostras são a fonte da verdade.
    """
    import zipfile, tempfile
    con = banco()
    try:
        sv = survey_obter(con, sid)
        if not sv: raise RuntimeError("survey não encontrado")
        am = survey_amostras(con, sid)
        calib = json.loads(sv.get("calibracao") or "{}")
    finally:
        con.close()
    if not am:
        raise RuntimeError("survey sem amostras")

    fixos = _fixos_do_survey(am)

    bandas = [banda] if banda else ["2.4 GHz", "5.8 GHz"]
    tmp = Path(tempfile.mkdtemp())
    todas = {}
    for b in bandas:
        try:
            todas.update(gerar_mapas_survey(sv, am, fixos, str(tmp), banda=b,
                                            cfg=cfg, calib=calib))
        except Exception as e:
            log.warning(f"[survey {sid}] banda {b}: {e}")
    if not todas:
        raise RuntimeError("nenhuma imagem gerada — sem amostras na banda?")

    import io as _io
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for chave, caminho in todas.items():
            z.write(caminho, Path(caminho).name)
    seguro = re.sub(r"[^\w\-]+", "_", sv.get("nome") or f"survey{sid}")[:40]
    return buf.getvalue(), f"Survey_{seguro}.zip"


def _fixos_do_survey(amostras, limiar=0.0003):
    """Quem não se deslocou ~30 m no período. Pelo DADO, não pelo nome:
    um ERM rebocado vira rota, e é isso que interessa no mapa."""
    por_radio = {}
    for a in amostras:
        if a.get("lat") is None: continue
        por_radio.setdefault(a["radio"], []).append(a)
    fixos = {}
    for radio, pts in por_radio.items():
        if len(pts) < 2:
            fixos[radio] = (pts[0]["lat"], pts[0]["lon"]); continue
        desl = max(abs(pts[0]["lat"] - p["lat"]) + abs(pts[0]["lon"] - p["lon"])
                   for p in pts)
        if desl <= limiar:
            fixos[radio] = (pts[0]["lat"], pts[0]["lon"])
    return fixos


# Grandezas que viram KMZ, na ordem em que aparecem no relatório. É esta
# lista que o slide cita ao mandar tirar o print.
# "sinal_cob" vem PRIMEIRO de proposito: numa captura do MeshMapper, a
# aba que abre e a de COBERTURA, nao a do enlace entregue. Cobertura e o
# que dimensiona repetidora; o enlace entregue e o que a aplicacao
# enfrentou. Abrir pela segunda faz o leitor concluir "falta radio" onde
# o problema e outro.
CAMPOS_KMZ = ["sinal_cob", "sinal", "snr", "ruido", "rtt", "perda", "interf"]


def gerar_todos_kmz(sid, cfg=None, campos=None, bandas=None):
    """Um KMZ por grandeza e por banda, num ZIP só.

    Serve o fluxo de montar o slide à mão: abre cada arquivo no Google
    Earth, tira o print e cola na moldura correspondente. Por isso vai
    junto um LEIA-ME dizendo qual arquivo vai em qual slide.

    Grandeza sem nenhuma amostra na banda é PULADA, com o motivo no
    relatório — arquivo vazio no zip só faria perder tempo abrindo.
    """
    import zipfile, io as _io

    con = banco()
    try:
        sv = survey_obter(con, sid)
        if not sv: raise RuntimeError("survey não encontrado")
        am = survey_amostras(con, sid)
        try:    mn = manual_listar(con, sid)
        except Exception: mn = []
    finally:
        con.close()
    if not am:
        raise RuntimeError("survey sem amostras")

    fixos = _fixos_do_survey(am)
    campos = campos or CAMPOS_KMZ
    if bandas is None:
        bandas = sorted({_norm_banda(a.get("banda")) for a in am
                         if a.get("banda")}) or [None]

    campos = [c for c in campos if c in FAIXAS_KML]
    buf = _io.BytesIO()
    gerados, pulados = [], []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # UM arquivo por banda, com todas as grandezas em abas dentro. Um
        # arquivo por grandeza obrigava a fechar e reabrir o Earth a cada
        # print; com abas, é um clique no painel de camadas.
        for banda in bandas:
            try:
                dados, nome = gerar_kml_survey(
                    sv, am, fixos, mn, cfg=cfg, campos=campos, banda=banda)
            except Exception as e:
                pulados.append((None, banda, str(e)))
                continue
            z.writestr(nome, dados)
            gerados.append((campos, banda, nome))
        z.writestr("LEIA-ME.txt", _leiame_kmz(sv, gerados, pulados))

    seguro = re.sub(r"[^\w\-]+", "_", sv.get("nome") or f"survey{sid}")[:40]
    log.info(f"[kmz] {len(gerados)} arquivo(s) gerado(s), "
             f"{len(pulados)} pulado(s)")
    if not gerados:
        raise RuntimeError("nenhum KMZ gerado — sem amostra georreferenciada")
    return buf.getvalue(), f"KMZ_{seguro}.zip"


def _leiame_kmz(sv, gerados, pulados):
    """Qual arquivo vai em qual slide. Sem isto, seis KMZ na pasta viram
    adivinhação na hora de montar o deck."""
    quando = datetime.fromtimestamp(sv.get("inicio") or time.time())
    L = ["KMZ do survey — " + (sv.get("nome") or "Survey"),
         f"{quando:%d/%m/%Y %H:%M}",
         "",
         "COMO USAR",
         "  1. Abra o .kmz com duplo clique (Google Earth Pro).",
         "  2. No painel de camadas (esquerda) cada GRANDEZA e uma aba.",
         "     Deixe UMA marcada por vez: ligadas juntas, os pontos de",
         "     todas se empilham no mesmo lugar e o mapa nao diz nada.",
         "  3. Enquadre a cava e tire o print (no Earth Pro: Arquivo >",
         "     Salvar > Salvar imagem, que sai sem a barra lateral).",
         "  4. Cole na moldura do slide indicado abaixo.",
         "",
         "ARQUIVO -> ABA -> SLIDE", ""]
    for campos_a, banda, nome in gerados:
        b = f" — {banda}" if banda else ""
        L.append(f"  {nome}")
        for campo in campos_a:
            rot = SLIDE_DE_CAMPO.get(campo, campo)
            aba = (limite_de(campo) or (None, None, None, campo))[3]
            L.append(f"      aba '{aba}'  ->  slide: "
                     f"5. Site Survey — {rot}{b}")
    if pulados:
        L += ["", "NAO GERADOS (sem medicao no periodo)", ""]
        for campo, banda, motivo in pulados:
            L.append(f"  {campo}{(' / ' + banda) if banda else ''}: {motivo}")
    L += ["",
          "DICA: a pasta ZONAS-PROBLEMA marca as regioes fora do requisito,",
          "e o alfinete traz a recomendacao. A pasta FORA DO REQUISITO vem",
          "desligada de proposito: ligada, ela cobre os pontos bons.",
          ""]
    return "\n".join(L)


# Grandeza -> titulo do slide onde o print dela deve ser colado.
SLIDE_DE_CAMPO = {
    "sinal": "Intensidade de Sinal (RSSI)",
    "snr":   "Relação Sinal/Ruído (SNR)",
    "ruido": "Noise Floor",
    "rtt":   "Latência (RTT)",
    "perda": "Packet Loss",
    "interf": "Interferência de Canal",
}


def kml_do_survey(sid, campo="sinal", cfg=None):
    """Carrega o survey do banco e devolve (bytes, nome) do KMZ."""
    con = banco()
    try:
        sv = survey_obter(con, sid)
        if not sv: raise RuntimeError("survey não encontrado")
        am = survey_amostras(con, sid)
        try:    mn = manual_listar(con, sid)
        except Exception: mn = []
    finally:
        con.close()
    if not am:
        raise RuntimeError("survey sem amostras")
    return gerar_kml_survey(sv, am, _fixos_do_survey(am), mn, cfg=cfg,
                            campo=campo)


def inventario_bcs(coletor):
    """Lista os BCs conhecidos com nome, GPS e estado, para a página."""
    itens = []
    if not coletor: return itens
    for ip in sorted(getattr(coletor, "bcs", []) or []):
        est = estado(ip, getattr(coletor, "falhas_limite", 3),
                         getattr(coletor, "manter_s", 300))
        d = getattr(est, "ultima_dados", None) or {}
        s = d.get("sistema") or {}
        nome = (s.get("nome") or coletor.nomes.get(ip) or ip).strip()
        itens.append({
            "ip": ip, "nome": nome,
            "gps": bool(s.get("gps_fix")),
            "online": not getattr(est, "offline", False),
            "modelo": s.get("modelo", ""),
            "sats": s.get("gps_sats"),
        })
    # Sem GPS por último: são os que não entram no mapa.
    itens.sort(key=lambda x: (not x["gps"], x["nome"]))
    return itens


def resumo_selecao(itens, selecionados):
    """'34 selecionados · 28 com GPS · 6 sem GPS não entrarão no mapa'."""
    sel = [i for i in itens if i["nome"] in set(selecionados)]
    com = sum(1 for i in sel if i["gps"])
    off = sum(1 for i in sel if not i["online"])
    return {"total": len(sel), "com_gps": com, "sem_gps": len(sel) - com,
            "offline": off}


# ══════════════════════════════════════════════════════════════
# FUNDO DE SATÉLITE
# ══════════════════════════════════════════════════════════════
# Mapa de survey sem imagem embaixo é um borrão colorido: ninguém
# reconhece a rampa, a praça nem o britador. A rede da mina tem internet,
# então o programa busca o recorte georreferenciado sozinho.
#
# Cacheado em disco por bbox+tamanho: gerar sete imagens de uma banda
# baixaria o mesmo recorte sete vezes.

FUNDO_CACHE_DIR = "fundo_cache"
FUNDO_SERVICO = ("https://services.arcgisonline.com/arcgis/rest/services/"
                 "World_Imagery/MapServer/export")
FUNDO_CREDITO = "Imagem: Esri World Imagery"


def fundo_satelite(bbox, largura=1600, altura=1200, cfg=None, timeout=25):
    """Baixa (ou lê do cache) o recorte de satélite do bbox.

    bbox = {"norte","sul","leste","oeste"} em graus decimais.

    Devolve (caminho_png, credito) ou (None, motivo) quando não deu — o
    chamador então usa a ortofoto local ou desenha sem fundo. Nunca
    levanta: mapa sem imagem ainda é útil, survey que falha não é.
    """
    import hashlib, urllib.request, urllib.parse

    n, s = float(bbox["norte"]), float(bbox["sul"])
    l, o = float(bbox["leste"]), float(bbox["oeste"])
    if not (n > s and l > o):
        return None, "bbox inválido"

    chave = hashlib.md5(
        f"{o:.6f},{s:.6f},{l:.6f},{n:.6f},{largura}x{altura}".encode()
    ).hexdigest()[:16]
    cache = Path(FUNDO_CACHE_DIR); cache.mkdir(parents=True, exist_ok=True)
    destino = cache / f"sat_{chave}.png"
    if destino.exists() and destino.stat().st_size > 2048:
        return str(destino), FUNDO_CREDITO

    servico = (cfg.get("relatorio", "fundo_servico", fallback=FUNDO_SERVICO)
               if cfg else FUNDO_SERVICO)
    url = servico + "?" + urllib.parse.urlencode({
        "bbox": f"{o},{s},{l},{n}",
        "bboxSR": 4326, "imageSR": 4326,
        "size": f"{int(largura)},{int(altura)}",
        "format": "png", "f": "image",
    })
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "rajant-monitor"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            dados = r.read()
        # Erro do serviço volta como JSON com HTTP 200 — checar a assinatura
        # PNG evita gravar um "{"error":...}" no cache e reusar para sempre.
        if len(dados) < 2048 or not dados.startswith(b"\x89PNG"):
            return None, "serviço não devolveu PNG (bloqueio ou bbox recusado)"
        destino.write_bytes(dados)
        log.info(f"[fundo] satélite baixado: {destino.name} "
                 f"({len(dados)//1024} KB)")
        return str(destino), FUNDO_CREDITO
    except Exception as e:
        return None, f"download falhou: {e}"


def _bbox_cfg(cfg, chave="fundo_bbox"):
    """Lê 'N,S,L,O' do config. Sem isso não dá para posicionar a imagem."""
    txt = (cfg.get("relatorio", chave, fallback="") if cfg else "") or ""
    partes = [p.strip() for p in txt.split(",") if p.strip()]
    if len(partes) != 4: return None
    try:
        n, s, l, o = (float(x) for x in partes)
    except ValueError:
        return None
    if not (n > s and l > o): return None
    return {"norte": n, "sul": s, "leste": l, "oeste": o}


def fundo_do_kmz(caminho_kmz, saida="kmz_fundo"):
    """GroundOverlay do KMZ: imagem JÁ georreferenciada pelo LatLonBox.

    É o melhor fundo possível numa mina — a ortofoto do levantamento
    topográfico mostra rampa, praça e britador, coisas que nenhum mapa
    público tem dentro da cava.
    """
    try:
        d = ler_kmz(caminho_kmz, saida)
    except Exception as e:
        return None, None, f"KMZ não lido: {e}"
    for ov in d.get("overlays", []):
        if None in (ov.get("norte"), ov.get("sul"), ov.get("leste"), ov.get("oeste")):
            continue
        if not Path(ov["img"]).exists():
            continue
        return (ov["img"],
                {"norte": ov["norte"], "sul": ov["sul"],
                 "leste": ov["leste"], "oeste": ov["oeste"]},
                None)
    return None, None, "KMZ sem GroundOverlay georreferenciado"


def resolver_fundo(bbox, cfg=None, largura=1600, altura=1200, kmz=None):
    """Escolhe o fundo do mapa, na ordem: KMZ → satélite → ortofoto local.

    Devolve (caminho, extensao, credito, aviso).

    `extensao` é o bbox DA IMAGEM, que não é necessariamente o dos dados:
    a ortofoto cobre a área que cobre, e desenhá-la esticada no bbox das
    amostras deslocaria o terreno em relação aos pontos medidos — um mapa
    bonito e geograficamente errado, que é pior que mapa sem fundo.

    O aviso vai para o log e para o rodapé da imagem: quem olha precisa
    saber que está vendo a ortofoto antiga porque a rede bloqueou o
    satélite.
    """
    # 1) KMZ do levantamento — já vem georreferenciado
    caminho_kmz = kmz or (cfg.get("relatorio", "survey_kmz", fallback="")
                          if cfg else "")
    if caminho_kmz and Path(caminho_kmz).exists():
        img, ext, err = fundo_do_kmz(caminho_kmz)
        if img:
            return img, ext, "Ortofoto do KMZ do levantamento", None
        log.warning(f"[fundo] {err}")

    # 2) Satélite pela internet
    caminho, info = fundo_satelite(bbox, largura, altura, cfg)
    if caminho:
        # Pedimos exatamente este bbox, então a extensão é a do pedido.
        return caminho, bbox, info, None

    # 3) Ortofoto local — exige o bbox dela, senão não dá para posicionar
    local = (cfg.get("relatorio", "fundo_local", fallback="") if cfg else "")
    if local and Path(local).exists():
        ext = _bbox_cfg(cfg)
        if ext:
            log.warning(f"[fundo] satélite indisponível ({info}); "
                        f"usando ortofoto local {local}")
            return local, ext, "Ortofoto local", f"satélite indisponível: {info}"
        log.warning("[fundo] fundo_local configurado sem fundo_bbox "
                    "(N,S,L,O) — sem as coordenadas a imagem ficaria "
                    "deslocada, então não será usada")
        return None, None, None, ("ortofoto local sem fundo_bbox — "
                                  "configure [relatorio] fundo_bbox = N,S,L,O")

    log.warning(f"[fundo] sem imagem de fundo ({info})")
    return None, None, None, f"sem fundo: {info}"


# ══════════════════════════════════════════════════════════════
# CALIBRAÇÃO DO MODELO DE PROPAGAÇÃO, POR BANDA
# ══════════════════════════════════════════════════════════════
# Modelo log-distância:   RSSI = A − 10·n·log10(d)
#   A = sinal de referência a 1 m · n = expoente de perda do ambiente
#
# Ajustar aos DADOS MEDIDOS, não a valor de catálogo, é o que corrige as
# "manchas grandes demais" do mapa de cobertura: o alcance passa a ser o
# que a cava entrega, não o que a folha do rádio promete. Cava tem
# obstrução de bancada e talude — n costuma ficar bem acima dos 2,0 do
# espaço livre.
#
# 2,4 e 5,8 GHz calibram separado: a 5,8 atenua mais e não faz sentido
# um n único para as duas.

def _dist_m(lat1, lon1, lat2, lon2):
    """Distância em metros (equirretangular — sobra precisão para < 10 km)."""
    import math
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dlat, dlon) * 6371000.0


def _norm_banda(b):
    """'5GHz', '5.8 GHz', 5800 → '5.8 GHz'; qualquer 2,4 → '2.4 GHz'."""
    s = str(b or "").lower().replace(",", ".")
    if not s: return None
    if "2.4" in s or "2400" in s or s.startswith("2"): return "2.4 GHz"
    if "5" in s: return "5.8 GHz"
    return None


def calibrar_banda(amostras, fixos, banda, minimo=25):
    """Ajusta A e n por mínimos quadrados para uma banda.

    `fixos` = {nome: (lat, lon)} das repetidoras/torres. A distância de
    cada amostra é a do ERM/ERB mais próximo — não sabemos qual rádio
    serviu de fato em todos os casos, e o mais próximo é a hipótese
    defensável.

    Devolve None quando não dá para confiar no ajuste; quem chama decide
    cair no padrão e avisar no relatório.
    """
    import math
    if not fixos:
        return {"ok": False, "motivo": "nenhuma repetidora/torre com posição"}

    pts = []
    for a in amostras:
        if _norm_banda(a.get("banda")) != banda: continue
        sinal = a.get("sinal")
        if sinal is None or a.get("lat") is None or a.get("lon") is None:
            continue
        d = min((_dist_m(a["lat"], a["lon"], c[0], c[1])
                 for c in fixos.values() if c), default=None)
        # Perto demais, o modelo log-distância não vale (campo próximo);
        # longe demais, é quase certo que o enlace veio de outro rádio.
        if d is None or d < 20.0 or d > 6000.0: continue
        pts.append((math.log10(d), float(sinal)))

    if len(pts) < minimo:
        return {"ok": False, "motivo": f"amostras insuficientes ({len(pts)} < {minimo})",
                "amostras": len(pts)}

    n_ = len(pts)
    sx = sum(p[0] for p in pts); sy = sum(p[1] for p in pts)
    sxx = sum(p[0]*p[0] for p in pts); sxy = sum(p[0]*p[1] for p in pts)
    den = n_*sxx - sx*sx
    if abs(den) < 1e-12:
        return {"ok": False, "motivo": "amostras sem variação de distância",
                "amostras": n_}
    incl = (n_*sxy - sx*sy) / den        # = -10n
    A    = (sy - incl*sx) / n_
    n    = -incl / 10.0

    rms = math.sqrt(sum((y - (A + incl*x))**2 for x, y in pts) / n_)

    # n fora de 1,6–4,5 quase sempre significa geometria errada (posição de
    # repetidora trocada) ou amostra contaminada — não um ambiente exótico.
    if not (1.6 <= n <= 4.5):
        return {"ok": False, "motivo": f"expoente implausível (n={n:.2f})",
                "n": round(n, 3), "amostras": n_, "rms": round(rms, 2)}

    # Alcance = distância onde o modelo cruza -85 dBm (piso utilizável).
    alcance = 10 ** ((A - (-85.0)) / (10.0 * n))
    return {"ok": True, "A": round(A, 2), "n": round(n, 3),
            "rms": round(rms, 2), "amostras": n_,
            "alcance_m": round(max(50.0, min(alcance, 8000.0)), 1)}


# Padrões quando não dá para calibrar. Conservadores de propósito: é
# melhor a mancha ficar menor que a realidade do que prometer cobertura
# que não existe.
CALIB_PADRAO = {
    "2.4 GHz": {"ok": False, "A": -40.0, "n": 2.8, "alcance_m": 900.0,
                "padrao": True},
    "5.8 GHz": {"ok": False, "A": -42.0, "n": 3.0, "alcance_m": 600.0,
                "padrao": True},
}


def calibrar_todas_bandas(amostras, fixos, minimo=25):
    """Calibra 2,4 e 5,8 GHz. Banda sem ajuste confiável cai no padrão,
    com o motivo preservado para sair no relatório."""
    out = {}
    for banda in ("2.4 GHz", "5.8 GHz"):
        r = calibrar_banda(amostras, fixos, banda, minimo)
        if r.get("ok"):
            out[banda] = r
        else:
            out[banda] = dict(CALIB_PADRAO[banda],
                              motivo=r.get("motivo", "sem dados"),
                              amostras=r.get("amostras", 0))
            log.info(f"[calib] {banda}: usando padrão — {out[banda]['motivo']}")
    return out


# ══════════════════════════════════════════════════════════════
# MAPAS DO SURVEY — três camadas que nunca se misturam
# ══════════════════════════════════════════════════════════════
# 1. MEDIDO pela frota      — cor cheia, só onde passaram. Verdade de campo.
# 2. COBERTURA ESTIMADA     — manchas do modelo, opacidade menor + rótulo.
# 3. INFRAESTRUTURA         — posição e nome dos ERM/ERB.
#
# Sobrepor medição e estimativa na mesma imagem sem distinção foi o que
# tornou os mapas anteriores indefensáveis: ninguém sabia dizer o que era
# medido e o que era palpite do modelo.

# Escala ABSOLUTA. Normalizar pelos dados pinta de vermelho o pior ponto
# de uma área inteiramente aprovada — e o operador conclui que há problema
# onde não há.
ESCALAS = {
    "sinal": {"rot": "RSSI", "un": "dBm", "lo": -90, "hi": -55, "req": -75,
              "melhor": "alto"},
    "snr":   {"rot": "SNR",  "un": "dB",  "lo": 5,   "hi": 45,  "req": 20,
              "melhor": "alto"},
    "ruido": {"rot": "Ruído","un": "dBm", "lo": -100,"hi": -70, "req": -85,
              "melhor": "baixo"},
    "rtt":   {"rot": "Latência","un":"ms","lo": 0,   "hi": 200, "req": 100,
              "melhor": "baixo"},
    "perda": {"rot": "Perda","un": "%",   "lo": 0,   "hi": 10,  "req": 2,
              "melhor": "baixo"},
    # Ocupação do meio por transmissor alheio. 20% é onde o CSMA começa a
    # atrasar o acesso ao meio de forma perceptível; acima de 50% a banda
    # útil despenca mesmo com RSSI ótimo — é o caso que confunde a
    # operação ("o sinal está cheio e a rede está lenta").
    "interf": {"rot": "Interferência", "un": "%", "lo": 0, "hi": 60,
               "req": 20, "melhor": "baixo"},
    # COBERTURA DISPONIVEL — o melhor vizinho de INFRAESTRUTURA visivel
    # naquele ponto, nao o enlace que atendeu. Responde "existe sinal
    # servivel aqui?", que e outra pergunta de "a aplicacao funcionou
    # aqui?". Mesma escala e mesmo requisito do RSSI: e RSSI, medido de
    # outra fonte.
    "sinal_cob": {"rot": "Cobertura disponível", "un": "dBm",
                  "lo": -90, "hi": -55, "req": -75, "melhor": "alto"},
}

# Vermelho → verde, o mesmo racional do heatmap do cliente.
_CORES_RF = ["#C0392B","#E74C3C","#E67E22","#F1C40F","#9ACD32","#27AE60",
             "#1E8449","#145A32"]


def _cmap_rf(invertido=False):
    from matplotlib.colors import LinearSegmentedColormap
    cores = list(reversed(_CORES_RF)) if invertido else _CORES_RF
    return LinearSegmentedColormap.from_list("rf", cores, N=256)


def _aspecto_geo(ax, lat_media):
    """Corrige a proporção: 1° de longitude encolhe por cos(latitude).

    TEM de ser chamado DEPOIS de todos os imshow — cada imshow com
    aspect="auto" reseta o aspecto dos eixos, e o mapa sai esticado.
    """
    import math
    ax.set_aspect(1.0 / max(0.1, math.cos(math.radians(lat_media))))


# Ligado por dados de demonstração. Um mapa bem feito, no template do
# cliente, com números plausíveis, circula internamente e vira "o survey
# da mina" — a marca existe para isso não acontecer por engano.
MARCA_DEMO = {"ativa": False,
              "texto": "DADOS SINTÉTICOS — NÃO É MEDIÇÃO REAL"}


def _marca_demo(ax):
    if not MARCA_DEMO.get("ativa"): return
    ax.text(0.5, 0.5, MARCA_DEMO["texto"], transform=ax.transAxes,
            ha="center", va="center", rotation=28, fontsize=26,
            color="#FF3B30", alpha=0.20, fontweight="bold", zorder=50)


def _moldura(ax, titulo, sub, credito=None, aviso=None):
    ax.set_facecolor("#0D2052")
    for s in ax.spines.values(): s.set_color("#2A3A5C")
    ax.tick_params(colors="#A0B0CC", labelsize=8)
    ax.set_xlabel("Longitude", color="#C8D0E0", fontsize=9)
    ax.set_ylabel("Latitude",  color="#C8D0E0", fontsize=9)
    ax.set_title(titulo, color="white", fontsize=13, fontweight="bold", pad=14)
    partes = [p for p in (sub, credito, aviso) if p]
    if MARCA_DEMO.get("ativa"):
        partes.append(MARCA_DEMO["texto"])
    if partes:
        ax.text(0.5, -0.085, "  ·  ".join(partes), transform=ax.transAxes,
                ha="center", va="top", color="#8FA0BC", fontsize=7.5)
    _marca_demo(ax)


def _legenda_faixas(fig, ax, esc, cmap):
    """Barra de cores com os VALORES em dBm/dB/ms, não rótulos vagos."""
    import matplotlib as mpl
    norm = mpl.colors.Normalize(vmin=esc["lo"], vmax=esc["hi"])
    sm = mpl.cm.ScalarMappable(norm=norm, cmap=cmap)
    cb = fig.colorbar(sm, ax=ax, shrink=.72, pad=.02)
    cb.set_label(f"{esc['rot']} ({esc['un']})", color="#C8D0E0", fontsize=9,
                 labelpad=16)
    cb.ax.tick_params(colors="#A0B0CC", labelsize=8)
    cb.outline.set_edgecolor("#2A3A5C")
    # Linha do requisito Modular, para leitura imediata de aprovado/reprovado.
    # O rótulo vai DENTRO da barra: fora, colidia com o texto do eixo.
    y = (esc["req"] - esc["lo"]) / (esc["hi"] - esc["lo"])
    cb.ax.axhline(y, color="white", lw=1.8, ls="--")
    cb.ax.text(0.5, y, f"req {esc['req']}", transform=cb.ax.transAxes,
               color="white", fontsize=7.5, va="bottom", ha="center",
               fontweight="bold",
               bbox=dict(fc="#0D2052CC", ec="none", boxstyle="round,pad=.18"))
    return cb


def _bbox_de(pontos, margem=0.12):
    """bbox dos pontos com margem. Filtra outlier de hemisfério — KML do
    cliente costuma vir com longitude de sinal trocado, e um único ponto
    desses estica a área por milhares de km e degenera a grade."""
    las = [p[0] for p in pontos]; los = [p[1] for p in pontos]
    if not las: return None
    import statistics as st
    if len(los) >= 4:
        mlo, mla = st.median(los), st.median(las)
        manter = [(la, lo) for la, lo in zip(las, los)
                  if (lo < 0) == (mlo < 0) and (la < 0) == (mla < 0)]
        if len(manter) >= max(3, len(las) * .5):
            descartados = len(las) - len(manter)
            if descartados:
                log.warning(f"[mapa] {descartados} ponto(s) com hemisfério "
                            f"invertido descartado(s)")
            las = [p[0] for p in manter]; los = [p[1] for p in manter]
    dla = max(las) - min(las) or 0.004
    dlo = max(los) - min(los) or 0.004
    return {"norte": max(las) + dla*margem, "sul":   min(las) - dla*margem,
            "leste": max(los) + dlo*margem, "oeste": min(los) - dlo*margem}


def gerar_mapas_survey(survey, amostras, fixos, saida_dir, banda=None,
                       cfg=None, calib=None, kmz=None):
    """Gera os PNGs do survey. Devolve {chave: caminho}."""
    import math
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    saida = Path(saida_dir); saida.mkdir(parents=True, exist_ok=True)
    imgs = {}
    am = [a for a in amostras
          if a.get("lat") is not None and a.get("lon") is not None]
    if banda:
        am = [a for a in am if _norm_banda(a.get("banda")) == banda]
    if not am:
        log.warning(f"[mapa] banda {banda}: sem amostras georreferenciadas")
        return imgs

    suf = f"_{(banda or 'geral').replace(' ','').replace('.','')}"
    nome_sv = survey.get("nome") or "Survey"
    quando = datetime.fromtimestamp(survey.get("inicio") or time.time())
    sub_base = f"{nome_sv} · {quando:%d/%m/%Y %H:%M}" + (f" · {banda}" if banda else "")

    pontos = [(a["lat"], a["lon"]) for a in am]
    pontos += [(c[0], c[1]) for c in fixos.values() if c]
    bbox = _bbox_de(pontos)
    lat_med = (bbox["norte"] + bbox["sul"]) / 2

    fundo, ext_fundo, credito, aviso = resolver_fundo(bbox, cfg, kmz=kmz)
    img_fundo = None
    if fundo:
        try: img_fundo = plt.imread(fundo)
        except Exception as e:
            log.warning(f"[mapa] fundo não carregado: {e}"); img_fundo = None

    # Dois retângulos diferentes, de propósito:
    #  `ext`        — os LIMITES DOS EIXOS, definidos pelos dados medidos;
    #  `ext_img`    — a extensão GEOGRÁFICA DA IMAGEM de fundo.
    # Desenhar a ortofoto em `ext` a esticaria para o bbox das amostras e
    # deslocaria o terreno em relação aos pontos — mapa bonito e errado.
    ext = [bbox["oeste"], bbox["leste"], bbox["sul"], bbox["norte"]]
    ext_img = ([ext_fundo["oeste"], ext_fundo["leste"],
                ext_fundo["sul"], ext_fundo["norte"]] if ext_fundo else ext)

    def base(titulo, sub_extra=""):
        fig, ax = plt.subplots(figsize=(13, 9), dpi=130)
        fig.patch.set_facecolor("#0D2052")
        if img_fundo is not None:
            # aspect="auto" aqui é proposital; o aspecto geográfico é
            # aplicado no fim, senão o imshow o sobrescreve.
            ax.imshow(img_fundo, extent=ext_img, aspect="auto", zorder=0,
                      interpolation="bilinear")
        ax.set_xlim(ext[0], ext[1]); ax.set_ylim(ext[2], ext[3])
        _moldura(ax, titulo, " · ".join(x for x in (sub_base, sub_extra) if x),
                 credito, aviso)
        return fig, ax

    def infra(ax, rotular=True):
        """Camada 3 — infraestrutura."""
        for nome, c in fixos.items():
            if not c: continue
            ax.plot(c[1], c[0], marker="^", ms=11, mfc="#F1C40F",
                    mec="white", mew=1.3, zorder=8, ls="none")
            if rotular:
                ax.annotate(nome[:14], (c[1], c[0]), textcoords="offset points",
                            xytext=(7, 5), fontsize=7.5, color="#F1C40F",
                            zorder=9,
                            path_effects=None)

    def fechar(fig, ax, chave):
        _aspecto_geo(ax, lat_med)          # sempre por último
        p = saida / f"{chave}{suf}.png"
        fig.tight_layout()
        fig.savefig(p, facecolor="#0D2052", bbox_inches="tight")
        plt.close(fig)
        imgs[chave] = str(p)

    # ── 1. ROTA MEDIDA, colorida pela qualidade medida no trecho ──
    fig, ax = base("Rota percorrida — qualidade medida",
                   "camada: MEDIDO pela frota")
    por_radio = {}
    for a in am:
        por_radio.setdefault(a["radio"], []).append(a)
    esc = ESCALAS["sinal"]; cmap = _cmap_rf()
    from matplotlib.collections import LineCollection
    total_tr = 0
    for radio, pts in por_radio.items():
        pts = sorted(pts, key=lambda x: x["ts"])
        if len(pts) < 2: continue
        xs = [p["lon"] for p in pts]; ys = [p["lat"] for p in pts]
        vs = [p.get("sinal") for p in pts]
        segs, cores = [], []
        for i in range(len(pts) - 1):
            v = vs[i] if vs[i] is not None else vs[i+1]
            if v is None: continue
            segs.append([(xs[i], ys[i]), (xs[i+1], ys[i+1])])
            cores.append(max(0.0, min(1.0,
                (v - esc["lo"]) / (esc["hi"] - esc["lo"]))))
        if not segs: continue
        lc = LineCollection(segs, cmap=cmap, array=np.array(cores),
                            linewidths=2.6, zorder=5, capstyle="round")
        lc.set_clim(0, 1)
        ax.add_collection(lc)
        total_tr += 1
        ax.plot(xs[0], ys[0], "o", ms=5, mfc="#27AE60", mec="white",
                mew=.9, zorder=6)
    infra(ax)
    _legenda_faixas(fig, ax, esc, cmap)
    ax.text(.01, .99, f"{len(am)} amostras · {total_tr} trajeto(s)",
            transform=ax.transAxes, va="top", color="#C8D0E0", fontsize=8.5,
            bbox=dict(fc="#0D2052CC", ec="#2A3A5C", boxstyle="round,pad=.4"))
    fechar(fig, ax, "rota")

    # ── 2. MAPAS MEDIDOS por grandeza ──
    for campo, esc in ESCALAS.items():
        vals = [(a["lat"], a["lon"], a[campo]) for a in am
                if a.get(campo) is not None]
        if len(vals) < 3:
            continue
        cmap = _cmap_rf(invertido=(esc["melhor"] == "baixo"))
        fig, ax = base(f"{esc['rot']} medido", "camada: MEDIDO pela frota")
        xs = [v[1] for v in vals]; ys = [v[0] for v in vals]
        cs = [max(esc["lo"], min(esc["hi"], v[2])) for v in vals]
        ax.scatter(xs, ys, c=cs, cmap=cmap, vmin=esc["lo"], vmax=esc["hi"],
                   s=42, edgecolors="white", linewidths=.35, zorder=5,
                   alpha=.95)
        infra(ax)
        _legenda_faixas(fig, ax, esc, cmap)
        op = ">" if esc["melhor"] == "alto" else "<"
        dentro = sum(1 for v in vals
                     if (v[2] > esc["req"] if op == ">" else v[2] < esc["req"]))
        ax.text(.01, .99,
                f"{len(vals)} pontos · {dentro/len(vals)*100:.1f}% dentro do "
                f"requisito ({op} {esc['req']} {esc['un']})",
                transform=ax.transAxes, va="top", color="#C8D0E0", fontsize=8.5,
                bbox=dict(fc="#0D2052CC", ec="#2A3A5C", boxstyle="round,pad=.4"))
        fechar(fig, ax, f"medido_{campo}")

    # ── 3. COBERTURA ESTIMADA (camada separada, sempre rotulada) ──
    cal = (calib or {}).get(banda or "5.8 GHz") or CALIB_PADRAO.get(
        banda or "5.8 GHz", {"n": 3.0, "A": -42.0, "alcance_m": 600.0})
    alcance = float(cal.get("alcance_m") or 600.0)
    if fixos:
        esc = ESCALAS["sinal"]; cmap = _cmap_rf()
        rot_cal = (f"modelo calibrado n={cal.get('n')} "
                   f"(RMS {cal.get('rms')} dB, {cal.get('amostras')} amostras)"
                   if cal.get("ok") else
                   f"modelo NÃO calibrado — {cal.get('motivo','sem dados')}")
        fig, ax = base("Cobertura estimada — planejamento",
                       f"camada: ESTIMATIVA · {rot_cal}")
        G = 320
        gx = np.linspace(ext[0], ext[1], G)
        gy = np.linspace(ext[2], ext[3], G)
        MX, MY = np.meshgrid(gx, gy)
        melhor = np.full(MX.shape, -200.0)
        # Opacidade máxima 62 %, caindo a ZERO no alcance calibrado. O
        # desvanecimento é função da DISTÂNCIA, não do RSSI: preso ao RSSI
        # ele satura a poucos metros do rádio e a mancha vira um bloco
        # opaco até a borda, escondendo o terreno. É a queda que produz o
        # efeito de ilhas do heatmap de referência.
        alpha = np.zeros(MX.shape)
        mlat = math.cos(math.radians(lat_med))
        for c in fixos.values():
            if not c: continue
            dx = (MX - c[1]) * 111320.0 * mlat
            dy = (MY - c[0]) * 111320.0
            d = np.hypot(dx, dy); d[d < 1] = 1
            rssi = cal.get("A", -42.0) - 10.0*cal.get("n", 3.0)*np.log10(d)
            rssi[d > alcance] = -200.0
            melhor = np.maximum(melhor, rssi)
            a = np.clip(1.0 - d / max(alcance, 1.0), 0, 1) ** 0.65 * 0.62
            alpha = np.maximum(alpha, a)
        vis = np.clip(melhor, esc["lo"], esc["hi"])
        alpha[melhor <= -199] = 0.0
        rgba = cmap((vis - esc["lo"]) / (esc["hi"] - esc["lo"]))
        rgba[..., 3] = alpha
        ax.imshow(rgba, extent=ext, origin="lower", aspect="auto", zorder=3,
                  interpolation="bilinear")
        infra(ax)
        _legenda_faixas(fig, ax, esc, cmap)
        ax.text(.01, .99,
                f"ESTIMATIVA DE PLANEJAMENTO — não é medição\n"
                f"alcance do modelo: {alcance:.0f} m",
                transform=ax.transAxes, va="top", color="#F1C40F", fontsize=9,
                fontweight="bold",
                bbox=dict(fc="#0D2052DD", ec="#F1C40F", boxstyle="round,pad=.45"))
        fechar(fig, ax, "cobertura")

    # ── 4. HISTOGRAMAS por faixa ──
    for campo, esc in ESCALAS.items():
        vals = [a[campo] for a in am if a.get(campo) is not None]
        if len(vals) < 3: continue
        fig, ax = plt.subplots(figsize=(8, 4.4), dpi=130)
        fig.patch.set_facecolor("#0D2052"); ax.set_facecolor("#152238")
        cmap = _cmap_rf(invertido=(esc["melhor"] == "baixo"))
        n_, bins, patches = ax.hist(vals, bins=18,
                                    range=(esc["lo"], esc["hi"]),
                                    edgecolor="#0D2052", linewidth=.6)
        for b, p in zip(bins, patches):
            p.set_facecolor(cmap((b - esc["lo"]) / (esc["hi"] - esc["lo"])))
        ax.axvline(esc["req"], color="white", ls="--", lw=1.8)
        ax.text(esc["req"], ax.get_ylim()[1]*.94, f" requisito {esc['req']}",
                color="white", fontsize=8.5, va="top")
        op = ">" if esc["melhor"] == "alto" else "<"
        dentro = sum(1 for v in vals
                     if (v > esc["req"] if op == ">" else v < esc["req"]))
        ax.set_title(f"{esc['rot']} — {dentro/len(vals)*100:.1f}% dentro do "
                     f"requisito ({len(vals)} amostras)",
                     color="white", fontsize=11, fontweight="bold")
        ax.set_xlabel(f"{esc['rot']} ({esc['un']})", color="#C8D0E0", fontsize=9)
        ax.set_ylabel("amostras", color="#C8D0E0", fontsize=9)
        ax.tick_params(colors="#A0B0CC", labelsize=8)
        for s in ax.spines.values(): s.set_color("#2A3A5C")
        p = saida / f"hist_{campo}{suf}.png"
        fig.tight_layout(); fig.savefig(p, facecolor="#0D2052"); plt.close(fig)
        imgs[f"hist_{campo}"] = str(p)

    log.info(f"[mapa] banda {banda or 'geral'}: {len(imgs)} imagens em {saida}")
    return imgs


TIPOS_MAPA_REDE = {
    # tipo -> (marcador, cor, rótulo da legenda)
    "ERB":   ("^", "#F1C40F", "Torres fixas (ERB)"),
    "ERM":   ("^", "#4FA3F7", "Repetidoras móveis (ERM)"),
    "Móvel": ("o", "#2ECC71", "Equipamentos móveis"),
}


def _tipo_no_mapa(nome, classificar):
    """ERB / ERM / Móvel. Backbone e Outros entram como móvel só se
    andaram — quem não andou vira infraestrutura, que é o que ele é."""
    cat = classificar(nome)
    if cat in TIPOS_MAPA_REDE:
        return cat
    return "Móvel"


def contar_rede(amostras, radios_survey=None, cfg=None, fixos=None):
    """Números do rodapé do Mapa da Rede.

    'Sem posição' são os rádios que PARTICIPARAM do survey e nunca
    reportaram fix — é o dado que a manutenção usa, e ele só existe se a
    lista de participantes vier junto (as amostras sozinhas não sabem
    quem ficou de fora).

    `fixos` conta como posicionado: uma torre com posição declarada
    aparece no mapa, e listá-la como 'sem posição' contradiria a imagem
    ao lado da tabela.
    """
    classificar = classificador_categorias(cfg_relatorio(
        cfg or configparser.ConfigParser()))
    com_pos = {n for n, c in (fixos or {}).items() if c}
    todos = set(radios_survey or []) | set(com_pos)
    for a in amostras:
        r = a.get("radio")
        if not r: continue
        todos.add(r)
        if a.get("lat") is not None and a.get("lon") is not None:
            com_pos.add(r)
    tipos = {t: 0 for t in TIPOS_MAPA_REDE}
    for r in com_pos:
        tipos[_tipo_no_mapa(r, classificar)] += 1
    return {"tipos": tipos, "com_gps": len(com_pos),
            "sem_posicao": sorted(todos - com_pos),
            "total": len(todos)}


def gerar_mapa_rede(survey, amostras, fixos, saida_dir, cfg=None, kmz=None,
                    radios_survey=None):
    """Mapa único da rede: onde está cada BreadCrumb e por onde a frota andou.

    Não é por banda — é um retrato da malha. As rotas entram só como
    contexto, em cinza translúcido: colorir por qualidade aqui competiria
    com os slides de RSSI/SNR, que existem exatamente para isso.

    Mesmo pipeline dos heatmaps (mesmo bbox, mesmo fundo, mesma proporção
    geográfica), para os slides ficarem comparáveis lado a lado.
    """
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    saida = Path(saida_dir); saida.mkdir(parents=True, exist_ok=True)
    am = [a for a in amostras
          if a.get("lat") is not None and a.get("lon") is not None]
    if not am and not fixos:
        log.warning("[mapa/rede] sem posição em nenhum equipamento")
        return None, {}

    cfgr = cfg_relatorio(cfg or configparser.ConfigParser())
    classificar = classificador_categorias(cfgr)

    pontos = [(a["lat"], a["lon"]) for a in am]
    pontos += [(c[0], c[1]) for c in fixos.values() if c]
    bbox = _bbox_de(pontos)
    lat_med = (bbox["norte"] + bbox["sul"]) / 2
    ext = [bbox["oeste"], bbox["leste"], bbox["sul"], bbox["norte"]]

    fundo, ext_fundo, credito, aviso = resolver_fundo(bbox, cfg, kmz=kmz)
    ext_img = ([ext_fundo["oeste"], ext_fundo["leste"],
                ext_fundo["sul"], ext_fundo["norte"]] if ext_fundo else ext)

    fig, ax = plt.subplots(figsize=(13, 9), dpi=130)
    fig.patch.set_facecolor("#0D2052")
    if fundo:
        try:
            ax.imshow(plt.imread(fundo), extent=ext_img, aspect="auto",
                      zorder=0, interpolation="bilinear")
        except Exception as e:
            log.warning(f"[mapa/rede] fundo não carregado: {e}")
    ax.set_xlim(ext[0], ext[1]); ax.set_ylim(ext[2], ext[3])

    # ── rotas por baixo, sem cor de qualidade ──
    por_radio = {}
    for a in am:
        por_radio.setdefault(a["radio"], []).append(a)
    n_rotas = 0
    for radio, pts in por_radio.items():
        if len(pts) < 2: continue
        pts = sorted(pts, key=lambda x: x["ts"])
        ax.plot([p["lon"] for p in pts], [p["lat"] for p in pts],
                "-", color="#B8C4D8", lw=1.0, alpha=.38, zorder=3,
                solid_capstyle="round")
        n_rotas += 1

    # ── posição de cada equipamento ──
    # Fixo = posição declarada; móvel = último ponto do trajeto, que é
    # onde ele estava ao fim do survey.
    posicoes = {}
    for nome, c in (fixos or {}).items():
        if c: posicoes[nome] = (c[0], c[1])
    for radio, pts in por_radio.items():
        if radio in posicoes: continue
        u = sorted(pts, key=lambda x: x["ts"])[-1]
        posicoes[radio] = (u["lat"], u["lon"])

    contagem = {t: 0 for t in TIPOS_MAPA_REDE}
    for nome, (la, lo) in posicoes.items():
        t = _tipo_no_mapa(nome, classificar)
        mk, cor, _ = TIPOS_MAPA_REDE[t]
        contagem[t] += 1
        ax.plot(lo, la, marker=mk, ms=11 if mk == "^" else 8, mfc=cor,
                mec="white", mew=1.2, ls="none", zorder=8)
        ax.annotate(nome[:14], (lo, la), textcoords="offset points",
                    xytext=(7, 5), fontsize=7, color=cor, zorder=9)

    from matplotlib.lines import Line2D
    itens = [Line2D([], [], marker=mk, color="none", mfc=cor, mec="white",
                    mew=1.1, ms=10, ls="none",
                    label=f"{rot}: {contagem[t]}")
             for t, (mk, cor, rot) in TIPOS_MAPA_REDE.items()]
    itens.append(Line2D([], [], color="#B8C4D8", lw=1.4, alpha=.6,
                        label=f"Rotas do survey: {n_rotas}"))
    leg = ax.legend(handles=itens, loc="upper right", fontsize=8.5,
                    facecolor="#0D2052", edgecolor="#2A3A5C", framealpha=.92)
    for t in leg.get_texts(): t.set_color("#E8EDF7")

    nome_sv = survey.get("nome") or "Survey"
    quando = datetime.fromtimestamp(survey.get("inicio") or time.time())
    _moldura(ax, "Mapa da Rede — posição dos BreadCrumbs",
             f"{nome_sv} · {quando:%d/%m/%Y %H:%M}", credito, aviso)
    _aspecto_geo(ax, lat_med)          # sempre por último: imshow reseta

    p = saida / "mapa_rede.png"
    fig.tight_layout()
    fig.savefig(p, facecolor="#0D2052", bbox_inches="tight")
    plt.close(fig)

    resumo = contar_rede(amostras, radios_survey, cfg, fixos=fixos)
    resumo["rotas"] = n_rotas
    resumo["tipos"] = contagem          # contagem do que foi de fato plotado
    log.info(f"[mapa/rede] {len(posicoes)} equipamentos, {n_rotas} rotas")
    return str(p), resumo


# ──────────────────────────────────────────────────────────────
# CAPTURA AO VIVO PARA O KMZ
# O usuário escolhe os BCs, a duração e o intervalo; durante a janela
# o serviço consulta os rádios DIRETAMENTE, no intervalo pedido, e monta
# o trajeto de cada equipamento.
#
# Antes isto amostrava o estado que o exporter já tinha buscado. Era
# barato, mas a resolução real nunca passava do ciclo do exporter (60 s
# por padrão): pedir 10 s devolvia o mesmo ponto seis vezes. A 40 km/h um
# caminhão anda ~11 m/s, então 60 s espaça os pontos em ~660 m — a rota
# vira uma reta grosseira e não dá para localizar onde o sinal caiu.
#
# O preço é carga na malha, e é por isso que existem sessão persistente,
# teto de threads e min_intervalo_s. O exporter continua rodando em
# paralelo: a carga total é a soma dos dois.
# ──────────────────────────────────────────────────────────────
CAPTURAS = {}
_CAP_LOCK = threading.Lock()
COLETOR_ATUAL = {}

DEFAULTS_SURVEY = {
    # Teto de consultas simultâneas. 150 conexões de uma vez derrubam a
    # malha antes de derrubar o servidor.
    "max_threads":         "12",
    "timeout_s":           "6",
    # Piso do intervalo: impede pedir 1 s com 150 rádios pela página.
    "min_intervalo_s":     "5",
    # Piso do modo continuo (intervalo pedido = 0): espera minima entre
    # ciclos. Nao e o intervalo — o ciclo costuma demorar mais que isto —,
    # e so o que impede uma selecao pequena de virar rajada no radio.
    "piso_continuo_s":     "0",
    # Cadencia do ping, independente do ciclo. O ping do Windows custa ~3 s
    # (`ping -n 4` espera ~1 s entre envios e nao aceita intervalo); preso
    # ao ciclo, ele impunha esse piso tambem a posicao, que e o que desenha
    # o rastro. 0 = pinga em todo ciclo.
    "ping_a_cada_s":       "15",
    # Teto de pings por ciclo. Sem ele, com a frota inteira selecionada o
    # ciclo fica maior que ping_a_cada_s, TODO radio vive vencido e o ping
    # volta a ser de todos — foi o que fez o ciclo dar 46 s com 159 radios.
    # Vazio = usa o teto de threads.
    "ping_max_por_ciclo":  "",
    # Falhas seguidas até desistir do rádio pelo resto do survey. Um rádio
    # morto não pode segurar o ciclo dos outros.
    "falhas_para_pular":   "3",
    # Rádio que falhou na consulta direta cai para o último estado do
    # exporter, marcado como fonte=cache. Ponto defasado e identificado é
    # melhor que buraco no trajeto.
    "usar_cache_fallback": "true",
}


def cfg_survey(cfg):
    """Garante a seção [survey] no config, como cfg_relatorio faz."""
    if cfg is None:
        cfg = configparser.ConfigParser()
    if not cfg.has_section("survey"):
        cfg.add_section("survey")
    for k, v in DEFAULTS_SURVEY.items():
        if not cfg.has_option("survey", k):
            cfg.set("survey", k, v)
    return cfg


class SessaoRadio:
    """Sessão BCAPI persistente com um rádio, para uso durante o survey.

    Autenticar custa caro no BreadCrumb — é a operação que mais pesa. A
    cada 10 s em 150 rádios, reautenticar por amostra derruba a malha.
    Então autentica uma vez e reusa; só refaz a sessão quando a consulta
    falha.

    Depois de `falhas_max` falhas SEGUIDAS o rádio é dado como
    indisponível e não é mais tentado neste survey: insistir num rádio
    morto atrasa o ciclo inteiro, e o ciclo é o que define a resolução do
    trajeto.
    """

    def __init__(self, ip, porta, role, senha, timeout=6, falhas_max=3):
        self.ip = ip; self.porta = porta; self.role = role; self.senha = senha
        self.timeout = timeout; self.falhas_max = max(1, int(falhas_max))
        self.bc = None
        self.falhas = 0
        self.desistiu = False
        self.ultimo_erro = None
        self.consultas = 0

    def _abrir(self):
        bc = exigir_rajant_api()(host=self.ip, port=self.porta,
                        role=self.role, password=self.senha)
        if not bc.reachable():    raise ConnectionRefusedError("nao alcancavel")
        if not bc.authenticate(): raise PermissionError("autenticacao falhou")
        self.bc = bc
        return bc

    def estado_bruto(self):
        """Texto do state. None quando o rádio já foi descartado.

        Levanta a exceção da consulta para quem chama decidir o fallback.
        """
        if self.desistiu:
            return None
        try:
            bc = self.bc or self._abrir()
            txt = _get_state_filtrado(bc)
            self.falhas = 0; self.consultas += 1
            return txt
        except Exception as e:
            # A sessão pode ter caído (rádio reiniciou, rota mudou). Joga
            # fora para a próxima tentativa reautenticar do zero.
            self.bc = None
            self.falhas += 1
            self.ultimo_erro = str(e)
            if self.falhas >= self.falhas_max:
                self.desistiu = True
                log.warning(f"[survey] {self.ip} descartado apos "
                            f"{self.falhas} falhas seguidas: {e}")
            raise

    def fechar(self):
        for m in ("close", "disconnect", "logout"):
            try:
                f = getattr(self.bc, m, None)
                if callable(f): f(); break
            except Exception: pass
        self.bc = None


# Descoberto uma vez por processo: a assinatura de get_state varia entre
# versões da rajant-api, e chutar o nome do parâmetro quebraria a coleta
# inteira. None = ainda não inspecionado; "" = não aceita filtro.
_FILTRO_ESTADO = {"param": None}

# Só precisamos destes ramos: posição, RF e identificação. Puxar o state
# inteiro de 150 rádios a cada 10 s é desperdício de banda na malha.
CAMINHOS_ESTADO = ("gps", "wireless", "system")


def _get_state_filtrado(bc):
    """get_state pedindo só os ramos de que o survey precisa.

    A biblioteca nem sempre expõe filtro de caminho, e o nome do parâmetro
    mudou entre versões. Em vez de assumir, inspeciona a assinatura na
    primeira chamada e guarda o resultado; sem filtro disponível, cai para
    o state inteiro — mais pesado, porém correto.
    """
    if _FILTRO_ESTADO["param"] is None:
        _FILTRO_ESTADO["param"] = ""
        try:
            import inspect
            ps = inspect.signature(bc.get_state).parameters
            for cand in ("stateFilterPath", "state_filter_path", "filterPath",
                         "filter_path", "path", "paths", "messagePath"):
                if cand in ps:
                    _FILTRO_ESTADO["param"] = cand
                    log.info(f"[survey] consulta enxuta via {cand}="
                             f"{','.join(CAMINHOS_ESTADO)}")
                    break
            else:
                log.info("[survey] rajant-api sem filtro de caminho; "
                         "consultando o state inteiro")
        except (TypeError, ValueError):
            log.info("[survey] assinatura de get_state nao inspecionavel; "
                     "consultando o state inteiro")

    par = _FILTRO_ESTADO["param"]
    if not par:
        return bc.get_state()
    try:
        return bc.get_state(**{par: list(CAMINHOS_ESTADO)})
    except Exception:
        # Aceita o argumento mas não esse formato: desliga o filtro de vez
        # em vez de repetir o erro a cada amostra.
        _FILTRO_ESTADO["param"] = ""
        log.warning("[survey] filtro de caminho recusado; "
                    "voltando ao state inteiro")
        return bc.get_state()

class CapturaGPS:
    def __init__(self, ident, ips, minutos, intervalo_s, coletor_ref, rotulo="",
                 alcance_m=1000.0, com_ping=True, ping_n=4, banco_path=None,
                 cfg=None):
        self.id         = ident
        self.ips        = list(ips)
        self.minutos    = float(minutos)
        self.cfg        = cfg_survey(cfg)
        sv              = self.cfg["survey"]
        self.min_int    = sv.getint("min_intervalo_s", fallback=5)
        # Modo contínuo (intervalo 0): o ciclo seguinte sai assim que o
        # anterior volta, sem espera fixa. É o que aproxima o traçado de
        # uma linha em vez de uma sequência de pontos — a 40 km/h, medir a
        # cada 20 s espaça os pontos em 220 m; a cada 1 s, em 11 m.
        #
        # O piso existe porque "contínuo" não é grátis: cada amostra é uma
        # ida e volta ao rádio pela própria malha que se está medindo. Com
        # poucos veículos selecionados o custo é baixo e o ganho é grande;
        # com a frota inteira, o ciclo já se alonga sozinho pelo teto de
        # threads, e o intervalo REAL vai para o relatório.
        self.continuo   = int(intervalo_s) <= 0
        # Piso 0 = sem pausa: o ciclo seguinte sai no instante em que o
        # anterior termina, e a cadência passa a ser só o tempo de ida e
        # volta ao rádio. O mínimo de 50 ms não é freio de carga — é
        # proteção contra laço vazio: se TODOS os rádios falharem na
        # hora, sem ele o processo giraria a 100% de CPU sem medir nada.
        self.piso_cont  = max(0.0, sv.getfloat("piso_continuo_s", fallback=0.0))
        self.intervalo  = (max(self.piso_cont, 0.05) if self.continuo
                           else max(self.min_int, int(intervalo_s)))
        self.max_thr    = max(1, sv.getint("max_threads", fallback=12))
        self.timeout_s  = sv.getint("timeout_s", fallback=6)
        self.falhas_max = sv.getint("falhas_para_pular", fallback=3)
        self.cache_ok   = sv.getboolean("usar_cache_fallback", fallback=True)
        self.sessoes    = {}          # ip -> SessaoRadio
        self.efetivo_s  = None        # intervalo que de fato aconteceu
        self.n_cache    = 0           # amostras que vieram do exporter
        self.n_direto   = 0
        self.coletor    = coletor_ref
        self.rotulo     = rotulo or f"Survey {datetime.now():%d/%m %H:%M}"
        self.alcance_m  = alcance_m
        self.com_ping   = com_ping
        self.ping_n     = max(1, int(ping_n))
        # Cadencia propria do ping, em segundos. 0 = todo ciclo.
        self.ping_a_cada = max(0.0, sv.getfloat("ping_a_cada_s", fallback=15.0))
        self._ultimo_ping = {}
        self._pingar_agora = set()
        # Teto de pings por ciclo. Com o padrao amarrado ao teto de
        # threads, o ping custa ~uma leva, nao a frota inteira.
        # Vazio no config = "usa o teto de threads". getint estoura com
        # string vazia, entao le como texto e so converte se houver valor.
        _pm = (sv.get("ping_max_por_ciclo", fallback="") or "").strip()
        try:
            self.ping_max = max(1, int(_pm)) if _pm else self.max_thr
        except ValueError:
            log.warning(f"[survey] ping_max_por_ciclo invalido ({_pm!r}); "
                        f"usando o teto de threads ({self.max_thr})")
            self.ping_max = self.max_thr
        # Instrumentacao do ciclo: sem ela, "esta lento" nao tem resposta.
        self._t_ping = 0.0        # segundos gastos em ping no ciclo
        self.perfil  = {}         # ultimo ciclo: total, ping, consulta
        self.banco_path = banco_path or BANCO_SURVEY
        self.inicio     = time.time()
        self.fim_previsto = self.inicio + self.minutos*60
        self.estado     = "coletando"
        self.amostras   = 0
        self.trajetos   = {}          # nome -> [(lat, lon, ts, snr, ruido, vel)]
        self.fixos      = {}          # nome -> (lat, lon)  (sem deslocamento)
        self.medidas    = []          # (lat, lon, rssi, snr, ruido) p/ heatmap
        self.erro       = None
        self.kmz        = None
        self.nome_kmz   = None
        self.survey_id  = None
        self.resumo     = None
        self._parar     = threading.Event()
        self.thread     = threading.Thread(target=self._rodar, daemon=True)

    # ── leitura de um BC ──
    def _abrir_sessoes(self):
        """Autentica uma vez por rádio, no começo do survey.

        Em paralelo: com 150 rádios, autenticar em série levaria minutos e
        o survey começaria com o trajeto já furado.
        """
        col = self.coletor
        porta = getattr(col, "port", 2300)
        role  = getattr(col, "role", "VIEW")
        senha = getattr(col, "password", "")
        for ip in self.ips:
            self.sessoes[ip] = SessaoRadio(ip, porta, role, senha,
                                           timeout=self.timeout_s,
                                           falhas_max=self.falhas_max)
        from concurrent.futures import ThreadPoolExecutor

        def _login(s):
            try: s._abrir()
            except Exception as e: s.ultimo_erro = str(e)   # tenta de novo no ciclo

        with ThreadPoolExecutor(max_workers=self.max_thr) as ex:
            list(ex.map(_login, self.sessoes.values()))
        ok = sum(1 for s in self.sessoes.values() if s.bc is not None)
        log.info(f"[survey] sessoes abertas: {ok}/{len(self.sessoes)}")

    def _dados_do_radio(self, ip):
        """(dados, fonte). Consulta direta; cache do exporter como recurso.

        Retorna fonte='direto' quando falou com o rádio agora e 'cache'
        quando reaproveitou o que o exporter tinha — a amostra guarda isso
        para o relatório não tratar ponto defasado como medição fresca.
        """
        ses = self.sessoes.get(ip)
        if ses is not None and not ses.desistiu:
            try:
                txt = ses.estado_bruto()
                if txt:
                    d = parse_state(txt)
                    # calcular_taxas usa o delta de bytes entre chamadas; a
                    # chave por IP é a mesma do exporter, então as duas
                    # fontes não se atrapalham (ambas só leem e atualizam).
                    return calcular_taxas(ip, d, time.monotonic()), "direto"
            except Exception:
                pass       # já contabilizado em SessaoRadio; cai para o cache

        if not self.cache_ok:
            return None, None
        est = estado(ip, getattr(self.coletor, "falhas_limite", 3),
                          getattr(self.coletor, "manter_s", 300))
        d = getattr(est, "ultima_dados", None)
        return (d, "cache") if d else (None, None)

    def _eleger_pings(self):
        """Escolhe QUAIS radios pingam neste ciclo.

        A cadencia por radio nao basta sozinha. Medido em campo com 159
        radios: o ciclo levava 46 s, e como 46 s > ping_a_cada_s, TODO
        radio estava sempre vencido — o ping voltava a ser de todos, todo
        ciclo, e sozinho respondia por ~90% do tempo (159 x 3 s / 12
        threads = 40 s).

        Entao ha um ORCAMENTO: no maximo `ping_max` radios por ciclo, os
        mais atrasados primeiro. O custo do ping deixa de crescer com o
        tamanho da frota e passa a ser ~uma leva de threads, enquanto a
        posicao — que e o que desenha o rastro — segue no ritmo do
        get_state. Cada radio e pingado a cada (N / ping_max) ciclos.
        """
        if self.ping_a_cada <= 0:
            self._pingar_agora = set(self.ips)      # comportamento antigo
            return
        agora = time.time()
        vencidos = [ip for ip in self.ips
                    if agora - self._ultimo_ping.get(ip, 0.0) >= self.ping_a_cada]
        vencidos.sort(key=lambda ip: self._ultimo_ping.get(ip, 0.0))
        self._pingar_agora = set(vencidos[:max(1, self.ping_max)])
        for ip in self._pingar_agora:
            self._ultimo_ping[ip] = agora

    def _toca_pingar(self, ip):
        """Este radio pinga neste ciclo? Quem decide e `_eleger_pings`."""
        return ip in self._pingar_agora

    def _amostra(self, ip, com_ping=True):
        """Uma amostra completa do equipamento.

        O ping é ICMP do servidor e não existe na BC API, por isso continua
        à parte da consulta de estado.

        Campo sem medição vira None, nunca 0.
        """
        d, fonte = self._dados_do_radio(ip)
        if not d: return None
        s = d.get("sistema") or {}
        if not s.get("gps_fix"): return None

        radios = d.get("radios") or []
        ruidos = [r.get("ruido") for r in radios if r.get("ruido")]

        # Melhor enlace ativo = qualidade da cobertura naquele ponto. Guarda
        # também custo, taxa e a banda do rádio que atendeu — o custo e a
        # taxa do MELHOR enlace são os que descrevem a rota real.
        melhor = None
        peers_ativos = 0
        for r in radios:
            for p in (r.get("peers") or []):
                if not p.get("ativo"): continue
                peers_ativos += 1
                if p.get("snr") is None: continue
                if melhor is None or p["snr"] > melhor["snr"]:
                    melhor = {"snr": p["snr"], "sinal": p.get("sinal"),
                              "custo": p.get("custo"), "taxa": p.get("taxa"),
                              "banda": r.get("freq"),
                              # QUEM serviu. Sem isso não dá para dizer
                              # "esta área é servida pelo ERB-03", que é a
                              # frase que o survey existe para produzir —
                              # nem para detectar handover e ping-pong.
                              "servidor": p.get("ip"),
                              # Interferência e canal do rádio QUE ATENDEU:
                              # a média dos rádios do BC misturaria bandas e
                              # esconderia a que está suja.
                              "interf": r.get("interf_pct"),
                              "canal": r.get("canal")}

        # Vazão real do enlace: delta de bytes ÷ Δt. calcular_taxas() já
        # produziu rx_mbps/tx_mbps por rádio; aqui somamos os rádios.
        vaz = [((r.get("rx_mbps") or 0) + (r.get("tx_mbps") or 0))
               for r in radios if r.get("rx_mbps") is not None]
        vazao = round(sum(vaz), 3) if vaz else None

        # O PING NAO ENTRA EM TODO CICLO — e ele que segurava o rastro.
        #
        # A posicao e o RF (RSSI, SNR, ruido, interferencia) vem do
        # get_state, que e rapido. rtt e perda vem do ICMP, e o ping do
        # Windows NAO tem opcao de intervalo: `ping -n 4` espera ~1 s
        # entre os envios e custa ~3 s por radio. Amarrado ao ciclo, ele
        # impunha um piso de ~3 s a TUDO, inclusive a posicao — que e o
        # que desenha o rastro. Era por isso que o modo continuo continuava
        # espacando as amostras.
        #
        # Agora o ping tem cadencia propria. Nos ciclos sem ping, rtt e
        # perda saem None: nao medido e None, nunca o valor anterior
        # repetido — carregar a ultima leitura para a posicao nova
        # inventaria medicao onde nao houve.
        rtt = perda = None
        if com_ping and self._toca_pingar(ip):
            _t0 = time.time()
            rtt, perda = ping_qualidade(ip, n=self.ping_n)
            self._t_ping += time.time() - _t0

        return {
            "nome": s.get("nome") or ip,
            "lat": s["gps_lat"], "lon": s["gps_lon"],
            "vel": s.get("gps_vel"),
            "snr":   melhor["snr"]   if melhor else None,
            "sinal": melhor["sinal"] if melhor else None,
            "custo": melhor["custo"] if melhor else None,
            "taxa":  melhor["taxa"]  if melhor else None,
            "banda": melhor["banda"] if melhor else None,
            "ruido": (sum(ruidos)/len(ruidos)) if ruidos else None,
            "rtt": rtt, "perda": perda,
            "vazao": vazao, "peers": peers_ativos or None,
            "sats": s.get("gps_sats"), "hdop": s.get("gps_hdop"),
            "fonte": fonte,
            # IP do peer traduzido para o nome do BC quando conhecido: o
            # relatório fala em "ERB-03", não em 10.0.0.7.
            "servidor": self._nome_do_ip(melhor["servidor"]) if melhor else None,
            "interf": melhor["interf"] if melhor else None,
            "canal":  melhor["canal"]  if melhor else None,
        }

    def _nome_do_ip(self, ip):
        if not ip: return None
        nomes = getattr(self.coletor, "nomes", None) or {}
        return nomes.get(ip) or ip

    def _rodar(self):
        from concurrent.futures import ThreadPoolExecutor
        con = None
        try:
            con = banco(self.banco_path)
            nomes = [self.coletor.nomes.get(ip, ip) for ip in self.ips] \
                    if getattr(self.coletor, "nomes", None) else list(self.ips)
            self.survey_id = survey_criar(con, self.rotulo, self.inicio,
                                          self.intervalo, nomes,
                                          alcance=self.alcance_m)
            log.info(f"[survey {self.survey_id}] '{self.rotulo}' — "
                     f"{len(self.ips)} radios, intervalo {self.intervalo}s")

            self._abrir_sessoes()

            # O ping é ~1 s por equipamento e a consulta de estado é a que
            # pesa no rádio; o teto de threads vem do config para o operador
            # poder baixá-lo se a malha reclamar.
            trab = min(self.max_thr, max(1, len(self.ips)))
            # Um pool só para todo o survey: recriar por ciclo jogava fora as
            # threads a cada 10 s sem necessidade.
            self._pool = ex = ThreadPoolExecutor(max_workers=trab)
            ciclo_ant = None
            while not self._parar.is_set() and time.time() < self.fim_previsto:
                ciclo = time.time()
                # O intervalo EFETIVO é a distância entre inícios de ciclo,
                # não o pedido. É ele que descreve a resolução do trajeto, e
                # é ele que vai para o relatório.
                if ciclo_ant is not None:
                    self.efetivo_s = round(ciclo - ciclo_ant, 1)
                    # Onde o ciclo gastou o tempo. "Esta espacado" sem isto
                    # e chute; com isto a pagina diz se foi o ping, a
                    # consulta ou um radio em timeout.
                    self.perfil = {
                        "ciclo_s": self.efetivo_s,
                        "ping_s": round(self._t_ping, 1),
                        "consulta_s": round(max(0.0, self.efetivo_s
                                                - self._t_ping), 1),
                    }
                self._t_ping = 0.0
                ciclo_ant = ciclo
                self._eleger_pings()
                res = list(ex.map(
                    lambda ip: self._seguro(ip, self.com_ping), self.ips))
                linhas = []
                for a in res:
                    if not a: continue
                    if a.get("fonte") == "cache": self.n_cache += 1
                    elif a.get("fonte") == "direto": self.n_direto += 1
                    nome = a["nome"]
                    pt = (a["lat"], a["lon"], ciclo, a["snr"], a["ruido"],
                          a["vel"], a["sinal"])
                    ult = self.trajetos.get(nome, [])
                    # ponto repetido (parado) não entra no trajeto, mas conta
                    if not ult or abs(ult[-1][0]-a["lat"]) > 1e-6 \
                               or abs(ult[-1][1]-a["lon"]) > 1e-6:
                        self.trajetos.setdefault(nome, []).append(pt)
                    if a["sinal"] is not None or a["snr"] is not None:
                        self.medidas.append((a["lat"], a["lon"], a["sinal"],
                                             a["snr"], a["ruido"]))
                    linhas.append(dict(a, radio=nome, ts=ciclo))
                    self.amostras += 1
                # Grava a cada ciclo, não no fim: se o processo cair no meio
                # de um survey de 8 h, o que já foi medido continua valendo.
                if linhas: amostras_gravar(con, self.survey_id, linhas)
                # Ciclo mais longo que o intervalo: segue para o próximo
                # tique sem esperar. Acumular fila só afastaria os ciclos
                # cada vez mais — melhor entregar o que dá e dizer a
                # resolução real em intervalo_efetivo_s.
                espera = self.intervalo - (time.time() - ciclo)
                if espera <= 0:
                    log.debug(f"[survey {self.survey_id}] ciclo estourou o "
                              f"intervalo ({-espera:.1f}s a mais)")
                    continue
                self._parar.wait(espera)

            # separa fixos (sem deslocamento real) dos móveis — pelo dado,
            # não pelo nome: um ERM rebocado vira rota, e é isso que importa
            moveis = {}
            for nome, pts in self.trajetos.items():
                if len(pts) < 2:
                    self.fixos[nome] = (pts[0][0], pts[0][1]) if pts else None
                    continue
                desl = max(abs(pts[0][0]-p[0]) + abs(pts[0][1]-p[1]) for p in pts)
                if desl > 0.0003:      # ~30 m
                    moveis[nome] = pts
                else:
                    self.fixos[nome] = (pts[0][0], pts[0][1])
            self.trajetos = moveis

            self.estado = "gerando"
            todas = survey_amostras(con, self.survey_id)
            self.resumo = survey_resumo(todas)
            calib = calibrar_todas_bandas(todas, self.fixos)
            survey_fechar(con, self.survey_id, time.time(), self.resumo, calib,
                          n_moveis=len(self.trajetos), n_fixos=len(self.fixos),
                          intervalo_efetivo_s=self.efetivo_s)
            self._montar_kmz()
            self.estado = "pronto"
            log.info(f"[survey {self.survey_id}] pronto: "
                     f"{self.resumo['amostras']} amostras, "
                     f"{self.resumo['radios']} equipamentos")
        except Exception as e:
            self.erro = str(e); self.estado = "erro"
            log.error(f"[captura {self.id[:8]}] falhou: {e}")
        finally:
            # As sessões seguram socket no rádio: fechar mesmo se o survey
            # abortou no meio, senão o BC fica com conexões penduradas.
            for s in self.sessoes.values():
                try: s.fechar()
                except Exception: pass
            pool = getattr(self, "_pool", None)
            if pool is not None:
                try: pool.shutdown(wait=False)
                except Exception: pass
            if con:
                try: con.close()
                except Exception: pass

    def _seguro(self, ip, com_ping):
        try:    return self._amostra(ip, com_ping)
        except Exception: return None

    def _montar_kmz(self):
        csv_tmp = None
        if self.medidas:
            import csv as _csv, tempfile
            csv_tmp = Path(tempfile.mkdtemp()) / "captura.csv"
            with open(csv_tmp, "w", newline="", encoding="utf-8") as f:
                w = _csv.writer(f)
                w.writerow(["Latitude","Longitude","Signal Strength","SNR","Noise"])
                for la, lo, sinal, snr, ruido in self.medidas:
                    w.writerow([f"{la:.7f}", f"{lo:.7f}",
                                "" if sinal is None else f"{sinal:.1f}",
                                "" if snr   is None else f"{snr:.1f}",
                                "" if ruido is None else f"{ruido:.1f}"])
        bcs_fixos = [(n, c[0], c[1]) for n, c in self.fixos.items() if c]
        dados, nome = gerar_kmz_survey(
            f"Survey_{datetime.now():%Y%m%d_%H%M}.kmz",
            csv_path=str(csv_tmp) if csv_tmp else None,
            bandas=("2.4 GHz",),           # captura ao vivo: uma malha só
            rotas_gps=self.trajetos,
            bcs_gps=bcs_fixos, cobertura=bool(bcs_fixos),
            alcance_m=self.alcance_m)
        self.kmz, self.nome_kmz = dados, nome

    def status(self):
        agora = time.time()
        total = max(1.0, self.fim_previsto - self.inicio)
        pct = min(100.0, 100.0*(agora - self.inicio)/total)
        vivos = sum(1 for s in self.sessoes.values() if not s.desistiu)
        return {"id": self.id, "estado": self.estado, "erro": self.erro,
                "pct": round(pct if self.estado == "coletando" else 100.0, 1),
                "restante_s": max(0, int(self.fim_previsto - agora)),
                "amostras": self.amostras,
                "equipamentos": len(self.trajetos),
                "fixos": len(self.fixos),
                "medidas": len(self.medidas),
                "pronto": self.estado == "pronto",
                # Resolução real do trajeto. Divergindo do pedido, é este
                # que vale — a página mostra os dois lado a lado.
                "intervalo_s": self.intervalo,
                "intervalo_efetivo_s": self.efetivo_s,
                "perfil": self.perfil,
                "radios_ativos": vivos,
                "radios_descartados": len(self.sessoes) - vivos,
                "amostras_direto": self.n_direto,
                "amostras_cache": self.n_cache,
                "detalhe": {n: len(v) for n, v in sorted(self.trajetos.items())[:40]}}

def rotas_do_prometheus(prom, ini_dt, fim_dt, passo="2m", max_equip=60):
    """Trajetos dos equipamentos a partir do GPS publicado pelo exporter."""
    ini_s = ini_dt.timestamp(); fim_s = min(fim_dt.timestamp(), time.time())
    lat_s = prom.range("rajant_gps_lat", ini_s, fim_s, passo)
    lon_s = prom.range("rajant_gps_lon", ini_s, fim_s, passo)
    lons = {}
    for r in lon_s:
        bc = r["metric"].get("bc", "?")
        lons[bc] = {int(float(t)): float(v) for t, v in r["values"]}
    rotas = {}
    for r in lat_s:
        bc = r["metric"].get("bc", "?")
        if bc not in lons: continue
        pts = []
        for t, v in r["values"]:
            ts = int(float(t))
            if ts in lons[bc]:
                pts.append((float(v), lons[bc][ts], ts))
        # descarta parados (repetidoras fixas entram como ponto, não rota)
        if len(pts) >= 2:
            dl = max(abs(pts[0][0]-p[0]) + abs(pts[0][1]-p[1]) for p in pts)
            if dl > 0.0004:            # ~40 m de deslocamento
                rotas[bc] = pts
    return dict(list(rotas.items())[:max_equip])

# chave de imagem -> (grandeza do KMZ, pasta a deixar visível no Earth)
_KMZ_DA_CHAVE = {
    "mapa_rede":    (None,     "BreadCrumbs + Rotas"),
    "rota":         ("sinal",  "Rotas"),
    "cobertura":    (None,     "ZONAS-PROBLEMA"),
    "medido_sinal": ("sinal",  "Medições"),
    "heatmap_rssi": ("sinal",  "Medições"),
    "medido_snr":   ("snr",    "Medições"),
    "heatmap_snr":  ("snr",    "Medições"),
    "medido_ruido": ("ruido",  "Medições"),
    "heatmap_ruido": ("ruido", "Medições"),
    "medido_perda": ("perda",  "Medições"),
    "heatmap_perda": ("perda", "Medições"),
    "medido_rtt":   ("rtt",    "Medições"),
    "heatmap_latencia": ("rtt", "Medições"),
    "medido_interf": ("interf", "Medições"),
}


def _moldura_para_colar(s, left, top, w, h, chave, banda=None):
    """Retângulo tracejado dizendo qual KMZ abrir e qual pasta ligar."""
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
    from pptx.enum.dml import MSO_LINE_DASH_STYLE

    campo, pasta = _KMZ_DA_CHAVE.get(chave, (None, "Medições"))
    suf = f"_{banda.replace(' ', '').replace('.', '')}" if banda else ""
    arq = f"Survey_*_todas{suf}.kmz"
    # A grandeza é ABA dentro do arquivo, não arquivo separado: a moldura
    # tem de dizer qual aba marcar, senão o operador liga todas e os
    # pontos se empilham.
    aba = (limite_de(campo) or (None, None, None, None))[3] if campo else None

    cx = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, left, top, w, h)
    cx.fill.solid(); cx.fill.fore_color.rgb = RGBColor.from_string("1A2744")
    cx.line.color.rgb = RGBColor.from_string("4FA3F7")
    cx.line.width = Pt(1.5)
    cx.line.dash_style = MSO_LINE_DASH_STYLE.DASH
    cx.shadow.inherit = False

    tf = cx.text_frame; tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    linhas = [("COLAR AQUI O PRINT DO GOOGLE EARTH", 13, True, "FFFFFF"),
              (arq, 11, True, "4FA3F7"),
              ((f"aba: {aba}   ·   " if aba else "") + f"camada: {pasta}"
               + (f"   ·   {banda}" if banda else ""), 10, False, "A0B0CC")]
    for i, (txt, tam, neg, cor) in enumerate(linhas):
        par = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        par.alignment = PP_ALIGN.CENTER
        r = par.add_run(); r.text = txt
        r.font.name = "Calibri"; r.font.size = Pt(tam); r.font.bold = neg
        r.font.color.rgb = RGBColor.from_string(cor)
    return cx


def inserir_imagens_survey(p, imgs, banda=None, modo="imagem"):
    """Preenche os slides de survey.

    modo="imagem"  — insere os PNGs gerados (comportamento antigo);
    modo="moldura" — deixa a moldura vazia com a instrução de qual KMZ
                     abrir e qual camada ligar, para o print ser colado
                     à mão. É o fluxo de quem monta o slide com a imagem
                     de satélite do Google Earth, que o PNG não tem.
    """
    from pptx.util import Inches
    # Título do slide -> chaves de imagem aceitas, na ordem de preferência.
    # Duas famílias de chave convivem: `medido_*`/`cobertura`, do gerador
    # novo (mapas em três camadas), e `heatmap_*`, do gerador antigo que
    # parte do CSV de campanha manual. Aceitar as duas evita que o slide
    # fique vazio dependendo de qual caminho gerou as imagens.
    mapa_slide = {
        "Mapa da Rede":                 ["mapa_rede"],
        "Rota Percorrida":              ["rota"],
        "Cobertura Estimada da Mina":   ["cobertura"],
        "Intensidade de Sinal (RSSI)":  ["medido_sinal", "heatmap_rssi"],
        "Relação Sinal/Ruído (SNR)":    ["medido_snr",   "heatmap_snr"],
        "Noise Floor":                  ["medido_ruido", "heatmap_ruido"],
        "Packet Loss":                  ["medido_perda", "heatmap_perda"],
        "Latência (RTT)":               ["medido_rtt",   "heatmap_latencia"],
    }
    inseridas = 0
    for titulo, chaves in mapa_slide.items():
        # O Mapa da Rede é um só, não por banda. Sem esta guarda o passe
        # de cada banda achava o slide pelo título sem sufixo e o
        # reescrevia — a moldura acabava rotulada com a última banda, o
        # que mandaria o operador colar ali o print da malha errada.
        if titulo == "Mapa da Rede" and banda:
            continue
        alvo = titulo if not banda else f"{titulo} — {banda}"
        _, s = _slide_por_titulo(p, alvo)
        if s is None: _, s = _slide_por_titulo(p, titulo)
        chave = next((c for c in chaves if c in imgs), None)
        if modo == "moldura" and chave is None:
            # Sem imagem gerada, a moldura ainda precisa existir: é ela
            # que diz qual print colar ali.
            chave = chaves[0]
        if s is None or chave is None: continue

        # O alvo pode ser de duas naturezas, conforme o template:
        #  1) uma caixa de texto "COLAR ... AQUI" (templates de rascunho);
        #  2) uma IMAGEM placeholder já posicionada (template do cliente).
        # Preferimos a maior imagem do slide: é a moldura do mapa, e não o
        # logotipo do cabeçalho.
        moldura = None
        for sh in s.shapes:
            if sh.has_text_frame and "COLAR" in (sh.text_frame.text or "").upper():
                moldura = sh; break
        if moldura is None:
            fotos = [sh for sh in s.shapes if sh.shape_type == 13]   # PICTURE
            if fotos:
                moldura = max(fotos, key=lambda sh: (sh.width or 0) * (sh.height or 0))
        if moldura is None: continue

        left, top, w, h = moldura.left, moldura.top, moldura.width, moldura.height
        moldura._element.getparent().remove(moldura._element)

        if modo == "moldura":
            # Não insere imagem: deixa a moldura preparada para o print do
            # Google Earth, dizendo QUAL arquivo abrir. Seis KMZ na pasta
            # sem essa indicação viram adivinhação na hora de montar.
            _moldura_para_colar(s, left, top, w, h, chave, banda)
            inseridas += 1
            continue

        # Encaixa DENTRO da moldura preservando a proporção da imagem.
        # Forçar largura e altura esticava o mapa, e mapa esticado mente
        # sobre distância: dois pontos a 300 m parecem a 500 m conforme a
        # direção. Sobra vira margem, centralizada.
        pic = s.shapes.add_picture(imgs[chave], left, top, width=w)
        if pic.height > h:
            fator = h / float(pic.height)
            pic.height, pic.width = h, int(pic.width * fator)
        pic.left = left + max(0, (w - pic.width) // 2)
        pic.top  = top  + max(0, (h - pic.height) // 2)
        inseridas += 1
    return inseridas

# ──────────────────────────────────────────────────────────────
# SLIDES-MODELO DE SITE SURVEY (estrutura do relatório Komatsu/MTS)
# Slides VAZIOS p/ preenchimento manual: tabelas, molduras de mapa
# e gráficos NATIVOS com as faixas dos histogramas do survey —
# clique com o botão direito no gráfico > "Editar Dados" e digite
# os percentuais medidos. Valores atuais são apenas exemplo.
# Gerados uma única vez no template: rajant_monitor.py --montar-template-survey
# ──────────────────────────────────────────────────────────────
def construir_slides_survey(p, banda=None):
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE

    C_BG   = RGBColor.from_string("0D2052")
    C_CARD = RGBColor.from_string("1A2744")
    C_HDR  = RGBColor.from_string("1A3A7A")
    C_ZEB  = RGBColor.from_string("152238")
    C_TXT  = RGBColor.from_string("C8D0E0")
    C_SUB  = RGBColor.from_string("A0B0CC")
    C_BR   = RGBColor.from_string("FFFFFF")
    C_BORDA= RGBColor.from_string("2A3A5C")
    # Degradê do survey (vermelho → verde), mesmo racional do heatmap
    GRAD = ["C0392B","E74C3C","E67E22","F1C40F","9ACD32","27AE60","1E8449","145A32"]
    def grad(n, invertido=False):
        idx = [round(i*(len(GRAD)-1)/(max(n-1,1))) for i in range(n)]
        cores = [GRAD[i] for i in idx]
        return list(reversed(cores)) if invertido else cores

    layout = p.slide_masters[0].slide_layouts[0]
    criados = []
    SUF = f" — {banda}" if banda else ""

    def novo(titulo, subtitulo):
        titulo = titulo + SUF
        s = p.slides.add_slide(layout)
        s.background.fill.solid(); s.background.fill.fore_color.rgb = C_BG
        for ph in list(s.placeholders):
            ph._element.getparent().remove(ph._element)
        tb = s.shapes.add_textbox(Inches(0.55), Inches(0.25), Inches(11.2), Inches(0.6))
        r = tb.text_frame.paragraphs[0].add_run(); r.text = titulo
        r.font.name="Calibri"; r.font.size=Pt(25); r.font.bold=True; r.font.color.rgb=C_BR
        st = s.shapes.add_textbox(Inches(0.55), Inches(0.85), Inches(11.5), Inches(0.35))
        r2 = st.text_frame.paragraphs[0].add_run(); r2.text = subtitulo
        r2.font.name="Calibri"; r2.font.size=Pt(12); r2.font.color.rgb=C_SUB
        b = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                               Inches(12.0), Inches(0.28), Inches(0.95), Inches(0.32))
        b.fill.solid(); b.fill.fore_color.rgb = C_HDR; b.line.fill.background()
        rb = b.text_frame.paragraphs[0].add_run(); rb.text = "◂ MENU"
        rb.font.name="Calibri"; rb.font.size=Pt(10); rb.font.bold=True; rb.font.color.rgb=C_BR
        b.text_frame.paragraphs[0].alignment = PP_ALIGN.CENTER
        try: b.click_action.target_slide = p.slides[1]
        except Exception: pass
        criados.append(s)
        return s

    def texto(s, x, y, w, h, txt, tam=11, bold=False, cor=None, italico=False):
        tb = s.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
        tf = tb.text_frame; tf.word_wrap = True
        for i, linha in enumerate(txt.split("\n")):
            pr = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            r = pr.add_run(); r.text = linha
            r.font.name="Calibri"; r.font.size=Pt(tam); r.font.bold=bold
            r.font.italic=italico; r.font.color.rgb = cor or C_TXT
        return tb

    def moldura_mapa(s, x, y, w, h, rotulo="COLAR MAPA / IMAGEM AQUI"):
        m = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                               Inches(x), Inches(y), Inches(w), Inches(h))
        m.fill.solid(); m.fill.fore_color.rgb = C_CARD
        m.line.color.rgb = C_BORDA; m.line.width = Pt(1); m.line.dash_style = 3  # tracejada
        m.shadow.inherit = False
        pr = m.text_frame.paragraphs[0]; pr.alignment = PP_ALIGN.CENTER
        r = pr.add_run(); r.text = rotulo
        r.font.name="Calibri"; r.font.size=Pt(12); r.font.italic=True; r.font.color.rgb=C_SUB
        return m

    def tabela(s, x, y, w, cab, linhas, tam=10, altura_linha=0.32):
        gf = s.shapes.add_table(len(linhas)+1, len(cab), Inches(x), Inches(y),
                                Inches(w), Inches(0.4 + altura_linha*len(linhas)))
        t = gf.table; t.first_row = False; t.horz_banding = False
        for j, txt2 in enumerate(cab):
            cel = t.cell(0, j); cel.fill.solid(); cel.fill.fore_color.rgb = C_HDR
            pr = cel.text_frame.paragraphs[0]; pr.alignment = PP_ALIGN.CENTER
            r = pr.add_run(); r.text = str(txt2)
            r.font.name="Calibri"; r.font.size=Pt(tam); r.font.bold=True; r.font.color.rgb=C_BR
        for i, lin in enumerate(linhas, start=1):
            for j, v in enumerate(lin):
                cel = t.cell(i, j); cel.fill.solid()
                cel.fill.fore_color.rgb = C_ZEB if i % 2 == 0 else C_CARD
                pr = cel.text_frame.paragraphs[0]
                pr.alignment = PP_ALIGN.LEFT if j in (0,1) and len(cab) > 3 else PP_ALIGN.CENTER
                r = pr.add_run(); r.text = str(v)
                r.font.name="Calibri"; r.font.size=Pt(tam); r.font.color.rgb=C_TXT
        return t

    def grafico_hist(s, x, y, w, h, titulo, faixas, exemplo, invertido=False):
        """Histograma de distribuição (% da área) com degradê por faixa.
        Gráfico NATIVO: botão direito > Editar Dados para inserir os valores."""
        cd = CategoryChartData(); cd.categories = faixas
        cd.add_series("% da área", exemplo)
        gf = s.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED,
                                Inches(x), Inches(y), Inches(w), Inches(h), cd)
        ch = gf.chart
        ch.font.name = "Calibri"; ch.font.size = Pt(9); ch.font.color.rgb = C_TXT
        ch.has_legend = False
        ch.has_title = True
        ch.chart_title.text_frame.text = titulo
        tr = ch.chart_title.text_frame.paragraphs[0].runs[0]
        tr.font.size = Pt(11); tr.font.bold = True; tr.font.color.rgb = C_BR
        ch.plots[0].gap_width = 30
        # fundo transparente
        from pptx.oxml.ns import qn
        from lxml import etree
        cs = ch._chartSpace
        spPr = cs.find(qn("c:spPr"))
        if spPr is None: spPr = etree.SubElement(cs, qn("c:spPr"))
        else:
            for chx in list(spPr): spPr.remove(chx)
        etree.SubElement(spPr, qn("a:noFill"))
        ln = etree.SubElement(spPr, qn("a:ln")); etree.SubElement(ln, qn("a:noFill"))
        try:
            ch.value_axis.has_major_gridlines = True
            gl = ch.value_axis.major_gridlines
            gl.format.line.color.rgb = C_BORDA; gl.format.line.width = Pt(0.5)
            ch.value_axis.maximum_scale = 100; ch.value_axis.minimum_scale = 0
        except Exception: pass
        for eixo in ("category_axis","value_axis"):
            try:
                ax = getattr(ch, eixo)
                ax.tick_labels.font.size = Pt(8)
                ax.tick_labels.font.color.rgb = C_SUB
                ax.format.line.color.rgb = C_BORDA
            except Exception: pass
        cores = grad(len(faixas), invertido)
        try:
            serie = ch.series[0]
            for i, pt in enumerate(serie.points):
                pt.format.fill.solid()
                pt.format.fill.fore_color.rgb = RGBColor.from_string(cores[i])
        except Exception: pass
        # rótulos de dados
        try:
            pl = ch.plots[0]; pl.has_data_labels = True
            pl.data_labels.font.size = Pt(8); pl.data_labels.font.color.rgb = C_TXT
            pl.data_labels.number_format = "0.#\"%\""; pl.data_labels.number_format_is_linked = False
        except Exception: pass
        return ch

    # ═══ 1. Metodologia e Requisitos ═══
    s = novo("5. Site Survey — Metodologia e Requisitos",
             "Base: survey passivo (adaptador -99 dBm) + ativo (rádio Rajant configurado como a frota) + GPS")
    texto(s, 0.55, 1.4, 6.0, 0.3, "METODOLOGIA", 11, True, C_SUB)
    c1 = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(0.55), Inches(1.75), Inches(6.0), Inches(4.6))
    c1.fill.solid(); c1.fill.fore_color.rgb = C_CARD; c1.line.color.rgb = C_BORDA; c1.shadow.inherit = False
    # Não é mais survey com veículo dedicado e kit: quem mede são os
    # rádios da própria frota, continuamente. Período, número de rádios e
    # intervalo efetivo são preenchidos por preencher_slides_survey().
    texto(s, 0.8, 1.95, 5.6, 4.2,
          "•  Survey contínuo pelos rádios da própria frota — sem\n"
          "    veículo dedicado nem kit de medição.\n"
          "•  Cada rádio é consultado diretamente pela BC API no\n"
          "    intervalo escolhido; o GPS do próprio equipamento\n"
          "    georreferencia cada amostra.\n"
          "•  Medidos por amostra: RSSI, SNR, ruído, custo e taxa do\n"
          "    melhor enlace, latência e perda (ICMP).\n"
          "•  Período da coleta: ____ / ____ a ____ / ____\n"
          "•  Rádios participantes: ____   ·   intervalo efetivo: ____ s", 11)
    texto(s, 6.9, 1.4, 6.0, 0.3, "REQUISITOS MODULAR MINING", 11, True, C_SUB)
    tabela(s, 6.9, 1.75, 6.05,
           ["Parâmetro","Requisito","Tipo"],
           [["Latency","< 100 ms","Ativo"],
            ["Packet loss","< 2%","Ativo"],
            ["Sequential packet loss","≤ 5 s","Ativo"],
            ["Rate of sequential loss","< 0,001 s","Ativo"],
            ["Throughput","> 1 Mbps / equip.","Ativo"],
            ["RSSI","> -75 dBm","Passivo"],
            ["SNR","> 20 dB","Passivo"]], tam=10)
    texto(s, 6.9, 4.6, 6.0, 1.6,
          "Fora dos requisitos, as aplicações Dispatch / ProVision /\n"
          "MineCare degradam: beacons e pacotes perdidos, posições\n"
          "GPS atrasadas e otimização com dados inconsistentes.", 10, cor=C_SUB, italico=True)

    # ═══ 2. Rota Percorrida ═══
    # Sem o bloco de ÁREAS PERCORRIDAS: os campos manuais de localidade não
    # são usados, e o mapa fica com a largura inteira do slide — a rota
    # numa cava tem muito mais a dizer que cinco linhas em branco.
    s = novo("5. Site Survey — Rota Percorrida",
             "Banda: ______  |  Data: ____/____/____  |  Trajeto medido pelos rádios da frota")
    moldura_mapa(s, 0.55, 1.4, 12.25, 5.3, "MAPA DA ROTA")

    # ═══ 2b. Cobertura estimada (a partir das posições dos BreadCrumbs) ═══
    s = novo("5. Site Survey — Cobertura Estimada da Mina",
             "RSSI previsto em toda a área a partir das posições reais dos BreadCrumbs "
             "(modelo log-distância) — mostra buracos onde o survey não passou")
    moldura_mapa(s, 0.55, 1.4, 7.6, 5.3, "COLAR MAPA DE COBERTURA AQUI")
    texto(s, 8.4, 1.6, 4.4, 0.3, "LEITURA", 11, True, C_SUB)
    texto(s, 8.4, 2.0, 4.4, 3.2,
          "Verde: RSSI ≥ -75 dBm (requisito Modular)\n"
          "Amarelo: zona limítrofe\n"
          "Vermelho: abaixo do requerido\n\n"
          "Linha branca: fronteira de -75 dBm\n"
          "Triângulos: BreadCrumbs (ERB/ERM)\n\n"
          "Estimativa de planejamento — usar junto\n"
          "com as medições de campo, não no lugar\n"
          "delas.", 10)
    texto(s, 8.4, 5.4, 4.4, 1.0,
          "Área ≥ -75 dBm: ______ %\nBuracos identificados: ____________", 11, True)

    # ═══ 3. RSSI ═══
    s = novo("5. Site Survey — Intensidade de Sinal (RSSI)",
             "Requisito: RSSI > -75 dBm  |  edite os dados do gráfico com os percentuais do survey")
    moldura_mapa(s, 0.55, 1.4, 6.0, 4.6, "COLAR HEATMAP RSSI AQUI")
    grafico_hist(s, 6.8, 1.4, 6.1, 4.6, "SIGNAL STRENGTH (dBm) — % da área",
                 ["-100–-75","-75–-70","-70–-65","-65–-60","-60–-55","-55–-50","-50–-45","≥ -45"],
                 [2.4, 0.7, 0.4, 0.6, 22.1, 69.3, 4.1, 0.3])
    texto(s, 0.55, 6.3, 12.3, 0.4,
          "____ % da área dentro dos valores requeridos (RSSI > -75 dBm).", 13, True, C_BR)

    # ═══ 4. SNR ═══
    s = novo("5. Site Survey — Relação Sinal/Ruído (SNR)",
             "Requisito: SNR > 20 dB  |  edite os dados do gráfico com os percentuais do survey")
    moldura_mapa(s, 0.55, 1.4, 6.0, 4.6, "COLAR HEATMAP SNR AQUI")
    grafico_hist(s, 6.8, 1.4, 6.1, 4.6, "SIGNAL TO NOISE RATIO (dB) — % da área",
                 ["0–20","20–25","25–30","30–35","35–40","≥ 40"],
                 [3.1, 0.4, 0.6, 22.1, 69.3, 4.4])
    texto(s, 0.55, 6.3, 12.3, 0.4,
          "____ % da área dentro dos valores requeridos (SNR > 20 dB).", 13, True, C_BR)

    # ═══ 5. Noise Floor ═══
    s = novo("5. Site Survey — Noise Floor",
             "Piso de ruído: limpo ≤ -100 dBm | moderado -99 a -90 | crítico > -90 dBm")
    moldura_mapa(s, 0.55, 1.4, 6.0, 4.6, "COLAR HEATMAP DE RUÍDO AQUI")
    grafico_hist(s, 6.8, 1.4, 6.1, 4.6, "NOISE (dBm) — % da área",
                 ["≤ -100","-100–-95","-95–-90","-90–-85","≥ -85"],
                 [10, 30, 40, 15, 5], invertido=True)
    texto(s, 0.55, 6.3, 12.3, 0.4,
          "Situação: ______________________________________________", 13, True, C_BR)

    # ═══ 6. Interferência de canal ═══
    s = novo("5. Site Survey — Interferência de Canal",
             "Sobreposição co-channel — colar telas do analisador de espectro (2.4/5 GHz)")
    moldura_mapa(s, 0.55, 1.4, 6.0, 4.6, "COLAR MAPA DE APs POR PONTO AQUI")
    moldura_mapa(s, 6.8, 1.4, 6.1, 3.4, "COLAR ESPECTRO / LISTA DE RÁDIOS AQUI")
    texto(s, 6.8, 5.0, 6.1, 0.3, "OBSERVAÇÕES", 11, True, C_SUB)
    texto(s, 6.8, 5.35, 6.1, 1.2,
          "____________________________________________\n"
          "____________________________________________", 11)

    # ═══ 7. Packet Loss ═══
    s = novo("5. Site Survey — Packet Loss",
             "Requisito: perda < 2%  |  edite os dados do gráfico com os percentuais do survey")
    moldura_mapa(s, 0.55, 1.4, 6.0, 4.6, "COLAR HEATMAP DE PERDA AQUI")
    grafico_hist(s, 6.8, 1.4, 6.1, 4.6, "PACKET LOSS (%) — % da área",
                 ["≤ 2","2–4","4–6","6–8","8–10","10–30","30–100"],
                 [90.8, 1.0, 0.5, 0.5, 0.5, 1.0, 5.7], invertido=True)
    texto(s, 0.55, 6.3, 12.3, 0.4,
          "Perda de pacotes acima de 2% em ____ % da área percorrida.", 13, True, C_BR)

    # ═══ 8. Latência ═══
    s = novo("5. Site Survey — Latência (RTT)",
             "Requisito: < 100 ms  |  edite os dados do gráfico com os percentuais do survey")
    moldura_mapa(s, 0.55, 1.4, 6.0, 4.6, "COLAR HEATMAP DE LATÊNCIA AQUI")
    grafico_hist(s, 6.8, 1.4, 6.1, 4.6, "ROUND-TRIP TIME (ms) — % da área",
                 ["0–10","10–20","20–30","30–50","50–70","70–90","90–100","100–200","200–500","≥ 500"],
                 [9, 28, 17, 9, 13, 8, 2, 10, 3, 1], invertido=True)
    texto(s, 0.55, 6.3, 12.3, 0.4,
          "Latência acima de 100 ms em ____ % da área.", 13, True, C_BR)

    # ═══ 9. Throughput por ponto ═══
    s = novo("5. Site Survey — Throughput por Ponto de Teste",
             "iperf3 em pontos fixos  |  requisito: > 1 Mbps por equipamento da frota")
    tabela(s, 0.55, 1.4, 6.7,
           ["Teste","Área","Taxa (Mbps)"],
           [[str(i), "________________________", "____"] for i in range(1, 11)],
           tam=10, altura_linha=0.34)
    moldura_mapa(s, 7.5, 1.4, 5.4, 4.6, "COLAR MAPA DOS PONTOS DE TESTE AQUI")
    texto(s, 0.55, 6.5, 12.3, 0.4,
          "Pontos abaixo de 1 Mbps: ______________________________  (fora do requerido p/ Dispatch / MineCare / ProVision)",
          12, True, C_BR)

    # ═══ 10. No Talk's KPI ═══
    s = novo("5. Site Survey — No Talk's KPI",
             "Mensagens sem resposta por equipamento  |  período da coleta: ____/____ a ____/____")
    tabela(s, 0.55, 1.4, 5.9,
           ["EQMPTID","Enviados","No Talks","Perf. (%)"],
           [["______","____","____","____"] for _ in range(12)],
           tam=9, altura_linha=0.29)
    # gráfico barras empilhadas OK × Falha (cores do relatório Komatsu)
    cd = None
    from pptx.chart.data import CategoryChartData as _CCD
    cd = _CCD(); cd.categories = [f"EQ-{i:02d}" for i in range(1, 9)]
    cd.add_series("OK",        [95, 92, 88, 97, 73, 39, 23, 12])
    cd.add_series("No Talk",   [5, 8, 12, 3, 27, 61, 77, 88])
    gf = s.shapes.add_chart(XL_CHART_TYPE.COLUMN_STACKED_100,
                            Inches(6.8), Inches(1.4), Inches(6.1), Inches(4.6), cd)
    ch = gf.chart
    ch.font.name="Calibri"; ch.font.size=Pt(9); ch.font.color.rgb=C_TXT
    ch.has_title = True; ch.chart_title.text_frame.text = "NO TALK POR EQUIPAMENTO (%)"
    tr = ch.chart_title.text_frame.paragraphs[0].runs[0]
    tr.font.size=Pt(11); tr.font.bold=True; tr.font.color.rgb=C_BR
    ch.has_legend = True
    from pptx.enum.chart import XL_LEGEND_POSITION
    ch.legend.position = XL_LEGEND_POSITION.BOTTOM; ch.legend.include_in_layout = False
    ch.series[0].format.fill.solid(); ch.series[0].format.fill.fore_color.rgb = RGBColor.from_string("1A3A7A")
    ch.series[1].format.fill.solid(); ch.series[1].format.fill.fore_color.rgb = RGBColor.from_string("2980B9")
    from pptx.oxml.ns import qn as _qn
    from lxml import etree as _et
    cs = ch._chartSpace
    spPr = cs.find(_qn("c:spPr"))
    if spPr is None: spPr = _et.SubElement(cs, _qn("c:spPr"))
    else:
        for chx in list(spPr): spPr.remove(chx)
    _et.SubElement(spPr, _qn("a:noFill"))
    ln = _et.SubElement(spPr, _qn("a:ln")); _et.SubElement(ln, _qn("a:noFill"))
    texto(s, 0.55, 6.55, 12.3, 0.5,
          "Equipamentos com No Talk > 15% (verificar hardware): ______________________________________________",
          12, True, C_BR)

    # ═══ 11. Análise, Recomendações e Conclusão ═══
    s = novo("5. Site Survey — Análise, Recomendações e Conclusão",
             "Síntese do survey — preencher após consolidação dos dados")
    for x, titulo2 in [(0.55,"ANÁLISE DOS RESULTADOS"), (4.75,"RECOMENDAÇÕES"), (8.95,"CONCLUSÃO")]:
        texto(s, x, 1.4, 4.0, 0.3, titulo2, 11, True, C_SUB)
        c = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                               Inches(x), Inches(1.75), Inches(3.98), Inches(5.0))
        c.fill.solid(); c.fill.fore_color.rgb = C_CARD
        c.line.color.rgb = C_BORDA; c.shadow.inherit = False
        texto(s, x+0.2, 1.95, 3.6, 4.6,
              "•  ______________________\n•  ______________________\n"
              "•  ______________________\n•  ______________________\n"
              "•  ______________________", 11)

    # ── Reposiciona o bloco inteiro dentro da seção de Site Survey ──
    # Para múltiplas bandas, cada bloco entra depois do bloco anterior.
    idx = None
    for ancora in ("Análise, Recomendações e Conclusão", "Mapa de Calor"):
        i, _ = _slide_por_titulo(p, ancora)
        if i is not None and p.slides[i] not in criados:
            idx = i; break
    if idx is None:
        idx, _ = _slide_por_titulo(p, "Mapa de Calor")
    if idx is not None:
        lst = p.slides._sldIdLst
        els = list(lst)[-len(criados):]
        for e in els: lst.remove(e)
        for k, e in enumerate(els):
            lst.insert(idx + 1 + k, e)
    return len(criados)


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


def _txt_anglo(s, x, y, w, h, texto, tam=11, negrito=False, cor=None,
               alinha=None, italico=False):
    from pptx.util import Inches, Pt
    tb = s.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame; tf.word_wrap = True
    for i, linha in enumerate(str(texto).split("\n")):
        par = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        if alinha is not None: par.alignment = alinha
        r = par.add_run(); r.text = linha
        r.font.name = ANGLO["fonte"]; r.font.size = Pt(tam)
        r.font.bold = negrito; r.font.italic = italico
        r.font.color.rgb = _rgb(cor or ANGLO["texto"])
    return tb


def slide_anglo(p, titulo, subtitulo=""):
    """Slide de conteúdo na identidade: fundo branco, logo, título e a
    régua azul — a assinatura visual do template."""
    from pptx.util import Inches, Pt
    from pptx.enum.shapes import MSO_SHAPE

    s = p.slides.add_slide(p.slide_masters[0].slide_layouts[6]
                           if len(p.slide_masters[0].slide_layouts) > 6
                           else p.slide_masters[0].slide_layouts[0])
    for ph in list(s.placeholders):
        ph._element.getparent().remove(ph._element)
    s.background.fill.solid(); s.background.fill.fore_color.rgb = _rgb(ANGLO["fundo"])

    lg = _logo_anglo()
    if lg:
        x, y, w, h = ANGLO["logo"]
        s.shapes.add_picture(lg, Inches(x), Inches(y), Inches(w), Inches(h))
    x, y, w, h = ANGLO["titulo"]
    _txt_anglo(s, x, y, w, h, titulo, 20, True, ANGLO["azul"])
    if subtitulo:
        _txt_anglo(s, x, y + 0.42, w, 0.3, subtitulo, 10.5, False,
                   ANGLO["suave"])
    rx, rw, rh = ANGLO["regua"]
    rg = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(rx),
                            Inches(ANGLO["regua_y"]), Inches(rw), Inches(rh))
    rg.fill.solid(); rg.fill.fore_color.rgb = _rgb(ANGLO["azul"])
    rg.line.fill.background(); rg.shadow.inherit = False
    return s


def capa_anglo(p, titulo, subtitulo=""):
    """Capa: fundo azul institucional, logo branco, título grande."""
    from pptx.util import Inches, Pt
    from pptx.enum.shapes import MSO_SHAPE

    s = p.slides.add_slide(p.slide_masters[0].slide_layouts[6]
                           if len(p.slide_masters[0].slide_layouts) > 6
                           else p.slide_masters[0].slide_layouts[0])
    for ph in list(s.placeholders):
        ph._element.getparent().remove(ph._element)
    fundo = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0,
                               Inches(13.333), Inches(7.5))
    fundo.fill.solid(); fundo.fill.fore_color.rgb = _rgb(ANGLO["azul"])
    fundo.line.fill.background(); fundo.shadow.inherit = False

    lg = _logo_anglo(branco=True)
    if lg:
        x, y, w, h = ANGLO["capa_logo"]
        s.shapes.add_picture(lg, Inches(x), Inches(y), Inches(w), Inches(h))
    x, y, w, h = ANGLO["capa_tit"]
    _txt_anglo(s, x, y, w, h, titulo, 30, True, "FFFFFF")
    if subtitulo:
        _txt_anglo(s, x, y + 1.35, w, 0.5, subtitulo, 14, False, "FFFFFF")
    _txt_anglo(s, 0.32, 7.12, 5.0, 0.3,
               f"© Anglo American, {datetime.now():%Y}", 9, False, "FFFFFF")
    return s


def ppt_survey_anglo(sid, cfg=None, bandas=None, sitios=None):
    """Deck EXCLUSIVO de site survey, na identidade Anglo.

    Separado do relatorio semanal a pedido: o survey virou dois tercos
    daquele deck e tem publico proprio. Aqui ele nao depende do template
    do cliente — os slides sao construidos do zero, entao a identidade e
    a mesma do inicio ao fim.

    `sitios` (de `sitios_parados()`) acrescenta o laudo das capturas
    feitas de um ponto fixo. Vem por parametro porque o censo nasce dos
    PEERS, e peer nao e amostra: nao esta no banco do survey, so no
    arquivo do MeshMapper.

    Devolve (bytes, nome).
    """
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.enum.text import PP_ALIGN
    import io as _io

    con = banco()
    try:
        sv = survey_obter(con, sid)
        if not sv: raise RuntimeError("survey nao encontrado")
        am = survey_amostras(con, sid)
        try:    mn = manual_listar(con, sid)
        except Exception: mn = []
    finally:
        con.close()
    if not am:
        raise RuntimeError("survey sem amostras")

    cfg = cfg_relatorio(cfg or configparser.ConfigParser())
    fixos = _fixos_do_survey(am)
    if bandas is None:
        bandas = sorted({_norm_banda(a.get("banda")) for a in am
                         if a.get("banda")}) or [None]
    quando = datetime.fromtimestamp(sv.get("inicio") or time.time())
    nome_sv = sv.get("nome") or "Site Survey"

    p = Presentation()
    p.slide_width  = Inches(13.333)
    p.slide_height = Inches(7.5)

    capa_anglo(p, f"Site Survey de Rede\n{nome_sv}",
               f"{quando:%d/%m/%Y %H:%M} · "
               + " e ".join(b for b in bandas if b))

    # ── Sumario: o que o leitor precisa saber em uma pagina ──
    s = slide_anglo(p, "Sumário", "Resultado do survey contra os "
                                  "requisitos Modular Mining")
    resumo = survey_resumo(am)
    srv = analisar_servidores(am)
    linhas = []
    for c in ("sinal", "snr", "rtt", "perda"):
        r = (resumo or {}).get(c)
        if not r: continue
        rot = (limite_de(c) or (None, None, None, c))[3]
        op, lim, un, _ = limite_de(c)
        linhas.append([rot, f"{op} {lim:g} {un}", f"{r['pct_ok']:.1f}%",
                       f"{r['med']:g} {un}", f"{r['p05']:g} {un}"])
    destaques = {}
    for i, l in enumerate(linhas, start=1):
        try:
            if float(l[2].rstrip("%")) < 90: destaques[(i, 2)] = "C0392B"
        except ValueError: pass
    if linhas:
        _tabela_anglo(s, 0.55, 1.45, 7.4,
                      ["Grandeza", "Requisito", "Dentro", "Mediana",
                       "Pior 5%"], linhas, destaques)

    cart = [("Amostras", f"{_milhar(len(am))}"),
            ("Rádios", str(len({a['radio'] for a in am}))),
            ("Intervalo efetivo",
             f"{sv.get('intervalo_efetivo_s') or sv.get('intervalo_s') or '—'} s"),
            ("Handovers", str(srv.get("n_handovers", 0))),
            ("Ping-pong", str(srv.get("n_pingpong", 0)))]
    y = 1.45
    for rot, val in cart:
        _cartao_anglo(s, 8.35, y, 4.45, 0.72, rot, val)
        y += 0.82

    # ── Laudo das capturas feitas paradas ──
    # Uma por slide. Vem antes das paginas de area de proposito: quem
    # capturou parado precisa ler primeiro que aquilo NAO e um mapa.
    for st in (sitios or []):
        r = st.get("resumo") or {}
        censo = [c for c in (st.get("censo") or []) if c["sinal"] is not None]
        s = slide_anglo(p, f"Vizinhança de {st['nome']}",
                        f"Captura parada — {st.get('pontos', 0)} leituras "
                        f"num raio de {st.get('raio_m', 0):g} m. "
                        f"Caracteriza o rádio, não a área ao redor.")
        linhas = [[c["nome"], "infra" if c["infra"] else "móvel",
                   f"{c['sinal']:g}",
                   "—" if c["snr"] is None else f"{c['snr']:g}",
                   "—" if c["presenca"] is None else f"{c['presenca']:g}%",
                   ", ".join(c["bandas"]) or "—"]
                  for c in censo[:16]]
        dest = {}
        req_c = float(ESCALAS["sinal"]["req"])
        for i, c in enumerate(censo[:16], start=1):
            if c["sinal"] <= req_c: dest[(i, 2)] = "C0392B"
            # Presenca baixa com sinal forte e veiculo de passagem, nao
            # cobertura: destacar impede que ele entre no laudo como se
            # fosse enlace permanente.
            if (c["presenca"] or 0) < 50: dest[(i, 4)] = "E67E22"
        _tabela_anglo(s, 0.55, 1.6, 8.1,
                      ["Vizinho", "Tipo", "RSSI (dBm)", "SNR (dB)",
                       "Presença", "Banda"], linhas, dest, tam=9)
        y = 1.6
        for rot, val in (("Vizinhos", str(r.get("vizinhos", 0))),
                         ("De infraestrutura", str(r.get("infra", 0))),
                         ("Móveis", str(r.get("moveis", 0))),
                         (f"Acima de {req_c:g} dBm", str(r.get("acima_req", 0))),
                         ("Presentes o tempo todo", str(r.get("constantes", 0)))):
            _cartao_anglo(s, 8.9, y, 3.9, 0.72, rot, val)
            y += 0.82
        _txt_anglo(s, 0.55, 6.75, 12.25, 0.5,
                   "O BreadCrumb não informa onde estão os vizinhos: o "
                   "censo diz com quem e com que sinal, não em que ponto "
                   "do terreno. Posição só de quem tiver captura própria.",
                   9.5, False, ANGLO["suave"], italico=True)

    # Sem nenhum equipamento em deslocamento nao ha area medida: zona e
    # moldura de mapa sairiam descrevendo um ponto como se fosse regiao.
    andou = [a for a in am if a.get("radio") not in fixos]
    if not andou:
        n_nav = _navegacao_anglo(p)
        if MARCA_DEMO.get("ativa"):
            marcar_ppt_demo(p)
        buf = _io.BytesIO(); p.save(buf)
        seguro = re.sub(r"[^\w\-]+", "_", nome_sv)[:40]
        log.info(f"[ppt/survey] {len(p.slides)} slides (captura parada), "
                 f"{len(am)} amostras, {n_nav} atalhos")
        return buf.getvalue(), f"Site_Survey_{seguro}_{quando:%Y%m%d}.pptx"

    # ── Zonas-problema por banda: a pagina acionavel ──
    try:
        grade = float(cfg.get("relatorio", "zonas_grade_m", fallback="50"))
    except (ValueError, TypeError):
        grade = 50.0
    for b in bandas:
        am_b = [a for a in am if _norm_banda(a.get("banda")) == b] or am
        try:    zonas = zonas_problema(am_b, "sinal", grade)
        except Exception: zonas = []
        rot_b = f" — {b}" if b else ""
        s = slide_anglo(p, f"Zonas-Problema{rot_b}",
                        "Regiões contíguas fora do requisito, com o BC que "
                        "as servia e sugestão de ponto para avaliação")
        if zonas:
            linhas = [[str(i), f"{_milhar(z['extensao_m'])} m",
                       f"{_milhar(z['area_m2'])} m² · {z['pct_area']:g}%",
                       z["servidor"] or "—",
                       f"{z['valor_mediano']:g} dBm",
                       f"{z['pior_valor']:g} dBm", str(z["n_radios"]),
                       f"{z['sugestao_lat']:.5f}, {z['sugestao_lon']:.5f}"]
                      for i, z in enumerate(zonas[:8], start=1)]
            dest = {}
            for i, z in enumerate(zonas[:8], start=1):
                dest[(i, 4)] = "C0392B"; dest[(i, 5)] = "C0392B"
                if z.get("sistemico"): dest[(i, 2)] = "E67E22"
            _tabela_anglo(s, 0.55, 1.5, 12.25,
                          ["#", "Extensão", "Área", "Servida por", "Mediana",
                           "Pior", "Equip.", "Sugestão (lat, lon)"],
                          linhas, dest, tam=9)
            _txt_anglo(s, 0.55, 6.7, 12.25, 0.5,
                       "A sugestão é ponto de partida para o projeto de RF, "
                       "não veredito: confirmar linha de visada e energia no "
                       "local.", 9.5, False, ANGLO["suave"], italico=True)
        else:
            _txt_anglo(s, 0.55, 1.6, 12.25, 0.5,
                       "Nenhuma zona contígua fora do requisito no período.",
                       13, True, "1E8449")

    # ── Molduras para o print do Google Earth ──
    for b in bandas:
        for campo, rot in (("sinal_cob", "Cobertura Disponível (melhor infra)"),
                           ("sinal", "Intensidade de Sinal (RSSI)"),
                           ("snr", "Relação Sinal/Ruído (SNR)"),
                           ("ruido", "Noise Floor"),
                           ("interf", "Interferência de Canal"),
                           ("perda", "Packet Loss"),
                           ("rtt", "Latência (RTT)")):
            rot_b = f" — {b}" if b else ""
            am_b = [a for a in am if _norm_banda(a.get("banda")) == b] or am
            # Grandeza sem NENHUMA medicao nao vira slide. Uma pagina com
            # escala, requisito e grafico vazio le-se como "medi e deu
            # tudo fora" — que e o oposto de "nao medi". E o caso de
            # latencia, perda e interferencia numa captura do MeshMapper,
            # que simplesmente nao fornece esses campos.
            if not any(a.get(campo) is not None for a in am_b):
                continue
            lim_ = limite_de(campo)
            sub = ("Rota medida, colorida pelo valor de cada amostra"
                   + (f"  ·  requisito {lim_[0]} {lim_[1]:g} {lim_[2]}"
                      if lim_ else ""))
            s = slide_anglo(p, f"{rot}{rot_b}", sub)

            # Mapa a esquerda, distribuicao a direita: a moldura mostra
            # ONDE, o grafico mostra QUANTO.
            suf = f"_{b.replace(' ', '').replace('.', '')}" if b else ""
            _moldura_anglo(s, 0.55, 1.45, 7.3, 5.4,
                           f"Survey_*_todas{suf}.kmz",
                           f"aba: {rot.split('(')[0].strip()}   ·   "
                           f"camada: Rotas" + (f"   ·   {b}" if b else ""))
            rotulos, pcts, un = distribuicao(am_b, campo)
            if rotulos:
                _grafico_anglo(s, 8.05, 1.45, 4.75, 3.4,
                               f"Distribuição ({un}) — % das amostras",
                               rotulos, pcts, campo=campo)
            r_res = (survey_resumo(am_b) or {}).get(campo)
            if r_res:
                _cartao_anglo(s, 8.05, 5.05, 2.28, 0.85, "Dentro do requisito",
                              f"{r_res['pct_ok']:.1f}%")
                _cartao_anglo(s, 10.52, 5.05, 2.28, 0.85, "Pior 5%",
                              f"{r_res['p05']:g} {un}")
            # Escala embaixo, na faixa que sobrou entre os cartões e o pé
            # do slide: sem ela o mapa é uma fita colorida sem significado
            # para quem não fez a medição.
            _escala_anglo(s, 8.05, 6.05, 4.75, campo)

    # ── Navegacao: indice + botoes em cada slide ──
    # Feita DEPOIS de tudo: os alvos precisam existir para o link nao
    # nascer morto. O indice entra logo apos a capa.
    n_nav = _navegacao_anglo(p)

    if MARCA_DEMO.get("ativa"):
        marcar_ppt_demo(p)

    buf = _io.BytesIO(); p.save(buf)
    seguro = re.sub(r"[^\w\-]+", "_", nome_sv)[:40]
    log.info(f"[ppt/survey] {len(p.slides)} slides, {len(am)} amostras, "
             f"{n_nav} atalhos")
    return buf.getvalue(), f"Site_Survey_{seguro}_{quando:%Y%m%d}.pptx"


def _navegacao_anglo(p, titulo_indice="Índice"):
    """Índice clicável + botão de volta em cada slide.

    Mesma regra do outro deck: um botão por seção não resolve quando a
    seção tem quinze slides. Aqui o índice lista TODOS e cada slide tem o
    caminho de volta.
    """
    from pptx.util import Inches, Pt
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import PP_ALIGN, MSO_ANCHOR

    def _titulo(s):
        cands = [(sh.top or 0, sh.text_frame.text.strip())
                 for sh in s.shapes if sh.has_text_frame
                 and sh.text_frame.text.strip()
                 and sh.text_frame.text.strip()[0] not in "◂▤"]
        return min(cands)[1].replace("\n", " ") if cands else ""

    alvos = [(i, _titulo(s)) for i, s in enumerate(p.slides)]
    # A capa (0) nao entra no indice, e o proprio indice ainda nao existe.
    itens = [(i, tt) for i, tt in alvos[1:] if tt]
    if not itens:
        return 0

    s = slide_anglo(p, titulo_indice,
                    "Clique num item para ir direto ao slide "
                    "(modo apresentação)")
    # Duas colunas: com 15 itens, uma so nao cabe na altura util.
    meio = (len(itens) + 1) // 2
    ligados = 0
    for col, grupo in enumerate((itens[:meio], itens[meio:])):
        x = 0.55 + col * 6.35
        y = 1.5
        for idx, tt in grupo:
            bt = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x),
                                    Inches(y), Inches(6.05), Inches(0.36))
            bt.fill.solid(); bt.fill.fore_color.rgb = _rgb(ANGLO["cartao"])
            bt.line.color.rgb = _rgb(ANGLO["linha"]); bt.shadow.inherit = False
            tf = bt.text_frame; tf.word_wrap = False
            tf.margin_left = Inches(0.12); tf.vertical_anchor = MSO_ANCHOR.MIDDLE
            r = tf.paragraphs[0].add_run(); r.text = f"›  {tt}"
            r.font.name = ANGLO["fonte"]; r.font.size = Pt(10.5)
            r.font.color.rgb = _rgb(ANGLO["azul"])
            if _link_para(bt, p.slides[idx]): ligados += 1
            y += 0.42

    # Move o indice para logo depois da capa.
    lst = p.slides._sldIdLst
    el = list(lst)[-1]; lst.remove(el); lst.insert(1, el)
    indice = p.slides[1]

    # Botao de volta em todos, menos capa e o proprio indice.
    for i, sl in enumerate(p.slides):
        if i <= 1: continue
        bt = sl.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(11.95),
                                 Inches(0.22), Inches(1.08), Inches(0.32))
        bt.fill.solid(); bt.fill.fore_color.rgb = _rgb(ANGLO["azul"])
        bt.line.fill.background(); bt.shadow.inherit = False
        tf = bt.text_frame; tf.vertical_anchor = MSO_ANCHOR.MIDDLE
        par = tf.paragraphs[0]; par.alignment = PP_ALIGN.CENTER
        r = par.add_run(); r.text = "◂ ÍNDICE"
        r.font.name = ANGLO["fonte"]; r.font.size = Pt(9)
        r.font.bold = True; r.font.color.rgb = _rgb("FFFFFF")
        _link_para(bt, indice)
        ligados += 1
    return ligados


def distribuicao(amostras, campo):
    """Percentual das amostras em cada faixa da escala.

    Alimenta o grafico de barras com MEDICAO, nao com o valor de exemplo
    que ficava no template esperando alguem editar a mao.
    Devolve (rotulos, percentuais, unidade).
    """
    faixas = FAIXAS_KML.get(campo)
    if not faixas: return [], [], ""
    lim_ = limite_de(campo)
    un = lim_[2] if lim_ else ""
    vals = [a[campo] for a in amostras if a.get(campo) is not None]
    if not vals: return [], [], un

    rotulos, contas, ant = [], [], None
    for lim, _cor in faixas:
        if ant is None:        rot = f"< {lim:g}"
        elif abs(lim) > 900:   rot = f"> {ant:g}"
        else:                  rot = f"{ant:g} a {lim:g}"
        n = sum(1 for v in vals
                if (ant is None or v >= ant) and v < lim)
        if abs(lim) > 900:
            n = sum(1 for v in vals if v >= ant)
        rotulos.append(rot); contas.append(n); ant = lim
    tot = len(vals)
    return rotulos, [round(100.0*c/tot, 1) for c in contas], un


def _grafico_anglo(s, x, y, w, h, titulo, rotulos, valores, campo=None):
    """Barras nativas do PowerPoint, na identidade. Nativo e nao imagem:
    quem recebe o deck pode editar os dados e o grafico acompanha."""
    from pptx.util import Inches, Pt
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION
    cd = CategoryChartData(); cd.categories = rotulos
    cd.add_series("% das amostras", valores)
    gf = s.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(x),
                            Inches(y), Inches(w), Inches(h), cd)
    ch = gf.chart
    ch.font.name = ANGLO["fonte"]; ch.font.size = Pt(9)
    ch.font.color.rgb = _rgb(ANGLO["texto"])
    ch.has_legend = False
    ch.has_title = True
    ch.chart_title.text_frame.text = titulo
    r = ch.chart_title.text_frame.paragraphs[0].runs[0]
    r.font.size = Pt(11); r.font.bold = True; r.font.color.rgb = _rgb(ANGLO["azul"])
    pl = ch.plots[0]; pl.gap_width = 40
    try:
        # Cor por faixa: a barra do trecho reprovado precisa saltar aos
        # olhos, senao o grafico so mostra "tem barra em todo lugar".
        faixas = FAIXAS_KML.get(campo) or []
        serie = pl.series[0]
        for i, pt in enumerate(serie.points):
            cor = faixas[i][1] if i < len(faixas) else ANGLO["azul"]
            pt.format.fill.solid(); pt.format.fill.fore_color.rgb = _rgb(cor)
    except Exception: pass
    try:
        pl.has_data_labels = True
        pl.data_labels.font.size = Pt(8)
        pl.data_labels.font.color.rgb = _rgb(ANGLO["texto"])
        pl.data_labels.number_format = '0.0"%"'
        pl.data_labels.number_format_is_linked = False
        pl.data_labels.position = XL_LABEL_POSITION.OUTSIDE_END
    except Exception: pass
    return ch


def _tabela_anglo(s, x, y, w, cab, linhas, destaques=None, tam=10):
    """Tabela na identidade: cabeçalho azul, zebra clara, texto escuro."""
    from pptx.util import Inches, Pt
    from pptx.enum.text import PP_ALIGN
    gf = s.shapes.add_table(len(linhas) + 1, len(cab), Inches(x), Inches(y),
                            Inches(w), Inches(0.34 + 0.3 * len(linhas)))
    tb = gf.table; tb.first_row = False; tb.horz_banding = False
    for j, txt in enumerate(cab):
        cel = tb.cell(0, j); cel.fill.solid()
        cel.fill.fore_color.rgb = _rgb(ANGLO["azul"])
        par = cel.text_frame.paragraphs[0]; par.alignment = PP_ALIGN.CENTER
        r = par.add_run(); r.text = str(txt)
        r.font.name = ANGLO["fonte"]; r.font.size = Pt(tam)
        r.font.bold = True; r.font.color.rgb = _rgb("FFFFFF")
    for i, lin in enumerate(linhas, start=1):
        for j, v in enumerate(lin):
            cel = tb.cell(i, j); cel.fill.solid()
            cel.fill.fore_color.rgb = _rgb(ANGLO["cartao"] if i % 2
                                           else ANGLO["fundo"])
            par = cel.text_frame.paragraphs[0]
            par.alignment = PP_ALIGN.LEFT if j == 0 else PP_ALIGN.CENTER
            r = par.add_run(); r.text = "—" if v in (None, "") else str(v)
            r.font.name = ANGLO["fonte"]; r.font.size = Pt(tam)
            cor = (destaques or {}).get((i, j))
            r.font.color.rgb = _rgb(cor or ANGLO["texto"])
            if cor: r.font.bold = True
    return tb


def _cartao_anglo(s, x, y, w, h, rotulo, valor):
    """Cartão de indicador: rótulo pequeno em cima, número grande."""
    from pptx.util import Inches, Pt
    from pptx.enum.shapes import MSO_SHAPE
    cx = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y),
                            Inches(w), Inches(h))
    cx.fill.solid(); cx.fill.fore_color.rgb = _rgb(ANGLO["cartao"])
    cx.line.color.rgb = _rgb(ANGLO["linha"]); cx.shadow.inherit = False
    cx.text_frame.text = ""
    _txt_anglo(s, x + 0.18, y + 0.08, w - 0.36, 0.24, rotulo, 9.5, False,
               ANGLO["suave"])
    _txt_anglo(s, x + 0.18, y + 0.30, w - 0.36, 0.36, valor, 16, True,
               ANGLO["azul"])
    return cx


def _escala_anglo(s, x, y, w, campo, blocos=28):
    """Barra da escala de cores, com os extremos e o requisito marcados.

    O slide mostrava o mapa colorido e o gráfico de distribuição sem dizer
    o que cada cor significa: quem abrisse o deck sem ter feito a medição
    via uma fita vermelha-e-verde e tinha de adivinhar o limiar.

    A cor sai de `cor_continua`, a MESMA função que pinta a rota no KMZ e
    o traçado do PNG. Redesenhar a escala com um gradiente próprio faria
    ela divergir do mapa na primeira vez que a paleta mudasse — e uma
    legenda que discorda do mapa é pior que legenda nenhuma.
    """
    from pptx.util import Inches, Pt
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import PP_ALIGN

    esc = ESCALAS.get(campo)
    if not esc:
        return None
    lo, hi = float(esc["lo"]), float(esc["hi"])
    un, req = esc.get("un", ""), esc.get("req")
    maior_melhor = esc.get("melhor") == "alto"

    _txt_anglo(s, x, y, w, 0.24,
               f"Escala — {esc.get('rot', campo)} ({un})", 9.5, True,
               ANGLO["suave"])

    yb, hb = y + 0.24, 0.26
    lb = w / blocos
    for i in range(blocos):
        v = lo + (hi - lo) * (i + 0.5) / blocos
        cor = cor_continua(v, esc)
        if not cor: continue
        r = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x + i * lb),
                               Inches(yb), Inches(lb + 0.004), Inches(hb))
        r.fill.solid(); r.fill.fore_color.rgb = _rgb(cor)
        r.line.fill.background(); r.shadow.inherit = False

    # Extremos: "pior" e "melhor" ficam do lado certo conforme a grandeza,
    # senão a legenda inverte o sentido em ruído, perda, RTT e interf.
    _txt_anglo(s, x, yb + hb + 0.02, w / 2, 0.22,
               f"{lo:g}", 8.5, False, ANGLO["suave"])
    cx = _txt_anglo(s, x + w / 2, yb + hb + 0.02, w / 2, 0.22,
                    f"{hi:g}", 8.5, False, ANGLO["suave"])
    cx.text_frame.paragraphs[0].alignment = PP_ALIGN.RIGHT

    if req is not None and lo != hi:
        f = (float(req) - lo) / (hi - lo)
        if 0.0 <= f <= 1.0:
            # Marca no ponto exato do requisito, não no meio do bloco: é
            # a linha que separa aprovado de reprovado.
            mk = s.shapes.add_shape(MSO_SHAPE.RECTANGLE,
                                    Inches(x + f * w - 0.008), Inches(yb - 0.05),
                                    Inches(0.016), Inches(hb + 0.10))
            mk.fill.solid(); mk.fill.fore_color.rgb = _rgb(ANGLO["texto"])
            mk.line.fill.background(); mk.shadow.inherit = False
            op = "≥" if maior_melhor else "≤"
            _txt_anglo(s, x, yb + hb + 0.24, w, 0.22,
                       f"requisito {op} {float(req):g} {un}", 8.5, True,
                       ANGLO["azul"])
    return yb + hb + 0.46


def _moldura_anglo(s, x, y, w, h, arquivo, detalhe):
    """Moldura tracejada para colar o print do Google Earth."""
    from pptx.util import Inches, Pt
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
    from pptx.enum.dml import MSO_LINE_DASH_STYLE
    cx = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y),
                            Inches(w), Inches(h))
    cx.fill.solid(); cx.fill.fore_color.rgb = _rgb(ANGLO["cartao"])
    cx.line.color.rgb = _rgb(ANGLO["azul"]); cx.line.width = Pt(1.4)
    cx.line.dash_style = MSO_LINE_DASH_STYLE.DASH
    cx.shadow.inherit = False
    tf = cx.text_frame; tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    for i, (txt, tam, neg, cor) in enumerate([
            ("COLAR AQUI O PRINT DO GOOGLE EARTH", 13, True, ANGLO["azul"]),
            (arquivo, 11, True, ANGLO["azul2"]),
            (detalhe, 10, False, ANGLO["suave"])]):
        par = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        par.alignment = PP_ALIGN.CENTER
        r = par.add_run(); r.text = txt
        r.font.name = ANGLO["fonte"]; r.font.size = Pt(tam)
        r.font.bold = neg; r.font.color.rgb = _rgb(cor)
    return cx


TITULO_INDICE = "5. Site Survey — Índice"

# Ordem em que os slides do survey aparecem no índice, por banda.
_ORDEM_SURVEY = [
    "Metodologia e Requisitos", "Rota Percorrida",
    "Cobertura Estimada da Mina", "Intensidade de Sinal (RSSI)",
    "Relação Sinal/Ruído (SNR)", "Noise Floor", "Interferência de Canal",
    "Packet Loss", "Latência (RTT)", "Throughput por Ponto de Teste",
    "No Talk's KPI", "Análise, Recomendações e Conclusão",
]


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


def _link_para(shape, slide):
    """Hyperlink de uma forma para um slide. Sem isto o 'índice' é só
    uma lista bonita que não leva a lugar nenhum."""
    try:
        _limpar_link_de_texto(shape)
        shape.click_action.target_slide = slide
        return True
    except Exception as e:
        log.debug(f"[nav] link falhou: {e}")
        return False


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

# Na capa o fundo NÃO vira branco: vira o azul institucional, como a capa
# do deck de survey. O texto já é branco e continua branco.
ANGLO_CAPA = {
    "0D2052": ANGLO["azul"],
    "16223C": ANGLO["azul"],
    "1A2744": ANGLO["azul2"],
    "1A3A7A": ANGLO["azul2"],
}

# Geometria do cabeçalho no semanal: logo à esquerda, título azul à direita
# dele, régua azul de ponta a ponta — a mesma assinatura do deck de survey.
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
    """Slide que já nasceu na identidade (os do deck de survey anexado)
    não leva um segundo logo em cima do primeiro."""
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
        # cartão claro com texto azul — a mesma convenção do índice do deck
        # de survey, para os dois relatórios se parecerem.
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


def construir_indice_survey(p, bandas=None):
    """Índice da seção 5, com um item por slide do survey.

    Existe porque o Site Survey virou dois terços do deck e o menu
    principal tem UM botão para tudo isso: quem quer ver a latência de
    5.8 GHz precisa passar 23 slides. O índice dá o atalho.
    """
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE

    _, existente = _slide_por_titulo(p, TITULO_INDICE)
    if existente is not None:
        for sh in list(existente.shapes):
            _remover_forma(sh)          # reconstrói: os alvos mudaram
        s = existente
    else:
        s = p.slides.add_slide(p.slide_masters[0].slide_layouts[0])
        for ph in list(s.placeholders):
            ph._element.getparent().remove(ph._element)
    s.background.fill.solid()
    s.background.fill.fore_color.rgb = RGBColor.from_string("0D2052")

    C_BR = RGBColor.from_string("FFFFFF"); C_SUB = RGBColor.from_string("A0B0CC")
    C_CARD = RGBColor.from_string("1A2744"); C_BORDA = RGBColor.from_string("2A3A5C")

    def txt(x, y, w, h, t, tam=11, negrito=False, cor=None):
        tb = s.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
        tf = tb.text_frame; tf.word_wrap = True
        r = tf.paragraphs[0].add_run(); r.text = t
        r.font.name = "Calibri"; r.font.size = Pt(tam); r.font.bold = negrito
        r.font.color.rgb = cor or C_BR
        return tb

    txt(0.55, 0.25, 11.2, 0.6, TITULO_INDICE, 20, True)
    txt(0.55, 0.85, 11.5, 0.35,
        "Clique num item para ir direto ao slide (modo apresentação)",
        11, False, C_SUB)

    if bandas is None:
        bandas = []
        for b in ("2.4 GHz", "5.8 GHz"):
            if _slide_por_titulo(p, f"Rota Percorrida — {b}")[1] is not None:
                bandas.append(b)

    # Coluna 1: o que não é por banda. Colunas seguintes: uma por banda.
    colunas = [("Geral", [TITULO_MAPA_REDE.split("— ")[-1],
                          TITULO_ZONAS.split("— ")[-1],
                          "Mapa de Calor RF"], None)]
    for b in bandas:
        colunas.append((b, _ORDEM_SURVEY, b))

    larg = 12.25 / max(1, len(colunas)) - 0.15
    ligados = 0
    for ci, (cab, itens, banda) in enumerate(colunas):
        x = 0.55 + ci * (larg + 0.15)
        cx = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x),
                                Inches(1.4), Inches(larg), Inches(5.5))
        cx.fill.solid(); cx.fill.fore_color.rgb = C_CARD
        cx.line.color.rgb = C_BORDA; cx.shadow.inherit = False
        txt(x + 0.2, 1.55, larg - 0.4, 0.3, cab, 12, True,
            RGBColor.from_string("4FA3F7"))
        y = 1.95
        for item in itens:
            alvo = None
            if banda:
                _, alvo = _slide_por_titulo(p, f"{item} — {banda}")
            if alvo is None:
                _, alvo = _slide_por_titulo(p, item)
            if alvo is None:
                continue        # slide não existe neste deck: não lista
            # Cartão, não caixa de texto: alvo de clique maior e com
            # realce visível de que ali se clica.
            bt = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                                    Inches(x + 0.18), Inches(y),
                                    Inches(larg - 0.36), Inches(0.3))
            bt.fill.solid()
            bt.fill.fore_color.rgb = RGBColor.from_string("14213F")
            bt.line.color.rgb = RGBColor.from_string("2A3A5C")
            bt.shadow.inherit = False
            tf = bt.text_frame; tf.word_wrap = False
            tf.margin_left = Inches(0.08); tf.margin_top = 0
            tf.margin_bottom = 0
            r = tf.paragraphs[0].add_run(); r.text = f"›  {item}"
            r.font.name = "Calibri"; r.font.size = Pt(10)
            r.font.color.rgb = C_BR
            if _link_para(bt, alvo): ligados += 1
            y += 0.35

    # Volta ao menu principal, no mesmo lugar dos outros slides.
    _, nav = _slide_por_titulo(p, "Navegação")
    if nav is None:
        for sl in p.slides:
            if any(sh.has_text_frame and sh.text_frame.text.strip()
                   .lower().startswith("navega") for sh in sl.shapes):
                nav = sl; break
    if nav is not None:
        bt = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(12.0),
                                Inches(0.28), Inches(0.95), Inches(0.32))
        bt.fill.solid(); bt.fill.fore_color.rgb = RGBColor.from_string("1A3A7A")
        bt.line.color.rgb = C_BORDA; bt.shadow.inherit = False
        r = bt.text_frame.paragraphs[0].add_run(); r.text = "◂ MENU"
        r.font.name = "Calibri"; r.font.size = Pt(10); r.font.bold = True
        r.font.color.rgb = C_BR
        _link_para(bt, nav)

    # Logo antes do primeiro slide da seção 5.
    idx = None
    for ancora in ("Mapa de Calor RF", TITULO_MAPA_REDE, TITULO_ZONAS):
        i, _ = _slide_por_titulo(p, ancora)
        if i is not None: idx = i; break
    if idx is not None:
        lst = p.slides._sldIdLst
        el = list(lst)[-1] if existente is None else None
        if el is not None:
            lst.remove(el); lst.insert(idx, el)
    log.info(f"[nav] índice do survey: {ligados} atalhos")
    return s


def _botao(s, x, y, w, h, texto, alvo, cor_fundo="1A3A7A", tam=9.5):
    """Botão retangular ligado a um slide. Devolve True se ligou."""
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
    bt = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y),
                            Inches(w), Inches(h))
    bt.fill.solid(); bt.fill.fore_color.rgb = RGBColor.from_string(cor_fundo)
    bt.line.color.rgb = RGBColor.from_string("2A3A5C"); bt.shadow.inherit = False
    tf = bt.text_frame; tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    par = tf.paragraphs[0]; par.alignment = PP_ALIGN.CENTER
    r = par.add_run(); r.text = texto
    r.font.name = "Calibri"; r.font.size = Pt(tam); r.font.bold = True
    r.font.color.rgb = RGBColor.from_string("FFFFFF")
    return _link_para(bt, alvo) if alvo is not None else False


def barra_navegacao_survey(p, bandas=None):
    """Barra de atalhos em cada slide do survey.

    Três coisas que faltavam para não precisar voltar ao menu a cada
    consulta: ir ao índice da seção, e pular para o MESMO assunto na outra
    banda — que é a comparação que o relatório de duas malhas pede o tempo
    todo.
    """
    _, indice = _slide_por_titulo(p, TITULO_INDICE)
    if indice is None: return 0
    if bandas is None:
        bandas = [b for b in ("2.4 GHz", "5.8 GHz")
                  if _slide_por_titulo(p, f"Rota Percorrida — {b}")[1]]

    postos = 0
    for assunto in _ORDEM_SURVEY + [TITULO_MAPA_REDE.split("— ")[-1],
                                    TITULO_ZONAS.split("— ")[-1]]:
        for b in (bandas or [None]):
            alvo = (f"{assunto} — {b}" if b else assunto)
            _, s = _slide_por_titulo(p, alvo)
            if s is None and b:      # slide sem sufixo de banda
                continue
            if s is None:
                _, s = _slide_por_titulo(p, assunto)
            if s is None: continue
            # Não duplica se o deck já foi processado antes.
            if any(sh.has_text_frame and "ÍNDICE" in (sh.text_frame.text or "")
                   for sh in s.shapes):
                continue
            _botao(s, 10.55, 0.28, 1.35, 0.32, "▤ ÍNDICE", indice)
            postos += 1
            # Alternador de banda: leva ao mesmo assunto na outra malha.
            if not b: continue
            outras = [x for x in bandas if x != b]
            px = 9.05
            for o in outras[:1]:
                _, alvo_o = _slide_por_titulo(p, f"{assunto} — {o}")
                if alvo_o is None: continue
                _botao(s, px, 0.28, 1.4, 0.32, f"⇄ {o}", alvo_o,
                       cor_fundo="2E6DA4")
    log.info(f"[nav] barra do survey em {postos} slide(s)")
    return postos


def atualizar_navegacao(p):
    """Aponta o botão 5 do menu principal para o índice do survey.

    O menu tem um botão por seção, mas a seção 5 virou 2/3 do deck. Sem
    isto, clicar nela cai no primeiro slide e o resto é passar página.
    """
    _, indice = _slide_por_titulo(p, TITULO_INDICE)
    if indice is None: return 0
    nav = None
    for sl in p.slides:
        if any(sh.has_text_frame and sh.text_frame.text.strip().lower()
               .startswith("navega") for sh in sl.shapes):
            nav = sl; break
    if nav is None: return 0
    n = 0
    for sh in nav.shapes:
        if not sh.has_text_frame: continue
        t = (sh.text_frame.text or "").strip()
        if not t.lower().startswith("5."): continue
        # O rótulo dizia "Mapa de Calor", que agora é só o primeiro de
        # quinze slides da seção.
        _ppt_set(sh.text_frame, "5. Site Survey")
        if _link_para(sh, indice): n += 1
    return n


TITULO_ZONAS = "5. Site Survey — Zonas-Problema e Ações"


def construir_slide_zonas(p):
    """Slide da lista de zonas com recomendação.

    É o que separa "34% dentro do requisito" de um survey entregável:
    percentual diz quanto está ruim, a zona diz onde e o que fazer.
    """
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor

    _, existente = _slide_por_titulo(p, TITULO_ZONAS)
    if existente is not None:
        return existente

    C_BG = RGBColor.from_string("0D2052"); C_SUB = RGBColor.from_string("A0B0CC")
    C_BR = RGBColor.from_string("FFFFFF")
    s = p.slides.add_slide(p.slide_masters[0].slide_layouts[0])
    s.background.fill.solid(); s.background.fill.fore_color.rgb = C_BG
    for ph in list(s.placeholders):
        ph._element.getparent().remove(ph._element)

    def txt(x, y, w, h, t, tam=11, negrito=False, cor=None):
        tb = s.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
        tf = tb.text_frame; tf.word_wrap = True
        for i, linha in enumerate(str(t).split("\n")):
            par = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            r = par.add_run(); r.text = linha
            r.font.name = "Calibri"; r.font.size = Pt(tam)
            r.font.bold = negrito; r.font.color.rgb = cor or C_BR
        return tb

    txt(0.55, 0.25, 11.2, 0.6, TITULO_ZONAS, 20, True)
    txt(0.55, 0.85, 11.5, 0.35,
        "Regiões contíguas fora do requisito, com o BC que as servia e "
        "sugestão de ponto para avaliação de rádio", 11, False, C_SUB)
    _tabela_ppt(s, 0.55, 1.4, 12.25,
                ["#", "Extensão", "Área", "Servida por", "Mediano",
                 "Pior", "Equip.", "Sugestão (lat, lon)"],
                [["—"] * 8], tam=9, altura_linha=0.36)

    # Logo depois do Mapa da Rede: o leitor vê onde está a malha e, na
    # sequência, onde ela falha. No fim do deck ninguém acha.
    idx = None
    for ancora in (TITULO_MAPA_REDE, "Metodologia e Requisitos",
                   "Rota Percorrida"):
        i, _ = _slide_por_titulo(p, ancora)
        if i is not None: idx = i; break
    if idx is not None:
        lst = p.slides._sldIdLst
        el = list(lst)[-1]
        lst.remove(el); lst.insert(idx + 1, el)
    return s


def preencher_slide_zonas(p, zonas, campo="sinal", limite_linhas=8):
    """Preenche a tabela de zonas. Sem zona, diz isso — não deixa em branco."""
    s = construir_slide_zonas(p)
    antiga = next((sh for sh in s.shapes if getattr(sh, "has_table", False)),
                  None)
    if antiga is not None:
        _remover_forma(antiga)
    _, lim, un, rot = limite_de(campo)

    if not zonas:
        _tabela_ppt(s, 0.55, 1.4, 12.25, ["Resultado"],
                    [[f"Nenhuma zona contígua fora do requisito "
                      f"({rot} {limite_de(campo)[0]} {lim:g} {un})."]],
                    tam=11, altura_linha=0.5)
        return 0

    linhas, destaques = [], {}
    for i, z in enumerate(zonas[:limite_linhas], start=1):
        # Zona sistêmica marcada na própria linha: sem isso a tabela
        # sugere que um rádio resolve uma mancha que cobre meia cava.
        sug = (f"{z['sugestao_lat']:.5f}, {z['sugestao_lon']:.5f}"
               + ("  (replanejar cobertura)" if z.get("sistemico") else ""))
        linhas.append([
            str(i), f"{_milhar(z['extensao_m'])} m",
            f"{_milhar(z['area_m2'])} m² · {z['pct_area']:g}%",
            z["servidor"] or "—",
            f"{z['valor_mediano']:g} {un}", f"{z['pior_valor']:g} {un}",
            str(z["n_radios"]), sug])
        destaques[(i, 4)] = "E8544A"; destaques[(i, 5)] = "E8544A"
        if z.get("sistemico"):
            destaques[(i, 2)] = "F0A640"; destaques[(i, 7)] = "F0A640"
    _tabela_ppt(s, 0.55, 1.4, 12.25,
                ["#", "Extensão", "Área", "Servida por", f"{rot} mediano",
                 "Pior", "Equip.", "Sugestão (lat, lon)"],
                linhas, destaques=destaques, tam=9, altura_linha=0.36)

    rodape = (f"{len(zonas)} zona(s) fora de {rot} {limite_de(campo)[0]} "
              f"{lim:g} {un}.")
    if len(zonas) > limite_linhas:
        rodape += f" Mostrando as {limite_linhas} maiores por área."
    rodape += ("  A sugestão é ponto de partida para o projeto de RF, "
               "não veredito: confirmar linha de visada e energia no local.")
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    tb = s.shapes.add_textbox(Inches(0.55), Inches(6.6), Inches(12.25),
                              Inches(0.6))
    tb.text_frame.word_wrap = True
    r = tb.text_frame.paragraphs[0].add_run(); r.text = rodape
    r.font.name = "Calibri"; r.font.size = Pt(10)
    r.font.color.rgb = RGBColor.from_string("A0B0CC"); r.font.italic = True
    return len(zonas[:limite_linhas])


TITULO_MAPA_REDE = "5. Site Survey — Mapa da Rede"


def construir_slide_mapa_rede(p):
    """Slide único da seção de Site Survey, ANTES dos slides por banda.

    Não é por banda de propósito: é o retrato da malha, e a malha é uma
    só. Devolve o slide (novo ou o que já existia — chamar duas vezes não
    duplica).
    """
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE

    _, existente = _slide_por_titulo(p, TITULO_MAPA_REDE)
    if existente is not None:
        return existente

    C_BG  = RGBColor.from_string("0D2052"); C_SUB = RGBColor.from_string("A0B0CC")
    C_BR  = RGBColor.from_string("FFFFFF"); C_CARD= RGBColor.from_string("1A2744")
    C_BORDA = RGBColor.from_string("2A3A5C")

    s = p.slides.add_slide(p.slide_masters[0].slide_layouts[0])
    s.background.fill.solid(); s.background.fill.fore_color.rgb = C_BG
    for ph in list(s.placeholders):
        ph._element.getparent().remove(ph._element)

    def txt(x, y, w, h, t, tam=11, negrito=False, cor=None):
        tb = s.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
        tf = tb.text_frame; tf.word_wrap = True
        for i, linha in enumerate(str(t).split("\n")):
            par = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            r = par.add_run(); r.text = linha
            r.font.name = "Calibri"; r.font.size = Pt(tam)
            r.font.bold = negrito; r.font.color.rgb = cor or C_BR
        return tb

    txt(0.55, 0.25, 11.2, 0.6, TITULO_MAPA_REDE, 20, True)
    txt(0.55, 0.85, 11.5, 0.35,
        "Posição de todos os BreadCrumbs e trajetos percorridos pela frota "
        "durante o survey", 11, False, C_SUB)

    # Moldura do mapa à esquerda; a tabela ocupa a coluna da direita.
    mold = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                              Inches(0.55), Inches(1.4), Inches(8.5), Inches(5.3))
    mold.fill.solid(); mold.fill.fore_color.rgb = C_CARD
    mold.line.color.rgb = C_BORDA; mold.shadow.inherit = False
    tf = mold.text_frame; tf.word_wrap = True
    r = tf.paragraphs[0].add_run(); r.text = "COLAR MAPA DA REDE AQUI"
    r.font.name = "Calibri"; r.font.size = Pt(12); r.font.color.rgb = C_SUB

    txt(9.3, 1.5, 3.5, 0.3, "COMPOSIÇÃO DA MALHA", 11, True, C_SUB)
    # Valores em branco: preencher_slides_survey() troca pelos medidos.
    # Traço em vez de zero — zero seria lido como "contei e deu nenhum".
    _tabela_ppt(s, 9.3, 1.9, 3.5,
                ["Item", "Qtd"],
                [["Torres fixas (ERB)", "—"],
                 ["Repetidoras móveis (ERM)", "—"],
                 ["Equipamentos móveis", "—"],
                 ["Equipamentos com GPS", "—"],
                 ["Sem posição", "—"],
                 ["Período do survey", "—"],
                 ["Intervalo efetivo", "—"]],
                tam=9, altura_linha=0.42)

    # Posiciona ANTES do primeiro slide de survey por banda.
    idx = None
    for ancora in ("Metodologia e Requisitos", "Rota Percorrida"):
        i, _ = _slide_por_titulo(p, ancora)
        if i is not None: idx = i; break
    if idx is not None:
        lst = p.slides._sldIdLst
        el = list(lst)[-1]
        lst.remove(el); lst.insert(idx, el)
    return s


def preencher_mapa_rede(p, sv, resumo_rede, amostras):
    """Números do slide de Mapa da Rede. Sem dado, mantém o traço."""
    s = construir_slide_mapa_rede(p)
    tab = next((sh.table for sh in s.shapes if getattr(sh, "has_table", False)),
               None)
    if tab is None or not resumo_rede:
        return 0

    tipos = resumo_rede.get("tipos") or {}
    ini = sv.get("inicio"); fim = sv.get("fim") or ini
    periodo = "—"
    if ini:
        d0 = datetime.fromtimestamp(ini); d1 = datetime.fromtimestamp(fim)
        periodo = (f"{d0:%d/%m %H:%M} – {d1:%H:%M}" if d0.date() == d1.date()
                   else f"{d0:%d/%m %H:%M} – {d1:%d/%m %H:%M}")
    ef = sv.get("intervalo_efetivo_s") or sv.get("intervalo_s")
    valores = [tipos.get("ERB"), tipos.get("ERM"), tipos.get("Móvel"),
               resumo_rede.get("com_gps"),
               len(resumo_rede.get("sem_posicao") or []),
               periodo, (f"{ef:g} s" if ef else None)]
    n = 0
    for i, v in enumerate(valores, start=1):
        if i >= len(tab.rows): break
        cel = tab.cell(i, 1)
        _ppt_set(cel.text_frame, "—" if v in (None, "") else str(v))
        n += 1
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
<title>Rede Rajant — Relatórios e Survey</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: Arial, Helvetica, sans-serif; background:#eef1f5;
         margin:0; padding:20px 14px 40px; color:#1f2d3d; }
  .card { max-width:720px; margin:22px auto; background:#fff; border-radius:10px;
          box-shadow:0 2px 12px rgba(0,0,0,.08); overflow:hidden; }
  .top { background:#1F4E79; color:#fff; padding:18px 22px; }
  .top h1 { margin:0; font-size:19px; }
  .top p  { margin:4px 0 0; font-size:13px; color:#cfe0f2; }

  .abas { display:flex; background:#f6f8fa; border-bottom:1px solid #e3e8ee;
          overflow-x:auto; }
  .abas button { flex:1 0 auto; border:0; background:none; color:#33404f;
          font:inherit; font-size:13.5px; font-weight:bold; cursor:pointer;
          padding:13px 10px; white-space:nowrap; }
  .abas button.on { background:#1F4E79; color:#fff; }

  .body { padding:20px 22px; }
  .painel { display:none; }
  .painel.on { display:block; }

  label { display:block; font-size:13px; font-weight:bold; margin:0 0 6px; }
  .fraco { font-size:12px; color:#66748a; font-weight:normal; }
  input[type=text], input[type=date], input[type=number], select, textarea {
          width:100%; padding:9px 10px; border:1px solid #c7d0da;
          border-radius:6px; font-size:14px; font-family:inherit; background:#fff; }
  textarea { resize:vertical; min-height:58px; }
  .linha { display:flex; gap:12px; flex-wrap:wrap; }
  .linha > div { flex:1; min-width:130px; }
  hr { border:0; border-top:1px solid #e3e8ee; margin:18px 0; }

  button.b { border:0; border-radius:7px; font:inherit; font-weight:bold;
             cursor:pointer; padding:11px 14px; font-size:14px; }
  .prim { background:#2E75B6; color:#fff; } .prim:hover { background:#245e92; }
  .ger  { background:#1F4E79; color:#fff; width:100%; } .ger:hover { background:#163a5c; }
  .verde{ background:#1E8449; color:#fff; } .verde:hover { background:#186c3b; }
  .neut { background:#e3e8ee; color:#33404f; } .neut:hover { background:#d5dde6; }
  button.b:disabled { opacity:.55; cursor:wait; }

  .caixa-erro { background:#FFC7CE; color:#9C0006; padding:10px 12px;
          border-radius:6px; font-size:13px; margin:0 0 14px; display:none; }
  /* Estado normal é informativo (carga estimada na malha) — amarelo aqui
     viraria ruído e o operador pararia de ler. O amarelo fica só para
     .atencao, quando a carga passa do limite. */
  .caixa-aviso { background:#EEF2F7; color:#33404f; padding:9px 11px;
          border-radius:6px; font-size:12.5px; margin-top:8px; display:none;
          border-left:3px solid #9fb3c8; }
  .caixa-aviso.atencao { background:#FFEB9C; color:#9C6500;
          border-left-color:#9C6500; font-weight:bold; }

  .tags { display:flex; gap:7px; flex-wrap:wrap; margin-bottom:12px; }
  .tags button { border:1px solid #c7d0da; background:#fff; color:#33404f;
          border-radius:20px; padding:6px 13px; font-size:12.5px; font-weight:bold;
          cursor:pointer; font-family:inherit; }
  .tags button.on { background:#2E75B6; color:#fff; border-color:#2E75B6; }

  .presets { display:flex; gap:7px; flex-wrap:wrap; }
  .presets button { border:1px solid #c7d0da; background:#fff; color:#33404f;
          border-radius:7px; padding:8px 12px; font-size:12.5px; font-weight:bold;
          cursor:pointer; font-family:inherit; }
  .presets button.on { background:#2E75B6; color:#fff; border-color:#2E75B6; }

  .lista { max-height:260px; overflow:auto; border:1px solid #c7d0da;
           border-radius:7px; background:#fbfcfd; padding:6px 8px; }
  .item { display:flex; align-items:center; gap:8px; padding:5px 3px;
          font-size:13px; border-bottom:1px solid #eef1f5; }
  .item:last-child { border-bottom:0; }
  .item input { margin:0; flex:0 0 auto; }
  .item .nm { font-weight:bold; }
  .item .tg { font-size:11px; color:#66748a; background:#eef1f5;
              padding:1px 6px; border-radius:10px; }
  .item .dt { margin-left:auto; font-size:11px; color:#9aa7b6; text-align:right; }
  .item.semgps .nm, .item.semgps .tg { color:#9aa7b6; }
  .resumo { font-size:12.5px; color:#33404f; margin:10px 0 6px; font-weight:bold; }
  .resumo em { font-style:normal; color:#9C6500; font-weight:normal; }
  .atalhos { font-size:12px; color:#66748a; margin:8px 0 0; }
  .atalhos a { color:#2E75B6; text-decoration:none; cursor:pointer; }
  .atalhos a:hover { text-decoration:underline; }
  .achados { font-size:12.5px; margin-top:7px; }
  .achados b { color:#1E8449; } .achados i { font-style:normal; color:#C0392B; }

  .barra { height:9px; background:#e3e8ee; border-radius:5px; overflow:hidden;
           margin:12px 0 8px; }
  .barra i { display:block; height:100%; background:#1E8449; width:0;
             transition:width .4s; }
  .status { font-size:12.5px; color:#33404f; }

  table { width:100%; border-collapse:collapse; font-size:12.5px; }
  th { text-align:left; font-size:11px; text-transform:uppercase; color:#66748a;
       padding:8px 6px; border-bottom:1px solid #c7d0da; white-space:nowrap; }
  td { padding:8px 6px; border-bottom:1px solid #eef1f5; }
  tr.emuso td { background:#eef6ff; }
  .rolar { overflow-x:auto; }
  /* 10 colunas em 720px: célula compacta evita rolagem na maioria dos casos */
  #tabHist th, #tabHist td { padding:7px 5px; font-size:11.5px; }
  #tabHist td:first-child, #tabHist th:first-child {
          position:sticky; left:0; background:#fff; z-index:1; }
  #tabHist tr.emuso td:first-child { background:#eef6ff; }
  /* Ações fixas à direita: com 10 colunas a tabela rola, e os botões
     ficavam fora da tela — "Google Earth" aparecia cortado como "Go".
     Fixando a última coluna eles ficam sempre alcançáveis. */
  #tabHist td:last-child, #tabHist th:last-child {
          position:sticky; right:0; background:#fff; z-index:1;
          box-shadow:-6px 0 6px -6px rgba(0,0,0,.18); }
  #tabHist tr.emuso td:last-child { background:#eef6ff; }
  .acao { border:0; background:none; color:#2E75B6; cursor:pointer; font:inherit;
          font-size:12px; padding:2px 5px; text-decoration:underline; }
  .acao.perigo { color:#C0392B; }
  .pill { font-size:10.5px; background:#1F4E79; color:#fff; padding:1px 7px;
          border-radius:10px; }
  .vazio { padding:22px 6px; text-align:center; color:#66748a; font-size:13px; }
  .bom { color:#27AE60; font-weight:bold; }
  .aten { color:#F1C40F; font-weight:bold; }
  .ruim { color:#C0392B; font-weight:bold; }

  .subabas { display:flex; gap:7px; margin-bottom:14px; }
  .subabas button { border:1px solid #c7d0da; background:#fff; color:#33404f;
          border-radius:7px; padding:7px 14px; font-size:13px; font-weight:bold;
          cursor:pointer; font-family:inherit; }
  .subabas button.on { background:#2E75B6; color:#fff; border-color:#2E75B6; }
  .radios { display:flex; gap:16px; margin-bottom:12px; font-size:13.5px; }
  .radios label { display:flex; align-items:center; gap:6px; font-weight:bold;
                  margin:0; cursor:pointer; }
  .nota { font-size:12px; color:#66748a; margin-top:5px; }
  @media (max-width:420px) {
    .linha > div { min-width:100%; }
    .body { padding:16px 14px; }
  }
</style>
</head>
<body>
<div class="card">
  <div class="top">
    <h1>Rede Rajant</h1>
    <p>Relatórios, site survey e medições de campo</p>
  </div>
  <div class="abas" id="abas">
    <button data-p="relatorios" class="on">Relatórios</button>
    <button data-p="survey">Survey</button>
    <button data-p="historico">Histórico</button>
    <button data-p="medicoes">Medições</button>
  </div>
  <div class="body">
    <div class="caixa-erro" id="erro"></div>

    <!-- ══════════════ RELATÓRIOS ══════════════ -->
    <div class="painel on" id="p-relatorios">
      <label>Períodos rápidos</label>
      <div class="presets" style="margin-bottom:16px">
        <button type="button" onclick="rapido(7)">Últimos 7 dias</button>
        <button type="button" onclick="rapido(30)">Últimos 30 dias</button>
      </div>
      <label>Período personalizado</label>
      <div class="linha">
        <div><label class="fraco">De</label><input type="date" id="ini"></div>
        <div><label class="fraco">Até</label><input type="date" id="fim"></div>
      </div>
      <p class="nota"><label style="display:inline;font-weight:normal">
        <input type="checkbox" id="demo"> Dados de demonstração (sem Prometheus)
      </label></p>
      <p class="nota" id="svEmUso"></p>
      <button class="b ger" id="btnXls" onclick="gerar('xlsx')"
              style="margin-top:12px">Gerar e baixar Excel</button>
      <button class="b ger" id="btnPpt" onclick="gerar('pptx')"
              style="margin-top:10px;background:#2E75B6">Gerar e baixar PPT</button>
      <p class="nota" style="margin-top:14px">O arquivo é gerado sob demanda a
        partir do Prometheus. Marque um survey em <b>Histórico → Usar no
        relatório</b> para que a aba Survey e os slides de Site Survey sejam
        preenchidos com as medições daquele survey.</p>
    </div>

    <!-- ══════════════ SURVEY ══════════════ -->
    <div class="painel" id="p-survey">
      <label>Rádios por categoria</label>
      <div class="tags" id="tags"></div>

      <label>Colar tags de rádio</label>
      <textarea id="colar" placeholder="ERM-20, ERB-04, CA-1010"></textarea>
      <div style="margin-top:8px"><button class="b prim" type="button"
              onclick="aplicarColadas()">Aplicar</button></div>
      <div class="achados" id="achados"></div>

      <hr>
      <div class="linha">
        <div><label class="fraco">Buscar</label>
          <input type="text" id="busca" placeholder="nome do rádio"></div>
        <div style="flex:0 0 auto;align-self:flex-end;padding-bottom:9px">
          <label style="display:inline;font-weight:normal;font-size:12.5px">
            <input type="checkbox" id="soGps"> Só com GPS</label>
          &nbsp;&nbsp;
          <label style="display:inline;font-weight:normal;font-size:12.5px">
            <input type="checkbox" id="soOnline"> Só online</label>
        </div>
      </div>

      <div class="linha" style="margin-top:12px">
        <div><label class="fraco">Perfil salvo</label>
          <select id="perfil"><option value="">— perfil —</option></select></div>
        <div style="flex:0 0 auto;align-self:flex-end;padding-bottom:1px">
          <button class="b neut" type="button" onclick="salvarPerfil()">Salvar seleção</button>
          <button class="b neut" type="button" onclick="excluirPerfil()">Excluir perfil</button>
        </div>
      </div>

      <p class="resumo" id="resumo">—</p>
      <div class="lista" id="lista"></div>
      <p class="atalhos">
        <a onclick="marcarTodos(true)">marcar todos</a> ·
        <a onclick="marcarTodos(false)">desmarcar</a> ·
        <a onclick="marcarGps()">só com GPS</a>
      </p>

      <hr>
      <label>Nome do survey</label>
      <input type="text" id="svNome" placeholder="Survey 2026-05-15">

      <label style="margin-top:14px">Duração</label>
      <div class="presets" id="presets">
        <button type="button" data-m="15">15 min</button>
        <button type="button" data-m="60">1 h</button>
        <button type="button" data-m="480">Turno (8 h)</button>
        <button type="button" data-m="1440">24 h</button>
      </div>
      <div class="linha" style="margin-top:10px">
        <div><label class="fraco">Minutos</label>
          <input type="number" id="minutos" value="60" min="1" max="1440"></div>
        <div><label class="fraco">Intervalo (s) &middot; <b>0 = contínuo</b></label>
          <input type="number" id="intervalo" value="0" min="0" max="300"></div>
        <div><label class="fraco">Alcance ERM/ERB (m)</label>
          <input type="number" id="alcance" value="1000" min="200" max="4000" step="100"></div>
      </div>
      <div class="caixa-aviso" id="avisoIntervalo"></div>
      <p class="nota">Valor inicial das manchas; será substituído pelo alcance
        calibrado quando houver amostras suficientes.</p>

      <div style="margin-top:14px">
        <button class="b verde" id="btnIniciar" onclick="iniciar()">Iniciar coleta</button>
        <button class="b neut" id="btnParar" onclick="parar()" disabled>Encerrar agora</button>
      </div>
      <div class="barra"><i id="prog"></i></div>
      <p class="status" id="statusCap">Nenhuma captura em andamento.</p>
      <p id="acoesPronto" style="display:none">
        <button class="b prim" type="button" onclick="baixarKmlCaptura()">Abrir no Google Earth</button>
        <button class="b neut" type="button" onclick="baixarImagensCaptura()">Baixar imagens</button>
        <button class="b neut" type="button" onclick="irPara('historico')">Ver no histórico</button>
      </p>
    </div>

    <!-- ══════════════ HISTÓRICO ══════════════ -->
    <div class="painel" id="p-historico">
      <div style="display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap;
                  margin-bottom:14px">
        <button class="b neut" type="button"
                onclick="carregarHistorico()">Atualizar</button>
        <div>
          <label style="margin:0 0 3px">Google Earth: colorir por</label>
          <select id="campoKml" style="margin:0">
            <option value="sinal">RSSI (dBm)</option>
            <option value="snr">SNR (dB)</option>
            <option value="rtt">Latência (ms)</option>
            <option value="perda">Perda (%)</option>
          </select>
        </div>
      </div>
      <div class="rolar"><table id="tabHist"><tbody></tbody></table></div>
    </div>

    <!-- ══════════════ MEDIÇÕES ══════════════ -->
    <div class="painel" id="p-medicoes">
      <label>Survey</label>
      <select id="medSurvey" onchange="carregarMedicoes()"></select>

      <div class="subabas" style="margin-top:16px">
        <button type="button" data-s="lancar" class="on">Lançar</button>
        <button type="button" data-s="importar">Importar CSV</button>
      </div>

      <div id="s-lancar">
        <div class="radios">
          <label><input type="radio" name="medtipo" value="iperf" checked
                        onchange="trocarTipo()"> Throughput (iperf)</label>
          <label><input type="radio" name="medtipo" value="trace"
                        onchange="trocarTipo()"> Trace</label>
        </div>
        <div id="fIperf">
          <div class="linha">
            <div><label class="fraco">Data</label><input type="date" id="ipData"></div>
            <div><label class="fraco">Banda</label><select id="ipBanda">
              <option>2.4 GHz</option><option>5.8 GHz</option></select></div>
            <div><label class="fraco">Mbps</label>
              <input type="number" id="ipMbps" step="0.1" min="0"></div>
          </div>
          <div class="linha" style="margin-top:10px">
            <div><label class="fraco">Local</label><input type="text" id="ipLocal"></div>
            <div><label class="fraco">Latitude</label>
              <input type="number" id="ipLat" step="0.0000001"></div>
            <div><label class="fraco">Longitude</label>
              <input type="number" id="ipLon" step="0.0000001"></div>
          </div>
          <div style="margin-top:10px"><label class="fraco">Observação</label>
            <input type="text" id="ipObs"></div>
        </div>
        <div id="fTrace" style="display:none">
          <div class="linha">
            <div><label class="fraco">Data</label><input type="date" id="trData"></div>
            <div><label class="fraco">Origem</label><input type="text" id="trOrig"></div>
            <div><label class="fraco">Destino</label><input type="text" id="trDest"></div>
          </div>
          <div class="linha" style="margin-top:10px">
            <div><label class="fraco">Saltos</label>
              <input type="number" id="trSaltos" min="0" step="1"></div>
            <div><label class="fraco">Custo total</label>
              <input type="number" id="trCusto" min="0" step="1"></div>
            <div><label class="fraco">Gargalo</label><input type="text" id="trGarg"></div>
          </div>
          <div style="margin-top:10px"><label class="fraco">Observação</label>
            <input type="text" id="trObs"></div>
        </div>
        <div style="margin-top:12px">
          <button class="b prim" id="btnAddMed" onclick="adicionarMedicao()">Adicionar</button>
        </div>
        <div class="rolar" style="margin-top:16px">
          <table id="tabMed"><tbody></tbody></table>
        </div>
      </div>

      <div id="s-importar" style="display:none">
        <p class="nota" style="margin-bottom:10px">
          <a class="acao" onclick="baixarModelo()">baixar modelo</a> —
          o CSV aceita <b>;</b> ou <b>,</b> como separador de campo e vírgula
          ou ponto no decimal.
        </p>
        <div class="radios">
          <label><input type="radio" name="imptipo" value="iperf" checked>
            Throughput (iperf)</label>
          <label><input type="radio" name="imptipo" value="trace"> Trace</label>
        </div>
        <input type="file" id="arqCsv" accept=".csv,text/csv">
        <div style="margin-top:12px">
          <button class="b prim" id="btnImp" onclick="importarCsv()">Importar</button>
        </div>
        <div id="resImport" style="margin-top:14px"></div>
      </div>
    </div>
  </div>
</div>

<script>
/* ══════════════════════════════════════════════════════════════
   ADAPTADOR DE ROTAS — única fonte de URLs. Nenhuma URL literal
   espalhada pelo código: as rotas divergiram da spec durante a
   implementação e isolar aqui evita caçar string solta depois.
   ══════════════════════════════════════════════════════════════ */
const API = {
  bcs:            '/bcs',
  capturaIniciar: '/captura/iniciar',
  capturaStatus:  '/captura/status',
  capturaParar:   '/captura/parar',
  capturaImagens: '/captura/imagens',
  capturaAtivas:  '/captura/ativas',
  perfisSalvar:   '/perfis/salvar',
  perfisAbrir:    '/perfis/abrir',
  perfisExcluir:  '/perfis/excluir',
  surveys:        '/surveys',
  survey:         '/survey',
  surveyExcluir:  '/survey/excluir',
  surveyImagens:  '/survey/imagens',
  surveyKml:      '/survey/kml',
  surveyKmzTodos: '/survey/kmz-todos',
  surveyPpt:      '/survey/ppt',
  capturaKml:     '/captura/kml',
  surveyComparar: '/survey/comparar',
  medListar:      '/medicoes/listar',
  medAdicionar:   '/medicoes/adicionar',
  medExcluir:     '/medicoes/excluir',
  medImportar:    '/medicoes/importar',
  medModelo:      '/medicoes/modelo',
  gerar:          '/gerar',
};

const $ = s => document.querySelector(s);
const q = (o) => Object.entries(o).filter(([,v]) => v !== undefined && v !== null
                 && v !== '').map(([k,v]) => k+'='+encodeURIComponent(v)).join('&');

let BCS = [], TAGS = {}, CICLO = null, SEL = new Set(), SURVEYS = [];
let CAP_ID = sessionStorage.getItem('capId') || null;
let TIMER = null;
let SV_EM_USO = sessionStorage.getItem('svEmUso') || null;

/* ── erro global ─────────────────────────────────────────────── */
function erro(msg) {
  const e = $('#erro');
  if (!msg) { e.style.display = 'none'; return; }
  e.textContent = msg; e.style.display = 'block';
}
async function json(url, opts) {
  try {
    const r = await fetch(url, opts);
    const txt = await r.text();
    if (!r.ok) throw new Error(txt.slice(0, 200) || ('HTTP ' + r.status));
    return txt ? JSON.parse(txt) : {};
  } catch (e) {
    if (e instanceof TypeError)
      throw new Error('Sem resposta do servidor. Verifique se o serviço está rodando.');
    throw e;
  }
}
async function comBotao(btn, texto, fn) {
  const orig = btn.textContent;
  btn.disabled = true; btn.textContent = texto;
  try { return await fn(); }
  finally { btn.disabled = false; btn.textContent = orig; }
}

/* ── abas ────────────────────────────────────────────────────── */
$('#abas').addEventListener('click', ev => {
  const b = ev.target.closest('button'); if (!b) return;
  irPara(b.dataset.p);
});
function irPara(nome) {
  erro('');
  document.querySelectorAll('#abas button').forEach(x =>
    x.classList.toggle('on', x.dataset.p === nome));
  document.querySelectorAll('.painel').forEach(x =>
    x.classList.toggle('on', x.id === 'p-' + nome));
  if (nome === 'historico') carregarHistorico();
  if (nome === 'medicoes') carregarSurveysNoSeletor();
}

/* ══════════════ RELATÓRIOS ══════════════ */
const hoje = new Date();
const iso = d => new Date(d.getTime() - d.getTimezoneOffset()*60000)
                   .toISOString().slice(0,10);
$('#fim').value = iso(hoje);
$('#ini').value = iso(new Date(Date.now() - 7*864e5));

function rapido(n) {
  $('#ini').value = iso(new Date(Date.now() - n*864e5));
  $('#fim').value = iso(new Date());
  gerar('xlsx');
}
async function gerar(fmt) {
  erro('');
  const ini = $('#ini').value, fim = $('#fim').value;
  if (!ini || !fim) return erro('Selecione as duas datas.');
  if (ini > fim)    return erro('A data inicial não pode ser depois da final.');
  const btn = fmt === 'pptx' ? $('#btnPpt') : $('#btnXls');
  await comBotao(btn, 'Gerando…', async () => {
    const p = {ini: ini, fim: fim, demo: $('#demo').checked ? '1' : '0', fmt: fmt};
    if (SV_EM_USO) p.survey_id = SV_EM_USO;
    try {
      const r = await fetch(API.gerar + '?' + q(p));
      if (!r.ok) throw new Error(await r.text());
      /* O survey pode falhar sem derrubar o relatorio. Antes isso so ia
         para o log do servidor e o PPT chegava sem os slides, calado. */
      const av = r.headers.get('X-Survey-Aviso');
      if (av) erro('Relatório gerado, mas o SURVEY não entrou: ' + av);
      await baixarResposta(r, 'relatorio.' + fmt);
    } catch (e) { erro('Erro ao gerar: ' + e.message); }
  });
}
async function baixarResposta(r, padrao) {
  const cd = r.headers.get('Content-Disposition') || '';
  const m = cd.match(/filename="?([^"]+)"?/);
  const blob = await r.blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = m ? m[1] : padrao;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(a.href);
}

/* ══════════════ SURVEY — rádios ══════════════ */
async function carregarBcs() {
  try {
    const d = await json(API.bcs);
    BCS = d.bcs || []; TAGS = d.tags || {}; CICLO = d.ciclo_s || null;
    montarTags(); montarPerfis(d.perfis || []); montarLista();
    if (CICLO) validarIntervalo();
  } catch (e) { erro(e.message); }
}
function montarTags() {
  const el = $('#tags'); el.innerHTML = '';
  Object.keys(TAGS).sort().forEach(t => {
    const b = document.createElement('button');
    b.type = 'button'; b.dataset.t = t;
    b.textContent = t + ' (' + TAGS[t] + ')';
    b.onclick = () => alternarTag(t);
    el.appendChild(b);
  });
  pintarTags();
}
const daTag = t => BCS.filter(b => b.tag === t);
function alternarTag(t) {
  const its = daTag(t);
  const todos = its.length && its.every(b => SEL.has(b.ip));
  its.forEach(b => todos ? SEL.delete(b.ip) : SEL.add(b.ip));
  pintarTags(); montarLista();
}
function pintarTags() {
  document.querySelectorAll('#tags button').forEach(b => {
    const its = daTag(b.dataset.t);
    b.classList.toggle('on', its.length > 0 && its.every(x => SEL.has(x.ip)));
  });
}
function montarPerfis(nomes) {
  const s = $('#perfil');
  s.innerHTML = '<option value="">— perfil —</option>';
  nomes.forEach(n => { const o = document.createElement('option');
                       o.value = n; o.textContent = n; s.appendChild(o); });
}
function visiveis() {
  const busca = ($('#busca').value || '').trim().toUpperCase();
  const soG = $('#soGps').checked, soO = $('#soOnline').checked;
  return BCS.filter(b =>
    (!busca || (b.nome || '').toUpperCase().includes(busca)) &&
    (!soG || b.gps) && (!soO || b.online));
}
function montarLista() {
  const el = $('#lista'); el.innerHTML = '';
  const its = visiveis().slice().sort((a, b) =>
    (a.gps === b.gps) ? String(a.nome).localeCompare(String(b.nome))
                      : (a.gps ? -1 : 1));
  if (!its.length) {
    el.innerHTML = '<div class="vazio">Nenhum rádio corresponde ao filtro.</div>';
  }
  its.forEach(b => {
    const d = document.createElement('div');
    d.className = 'item' + (b.gps ? '' : ' semgps');
    const detalhes = [b.ip];
    if (!b.gps) detalhes.push('sem GPS');
    if (!b.online) detalhes.push('offline');
    const c = document.createElement('input');
    c.type = 'checkbox'; c.checked = SEL.has(b.ip);
    c.onchange = () => { c.checked ? SEL.add(b.ip) : SEL.delete(b.ip);
                         pintarTags(); atualizarResumo(); };
    const nm = document.createElement('span');
    nm.className = 'nm'; nm.textContent = b.nome;
    const tg = document.createElement('span');
    tg.className = 'tg'; tg.textContent = b.tag || '?';
    const dt = document.createElement('span');
    dt.className = 'dt'; dt.textContent = detalhes.join(' · ');
    d.append(c, nm, tg, dt);
    el.appendChild(d);
  });
  atualizarResumo();
}
function atualizarResumo() {
  const sel = BCS.filter(b => SEL.has(b.ip));
  const comGps = sel.filter(b => b.gps).length;
  const sem = sel.length - comGps;
  let txt = sel.length + ' selecionados · ' + comGps + ' com GPS';
  $('#resumo').innerHTML = txt + (sem
    ? ' · <em>' + sem + ' sem GPS não entrarão no mapa</em>' : '');
  validarIntervalo();
}
function marcarTodos(v) {
  visiveis().forEach(b => v ? SEL.add(b.ip) : SEL.delete(b.ip));
  pintarTags(); montarLista();
}
function marcarGps() {
  SEL.clear(); BCS.filter(b => b.gps).forEach(b => SEL.add(b.ip));
  pintarTags(); montarLista();
}
['busca','soGps','soOnline'].forEach(id =>
  $('#'+id).addEventListener('input', montarLista));

function aplicarColadas() {
  erro('');
  const bruto = ($('#colar').value || '').split(/[,;\n\r]+/)
                  .map(s => s.trim()).filter(Boolean);
  if (!bruto.length) { $('#achados').innerHTML = ''; return; }
  const porNome = new Map(BCS.map(b => [String(b.nome).toUpperCase(), b]));
  const achou = [], faltou = [];
  bruto.forEach(t => {
    const b = porNome.get(t.toUpperCase());
    if (b) achou.push(b); else faltou.push(t);
  });
  SEL.clear(); achou.forEach(b => SEL.add(b.ip));
  pintarTags(); montarLista();
  $('#achados').innerHTML = '<b>' + achou.length + ' casados</b>'
    + (faltou.length ? ' · <i>' + faltou.length + ' não encontrados: '
                       + faltou.join(', ') + '</i>' : '');
}

$('#perfil').addEventListener('change', async ev => {
  const nome = ev.target.value; if (!nome) return;
  try {
    const d = await json(API.perfisAbrir + '?' + q({nome: nome}));
    SEL = new Set(d.ips || []);
    pintarTags(); montarLista();
  } catch (e) { erro(e.message); }
});
async function salvarPerfil() {
  erro('');
  if (!SEL.size) return erro('Selecione ao menos um BreadCrumb.');
  const nome = prompt('Nome do perfil:');
  if (!nome) return;
  try {
    const d = await json(API.perfisSalvar + '?' +
                         q({nome: nome, ips: [...SEL].join(',')}));
    montarPerfis(d.perfis || []); $('#perfil').value = nome;
  } catch (e) { erro(e.message); }
}
async function excluirPerfil() {
  erro('');
  const nome = $('#perfil').value;
  if (!nome) return erro('Escolha um perfil para excluir.');
  if (!confirm('Excluir o perfil "' + nome + '"?')) return;
  try {
    const d = await json(API.perfisExcluir + '?' + q({nome: nome}));
    montarPerfis(d.perfis || []);
  } catch (e) { erro(e.message); }
}

/* ── coleta ──────────────────────────────────────────────────── */
$('#svNome').value = 'Survey ' + iso(hoje);
$('#presets').addEventListener('click', ev => {
  const b = ev.target.closest('button'); if (!b) return;
  $('#minutos').value = b.dataset.m;
  document.querySelectorAll('#presets button').forEach(x =>
    x.classList.toggle('on', x === b));
});
$('#minutos').addEventListener('input', () =>
  document.querySelectorAll('#presets button').forEach(x =>
    x.classList.toggle('on', x.dataset.m === $('#minutos').value)));
$('#intervalo').addEventListener('change', validarIntervalo);
$('#intervalo').addEventListener('input', validarIntervalo);
/* A captura consulta os rádios DIRETO, no intervalo pedido — não lê mais o
   cache do exporter. Então o que o operador precisa saber não é o ciclo do
   exporter, e sim quanta consulta isso põe na malha. O exporter continua
   rodando: a carga real é a soma dos dois. */
function validarIntervalo() {
  const el = $('#avisoIntervalo');
  const v = parseInt($('#intervalo').value || '0', 10);
  const n = SEL.size;
  if (!n) { el.style.display = 'none'; return; }
  if (!v) {
    // Continuo: nao ha "a cada X s" para dividir, e a taxa passa a ser o
    // tempo de resposta do radio. Dizer isso evita tanto o susto do
    // numero quanto a impressao de que da para pedir a frota inteira.
    el.classList.toggle('atencao', n > 12);
    el.textContent = 'Contínuo: mede sem pausa entre ciclos — é o que dá '
                   + 'rastro sem espaçamento. Com ' + n + ' rádio(s), a '
                   + 'cadência passa a ser o tempo de resposta deles.'
                   + (n > 12 ? ' Seleção grande para contínuo: prefira só '
                             + 'os veículos do trajeto.' : '');
    el.style.display = 'block';
    return;
  }
  const cps = n / v;
  el.classList.toggle('atencao', cps > 10);
  el.textContent = n + ' rádios a cada ' + v + 's ≈ ' + cps.toFixed(1)
                 + ' consultas/s (além do exporter).'
                 + (cps > 10 ? ' Carga alta na malha. Aumente o intervalo'
                             + ' ou reduza a seleção.' : '');
  el.style.display = 'block';
  return;
}

async function iniciar() {
  erro('');
  if (!SEL.size)  return erro('Selecione ao menos um BreadCrumb.');
  const nome = ($('#svNome').value || '').trim();
  if (!nome)      return erro('Dê um nome ao survey.');
  await comBotao($('#btnIniciar'), 'Iniciando…', async () => {
    try {
      const d = await json(API.capturaIniciar + '?' + q({
        ips: [...SEL].join(','), minutos: $('#minutos').value,
        intervalo: $('#intervalo').value, rotulo: nome, nome: nome,
        alcance: $('#alcance').value}));
      CAP_ID = d.id; sessionStorage.setItem('capId', CAP_ID);
      if (d.aviso) { $('#avisoIntervalo').textContent = d.aviso;
                     $('#avisoIntervalo').classList.add('atencao');
                     $('#avisoIntervalo').style.display = 'block'; }
      $('#acoesPronto').style.display = 'none';
      acompanhar();
    } catch (e) { erro(e.message); }
  });
}
async function parar() {
  if (!CAP_ID) return;
  try { await json(API.capturaParar + '?' + q({id: CAP_ID})); }
  catch (e) { erro(e.message); }
}
function acompanhar() {
  if (TIMER) clearInterval(TIMER);
  const passo = async () => {
    if (!CAP_ID) return;
    try {
      const s = await json(API.capturaStatus + '?' + q({id: CAP_ID}));
      pintarStatus(s);
      if (s.estado === 'pronto' || s.estado === 'erro') {
        clearInterval(TIMER); TIMER = null;
        $('#btnParar').disabled = true;
        if (s.estado === 'pronto') $('#acoesPronto').style.display = 'block';
        else erro('Falhou: ' + (s.erro || 'motivo não informado'));
      }
    } catch (e) { clearInterval(TIMER); TIMER = null; erro(e.message); }
  };
  passo(); TIMER = setInterval(passo, 2000);
}
function pintarStatus(s) {
  $('#prog').style.width = (s.pct || 0) + '%';
  $('#btnParar').disabled = !(s.estado === 'coletando');
  const el = $('#statusCap');
  if (s.estado === 'coletando') {
    const r = s.restante_s || 0;
    const m = Math.floor(r / 60), seg = r % 60;
    let t = 'Coletando… restam ' + m + 'm' + String(seg).padStart(2,'0')
      + 's · ' + (s.amostras || 0) + ' amostras · '
      + (s.equipamentos || 0) + ' equipamentos em movimento · '
      + (s.fixos || 0) + ' fixos';
    /* O intervalo efetivo só aparece quando DIVERGE do pedido: se o ciclo
       está dando conta, repetir o número certo é ruído. Quando estoura, é a
       informação mais importante da tela — é a resolução real do trajeto. */
    const ef = s.intervalo_efetivo_s, pd = s.intervalo_s;
    if (ef && pd && Math.abs(ef - pd) >= 1)
      t += ' · intervalo efetivo: ' + ef + 's (pedido: ' + pd + 's)';
    if (s.radios_descartados)
      t += ' · ' + s.radios_descartados + ' rádio(s) sem resposta';
    if (s.amostras_cache)
      t += ' · ' + s.amostras_cache + ' do cache';
    el.textContent = t;
  } else if (s.estado === 'gerando') {
    el.textContent = 'Gerando imagens e salvando…';
  } else if (s.estado === 'pronto') {
    el.textContent = 'Pronto: ' + (s.equipamentos || 0) + ' rotas, '
      + (s.fixos || 0) + ' pontos fixos, ' + (s.medidas || 0) + ' medições.';
  } else if (s.estado === 'erro') {
    el.textContent = 'Falhou.';
  }
}
function baixarImagensCaptura() {
  if (!CAP_ID) return;
  window.location = API.capturaImagens + '?' + q({id: CAP_ID});
}
/* Grandeza que colore os pontos no Google Earth. O seletor fica no
   histórico; sem ele, RSSI — é o requisito que a Modular cobra primeiro. */
function campoKml() {
  const el = document.getElementById('campoKml');
  return (el && el.value) || 'sinal';
}
function baixarKmlCaptura() {
  if (!CAP_ID) return;
  window.location = API.capturaKml + '?' + q({id: CAP_ID, campo: campoKml()});
}
/* Reconexão: a captura roda no servidor, fechar a aba não interrompe. */
async function reconectar() {
  try {
    if (CAP_ID) {
      const s = await json(API.capturaStatus + '?' + q({id: CAP_ID}));
      if (s && s.estado) {
        pintarStatus(s);
        if (s.estado === 'coletando' || s.estado === 'gerando') return acompanhar();
        if (s.estado === 'pronto') $('#acoesPronto').style.display = 'block';
        return;
      }
    }
    const d = await json(API.capturaAtivas);
    const a = (d.capturas || [])[0];
    if (a) { CAP_ID = a.id; sessionStorage.setItem('capId', CAP_ID); acompanhar(); }
  } catch (e) { /* servidor reiniciado: segue sem acompanhamento */ }
}

/* ══════════════ HISTÓRICO ══════════════ */
const pct = v => (v === null || v === undefined) ? '—'
  : v.toLocaleString('pt-BR', {minimumFractionDigits:1, maximumFractionDigits:1}) + '%';
function classePct(v) {
  if (v === null || v === undefined) return '';
  return v >= 95 ? 'bom' : (v >= 80 ? 'aten' : 'ruim');
}
function dataHora(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, '0');
  return p(d.getDate()) + '/' + p(d.getMonth()+1) + '/' + d.getFullYear()
       + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}
function duracao(s) {
  if (!s || s <= 0) return '—';
  const m = Math.round(s / 60);
  return m >= 60 ? (Math.floor(m/60) + 'h' + String(m%60).padStart(2,'0')) : (m + 'min');
}
async function carregarHistorico() {
  erro('');
  const tb = $('#tabHist').querySelector('tbody');
  try {
    const d = await json(API.surveys);
    SURVEYS = d.surveys || [];
  } catch (e) { erro(e.message); return; }
  if (!SURVEYS.length) {
    $('#tabHist').innerHTML =
      '<tbody><tr><td class="vazio">Nenhum survey registrado ainda.</td></tr></tbody>';
    return;
  }
  const cab = ['Nome','Data/hora','Duração','Equip.','Amostras',
               'RSSI ok','SNR ok','Latência ok','Perda ok','Ações'];
  let html = '<thead><tr>' + cab.map(c => '<th>'+c+'</th>').join('') + '</tr></thead><tbody>';
  SURVEYS.forEach(s => {
    const r = s.resumo || {};
    const eq = (s.n_moveis || 0) + (s.n_fixos || 0);
    const cel = k => {
      const v = r[k] ? r[k].pct_ok : null;
      return '<td class="' + classePct(v) + '">' + pct(v) + '</td>';
    };
    const emUso = String(s.id) === String(SV_EM_USO);
    html += '<tr class="' + (emUso ? 'emuso' : '') + '">'
      + '<td><b>' + (s.nome || '—') + '</b>'
      + (emUso ? ' <span class="pill">em uso</span>' : '') + '</td>'
      + '<td>' + dataHora(s.inicio) + '</td>'
      + '<td>' + duracao((s.fim || 0) - (s.inicio || 0)) + '</td>'
      + '<td>' + eq + ' (' + (s.n_moveis||0) + '+' + (s.n_fixos||0) + ')</td>'
      + '<td>' + (s.n_amostras || 0) + '</td>'
      + cel('sinal') + cel('snr') + cel('rtt') + cel('perda')
      + '<td style="white-space:nowrap">'
      /* Rótulos curtos: a coluna de ações é a última de uma tabela larga
         e legendas longas eram cortadas pela rolagem — "Google Earth"
         aparecia como "Go". O title explica sem ocupar largura. */
      + '<button class="acao" data-a="img" title="Baixar os PNGs do survey">'
      + 'Imagens</button>'
      + '<button class="acao" data-a="kml" title="Abrir no Google Earth '
      + '(satélite real da mina)">KML</button>'
      + '<button class="acao" data-a="kmzs" title="Baixar um KMZ por '
      + 'grandeza e banda, para montar os slides à mão">KMZ+</button>'
      + '<button class="acao" data-a="ppt" title="Deck exclusivo de site '
      + 'survey, na identidade Anglo">PPT</button>'
      + '<button class="acao" data-a="usar" title="Usar este survey no '
      + 'relatório">Usar</button>'
      + '<button class="acao perigo" data-a="del">Excluir</button></td></tr>';
  });
  $('#tabHist').innerHTML = html + '</tbody>';
  const rol = $('#tabHist').closest('.rolar');
  if (rol) rol.scrollLeft = 0;
  $('#tabHist').querySelectorAll('button.acao').forEach((b, i) => {
    const linha = b.closest('tr');
    const idx = [...linha.parentNode.children].indexOf(linha);
    const s = SURVEYS[idx];
    b.onclick = () => acaoHistorico(b.dataset.a, s);
  });
}
async function acaoHistorico(acao, s) {
  erro('');
  if (acao === 'img') {
    window.location = API.surveyImagens + '?' + q({id: s.id});
  } else if (acao === 'ppt') {
    /* Deck so de survey, separado do relatorio semanal. */
    window.location = API.surveyPpt + '?' + q({id: s.id});
  } else if (acao === 'kmzs') {
    /* Um KMZ por grandeza e banda, num ZIP com LEIA-ME: é o material de
       quem tira o print no Google Earth e cola na moldura do slide. */
    window.location = API.surveyKmzTodos + '?' + q({id: s.id});
  } else if (acao === 'kml') {
    /* O Google Earth já traz o satélite da mina — é o fundo que a rede
       daqui não deixa baixar para o PNG. */
    window.location = API.surveyKml + '?' + q({id: s.id, campo: campoKml()});
  } else if (acao === 'usar') {
    SV_EM_USO = s.id; sessionStorage.setItem('svEmUso', String(s.id));
    marcarEmUso(); carregarHistorico();
  } else if (acao === 'del') {
    if (!confirm('Excluir o survey "' + (s.nome || '') +
                 '"? Esta ação não pode ser desfeita.')) return;
    try {
      await json(API.surveyExcluir + '?' + q({id: s.id}));
      if (String(s.id) === String(SV_EM_USO)) {
        SV_EM_USO = null; sessionStorage.removeItem('svEmUso'); marcarEmUso();
      }
      carregarHistorico();
    } catch (e) { erro(e.message); }
  }
}
function marcarEmUso() {
  const s = SURVEYS.find(x => String(x.id) === String(SV_EM_USO));
  $('#svEmUso').innerHTML = SV_EM_USO
    ? 'Survey em uso no relatório: <b>' + (s ? s.nome : SV_EM_USO) + '</b>'
    : '';
}

/* ══════════════ MEDIÇÕES ══════════════ */
document.querySelector('.subabas').addEventListener('click', ev => {
  const b = ev.target.closest('button'); if (!b) return;
  document.querySelectorAll('.subabas button').forEach(x =>
    x.classList.toggle('on', x === b));
  $('#s-lancar').style.display   = b.dataset.s === 'lancar' ? 'block' : 'none';
  $('#s-importar').style.display = b.dataset.s === 'importar' ? 'block' : 'none';
});
function trocarTipo() {
  const t = document.querySelector('input[name=medtipo]:checked').value;
  $('#fIperf').style.display = t === 'iperf' ? 'block' : 'none';
  $('#fTrace').style.display = t === 'trace' ? 'block' : 'none';
  carregarMedicoes();
}
async function carregarSurveysNoSeletor() {
  try {
    const d = await json(API.surveys);
    SURVEYS = d.surveys || [];
  } catch (e) { erro(e.message); return; }
  const s = $('#medSurvey'); const antes = s.value;
  s.innerHTML = '';
  SURVEYS.forEach(x => {
    const o = document.createElement('option');
    o.value = x.id;
    o.textContent = (x.nome || '—') + ' — ' + dataHora(x.inicio).slice(0, 5);
    s.appendChild(o);
  });
  if (antes && SURVEYS.some(x => String(x.id) === antes)) s.value = antes;
  carregarMedicoes();
}
$('#ipData').value = iso(hoje); $('#trData').value = iso(hoje);

async function adicionarMedicao() {
  erro('');
  const sid = $('#medSurvey').value;
  if (!sid) return erro('Escolha um survey.');
  const tipo = document.querySelector('input[name=medtipo]:checked').value;
  const corpo = {survey_id: sid, tipo: tipo};
  if (tipo === 'iperf') {
    corpo.data = $('#ipData').value; corpo.local = $('#ipLocal').value;
    corpo.lat = $('#ipLat').value;   corpo.lon = $('#ipLon').value;
    corpo.banda = $('#ipBanda').value; corpo.mbps = $('#ipMbps').value;
    corpo.obs = $('#ipObs').value;
    if (!corpo.mbps) return erro('Informe o throughput em Mbps.');
  } else {
    corpo.data = $('#trData').value; corpo.origem = $('#trOrig').value;
    corpo.destino = $('#trDest').value; corpo.saltos = $('#trSaltos').value;
    corpo.custo_total = $('#trCusto').value; corpo.gargalo = $('#trGarg').value;
    corpo.obs = $('#trObs').value;
    if (!corpo.destino) return erro('Informe o destino do trace.');
  }
  await comBotao($('#btnAddMed'), 'Enviando…', async () => {
    try {
      await json(API.medAdicionar, {method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(corpo)});
      ['ipLocal','ipLat','ipLon','ipMbps','ipObs','trOrig','trDest','trSaltos',
       'trCusto','trGarg','trObs'].forEach(id => { if ($('#'+id)) $('#'+id).value=''; });
      carregarMedicoes();
    } catch (e) { erro(e.message); }
  });
}
async function carregarMedicoes() {
  const sid = $('#medSurvey').value;
  const tb = $('#tabMed');
  if (!sid) { tb.innerHTML = '<tbody><tr><td class="vazio">Nenhum survey.</td></tr></tbody>'; return; }
  let ms = [];
  try {
    const d = await json(API.medListar + '?' + q({survey_id: sid}));
    ms = d.medicoes || [];
  } catch (e) { erro(e.message); return; }
  const tipo = document.querySelector('input[name=medtipo]:checked').value;
  ms = ms.filter(m => m.tipo === tipo);
  if (!ms.length) {
    tb.innerHTML = '<tbody><tr><td class="vazio">Nenhuma medição lançada neste survey.</td></tr></tbody>';
    return;
  }
  const dataBr = s => s ? String(s).split('-').reverse().join('/') : '—';
  const nb = v => (v === null || v === undefined || v === '') ? '—'
    : Number(v).toLocaleString('pt-BR');
  let cab, linhas;
  if (tipo === 'iperf') {
    cab = ['Data','Local','Banda','Mbps','Obs.',''];
    linhas = ms.map(m => [dataBr(m.data), m.local || '—', m.banda || '—',
                          nb(m.mbps), m.obs || '', m.id]);
  } else {
    cab = ['Data','Origem','Destino','Saltos','Custo','Gargalo',''];
    linhas = ms.map(m => [dataBr(m.data), m.origem || '—', m.destino || '—',
                          nb(m.saltos), nb(m.custo_total), m.gargalo || '—', m.id]);
  }
  let html = '<thead><tr>' + cab.map(c => '<th>'+c+'</th>').join('') + '</tr></thead><tbody>';
  linhas.forEach(l => {
    const id = l[l.length-1];
    html += '<tr>' + l.slice(0,-1).map(v => '<td>'+v+'</td>').join('')
         + '<td><button class="acao perigo" data-id="'+id+'">excluir</button></td></tr>';
  });
  tb.innerHTML = html + '</tbody>';
  tb.querySelectorAll('button.acao').forEach(b => {
    b.onclick = async () => {
      if (!confirm('Excluir esta medição?')) return;
      try { await json(API.medExcluir + '?' + q({id: b.dataset.id}));
            carregarMedicoes(); }
      catch (e) { erro(e.message); }
    };
  });
}
function baixarModelo() {
  const t = document.querySelector('input[name=imptipo]:checked').value;
  window.location = API.medModelo + '?' + q({tipo: t});
}
async function importarCsv() {
  erro('');
  const sid = $('#medSurvey').value;
  if (!sid) return erro('Escolha um survey.');
  const f = $('#arqCsv').files[0];
  if (!f) return erro('Escolha um arquivo CSV.');
  const tipo = document.querySelector('input[name=imptipo]:checked').value;
  await comBotao($('#btnImp'), 'Importando…', async () => {
    try {
      const texto = await f.text();
      const d = await json(API.medImportar + '?' + q({survey_id: sid, tipo: tipo}),
        {method: 'POST', headers: {'Content-Type': 'text/csv'}, body: texto});
      const erros = d.erros || [];
      let html = '<p class="nota"><b>' + (d.inseridas || 0)
               + ' linhas importadas</b>'
               + (erros.length ? ' · ' + erros.length + ' com erro' : '') + '</p>';
      if (erros.length)
        html += '<p style="font-size:12.5px;color:#C0392B">'
              + erros.map(e => String(e)).join('<br>') + '</p>';
      $('#resImport').innerHTML = html;
      carregarMedicoes();
    } catch (e) { erro(e.message); }
  });
}

/* ══════════════ INÍCIO ══════════════ */
carregarBcs();
marcarEmUso();
reconectar();
</script>
</body></html>"""

def criar_handler(cfg):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a): pass  # silencia log padrão

        def do_POST(self):
            """Medições manuais: lançamento avulso e importação de CSV.

            POST porque o CSV vai no corpo — colar uma planilha inteira numa
            query string não passa nos limites de URL.
            """
            parsed = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(parsed.query)
            try:
                tam = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                tam = 0
            corpo = self.rfile.read(min(tam, 8 * 1024 * 1024)) if tam else b""

            if parsed.path == "/medicoes/adicionar":
                try:
                    dados = json.loads(corpo.decode("utf-8") or "{}")
                except Exception:
                    self._envia(400, b'{"erro":"JSON invalido"}',
                                "application/json"); return
                sid  = dados.get("survey_id")
                tipo = dados.get("tipo")
                if not sid or tipo not in ("iperf", "trace"):
                    self._envia(400, b'{"erro":"survey_id e tipo obrigatorios"}',
                                "application/json"); return
                for c in ("lat", "lon", "mbps", "custo_total"):
                    if dados.get(c) not in (None, ""):
                        dados[c] = _num(dados[c])
                if dados.get("saltos") not in (None, ""):
                    s = _num(dados["saltos"])
                    dados["saltos"] = int(s) if s is not None else None
                con = banco()
                try:
                    manual_gravar(con, sid, tipo, dados)
                    novo = con.execute("SELECT MAX(id) FROM medicao_manual "
                                       "WHERE survey_id=?", (sid,)).fetchone()[0]
                finally:
                    con.close()
                self._envia(200, json.dumps({"id": novo}).encode(),
                            "application/json; charset=utf-8")
                return

            if parsed.path == "/medicoes/importar":
                sid  = q.get("survey_id", [""])[0]
                tipo = q.get("tipo", ["iperf"])[0]
                if not sid:
                    self._envia(400, b'{"erro":"survey_id obrigatorio"}',
                                "application/json"); return
                texto = corpo.decode("utf-8-sig", errors="replace")
                con = banco()
                try:
                    n, erros = importar_csv_manual(con, sid, tipo, texto)
                finally:
                    con.close()
                # Linha ruim não aborta o lote: informa e segue.
                self._envia(200, json.dumps({"inseridas": n,
                                             "erros": erros}).encode(),
                            "application/json; charset=utf-8")
                return

            self._envia(404, b'{"erro":"rota nao encontrada"}',
                        "application/json")
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
            # ── Lista de BreadCrumbs disponíveis para a captura ──
            if parsed.path == "/bcs":
                col = COLETOR_ATUAL.get("ref")
                itens = inventario_bcs(col)
                # `tag` = prefixo do nome (ERM-20 -> ERM). É o que alimenta
                # os botões de seleção em massa: com 150 nós, checkbox um a
                # um não escala.
                pref = [p for _, p in prefixos_frota(cfg)]
                for i in itens:
                    i["tag"] = tag_do_nome(i["nome"], pref)
                contagem = {}
                for i in itens:
                    if i["tag"]:
                        contagem[i["tag"]] = contagem.get(i["tag"], 0) + 1
                corpo = json.dumps({
                    "bcs": itens,
                    "ciclo_s": getattr(col, "interval", None) if col else None,
                    "tags": contagem,
                    "rotulos": {p: r for r, p in prefixos_frota(cfg)},
                    "perfis": sorted(perfis_salvos(cfg).keys()),
                })
                self._envia(200, corpo.encode("utf-8"), "application/json; charset=utf-8")
                return

            # ── Perfis de seleção ──────────────────────────────
            if parsed.path == "/perfis/salvar":
                q = urllib.parse.parse_qs(parsed.query)
                nome = (q.get("nome", [""])[0] or "").strip()
                ips  = [i for i in (q.get("ips", [""])[0] or "").split(",")
                        if i.strip()]
                if not nome or not ips:
                    self._envia(400, b'{"erro":"nome e selecao obrigatorios"}',
                                "application/json"); return
                perfil_gravar(cfg, nome, ips)
                self._envia(200, json.dumps({"ok": True,
                    "perfis": sorted(perfis_salvos(cfg).keys())}).encode(),
                    "application/json; charset=utf-8")
                return

            if parsed.path == "/perfis/excluir":
                nome = urllib.parse.parse_qs(parsed.query).get("nome", [""])[0]
                perfil_excluir(cfg, nome)
                self._envia(200, json.dumps({"ok": True,
                    "perfis": sorted(perfis_salvos(cfg).keys())}).encode(),
                    "application/json; charset=utf-8")
                return

            if parsed.path == "/perfis/abrir":
                nome = urllib.parse.parse_qs(parsed.query).get("nome", [""])[0]
                self._envia(200, json.dumps(
                    {"ips": perfil_ips(cfg, nome)}).encode(),
                    "application/json; charset=utf-8")
                return

            # ── Capturas em andamento (reconexão ao reabrir a aba) ──
            # A captura roda no servidor: fechar o navegador não interrompe.
            if parsed.path == "/captura/ativas":
                with _CAP_LOCK:
                    ativas = [dict(j.status(), nome=j.rotulo)
                              for j in CAPTURAS.values()
                              if j.estado in ("coletando", "gerando")]
                self._envia(200, json.dumps({"capturas": ativas}).encode(),
                            "application/json; charset=utf-8")
                return

            # ── Histórico de surveys ───────────────────────────
            if parsed.path == "/surveys":
                q = urllib.parse.parse_qs(parsed.query)
                try: lim = int(q.get("limite", ["100"])[0])
                except ValueError: lim = 100
                con = banco()
                try:
                    lst = survey_listar(con, lim)
                    for s in lst:
                        s["resumo"] = json.loads(s.get("resumo") or "{}")
                        s["calibracao"] = json.loads(s.get("calibracao") or "{}")
                        s["radios"] = json.loads(s.get("radios") or "[]")
                finally: con.close()
                self._envia(200, json.dumps({"surveys": lst}).encode(),
                            "application/json; charset=utf-8")
                return

            if parsed.path == "/survey":
                sid = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
                con = banco()
                try:
                    sv = survey_obter(con, sid)
                    if not sv:
                        self._envia(404, b'{"erro":"survey nao encontrado"}',
                                    "application/json"); return
                    sv["resumo"] = json.loads(sv.get("resumo") or "{}")
                    sv["calibracao"] = json.loads(sv.get("calibracao") or "{}")
                    sv["radios"] = json.loads(sv.get("radios") or "[]")
                    corpo = json.dumps({
                        "survey": sv,
                        "amostras": survey_amostras(con, sid),
                        "medicoes": manual_listar(con, sid)})
                finally: con.close()
                self._envia(200, corpo.encode(), "application/json; charset=utf-8")
                return

            if parsed.path == "/survey/excluir":
                sid = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
                con = banco()
                try: survey_excluir(con, sid)
                finally: con.close()
                self._envia(200, b'{"ok":true}', "application/json")
                return

            if parsed.path == "/survey/comparar":
                sid = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
                con = banco()
                try:
                    sv = survey_obter(con, sid)
                    if not sv:
                        self._envia(404, b'{"erro":"survey nao encontrado"}',
                                    "application/json"); return
                    atual = json.loads(sv.get("resumo") or "{}")
                    ant = survey_anterior(con, sid)
                    anterior = json.loads(ant.get("resumo") or "{}") if ant else None
                    corpo = json.dumps({
                        "atual": atual, "anterior": anterior,
                        "anterior_nome": ant.get("nome") if ant else None,
                        "delta": comparar_surveys(atual, anterior)})
                finally: con.close()
                self._envia(200, corpo.encode(), "application/json; charset=utf-8")
                return

            # ── Imagens do survey (ZIP), do histórico ou da captura ──
            if parsed.path in ("/survey/imagens", "/captura/imagens"):
                q = urllib.parse.parse_qs(parsed.query)
                sid = q.get("id", [""])[0]
                banda = q.get("banda", [""])[0] or None
                if parsed.path == "/captura/imagens":
                    job = CAPTURAS.get(sid)
                    if not job or not job.survey_id:
                        self._envia(404, b"captura sem survey",
                                    "text/plain; charset=utf-8"); return
                    sid = job.survey_id
                try:
                    dados, nome = zip_imagens_survey(sid, banda, cfg)
                except Exception as e:
                    self._envia(500, str(e).encode(),
                                "text/plain; charset=utf-8"); return
                self._envia(200, dados, "application/zip",
                            {"Content-Disposition": f'attachment; filename="{nome}"'})
                return

            # ── KMZ para o Google Earth ────────────────────────
            # O Google Earth já traz o satélite da mina, que é o fundo que
            # a rede daqui não deixa baixar.
            if parsed.path == "/survey/ppt":
                sid = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
                try:
                    dados, nome = ppt_survey_anglo(sid, cfg)
                except Exception as e:
                    self._envia(500, str(e).encode(),
                                "text/plain; charset=utf-8"); return
                self._envia(200, dados,
                            "application/vnd.openxmlformats-officedocument"
                            ".presentationml.presentation",
                            {"Content-Disposition": f'attachment; filename="{nome}"'})
                return

            if parsed.path == "/survey/kmz-todos":
                sid = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
                try:
                    dados, nome = gerar_todos_kmz(sid, cfg)
                except Exception as e:
                    self._envia(500, str(e).encode(),
                                "text/plain; charset=utf-8"); return
                self._envia(200, dados, "application/zip",
                            {"Content-Disposition": f'attachment; filename="{nome}"'})
                return

            if parsed.path in ("/survey/kml", "/captura/kml"):
                q = urllib.parse.parse_qs(parsed.query)
                sid = q.get("id", [""])[0]
                campo = q.get("campo", ["sinal"])[0]
                if parsed.path == "/captura/kml":
                    job = CAPTURAS.get(sid)
                    if not job or not job.survey_id:
                        self._envia(404, b"captura sem survey",
                                    "text/plain; charset=utf-8"); return
                    sid = job.survey_id
                try:
                    dados, nome = kml_do_survey(sid, campo, cfg)
                except Exception as e:
                    self._envia(500, str(e).encode(),
                                "text/plain; charset=utf-8"); return
                self._envia(200, dados,
                            "application/vnd.google-earth.kmz",
                            {"Content-Disposition": f'attachment; filename="{nome}"'})
                return

            # ── Medições manuais ───────────────────────────────
            if parsed.path == "/medicoes/listar":
                sid = urllib.parse.parse_qs(parsed.query).get("survey_id", [""])[0]
                con = banco()
                try: ms = manual_listar(con, sid)
                finally: con.close()
                self._envia(200, json.dumps({"medicoes": ms}).encode(),
                            "application/json; charset=utf-8")
                return

            if parsed.path == "/medicoes/excluir":
                mid = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
                con = banco()
                try:
                    con.execute("DELETE FROM medicao_manual WHERE id=?", (mid,))
                    con.commit()
                finally: con.close()
                self._envia(200, b'{"ok":true}', "application/json")
                return

            if parsed.path == "/medicoes/modelo":
                tipo = urllib.parse.parse_qs(parsed.query).get("tipo",
                                                              ["iperf"])[0]
                if tipo not in ("iperf", "trace"): tipo = "iperf"
                csv_txt = template_csv_manual(tipo)
                self._envia(200, csv_txt.encode("utf-8-sig"),
                            "text/csv; charset=utf-8",
                            {"Content-Disposition":
                             f'attachment; filename="modelo_{tipo}.csv"'})
                return

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

            # ── Rádios e bandas disponíveis (para o formulário de survey) ──
            if parsed.path == "/radios":
                radios, bandas = set(), set()
                try:
                    prom = Prometheus(cfg.get("relatorio", "prometheus_url"))
                    for r in prom.instant("rajant_radio_canal"):
                        m = r.get("metric", {})
                        if m.get("radio"): radios.add(m["radio"])
                        if m.get("freq"):  bandas.add(m["freq"])
                except Exception as e:
                    log.debug(f"[web] /radios indisponivel: {e}")
                corpo = json.dumps({"radios": sorted(radios),
                                    "bandas": sorted(bandas)})
                self._envia(200, corpo.encode("utf-8"),
                            "application/json; charset=utf-8")
                return

            # ── Inicia uma captura ──
            if parsed.path == "/captura/iniciar":
                q = urllib.parse.parse_qs(parsed.query)
                col = COLETOR_ATUAL.get("ref")
                if not col:
                    self._envia(503, "Coletor nao esta ativo".encode(), "text/plain; charset=utf-8")
                    return
                ips = [i for i in (q.get("ips", [""])[0] or "").split(",") if i.strip()]
                if not ips:
                    self._envia(400, "Selecione ao menos um BreadCrumb".encode(),
                                "text/plain; charset=utf-8"); return
                try:
                    minutos   = float(q.get("minutos", ["10"])[0])
                    intervalo = int(float(q.get("intervalo", ["15"])[0]))
                except ValueError:
                    self._envia(400, "Parametros invalidos".encode(),
                                "text/plain; charset=utf-8"); return
                minutos = max(0.5, min(minutos, 240))
                import uuid
                ident = uuid.uuid4().hex
                try:    alcance = float(q.get("alcance", ["1000"])[0])
                except ValueError: alcance = 1000.0
                job = CapturaGPS(ident, ips, minutos, intervalo, col,
                                 rotulo=q.get("rotulo", [""])[0],
                                 alcance_m=max(200.0, min(alcance, 4000.0)),
                                 cfg=cfg)
                with _CAP_LOCK:
                    CAPTURAS[ident] = job
                    # limpa capturas antigas (mais de 6 h)
                    for k in [k for k, v in CAPTURAS.items()
                              if time.time() - v.inicio > 6*3600 and k != ident]:
                        CAPTURAS.pop(k, None)
                job.thread.start()
                log.info(f"[captura {ident[:8]}] {len(ips)} BCs | {minutos:g} min | "
                         f"intervalo {job.intervalo}s")
                # O aviso antigo falava do ciclo do exporter; agora a captura
                # consulta os radios direto, entao o que importa e a carga
                # que isso poe na malha — e ela e somada a do exporter, que
                # continua rodando.
                aviso = ""
                cps = len(ips) / float(job.intervalo)
                if job.continuo:
                    # No continuo o ciclo se alonga sozinho pelo teto de
                    # threads, entao "consultas/s" e um TETO, nao a taxa
                    # que vai acontecer. Dizer isso evita que o numero
                    # assuste sem motivo — e que passe despercebido quando
                    # a selecao e grande de verdade.
                    aviso = ((f"Modo continuo: mede sem parar, sem espera "
                              f"entre ciclos." if job.piso_cont <= 0 else
                              f"Modo continuo: piso de {job.piso_cont:g}s "
                              f"entre ciclos.")
                             + f" A cadencia passa a ser o tempo de resposta "
                               f"dos radios; com {len(ips)} selecionado(s) o "
                               f"teto e {cps:.0f} consultas/s. O intervalo "
                               f"REAL aparece no fim e vai para o relatorio.")
                    if len(ips) > 12:
                        aviso += (" Selecao grande para continuo: prefira so "
                                  "os veiculos do trajeto.")
                elif cps > 10:
                    aviso = (f"Carga alta na malha: {cps:.1f} consultas/s "
                             f"({len(ips)} radios a cada {job.intervalo}s). "
                             f"Aumente o intervalo ou reduza a selecao.")
                elif job.intervalo > intervalo:
                    aviso = (f"Intervalo elevado para {job.intervalo}s "
                             f"(minimo configurado em [survey] "
                             f"min_intervalo_s).")
                self._envia(200, json.dumps(
                    {"id": ident, "aviso": aviso,
                     "intervalo_s": job.intervalo,
                     "consultas_s": round(cps, 1)}).encode(),
                            "application/json; charset=utf-8")
                return

            if parsed.path == "/captura/status":
                ident = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
                job = CAPTURAS.get(ident)
                if not job:
                    self._envia(404, b"{}", "application/json"); return
                self._envia(200, json.dumps(job.status()).encode(),
                            "application/json; charset=utf-8")
                return

            if parsed.path == "/captura/parar":
                ident = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
                job = CAPTURAS.get(ident)
                if job:
                    job.fim_previsto = time.time()   # encerra e parte p/ geracao
                    job._parar.set()
                self._envia(200, b'{"ok":true}', "application/json")
                return

            if parsed.path == "/captura/kmz":
                ident = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
                job = CAPTURAS.get(ident)
                if not job or not job.kmz:
                    self._envia(404, "KMZ ainda nao esta pronto".encode(),
                                "text/plain; charset=utf-8"); return
                self._envia(200, job.kmz,
                            "application/vnd.google-earth.kmz",
                            {"Content-Disposition": f'attachment; filename="{job.nome_kmz}"'})
                return

            if parsed.path == "/diagnostico":
                q = urllib.parse.parse_qs(parsed.query)
                ip = (q.get("ip") or [""])[0].strip()
                if not ip:
                    self._envia(400, b"Informe ?ip=10.x.x.x", "text/plain; charset=utf-8"); return
                try:
                    sc = cfg["servidor"] if cfg.has_section("servidor") else {}
                    porta_api = cfg.getint("rede","porta_bcapi", fallback=2300)
                    role  = cfg.get("rede","role", fallback="VIEW")
                    senha = cfg.get("rede","password", fallback="")
                    bc = exigir_rajant_api()(host=ip, port=porta_api, role=role, password=senha)
                    if not bc.reachable():    raise RuntimeError("BC nao alcancavel")
                    if not bc.authenticate(): raise RuntimeError("Autenticacao falhou")
                    raw = bc.get_state()
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

                    # ── Recorte do survey, independente do relatório ──
                    # Dois caminhos: um survey JÁ GRAVADO (botão "Usar no
                    # relatório" do histórico, que manda survey_id) ou um
                    # recorte do Prometheus por período (sv=1 + datas).
                    # O survey_id vinha sendo IGNORADO aqui: a página
                    # mandava, o servidor não lia, e o PPT saía sem os
                    # slides de survey sem dizer por quê.
                    survey = None
                    sv_id = (q.get("survey_id", [""])[0] or "").strip()
                    if sv_id:
                        survey = {"survey_id": sv_id}
                        bandas = [b for b in q.get("svbanda", [""])[0].split(",")
                                  if b.strip()]
                        if bandas: survey["bandas"] = bandas
                        log.info(f"[web] survey gravado #{sv_id}")
                    elif q.get("sv", ["0"])[0] == "1":
                        def _dt(txt):
                            for f in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M",
                                      "%Y-%m-%d"):
                                try: return datetime.strptime(txt, f)
                                except ValueError: pass
                            raise ValueError(f"data/hora invalida no survey: {txt}")
                        sv_i = _dt(q.get("svini", [""])[0])
                        sv_f = _dt(q.get("svfim", [""])[0])
                        if sv_f <= sv_i:
                            raise ValueError("o fim do survey precisa ser "
                                             "depois do inicio")
                        bandas = [b for b in q.get("svbanda", [""])[0].split(",")
                                  if b.strip()]
                        survey = {
                            "ini": sv_i, "fim": sv_f,
                            "radio":  q.get("svradio", [""])[0] or "",
                            "bandas": bandas or None,
                            "passo":  int(float(q.get("svpasso", ["60"])[0])),
                            "vel_min": float(q.get("svvel", ["1"])[0]),
                            "grade_m": float(q.get("svgrade", ["0"])[0]),
                            "kmz": (cfg.get("relatorio", "survey_kmz",
                                            fallback="") or None),
                        }
                        log.info(f"[web] survey {sv_i:%d/%m %H:%M}"
                                 f"..{sv_f:%d/%m %H:%M} "
                                 f"radio={survey['radio'] or 'todos'}")

                    log.info(f"[web] Relatório {fmt} {ini}..{fim} demo={demo}"
                             f"{' +survey' if survey else ''}")
                    if fmt == "pptx":
                        dados, nome = gerar_ppt(cfg, ini, fim, demo=demo,
                                                survey=survey)
                        ctype = ("application/vnd.openxmlformats-officedocument"
                                 ".presentationml.presentation")
                    else:
                        dados, nome = montar_relatorio(cfg, ini, fim, demo=demo)
                        ctype = ("application/vnd.openxmlformats-officedocument"
                                 ".spreadsheetml.sheet")
                    cab = {"Content-Disposition": f'attachment; filename="{nome}"'}
                    # O navegador baixa o arquivo e nao mostra corpo nenhum;
                    # o aviso vai por cabecalho para a pagina poder exibir
                    # que o survey foi pedido e nao entrou.
                    av = getattr(gerar_ppt, "ultimo_aviso", "")
                    if fmt == "pptx" and av:
                        cab["X-Survey-Aviso"] = av[:200]
                        cab["Access-Control-Expose-Headers"] = "X-Survey-Aviso"
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
    ap = argparse.ArgumentParser(description="Rajant Monitor — exporter Prometheus + relatorios Excel")
    ap.add_argument("--seed"); ap.add_argument("--password")
    ap.add_argument("--role", default=None, choices=["VIEW","admin","co"])
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
    ap.add_argument("--montar-template-survey", action="store_true",
                    help="Adiciona os slides-modelo de Site Survey ao template PPT "
                         "e salva como *_com_Survey.pptx (executar uma unica vez).")
    ap.add_argument("--bandas", default="2.4 GHz,5.8 GHz",
                    help="Bandas do survey, separadas por virgula (padrao: '2.4 GHz,5.8 GHz'). "
                         "Use '' para um bloco unico sem segmentacao.")
    ap.add_argument("--survey-do-prometheus", action="store_true",
                    help="monta o CSV de survey a partir das metricas ja "
                         "coletadas (a frota movel como enxame de sondas), "
                         "em vez de exigir campanha manual")
    ap.add_argument("--survey-periodo", nargs=2, metavar=("INI","FIM"),
                    help="periodo do survey continuo: AAAA-MM-DD ou "
                         "'AAAA-MM-DD HH:MM' (permite recortar por turno)")
    ap.add_argument("--survey-radio", metavar="NOME",
                    help="analisa so este radio (ex.: wlan0). Sem a flag, "
                         "agrega todos os radios de cada banda")
    ap.add_argument("--survey-passo", type=int, default=60,
                    help="passo da consulta ao Prometheus em segundos "
                         "(padrao 60; use o mesmo do intervalo de coleta)")
    ap.add_argument("--survey-vel-min", type=float, default=1.0,
                    help="descarta amostras abaixo desta velocidade em km/h "
                         "(padrao 1.0) — equipamento parado enviesa o mapa")
    ap.add_argument("--survey-grade-m", type=float, default=0.0,
                    help="agrega as amostras em celulas de N metros "
                         "(padrao 0 = desligado)")
    ap.add_argument("--survey-csv", metavar="ARQ.CSV",
                    help="Gera heatmaps/rota/histogramas a partir de um CSV georreferenciado "
                         "e insere nos slides de survey do template.")
    ap.add_argument("--survey-fundo", metavar="IMG",
                    help="Imagem de fundo (satelite da area) para os heatmaps.")
    ap.add_argument("--survey-kmz", metavar="ARQ.KMZ",
                    help="KMZ/KML do Google Earth: usa o GroundOverlay como fundo "
                         "GEORREFERENCIADO e plota os Placemarks (repetidoras).")
    ap.add_argument("--kmz-fundo", choices=["auto","vetorial","nenhum"], default="auto",
                    help="auto/vetorial: sem imagem no KMZ, desenha o contorno da "
                         "mina como base. nenhum: fundo limpo.")
    ap.add_argument("--fundo-bbox", metavar="N,S,L,O",
                    help="Limites geograficos da imagem de --survey-fundo "
                         "(ex.: -18.8810,-18.9502,-43.4012,-43.4396). Sem isso, "
                         "usa o BBOX do KMZ.")
    ap.add_argument("--kml-do-survey", metavar="ID",
                    help="Gera o KMZ de um survey do historico para abrir no "
                         "Google Earth (pontos de medicao, rotas e BCs).")
    ap.add_argument("--ppt-survey", metavar="ID",
                    help="Gera o deck EXCLUSIVO de site survey, na identidade "
                         "Anglo, separado do relatorio semanal.")
    ap.add_argument("--kmz-todos", metavar="ID",
                    help="Gera UM KMZ por grandeza e por banda, num ZIP com "
                         "LEIA-ME dizendo qual arquivo vai em qual slide.")
    ap.add_argument("--kml-campo", default="sinal",
                    choices=["sinal", "snr", "rtt", "perda"],
                    help="Grandeza que colore os pontos do KMZ (padrao: sinal).")
    ap.add_argument("--testar-fundo", action="store_true",
                    help="Diz qual fundo os mapas do survey vao usar e por que, "
                         "sem gerar relatorio. Use antes de rodar o relatorio "
                         "de verdade para nao descobrir o problema no PPT.")
    ap.add_argument("--cobertura-expoente", type=float, default=2.3,
                    help="Expoente de propagacao do mapa de cobertura (2.0 espaco "
                         "livre, 2.3 cava aberta, 3.0+ obstruido).")
    ap.add_argument("--cobertura-ptx", type=float, default=25.0,
                    help="Potencia de transmissao (dBm) do modelo de cobertura.")
    ap.add_argument("--cobertura-ganho", type=float, default=8.0,
                    help="Ganho de antena (dBi) por ponta no modelo de cobertura.")
    ap.add_argument("--raio-interp", type=float, default=300.0,
                    help="Raio de influencia (m) da interpolacao dos pontos medidos.")
    ap.add_argument("--kmz-pontos", action="store_true",
                    help="Plota tambem os Placemarks do KMZ (por padrao sao "
                         "ignorados: em KMZ de planejamento nao sao repetidoras).")
    ap.add_argument("--gps-do-prometheus", action="store_true",
                    help="Plota tambem os BreadCrumbs com GPS lidos do Prometheus.")
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
    manter_s  = cfg.getint("coleta","manter_ultimo_valor_s",fallback=300)
    redesc    = cfg.getint("coleta","redescoberta_ciclos",  fallback=10)
    falhas_rem= cfg.getint("coleta","falhas_para_remover",  fallback=20)
    # Coleta acelerada dos móveis (site survey contínuo). O padrão de nome
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

    # ── Deck exclusivo de site survey ─────────────────────────────────
    if args.ppt_survey:
        cfg_relatorio(cfg)
        try:
            dados, nome = ppt_survey_anglo(args.ppt_survey, cfg)
        except Exception as e:
            log.error(f"Falha ao gerar o deck de survey: {e}"); sys.exit(1)
        Path(nome).write_bytes(dados)
        print(f"\n  {nome}  ({len(dados)/1024:.0f} KB)\n")
        sys.exit(0)

    # ── Todos os KMZ, para montar os slides a mao ─────────────────────
    if args.kmz_todos:
        cfg_relatorio(cfg)
        try:
            dados, nome = gerar_todos_kmz(args.kmz_todos, cfg)
        except Exception as e:
            log.error(f"Falha ao gerar os KMZ: {e}"); sys.exit(1)
        Path(nome).write_bytes(dados)
        print(f"\n  {nome}  ({len(dados)/1024:.0f} KB)")
        print("  Descompacte e abra cada .kmz no Google Earth.")
        print("  O LEIA-ME.txt diz qual arquivo vai em qual slide.\n")
        sys.exit(0)

    # ── KMZ do survey para o Google Earth ─────────────────────────────
    if args.kml_do_survey:
        cfg_relatorio(cfg)
        try:
            dados, nome = kml_do_survey(args.kml_do_survey, args.kml_campo, cfg)
        except Exception as e:
            log.error(f"Falha ao gerar o KMZ: {e}"); sys.exit(1)
        Path(nome).write_bytes(dados)
        print(f"\n  {nome}  ({len(dados)/1024:.0f} KB)")
        print("  Abra com duplo clique — o Google Earth ja traz o satelite.")
        print("  Camadas: BreadCrumbs · Rotas · Medicoes · Fora do requisito\n")
        sys.exit(0)

    # ── Diagnostico do fundo dos mapas ────────────────────────────────
    # Descobrir que o fundo nao funciona olhando o PPT pronto custa uma
    # geracao inteira de relatorio. Aqui responde em segundos.
    if args.testar_fundo:
        cfg_relatorio(cfg)
        bbox = _bbox_cfg(cfg) or {"norte": -27.72, "sul": -27.74,
                                  "leste": -50.05, "oeste": -50.08}
        caminho, ext, credito, aviso = resolver_fundo(
            bbox, cfg, kmz=args.survey_kmz)
        print()
        if caminho:
            print(f"  FUNDO OK: {credito}")
            print(f"  arquivo:  {caminho}")
            print(f"  cobre:    N {ext['norte']}  S {ext['sul']}  "
                  f"L {ext['leste']}  O {ext['oeste']}")
            if aviso:
                print(f"  ressalva: {aviso}")
        else:
            print("  SEM FUNDO — os mapas sairao so com eixos lat/lon.")
            print(f"  motivo:   {aviso}")
            print()
            print("  Tres caminhos, do mais facil para o mais trabalhoso:")
            print("  1. liberar services.arcgisonline.com no firewall da mina")
            print("     (teste: curl -sI https://services.arcgisonline.com)")
            print("  2. [relatorio] survey_kmz = /caminho/levantamento.kmz")
            print("     — precisa ter GroundOverlay, nao so Placemarks")
            print("  3. [relatorio] fundo_local = orto.png")
            print("     [relatorio] fundo_bbox  = N,S,L,O  (obrigatorio)")
        print()
        sys.exit(0 if caminho else 1)

    # ── Geracao pontual de relatorio por linha de comando (nao sobe servico) ──
    # Survey contínuo: gera o CSV a partir do Prometheus e segue pelo mesmo
    # caminho do CSV manual — todo o pipeline de imagens/KMZ é reaproveitado.
    if args.survey_do_prometheus:
        if not args.survey_periodo:
            log.error("--survey-do-prometheus exige --survey-periodo INI FIM "
                      "(AAAA-MM-DD ou 'AAAA-MM-DD HH:MM')"); sys.exit(1)

        def _instante(txt, fim=False):
            """Aceita data ou data+hora. Análise por turno (06:00-14:00) é o
            recorte natural numa mina, e só data não permitia isso."""
            txt = txt.strip()
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
                try: return datetime.strptime(txt, fmt), True
                except ValueError: pass
            try:
                d = datetime.strptime(txt, "%Y-%m-%d")
            except ValueError:
                return None, False
            # só data: o fim do período vai até o último segundo do dia
            return (d.replace(hour=23, minute=59, second=59) if fim else d), True

        _ini, ok1 = _instante(args.survey_periodo[0])
        _fim, ok2 = _instante(args.survey_periodo[1], fim=True)
        if not (ok1 and ok2):
            log.error("periodo invalido; use AAAA-MM-DD ou "
                      "'AAAA-MM-DD HH:MM'"); sys.exit(1)
        if _fim <= _ini:
            log.error("o fim do periodo precisa ser depois do inicio")
            sys.exit(1)
        log.info(f"[survey] periodo {_ini:%d/%m/%Y %H:%M} -> "
                 f"{_fim:%d/%m/%Y %H:%M}"
                 + (f" | radio {args.survey_radio}" if args.survey_radio else ""))
        cfg_relatorio(cfg)
        try:
            _prom = Prometheus(cfg.get("relatorio", "prometheus_url"))
            _info = gerar_csv_survey_prometheus(
                _prom, _ini.timestamp(), _fim.timestamp(), args.survey_passo,
                "survey_imgs/survey_prometheus.csv",
                vel_min=args.survey_vel_min, grade_m=args.survey_grade_m,
                radio=args.survey_radio or "")
        except Exception as e:
            log.error(f"Falha ao montar o survey do Prometheus: {e}")
            sys.exit(1)
        args.survey_csv = _info["caminho"]
        if not args.bandas or args.bandas == "2.4 GHz,5.8 GHz":
            # usa as bandas que realmente aparecem nos dados
            args.bandas = ",".join(b for b in _info["bandas"] if b) or None

    if args.montar_template_survey or args.survey_csv:
        from pptx import Presentation
        template = cfg.get("relatorio", "template_ppt",
                           fallback="Relatorio_Semanal_Rede.pptx")
        if not Path(template).exists():
            log.error(f"Template nao encontrado: {template}"); sys.exit(1)
        bandas = [b.strip() for b in (args.bandas or "").split(",") if b.strip()] or [None]
        p = Presentation(template)

        if args.montar_template_survey:
            total = 0
            for b in bandas:
                total += construir_slides_survey(p, banda=b)
            log.info(f"{total} slides de survey adicionados "
                     f"({len(bandas)} banda(s): {', '.join(str(b) for b in bandas)})")

        if args.survey_csv:
            if not Path(args.survey_csv).exists():
                log.error(f"CSV nao encontrado: {args.survey_csv}"); sys.exit(1)
            fbbox = None
            if args.fundo_bbox:
                try:
                    n_, s_, l_, o_ = (float(v) for v in args.fundo_bbox.split(","))
                    fbbox = {"norte": n_, "sul": s_, "leste": l_, "oeste": o_}
                except Exception:
                    log.error("--fundo-bbox invalido; use N,S,L,O"); sys.exit(1)
            bcs_gps = []
            if args.gps_do_prometheus:
                try:
                    prom = Prometheus(cfg.get("relatorio","prometheus_url"))
                    lats = {r["metric"].get("bc","?"): float(r["value"][1])
                            for r in prom.instant("rajant_gps_lat")}
                    lons = {r["metric"].get("bc","?"): float(r["value"][1])
                            for r in prom.instant("rajant_gps_lon")}
                    bcs_gps = [(n, lats[n], lons[n]) for n in lats if n in lons]
                    log.info(f"  {len(bcs_gps)} BreadCrumbs com GPS no Prometheus")
                except Exception as e:
                    log.warning(f"  GPS do Prometheus indisponivel: {e}")
            for b in bandas:
                try:
                    imgs = gerar_imagens_survey(args.survey_csv, "survey_imgs",
                                                banda=b, fundo=args.survey_fundo,
                                                kmz=args.survey_kmz, bcs_gps=bcs_gps,
                                                kmz_fundo=args.kmz_fundo,
                                                kmz_pontos=args.kmz_pontos,
                                                fundo_bbox=fbbox, raio_m=args.raio_interp)
                    # mapa de cobertura de toda a mina (posicoes dos BCs)
                    if args.survey_kmz:
                        try:
                            kd = ler_kmz(args.survey_kmz, "survey_imgs/kmz")
                            arq_cob, pct_cob = mapa_cobertura(
                                kd, "survey_imgs", banda=b, fundo=args.survey_fundo,
                                fundo_bbox=fbbox, expoente=args.cobertura_expoente,
                                ptx_dbm=args.cobertura_ptx, ganho_dbi=args.cobertura_ganho)
                            imgs["cobertura"] = arq_cob
                            log.info(f"  Cobertura {b}: {pct_cob:.0f}% da area >= -75 dBm")
                        except Exception as e:
                            log.warning(f"  cobertura {b} nao gerada: {e}")
                    n = inserir_imagens_survey(p, imgs, banda=b)
                    g = atualizar_graficos_survey(p, args.survey_csv, banda=b)
                    log.info(f"  Banda {b or 'unica'}: {imgs['_pontos']} pontos | "
                             f"metricas {imgs['_metricas']} | {n} imagens | "
                             f"{g} graficos atualizados")
                except Exception as e:
                    log.error(f"  Banda {b or 'unica'}: falha ao gerar imagens: {e}")

        saida = Path(template).stem + ("_com_Survey.pptx" if args.montar_template_survey
                                       else "_com_Heatmaps.pptx")
        p.save(saida)
        log.info(f"Arquivo gerado: {saida}")
        return

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
        if not bc.authenticate(): log.error("Autenticacao falhou"); sys.exit(3)
        raw = bc.get_state()
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
        if not bc.authenticate(): log.error("Autenticacao falhou"); sys.exit(3)
        raw = bc.get_state()
        txt = json.dumps(raw, indent=2) if isinstance(raw, dict) else str(raw)
        arq = f"state_{ipd.replace('.','_')}.txt"
        Path(arq).write_text(txt, encoding="utf-8")
        dados = parse_state(raw)
        log.info(f"State salvo em {arq}")
        log.info(f"Parse: {len(dados['radios'])} radios | "
                 f"{len(dados['ethernet'])} portas eth "
                 f"({', '.join(e['nome'] for e in dados['ethernet']) or 'NENHUMA'})")
        return

    if not seeds:    log.error("Nenhum seed configurado (secao [rede], chave seeds)."); sys.exit(1)
    if not password: log.error("Senha nao configurada (secao [rede], chave password)."); sys.exit(1)

    descoberta_on = cfg.getboolean("coleta", "descoberta", fallback=True)
    cache_on      = cfg.getboolean("coleta", "usar_cache",  fallback=True)
    so_com_tag    = cfg.getboolean("coleta", "somente_com_tag", fallback=False)
    prefs = None
    if so_com_tag:
        cfg_relatorio(cfg)
        prefs = [pref for _, pref in prefixos_frota(cfg)]
        if not prefs:
            log.warning("[coleta] somente_com_tag ligado mas "
                        "[relatorio] prefixos_frota esta vazio; "
                        "filtro DESLIGADO para nao descartar tudo")
            prefs = None

    cache = CacheIPs(CACHE_FILE, ativo=cache_on)
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
    log.info(f"  Seeds:     {len(seeds)} IP(s)")
    log.info(f"  Descoberta:{'ligada' if descoberta_on else 'DESLIGADA (so os seeds)'}")
    log.info(f"  Cache:     "
             + (f"{CACHE_FILE} ({cache.total()} IPs)" if cache_on
                else "desligado"))
    if prefs:
        log.info(f"  Filtro:    so nomes com prefixo {', '.join(prefs)}")
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
        falhas_remover=falhas_rem, cache=cache,
        interval_moveis=int_moveis, padrao_movel=padr_movel,
        descoberta=descoberta_on, prefixos_tag=prefs,
    )
    COLETOR_ATUAL["ref"] = coletor      # habilita a captura pela pagina web
    coletor.run()

if __name__ == "__main__":
    main()
