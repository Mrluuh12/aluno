"""
Motor do site survey: leitura dos rádios e do MeshMapper, análise, KMZ,
PPT e Excel. Usado por site_survey.py (a janela), coleta_rajant.py (a
coleta ao vivo) e survey_meshmapper.py (o relatório a partir do arquivo).

Projeto separado do rajant_monitor (exportador + Grafana + semanal).
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

import re, json, time, logging, threading, configparser, sys, math
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


# ══════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════
CONFIG_FILE = "config.ini"

DEFAULT_CONFIG = """\
[rede]
# Acesso aos rádios para a coleta ao vivo (o relatório a partir do arquivo
# do MeshMapper não usa rede).
seeds                   = 10.188.96.140
password                = anglomesh-view
role                    = VIEW
porta_bcapi             = 2300
"""

def carregar_config():
    cfg = configparser.ConfigParser()
    if not Path(CONFIG_FILE).exists():
        Path(CONFIG_FILE).write_text(DEFAULT_CONFIG, encoding="utf-8")
    cfg.read(CONFIG_FILE, encoding="utf-8")
    return cfg


log = logging.getLogger("rajant")


# REMOVIDA: rajant_session_state — sessionState/connectionState não existem
# em nenhum .proto. O único estado de sessão do protocolo é State.AdminSession
# (sessões administrativas conectadas AO BreadCrumb), que não é o que a série
# media. "Sessão OK" do ponto de vista do coletor já é rajant_online.


# REMOVIDAS: rajant_eth_rx_erros, _tx_erros, _rx_crc, _rx_drop, _tx_drop,
# _mudancas. State.Wired carrega stats do tipo CommStats, que tem SÓ quatro
# contadores: rxBytes, rxPackets, txBytes, txPackets. Não há erro, CRC,
# descarte nem contagem de mudanças de link em lugar algum do protocolo.
# Publicá-las como 0 dizia "porta perfeita" quando o dado nunca foi medido.


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
# CONFIGURAÇÃO DOS RELATÓRIOS DO SURVEY
# ══════════════════════════════════════════════════════════════


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
# ESTILOS DO EXCEL
# ──────────────────────────────────────────────────────────────
F_TIT  = Font(name="Arial", size=15, bold=True, color="FFFFFF")
F_SUB  = Font(name="Arial", size=9,  color="D9E2F3")
F_H    = Font(name="Arial", size=9,  bold=True, color="FFFFFF")
F_TXT  = Font(name="Arial", size=9)
F_TXTB = Font(name="Arial", size=9,  bold=True)
FILL_TIT = PatternFill("solid", fgColor="1F4E79")
FILL_H   = PatternFill("solid", fgColor="2E75B6")
FILL_ZEB = PatternFill("solid", fgColor="F2F2F2")
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


def _faixa_idx(v, faixas, na_de_baixo=()):
    """Índice da faixa de `v`. faixas: [(limite, 'RRGGBB')], limite crescente.

    Por padrão o valor EXATO de um limite vai para a faixa de cima
    ([a, b)). Os limites em `na_de_baixo` ficam com a faixa de baixo —
    ver `limites_na_faixa_de_baixo`.
    """
    for k, (lim, _cor) in enumerate(faixas):
        if v < lim or (v == lim and lim in na_de_baixo):
            return k
    return len(faixas) - 1


def _bucket_cor(v, faixas, na_de_baixo=()):
    """faixas: [(limite, 'RRGGBB')] em ordem crescente de limite."""
    if v is None: return COR_SEM_VALOR
    return faixas[_faixa_idx(v, faixas, na_de_baixo)][1]


def atende(v, op, lim):
    """A leitura cumpre o requisito? Um lugar só para os quatro operadores:
    o Sumário, a pasta Fora do requisito e a cor do mapa decidem aqui."""
    return {">": v > lim, ">=": v >= lim,
            "<": v < lim, "<=": v <= lim}[op]


def limites_na_faixa_de_baixo(campo):
    """Limites cujo valor EXATO pertence à faixa de baixo, nesta grandeza.

    A convenção de origem define a maioria (`_CONVENCAO_NA_FAIXA_DE_BAIXO`);
    o limite do REQUISITO é decidido pelo operador dele. O rádio reporta
    inteiro, −75 dBm exato é comum, e o requisito é RSSI > −75: −75 é
    reprovado. Com a faixa sempre [a, b), ele caía no laranja de "atende",
    enquanto o Sumário e a pasta Fora do requisito o contavam como
    reprovado — o mapa dizia uma coisa e o número outra.

    Tirado do operador e não escrito à mão: se o requisito mudar para
    `>=`, o −75 muda de faixa sozinho, e cor e contagem seguem juntas.
    """
    out = set(_CONVENCAO_NA_FAIXA_DE_BAIXO.get(campo, ()))
    req = limite_de(campo)
    if req:
        op, lim = req[0], float(req[1])
        # O valor exato do limite vai para o lado que o operador decide:
        # reprovado em ">" e "<"; aprovado em ">=" e "<=". Em grandeza em
        # que alto é melhor, o lado reprovado é a faixa de baixo; em que
        # baixo é melhor, a de cima.
        if op in (">", "<="):
            out.add(lim)
        else:
            out.discard(lim)
    return frozenset(out)


def cor_da_leitura(v, campo):
    """A cor da leitura na legenda da grandeza — a régua única do laudo."""
    return _bucket_cor(v, FAIXAS_KML[campo], limites_na_faixa_de_baixo(campo))


_OP_TXT = {">": ">", ">=": "≥", "<": "<", "<=": "≤"}


def nome_da_aba(campo):
    """O nome da pasta da grandeza no KMZ — e o que a moldura do slide
    manda ligar. Uma função só para os dois: a moldura cortava o título do
    slide ("Intensidade de Sinal", "Noise Floor") e mandava procurar no
    Google Earth abas que se chamam "RSSI (melhor ERB/ERM do ponto)" e
    "Ruído"."""
    lim = limite_de(campo)
    return lim[3] if lim else campo.upper()


# A pasta de dentro da aba que traz o rastro colorido — o que o print mostra.
CAMADA_DO_RASTRO = "Trajeto"
CAMADA_DO_CALOR = "Mapa de calor"


def sem_rota(v, campo):
    """Custo do caminho = 2147483647 é o trace SEM rota até o destino.

    É medição — a pior possível —, não ausência dela: fica no vermelho
    da régua, como no MeshMapper. Mas escrever o número é ilegível e,
    numa média, destrói o valor típico. Por isso se escreve "sem rota".
    """
    return (campo == "custo_caminho" and v is not None
            and v >= CUSTO_SEM_ROTA)


def fmt_leitura(v, campo, com_unidade=True):
    """O valor como o laudo o escreve. None → "—"; sem rota → "sem rota".

    `com_unidade=False` devolve o NÚMERO (para a célula do Excel continuar
    numérica), trocando só o sem-rota por texto.
    """
    if v is None:
        return "—" if com_unidade else None
    if sem_rota(v, campo):
        return "sem rota"
    if not com_unidade:
        return v
    un = (ESCALAS.get(campo) or {}).get("un", "")
    return f"{v:g} {un}".rstrip()

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

# Faixas de cor por grandeza: (limite, RRGGBB), limite crescente. De que
# lado de cada limite fica o valor EXATO está em `limites_na_faixa_de_baixo`.
#
# De onde vem cada régua — nada aqui é inventado, e a legenda de cada mapa
# imprime a fonte (FONTE_FAIXAS):
#
#   CORES. As chapadas oficiais do Rajant MeshMapper, lidas do doc.kml
#   que o BC|Commander 11.29.1 gerou na mina: ff0000ff / ff006af2 /
#   ff30ad3e (aabbggrr) = FF0000 poor, F26A00 good, 3EAD30 great. Amarelo
#   e vermelho-escuro não existem chapados no MeshMapper; vêm do tom
#   dominante dos alfinetes dele (yellow_pin DD9F17, red_pin B70404). O
#   5C0000 da faixa inutilizável é o único tom que não é da Rajant: o
#   vermelho dela escurecido, para a pior faixa não ser confundida com a
#   penúltima.
#
#   RSSI (dBm). O MeshMapper NÃO classifica o Signal — no balão ele sai
#   sem cor, conferido em 11.216 células. A régua é a convenção de Wi-Fi
#   mais citada (MetaGeek/Oscium, "Understanding RSSI"): -67 muito bom,
#   mínimo para voz e vídeo — a mesma borda de célula de voz do guia de
#   site survey da Cisco —; -70 ok, mínimo para entrega confiável; -80
#   ruim, mínimo para conectividade básica; -90 inutilizável. Mais o -75
#   do requisito Modular, para aprovado e reprovado não dividirem cor.
#
#   SNR (dB). A régua OFICIAL da Rajant, gravada pelo próprio MeshMapper
#   no data.json (goodRSSI = 20, greatRSSI = 30 — no vocabulário Rajant
#   "RSSI" é o SNR em dB) e conferida célula a célula: 19 vermelho, 20
#   laranja, 29 laranja, 30 verde.
#
#   RUÍDO (dBm). Sem régua oficial em lugar nenhum. É DERIVADO das duas
#   oficiais: um sinal no mínimo Modular (-75) sobre ruído N chega com
#   SNR = -75 - N, classificado pela régua Rajant. N <= -105 dá SNR >= 30;
#   N >= -95 dá SNR <= 20. O corte de -85 que havia aqui era incoerente
#   com os próprios requisitos: -75 sobre -85 é SNR 10, reprovado.
FAIXAS_KML = {
    "sinal": [(-90, "5C0000"), (-80, "B70404"), (-75, "FF0000"),
              (-70, "F26A00"), (-67, "DD9F17"), (999, "3EAD30")],
    # Cobertura usa as MESMAS faixas do RSSI: e a mesma grandeza, lida de
    # outra fonte. Faixas proprias fariam duas reguas para o mesmo dBm.
    "sinal_cob": [(-90, "5C0000"), (-80, "B70404"), (-75, "FF0000"),
                  (-70, "F26A00"), (-67, "DD9F17"), (999, "3EAD30")],
    "snr":   [(20, "FF0000"), (30, "F26A00"), (999, "3EAD30")],
    "rtt":   [(20, "27AE60"), (50, "9ACD32"), (100, "F1C40F"),
              (200, "E67E22"), (400, "E74C3C"), (99999, "C0392B")],
    "perda": [(0.5, "27AE60"), (1, "9ACD32"), (2, "F1C40F"),
              (5, "E67E22"), (10, "E74C3C"), (101, "C0392B")],
    "interf": [(5, "27AE60"), (10, "9ACD32"), (20, "F1C40F"),
               (35, "E67E22"), (50, "E74C3C"), (101, "C0392B")],
    # Piso de ruído: quanto mais negativo, mais limpo. Derivado — ver acima.
    "ruido": [(-105, "3EAD30"), (-95, "F26A00"), (999, "FF0000")],
    # Custo do caminho até o destino do trace (Trace Path Cost). Régua
    # OFICIAL da Rajant, do mesmo data.json: greatPath = 10000, goodPath =
    # 20000 — e é por ela que o MeshMapper pinta a linha do trajeto (as
    # "Average Cost" do doc.kml: laranja até 19200, vermelho a partir de
    # 20308). Sem rota (2147483647) cai no vermelho, como no MeshMapper.
    "custo_caminho": [(10000, "3EAD30"), (20000, "F26A00"),
                      (2147483647, "FF0000")],
    # Enlaces bons no ponto (custo e SNR na régua da Rajant): o número e a
    # cor do pino do MeshMapper — 0 vermelho, 1 laranja, 2 amarelo, 3+
    # verde. Cores medidas nos pinos do KMZ oficial de 28/09/2026.
    "enl_bons": [(0, "D80E0E"), (1, "DD581D"), (2, "DD9F17"),
                 (2147483647, "1D991D")],
}

# Limites cujo valor EXATO pertence à faixa de BAIXO pela convenção de
# origem, sem contar o requisito (que `limites_na_faixa_de_baixo` acerta
# pelo operador). O rádio reporta dBm e dB INTEIROS, então o valor exato
# de cada limite é comum — e cada um tem de cair onde a fonte diz:
#   -90 dBm   "unusable" na MetaGeek: o próprio -90 já é inutilizável.
#   -105 dBm  derivado de SNR >= 30 (Rajant, inclusivo): -75 - (-105) = 30.
# Os demais limites de convenção são mínimos atingidos na igualdade
# (-67 é muito bom, -70 é ok, -80 é conectividade básica, SNR 30 é great)
# e ficam na faixa de cima, que é o comportamento padrão.
_CONVENCAO_NA_FAIXA_DE_BAIXO = {
    "sinal": {-90.0}, "sinal_cob": {-90.0}, "ruido": {-105.0},
    # Custo: "great" ATÉ 10000. Por analogia com o SNR, cuja fronteira foi
    # conferida célula a célula (20 é good, 30 é great): o limite pertence
    # à classe melhor. Custo exatamente 10000 não aparece nos arquivos,
    # então esta fronteira não pôde ser conferida na prática.
    "custo_caminho": {10000.0},
    # Contagem: cada faixa é um número só (0, 1, 2), e 3+ é o resto.
    "enl_bons": {0.0, 1.0, 2.0},
}

# Classificação de cada faixa, na ordem de FAIXAS_KML (limite crescente).
CLASSES_FAIXA = {
    "sinal":     ["inutilizável", "abaixo da conectividade básica",
                  "reprovado", "atende o requisito", "ok", "muito bom"],
    "snr":       ["ruim", "bom", "ótimo"],
    "ruido":     ["ótimo", "bom", "reprova"],
    "custo_caminho": ["ótimo", "bom", "ruim ou sem rota"],
    "enl_bons": ["sem enlace bom", "sem redundância", "redundância dupla",
                 "redundância tripla ou mais"],
}
CLASSES_FAIXA["sinal_cob"] = CLASSES_FAIXA["sinal"]

# Fonte de cada régua, impressa no rodapé da legenda do mapa e no slide
# de Metodologia: a cor só é conferível se a régua for rastreável.
FONTE_FAIXAS = {
    "sinal": "Faixas: MetaGeek/Oscium (-90, -80, -70, -67) e requisito "
             "Modular (-75). Cores: Rajant MeshMapper.",
    "snr":   "Faixas e cores: Rajant MeshMapper (goodRSSI 20, greatRSSI 30).",
    "ruido": "Derivado: sinal de -75 dBm (Modular) sobre este ruído, "
             "SNR pela régua Rajant.",
}
FONTE_FAIXAS["custo_caminho"] = ("Faixas e cores: Rajant MeshMapper (greatPath "
                                  "10000, goodPath 20000). Sem rota conta como "
                                  "ruim, como no MeshMapper.")
FONTE_FAIXAS["sinal_cob"] = FONTE_FAIXAS["sinal"]
FONTE_FAIXAS["enl_bons"] = ("Regra e cores: Rajant MeshMapper. Enlace bom: "
                            "custo ≤ 10000 e SNR ≥ 20; ótimo: custo ≤ 5000 e "
                            "SNR ≥ 30 (goodCost, goodRSSI, greatCost, "
                            "greatRSSI).")

# Grandezas que o rádio reporta em INTEIRO (int32 no State.proto e no
# arquivo do MeshMapper). Nelas a legenda escreve o intervalo exato em
# inteiros — "-74 a -71" —, sem ambiguidade de fronteira.
_CAMPOS_INTEIROS = {"sinal", "sinal_cob", "snr", "ruido", "custo_caminho",
                    "enl_bons"}

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
    # Primeiro o que PINTA o ponto — o RSSI da melhor ERB/ERM — e depois
    # o enlace que atendeu. Antes o balão mostrava só o segundo, rotulado
    # "RSSI": um ponto laranja a -72 abria com "RSSI -89 (fora)".
    campos = (("enl_bons", "Enlaces bons (Rajant)", "", 0),
              ("enl_otimos", "Enlaces ótimos", "", 0),
              ("sinal_cob", "RSSI (melhor ERB/ERM)", "dBm", 1),
              ("sinal", "RSSI do enlace", "dBm", 1), ("snr", "SNR", "dB", 1),
              ("ruido", "Ruído", "dBm", 1),
              ("custo_caminho", "Custo do caminho", "", 0),
              ("rtt", "Latência", "ms", 1),
              ("perda", "Perda", "%", 2), ("vazao", "Vazão", "Mbps", 2),
              ("taxa", "Taxa do enlace", "Mbps", 0),
              ("custo", "Custo do enlace", "", 0),
              ("peers", "Enlaces ativos", "", 0),
              ("vel", "Velocidade", "km/h", 1),
              ("sats", "Satélites", "", 0), ("hdop", "HDOP", "", 1))
    for c, rot, un, casas in campos:
        v = a.get(c)
        if v is None: continue
        if c == "sinal_cob" and a.get("servidor_cob"):
            rot = f"RSSI (melhor ERB/ERM: {_esc(a['servidor_cob'])})"
        try: txt = f"{float(v):.{casas}f}".rstrip("0").rstrip(".") if casas else f"{v}"
        except (TypeError, ValueError): txt = str(v)
        if sem_rota(v, c):
            txt = "sem rota"
        fora = ""
        if c in REQUISITOS or c in ("sinal_cob", "custo_caminho"):
            op, lim, _, _ = limite_de(c)
            try:
                ruim = not atende(float(v), op, lim)
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


def _png_calor(valor, alfa, cmap, vmin, vmax, caminho, faixas=None,
               na_de_baixo=()):
    """Raster RGBA com transparência POR PIXEL.

    O `_png_overlay` usa alfa constante, que serve a heatmap de área
    inteira. Aqui a borda precisa esvanecer, senão o rastro vira uma
    salsicha de contorno duro.

    `faixas` — [(limite_superior, "RRGGBB")] — pinta em DEGRAUS, com
    exatamente as cores da legenda. É o padrão, e o motivo é que o
    contrário estava errado:

    Antes o raster interpolava um gradiente contínuo de 256 tons entre
    `vmin` e `vmax`, enquanto a legenda do balão listava as faixas de
    `FAIXAS_KML`. As duas réguas não coincidem em ponto nenhum. Um pixel
    de −78 dBm caía em 34% do gradiente e saía laranja; a legenda diz que
    −80 a −75 é vermelho. Quem conferisse cor contra legenda encontrava
    outra coisa — e num laudo de aprovação isso é o bastante para
    invalidar a leitura.

    Com degraus, a cor do raster é a MESMA de `_bucket_cor()`, que já
    pinta os pontos e as linhas. Uma régua só para o arquivo inteiro.

    Sem `faixas`, mantém o gradiente contínuo — é o caminho de quem
    desenha PNG fora do KML, onde não há legenda por faixa.
    """
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    if faixas:
        lims = np.array([f[0] for f in faixas], dtype=float)
        cores = np.array(
            [[int(c[i:i+2], 16) / 255.0 for i in (0, 2, 4)] + [1.0]
             for _, c in faixas], dtype=float)
        # O MESMO lado da fronteira de `_faixa_idx`. `v < lim` estrito é
        # `side="right"`; os limites de `na_de_baixo` puxam o valor exato
        # para a faixa de baixo, um a um. Errar o lado punha o valor exato
        # de cada limite na faixa errada, e cada fronteira virava uma
        # linha de cor errada no mapa.
        v = np.nan_to_num(valor, nan=lims[-1])
        idx = np.searchsorted(lims, v, side="right")
        for k, lim in enumerate(lims):
            if lim in na_de_baixo:
                idx = np.where(v == lim, k, idx)
        idx = np.clip(idx, 0, len(faixas) - 1)
        rgba = cores[idx]
    else:
        norm = np.clip((valor - vmin) / max(vmax - vmin, 1e-9), 0, 1)
        rgba = cmap(np.nan_to_num(norm, nan=0.0))
    rgba = np.array(rgba, dtype=float, copy=True)
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


def _trechos_continuos(pts, fator=3.0, piso_s=30.0, salto_m=250.0,
                       manter_isolados=False):
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

    `manter_isolados` devolve também os trechos de UMA leitura só — a
    que ficou sozinha entre dois buracos. Sem linha a traçar, mas é
    medição e não pode sumir do mapa.
    """
    minimo = 1 if manter_isolados else 2
    if len(pts) < 2:
        return [pts] if len(pts) >= minimo else []
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
    return [t for t in trechos if len(t) >= minimo]


def _amostras_do_rastro(amostras, fixos=None, andou_m=30.0):
    """Só as amostras de quem se DESLOCOU — é o que vira rastro.

    O rádio parado dá dezenas de amostras no mesmo ponto: vira uma bola
    (ou um rabisco de GPS) isolada no mapa, e como BC fixo enxerga o
    vizinho de perto, ela sai verde. Eram essas as "bolas espalhadas e
    desconectadas" — e boa parte do verde que não batia com a mina.

    Com a lista de fixos, tira os fixos; sem ela (chamada solta, teste),
    separa pelo próprio dado. Lista vazia quando NINGUÉM andou — captura
    feita parada, tipicamente o MeshMapper ligado numa repetidora: 115
    leituras empilhadas em 0,5 m viravam uma mancha com a escala de ÁREA,
    a leitura errada mais cara que existe, porque parece um mapa.
    """
    parados = set(fixos or {})
    if parados:
        moveis = [a for a in amostras if a.get("radio") not in parados]
        return moveis or list(amostras)
    por_r = {}
    for a in amostras:
        if a.get("lat") is None: continue
        por_r.setdefault(a["radio"], []).append(a)
    andou = set()
    for r, ps in por_r.items():
        if len(ps) < 2: continue
        d = max(_dist_m(ps[0]["lat"], ps[0]["lon"], q["lat"], q["lon"])
                for q in ps)
        if d > andou_m: andou.add(r)
    return [a for a in amostras if a.get("radio") in andou]


def _fita_por_amostra(corrida, campo, faixas):
    """Uma corrida contínua → [(cor, [(lat, lon)...], [valores])].

    Cada amostra é dona do caminho entre o ponto médio com a anterior e o
    ponto médio com a seguinte — a mesma regra do calor, "vale a leitura
    real mais próxima", levada para a linha. Nenhum trecho mostra média
    de duas leituras nem cor interpolada entre elas.

    Trechos vizinhos na MESMA faixa viram uma polilinha só: um Placemark
    por par de pontos dava milhares de objetos, pesados de abrir e com
    emenda visível entre segmentos.
    """
    n = len(corrida)
    xy = [(p["lat"], p["lon"]) for p in corrida]
    meios = [((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
             for a, b in zip(xy, xy[1:])]
    pedacos = []
    for i, p in enumerate(corrida):
        geo = (([meios[i - 1]] if i > 0 else []) + [xy[i]]
               + ([meios[i]] if i < n - 1 else []))
        v = p.get(campo)
        cor = _bucket_cor(v, faixas, limites_na_faixa_de_baixo(campo))
        if pedacos and pedacos[-1][0] == cor:
            # O primeiro vértice é o ponto médio que o pedaço anterior já
            # terminou — repeti-lo daria segmento de comprimento zero.
            pedacos[-1][1].extend(geo[1:])
            pedacos[-1][2].append(v)
        else:
            pedacos.append([cor, geo, [v]])
    return [tuple(p) for p in pedacos]


# Raio do grupo da trilha consolidada, em metros: uma leitura por passagem
# a ~40 km/h, e maior que o erro do GPS do rádio mais a largura da pista,
# para que dois caminhões na mesma estrada caiam no mesmo grupo.
TRILHA_CELULA_M = 12.0


def _valor_da_celula(vals, campo):
    """Mediana CONSERVADORA das leituras do grupo — sempre uma leitura
    real, nunca média: com número par, a do meio do lado pior."""
    vs = sorted(v for v in vals if isinstance(v, (int, float)))
    if not vs:
        return None
    melhor_alto = (ESCALAS.get(campo) or {}).get("melhor", "alto") == "alto"
    return vs[(len(vs) - 1) // 2] if melhor_alto else vs[len(vs) // 2]


def trilha_consolidada(amostras, campo, faixas, celula_m=TRILHA_CELULA_M,
                       suavizar=2):
    """Uma linha por CAMINHO, não uma por veículo.

    Com 29 rádios nas mesmas estradas, uma linha por veículo virava uma
    faixa larga e salpicada: o GPS de cada um erra alguns metros para um
    lado, e a cor de um aparecia por baixo da do outro. Aqui as leituras
    de todos se juntam em grupos ao longo da pista: cada leitura entra no
    grupo mais próximo a até `celula_m`, ou abre um. O grupo fica no
    centro das suas leituras — o meio da pista — e vale a mediana
    conservadora das leituras a até `celula_m` dele, de todos os veículos. Dois grupos se ligam quando um veículo passou de
    um para o outro sem buraco de medição (`_trechos_continuos`); as
    ligações viram polilinhas, uma por trecho entre cruzamentos,
    suavizadas para tirar o zigue-zague do GPS.

    Grupo e não grade fixa: a grade corta a estrada de lado, e dois
    caminhões na mesma pista, a 10 m um do outro, caíam em células
    vizinhas — a linha abria em degrau.

    Devolve (pecas, cadeias, soltas):
      pecas  — [(cor, [(lat, lon)...], [valores dos grupos])], como a fita;
      cadeias — geometria de cada trecho, para o contorno;
      soltas — [(lat, lon, valor, n_leituras)] dos grupos sem ligação.
    """
    pts = [a for a in amostras if a.get("lat") is not None
           and a.get("lon") is not None]
    if not pts:
        return [], [], []
    lat0 = sum(a["lat"] for a in pts) / len(pts)
    lon0 = sum(a["lon"] for a in pts) / len(pts)
    kx = 111320.0 * math.cos(math.radians(lat0)); ky = 110540.0
    R = float(celula_m)

    lideres = []                 # (x, y) de quem abriu o grupo
    grade = {}                   # hash espacial dos líderes, lado R
    cel = []                     # por grupo: soma das posições e n

    def grupo(x, y):
        gx, gy = math.floor(x / R), math.floor(y / R)
        melhor, dmin = None, R
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for g in grade.get((gx + dx, gy + dy), ()):
                    lx, ly = lideres[g]
                    d = math.hypot(x - lx, y - ly)
                    if d <= dmin:
                        melhor, dmin = g, d
        if melhor is None:
            melhor = len(lideres)
            lideres.append((x, y))
            grade.setdefault((gx, gy), []).append(melhor)
            cel.append({"x": 0.0, "y": 0.0, "n": 0})
        return melhor

    viz = {}
    por_radio = {}
    todas = {}                   # hash espacial das leituras, lado R
    for a in pts:
        por_radio.setdefault(a.get("radio"), []).append(a)
    for r in sorted(por_radio, key=str):
        ps = sorted(por_radio[r], key=lambda x: x.get("ts") or 0)
        for corrida in _trechos_continuos(ps, manter_isolados=True):
            ant = None
            for a in corrida:
                x = (a["lon"] - lon0) * kx; y = (a["lat"] - lat0) * ky
                todas.setdefault((math.floor(x / R), math.floor(y / R)),
                                 []).append((x, y, a.get(campo)))
                k = grupo(x, y)
                c = cel[k]
                c["x"] += x; c["y"] += y; c["n"] += 1
                viz.setdefault(k, set())
                if ant is not None and ant != k:
                    viz[ant].add(k); viz[k].add(ant)
                ant = k
    for c in cel:
        c["x"] /= c["n"]; c["y"] /= c["n"]
        c["lat"] = lat0 + c["y"] / ky; c["lon"] = lon0 + c["x"] / kx
        # O valor é o das leituras a até R do PONTO da linha — de todos os
        # veículos —, não só das que caíram no grupo: com a divisão em
        # grupos desigual, um caminhão bom e um ruim no mesmo trecho davam
        # grupos alternando verde e vermelho.
        gx, gy = math.floor(c["x"] / R), math.floor(c["y"] / R)
        c["vals"] = [v for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                     for x, y, v in todas.get((gx + dx, gy + dy), ())
                     if math.hypot(x - c["x"], y - c["y"]) <= R]
        c["v"] = _valor_da_celula(c["vals"], campo)

    # Atalho em triângulo: o veículo mais rápido pula um grupo (A→C) que
    # os outros visitam (A→B→C). A ligação A–C é redundante e faria um
    # cruzamento falso no meio da pista.
    def dist(a, b):
        return math.hypot(cel[a]["x"] - cel[b]["x"], cel[a]["y"] - cel[b]["y"])
    for a in list(viz):
        for c_ in list(viz[a]):
            if c_ <= a or c_ not in viz[a]:
                continue
            dac = dist(a, c_)
            for b in viz[a] & viz[c_]:
                if (dist(a, b) < dac and dist(b, c_) < dac
                        and dist(a, b) + dist(b, c_) <= 1.3 * dac):
                    viz[a].discard(c_); viz[c_].discard(a)
                    break

    # Trechos entre cruzamentos: anda pelos grupos de grau 2 a partir de
    # cada ponta ou cruzamento; o que sobrar sem ponta é anel.
    usadas = set()
    cadeias_k = []

    def andar(a, b):
        cad = [a, b]
        usadas.add(frozenset((a, b)))
        while len(viz[cad[-1]]) == 2:
            prox = [n for n in viz[cad[-1]] if frozenset((cad[-1], n)) not in usadas]
            if not prox:
                break
            usadas.add(frozenset((cad[-1], prox[0])))
            cad.append(prox[0])
        return cad

    for k in sorted(viz):
        if len(viz[k]) != 2:
            for n in sorted(viz[k]):
                if frozenset((k, n)) not in usadas:
                    cadeias_k.append(andar(k, n))
    for k in sorted(viz):
        for n in sorted(viz[k]):
            if frozenset((k, n)) not in usadas:
                cadeias_k.append(andar(k, n))

    pecas, cadeias = [], []
    for cad in cadeias_k:
        geo = [(cel[k]["lat"], cel[k]["lon"]) for k in cad]
        # Suaviza só o miolo: pontas e cruzamentos ficam onde estão, e os
        # trechos continuam se encontrando.
        for _ in range(suavizar):
            if len(geo) < 3:
                break
            geo = ([geo[0]]
                   + [((p[0] + 2 * q[0] + r_[0]) / 4.0, (p[1] + 2 * q[1] + r_[1]) / 4.0)
                      for p, q, r_ in zip(geo, geo[1:], geo[2:])]
                   + [geo[-1]])
        cadeias.append(geo)
        falsas = [{"lat": la, "lon": lo, campo: cel[k]["v"]}
                  for (la, lo), k in zip(geo, cad)]
        pecas.extend(_fita_por_amostra(falsas, campo, faixas))
    soltas = [(c["lat"], c["lon"], c["v"], c["n"])
              for k, c in enumerate(cel) if not viz.get(k)]
    return pecas, cadeias, soltas


# Cor e nome do trecho cuja leitura não tem valor da grandeza. Nunca zero,
# nunca a cor de uma faixa: cinza, e dito na legenda.
COR_SEM_VALOR = "808080"
_SEM_VALOR = {"custo_caminho": "sem trace neste trecho"}


def sem_valor_txt(campo):
    return _SEM_VALOR.get(campo, "sem medição neste trecho")


def _rotulo_trecho(vals, un, campo=None):
    """Nome do trecho no Google Earth: a faixa MEDIDA nele, não a da
    legenda — clicar na linha diz o que foi lido ali."""
    vs = [v for v in vals if isinstance(v, (int, float))]
    sr = [v for v in vs if sem_rota(v, campo)]
    vs = [v for v in vs if not sem_rota(v, campo)]
    if not vs:
        return "sem rota" if sr else sem_valor_txt(campo)
    lo, hi = min(vs), max(vs)
    txt = (f"{lo:.0f} {un}" if round(lo) == round(hi)
           else f"{lo:.0f} a {hi:.0f} {un}").rstrip()
    return txt + (" · sem rota" if sr else "")


def _faixas_rotuladas(campo):
    """[(rótulo da faixa, 'RRGGBB', classificação)] na ordem de FAIXAS_KML.

    O rótulo diz exatamente quais valores a faixa contém, fronteira
    incluída. "-75 a -70" deixava em aberto onde ficava o -75 — e ele é
    reprovado. Nas grandezas inteiras sai o intervalo em inteiros
    ("-74 a -71"); nas demais, a desigualdade ("≥ 2 e < 5").

    Fonte única dos rótulos: legenda do KMZ, balão, slide de Metodologia
    e gráfico de distribuição leem daqui.
    """
    faixas = FAIXAS_KML[campo]
    baixo = limites_na_faixa_de_baixo(campo)
    classes = CLASSES_FAIXA.get(campo, [])
    inteiro = campo in _CAMPOS_INTEIROS
    out, ant = [], None
    for k, (lim, cor) in enumerate(faixas):
        ultimo = k == len(faixas) - 1
        # O limite de baixo desta faixa pertence a ela se NÃO foi puxado
        # para a faixa anterior; o de cima, se foi puxado para esta.
        tem_ant = ant is not None and ant not in baixo
        tem_lim = lim in baixo
        if inteiro:
            lo = None if ant is None else int(ant if tem_ant else ant + 1)
            hi = None if ultimo else int(lim if tem_lim else lim - 1)
            if lo is None:   rng = f"≤ {hi}" if hi != 0 or campo != "enl_bons" else "0"
            elif hi is None: rng = f"≥ {lo}"
            elif lo == hi:   rng = f"{lo}"
            else:            rng = f"{lo} a {hi}"
        else:
            s_lo = None if ant is None else (f"≥ {ant:g}" if tem_ant
                                             else f"> {ant:g}")
            s_hi = None if ultimo else (f"≤ {lim:g}" if tem_lim
                                        else f"< {lim:g}")
            rng = " e ".join(x for x in (s_lo, s_hi) if x)
        out.append((rng, cor, classes[k] if k < len(classes) else ""))
        ant = lim
    return out


# Raio do calor: cada ponto do mapa toma a faixa que reúne MAIS trecho
# medido a até esta distância. Largo o bastante para a mina inteira caber
# na tela e a faixa ainda se ler (o KMZ de referência dá ~100 m de
# largura); curto para não pintar o banco de cima com a pista de baixo.
RAIO_CALOR_M = 40.0
# Passo em que o trajeto é amostrado entre duas leituras do mesmo veículo.
_PASSO_CALOR_M = 5.0
_ALFA_CALOR, _ALFA_CALOR_SEM = 0.82, 0.5


def _pior_primeiro(campo, n):
    """Peso de desempate por faixa: empate na contagem vai para a PIOR."""
    alto = (ESCALAS.get(campo) or {}).get("melhor", "alto") == "alto"
    return [((n - k) if alto else (k + 1)) * 1e-3 for k in range(n)]


def calor_por_faixas(amostras, campo, raio_m=RAIO_CALOR_M, res_m=2.5):
    """Mapa de calor por FAIXA, para GroundOverlay.

    Cada pixel toma a faixa da legenda que reúne MAIS trecho medido a até
    `raio_m` dele. O trecho é o caminho do veículo entre duas leituras, e
    cada leitura é dona da metade dele até a vizinha — a regra da trilha:
    nenhum valor é inventado entre duas leituras, e onde houve buraco de
    medição (`_trechos_continuos`) não há trecho. Contar só as leituras
    deixava o mapa em contas soltas: depois do descarte dos parados, um
    caminhão a 40 km/h fica com leituras a 20–100 m uma da outra.

    Contagem, não média: a cor é sempre uma faixa da régua. Empate vai
    para a pior. Onde só há leitura SEM valor (sem trace, por exemplo),
    cinza mais claro; onde não há leitura, transparente.

    Devolve (png_bytes, (norte, sul, leste, oeste), n_leituras) ou None.
    """
    import numpy as np, io as _io
    from PIL import Image
    from scipy.ndimage import gaussian_filter
    faixas = FAIXAS_KML[campo]
    baixo = limites_na_faixa_de_baixo(campo)
    lidas = [a for a in amostras
             if a.get("lat") is not None and a.get("lon") is not None]
    if not lidas:
        return None
    lat0 = sum(a["lat"] for a in lidas) / len(lidas)
    kx = 111320.0 * math.cos(math.radians(lat0)); ky = 110540.0
    # O trajeto amostrado a cada _PASSO_CALOR_M, cada ponto com o valor da
    # leitura mais próxima NO TRAJETO (metade do caminho para cada lado).
    pts = []
    por_radio = {}
    for a in lidas:
        por_radio.setdefault(a.get("radio"), []).append(a)
    for r in sorted(por_radio, key=str):
        ls = sorted(por_radio[r], key=lambda x: x.get("ts") or 0)
        for corrida in _trechos_continuos(ls, manter_isolados=True):
            if len(corrida) == 1:
                a = corrida[0]; pts.append((a["lat"], a["lon"], a.get(campo)))
                continue
            for p, q in zip(corrida, corrida[1:]):
                d = _dist_m(p["lat"], p["lon"], q["lat"], q["lon"])
                n_ = max(1, int(d / _PASSO_CALOR_M))
                for t in range(n_):
                    f = t / n_
                    pts.append((p["lat"] + (q["lat"] - p["lat"]) * f,
                                p["lon"] + (q["lon"] - p["lon"]) * f,
                                (p if f < 0.5 else q).get(campo)))
            a = corrida[-1]; pts.append((a["lat"], a["lon"], a.get(campo)))
    xs = [p[1] * kx for p in pts]; ys = [p[0] * ky for p in pts]
    margem = raio_m + 8.0
    x0, x1 = min(xs) - margem, max(xs) + margem
    y0, y1 = min(ys) - margem, max(ys) + margem
    res = max(res_m, (x1 - x0) / 4000.0, (y1 - y0) / 4000.0)
    W = int(math.ceil((x1 - x0) / res)) + 1
    H = int(math.ceil((y1 - y0) / res)) + 1
    n = len(faixas)
    cont = np.zeros((n + 1, H, W), dtype=np.float32)     # n = sem valor
    r = int(math.ceil(raio_m / res))
    yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
    disco = ((xx * xx + yy * yy) <= (raio_m / res) ** 2).astype(np.float32)
    for (la, lo, v), x, y in zip(pts, xs, ys):
        k = (_faixa_idx(v, faixas, baixo) if isinstance(v, (int, float))
             and not sem_rota(v, campo) else (n - 1 if isinstance(v, (int, float))
                                              else n))
        c, l = int((x - x0) / res), int((y1 - y) / res)
        cont[k, l - r:l + r + 1, c - r:c + r + 1] += disco
    com_valor = cont[:n].sum(axis=0) > 0
    so_sem = (~com_valor) & (cont[n] > 0)
    # Contagem suavizada (~4 m) antes de escolher a faixa: a fronteira
    # entre duas faixas sai limpa, sem as lascas de um pixel que o disco
    # deixava onde os votos empatavam quase. Só muda a forma da fronteira —
    # a cor continua sendo a faixa da maioria, nunca uma média.
    sig = max(0.8, 4.0 / res)
    suave = np.stack([gaussian_filter(cont[k], sig) for k in range(n)])
    peso = np.array(_pior_primeiro(campo, n), dtype=np.float32)[:, None, None]
    cls = np.argmax(suave + peso * (suave > 1e-3), axis=0)
    paleta = np.array([[int(c[i:i + 2], 16) for i in (0, 2, 4)]
                       for _, c in faixas], dtype=np.float32)
    rgb = paleta[cls]
    rgb[so_sem] = [int(COR_SEM_VALOR[i:i + 2], 16) for i in (0, 2, 4)]
    alfa = np.where(com_valor, _ALFA_CALOR, np.where(so_sem, _ALFA_CALOR_SEM, 0.0))
    # Borda macia (1 px de desfoque) e sombra escura FORA da mancha: ela se
    # separa do chão da cava sem linha de contorno. Dentro, o RGB é o da
    # faixa, exato — só a transparência varia; a sombra composta por baixo
    # escurecia a cor e ela deixava de ser a da legenda.
    cobre = com_valor | so_sem
    # Borda esfumada em ~6 m, não num pixel: a faixa termina macia, como
    # calor, e o miolo segue com a transparência cheia.
    alfa = (alfa * np.clip(gaussian_filter(cobre.astype(np.float32),
                                           max(1.0, 3.0 / res)) * 1.6,
                           0, 1)).astype(np.float32)
    sombra = (gaussian_filter(cobre.astype(np.float32), 3.0) * 0.28).astype(np.float32)
    rgb[~cobre] = 0
    a_fim = np.where(cobre, alfa, sombra)
    img = np.dstack([rgb, np.clip(a_fim * 255, 0, 255)]).astype(np.uint8)
    buf = _io.BytesIO()
    Image.fromarray(img, "RGBA").save(buf, "PNG", optimize=True)
    caixa = (y1 / ky, y0 / ky, x1 / kx, x0 / kx)
    return buf.getvalue(), caixa, len(lidas)


def icones_kmz():
    """Ícones brancos com borda escura, tingidos pela cor do IconStyle. Vão
    DENTRO do KMZ: os do servidor do Google não abrem na mina sem internet."""
    import io as _io
    from PIL import Image, ImageDraw
    out = {}
    for nome, forma in (("pt_dot", "c"), ("fx_square", "q")):
        g = 256
        im = Image.new("RGBA", (g, g), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        if forma == "c":
            d.ellipse((24, 24, g - 24, g - 24), fill=(20, 22, 28, 255))
            d.ellipse((52, 52, g - 52, g - 52), fill=(255, 255, 255, 255))
        else:
            d.rounded_rectangle((28, 28, g - 28, g - 28), 44, fill=(20, 22, 28, 255))
            d.rounded_rectangle((56, 56, g - 56, g - 56), 26, fill=(255, 255, 255, 255))
        im = im.resize((64, 64), Image.LANCZOS)
        buf = _io.BytesIO(); im.save(buf, "PNG")
        out[f"files/{nome}.png"] = buf.getvalue()
    return out


# Pinos no formato do MeshMapper: gota com o número de enlaces bons, e
# estrela com o de ótimos. Cores medidas nos pinos do KMZ oficial.
PINOS_RAJANT = {
    "pin0": ("gota", "0", "D80E0E", "B70404"),
    "pin1": ("gota", "1", "DD581D", "CE4A00"),
    "pin2": ("gota", "2", "DD9F17", "C6910E"),
    "pin3": ("gota", "3+", "1D991D", "17821A"),
    "pinx": ("gota", "!", "0BA6DD", "0590CB"),
    "est1": ("estrela", "1", "C1793E", "B76F2E"),
    "est2": ("estrela", "2", "CCCCCC", "B8B8B8"),
    "est3": ("estrela", "3+", "D4AF37", "C29D2B"),
}


def pino_da_leitura(a):
    """O pino do MeshMapper para a amostra: sem vizinho, "!"; com enlace
    ótimo, estrela com quantos; senão gota com quantos bons (3 = 3+)."""
    if a.get("enl_total") is None:
        return None
    if not a["enl_total"]:
        return "pinx"
    if a.get("enl_otimos"):
        return f"est{min(a['enl_otimos'], 3)}"
    return f"pin{min(a.get('enl_bons') or 0, 3)}"


# Um pino a cada tanto de estrada. O MeshMapper põe um por leitura — com
# um veículo e 41 pontos, legível; com 29 veículos viraria um tapete de
# milhares de pinos por cima do calor.
PINO_RAIO_M = 30.0


def pinos_por_trecho(amostras, raio_m=PINO_RAIO_M):
    """[(leitura, n_no_trecho)]: uma leitura REAL por trecho de `raio_m`.

    As leituras se agrupam em torno da primeira que abriu o grupo; fica a
    do meio pelo lado pior (a mesma mediana conservadora da trilha) —
    o pino mostra uma leitura que aconteceu, com o número dela."""
    grupos, grade, lideres = [], {}, []
    lat0 = next((a["lat"] for a in amostras if a.get("lat") is not None), 0.0)
    kx = 111320.0 * math.cos(math.radians(lat0)); ky = 110540.0
    for a in sorted(amostras, key=lambda x: x.get("ts") or 0):
        if a.get("lat") is None or a.get("enl_total") is None:
            continue
        x, y = a["lon"] * kx, a["lat"] * ky
        gx, gy = math.floor(x / raio_m), math.floor(y / raio_m)
        alvo = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for g in grade.get((gx + dx, gy + dy), ()):
                    lx, ly = lideres[g]
                    if math.hypot(x - lx, y - ly) <= raio_m:
                        alvo = g; break
                if alvo is not None: break
            if alvo is not None: break
        if alvo is None:
            alvo = len(grupos); grupos.append([]); lideres.append((x, y))
            grade.setdefault((gx, gy), []).append(alvo)
        grupos[alvo].append(a)
    out = []
    for g in grupos:
        v = _valor_da_celula([a.get("enl_bons") for a in g], "enl_bons")
        rep_ = next((a for a in g if a.get("enl_bons") == v), g[0])
        out.append((rep_, len(g)))
    return out


def _nome_do_pino(a):
    """O texto do selo no balão do pino."""
    if not a.get("enl_total"):
        return "sem vizinho"
    b = a.get("enl_bons") or 0
    return f"{b} enlace{'s' if b != 1 else ''} bo{'ns' if b != 1 else 'm'}"


def _balao_enlaces(a, n_trecho=1):
    """Balão do pino: a contagem, os enlaces bons pelo nome e o que mais foi
    medido no ponto."""
    q = datetime.fromtimestamp(a["ts"]).strftime("%d/%m/%Y %H:%M:%S") if a.get("ts") else ""
    linhas = [("Enlaces bons", a.get("enl_bons")),
              ("Enlaces ótimos", a.get("enl_otimos")),
              ("Vizinhos", a.get("enl_total")),
              ("Custo do caminho", fmt_leitura(a.get("custo_caminho"), "custo_caminho")
               if a.get("custo_caminho") is not None else None),
              ("Sai por", a.get("servidor")),
              ("RSSI (melhor ERB/ERM)", fmt_leitura(a.get("sinal_cob"), "sinal_cob")
               if a.get("sinal_cob") is not None else None),
              ("Leituras neste trecho", n_trecho if n_trecho > 1 else None)]
    tab = "".join(f"<tr><td style='color:#6B7280'>{_esc(k)}</td>"
                  f"<td><b>{_esc(v)}</b></td></tr>"
                  for k, v in linhas if v is not None)
    lista = a.get("enl_lista") or []
    itens = "".join(
        f"<tr><td>{'★ ' if c == 'otimo' else ''}{_esc(n)}</td>"
        f"<td>{_esc(f'{cu:.0f}' if cu is not None else '—')}</td>"
        f"<td>{_esc(f'{sn:.0f} dB' if sn is not None else '—')}</td></tr>"
        for n, cu, sn, c in lista[:8])
    extra = (f"<p style='margin:8px 0 2px'><b>Enlaces bons</b></p>"
             f"<table style='font-size:12px'><tr style='color:#6B7280'>"
             f"<td>vizinho</td><td>custo</td><td>SNR</td></tr>{itens}</table>"
             if itens else "")
    return (f"<![CDATA[<p style='margin:6px 0 2px'><b>{_esc(a.get('radio'))}</b>"
            f" <small>{_esc(q)}</small></p><table style='font-size:12px'>{tab}"
            f"</table>{extra}]]>")


def pinos_png():
    """Os PNGs dos pinos, para ir dentro do KMZ. A fonte é a DejaVu que vem
    com o matplotlib: a mesma no Windows e no Linux, sem depender do que
    está instalado na máquina."""
    import io as _io, os as _os, math as _m
    import matplotlib
    from PIL import Image, ImageDraw, ImageFont
    fonte = _os.path.join(matplotlib.get_data_path(), "fonts", "ttf",
                          "DejaVuSans-Bold.ttf")
    out = {}
    for nome, (forma, txt, claro, escuro) in PINOS_RAJANT.items():
        k = 4                                   # desenha grande e reduz
        W, H = (64, 80) if forma == "gota" else (64, 64)
        im = Image.new("RGBA", (W * k, H * k), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        c1 = tuple(int(claro[i:i + 2], 16) for i in (0, 2, 4)) + (255,)
        c2 = tuple(int(escuro[i:i + 2], 16) for i in (0, 2, 4)) + (255,)
        if forma == "gota":
            r = 29 * k; cx, cy = 32 * k, 31 * k
            ponta = (cx, 78 * k)
            corpo = [(cx - r * 0.72, cy + r * 0.69), ponta, (cx + r * 0.72, cy + r * 0.69)]
            d.polygon(corpo, fill=c1)
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=c1)
            # metade direita mais escura, como o pino da Rajant
            meia = Image.new("RGBA", im.size, (0, 0, 0, 0))
            dm = ImageDraw.Draw(meia)
            dm.polygon(corpo, fill=c2)
            dm.ellipse((cx - r, cy - r, cx + r, cy + r), fill=c2)
            mascara = Image.new("L", im.size, 0)
            ImageDraw.Draw(mascara).rectangle((cx, 0, W * k, H * k), fill=255)
            im.paste(meia, (0, 0), Image.composite(meia, Image.new("RGBA", im.size), mascara).split()[3])
            tam = 30 if len(txt) == 1 else 24
            centro = (cx, cy)
        else:
            cx, cy, R, r = 32 * k, 34 * k, 31 * k, 13 * k
            pts = []
            for i in range(10):
                ang = -_m.pi / 2 + i * _m.pi / 5
                rr = R if i % 2 == 0 else r
                pts.append((cx + rr * _m.cos(ang), cy + rr * _m.sin(ang)))
            d.polygon(pts, fill=c1)
            d.polygon([pts[0], (cx, cy)] + pts[5:] + [pts[0]] if False else
                      [(cx, cy)] + pts[5:10] + [pts[0]], fill=c2)
            tam = 22 if len(txt) == 1 else 17
            centro = (cx, cy + 2 * k)
        f = ImageFont.truetype(fonte, tam * k)
        d.text(centro, txt, font=f, fill=(255, 255, 255, 255), anchor="mm")
        im = im.resize((W, H), Image.LANCZOS)
        buf = _io.BytesIO(); im.save(buf, "PNG")
        out[f"files/{nome}.png"] = buf.getvalue()
    return out


def _texto_sobre(cor_hex):
    """Cor de texto legível sobre o fundo `cor_hex` (selo do balão)."""
    r, g, b = (int(cor_hex[i:i + 2], 16) for i in (0, 2, 4))
    return "0F141E" if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else "FFFFFF"


def _balao_com_selo(cor):
    """BalloonStyle com o valor num selo da cor da faixa."""
    return ("<BalloonStyle><text><![CDATA[<div style=\"font-family:'Segoe UI',"
            "Arial,sans-serif;min-width:230px\"><div style=\"display:inline-block;"
            f"padding:3px 12px;border-radius:12px;background:#{cor};"
            f"color:#{_texto_sobre(cor)};font-size:16px;font-weight:bold\">"
            "$[name]</div>$[description]</div>]]></text></BalloonStyle>")


def _requisito_entre(campo):
    """Posição (na ordem da legenda: melhor em cima) da fronteira entre as
    faixas que atendem o requisito e as que não atendem; None se nenhuma."""
    req = limite_de(campo)
    if not req:
        return None
    op, lim = req[0], req[1]
    faixas = FAIXAS_KML[campo]
    reps = []
    for k, (l, _c) in enumerate(faixas):
        ant = faixas[k - 1][0] if k else None
        if ant is None:
            v = l - 1
        elif k == len(faixas) - 1:
            v = ant + 1
        else:
            v = (ant + l) / 2.0
        reps.append(atende(v, op, lim))
    if (ESCALAS.get(campo) or {}).get("melhor", "alto") == "alto":
        reps = list(reversed(reps))
    for i in range(1, len(reps)):
        if reps[i - 1] and not reps[i]:
            return i
    return None


def _legenda_de_faixas_png(caminho, campo, banda=None, linha=None):
    """A legenda da grandeza, em cartão escuro, para o ScreenOverlay.

    Melhor faixa em cima, pior embaixo, em qualquer grandeza. O requisito
    é uma linha tracejada ENTRE as faixas que atendem e as que não; por
    último o cinza da leitura sem valor. Toda cor que o mapa pinta está
    aqui, e só elas.
    """
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch
    from matplotlib.textpath import TextPath
    from matplotlib.font_manager import FontProperties
    import textwrap as _tw

    def larg(txt, tam, negrito=False):
        """Largura do texto, em pixels a 100 dpi — medida, não estimada."""
        fp = FontProperties(weight="bold" if negrito else "normal")
        return TextPath((0, 0), txt, size=tam, prop=fp).get_extents().width * 100 / 72

    esc = ESCALAS.get(campo) or {}
    linhas = _faixas_rotuladas(campo)
    if esc.get("melhor", "alto") == "alto":
        linhas = list(reversed(linhas))
    corte = _requisito_entre(campo)
    linhas = list(linhas) + [(sem_valor_txt(campo), COR_SEM_VALOR, "")]
    req = limite_de(campo)
    fonte = FONTE_FAIXAS.get(campo, "")
    notas = ([f"Calor: faixa com mais trecho medido num raio de "
              f"{RAIO_CALOR_M:g} m (empate: a pior). Sem leitura por perto: "
              f"transparente. Quadrado: ERB/ERM, mediana das leituras dela."]
             + (["Pino: enlaces bons no ponto. Estrela: enlaces ótimos. "
                 "\"!\": sem vizinho."] if campo == "enl_bons" else [])
             + ([fonte] if fonte else []))
    linhas_l = _faixas_rotuladas(linha) if linha else []
    notas = [l for t in notas for l in _tw.wrap(t, 64)]
    titulo = esc.get("rot", nome_da_aba(campo))
    lg = max(372, int(16 + larg(titulo, 11, True) + 10
                      + (larg(banda, 8, True) + 14 if banda else 0) + 18))
    alt = (58 + 26 * len(linhas) + (22 if corte is not None else 0)
           + (30 + 20 * len(linhas_l) if linhas_l else 0)
           + 14 + 12 * len(notas) + 12)
    fig = plt.figure(figsize=(lg / 100, alt / 100), dpi=100)
    fig.patch.set_alpha(0)
    ax = fig.add_axes([0, 0, 1, 1]); ax.axis("off")
    ax.set_xlim(0, lg); ax.set_ylim(alt, 0)
    ax.add_patch(FancyBboxPatch((3, 3), lg - 6, alt - 6,
                                boxstyle="round,pad=0,rounding_size=12",
                                facecolor="#1B2230", edgecolor="#3A4458",
                                linewidth=1.0, alpha=0.93))
    ax.text(16, 22, titulo, color="#FFFFFF", fontsize=11, fontweight="bold",
            va="center")
    if banda:
        x_b = 16 + larg(titulo, 11, True) + 10
        ax.add_patch(FancyBboxPatch((x_b, 13), larg(banda, 8, True) + 14, 18,
                                    boxstyle="round,pad=0,rounding_size=9",
                                    facecolor="#303A4E", edgecolor="none"))
        ax.text(x_b + 7, 22, banda, color="#E6E9EF", fontsize=8,
                fontweight="bold", va="center")
    un = esc.get("un", "")
    if un:
        ax.text(16, 42, un, color="#9AA3B2", fontsize=7.5, va="center")
    x_cls = 58 + max([larg(r, 9, True) for r, _c, k in linhas if k]
                     + [larg(r, 8) for r, _c, _k in linhas_l],
                     default=0) + 18
    y = 58
    for i, (rng, cor, cls) in enumerate(linhas):
        if corte is not None and i == corte:
            ax.plot([16, lg - 16], [y + 4, y + 4], color="#FFFFFF",
                    linewidth=0.9, linestyle=(0, (4, 3)), alpha=0.85)
            op, lim, un_r, _rot = req
            nome_req = ("Referência Rajant" if campo in ("custo_caminho", "enl_bons")
                        else "Requisito Modular" if campo in REQUISITOS
                        or campo == "sinal_cob" else "Referência")
            txt = f"{nome_req}: {_OP_TXT[op]} {lim:g} {un_r}".rstrip()
            w = larg(txt, 7, True) + 18
            ax.add_patch(FancyBboxPatch((lg - 16 - w, y - 5), w, 18,
                                        boxstyle="round,pad=0,rounding_size=9",
                                        facecolor="#1B2230", edgecolor="#FFFFFF",
                                        linewidth=0.9, zorder=3))
            ax.text(lg - 16 - w / 2, y + 4, txt, color="#FFFFFF", fontsize=7,
                    fontweight="bold", va="center", ha="center", zorder=4)
            y += 22
        ax.add_patch(FancyBboxPatch((16, y + 3), 32, 14,
                                    boxstyle="round,pad=0,rounding_size=7",
                                    facecolor="#" + cor, edgecolor="none"))
        sem = cor == COR_SEM_VALOR and not cls
        ax.text(58, y + 10, rng, color="#9AA3B2" if sem else "#FFFFFF",
                fontsize=8 if sem else 9, fontweight="normal" if sem else "bold",
                va="center")
        if cls:
            ax.text(x_cls, y + 10, cls, color="#C3CAD6", fontsize=7.5, va="center")
        y += 26
    if linhas_l:
        ax.plot([16, lg - 16], [y + 4, y + 4], color="#3A4458", linewidth=0.8)
        ax.text(16, y + 18, f"Linha do trajeto: {ESCALAS[linha]['rot'].lower()}",
                color="#FFFFFF", fontsize=8, fontweight="bold", va="center")
        y += 30
        for rng, cor, cls in linhas_l:
            ax.add_patch(FancyBboxPatch((16, y + 5), 32, 5,
                                        boxstyle="round,pad=0,rounding_size=2.5",
                                        facecolor="#" + cor, edgecolor="none"))
            ax.text(58, y + 8, rng, color="#FFFFFF", fontsize=8, va="center")
            ax.text(x_cls, y + 8, cls, color="#C3CAD6", fontsize=7.5, va="center")
            y += 20
    ax.plot([16, lg - 16], [y + 4, y + 4], color="#3A4458", linewidth=0.8)
    ax.text(16, y + 12, "\n".join(notas), color="#9AA3B2", fontsize=6.5,
            va="top", linespacing=1.35)
    fig.savefig(caminho, transparent=True)
    plt.close(fig)
    return caminho


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
    cores_ponto = set()
    cores_todas = {c for cp in campos for _, c in FAIXAS_KML[cp]}
    cores_todas.add(COR_SEM_VALOR)     # trecho sem medição
    if "enl_bons" in campos:           # a linha da aba principal é o custo
        cores_todas |= {c for _, c in FAIXAS_KML["custo_caminho"]}
    # Ícones dentro do KMZ; no KML solto não há onde guardá-los.
    ico_pt = ("files/pt_dot.png" if comprimir else
              "http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png")
    ico_fx = ("files/fx_square.png" if comprimir else
              "http://maps.google.com/mapfiles/kml/shapes/square.png")
    estilos = ['<Style id="radio"><ListStyle><listItemType>radioFolder'
               '</listItemType></ListStyle></Style>']
    for cor in sorted(cores_todas):
        # A fita colorida E o dado. Largura de survey de verdade: com
        # 2,6 px a rota sumia sobre o satelite da cava, que e claro e
        # cheio de textura — parecia risco de GPS, nao medicao.
        # OPACA: com alfa, o vermelho do chao da cava tingia o verde e a
        # cor vista deixava de ser a da faixa da legenda.
        estilos.append(
            f'<Style id="l{cor}"><LineStyle><color>{_kml_cor(cor, 255)}</color>'
            f'<width>6</width></LineStyle></Style>')
    # Contorno da fita: mais largo, escuro e por baixo — separa a cor do
    # terreno. Pode ser firme porque TODOS os contornos vão antes de todas
    # as cores (ver `_aba`): o de um veículo não cobre mais a cor de outro
    # no cruzamento de pistas, que era o motivo de ele ser quase apagado.
    estilos.append(
        '<Style id="lcontorno"><LineStyle><color>b4141008</color>'
        '<width>9</width></LineStyle></Style>')
    estilos.append(
        f'<Style id="pFora"><IconStyle><color>{_kml_cor("FF0000")}</color>'
        f'<scale>0.7</scale><Icon><href>{ico_pt}</href></Icon><hotSpot x="0.5" '
        f'y="0.5" xunits="fraction" yunits="fraction"/></IconStyle><LabelStyle>'
        f'<scale>0</scale></LabelStyle>{_balao_com_selo("FF0000")}</Style>')
    for tipo, (href, cor) in _ICONES_KML.items():
        estilos.append(
            f'<Style id="bc{_esc(tipo)}"><IconStyle><color>{_kml_cor(cor)}</color>'
            f'<scale>1.15</scale><Icon><href>{href}</href></Icon></IconStyle>'
            f'<LabelStyle><scale>0.85</scale></LabelStyle></Style>')
    estilos.append(
        '<Style id="manual"><IconStyle><color>ffff00ff</color><scale>1.0</scale>'
        '<Icon><href>http://maps.google.com/mapfiles/kml/shapes/ruler.png'
        '</href></Icon></IconStyle></Style>')
    # Os estilos "zona" (polígono da zona-problema) e "sugestao" (alfinete
    # do local recomendado) sairam junto com a pasta ZONAS-PROBLEMA. Estilo
    # sem styleUrl que o use e peso morto no arquivo, e um <Style id> que
    # sobrevive ao que ele estilizava convida a ressuscitar a pasta por
    # engano.

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
    # Abre no mapa de calor por faixa (`calor_por_faixas`); a trilha e as
    # leituras ficam na aba, desligadas.

    # PNGs do calor, embutidos no KMZ. Em KML solto nao ha onde guardar a
    # imagem, e overlay apontando para arquivo ausente nao desenha nada —
    # entao o calor so sai no KMZ.
    extras = []
    cores_fixos = set()
    rx_infra = re.compile(PADRAO_INFRA, re.I)

    def _calor_overlay(campo_a):
        """O mapa de calor da aba: a camada que se lê de relance."""
        if not comprimir:
            return ""
        do_calor = [a for a in am if not rx_infra.search(a.get("radio") or "")]
        try:
            r = calor_por_faixas(do_calor, campo_a)
        except Exception as e:
            log.warning(f"[kml] calor de {campo_a} falhou: {e}")
            return ""
        if not r:
            return ""
        png, (n_, s_, l_, o_), n_lei = r
        nome_png = f"files/calor_{campo_a}.png"
        extras.append((nome_png, png))
        n_eq = len({a.get("radio") for a in do_calor})
        return (f"<GroundOverlay><name>{CAMADA_DO_CALOR}</name>"
                f"<description><![CDATA[{n_lei} leituras de {n_eq} "
                f"equipamentos; ERB/ERM ficam fora. Cor: faixa com mais "
                f"trecho medido num raio de {RAIO_CALOR_M:g} m.]]></description>"
                f"<drawOrder>1</drawOrder><Icon><href>{nome_png}</href></Icon>"
                f"<LatLonBox><north>{n_:.7f}</north><south>{s_:.7f}</south>"
                f"<east>{l_:.7f}</east><west>{o_:.7f}</west></LatLonBox>"
                f"</GroundOverlay>")

    def _fixos_na_aba(campo_a, un_a):
        """Um quadrado por ERB/ERM, na cor da mediana das leituras dela."""
        por = {}
        for a in am:
            if rx_infra.search(a.get("radio") or ""):
                por.setdefault(a["radio"], []).append(a)
        itens = []
        for nome, ls in sorted(por.items()):
            v = _valor_da_celula([a.get(campo_a) for a in ls], campo_a)
            cor = cor_da_leitura(v, campo_a)
            cores_fixos.add(cor)
            la = sorted(a["lat"] for a in ls)[len(ls) // 2]
            lo = sorted(a["lon"] for a in ls)[len(ls) // 2]
            saidas = [a.get("servidor") for a in ls if a.get("servidor")]
            sai = max(set(saidas), key=saidas.count) if saidas else None
            linhas = [(nome_da_aba(campo_a), fmt_leitura(v, campo_a)),
                      ("Leituras", str(len(ls)))]
            if sai:
                linhas.append(("Sai por", sai))
            tab = "".join(f"<tr><td>{_esc(k)}</td><td><b>{_esc(x)}</b></td></tr>"
                          for k, x in linhas)
            itens.append(_placemark(
                nome, f"<![CDATA[<table style='font-size:12px;margin-top:6px'>"
                      f"{tab}</table>]]>", f"fx{cor}", ponto=(la, lo)))
        if not itens:
            return ""
        return (f"<Folder><name>ERB/ERM ({len(itens)})</name><open>0</open>"
                f"<description><![CDATA[Quadrado na cor da mediana das "
                f"leituras de cada ERB/ERM.]]></description>{''.join(itens)}"
                f"</Folder>")

    def _calor_no_kmz(amostras_aba, campo, visivel, sufixo="", rotulo=None):
        if not comprimir:
            return ""
        amostras_aba = _amostras_do_rastro(amostras_aba, fixos)
        if not amostras_aba:
            log.info("[kml] nenhuma amostra em deslocamento: "
                     "sem rastro de calor nesta aba")
            return ""
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
            # As MESMAS faixas da legenda do balão e das linhas. Ver a
            # docstring de _png_calor: o gradiente contínuo daqui não
            # batia com a legenda em ponto nenhum.
            _png_calor(valor, alfa,
                       _cmap_rf(invertido=(esc.get("melhor") == "baixo")),
                       float(esc["lo"]), float(esc["hi"]), cam,
                       faixas=FAIXAS_KML.get(campo),
                       na_de_baixo=limites_na_faixa_de_baixo(campo))
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

    def _legenda_na_tela(campo_a):
        # A legenda vai NO MAPA, não só no balão do documento: o print do
        # Google Earth que vai para o slide leva junto a régua com que foi
        # pintado, e a cor pode ser conferida contra ela na própria imagem.
        # Sai das mesmas FAIXAS_KML que pintam a linha — desenhada à parte,
        # divergiria do mapa na primeira vez que uma faixa mudasse.
        if not comprimir:
            return ""
        try:
            import tempfile as _tf, os as _os
            nome_png = f"legenda_{campo_a}.png"
            cam = _os.path.join(_tf.mkdtemp(), nome_png)
            _legenda_de_faixas_png(cam, campo_a, banda=banda,
                                   linha=("custo_caminho" if campo_a == "enl_bons"
                                          and any(a.get("custo_caminho") is not None
                                                  for a in am) else None))
            with open(cam, "rb") as fh:
                extras.append((f"files/{nome_png}", fh.read()))
        except Exception as e:
            log.warning(f"[kml] legenda de {campo_a} falhou: {e}")
            return ""
        return (f"<ScreenOverlay><name>Legenda</name>"
                f"<Icon><href>files/{nome_png}</href></Icon>"
                f'<overlayXY x="0" y="0" xunits="fraction" yunits="fraction"/>'
                f'<screenXY x="12" y="30" xunits="pixels" yunits="pixels"/>'
                f'<size x="0" y="0" xunits="pixels" yunits="pixels"/>'
                f"</ScreenOverlay>")

    def _aba(campo_a, visivel):
        faixas_a = FAIXAS_KML[campo_a]
        op_a, lim_a, un_a, _r = (limite_de(campo_a)
                                 or (">", None, "", campo_a.upper()))
        rot_a = nome_da_aba(campo_a)
        dentro = []

        # ── o trajeto: fita contínua, como o MeshMapper desenha ──
        # Uma linha por onde o rádio passou, colorida pela leitura. Era
        # calor — um núcleo de raio fixo por amostra —, e com amostras a
        # 60-80 m e raio de 25 m o rastro saía em bolhas soltas, borradas
        # nas bordas, com o chão da cava tingindo a cor pela transparência.
        #
        # FIDELIDADE À LEGENDA, por construção:
        #   · a cor é a FAIXA da legenda (`_bucket_cor` sobre FAIXAS_KML),
        #     não um gradiente — a mesma régua do balão, da legenda na
        #     tela e do slide de Metodologia;
        #   · cada amostra é dona do caminho entre o ponto médio com a
        #     anterior e o ponto médio com a seguinte. Qualquer ponto da
        #     linha mostra a leitura REAL mais próxima — sem média, sem
        #     interpolação entre duas leituras;
        #   · a linha é opaca: com transparência, o vermelho do chão da
        #     cava tingia o verde e a cor vista deixava de ser a da faixa.
        #
        # E a linha não inventa caminho: `_trechos_continuos` a parte onde
        # houve buraco de medição (tempo ou salto de posição). Foi a reta
        # atravessando a cava que tinha tirado a linha do padrão antes.
        # UMA linha por caminho (`trilha_consolidada`): com uma por veículo,
        # 29 rádios nas mesmas estradas viravam uma faixa larga e salpicada.
        contornos, fitas, soltos = [], [], []
        do_rastro = _amostras_do_rastro(am, fixos)
        por_radio_rastro = {a["radio"] for a in do_rastro}
        # Na aba dos enlaces a linha é a do MeshMapper: o custo do caminho
        # (Trace Path). Nas outras, a própria grandeza.
        campo_l = ("custo_caminho" if campo_a == "enl_bons" else campo_a)
        linha_ligada = campo_a == "enl_bons"
        if campo_l != campo_a and not any(a.get(campo_l) is not None
                                          for a in do_rastro):
            campo_l = None
        pecas, cadeias, soltas = (
            trilha_consolidada(do_rastro, campo_l, FAIXAS_KML[campo_l])
            if campo_l else ([], [], []))
        un_l = (ESCALAS.get(campo_l) or {}).get("un", "") if campo_l else ""
        n_trechos = len(cadeias)
        for geo in cadeias:
            contornos.append(_placemark(None, None, "lcontorno", linha=geo))
        desc_cel = (f"<![CDATA[mediana das leituras de todos os rádios, em "
                    f"grupos de {TRILHA_CELULA_M:g} m ao longo da pista]]>")
        for cor, geo, vals in pecas:
            fitas.append(_placemark(_rotulo_trecho(vals, un_l, campo_l),
                                    desc_cel, f"l{cor}", linha=geo))
        for la, lo, v, n in soltas:
            # Célula sem ligação (leitura isolada entre dois buracos): não
            # há linha a traçar, mas a medida não pode sumir do mapa.
            cor = cor_da_leitura(v, campo_l)
            cores_ponto.add(cor)
            soltos.append(_placemark(
                _rotulo_trecho([v], un_l, campo_l),
                f"<![CDATA[{n} leitura{'s' if n != 1 else ''}]]>",
                f"q{cor}", ponto=(la, lo)))
        # O calor é a camada que abre ligada: é a que se lê de relance.
        calor = _calor_overlay(campo_a)
        if calor:
            dentro.append(calor)
        fx = _fixos_na_aba(campo_a, un_a)
        if fx:
            dentro.append(fx)
        if campo_a == "enl_bons":
            pinos = [_placemark(_nome_do_pino(a), _balao_enlaces(a, n),
                                pino_da_leitura(a), ponto=(a["lat"], a["lon"]))
                     for a, n in pinos_por_trecho(am) if pino_da_leitura(a)]
            if pinos:
                dentro.append(f"<Folder><name>Pinos ({len(pinos)})</name>"
                              f"<open>0</open>{''.join(pinos)}</Folder>")
        if contornos or soltos:
            # TODOS os contornos antes de TODAS as cores: com um contorno
            # por veículo intercalado, o de um cobria a cor do outro no
            # cruzamento de pistas. Desligada quando há calor: as duas
            # juntas se cobrem.
            dentro.append(
                f"<Folder><name>{CAMADA_DO_RASTRO}"
                f"{' — custo do caminho' if campo_l != campo_a else ''} "
                f"({len(por_radio_rastro)} "
                f"rádio{'s' if len(por_radio_rastro) != 1 else ''})</name>"
                f"<open>0</open><visibility>"
                f"{1 if (linha_ligada or not calor) else 0}</visibility>"
                f"{''.join(contornos)}{''.join(fitas)}"
                f"{''.join(soltos)}</Folder>")
        leg = _legenda_na_tela(campo_a)
        if leg and (contornos or soltos or calor):
            dentro.append(leg)

        # pontos de medição, com balão e linha do tempo
        usados, passo = _decimar(sorted(am, key=lambda x: x.get("ts") or 0),
                                 max_pontos)
        marcas = []
        for a in usados:
            v = a.get(campo_a)
            # A MESMA faixa da linha: ponto e linha discordarem de cor no
            # mesmo lugar tiraria a confiança nos dois.
            cor = cor_da_leitura(v, campo_a)
            cores_ponto.add(cor)
            quando_a = (datetime.fromtimestamp(a["ts"]).strftime(
                "%Y-%m-%dT%H:%M:%S") if a.get("ts") else None)
            rotulo = (_rotulo_trecho([v], un_a, campo_a)
                      if isinstance(v, (int, float))
                      else a.get("radio", ""))
            # Sem TimeStamp: com ele o Google Earth abre a barra de tempo e
            # esconde o que está fora do intervalo escolhido. A hora está
            # no balão.
            marcas.append(_placemark(rotulo, _balao_amostra(a), f"q{cor}",
                                     ponto=(a["lat"], a["lon"])))
        nota = (f" — 1 a cada {passo} amostras" if passo > 1 else "")
        # Desligada: com milhares de leituras, os pontos cobrem a fita.
        # Ligada, dá o balão de cada leitura com todas as grandezas. Na aba
        # dos enlaces os pinos já são as leituras, cada uma com balão.
        if campo_a != "enl_bons":
            dentro.append(
                f"<Folder><name>Medições ({len(marcas)})</name><open>0</open>"
                f"<visibility>0</visibility>"
                f"<description><![CDATA[Clique num ponto para ver todas as "
                f"grandezas medidas ali.{nota}]]></description>"
                f"{''.join(marcas)}</Folder>")

        # fora do requisito — desligada: ligada, cobre os pontos bons
        if lim_a is not None:
            ruins = [a for a in am if a.get(campo_a) is not None
                     and not atende(a[campo_a], op_a, lim_a)]
            ruins, _ = _decimar(ruins, max_pontos // 2)
            if ruins:
                itens = [_placemark(_rotulo_trecho([a[campo_a]], un_a, campo_a),
                                    _balao_amostra(a), "pFora",
                                    ponto=(a["lat"], a["lon"]))
                         for a in ruins]
                dentro.append(
                    f"<Folder><name>{'Fora da referência' if campo_a == 'custo_caminho' else 'Fora do requisito'} — "
                    f"{_esc(_OP_TXT.get(op_a, op_a))} "
                    f"{(f'{lim_a:g} {un_a}').rstrip()} ({len(ruins)})</name><open>0</open>"
                    f"<visibility>0</visibility>{''.join(itens)}</Folder>")

        # A pasta ZONAS-PROBLEMA saiu a pedido: ela sugeria coordenada
        # para avaliar radio, que e decisao de projeto de RF e nao
        # conclusao de medicao. `zonas_problema()` segue no modulo e no
        # relatorio semanal; o que mudou foi o laudo de survey parar de
        # apresentar sugestao ao lado do que foi medido.

        req = (f" · requisito {op_a} {lim_a:g} {un_a}" if lim_a is not None
               else "")
        return (f"<Folder><name>{_esc(rot_a)}</name><open>0</open>"
                f"<visibility>{1 if visivel else 0}</visibility>"
                f"<description><![CDATA[Cor por {_esc(rot_a)}{_esc(req)}"
                f"]]></description>{''.join(dentro)}</Folder>")

    # Uma grandeza por vez: no painel do Google Earth as abas viram botão
    # de rádio, e ligar uma desliga a outra.
    abas = [_aba(cp, visivel=(i == 0)) for i, cp in enumerate(campos)]
    pastas.append(f"<Folder><name>Grandeza</name><open>1</open>"
                  f"<styleUrl>#radio</styleUrl>{''.join(abas)}</Folder>")

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

    # Agora que se sabe QUAIS cores de ponto apareceram, emite so essas.
    # A fita usa os estilos `l<cor>`, um por faixa da legenda, emitidos no
    # começo; o antigo `r<cor>` era do gradiente de 40 tons, que saiu.
    for cor in sorted(cores_ponto):
        estilos.append(
            f'<Style id="q{cor}"><IconStyle><color>{_kml_cor(cor)}</color>'
            f'<scale>0.5</scale><Icon><href>{ico_pt}</href></Icon>'
            f'<hotSpot x="0.5" y="0.5" xunits="fraction" yunits="fraction"/>'
            f'</IconStyle><LabelStyle><scale>0</scale></LabelStyle>'
            f'{_balao_com_selo(cor)}</Style>')
    # Quadrado da ERB/ERM: nome só ao passar o mouse, para não cobrir o mapa.
    for cor in sorted(cores_fixos):
        for sufixo, esc_i, esc_l in (("n", 0.85, 0), ("h", 1.2, 0.9)):
            estilos.append(
                f'<Style id="fx{cor}{sufixo}"><IconStyle><color>{_kml_cor(cor)}'
                f'</color><scale>{esc_i}</scale><Icon><href>{ico_fx}</href>'
                f'</Icon><hotSpot x="0.5" y="0.5" xunits="fraction" '
                f'yunits="fraction"/></IconStyle><LabelStyle><scale>{esc_l}'
                f'</scale></LabelStyle>{_balao_com_selo(cor)}</Style>')
        estilos.append(
            f'<StyleMap id="fx{cor}"><Pair><key>normal</key><styleUrl>#fx{cor}n'
            f'</styleUrl></Pair><Pair><key>highlight</key><styleUrl>#fx{cor}h'
            f'</styleUrl></Pair></StyleMap>')
    if comprimir:
        extras.extend(icones_kmz().items())
    if "enl_bons" in campos:
        for nome_p, (forma, _t, claro, _e) in PINOS_RAJANT.items():
            href = (f"files/{nome_p}.png" if comprimir else ico_pt)
            ponta = ('<hotSpot x="0.5" y="0" xunits="fraction" yunits="fraction"/>'
                     if forma == "gota" else
                     '<hotSpot x="0.5" y="0.5" xunits="fraction" yunits="fraction"/>')
            cor_i = "" if comprimir else f"<color>{_kml_cor(claro)}</color>"
            estilos.append(
                f'<Style id="{nome_p}"><IconStyle>{cor_i}<scale>0.8</scale>'
                f'<Icon><href>{href}</href></Icon>{ponta}</IconStyle>'
                f'<LabelStyle><scale>0</scale></LabelStyle>'
                f'{_balao_com_selo(claro)}</Style>')
        if comprimir:
            extras.extend(pinos_png().items())

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
    # Os MESMOS rótulos da legenda na tela e do slide de Metodologia.
    legenda = [f"<tr><td bgcolor='#{cor}' width='26'>&nbsp;</td>"
               f"<td>{_esc(rng)} {unid}</td><td><i>{_esc(cls)}</i></td></tr>"
               for rng, cor, cls in _faixas_rotuladas(campo)]
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

    lats = sorted(a["lat"] for a in am); lons = sorted(a["lon"] for a in am)
    olhar = ""
    if lats:
        ext = max(_dist_m(lats[0], lons[0], lats[-1], lons[0]),
                  _dist_m(lats[0], lons[0], lats[0], lons[-1]), 300.0)
        olhar = (f"<LookAt><longitude>{(lons[0] + lons[-1]) / 2:.6f}</longitude>"
                 f"<latitude>{(lats[0] + lats[-1]) / 2:.6f}</latitude>"
                 f"<altitude>0</altitude><heading>0</heading><tilt>0</tilt>"
                 f"<range>{ext * 1.35:.0f}</range>"
                 f"<altitudeMode>relativeToGround</altitudeMode></LookAt>")
    doc = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
           f'<name>{rot_doc} — {quando:%d/%m/%Y}</name><open>1</open>'
           f'{olhar}<description>{desc}</description>'
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


def limpar_enlace(sig, snr, custo):
    """Limpa um trio (sinal, SNR, custo) — do arquivo OU do rádio.

    O arquivo usa 0 como "ainda nao medi este enlace", e 0 dBm nao existe
    num radio de malha — seria uma potencia recebida igual a 1 mW, colada
    na antena. Deixar passar nao e detalhe: `cobertura_disponivel()`
    escolhe o vizinho por `max(sinal)`, e o 0 ganha de todos. Medido no
    arquivo real do CA-1006: tres pontos do trajeto saiam pintados de
    verde maximo com "87 dB disponiveis e nao usados", em cima de um 0 do
    ERB-11 L2. Numero inventado entrando no laudo como prova.

    Vale para o enlace servidor E para cada vizinho da lista — o furo
    estava justamente na lista, que nao passava por aqui.

    Vale tambem para a coleta AO VIVO, e o furo se repetiu ali: `_i()` do
    parser devolve 0 quando o campo nao existe, entao um vizinho sem
    `signal` chegava como 0 dBm e GANHAVA a eleicao do melhor sinal. O
    laudo saiu com "RSSI mediana 0,16 dBm, 100% dentro do requisito" —
    aprovado por uma medida que nunca existiu. Por isso a regra mora aqui,
    num lugar so, e os dois caminhos passam por ela.
    """
    if sig == 0: sig = None
    if snr == 0 and sig is None: snr = None
    if custo is not None and custo >= CUSTO_SEM_ROTA:
        custo = None                          # INT_MAX = sem rota
    ruido = (sig - snr) if (sig is not None and snr is not None) else None
    return sig, snr, custo, ruido


def _mm_amostra(nome_movel, ts, lat, lon, alt, path, n_peers):
    """Uma amostra no formato interno, a partir do enlace servidor."""
    # `custo` é o do ENLACE — o primeiro salto, `hopcost` no data.json e
    # "Hop Cost" no CSV —, como na coleta ao vivo. Antes vinha o `cost`,
    # que é o do CAMINHO inteiro: a mesma coluna "Custo" do Excel dizia
    # coisas diferentes conforme a origem. O caminho vai em custo_caminho.
    sig, snr, custo, ruido = limpar_enlace(
        _mm_num(path.get("signal")),
        _mm_num(path.get("rssi")),            # sim: 'rssi' do arquivo é SNR
        _mm_num(path.get("hopcost")))
    freq = _mm_num(path.get("freq")) or _mm_num(path.get("frequency"))
    # Custo do CAMINHO até o destino do trace — o "Trace Path Cost" do
    # balão do MeshMapper, a métrica pela qual ele pinta o trajeto. É o
    # mesmo número de `custo`, mas `custo` na coleta ao vivo é o custo do
    # ENLACE com o melhor vizinho: mesmo nome, grandezas diferentes. Por
    # isso campo próprio. 2147483647 (sem rota) fica: é a pior medição
    # possível, não ausência dela. 0 não é custo de rota nenhuma — o
    # `pathCost` do ponto vem sempre 0 nos arquivos — e vira None.
    custo_cam = _mm_num(path.get("cost")) or None
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
        "custo_caminho": custo_cam,
        "taxa": _mm_num(path.get("rate")),    # MCS em Mbps, apesar do rótulo
        "peers": n_peers,
        "banda": _norm_banda(freq) if freq else None,
        "canal": _mm_num(path.get("channel")),
        "servidor": (path.get("name") or path.get("mac") or "").strip() or None,
        "fonte": "meshmapper",
    }


# A régua do MeshMapper da Rajant, a mesma do arquivo da mina (data.json,
# `configuration`, BC|Commander 11.29.1). Cada arquivo traz a sua; estes
# são os valores quando não há arquivo (coleta ao vivo).
LIMIARES_RAJANT = {"goodCost": 10000, "greatCost": 5000,
                   "goodRSSI": 20, "greatRSSI": 30,
                   "goodPath": 20000, "greatPath": 10000}


def classe_enlace(custo, snr, lim=None):
    """'otimo' | 'bom' | 'ruim', pela regra impressa no balão do MeshMapper:
    ótimo com custo E SNR na faixa ótima; bom com os dois pelo menos na
    boa; ruim com qualquer um na ruim. Custo até o limite e SNR a partir
    dele — conferido no arquivo de 28/09 (ERM-24 PTP a 9198 e SNR 36 conta
    como bom; a 11355 não)."""
    l = dict(LIMIARES_RAJANT, **{k: v for k, v in (lim or {}).items()
                                 if v is not None})
    if custo is None or snr is None:
        return "ruim"
    if custo <= l["greatCost"] and snr >= l["greatRSSI"]:
        return "otimo"
    if custo <= l["goodCost"] and snr >= l["goodRSSI"]:
        return "bom"
    return "ruim"


def contar_enlaces(vizinhos, lim=None):
    """Os enlaces do ponto pela régua da Rajant — o número do pino do
    MeshMapper: 0 vermelho, 1 laranja, 2 amarelo, 3+ verde (bons, ótimos
    incluídos); estrela para os ótimos.

    `vizinhos`: [{nome, custo, snr, ...}] de TODAS as WLANs do rádio, como
    no MeshMapper (a contagem do pino soma wlan0 e wlan1).
    Devolve {enl_bons, enl_otimos, enl_total, enl_lista}."""
    bons, otimos, lista = 0, 0, []
    for v in vizinhos:
        c = classe_enlace(v.get("custo"), v.get("snr"), lim)
        if c != "ruim":
            bons += 1
            lista.append((v.get("nome") or v.get("ip") or "?", v.get("custo"),
                          v.get("snr"), c))
        if c == "otimo":
            otimos += 1
    lista.sort(key=lambda t: (t[3] != "otimo", t[1] if t[1] is not None else 1e12))
    return {"enl_bons": bons, "enl_otimos": otimos,
            "enl_total": len(vizinhos), "enl_lista": lista}


def anexar_enlaces(amostras, peers, lim=None):
    """Conta os enlaces das amostras que ainda não têm (`enl_total`),
    casando vizinho e amostra por `ponto` — a posição da amostra na lista,
    contando de 1, como em `cobertura_disponivel`."""
    por_ponto = {}
    for p in peers or []:
        por_ponto.setdefault(p.get("ponto"), []).append(p)
    for i, a in enumerate(amostras, start=1):
        if a.get("enl_total") is None:
            a.update(contar_enlaces(por_ponto.get(i, []), lim))
    return amostras


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
        sv, am, pr = _mm_dos_csv(p)
        anexar_enlaces(am, pr, sv.get("limiares_mm"))
        return sv, am, pr
    raise RuntimeError(f"{p.name}: esperado .kmz, .json ou .csv do MeshMapper")


def _mm_do_json(d, rotulo):
    pontos = d.get("points") or []
    if not pontos:
        raise RuntimeError("captura do MeshMapper sem pontos")
    cfg_mm = d.get("configuration") or {}
    meta = cfg_mm.get("crumbMeta") or {}
    movel = (meta.get("name") or meta.get("serialStr") or "movel").strip()
    lim_mm = {k: cfg_mm.get(k) for k in LIMIARES_RAJANT}

    amostras, peers = [], []
    for i, pt in enumerate(pontos, start=1):
        ts = (pt.get("unixTimeStamp") or 0) / 1000.0 or None
        lat, lon = pt.get("gpsLat"), pt.get("gpsLong")
        if lat is None or lon is None:
            continue                       # sem posição não vai para o mapa
        path = ((pt.get("traceInfo") or {}).get("path")) or {}
        amostras.append(_mm_amostra(movel, ts, lat, lon, pt.get("gpsAlt"),
                                    path, pt.get("numActivePeers")))
        do_ponto = []
        for wlan, lista in (pt.get("wlanPeers") or {}).items():
            for q in (lista or []):
                sig, snr, custo, ruido = limpar_enlace(
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
                do_ponto.append(peers[-1])
        amostras[-1].update(contar_enlaces(do_ponto, lim_mm))

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
        # Até onde o custo do caminho foi medido (traceInfo.host). O mais
        # frequente: o MeshMapper traça sempre para o mesmo host.
        "destino_trace": _mais_comum(
            ((pt.get("traceInfo") or {}).get("host") for pt in pontos)),
    }
    return sv, amostras, peers


def _mais_comum(valores):
    """O valor não vazio mais frequente, ou None."""
    from collections import Counter
    c = Counter(v for v in valores if v)
    return c.most_common(1)[0][0] if c else None


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
             "cost": r.get("Cost"), "hopcost": r.get("Hop Cost"),
             "rate": r.get("Rate (Kb/s)"),
             "channel": r.get("Channel"), "freq": r.get("Frequency"),
             "name": r.get("IP/MAC")}, None))

    peers = []
    if pinfo.exists():
        for r in _csv.DictReader(open(pinfo, encoding="utf-8", errors="replace")):
            sig, snr, custo, ruido = limpar_enlace(
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
    amostras, peers, sv["descartadas_parado"] = descartar_parados(amostras, peers)
    sv["cobertura"] = cobertura_disponivel(amostras, peers)
    fechar = con is None
    con = con or banco()
    try:
        radios = sorted({a["radio"] for a in amostras})
        sid = survey_criar(con, nome or sv["nome"], sv["inicio"],
                           sv.get("intervalo_s"), radios)
        survey_destino_trace(con, sid, sv.get("destino_trace"))
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
        am_i, pr_i, desc_i = descartar_parados(am_i, pr_i)
        # A cobertura e por ARQUIVO: os numeros de ponto se repetem entre
        # capturas, e cruza-los ligaria o vizinho de uma ao trajeto de
        # outra.
        cob_i = cobertura_disponivel(am_i, pr_i)
        for a in am_i:
            a["arquivo"] = Path(c).name
        amostras.extend(am_i); peers.extend(pr_i)
        origens.append({"arquivo": Path(c).name, "ok": True,
                        "movel": sv_i.get("movel"), "cobertura": cob_i,
                        "descartadas_parado": desc_i,
                        "amostras": len(am_i), "peers": len(pr_i),
                        "inicio": sv_i.get("inicio"), "fim": sv_i.get("fim"),
                        "versao_bcc": sv_i.get("versao_bcc"),
                        "limiares_mm": sv_i.get("limiares_mm") or {},
                        "destino_trace": sv_i.get("destino_trace")})
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
        "destino_trace": next((o.get("destino_trace") for o in ok
                               if o.get("destino_trace")), None),
        "cobertura": {
            "com_infra": sum((o.get("cobertura") or {}).get("com_infra", 0) for o in ok),
            "sem_infra": sum((o.get("cobertura") or {}).get("sem_infra", 0) for o in ok),
            "padrao": PADRAO_INFRA,
        },
        "origens": origens,
        "descartadas_parado": sum(o.get("descartadas_parado", 0) for o in ok),
    }

    fechar = con is None
    con = con or banco()
    try:
        radios = sorted({a["radio"] for a in amostras})
        sid = survey_criar(con, sv["nome"], sv["inicio"], None, radios)
        survey_destino_trace(con, sid, sv.get("destino_trace"))
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
    com_vizinhanca = _cfg_bool(cfg, "relatorio", "vizinhanca", False)
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
        # A coluna Mediana lia `r.get("mediana")`, chave que o resumo não
        # tinha: saía vazia em toda linha. O operador vem de `limite_de`,
        # o mesmo que decide o "Dentro".
        op, lim_e = (limite_de(campo) or (None, None))[:2]
        lin = _td(ws, lin, [e.get("rot", campo), e.get("un", ""),
                            r.get("n"), fmt_leitura(r.get("mediana"), campo, False),
                            fmt_leitura(r.get("pior5"), campo, False),
                            f"{_OP_TXT.get(op, op)} {lim_e:g}" if op else "",
                            r.get("pct_ok")],
                  zebra=(lin % 2 == 0))

    # ── 3. Amostras ──
    ws = wb.create_sheet("Amostras")
    _cab(ws, "Trajeto medido", f"{len(amostras)} pontos", 14)
    lin = 4
    # Duas colunas de RSSI, e a diferenca entre elas e o diagnostico —
    # entao cada uma diz de qual enlace fala. "RSSI (dBm)" sozinho ao
    # lado de "Cobertura (dBm)" nao dizia, e o laudo (mapa e slides)
    # reporta a SEGUNDA.
    _th(ws, lin, ["#", "Hora (UTC)", "Latitude", "Longitude", "Alt (m)",
                  "Banda", "Canal", "RSSI do enlace (dBm)", "SNR (dB)",
                  "Ruído (dBm)", "Custo do enlace", "Custo do caminho",
                  "Taxa (Mbps)", "Vizinhos", "Enlaces bons", "Enlaces ótimos",
                  "Servidor", "RSSI da melhor ERB/ERM (dBm)",
                  "Melhor ERB/ERM", "Δ não usado (dB)"],
        [6, 19, 12, 12, 9, 10, 8, 19, 10, 12, 14, 16, 12, 10, 12, 13, 22, 24,
         22, 15])
    lin += 1
    for i, a in enumerate(sorted(amostras, key=lambda x: x.get("ts") or 0), 1):
        lin = _td(ws, lin, [i, dt(a.get("ts")), a.get("lat"), a.get("lon"),
                            a.get("alt"), a.get("banda"), a.get("canal"),
                            a.get("sinal"), a.get("snr"), a.get("ruido"),
                            a.get("custo"),
                            fmt_leitura(a.get("custo_caminho"),
                                        "custo_caminho", False),
                            a.get("taxa"), a.get("peers"),
                            a.get("enl_bons"), a.get("enl_otimos"),
                            a.get("servidor"),
                            a.get("sinal_cob"), a.get("servidor_cob"),
                            a.get("delta_cob")], estilo=False)
    ws.freeze_panes = "A5"

    # ── 4. Censo de vizinhos (opcional) ──
    # DESLIGADO por padrão. O laudo que se usa é o de trajeto — RSSI, SNR
    # e ruído por banda, coloridos pela melhor ERB/ERM do ponto. As abas
    # de vizinhança respondem outra pergunta e, misturadas com aquelas,
    # confundem mais do que informam. Ligue com
    #
    #   [relatorio]
    #   vizinhanca = true
    #
    # quando a pergunta for "quem fala com este rádio", que é o caso de
    # uma captura feita parada.
    censo = censo_vizinhos(peers, len(amostras)) if com_vizinhanca else []
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
    pegada = amostras_por_repetidora(amostras, peers) if com_vizinhanca else {}
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

    # ── 5. Vizinhos, leitura a leitura (opcional) ──
    # Mesma chave: e o dado BRUTO por tras da cobertura disponivel. Util
    # para auditar um numero, ruidoso no laudo do dia a dia.
    if not com_vizinhanca:
        buf = _io.BytesIO(); wb.save(buf)
        quando = (datetime.utcfromtimestamp(sv["inicio"]) if sv.get("inicio")
                  else datetime.now())
        return buf.getvalue(), (f"Survey_{_slug_arquivo(movel)}_"
                                f"{quando:%Y%m%d_%H%M}.xlsx")
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


# Parado = a menos disto da última leitura guardada do mesmo rádio E a
# menos de VEL_PARADO_KMH. O erro do GPS de um equipamento parado passa de
# 5 m; a velocidade separa o parado do caminhão que anda devagar, com
# leituras a 5 m uma da outra — esse não está no mesmo ponto.
RAIO_MESMO_PONTO_M = 10.0
VEL_PARADO_KMH = 3.6          # 1 m/s


def parado_no_mesmo_ponto(ultimo, lat, lon, vel_kmh, raio_m=RAIO_MESMO_PONTO_M):
    """A leitura é do mesmo ponto da última guardada (`ultimo` = (lat, lon))?
    Sem velocidade, decide só a distância."""
    if ultimo is None or lat is None or lon is None:
        return False
    if vel_kmh is not None and vel_kmh >= VEL_PARADO_KMH:
        return False
    return _dist_m(ultimo[0], ultimo[1], lat, lon) < raio_m


def descartar_parados(amostras, peers=None, raio_m=RAIO_MESMO_PONTO_M):
    """Uma leitura por parada: tira a leitura do rádio parado a menos de
    `raio_m` da última leitura GUARDADA dele.

    Equipamento parado — carregadeira no banco, caminhão na fila, ERM no
    poste — dava dezenas de leituras no mesmo lugar, e cada uma pesava
    nos percentuais, nas medianas e no mapa como um trecho de estrada
    medido. Compara-se com a última guardada, não com a anterior: o
    parado que se arrasta 2 m por leitura não escapa, e o caminhão que
    volta ao mesmo lugar mais tarde conta de novo.

    `peers` (vizinhos ligados à amostra por `ponto`, a posição dela na
    lista, contando de 1) sai junto: os da leitura descartada saem, e os
    outros ganham o `ponto` novo.

    Devolve (amostras, peers, n_descartadas).
    """
    guardadas, novo_ponto, ult, ant = [], {}, {}, {}
    for i, a in enumerate(amostras, start=1):
        lat, lon = a.get("lat"), a.get("lon")
        r = a.get("radio")
        if lat is not None and lon is not None:
            # `vel` é km/h (gpsSpeedKph). O MeshMapper não traz: sai do
            # deslocamento desde a leitura anterior do mesmo rádio.
            vel = a.get("vel")
            p = ant.get(r)
            if vel is None and p is not None and a.get("ts") and p[2] \
                    and a["ts"] > p[2]:
                vel = _dist_m(p[0], p[1], lat, lon) / (a["ts"] - p[2]) * 3.6
            ant[r] = (lat, lon, a.get("ts"))
            if parado_no_mesmo_ponto(ult.get(r), lat, lon, vel, raio_m):
                continue
            ult[r] = (lat, lon)
        guardadas.append(a)
        novo_ponto[i] = len(guardadas)
    if peers is None:
        return guardadas, None, len(amostras) - len(guardadas)
    pr = []
    for p in peers:
        j = novo_ponto.get(p.get("ponto"))
        if j is not None:
            pr.append(dict(p, ponto=j))
    return guardadas, pr, len(amostras) - len(guardadas)


def cobertura_disponivel(amostras, peers, padrao=None):
    """Anexa a cada amostra o MELHOR vizinho de infraestrutura do ponto.

    Duas perguntas diferentes vivem no mesmo arquivo:

      "existe sinal servivel aqui?"   -> o melhor vizinho de infra
      "a aplicacao funcionou aqui?"   -> o enlace que o InstaMesh usou

    Medido no arquivo do cliente, as duas divergem muito: o enlace
    entregue tinha mediana -88 dBm e 81% fora do requisito, enquanto o
    melhor vizinho de infra dava -66 dBm e 100% dentro. Reportar so a
    primeira leva a conclusao errada de que falta radio na area.
    (Aquela apuracao e anterior ao recorte por banda abaixo e misturava
    as duas malhas; o numero da campanha precisa ser reapurado.)

    A eleicao acontece DENTRO DA BANDA da amostra. No arquivo do cliente
    o mesmo ponto enxerga ERM-28 a -71 dBm em 2,4 GHz e ERM-09 a -86 dBm
    em 5,8 GHz; a amostra e de 5,8 GHz (e a banda do enlace que atendeu)
    e sem o recorte ela levava o -71 para o mapa de 5,8 GHz. Os 15 dB de
    diferenca sao justamente o que separa aprovado de reprovado, e a
    razao de 2,4 e 5,8 sairem em arquivos separados: nao adianta separar
    os arquivos se a leitura de uma banda entra no mapa da outra.

    Ponto sem nenhum vizinho na banda da amostra fica SEM `sinal_cob` —
    buraco no mapa, que se le como "nao medi", em vez de pintado com a
    medida da outra banda.

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

    n_infra = n_fallback = n_sem_banda = 0
    for i, a in enumerate(amostras, start=1):
        lista = por_ponto.get(i) or []
        banda_a = _norm_banda(a.get("banda")) if a.get("banda") else None
        if banda_a:
            mesma = [(eh, p) for eh, p in lista
                     if _norm_banda(p.get("banda")) == banda_a]
            if lista and not mesma:
                n_sem_banda += 1
            lista = mesma
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
            "sem_vizinho_na_banda": n_sem_banda,
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


def _passo_tipico(amostras):
    """Mediana da distância entre amostras CONSECUTIVAS do mesmo rádio.

    É o número que descreve a resolução de um survey — "intervalo
    efetivo em segundos" não serve quando vários equipamentos medem ao
    mesmo tempo: os carimbos se intercalam e a mediana dos intervalos
    vira o tempo entre leituras de rádios DIFERENTES, que não significa
    nada. O que o mapa precisa é densidade no espaço.
    """
    por = {}
    for a in amostras:
        if a.get("lat") is None or a.get("lon") is None: continue
        por.setdefault(a.get("radio"), []).append(a)
    ds = []
    for pts in por.values():
        pts = sorted(pts, key=lambda x: x.get("ts") or 0)
        for p, q in zip(pts, pts[1:]):
            ds.append(_dist_m(p["lat"], p["lon"], q["lat"], q["lon"]))
    if not ds: return None
    ds.sort()
    return round(ds[len(ds) // 2], 1)


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
                      ("intervalo_efetivo_s", "REAL"),
                      # Destino do trace do MeshMapper: o custo do caminho
                      # só significa algo dizendo ATÉ ONDE.
                      ("destino_trace", "TEXT")):
        if col not in tem:
            con.execute(f"ALTER TABLE survey ADD COLUMN {col} {tipo}")
    tem_am = {r["name"] for r in con.execute("PRAGMA table_info(amostra)")}
    for col, tipo in (("fonte", "TEXT"), ("servidor", "TEXT"),
                      ("interf", "REAL"), ("canal", "INTEGER"),
                      # Cobertura disponivel no ponto: o melhor vizinho de
                      # infraestrutura, que e outra grandeza do enlace que
                      # atendeu. Ver cobertura_disponivel().
                      ("sinal_cob", "REAL"), ("snr_cob", "REAL"),
                      ("servidor_cob", "TEXT"), ("delta_cob", "REAL"),
                      ("custo_caminho", "REAL"),
                      # Enlaces do ponto pela régua da Rajant (o número do
                      # pino do MeshMapper). Ver contar_enlaces().
                      ("enl_bons", "INTEGER"), ("enl_otimos", "INTEGER"),
                      ("enl_total", "INTEGER")):
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


def survey_destino_trace(con, sid, host):
    """Registra até onde o MeshMapper tracejou o caminho (traceInfo.host)."""
    if host:
        con.execute("UPDATE survey SET destino_trace=? WHERE id=?", (host, sid))
        con.commit()


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
            "sinal_cob","snr_cob","servidor_cob","delta_cob",
            "custo_caminho", "enl_bons", "enl_otimos", "enl_total"]
    con.executemany(
        f"INSERT INTO amostra (survey_id,{','.join(cols)}) "
        f"VALUES (?,{','.join('?'*len(cols))})",
        [tuple([sid] + [l.get(c) for c in cols]) for l in linhas])
    con.commit()
    return len(linhas)


def survey_obter(con, sid):
    r = con.execute("SELECT * FROM survey WHERE id=?", (sid,)).fetchone()
    return dict(r) if r else None


def survey_amostras(con, sid, banda=None):
    q = "SELECT * FROM amostra WHERE survey_id=?"
    p = [sid]
    if banda:
        q += " AND banda=?"; p.append(banda)
    return [dict(r) for r in con.execute(q + " ORDER BY ts", p)]


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
    # Toda grandeza do laudo que tem régua e foi medida, não só as do
    # contrato: RSSI da melhor ERB/ERM, custo do caminho (referência
    # Rajant), ruído (derivado) e interferência. Sem isto a aba Por
    # Grandeza escrevia "não medido, 0 amostras" para o ruído que estava
    # no mapa ao lado.
    for extra in CAMPOS_KMZ:
        if extra not in alvos and limite_de(extra) and any(
                a.get(extra) is not None for a in amostras):
            alvos[extra] = limite_de(extra)
    for campo, (op, lim, un, rot) in alvos.items():
        vs = [a[campo] for a in amostras if a.get(campo) is not None]
        if not vs:
            out[campo] = None
            continue
        dentro = sum(1 for v in vs if atende(v, op, lim))
        vs_ord = sorted(vs)
        n = len(vs_ord)
        # O pior 5% depende do sentido: em RSSI e SNR são os MENORES
        # valores; em ruído, latência, perda e custo, os MAIORES. `p05`
        # (o 5º percentil de baixo) fica como estava para quem o usa pelo
        # nome, mas o "Pior 5%" dos relatórios lê `pior5`.
        alto = op in (">", ">=")
        out[campo] = {
            "n": n, "pct_ok": round(dentro / n * 100, 1),
            # `med` é a MÉDIA, e é assim que o semanal a rotula. Onde o
            # relatório escreve "Mediana", lê `mediana`: com um ponto sem
            # rota (custo 2147483647) a média do custo vira um número de
            # dez dígitos, e ninguém a chamaria de típica.
            "med": round(sum(vs) / n, 2),
            "mediana": round(vs_ord[n // 2] if n % 2
                             else (vs_ord[n // 2 - 1] + vs_ord[n // 2]) / 2, 2),
            "min": round(vs_ord[0], 2), "max": round(vs_ord[-1], 2),
            "p05": round(vs_ord[int(n * .05)], 2),
            "pior5": round(vs_ord[int(n * .05)] if alto
                           else vs_ord[min(n - 1, int(n * .95))], 2),
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
    # `op` explícito vale sobre o derivado do sentido: a referência da
    # Rajant para custo do caminho é "good" ATÉ 20000, inclusive — "<=",
    # não o "<" estrito que o sentido "baixo" daria.
    op = e.get("op") or (">" if e["melhor"] == "alto" else "<")
    return (op, float(e["req"]), e["un"], e["rot"])


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


def _milhar(n):
    """1234567 -> '1.234.567'. Só o separador de milhar: um replace cego
    na frase inteira levaria junto as vírgulas do texto e das coordenadas."""
    return f"{int(n):,}".replace(",", ".")


# ══════════════════════════════════════════════════════════════
# COMPARAÇÃO ENTRE SURVEYS — colunas "Semana anterior" e "Tendência"
# ══════════════════════════════════════════════════════════════


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
# UMA aba de RSSI, nao duas. "Cobertura disponivel" e "RSSI" mostravam a
# mesma grandeza em dBm — uma pelo melhor ERB/ERM do ponto, a outra pelo
# enlace que o InstaMesh escolheu. Lado a lado no mesmo arquivo viravam
# duas leituras do mesmo mapa, e quem abria nao sabia qual valia.
#
# Fica a do melhor ERB/ERM: e ela que responde "ha cobertura aqui". O
# enlace que atendeu continua gravado, coluna a coluna, na aba Amostras.
# A primeira é a que abre: a régua do MeshMapper, enlaces bons por ponto.
CAMPOS_KMZ = ["enl_bons", "sinal_cob", "snr", "ruido", "custo_caminho",
              "interf", "rtt", "perda"]


# Grandeza -> titulo do slide onde o print dela deve ser colado.
# Sem a entrada, o LEIA-ME cai no `campo` cru e manda o operador procurar
# um slide chamado "sinal_cob", que nao existe no deck.
SLIDE_DE_CAMPO = {
    "enl_bons": "Enlaces Bons (critério Rajant)",
    "sinal_cob": "Intensidade de Sinal (RSSI)",
    "sinal": "Intensidade de Sinal (RSSI)",
    "snr":   "Relação Sinal/Ruído (SNR)",
    "ruido": "Noise Floor",
    "rtt":   "Latência (RTT)",
    "perda": "Packet Loss",
    "interf": "Interferência de Canal",
    "custo_caminho": "Custo do Caminho (Trace Path Cost)",
}


# ══════════════════════════════════════════════════════════════
# FUNDO DE SATÉLITE
# ══════════════════════════════════════════════════════════════
# Mapa de survey sem imagem embaixo é um borrão colorido: ninguém
# reconhece a rampa, a praça nem o britador. A rede da mina tem internet,
# então o programa busca o recorte georreferenciado sozinho.
#
# Cacheado em disco por bbox+tamanho: gerar sete imagens de uma banda
# baixaria o mesmo recorte sete vezes.


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
    # -95 é DERIVADO, não contratual: é o ruído acima do qual um sinal no
    # mínimo Modular (-75 dBm) já não chega a SNR 20. O -85 de antes dava
    # SNR 10 nesse sinal — aprovava ruído que reprova o enlace.
    "ruido": {"rot": "Ruído","un": "dBm", "lo": -110,"hi": -80, "req": -95,
              "melhor": "baixo"},
    # Custo do caminho até o destino do trace, a métrica pela qual o
    # MeshMapper pinta o trajeto. Não é requisito Modular: a referência é
    # o goodPath da Rajant, e "good" inclui o próprio 20000 — daí o `op`.
    "custo_caminho": {"rot": "Custo do caminho", "un": "", "lo": 0,
                      "hi": 40000, "req": 20000, "melhor": "baixo",
                      "op": "<="},
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
    "sinal_cob": {"rot": "RSSI (melhor ERB/ERM do ponto)", "un": "dBm",
                  "lo": -90, "hi": -55, "req": -75, "melhor": "alto"},
    # Enlaces bons no ponto, pela régua da Rajant (contar_enlaces). A
    # referência é ter PELO MENOS UM: o pino "0", vermelho, no MeshMapper.
    # "> 0" e não ">= 1": nesta régua de inteiros a fronteira entre o
    # vermelho e o laranja é o 0, e com ">= 1" o próprio 1 mudaria de faixa.
    "enl_bons": {"rot": "Enlaces bons (Rajant)", "un": "", "lo": 0, "hi": 6,
                 "req": 0, "melhor": "alto", "op": ">"},
}

# Vermelho → verde, o mesmo racional do heatmap do cliente.
_CORES_RF = ["#C0392B","#E74C3C","#E67E22","#F1C40F","#9ACD32","#27AE60",
             "#1E8449","#145A32"]


def _cmap_rf(invertido=False):
    from matplotlib.colors import LinearSegmentedColormap
    cores = list(reversed(_CORES_RF)) if invertido else _CORES_RF
    return LinearSegmentedColormap.from_list("rf", cores, N=256)


# Ligado por dados de demonstração. Um mapa bem feito, no template do
# cliente, com números plausíveis, circula internamente e vira "o survey
# da mina" — a marca existe para isso não acontecer por engano.
MARCA_DEMO = {"ativa": False,
              "texto": "DADOS SINTÉTICOS — NÃO É MEDIÇÃO REAL"}


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


# ══════════════════════════════════════════════════════════════
# A MALHA CONHECIDA
# ══════════════════════════════════════════════════════════════
# Os rádios da mina, por IP e nome. Serve de PONTO DE PARTIDA para a
# descoberta: cada um é contatado direto, e a busca ainda segue os
# vizinhos de cada um — então rádio novo aparece mesmo sem estar aqui.
#
# Por que isto existe, e não é só cache: `ipv4Address` é OPCIONAL no
# State.Peer. Um rádio cujos vizinhos só trazem MAC não leva a busca a
# lugar nenhum, e foi o que aconteceu em campo — partindo só do
# 10.188.96.140, a descoberta achou UM equipamento numa malha de 150.
#
# Lista tirada dos caches reais da operação. Rádio que sai da frota
# simplesmente não responde e aparece com o motivo na tela; não some
# calado nem trava a busca.
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
    "10.188.97.154": "ERM-32 PTP CAM",
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
    "10.188.99.5": "CA-1005",
    "10.188.99.6": "CA-1006",
    "10.188.99.7": "CA-1007",
    "10.188.99.9": "CA-1009",
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
    "10.188.99.39": "CA-1039",
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
    "10.188.99.139": "RED004",
    "10.188.99.140": "EH-6003",
    "10.188.99.141": "RED065",
    "10.188.99.146": "EH-6550",
    "10.188.99.147": "EH-6551",
    "10.188.99.151": "PF-4601",
    "10.188.99.155": "PF-4701",
    "10.188.99.156": "PF-4702 FLANDERS",
    "10.188.99.157": "PF-4703",
    "10.188.99.158": "PF-4704",
    "10.188.99.159": "PF-4705",
    "10.188.99.167": "TT-3503 TECWISE",
    "10.188.99.169": "TT-3505 TECWISE",
    "10.188.99.170": "TT-3506",
    "10.188.99.171": "TT-3507",
    "10.188.99.172": "TT-3508 TECWISE",
    "10.188.99.173": "TT-3509 TECWISE",
    "10.188.99.174": "TT-3510 TECWISE",
    "10.188.99.175": "TT-3511 TECWISE",
    "10.188.99.176": "TT-3512 TECWISE",
    "10.188.99.189": "TT-3705",
    "10.188.99.190": "TT-3706",
    "10.188.99.192": "TT-3802",
    "10.188.99.194": "PA-5801",
    "10.188.99.195": "PA-5802",
    "10.188.99.199": "TN-4001",
    "10.188.99.200": "TN-4002",
    "10.188.99.210": "EH-6402",
    "10.188.99.212": "EH-6404",
    "10.188.99.213": "EH-6405",
    "10.188.99.216": "CP-7001",
    "10.188.99.217": None,
    "10.188.99.218": "CP-7003",
    "10.188.99.219": "CP-7004",
    "10.188.99.224": "PR-7202",
    "10.188.99.227": "MA-5101",
    "10.188.99.229": "MA-5103",
    "10.188.99.230": "MA-5104",
    "10.188.99.237": "EH-6552",
    "10.188.99.238": "EH-6553",
    "10.188.99.240": "MA-5201",
    "10.188.99.241": "MA-5202",
    "10.188.99.242": "MA-5203",
    "10.188.100.155": "PF-4701 FLANDERS",
    "10.188.100.157": "PF-4703 FLANDERS",
    "10.188.100.158": "PF-4704 FLANDERS",
    "10.188.105.170": "TT-3506 KOMATSU",
    "10.188.105.171": "TT-3507 KOMATSU",
    "10.188.111.46": "PCP-002 Sensing POD",
}


def ler_lista_de_ips(caminho):
    """Lê uma lista de rádios de arquivo. Devolve {ip: nome}.

    Aceita o `rajant_ips_cache.json` que o coletor grava — `{ip: {nome,
    ...}}` — e também texto solto com um IP por linha, com o nome opcional
    depois de vírgula, ponto e vírgula ou espaço.

    Existe porque descoberta pela malha pode não ter por onde andar: o
    `ipv4Address` é OPCIONAL no State.Peer, e um rádio cujos vizinhos só
    trazem MAC não leva a lugar nenhum. Quem já tem a lista dos próprios
    equipamentos não deve ficar refém disso.
    """
    p = Path(caminho)
    if not p.exists():
        raise RuntimeError(f"arquivo nao encontrado: {caminho}")
    txt = p.read_text(encoding="utf-8", errors="replace").strip()
    out = {}
    if txt.startswith("{"):
        d = json.loads(txt)
        for ip, v in d.items():
            nome = v.get("nome") if isinstance(v, dict) else (v or None)
            out[str(ip).strip()] = (str(nome).strip() if nome else str(ip))
    else:
        for linha in txt.splitlines():
            linha = linha.strip()
            if not linha or linha.startswith("#"):
                continue
            partes = re.split(r"[,;\t]| {2,}", linha, maxsplit=1)
            ip = partes[0].strip()
            if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip):
                continue
            out[ip] = (partes[1].strip() if len(partes) > 1 and partes[1].strip()
                       else ip)
    if not out:
        raise RuntimeError(f"{p.name}: nenhum IP reconhecido")
    log.info(f"[lista] {len(out)} equipamento(s) lidos de {p.name}")
    return out


def descobrir_malha(seeds, role="CO", senha="", porta=2300, timeout_s=6,
                    max_threads=24, seguir_peers=True, limite=600,
                    incluir_conhecidos=True, aviso=None):
    """Percorre a malha a partir dos seeds e devolve o inventário.

    `{ip: {"nome", "tem_gps", "serial", "modelo", "banda", "vizinhos"}}`.

    É a mesma mecânica da descoberta do coletor — parte dos seeds, lê o
    State de cada um e segue os `peers_ips` —, mas sem métrica, sem cache
    em disco e sem filtro de tag. Serve para montar a lista de seleção do
    app de coleta: ali o usuário quer ver TUDO que responde, e decidir
    ele mesmo o que entra.

    `tem_gps` é o que separa quem pode ir para o mapa de quem só pode ir
    para o censo: rádio sem módulo de GPS, ou com `gpsSwitch.enabled`
    falso, não sabe onde está. Melhor dizer isso na lista do que deixar o
    usuário marcar e descobrir no fim que não saiu ponto nenhum.

    `limite` é o teto de rádios visitados. Sem ele, uma malha com rota
    para outra rede sairia visitando endereço alheio até estourar o
    tempo do usuário.
    """
    Bc = exigir_rajant_api()
    role = conferir_papel(role)
    achados, vistos = {}, set()
    lk = threading.Lock()
    sem = threading.Semaphore(max(1, int(max_threads)))

    def _diz(t):
        log.info(f"[descoberta] {t}")
        if aviso:
            try: aviso(t)
            except Exception: pass

    def _um(ip):
        with sem:
            try:
                bc = Bc(host=ip, port=porta, role=role, password=senha)
                if not bc.reachable():
                    raise ConnectionRefusedError("nao alcancavel")
                autenticar(bc)
                # get_state() devolve o OBJETO State, não texto — quem
                # converte é o parse_state, com str(). Medir o tamanho
                # antes disso estourava com "object of type 'State' has
                # no len()", e a exceção matava a thread sem registrar
                # nada: 156 rádios sumiam calados. Converte-se uma vez
                # aqui e o resto trabalha sobre o texto.
                txt = str(bc.get_state())
                d = parse_state(txt)
            except Exception as e:
                with lk:
                    # Rádio que não responde ainda aparece COM O NOME que
                    # se conhece: uma tela com dez linhas de IP cru não
                    # diz quais equipamentos estão fora.
                    achados[ip] = {"ip": ip,
                                   "nome": REDE_CONHECIDA.get(ip) or ip,
                                   "tem_gps": False,
                                   "serial": None, "modelo": None,
                                   "vizinhos": 0, "erro": str(e)}
                return set()
            s = d["sistema"]
            n_radios = len(d.get("radios") or [])
            n_peers = sum(len(r.get("peers") or []) for r in d.get("radios") or [])
            ips = set(d.get("peers_ips") or ())
            # Vizinho SEM ipv4Address não serve para expandir: o campo é
            # opcional no State.Peer, e sem ele não há para onde ir. Sem
            # esta contagem, "achei só o seed" fica indistinguível de
            # "achei vizinhos mas nenhum tinha IP" — e são problemas
            # diferentes, com soluções diferentes.
            with lk:
                achados[ip] = {
                    "ip": ip,
                    "nome": s.get("nome") or ip,
                    # gps_fix já embute o gpsSwitch desligado.
                    "tem_gps": bool(s.get("gps_fix")),
                    "qualidade_gps": s.get("gps_qual"),
                    "serial": s.get("serial_num"),
                    "modelo": s.get("modelo_fab") or s.get("modelo"),
                    "vizinhos": n_peers,
                    "radios": n_radios,
                    "vizinhos_com_ip": len(ips),
                    "bytes_state": len(txt or ""),
                    "erro": None,
                }
            return ips

    fila = {str(x).strip() for x in (seeds or []) if str(x).strip()}
    if incluir_conhecidos:
        # Cada rádio conhecido é um ponto de partida PRÓPRIO, não só um
        # nome. Partindo de um seed só, a busca depende de os vizinhos
        # trazerem IP — e `ipv4Address` é opcional no State.Peer. Em campo
        # isso achou UM equipamento numa malha de 150.
        fila |= set(REDE_CONHECIDA)
    if not fila:
        raise RuntimeError("nenhum endereço de partida")
    _diz(f"partindo de {len(fila)} endereço(s)")
    while fila:
        lote = [ip for ip in fila if ip not in vistos][:max(0, limite - len(vistos))]
        if not lote:
            break
        vistos.update(lote)
        novos, ths, saidas = set(), [], {}

        def _worker(x):
            # Qualquer erro AQUI DENTRO tem de virar linha na tela, nunca
            # thread morta. Um `len()` mal colocado derrubou as 156
            # threads de uma vez e a tela ficou vazia sem explicar por
            # quê — o traceback saiu no console, que o usuário de janela
            # não vê.
            try:
                saidas[x] = _um(x)
            except BaseException as e:
                with lk:
                    achados[x] = {"ip": x,
                                  "nome": REDE_CONHECIDA.get(x) or x,
                                  "tem_gps": False, "serial": None,
                                  "modelo": None, "vizinhos": 0,
                                  "erro": f"falha interna: "
                                          f"{type(e).__name__}: {e}"}
                saidas[x] = set()
        for ip in lote:
            t = threading.Thread(target=_worker, args=(ip,), daemon=True)
            ths.append(t); t.start()
        for t in ths:
            t.join(timeout=timeout_s + 10)
        ok = sum(1 for ip in lote if not achados.get(ip, {}).get("erro"))
        _diz(f"{len(vistos)} visitados, {ok} responderam neste lote")
        # Sem vizinho COM IP a busca morre ali, e o usuário fica com um
        # rádio na tela achando que a rede tem um. Dizer o motivo é o que
        # separa "a malha é essa" de "o state veio sem o que preciso".
        # Só se relata quando o lote inteiro foi estéril — com 140 pontos
        # de partida, uma linha por rádio viraria ruído.
        mudos = [achados[ip] for ip in lote
                 if not achados.get(ip, {}).get("erro")
                 and not achados.get(ip, {}).get("vizinhos_com_ip")]
        if mudos and len(mudos) == ok and ok:
            v = mudos[0]
            _diz(f"  {len(mudos)} rádio(s) responderam sem NENHUM vizinho com "
                 f"endereço IP — não há por onde continuar a busca. "
                 f"Exemplo: {v.get('nome')} devolveu {v.get('bytes_state', 0)} "
                 f"bytes, {v.get('radios', 0)} rádio(s), "
                 f"{v.get('vizinhos', 0)} vizinho(s). "
                 f"A busca depende da lista conhecida e dos seeds.")
        if not seguir_peers:
            break
        for s_ in saidas.values():
            novos |= {p for p in s_ if p not in vistos}
        fila = novos

    com_gps = sum(1 for v in achados.values() if v["tem_gps"])
    _diz(f"fim: {len(achados)} rádios, "
         f"{sum(1 for v in achados.values() if not v['erro'])} responderam, "
         f"{com_gps} com GPS")
    # Os motivos, do mais frequente: "156 x o rádio recusou o login" diz
    # numa linha o que 156 linhas vermelhas não dizem.
    motivos = {}
    for v in achados.values():
        if v["erro"]:
            motivos[v["erro"]] = motivos.get(v["erro"], 0) + 1
    for m, n in sorted(motivos.items(), key=lambda kv: -kv[1])[:4]:
        _diz(f"  {n}x  {m}")
    return achados


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
        autenticar(bc)
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
            txt = estado_da_sessao(bc)
            if txt is False or txt is None:
                # get_state() da biblioteca devolve False em vez de levantar.
                # Contar isso como sucesso zerava as falhas, e o rádio mudo
                # nunca era descartado nem reconectado.
                raise RuntimeError("o rádio não devolveu o State")
            self.falhas = 0; self.consultas += 1
            return txt
        except Exception as e:
            # A sessão pode ter caído (rádio reiniciou, rota mudou). Joga
            # fora para a próxima tentativa reautenticar do zero.
            self._largar()
            self.falhas += 1
            self.ultimo_erro = str(e)
            if self.falhas >= self.falhas_max:
                self.desistiu = True
                log.warning(f"[survey] {self.ip} descartado apos "
                            f"{self.falhas} falhas seguidas: {e}")
            raise

    def trace(self, destino, espera_s=2.0):
        """Custo do caminho até `destino`, na sessão já aberta. Levanta
        TraceIndisponivel quando o rádio não entrega — quem chama decide."""
        if self.bc is None or not bc_direto_disponivel(self.bc):
            raise TraceIndisponivel("sessão sem acesso direto à BCAPI")
        try:
            return bc_trace(self.bc, destino, espera_s=espera_s)
        except TraceIndisponivel:
            raise
        except Exception:
            # Erro de transporte no meio do trace deixa a conexão num
            # estado desconhecido (meia mensagem pendente): refaz a sessão.
            self._largar()
            raise

    def _largar(self):
        """Fecha o socket e solta a sessão. A Breadcrumb não tem close():
        soltar só a referência deixava a sessão aberta no rádio até ele
        desistir dela — com o TRACE dela ainda ocupando a tarefa."""
        conn = getattr(self.bc, "connection", None)
        if conn is not None:
            try: conn.close()
            except Exception: pass
        self.bc = None

    def fechar(self):
        for m in ("close", "disconnect", "logout"):
            try:
                f = getattr(self.bc, m, None)
                if callable(f): f(); break
            except Exception: pass
        self._largar()


# Descoberto uma vez por processo: a assinatura de get_state varia entre
# versões da rajant-api, e chutar o nome do parâmetro quebraria a coleta
# inteira. None = ainda não inspecionado; "" = não aceita filtro.
_FILTRO_ESTADO = {"param": None}

# Só precisamos destes ramos: posição, RF e identificação. Puxar o state
# inteiro de 150 rádios a cada 10 s é desperdício de banda na malha.
#
# CONFERIDO na rajant-api 0.1.1 (o pacote do PyPI, lido): `get_state()`
# não recebe filtro nenhum, então a inspeção de `_get_state_filtrado` não
# acha parâmetro e cai no State INTEIRO. O filtro existe no protocolo
# (BCMessage.stateFilterPath, campo 601, repetido); a coleta o usa por
# `estado_da_sessao`. Formato CONFERIDO num rádio da mina (10.188.99.4,
# 26/09/2026): "gps" e "/gps" devolvem só os ramos pedidos (2307 bytes
# contra 9318 do State inteiro); "State.gps" e "state.gps" voltam vazios.
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


# ══════════════════════════════════════════════════════════════
# BCAPI DIRETO — leitura inteira, filtro de caminho e TRACE
# ══════════════════════════════════════════════════════════════
# A rajant-api 0.1.1 (lida do PyPI) tem três limites para a coleta:
#
#   · `get_message()` faz UM `recv(65535)` e interpreta o que veio. Pelo
#     TLS, um recv devolve no máximo um registro (~16 KB); o State de um
#     rádio com muitos vizinhos pode passar disso (o 10.188.99.4, com
#     poucos, já tinha 19 KB descomprimidos e 9 KB na rede), e a leitura
#     falha como "sem resposta" — sem erro nenhum que diga por quê.
#   · `get_state()` faz um ping de sistema antes de CADA consulta.
#   · não há filtro útil (get_state_filter anexa um caminho só) nem TRACE.
#
# Aqui se usa a mesma sessão autenticada da biblioteca (connection,
# build_message, send_message, seq_number) e o MESMO enquadramento do
# código dela: cabeçalho de 8 bytes `>ibbbb` (tamanho, compressão 2 =
# deflate cru, wbits -15) e o corpo. A diferença é ler até o tamanho que o
# cabeçalho declara.
import struct as _struct, zlib as _zlib

# Destino do trace: o mesmo host para onde o MeshMapper do cliente traçou
# (traceInfo.host em todos os pontos dos três arquivos recebidos). Com o
# mesmo destino, o custo do caminho da coleta e o do MeshMapper são a mesma
# grandeza.
DESTINO_TRACE_PADRAO = "10.188.96.11"


class TraceIndisponivel(RuntimeError):
    """O rádio não entregou o TRACE. A coleta segue sem ele."""


class TraceRecusado(TraceIndisponivel):
    """O rádio RECUSOU a tarefa (runTaskResult FAILURE) — papel sem
    permissão, firmware sem TRACE. Não adianta insistir naquele rádio."""


class TraceOcupado(TraceIndisponivel):
    """Já há um TRACE rodando no rádio. Passageiro: tenta-se de novo.

    O rádio tem UMA tarefa por vez (`State.task`), de qualquer sessão. Em
    campo (26/09/2026): "Cannot run new task TRACE from session[…]: task
    TRACE is already running". Ocupa a tarefa um TRACE nosso que passou do
    prazo, o de uma sessão que caiu no meio, ou o de outro cliente ligado
    no rádio (MeshMapper, BC|Commander). Na v17 isso contava como recusa
    e o rádio saía do trace na primeira vez."""


_RX_OCUPADO = re.compile(r"already running|busy|in progress", re.I)


def motivo_curto(texto):
    """O motivo sem o que muda a cada vez — a sessão e a idade dela —,
    para contar motivos iguais como iguais."""
    return re.sub(r"\s*from session\[[^\]]*\]", "", str(texto))


def descrever_tarefa(st):
    """A tarefa do rádio e quem a disparou, em texto, de um State inteiro.

    `State.task` é um TaskObject (Common.proto): comando, início, fim e
    TaskStatus com estado, id e `adminSessionID` — a sessão dona, que se
    acha em `State.adminSessions` (endereço, idade, cliente)."""
    linhas = []
    try:
        t = st.task
        s = t.status
        cmd = t.command
        linhas.append(
            f"tarefa: {type(cmd).TaskAction.Name(cmd.action)} {cmd.arguments}  "
            f"estado: {type(s).TaskState.Name(s.state) if s.HasField('state') else '?'}  "
            f"id {s.id or '?'}")
        linhas.append(f"início {t.startTime or '?'}  fim {t.stopTime or '—'}")
        dona = s.adminSessionID if s.HasField("adminSessionID") else None
        papeis = {v: k for k, v in (papeis_bcapi() or {}).items()}
        for a in st.adminSessions:
            marca = "  ← dona da tarefa" if dona is not None and a.sessionID == dona else ""
            linhas.append(
                f"sessão {a.sessionID:x}: {a.address}:{a.port}  {a.age} s  "
                f"{papeis.get(a.role, a.role)}  {a.userAgent or ''} "
                f"{a.remoteUsername or ''}".rstrip() + marca)
        if not len(st.adminSessions):
            linhas.append("sessões: (o State não trouxe adminSessions)")
    except Exception as e:
        linhas.append(f"(não deu para ler a tarefa: {type(e).__name__}: {e})")
    return "\n".join(linhas)


# Como a coleta está lendo, aprendido na primeira leitura boa e valendo
# para o processo inteiro: None = ainda não se sabe.
#   direto: a leitura enquadrada funciona (senão, volta a get_state()).
#   filtro: o rádio entende CAMINHOS_ESTADO em stateFilterPath. O formato
#           do caminho não está no .proto; se o State filtrado vier sem
#           nenhum dos ramos pedidos, o filtro sai e fica o State inteiro.
_BC_MODO = {"direto": None, "filtro": None, "falhas_direto": 0}


def estado_da_sessao(bc):
    """O State de uma leitura da coleta, do jeito mais leve que funcionar.

    Na ordem: filtrado e enquadrado; inteiro e enquadrado; e, se o modo
    direto nunca funcionar, o `get_state()` da biblioteca, como antes. No
    pior caso fica igual ao que era.
    """
    if not bc_direto_disponivel(bc) or _BC_MODO["direto"] is False:
        return _get_state_filtrado(bc)
    try:
        if _BC_MODO["filtro"] is not False:
            st = bc_state(bc, CAMINHOS_ESTADO)
            if (st.HasField("system") or st.HasField("gps")
                    or len(st.wireless)):
                _BC_MODO["direto"] = True
                if _BC_MODO["filtro"] is None:
                    _BC_MODO["filtro"] = True
                    log.info("[survey] leitura enxuta: só "
                             f"{', '.join(CAMINHOS_ESTADO)}")
                return st
            if _BC_MODO["filtro"] is None:
                _BC_MODO["filtro"] = False
                log.warning("[survey] o rádio não entendeu o filtro de "
                            "caminho; lendo o State inteiro")
        st = bc_state(bc, None)
        _BC_MODO["direto"] = True
        return st
    except (ConnectionError, OSError, TimeoutError):
        raise                          # transporte: não é culpa do modo
    except Exception:
        if _BC_MODO["direto"] is None:
            _BC_MODO["falhas_direto"] += 1
            if _BC_MODO["falhas_direto"] >= 3:
                _BC_MODO["direto"] = False
                log.warning("[survey] leitura direta não funcionou em 3 "
                            "tentativas; voltando ao get_state() da "
                            "biblioteca")
        raise


def bc_direto_disponivel(bc):
    """A sessão é da rajant-api de verdade (e não um objeto de teste)?"""
    return (getattr(bc, "connection", None) is not None
            and callable(getattr(bc, "build_message", None))
            and callable(getattr(bc, "send_message", None)))


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


def papel_bcapi(role):
    """O usuário (papel) do jeito que a rajant-api o procura: maiúsculo.

    A biblioteca faz `roles[role]` num dicionário que só tem VIEW, LOCAL,
    ADMIN, CO e JOIN. Com "co" em minúsculo dá KeyError — e o `except:`
    do authenticate() dela engole o erro e devolve só False. Na tela saía
    "autenticacao falhou" nos 156 rádios, com a senha certa.
    """
    return str(role or "").strip().upper()


def papeis_bcapi():
    """{papel: número} que a biblioteca instalada conhece, ou None."""
    try:
        from rajant_api import Message_pb2
        return dict(Message_pb2.Common__pb2.Role.items())
    except Exception:
        return None


def conferir_papel(role):
    """O papel normalizado; ValueError se a BCAPI não tiver esse usuário.
    Para dizer ANTES de bater em 156 rádios com um nome que não existe."""
    papel = papel_bcapi(role)
    papeis = papeis_bcapi()
    if papeis and papel not in papeis:
        raise ValueError(f"usuário '{role}' não existe na BCAPI — use "
                         + ", ".join(sorted(papeis, key=papeis.get)))
    return papel


AGENTE_BCAPI = "site_survey"

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
                _BC_MODO["login_reserva"] = _BC_MODO.get("login_reserva", 0) + 1
                if _BC_MODO["login_reserva"] == 1:
                    log.warning(f"[survey] login próprio falhou em {bc.host} "
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


def bc_pedir(bc, montar, aceita, max_msgs=6):
    """Envia um pedido e devolve a primeira resposta que `aceita`."""
    m = bc.build_message()
    montar(m)
    bc.send_message(m)
    for _ in range(max_msgs):
        r = bc_receber(bc)
        if aceita(r):
            return r
    raise RuntimeError("o rádio não devolveu a resposta esperada")


def bc_state(bc, caminhos=None):
    """State do rádio, opcionalmente só dos `caminhos` pedidos."""
    def montar(m):
        m.state.Clear()           # exatamente o pedido da biblioteca
        if caminhos:
            m.stateFilterPath.extend(list(caminhos))
    return bc_pedir(bc, montar, lambda r: r.HasField("state")).state


def _descomprimir_saida(dados, compressao=None):
    """Saída de tarefa. TaskOutput.CompressionType: GZIP=0, LZMA=1, LZ=3,
    NONE=4 — e GZIP é também o valor quando o campo não vem, então a
    assinatura dos bytes decide antes do enum."""
    import gzip, lzma
    if dados[:2] == b"\x1f\x8b":
        return gzip.decompress(dados)
    if dados[:6] == b"\xfd7zXZ\x00" or compressao == 1:
        try: return lzma.decompress(dados)
        except Exception: pass
    if compressao == 0:
        for wbits in (-15, 15):
            try: return _zlib.decompress(dados, wbits)
            except Exception: pass
    return dados


# A saída do TRACE, como um rádio de verdade a devolveu (10.188.99.4, papel
# VIEW, 26/09/2026): TEXTO da ferramenta `imtrace` do firmware, em gzip,
# sem `compression` nem `content` no TaskOutput:
#
#   Target IP(s):  10.188.96.11(396)
#   Target MAC(s): 00:00:0C:9F:F1:8C
#   imtrace to 00:00:0C:9F:F1:8C (10.188.96.11(396)):peer=84:8D:84:F0:82:75/
#     wlan0wds50/128433 cost=9225 hop cost=6352 channel=6 display channel=6
#     freq=2437 signal=-42 rssi=53 rate=58.5
#
# Os mesmos campos do traceInfo.path do MeshMapper: `peer` é
# MAC/interface/encap (o encap é o sufixo do serial do vizinho), `cost` o
# do caminho, `hop cost` o do salto, `rssi` o SNR em dB.
_RX_IMTRACE = {
    "destino": re.compile(r"Target IP\(s\):\s*([0-9.]+)"),
    "peer":    re.compile(r"\bpeer=(\S+)"),
    "custo":   re.compile(r"(?<!hop )\bcost=(\d+)"),
    "salto":   re.compile(r"\bhop cost=(\d+)"),
    "canal":   re.compile(r"(?<!display )\bchannel=(\d+)"),
    "freq":    re.compile(r"\bfreq=(\d+)"),
    "sinal":   re.compile(r"\bsignal=(-?\d+)"),
    "snr":     re.compile(r"\brssi=(-?\d+)"),
    "taxa":    re.compile(r"\brate=([\d.]+)"),
}


def _custo_de_rota(v):
    """0 não é custo de rota; o máximo de int32 OU de uint32 é sem rota."""
    if v is None or v == 0:
        return None
    return CUSTO_SEM_ROTA if v >= CUSTO_SEM_ROTA else v


def trace_de_texto(txt):
    """O texto do `imtrace` → o dict de `_trace_para_dict`. None quando
    não há linha de trace com custo — o formato de "sem rota" em texto não
    foi visto em campo, e ausência de número não vira número."""
    linha = next((l for l in txt.replace("\x00", "").splitlines()
                  if l.strip().startswith("imtrace")), None)
    if not linha:
        return None
    def achar(chave, conv, onde=linha):
        m = _RX_IMTRACE[chave].search(onde)
        return conv(m.group(1)) if m else None
    custo = achar("custo", int)
    if custo is None:
        return None
    mac = iface = encap = None
    peer = achar("peer", str)
    if peer:
        partes = peer.split("/")
        mac = partes[0] or None
        iface = partes[1] if len(partes) > 1 else None
        encap = int(partes[2]) if len(partes) > 2 and partes[2].isdigit() else None
    salto = achar("salto", int)
    sinal, snr = achar("sinal", int), achar("snr", int)
    return {
        "destino": achar("destino", str, txt),
        "tipo": ("WIRELESS_PEER" if iface and iface.startswith("wlan")
                 else iface),
        "saida": iface, "mac": mac.lower() if mac else None, "encap": encap,
        "custo_caminho": _custo_de_rota(custo),
        "custo_saida": (None if _custo_de_rota(salto) in (None, CUSTO_SEM_ROTA)
                        else salto),
        "sinal": sinal or None, "snr": snr or None,
        "taxa": achar("taxa", float),
        "canal": achar("canal", int), "freq": achar("freq", int),
    }


def trace_de_bytes(dados, compressao=None):
    """Interpreta a saída do TRACE. Na ordem: o texto do `imtrace` (o que o
    rádio devolve de fato), a mensagem `Trace` binária e a `Trace` em
    text-format — as duas últimas pelo Common.proto, para firmware que as
    use. Devolve o dict de `_trace_para_dict`, ou None."""
    dados = _descomprimir_saida(dados, compressao)
    texto = dados.decode("utf-8", "replace")
    if "imtrace" in texto:
        return trace_de_texto(texto)
    from rajant_api import Common_pb2
    t = Common_pb2.Trace()
    try:
        t.ParseFromString(dados)
        if t.HasField("path"):
            return _trace_para_dict(t)
    except Exception:
        pass
    try:
        from google.protobuf import text_format
        t = Common_pb2.Trace()
        text_format.Parse(texto, t, allow_unknown_field=True)
        if t.HasField("path"):
            return _trace_para_dict(t)
    except Exception:
        pass
    return None


def _trace_para_dict(t):
    """Os campos que o laudo usa, com os mesmos cuidados do arquivo do
    MeshMapper: 0 não é custo de rota, 2147483647 é sem rota."""
    p = t.path
    tipo = type(p).PathType.Name(p.type) if p.HasField("type") else None
    custo = p.cost if p.HasField("cost") else None
    salto = (p.hopCost if "hopCost" in type(p).DESCRIPTOR.fields_by_name
             and p.HasField("hopCost") else None)
    return {
        "destino": t.host or None,
        "tipo": tipo,
        "saida": (p.name or "").strip() or None,
        "mac": p.mac.hex(":") if p.mac else None,
        "encap": p.encapid if p.HasField("encapid") else None,
        "custo_caminho": _custo_de_rota(custo),
        "custo_saida": (None if salto is None or salto >= CUSTO_SEM_ROTA
                        else salto or None),
        "sinal": (p.signal or None) if p.HasField("signal") else None,
        "snr": (p.rssi or None) if p.HasField("rssi") else None,
        "taxa": (p.rate or None) if p.HasField("rate") else None,
        "canal": p.channel if p.HasField("channel") else None,
        "freq": p.freq if p.HasField("freq") else None,
    }


def bc_trace(bc, destino, espera_s=3.0, registro=None):
    """Por onde o rádio sai até `destino`, pelo TRACE do próprio rádio.

    Pelo Common.proto e pelo Message.proto: pede-se a tarefa em
    `runTask` (TaskCommand.TRACE, `arguments` = destino); o rádio responde
    em `runTaskResult` (com o id); a saída é baixada por
    `taskOutputRequest`, em fragmentos até SUCCESS. A saída não é a
    mensagem `Trace`: é o texto do `imtrace` em gzip — ver
    `trace_de_texto`, conferido num rádio da mina.

    SÓ TRACE. A mesma mensagem pede REBOOT, ZEROIZE, CLEAR e KICK; a ação
    é uma constante aqui, nunca um parâmetro.

    `registro` (lista) recebe cada mensagem trocada, em texto — é o que o
    diagnóstico de campo grava. Devolve o dict de `_trace_para_dict`.
    """
    def _reg(rotulo, msg):
        if registro is not None:
            registro.append((rotulo, str(msg)))

    def montar(m):
        m.runTask.action = type(m.runTask).TRACE
        m.runTask.arguments = destino
    _reg("pedido runTask", "action: TRACE\narguments: " + destino)
    r = bc_pedir(bc, montar, lambda r: r.HasField("runTaskResult"))
    res = r.runTaskResult
    _reg("runTaskResult", res)
    if res.status == type(res).FAILURE:
        desc = res.description or "o rádio recusou a tarefa"
        if _RX_OCUPADO.search(desc):
            raise TraceOcupado(desc)
        raise TraceRecusado(desc)
    tid = res.id if res.HasField("id") else None

    # Em campo a saída ficou pronta em ~0,8 s, depois de 3 FAILED e 5
    # UNAVAILABLE a cada ~90 ms. Esperar um pouco antes da primeira
    # pergunta e perguntar a cada 100 ms dá o mesmo tempo com metade das
    # mensagens na malha.
    prazo = time.monotonic() + espera_s
    time.sleep(0.3)
    dados, pos, ultimo = b"", 0, None
    while True:
        def montar_saida(m, pos=pos):
            m.taskOutputRequest.position = pos
            if tid is not None:
                m.taskOutputRequest.id = tid
        r = bc_pedir(bc, montar_saida,
                     lambda r: r.HasField("taskOutputResponse"))
        o = r.taskOutputResponse
        _reg(f"taskOutputResponse (posição {pos})",
             f"status: {type(o).Status.Name(o.status) if o.HasField('status') else '?'}"
             f"\nposition: {o.position}\nbytes: {len(o.data)}"
             + (f"\noutput {{\n{o.output}}}" if o.HasField("output") else ""))
        ultimo = o
        if o.status == type(o).FRAGMENT:
            dados += o.data
            pos = o.position or (pos + len(o.data))
            continue
        if o.status == type(o).SUCCESS:
            dados += o.data
            break
        # FAILED ou UNAVAILABLE: a tarefa pode ainda não ter terminado.
        if time.monotonic() >= prazo:
            try:
                _reg("State.task", bc_state(bc, None).task)
            except Exception as e:
                _reg("State.task (falhou)", e)
            raise TraceIndisponivel(
                "saída do trace indisponível: "
                + type(o).Status.Name(o.status))
        time.sleep(0.1)

    compressao = (ultimo.output.compression
                  if ultimo is not None and ultimo.HasField("output")
                  and ultimo.output.HasField("compression") else None)
    if registro is not None:
        import base64
        registro.append(("saída (base64)", base64.b64encode(dados).decode()))
        registro.append(("saída (texto)", _descomprimir_saida(
            dados, compressao).decode("utf-8", "replace").replace("\x00", "")))
    t = trace_de_bytes(dados, compressao)
    if t is None:
        # O trecho do que o rádio disse vai junto: é assim que se aprende o
        # formato de "sem rota" em texto, que ainda não foi visto em campo.
        dito = " ".join(_descomprimir_saida(dados, compressao).decode(
            "utf-8", "replace").replace("\x00", "").split())
        raise TraceIndisponivel(f"saída sem caminho com custo: {dito[:160]!r}")
    _reg("trace interpretado", "\n".join(f"{k}: {v}" for k, v in t.items()))
    return t


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
    # `sinal_cob` e nao `sinal`: o laudo reporta o RSSI do melhor ERB/ERM
    # do ponto, que e o mesmo numero do mapa. Reportar aqui o enlace que
    # atendeu e colorir o mapa pelo outro daria duas respostas para a
    # mesma pergunta na mesma pagina.
    for c in ("sinal_cob", "snr", "custo_caminho", "rtt", "perda"):
        r = (resumo or {}).get(c)
        if not r: continue
        rot = (limite_de(c) or (None, None, None, c))[3]
        op, lim, un, _ = limite_de(c)
        # Só é requisito o que está no contrato; o custo do caminho entra
        # com a régua da Rajant, e o rótulo diz de quem ela é.
        dono = "" if c in REQUISITOS or c == "sinal_cob" else " (Rajant)"
        linhas.append([rot, f"{_OP_TXT[op]} {lim:g} {un}".rstrip() + dono,
                       f"{r['pct_ok']:.1f}%",
                       fmt_leitura(r["mediana"], c),
                       fmt_leitura(r["pior5"], c)])
    destaques = {}
    for i, l in enumerate(linhas, start=1):
        try:
            if float(l[2].rstrip("%")) < 90: destaques[(i, 2)] = "C0392B"
        except ValueError: pass
    if linhas:
        _tabela_anglo(s, 0.55, 1.45, 7.4,
                      ["Grandeza", "Requisito", "Dentro", "Mediana",
                       "Pior 5%"], linhas, destaques)

    # Ping-pong saiu dos cartões: é diagnóstico de roteamento do
    # InstaMesh, não de cobertura, e num sumário de survey puxava a
    # conversa para outro assunto. Segue calculado e no texto das
    # recomendações, onde tem contexto.
    ext = extensao_da_captura(am)
    _passo = _passo_tipico(am)
    cart = [("Amostras", f"{_milhar(len(am))}"),
            ("Rádios", str(len({a['radio'] for a in am}))),
            # `is not None`, nao truthy: passo 0 m e um RESULTADO — quer
            # dizer que mais da metade das leituras saiu com o equipamento
            # imovel —, e some se for tratado como ausente.
            ("Passo entre amostras",
             f"{_passo:g} m" if _passo is not None else "—"),
            ("Área coberta",
             f"{_milhar(round(ext['raio_m']))} m de raio" if ext else "—"),
            ("Handovers", str(srv.get("n_handovers", 0)))]
    y = 1.45
    for rot, val in cart:
        _cartao_anglo(s, 8.35, y, 4.45, 0.72, rot, val)
        y += 0.82

    # ── Metodologia ──
    # Ficha tecnica da medicao, nao texto explicativo: instrumento,
    # grandeza, criterio de amostragem, regua e limitacoes. Vem antes das
    # paginas de mapa porque duas coisas nao se leem no mapa — qual
    # enlace define a cor e onde ficam os cortes da escala.
    s = slide_anglo(p, "Metodologia",
                    "Parâmetros da medição e critérios de classificação")
    passo = _passo_tipico(am)
    ao_vivo = any(a.get("fonte") == "direto" for a in am)
    canais = sorted({int(a["canal"]) for a in am if a.get("canal")})
    n_eq = len({a["radio"] for a in am})
    req_s = float(ESCALAS["sinal_cob"]["req"])
    req_n = float(ESCALAS["snr"]["req"])
    tem_caminho = any(a.get("custo_caminho") is not None for a in am)
    ficha = [
        ["Instrumento",
         "BC API — leitura direta do State de cada rádio" if ao_vivo
         else f"Rajant MeshMapper {sv.get('versao_bcc') or ''}".strip()],
        ["Equipamentos",
         f"{n_eq} rádio{'s' if n_eq != 1 else ''} · "
         f"{_milhar(len(am))} amostras"],
        ["Grandeza de referência",
         "RSSI do enlace de maior intensidade com ERB/ERM visível no "
         "ponto, na banda do mapa"],
        ["Georreferenciamento",
         "GPS do próprio equipamento, na mesma leitura do sinal"],
        ["Validação de posição",
         "amostra descartada quando o horário do fix não avançou"],
        ["Espaçamento entre amostras",
         (f"mediana {passo:g} m" + (" (equipamento parado em mais da metade "
                                    "das leituras)" if passo == 0 else ""))
         if passo is not None else "não aplicável"],
        ["Bandas",
         " · ".join(b for b in bandas if b) + " — arquivos separados"
         + (f" · canais {', '.join(str(c) for c in canais)}" if canais else "")],
        ["Requisitos (Modular Mining)",
         f"RSSI > {req_s:g} dBm · SNR > {req_n:g} dB"],
        ["Custo do caminho",
         f"trace do MeshMapper até {sv['destino_trace']} — régua Rajant "
         f"(greatPath 10000, goodPath 20000)"
         if tem_caminho and sv.get("destino_trace") else
         "trace do MeshMapper — régua Rajant (greatPath 10000, goodPath 20000)"
         if tem_caminho else "não medido na coleta direta (sem trace)"],
        ["Grandezas não medidas", "latência e perda de pacotes"],
    ]
    _tabela_anglo(s, 0.55, 1.5, 7.5, ["Parâmetro", "Especificação"],
                  ficha, tam=10)

    # A regua, com as cores DE VERDADE, lidas de FAIXAS_KML. Desenhada a
    # mao ela divergiria do mapa na primeira vez que as faixas mudassem.
    _txt_anglo(s, 8.15, 1.5, 4.65, 0.3, "Escala de classificação — RSSI",
               10, True, ANGLO["texto"])
    # Rótulos, cores e classes de `_faixas_rotuladas`: a mesma fonte da
    # legenda na tela do KMZ. Pior embaixo, como no mapa.
    y = 1.85
    faixas_s = FAIXAS_KML["sinal_cob"]
    for k, (rot, cor, cls) in reversed(list(enumerate(
            _faixas_rotuladas("sinal_cob")))):
        ant = faixas_s[k - 1][0] if k else None
        try:
            from pptx.util import Inches as _In
            from pptx.enum.shapes import MSO_SHAPE
            from pptx.dml.color import RGBColor as _RGB
            cx = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, _In(8.15), _In(y),
                                    _In(0.42), _In(0.24))
            cx.fill.solid(); cx.fill.fore_color.rgb = _RGB.from_string(cor)
            cx.line.fill.background(); cx.shadow.inherit = False
        except Exception:
            pass
        _txt_anglo(s, 8.70, y - 0.03, 4.1, 0.3,
                   f"{rot} dBm   {cls}".rstrip(), 9.5,
                   negrito=(ant == req_s), cor=ANGLO["texto"])
        y += 0.30
    _txt_anglo(s, 8.15, y + 0.05, 4.65, 0.5,
               f"Requisito Modular Mining: RSSI > {req_s:g} dBm. "
               f"{req_s:g} dBm exato é reprovado.", 9,
               False, ANGLO["suave"])
    _txt_anglo(s, 0.55, 6.8, 12.25, 0.4,
               f"{FONTE_FAIXAS['sinal_cob']} O mapa do KMZ usa esta mesma "
               f"tabela.", 9, False, ANGLO["suave"])

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

    # Os slides de Zonas-Problema sairam junto, pelo mesmo motivo: a
    # pagina listava coordenada sugerida para avaliar radio.

    # ── Molduras para o print do Google Earth ──
    for b in bandas:
        # UMA pagina de RSSI, a do melhor ERB/ERM do ponto. Duas paginas
        # com o mesmo rotulo em dBm faziam o leitor procurar a diferenca
        # entre elas em vez de ler o mapa.
        for campo, rot in (("enl_bons", SLIDE_DE_CAMPO["enl_bons"]),
                           ("sinal_cob", "Intensidade de Sinal (RSSI)"),
                           ("snr", "Relação Sinal/Ruído (SNR)"),
                           ("ruido", "Noise Floor"),
                           ("custo_caminho",
                            "Custo do Caminho (Trace Path Cost)"),
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
            nome_lim = ("requisito" if campo in REQUISITOS
                        or campo == "sinal_cob" else "referência Rajant")
            sub = (("Pino: enlaces bons no ponto (custo ≤ 10000 e SNR ≥ 20)"
                    if campo == "enl_bons" else
                    "Rota medida, colorida pelo valor de cada amostra")
                   + (f"  ·  {nome_lim} {_OP_TXT[lim_[0]]} {lim_[1]:g} "
                      f"{lim_[2]}".rstrip() if lim_ else ""))
            s = slide_anglo(p, f"{rot}{rot_b}", sub)

            # Mapa a esquerda, distribuicao a direita: a moldura mostra
            # ONDE, o grafico mostra QUANTO.
            suf = f"_{b.replace(' ', '').replace('.', '')}" if b else ""
            # A aba e a camada com os nomes EXATOS do painel do Google
            # Earth. Era "camada: Rotas" — pasta que deixou de existir
            # quando o rastro virou a fita do Trajeto.
            _moldura_anglo(s, 0.55, 1.45, 7.3, 5.4,
                           f"Survey_*_todas{suf}.kmz",
                           f"aba: {nome_da_aba(campo)}   ·   "
                           f"camada: {CAMADA_DO_CALOR}"
                           + (f"   ·   {b}" if b else ""))
            rotulos, pcts, un = distribuicao(am_b, campo)
            if rotulos:
                _grafico_anglo(s, 8.05, 1.45, 4.75, 3.4,
                               "Distribuição" + (f" ({un})" if un else "")
                               + " — % das amostras",
                               rotulos, pcts, campo=campo)
            r_res = (survey_resumo(am_b) or {}).get(campo)
            if r_res:
                # "Requisito" só para o que é do contrato. Ruído e custo
                # do caminho são comparados a uma referência — derivada e
                # da Rajant — e o cartão diz isso.
                _cartao_anglo(s, 8.05, 5.05, 2.28, 0.85,
                              "Dentro do requisito" if campo in REQUISITOS
                              or campo == "sinal_cob"
                              else "Dentro da referência",
                              f"{r_res['pct_ok']:.1f}%")
                _cartao_anglo(s, 10.52, 5.05, 2.28, 0.85, "Pior 5%",
                              fmt_leitura(r_res["pior5"], campo))
            # Escala embaixo, na faixa que sobrou entre os cartões e o pé
            # do slide: sem ela o mapa é uma fita colorida sem significado
            # para quem não fez a medição.
            _escala_anglo(s, 8.05, 6.05, 4.75, campo)

    # ── Diagnóstico e conclusões: em branco, para preencher ──
    # O deck entrega o que foi MEDIDO. A leitura daquilo — o que explica
    # o número e o que fazer a respeito — é de quem conhece a operação, e
    # sai de uma reunião, não de um gerador de relatório. Estas páginas
    # entram estruturadas e vazias.
    s = slide_anglo(p, "Diagnóstico",
                    "Leitura dos resultados — preencher")
    _txt_anglo(s, 0.55, 1.5, 12.25, 0.32,
               "Áreas críticas identificadas", 11, True, ANGLO["texto"])
    y = 1.95
    for _ in range(4):
        _linha_anglo(s, 0.75, y + 0.26, 12.05)
        y += 0.52
    _txt_anglo(s, 0.55, y + 0.12, 12.25, 0.32,
               "Causa provável", 11, True, ANGLO["texto"])
    y += 0.55
    for _ in range(3):
        _linha_anglo(s, 0.75, y + 0.26, 12.05)
        y += 0.52

    s = slide_anglo(p, "Conclusões e Ações",
                    "Encaminhamentos — preencher")
    _tabela_anglo(s, 0.55, 1.5, 12.25,
                  ["#", "Ação", "Local / equipamento", "Responsável", "Prazo"],
                  [[str(i), "", "", "", ""] for i in range(1, 8)], tam=11)

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

    # A MESMA classificação do mapa, fronteira incluída: contar aqui com
    # outra regra faria a barra de "reprovado" discordar da cor do ponto
    # justamente nas leituras exatas de limite, que são as mais comuns.
    baixo = limites_na_faixa_de_baixo(campo)
    contas = [0] * len(faixas)
    for v in vals:
        contas[_faixa_idx(v, faixas, baixo)] += 1
    rotulos = [r for r, _c, _k in _faixas_rotuladas(campo)]
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
            # None vira "—" (não medido); string vazia fica VAZIA. São
            # coisas diferentes: a tabela de ações do fim do deck é um
            # formulário para preencher, e "—" ali se lê como "não se
            # aplica" em vez de "escreva aqui".
            r = par.add_run()
            r.text = "—" if v is None else str(v)
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


def _linha_anglo(s, x, y, w):
    """Pauta fina, para o slide que vai ser preenchido a mão ou digitado.

    Retângulo de 1 pt em vez de conector: conector no python-pptx nasce
    com a cor e a espessura do tema do arquivo, que aqui é o padrão do
    PowerPoint e sai preto e grosso no meio de um deck claro.
    """
    from pptx.util import Inches, Pt
    from pptx.enum.shapes import MSO_SHAPE
    cx = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y),
                            Inches(w), Pt(0.75))
    cx.fill.solid(); cx.fill.fore_color.rgb = _rgb(ANGLO["linha"])
    cx.line.fill.background(); cx.shadow.inherit = False
    return cx


def _escala_anglo(s, x, y, w, campo):
    """Barra da escala de cores do slide, com o requisito marcado.

    Um bloco por FAIXA da legenda, com a cor e o intervalo exatos de
    `_faixas_rotuladas` — a mesma fonte da legenda impressa no mapa do
    Google Earth que é colado ao lado. Era um gradiente contínuo de 28
    tons: o print mostrava as faixas e a barra do slide mostrava outra
    régua, e ainda escrevia "requisito ≥ -75" com o requisito sendo > -75.
    """
    from pptx.util import Inches, Pt
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import PP_ALIGN

    esc = ESCALAS.get(campo)
    faixas = FAIXAS_KML.get(campo)
    if not esc or not faixas:
        return None
    un = esc.get("un", "")
    rotulos = _faixas_rotuladas(campo)

    _txt_anglo(s, x, y, w, 0.24,
               f"Escala — {esc.get('rot', campo)}" + (f" ({un})" if un else ""),
               9.5, True,
               ANGLO["suave"])

    yb, hb = y + 0.24, 0.26
    lb = w / len(rotulos)
    for i, (rng, cor, _cls) in enumerate(rotulos):
        r = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x + i * lb),
                               Inches(yb), Inches(lb + 0.004), Inches(hb))
        r.fill.solid(); r.fill.fore_color.rgb = _rgb(cor)
        r.line.fill.background(); r.shadow.inherit = False
        t = _txt_anglo(s, x + i * lb, yb + hb + 0.02, lb, 0.22, rng, 7.5,
                       False, ANGLO["suave"])
        t.text_frame.paragraphs[0].alignment = PP_ALIGN.CENTER

    req = limite_de(campo)
    if req is not None:
        op, lim = req[0], float(req[1])
        k = next((i for i, (l, _c) in enumerate(faixas) if l == lim), None)
        if k is not None:
            # Marca na divisa entre as duas faixas que o requisito separa:
            # é a linha entre aprovado e reprovado.
            fx = x + (k + 1) * lb
            mk = s.shapes.add_shape(MSO_SHAPE.RECTANGLE,
                                    Inches(fx - 0.008), Inches(yb - 0.05),
                                    Inches(0.016), Inches(hb + 0.10))
            mk.fill.solid(); mk.fill.fore_color.rgb = _rgb(ANGLO["texto"])
            mk.line.fill.background(); mk.shadow.inherit = False
        nome = ("requisito" if campo in REQUISITOS or campo == "sinal_cob"
                else "referência Rajant" if campo == "custo_caminho"
                else "referência")
        _txt_anglo(s, x, yb + hb + 0.26, w, 0.22,
                   f"{nome} {_OP_TXT[op]} {lim:g} {un}".rstrip(), 8.5, True,
                   ANGLO["azul"])
    return yb + hb + 0.50


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

