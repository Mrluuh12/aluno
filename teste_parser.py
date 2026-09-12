#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TESTES DO PARSER — rajant_monitor.py

Um caso por correção da auditoria das métricas contra os .proto oficiais
do bcapi (bcapi-ref/proto/). Cada teste referencia o campo do protocolo
que justifica o comportamento esperado.

Regra que orienta todos os casos: campo que NÃO existe no protocolo não
pode virar 0 — 0 é indistinguível de "medido e sem ocorrência". Ou vira
None (e a série não é publicada), ou a métrica deixa de existir.

    python3 teste_parser.py
"""
import sys, types, unittest, tempfile, shutil, os, csv, inspect, re, pathlib, json, time
import sqlite3
from pathlib import Path
from unittest import mock

# ── rajant_api não é instalável em CI; o módulo faz sys.exit(1) sem ele ──
if 'rajant_api' not in sys.modules:
    _fake = types.ModuleType('rajant_api')
    class Breadcrumb:                     # stub: nenhum teste vai à rede
        def __init__(self, *a, **k): pass
    _fake.Breadcrumb = Breadcrumb
    sys.modules['rajant_api'] = _fake

import rajant_monitor as rm


# ══════════════════════════════════════════════════════════════════
# FIXTURE — State no formato text-format EXATO do State.proto
# ══════════════════════════════════════════════════════════════════
# Inclui de propósito a ARMADILHA `configuration`: ela repete os nomes
# wired / wireless / battery / general com os campos de CONFIGURAÇÃO,
# que não têm contador nenhum. Um parser que extraia do texto inteiro
# pega esses blocos e lê zero em tudo.
STATE = """
system {
  kernel: "3.18.20"
  platform: "ES1"
  uptime: 864000000.0
  idle: 691200000.0
  running: true
  bridgeup: true
  version: "11.25.0"
  freeMemory: 131072
  temperature: 4520
  bootCounter: 7
  reboot: false
  ipv4 {
    address: "10.188.96.140"
    subnet: "255.255.255.0"
  }
}
build {
  date: "2024-03-01"
  version: "11.25.0"
  number: "4471"
}
instamesh {
  arpDropped: 3
  arpRequests: 900
  arpTotal: 1200
  floodsDropped: 44
  packetsDropped: 120
  packetsMulticast: 5000
  packetsReceived: 900000
  packetsSent: 850000
  sourceFloodsDropped: 17
  timeWaited: 90
  discoveriesSourced: 12
  discoveriesPassed: 34
}
battery {
  hardwareRevision: "B"
  milliamps: 1500
  charging: false
  capacityPercent: 87
  temperatureCelsius: 31
  dischargeTimeMinutes: 210
  chargeTimeMinutes: 0
}
alertSystem {
  bestRadioRate: 866
  ledMode: "normal"
  alerts {
    index: 1
    type: WARNING
    message: "link marginal"
  }
  alerts {
    index: 2
    type: INFORMATION
    message: "config salva"
  }
}
gps {
  gpsSwitch {
    enabled: true
  }
  gpsPos {
    gpsTime: 143025.0
    gpsLat: "2743.8950S"
    gpsLong: "05004.1429W"
    gpsPrecisionH: 0.9
    gpsQuality: 1.0
    gpsSatsInView: 9
    gpsAlt: 812.5
  }
  gpsVel {
    gpsTrackDegreesTrue: 187.3
    gpsSpeedKnots: 22.95
    gpsSpeedKph: 42.5
  }
}
wireless {
  key: 1
  mac: "00:11:22:33:44:55"
  name: "wlan0"
  noise: -95
  channel: 149
  txpower: 23
  type: "5GHz"
  stats {
    rxBytes: 5000000
    rxPackets: 40000
    txBytes: 3000000
    txPackets: 30000
  }
  ap {
    key: 1
    essid: "MESH-OPERACAO"
    client {
      mac: "aa:bb:cc:00:00:01"
      rate: 650
      rssi: 38
      signal: -57
      age: 4
    }
    client {
      mac: "aa:bb:cc:00:00:02"
      rate: 780
      rssi: 41
      signal: -54
      age: 2
    }
    client {
      mac: "aa:bb:cc:00:00:03"
      rate: 520
      rssi: 29
      signal: -66
      age: 9
    }
  }
  peer {
    mac: "00:11:22:33:44:66"
    enabled: true
    cost: 120
    rate: 1300
    rssi: 42
    signal: -58
    age: 3
    ipv4Address: "10.188.96.141"
    stats {
      rxBytes: 900000
      txBytes: 700000
    }
  }
  peer {
    mac: "00:11:22:33:44:77"
    enabled: true
    cost: 340
    rate: 650
    rssi: 21
    signal: -81
    age: 11
  }
  channelActiveTime: 100000
  channelBusyTime: 22000
  channelReceiveTime: 9000
  channelTransmitTime: 6000
}
wired {
  key: 1
  mac: "00:11:22:33:44:88"
  masterMac: "00:11:22:33:44:88"
  aptState: APT_STATE_MASTER
  stats {
    rxBytes: 12000000
    rxPackets: 90000
    txBytes: 8000000
    txPackets: 70000
  }
  peer {
    mac: "00:11:22:33:44:99"
    enabled: true
    cost: 10
    ipv4Address: "10.188.96.142"
  }
  name: "eth0"
  ipv4 {
    address: "10.188.96.140"
  }
}
wired {
  key: 2
  mac: "00:11:22:33:44:aa"
  aptState: APT_STATE_NONE
  stats {
    rxBytes: 0
    rxPackets: 0
    txBytes: 0
    txPackets: 0
  }
  name: "eth1"
}
configuration {
  saved {
    general {
      name: "BC-MINA-01"
      notes: "backbone"
      groups {
        name: "MINA-NORTE"
      }
      groups {
        name: "BACKBONE"
      }
    }
    battery {
      warningThresholdMinutes: 30
      errorThresholdMinutes: 10
    }
    wired {
      name: "eth0"
      requestFallback: false
      speedMbps: 1000
    }
    wireless {
      name: "wlan0"
      ap {
        essid: "MESH-OPERACAO"
      }
    }
  }
}
"""


class BaseParser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = rm.parse_state(STATE)
        cls.s = cls.d["sistema"]
        cls.im = cls.d["instamesh"]
        cls.r = cls.d["radios"][0]
        cls.eth = {e["nome"]: e for e in cls.d["ethernet"]}


# ══════════════════════════════════════════════════════════════════
# 1. ARMADILHA `configuration`
# ══════════════════════════════════════════════════════════════════
class TestBlocoConfiguration(BaseParser):
    """State e Config têm mensagens de mesmo nome; o parser tem de ler o
    bloco de ESTADO, não o de configuração."""

    def test_wired_vem_do_estado_nao_da_config(self):
        # Config.Wired tem name/requestFallback/speedMbps e nenhum contador.
        self.assertEqual(self.eth["eth0"]["rx_bytes"], 12000000)
        self.assertEqual(self.eth["eth0"]["tx_bytes"], 8000000)

    def test_apenas_duas_portas_a_config_nao_vira_porta(self):
        self.assertEqual(len(self.d["ethernet"]), 2)

    def test_bateria_vem_de_state_battery_nao_de_config_battery(self):
        # Config.Battery só tem warning/errorThresholdMinutes. Se o parser
        # pegasse esse bloco, capacityPercent viria 0.
        self.assertEqual(self.s["bateria"]["pct"], 87)

    def test_nome_vem_de_configuration_saved_general_name(self):
        # Buscar `name:` na configuration inteira pegaria "MINA-NORTE"
        # (nome de grupo) ou "eth0" (nome de porta).
        self.assertEqual(self.s["nome"], "BC-MINA-01")


# ══════════════════════════════════════════════════════════════════
# 2. CAMPOS INVENTADOS — nunca podem virar 0
# ══════════════════════════════════════════════════════════════════
class TestCamposInexistentes(BaseParser):

    def test_sem_chaves_de_erro_de_ethernet(self):
        # CommStats = rxBytes, rxPackets, txBytes, txPackets. Ponto.
        for chave in ("rx_err","tx_err","rx_crc","rx_drop","tx_drop","mudancas"):
            self.assertNotIn(chave, self.eth["eth0"],
                             f"{chave} não existe em State.Wired/CommStats")

    def test_sem_chaves_de_radar_e_phy(self):
        for chave in ("radar_detec","radar_pulsos","phy"):
            self.assertNotIn(chave, self.r,
                             f"{chave} não existe em State.Wireless")

    def test_sem_txpower_por_peer(self):
        # State.Peer não tem txpower; só State.Wireless tem.
        self.assertNotIn("txpwr", self.r["peers"][0])

    def test_sem_session_state(self):
        self.assertNotIn("session_st", self.s)

    def test_sem_metricas_de_tensao(self):
        # Não existe bloco `sensors` em nenhum .proto.
        for chave in ("volt_v","volt_min_v","volt_max_v","bateria_v"):
            self.assertNotIn(chave, self.s)

    def test_instamesh_sem_campos_inventados(self):
        for chave in ("unicast","ovf","undel_rx","undel_tx"):
            self.assertNotIn(chave, self.im,
                             f"{chave} não consta de State.InstaMesh")

    def test_metricas_removidas_nao_existem_mais(self):
        for nome in ("m_volt","m_volt_min","m_volt_max","m_bateria",
                     "m_session_state","m_r_radar","m_r_pulsos","m_r_phy",
                     "m_p_txpwr","m_e_rx_err","m_e_tx_err","m_e_rx_crc",
                     "m_e_rx_drop","m_e_tx_drop","m_e_mudancas",
                     "m_im_unicast","m_im_ovf","m_im_undel_rx","m_im_undel_tx"):
            self.assertFalse(hasattr(rm, nome), f"{nome} deveria ter sido removida")


# ══════════════════════════════════════════════════════════════════
# 3. InstaMesh — campos reais
# ══════════════════════════════════════════════════════════════════
class TestInstaMesh(BaseParser):

    def test_contadores_reais(self):
        self.assertEqual(self.im["pkt_tx"], 850000)
        self.assertEqual(self.im["pkt_rx"], 900000)
        self.assertEqual(self.im["pkt_drop"], 120)
        self.assertEqual(self.im["floods"], 44)
        self.assertEqual(self.im["arp"], 1200)

    def test_campos_novos_aproveitados(self):
        # sourceFloodsDropped e packetsMulticast existiam e não eram lidos.
        self.assertEqual(self.im["src_floods"], 17)
        self.assertEqual(self.im["multicast"], 5000)


# ══════════════════════════════════════════════════════════════════
# 4. APT / link ethernet
# ══════════════════════════════════════════════════════════════════
class TestAPT(BaseParser):

    def test_apt_state_usa_enum_do_proto(self):
        # APT_STATE_MASTER = 0, APT_STATE_NONE = 2
        self.assertEqual(self.eth["eth0"]["apt_int"], 0)
        self.assertEqual(self.eth["eth1"]["apt_int"], 2)

    def test_apt_master_derivado_de_aptstate(self):
        # aptMaster/isMaster não existem; deriva de Wired.aptState == MASTER
        self.assertTrue(self.s["apt_master"])

    def test_link_inferido(self):
        self.assertTrue(self.eth["eth0"]["link"])    # MASTER + peer
        self.assertFalse(self.eth["eth1"]["link"])   # NONE, sem peer

    def test_peers_por_porta(self):
        self.assertEqual(self.eth["eth0"]["peers_eth"], 1)
        self.assertEqual(self.eth["eth1"]["peers_eth"], 0)

    def test_apt_master_falso_sem_porta_master(self):
        st = STATE.replace("aptState: APT_STATE_MASTER", "aptState: APT_STATE_SLAVE")
        self.assertFalse(rm.parse_state(st)["sistema"]["apt_master"])


# ══════════════════════════════════════════════════════════════════
# 5. Clientes de AP — contagem de blocos
# ══════════════════════════════════════════════════════════════════
class TestClientesAP(BaseParser):

    def test_conta_blocos_client(self):
        # State.Wireless.AP tem `repeated Client client`; não há clientCount.
        self.assertEqual(self.r["aps"][0]["clients"], 3)

    def test_total_por_radio(self):
        self.assertEqual(self.r["total_cli"], 3)

    def test_ap_entra_sem_campo_enabled(self):
        # State.Wireless.AP NÃO tem `enabled`. Exigi-lo zerava a lista de APs.
        self.assertEqual(len(self.r["aps"]), 1)
        self.assertEqual(self.r["aps"][0]["essid"], "MESH-OPERACAO")


# ══════════════════════════════════════════════════════════════════
# 6. Peers — SNR, taxa, IP opcional
# ══════════════════════════════════════════════════════════════════
class TestPeers(BaseParser):

    def test_taxa_dividida_por_10(self):
        # bc_livestats.py oficial: pv.rate / 10 = Mbps
        self.assertEqual(self.r["peers"][0]["taxa"], 130.0)

    def test_snr_e_sinal_menos_ruido(self):
        # signal=-58, noise=-95  ->  SNR = 37 dB
        # A conta antiga (rssi - abs(noise)) dava 42-95 = -53.
        self.assertEqual(self.r["peers"][0]["snr"], 37)
        self.assertEqual(self.r["peers"][1]["snr"], 14)   # -81 - (-95)

    def test_peer_sem_ipv4_nao_e_descartado(self):
        # ipv4Address é `optional` em State.Peer: exigir descartava o enlace
        # inteiro, com taxa, custo, RSSI e SNR junto.
        self.assertEqual(len(self.r["peers"]), 2)
        self.assertTrue(self.r["peers"][1]["ip"].startswith("mac:"))

    def test_good_peers_usa_snr_correto(self):
        # Só o primeiro peer tem SNR > 30 dB.
        self.assertEqual(self.r["good_peers"], 1)

    def test_custo_e_first_hop(self):
        self.assertEqual(self.r["peers"][0]["custo"], 120)
        self.assertEqual(self.r["first_hop"], 120)


# ══════════════════════════════════════════════════════════════════
# 7. GPS
# ══════════════════════════════════════════════════════════════════
class TestGPS(BaseParser):

    def test_nmea_convertido(self):
        self.assertAlmostEqual(self.s["gps_lat"], -27.7316, places=3)
        self.assertAlmostEqual(self.s["gps_lon"], -50.0690, places=3)
        self.assertEqual(self.s["gps_fix"], 1)

    def test_velocidade_em_kph_sem_reconverter(self):
        # gpsSpeedKph JÁ está em km/h. O parser antigo lia `speed`
        # (inexistente) e ainda multiplicava por 1,852.
        self.assertEqual(self.s["gps_vel"], 42.5)

    def test_rumo_de_gpstrackdegreestrue(self):
        # `course`/`heading`/`track` não existem no Gps.proto.
        self.assertEqual(self.s["gps_rumo"], 187.3)

    def test_campos_de_qualidade(self):
        self.assertEqual(self.s["gps_sats"], 9)
        self.assertEqual(self.s["gps_alt"], 812.5)
        self.assertEqual(self.s["gps_hdop"], 0.9)

    def test_fallback_para_knots(self):
        st = STATE.replace("    gpsSpeedKph: 42.5\n", "")
        # 22.95 kn * 1.852 = 42.5 km/h
        self.assertAlmostEqual(rm.parse_state(st)["sistema"]["gps_vel"], 42.5, places=1)

    def test_sem_velocidade_fica_none_nao_zero(self):
        st = STATE.replace("    gpsSpeedKnots: 22.95\n", "").replace(
            "    gpsSpeedKph: 42.5\n", "")
        self.assertIsNone(rm.parse_state(st)["sistema"]["gps_vel"])

    def test_gpsswitch_desligado_invalida_posicao(self):
        st = STATE.replace("enabled: true", "enabled: false", 1)
        d = rm.parse_state(st)
        self.assertIsNone(d["sistema"]["gps_lat"])
        self.assertEqual(d["sistema"]["gps_fix"], 0)


# ══════════════════════════════════════════════════════════════════
# 8. Sistema — temperatura, CPU, bateria, alertas
# ══════════════════════════════════════════════════════════════════
class TestSistema(BaseParser):

    def test_temperatura_centi_grau(self):
        self.assertEqual(self.s["temp_c"], 45.2)

    def test_temperatura_grau_inteiro(self):
        # Firmware que publica o grau direto não pode virar 0,45 °C.
        st = STATE.replace("temperature: 4520", "temperature: 45")
        self.assertEqual(rm.parse_state(st)["sistema"]["temp_c"], 45.0)

    def test_cpu_none_na_primeira_coleta(self):
        # cpuLoad não existe; a carga vem do delta de idle/uptime e exige
        # duas coletas. None = não publica (0 seria "CPU ociosa", mentira).
        self.assertIsNone(self.s["cpu_pct"])

    def test_cpu_expoe_idle_bruto(self):
        self.assertEqual(self.s["cpu_idle"], 691200000.0)
        self.assertEqual(self.s["uptime_raw"], 864000000.0)

    def test_bateria_completa(self):
        b = self.s["bateria"]
        self.assertEqual(b["pct"], 87)
        self.assertEqual(b["ma"], 1500)
        self.assertEqual(b["temp_c"], 31)
        self.assertEqual(b["desc_min"], 210)
        self.assertFalse(b["carregando"])

    def test_sem_bateria_tudo_none(self):
        # Modelo sem bateria: nenhuma série publicada, nada de zeros.
        st = rm.remover_bloco(STATE, 'battery')
        b = rm.parse_state(st)["sistema"]["bateria"]
        self.assertTrue(all(v is None for v in b.values()))

    def test_alert_system(self):
        self.assertEqual(self.s["best_rate"], 866)
        self.assertEqual(self.s["alertas"], 2)

    def test_reboot_e_bridge(self):
        self.assertEqual(self.s["reboot"], 0)
        self.assertTrue(self.s["bridge"])

    def test_grupos(self):
        self.assertEqual(self.s["grupos"], "MINA-NORTE|BACKBONE")

    def test_ip_e_modelo(self):
        self.assertEqual(self.s["ip"], "10.188.96.140")
        self.assertEqual(self.s["modelo"], "ES1")


# ══════════════════════════════════════════════════════════════════
# 9. Rádio — canal, ruído, ocupação
# ══════════════════════════════════════════════════════════════════
class TestRadio(BaseParser):

    def test_campos_basicos(self):
        self.assertEqual(self.r["nome"], "wlan0")
        self.assertEqual(self.r["canal"], 149)
        self.assertEqual(self.r["freq"], "5GHz")
        self.assertEqual(self.r["ruido"], -95)
        self.assertEqual(self.r["txpwr"], 23)

    def test_stats_do_radio(self):
        self.assertEqual(self.r["rx_bytes"], 5000000)
        self.assertEqual(self.r["tx_pkts"], 30000)

    def test_ocupacao_de_canal(self):
        self.assertEqual(self.r["ch_active"], 100000)
        self.assertEqual(self.r["busy_pct"], 22.0)
        self.assertEqual(self.r["rx_pct"], 9.0)


# ══════════════════════════════════════════════════════════════════
# 10. CPU derivada — precisa de duas coletas
# ══════════════════════════════════════════════════════════════════
class TestCPUDerivada(unittest.TestCase):
    """100 * (1 - Δidle/Δuptime). A razão é adimensional: vale para
    segundos ou milissegundos, desde que os dois campos usem a mesma
    unidade."""

    def setUp(self):
        rm._ultimo.clear()

    def _coleta(self, ip, uptime, idle, ts):
        st = STATE.replace("uptime: 864000000.0", f"uptime: {uptime}") \
                  .replace("idle: 691200000.0",   f"idle: {idle}")
        return rm.calcular_taxas(ip, rm.parse_state(st), ts)

    def test_carga_75_pct(self):
        self._coleta("1.1.1.1", 1000.0, 500.0, 100.0)
        # +100 de uptime, +25 de idle  ->  75% de carga
        d = self._coleta("1.1.1.1", 1100.0, 525.0, 200.0)
        self.assertEqual(d["sistema"]["cpu_pct"], 75.0)

    def test_maquina_ociosa(self):
        self._coleta("2.2.2.2", 1000.0, 500.0, 100.0)
        d = self._coleta("2.2.2.2", 1100.0, 600.0, 200.0)
        self.assertEqual(d["sistema"]["cpu_pct"], 0.0)

    def test_reboot_no_meio_nao_publica(self):
        # uptime volta a zero: delta negativo, resultado sem sentido.
        self._coleta("3.3.3.3", 100000.0, 90000.0, 100.0)
        d = self._coleta("3.3.3.3", 50.0, 40.0, 200.0)
        self.assertIsNone(d["sistema"]["cpu_pct"])

    def test_idle_por_nucleo_nao_publica(self):
        # Δidle > Δuptime (multicore) -> carga negativa -> não publica.
        self._coleta("4.4.4.4", 1000.0, 500.0, 100.0)
        d = self._coleta("4.4.4.4", 1100.0, 900.0, 200.0)
        self.assertIsNone(d["sistema"]["cpu_pct"])


# ══════════════════════════════════════════════════════════════════
# 11. publicar() nunca pode estourar
# ══════════════════════════════════════════════════════════════════
class TestPublicar(unittest.TestCase):
    """prometheus_client faz float(valor): .set(None) levanta TypeError.
    Como _coletar_bc captura Exception, um único campo ausente marcava o
    BreadCrumb inteiro como OFFLINE."""

    def test_publica_state_completo(self):
        est = rm.EstadoBC(3, 300)
        est.ok({}, 1.0)
        rm.publicar("10.188.96.140", rm.parse_state(STATE), est)

    def test_publica_state_minimo_sem_estourar(self):
        # Sem GPS, sem bateria, sem alertas, sem CPU: tudo None.
        est = rm.EstadoBC(3, 300)
        est.ok({}, 1.0)
        minimo = """
system {
  platform: "ES1"
  uptime: 1000.0
  freeMemory: 65536
  temperature: 40
}
wired {
  name: "eth0"
  aptState: APT_STATE_NONE
  stats { rxBytes: 1 rxPackets: 1 txBytes: 1 txPackets: 1 }
}
"""
        rm.publicar("10.0.0.9", rm.parse_state(minimo), est)

    def test_pub_ignora_none(self):
        from prometheus_client import Gauge
        g = Gauge("teste_pub_none", "t", ["bc","ip"])
        est = rm.EstadoBC(3, 300); est.ok({}, 1.0)
        # publicar() define pub() internamente; aqui validamos o contrato
        # diretamente: .set(None) tem de estourar, e é isso que pub() evita.
        with self.assertRaises(TypeError):
            g.labels(bc="x", ip="y").set(None)


# ══════════════════════════════════════════════════════════════════
# 12. Regressões de robustez já existentes
# ══════════════════════════════════════════════════════════════════
class TestRobustez(unittest.TestCase):

    def test_nmea_para_graus(self):
        self.assertAlmostEqual(rm.nmea_para_graus("2743.8950S"), -27.7316, places=3)
        self.assertAlmostEqual(rm.nmea_para_graus("05004.1429W"), -50.0690, places=3)
        self.assertIsNone(rm.nmea_para_graus(""))

    def test_state_vazio_nao_estoura(self):
        d = rm.parse_state("")
        self.assertEqual(d["radios"], [])
        self.assertEqual(d["ethernet"], [])
        self.assertFalse(d["sistema"]["apt_master"])

    def test_porta_sem_nome_recebe_rotulo(self):
        # label 'porta' vazio some do Grafana.
        st = 'wired {\n  aptState: APT_STATE_LINK\n  stats { rxBytes: 5 txBytes: 5 }\n}\n'
        self.assertEqual(rm.parse_state(st)["ethernet"][0]["nome"], "eth0")

    def test_extrair_blocos_aninhados(self):
        txt = "a {\n  b {\n    c: 1\n  }\n}\na {\n  d: 2\n}\n"
        self.assertEqual(len(rm.extrair_blocos(txt, 'a')), 2)

    def test_peers_ips(self):
        d = rm.parse_state(STATE)
        self.assertIn("10.188.96.141", d["peers_ips"])


# ══════════════════════════════════════════════════════════════════
# 13. SITE SURVEY CONTÍNUO — CSV a partir do Prometheus
# ══════════════════════════════════════════════════════════════════
class PromFalso:
    """Prometheus de mentira: devolve séries conforme a consulta pedida."""

    def __init__(self, series):
        self.series = series
        self.consultas = []

    def range(self, q, ini, fim, passo):
        self.consultas.append(q)
        for chave, resultado in self.series.items():
            if chave in q:
                return resultado
        return []


def _serie(rotulos, pares):
    return [{"metric": rotulos,
             "values": [[ts, str(v)] for ts, v in pares]}]


class TestSurveyPrometheus(unittest.TestCase):
    """A frota móvel como enxame de sondas. Só funciona porque a auditoria
    consertou SNR, velocidade e rumo — os três eram 0 ou negativos."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        bc = {"bc": "CA-4007"}
        bcf = {"bc": "CA-4007", "freq": "5GHz"}
        self.series = {
            "rajant_gps_lat":       _serie(bc,  [(100, -27.7316), (160, -27.7320)]),
            "rajant_gps_lon":       _serie(bc,  [(100, -50.0690), (160, -50.0695)]),
            "rajant_gps_fix":       _serie(bc,  [(100, 1), (160, 1)]),
            "rajant_gps_vel_kmh":   _serie(bc,  [(100, 24.0), (160, 26.0)]),
            "rajant_gps_rumo_graus": _serie(bc, [(100, 187.3), (160, 190.1)]),
            "rajant_im_perda_pct":  _serie(bc,  [(100, 0.4), (160, 0.6)]),
            "rajant_ping_rtt_ms":   _serie(bc,  [(100, 8.1), (160, 9.2)]),
            "rajant_peer_snr_db":   _serie(bcf, [(100, 37.0), (160, 22.0)]),
            "rajant_peer_sinal_dbm": _serie(bcf, [(100, -58.0), (160, -71.0)]),
            "rajant_radio_ruido_dbm": _serie(bcf, [(100, -95.0), (160, -93.0)]),
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _gerar(self, **kw):
        destino = os.path.join(self.tmp, "survey.csv")
        info = rm.gerar_csv_survey_prometheus(
            PromFalso(self.series), 0, 1000, 60, destino, **kw)
        with open(destino, newline="", encoding="utf-8") as f:
            return info, list(csv.DictReader(f))

    def test_gera_linhas_com_rf_e_posicao(self):
        info, linhas = self._gerar()
        self.assertEqual(info["linhas"], 2)
        self.assertEqual(linhas[0]["lat"], "-27.7316")
        self.assertEqual(linhas[0]["snr"], "37.0")
        self.assertEqual(linhas[0]["banda"], "5GHz")

    def test_colunas_batem_com_o_schema_do_survey(self):
        # O CSV precisa ser lido pelo mesmo parser da campanha manual.
        _, linhas = self._gerar()
        for c in ("lat", "lon", "snr", "rssi", "ruido", "perda",
                  "latencia", "banda"):
            self.assertIn(c, linhas[0])

    def test_csv_gerado_e_aceito_por_ler_csv_survey(self):
        info, _ = self._gerar()
        dados = rm.ler_csv_survey(info["caminho"])
        self.assertEqual(dados["_n"], 2)
        # as métricas reconhecidas alimentam os heatmaps existentes
        for m in ("snr", "rssi", "ruido"):
            self.assertIn(m, dados["_metricas"])

    def test_descarta_equipamento_parado(self):
        # Parado despeja amostras na mesma coordenada e enviesa a interpolação.
        self.series["rajant_gps_vel_kmh"] = _serie(
            {"bc": "CA-4007"}, [(100, 0.0), (160, 26.0)])
        info, linhas = self._gerar(vel_min=1.0)
        self.assertEqual(info["linhas"], 1)
        self.assertEqual(info["descartados"]["parado"], 1)

    def test_vel_min_zero_mantem_tudo(self):
        self.series["rajant_gps_vel_kmh"] = _serie(
            {"bc": "CA-4007"}, [(100, 0.0), (160, 0.0)])
        info, _ = self._gerar(vel_min=0)
        self.assertEqual(info["linhas"], 2)

    def test_descarta_sem_fix(self):
        self.series["rajant_gps_fix"] = _serie(
            {"bc": "CA-4007"}, [(100, 0), (160, 1)])
        info, _ = self._gerar()
        self.assertEqual(info["descartados"]["sem_fix"], 1)
        self.assertEqual(info["linhas"], 1)

    def test_consulta_puxa_freq_com_group_left(self):
        # rajant_peer_* não tem o label freq; sem o join o CSV sai sem banda.
        prom = PromFalso(self.series)
        rm.gerar_csv_survey_prometheus(
            prom, 0, 1000, 60, os.path.join(self.tmp, "s.csv"))
        snr_q = [q for q in prom.consultas if "rajant_peer_snr_db" in q][0]
        self.assertIn("group_left(freq)", snr_q)
        self.assertIn("rajant_radio_canal * 0 + 1", snr_q)

    def test_agrega_por_grade(self):
        info, linhas = self._gerar(grade_m=100000.0)   # célula gigante
        self.assertEqual(info["linhas"], 1)
        self.assertEqual(linhas[0]["_amostras"], "2")

    def test_sem_gps_falha_com_mensagem_util(self):
        self.series["rajant_gps_lat"] = []
        self.series["rajant_gps_lon"] = []
        with self.assertRaises(RuntimeError) as ctx:
            self._gerar()
        self.assertIn("gpsSwitch", str(ctx.exception))

    def test_preserva_o_bc_que_estava_servindo(self):
        # `max by (bc, freq)` no PromQL descartava os labels peer e radio.
        # É o peer que diz qual BreadCrumb cobria aquele ponto.
        self.series["rajant_peer_snr_db"] = (
            _serie({"bc": "CA-4007", "freq": "5GHz", "radio": "wlan0",
                    "peer": "10.188.96.141"}, [(100, 37.0)])
            + _serie({"bc": "CA-4007", "freq": "5GHz", "radio": "wlan0",
                      "peer": "10.188.96.199"}, [(100, 12.0)]))
        _, linhas = self._gerar()
        # vence o enlace de melhor SNR, e o servidor vem junto
        self.assertEqual(linhas[0]["snr"], "37.0")
        self.assertEqual(linhas[0]["servidor"], "10.188.96.141")
        self.assertEqual(linhas[0]["radio"], "wlan0")

    def test_handover_aparece_como_troca_de_servidor(self):
        self.series["rajant_peer_snr_db"] = (
            _serie({"bc": "CA-4007", "freq": "5GHz", "radio": "wlan0",
                    "peer": "BC-NORTE"}, [(100, 34.0), (160, 9.0)])
            + _serie({"bc": "CA-4007", "freq": "5GHz", "radio": "wlan0",
                      "peer": "BC-SUL"}, [(100, 11.0), (160, 31.0)]))
        _, linhas = self._gerar()
        self.assertEqual([l["servidor"] for l in linhas],
                         ["BC-NORTE", "BC-SUL"])

    def test_filtro_por_radio(self):
        self.series["rajant_peer_snr_db"] = (
            _serie({"bc": "CA-4007", "freq": "5GHz", "radio": "wlan0",
                    "peer": "p1"}, [(100, 40.0)])
            + _serie({"bc": "CA-4007", "freq": "5GHz", "radio": "wlan1",
                      "peer": "p2"}, [(100, 15.0)]))
        # sem filtro vence o wlan0 (melhor SNR)
        _, todos = self._gerar()
        self.assertEqual(todos[0]["radio"], "wlan0")
        # com filtro, só o wlan1 entra
        _, so_wlan1 = self._gerar(radio="wlan1")
        self.assertEqual(so_wlan1[0]["radio"], "wlan1")
        self.assertEqual(so_wlan1[0]["snr"], "15.0")

    def test_filtro_de_radio_chega_na_consulta_de_ruido(self):
        prom = PromFalso(self.series)
        rm.gerar_csv_survey_prometheus(
            prom, 0, 1000, 60, os.path.join(self.tmp, "s.csv"), radio="wlan1")
        q = [c for c in prom.consultas if "rajant_radio_ruido_dbm" in c][0]
        self.assertIn('radio="wlan1"', q)

    def test_grade_usa_servidor_dominante(self):
        # Média não faz sentido para identificador: vale quem serviu mais.
        self.series["rajant_peer_snr_db"] = (
            _serie({"bc": "CA-4007", "freq": "5GHz", "peer": "BC-A"},
                   [(100, 30.0), (160, 30.0)]))
        info, linhas = self._gerar(grade_m=100000.0)
        self.assertEqual(info["linhas"], 1)
        self.assertEqual(linhas[0]["servidor"], "BC-A")
        self.assertEqual(linhas[0]["_servidores"], "1")

    def test_relatorio_lista_servidores_e_radios(self):
        self.series["rajant_peer_snr_db"] = (
            _serie({"bc": "CA-4007", "freq": "5GHz", "radio": "wlan0",
                    "peer": "BC-A"}, [(100, 30.0)])
            + _serie({"bc": "CA-4007", "freq": "5GHz", "radio": "wlan0",
                      "peer": "BC-B"}, [(160, 28.0)]))
        info, _ = self._gerar()
        self.assertEqual(info["servidores"], ["BC-A", "BC-B"])
        self.assertEqual(info["radios"], ["wlan0"])

    def test_csv_com_servidor_ainda_e_lido_pelo_parser_de_survey(self):
        # As colunas novas não podem quebrar o pipeline existente.
        info, _ = self._gerar()
        dados = rm.ler_csv_survey(info["caminho"])
        self.assertEqual(dados["_n"], 2)
        self.assertIn("snr", dados["_metricas"])

    def test_tudo_descartado_falha_explicando(self):
        self.series["rajant_gps_vel_kmh"] = _serie(
            {"bc": "CA-4007"}, [(100, 0.0), (160, 0.0)])
        with self.assertRaises(RuntimeError) as ctx:
            self._gerar(vel_min=5.0)
        self.assertIn("survey-vel-min", str(ctx.exception))


# ══════════════════════════════════════════════════════════════════
# 14. COLETA ACELERADA DOS MÓVEIS
# ══════════════════════════════════════════════════════════════════
class TestIntervaloMoveis(unittest.TestCase):
    """A resolução do survey é velocidade × intervalo. Coletar os móveis
    mais rápido melhora o mapa sem multiplicar o volume da frota toda."""

    PADRAO = "^CA ; ^PA ; ^PF ; ^TT ; ^EH ; CAMINH ; ESCAV ; PERFURA"

    def _coletor(self, interval=60, moveis=20, padrao=None):
        c = rm.RajantCollector(
            seeds=[], role="VIEW", password="", port=2300, interval=interval,
            max_threads=1, timeout=1, tentativas=1, falhas_limite=3,
            manter_s=300, redesc_ciclos=10, falhas_remover=20,
            cache=rm.CacheIPs(os.path.join(tempfile.mkdtemp(), "c.json")),
            interval_moveis=moveis,
            padrao_movel=self.PADRAO if padrao is None else padrao)
        return c

    def test_passo_dos_fixos(self):
        c = self._coletor(interval=60, moveis=20)
        self.assertEqual(c.interval_moveis, 20)
        self.assertEqual(c.passo_fixos, 3)      # fixos a cada 3 voltas = 60s

    def test_classifica_moveis_pelo_nome(self):
        c = self._coletor()
        c.nomes = {"1.1.1.1": "CA-4007", "2.2.2.2": "BACKBONE-ANEL-N",
                   "3.3.3.3": "ESCAV-12", "4.4.4.4": "ERB-005"}
        self.assertTrue(c._eh_movel("1.1.1.1"))
        self.assertFalse(c._eh_movel("2.2.2.2"))
        self.assertTrue(c._eh_movel("3.3.3.3"))
        self.assertFalse(c._eh_movel("4.4.4.4"))

    def test_nome_desconhecido_conta_como_movel(self):
        # Não deixa equipamento recém-descoberto fora do primeiro ciclo.
        c = self._coletor()
        self.assertTrue(c._eh_movel("9.9.9.9"))

    def test_desligado_por_padrao(self):
        c = self._coletor(moveis=0)
        self.assertEqual(c.interval_moveis, 0)
        self.assertEqual(c.passo_fixos, 1)
        self.assertIsNone(c.re_movel)
        # sem aceleração, todo BC entra em todo ciclo
        self.assertTrue(c._eh_movel("qualquer"))

    def test_ignora_intervalo_maior_que_o_geral(self):
        # 90s de "móveis" com 60s geral não faz sentido — desliga.
        c = self._coletor(interval=60, moveis=90)
        self.assertEqual(c.interval_moveis, 0)

    def test_padrao_invalido_nao_derruba_o_coletor(self):
        c = self._coletor(padrao="[nao-fecha")
        self.assertIsNone(c.re_movel)
        self.assertEqual(c.interval_moveis, 0)

    def test_sem_padrao_desliga(self):
        c = self._coletor(padrao="")
        self.assertEqual(c.interval_moveis, 0)


# ══════════════════════════════════════════════════════════════════
# 15. SURVEY ANEXADO AO PPT E FORMULÁRIO WEB
# ══════════════════════════════════════════════════════════════════
class TestSurveyNoPPT(unittest.TestCase):
    """O survey entra no PPT com período e rádio PRÓPRIOS: o relatório cobre
    a semana, mas a análise de cobertura interessa num turno."""

    def test_pagina_tem_as_quatro_abas(self):
        html = rm.PAGINA_HTML
        for aba in ("relatorios", "survey", "historico", "medicoes"):
            self.assertIn(f'data-p="{aba}"', html, f"falta a aba {aba}")

    def test_pagina_tem_os_campos_do_survey(self):
        html = rm.PAGINA_HTML
        for campo in ("tags", "colar", "busca", "soGps", "soOnline", "perfil",
                      "lista", "resumo", "svNome", "minutos", "intervalo",
                      "alcance", "btnIniciar", "btnParar"):
            self.assertIn(f'id="{campo}"', html, f"falta o campo {campo}")

    def test_pagina_tem_presets_de_duracao(self):
        # 15 min · 1 h · turno (8 h) · 24 h
        html = rm.PAGINA_HTML
        for m in ("15", "60", "480", "1440"):
            self.assertIn(f'data-m="{m}"', html)

    def test_pagina_avisa_da_carga_na_malha(self):
        # A captura consulta os rádios direto, no intervalo pedido: o ciclo
        # do exporter deixou de limitar a resolução. O que o operador
        # precisa saber agora é quanta consulta isso põe na malha.
        html = rm.PAGINA_HTML
        self.assertIn("consultas/s", html)
        self.assertIn("Carga alta na malha", html)
        self.assertNotIn("a resolução real será de", html,
                         "aviso do ciclo do exporter ficou para trás")

    def test_acoes_do_historico_ficam_fixas_a_direita(self):
        # A tabela tem 10 colunas e rola; sem a coluna fixa os botões
        # saíam da tela e "Google Earth" aparecia cortado como "Go".
        html = rm.PAGINA_HTML
        self.assertIn("#tabHist td:last-child", html)
        self.assertIn("position:sticky; right:0", html)

    def test_pagina_oferece_o_kml(self):
        html = rm.PAGINA_HTML
        self.assertIn("surveyKml:", html)
        self.assertIn('data-a="kml"', html)
        self.assertIn('id="campoKml"', html)

    def test_pagina_mostra_intervalo_efetivo(self):
        # Ciclo que estoura o intervalo tem de aparecer na tela: é a
        # resolução real do trajeto, não a pedida.
        self.assertIn("intervalo_efetivo_s", rm.PAGINA_HTML)
        self.assertIn("intervalo efetivo:", rm.PAGINA_HTML)

    def test_adaptador_de_rotas_unico(self):
        # Nenhuma URL literal espalhada: tudo passa pelo objeto API.
        html = rm.PAGINA_HTML
        self.assertIn("const API = {", html)
        for rota in ("capturaIniciar", "surveys", "medImportar", "medModelo"):
            self.assertIn(rota + ":", html)

    def test_survey_em_uso_vai_para_o_relatorio(self):
        # O vínculo survey→relatório agora é o "Usar no relatório" do
        # histórico, não um formulário separado.
        html = rm.PAGINA_HTML
        self.assertIn("p.survey_id = SV_EM_USO", html)
        self.assertIn("svEmUso", html)

    def test_pagina_sem_chaves_duplas(self):
        # A página é servida como string crua; `{{` quebraria o CSS/JS.
        self.assertNotIn("{{", rm.PAGINA_HTML)
        self.assertNotIn("}}", rm.PAGINA_HTML)

    def test_pagina_e_raw_string(self):
        # Sem raw string o Python interpreta o \n da regex do split e ela
        # chega quebrada em duas linhas ao navegador.
        fonte = open(rm.__file__, encoding="utf-8").read()
        self.assertIn('PAGINA_HTML = r"""', fonte)
        self.assertIn(r"split(/[,;\n\r]+/)", rm.PAGINA_HTML)

    def test_pagina_sem_dependencia_externa(self):
        baixo = rm.PAGINA_HTML.lower()
        for termo in ("cdn.", "googleapis", "unpkg", "jsdelivr"):
            self.assertNotIn(termo, baixo)

    def test_endpoint_radios_declarado(self):
        self.assertIn('parsed.path == "/radios"',
                      open(rm.__file__, encoding="utf-8").read())

    def test_gerar_ppt_aceita_survey(self):
        import inspect
        sig = inspect.signature(rm.gerar_ppt)
        self.assertIn("survey", sig.parameters)
        self.assertIsNone(sig.parameters["survey"].default)

    def test_anexar_exige_periodo(self):
        with self.assertRaises(RuntimeError) as ctx:
            rm.anexar_survey_ao_ppt(None, None, {"radio": "wlan0"})
        self.assertIn("periodo", str(ctx.exception))

    def test_falha_no_survey_nao_derruba_o_ppt(self):
        # Sem Prometheus o survey falha; o PPT tem de sair mesmo assim,
        # sem o sufixo _com_Survey e com o motivo no log.
        fonte = inspect.getsource(rm.gerar_ppt)
        self.assertIn("except Exception", fonte)
        self.assertIn("survey nao anexado", fonte)
        # e o sufixo só entra quando slides foram realmente criados
        self.assertIn('sufixo_sv = "_com_Survey"', fonte)


class TestParametrosSurveyWeb(unittest.TestCase):
    """Validação dos parâmetros que a página envia para /gerar."""

    def _parse(self, txt):
        """Replica o parser de data/hora do endpoint."""
        from datetime import datetime as _dt
        for f in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try: return _dt.strptime(txt, f)
            except ValueError: pass
        raise ValueError(txt)

    def test_aceita_datetime_local_do_navegador(self):
        # <input type="datetime-local"> manda 2026-07-15T06:00
        d = self._parse("2026-07-15T06:00")
        self.assertEqual((d.hour, d.minute), (6, 0))

    def test_aceita_data_pura(self):
        d = self._parse("2026-07-15")
        self.assertEqual(d.hour, 0)

    def test_rejeita_lixo(self):
        with self.assertRaises(ValueError):
            self._parse("15/07/2026")


# ══════════════════════════════════════════════════════════════════
# 16. PAINEL HTML PRÓPRIO
# ══════════════════════════════════════════════════════════════════
class TestPainelHTML(unittest.TestCase):
    """Painel em HTML/CSS/JS servido em /painel, com proxy /api/q para o
    Prometheus (evita CORS e mantém a URL no servidor)."""

    def setUp(self):
        self.cfg = rm.configparser.ConfigParser()
        self.cfg.add_section("relatorio")

    def test_arquivo_do_painel_existe(self):
        p = pathlib.Path(rm.__file__).parent / "painel" / "visao-geral.html"
        self.assertTrue(p.exists(), "painel/visao-geral.html nao encontrado")
        html = p.read_text(encoding="utf-8")
        self.assertIn("<!DOCTYPE html>", html)
        # sem dependência externa: nada de CDN nem fonte remota
        self.assertNotIn("http://cdn", html.lower())
        self.assertNotIn("https://cdn", html.lower())
        self.assertNotIn("googleapis", html.lower())

    def test_painel_so_consulta_metricas_que_existem(self):
        p = pathlib.Path(rm.__file__).parent / "painel" / "visao-geral.html"
        html = p.read_text(encoding="utf-8")
        publicadas = set(re.findall(r'Gauge\("([a-z0-9_]+)"',
                                    open(rm.__file__, encoding="utf-8").read()))
        usadas = set(re.findall(r'\brajant_[a-z0-9_]+\b', html))
        for m in sorted(usadas):
            self.assertIn(m, publicadas,
                          f"{m} nao e publicada pelo exporter")

    def test_painel_nao_usa_metrica_removida(self):
        p = pathlib.Path(rm.__file__).parent / "painel" / "visao-geral.html"
        html = p.read_text(encoding="utf-8")
        removidas = {"rajant_voltagem_v", "rajant_bateria_v",
                     "rajant_session_state", "rajant_im_overflows",
                     "rajant_radio_phy_erros", "rajant_peer_txpower_dbm",
                     "rajant_eth_rx_erros", "rajant_eth_mudancas"}
        for m in removidas:
            self.assertNotIn(m, html, f"{m} foi removida na auditoria")

    def test_painel_html_lido_do_disco(self):
        # Editar o arquivo e recarregar o navegador basta — sem reiniciar.
        self.cfg.set("relatorio", "painel_html", "painel/visao-geral.html")
        html = rm.painel_html(self.cfg)
        self.assertIn("Visão Geral da Malha", html)

    def test_painel_ausente_da_mensagem_util(self):
        self.cfg.set("relatorio", "painel_html", "nao/existe.html")
        html = rm.painel_html(self.cfg)
        self.assertIn("não encontrado", html)
        self.assertIn("nao/existe.html", html)

    def test_proxy_api_q_declarado(self):
        fonte = open(rm.__file__, encoding="utf-8").read()
        self.assertIn('parsed.path == "/api/q"', fonte)
        # a janela e o passo são limitados, para não derrubar o Prometheus
        compacto = fonte.replace(" ", "")
        self.assertIn("min(janela,90*86400)", compacto)
        self.assertIn("min(passo,3600)", compacto)

    def test_rota_painel_declarada(self):
        fonte = open(rm.__file__, encoding="utf-8").read()
        self.assertIn('("/painel", "/painel.html")', fonte)


# ══════════════════════════════════════════════════════════════════
# 17. PERSISTÊNCIA DOS SURVEYS
# ══════════════════════════════════════════════════════════════════
class TestPersistenciaSurvey(unittest.TestCase):
    """Sem persistir, a comparação com a semana anterior (colunas
    'Semana anterior' e 'Tendência' do PPT) é impossível."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.con = rm.banco(os.path.join(self.tmp, "t.db"))

    def tearDown(self):
        self.con.close(); shutil.rmtree(self.tmp, ignore_errors=True)

    def _survey(self, nome="S1", t0=1000.0):
        return rm.survey_criar(self.con, nome, t0, 30, ["CA-1010", "ERM-20"])

    def test_cria_e_lista(self):
        sid = self._survey()
        lst = rm.survey_listar(self.con)
        self.assertEqual(len(lst), 1)
        self.assertEqual(lst[0]["nome"], "S1")
        self.assertEqual(json.loads(lst[0]["radios"]), ["CA-1010", "ERM-20"])

    def test_grava_amostras_e_conta(self):
        sid = self._survey()
        rm.amostras_gravar(self.con, sid, [
            {"radio": "CA-1010", "ts": 1, "lat": -27.7, "lon": -50.0, "snr": 30},
            {"radio": "ERM-20", "ts": 2, "lat": -27.7, "lon": -50.0, "snr": 25}])
        lst = rm.survey_listar(self.con)[0]
        self.assertEqual(lst["n_amostras"], 2)
        self.assertEqual(lst["n_radios"], 2)

    def test_campo_ausente_vira_null_nao_zero(self):
        # Zero seria lido como "medido e deu zero".
        sid = self._survey()
        rm.amostras_gravar(self.con, sid, [
            {"radio": "CA-1010", "ts": 1, "lat": -27.7, "lon": -50.0}])
        a = rm.survey_amostras(self.con, sid)[0]
        for campo in ("snr", "sinal", "rtt", "perda", "vazao", "custo"):
            self.assertIsNone(a[campo], f"{campo} deveria ser NULL")

    def test_excluir_leva_as_amostras(self):
        sid = self._survey()
        rm.amostras_gravar(self.con, sid, [{"radio": "X", "ts": 1}])
        rm.survey_excluir(self.con, sid)
        self.assertEqual(rm.survey_listar(self.con), [])
        self.assertEqual(rm.survey_amostras(self.con, sid), [])

    def test_survey_anterior_para_tendencia(self):
        s1 = self._survey("Semana 1", 1000.0)
        rm.survey_fechar(self.con, s1, 2000.0, {"amostras": 10})
        s2 = self._survey("Semana 2", 5000.0)
        ant = rm.survey_anterior(self.con, s2)
        self.assertIsNotNone(ant)
        self.assertEqual(ant["nome"], "Semana 1")

    def test_sem_anterior_devolve_none(self):
        s1 = self._survey("Primeiro", 1000.0)
        self.assertIsNone(rm.survey_anterior(self.con, s1))

    def test_medicoes_manuais(self):
        sid = self._survey()
        rm.manual_gravar(self.con, sid, "iperf",
                         {"data": "2026-05-15", "local": "Praça 3",
                          "banda": "5.8 GHz", "mbps": 42.5})
        rm.manual_gravar(self.con, sid, "trace",
                         {"origem": "CA-1010", "destino": "CORE-01",
                          "saltos": 4, "custo_total": 480, "gargalo": "ERM-20"})
        self.assertEqual(len(rm.manual_listar(self.con, sid)), 2)
        self.assertEqual(len(rm.manual_listar(self.con, sid, "iperf")), 1)
        self.assertEqual(rm.manual_listar(self.con, sid, "iperf")[0]["mbps"], 42.5)


class TestResumoSurvey(unittest.TestCase):
    """Percentual dentro de cada requisito Modular Mining."""

    def test_pct_dentro_de_cada_requisito(self):
        am = [{"radio": "A", "sinal": -60, "snr": 30, "rtt": 8,  "perda": 0.5},
              {"radio": "A", "sinal": -80, "snr": 15, "rtt": 150,"perda": 5.0},
              {"radio": "B", "sinal": -70, "snr": 25, "rtt": 20, "perda": 1.0}]
        r = rm.survey_resumo(am)
        self.assertEqual(r["amostras"], 3)
        self.assertEqual(r["radios"], 2)
        # RSSI > -75: dois de três
        self.assertAlmostEqual(r["sinal"]["pct_ok"], 66.7, places=1)
        self.assertAlmostEqual(r["snr"]["pct_ok"], 66.7, places=1)
        self.assertAlmostEqual(r["rtt"]["pct_ok"], 66.7, places=1)
        self.assertAlmostEqual(r["perda"]["pct_ok"], 66.7, places=1)

    def test_campo_sem_amostra_e_none_nao_zero_pct(self):
        # 0 % de aprovação e "não medido" são coisas diferentes.
        r = rm.survey_resumo([{"radio": "A", "sinal": -60}])
        self.assertIsNone(r["rtt"])
        self.assertIsNone(r["vazao"])
        self.assertIsNotNone(r["sinal"])

    def test_limiares_sao_os_da_modular(self):
        self.assertEqual(rm.REQUISITOS["sinal"][1], -75.0)
        self.assertEqual(rm.REQUISITOS["snr"][1],    20.0)
        self.assertEqual(rm.REQUISITOS["rtt"][1],   100.0)
        self.assertEqual(rm.REQUISITOS["perda"][1],   2.0)


# ══════════════════════════════════════════════════════════════════
# 18. CALIBRAÇÃO DA PROPAGAÇÃO
# ══════════════════════════════════════════════════════════════════
class TestCalibracao(unittest.TestCase):
    """RSSI = A - 10n·log10(d), ajustado aos dados medidos e não ao
    catálogo — é o que corrige as manchas grandes demais no mapa."""

    FIXOS = {"ERM-20": (-27.7300, -50.0700)}

    def _amostras(self, A, n, banda="5.8 GHz", pontos=300, ruido=0.0,
                  d0=100, passo=4.7):
        import math, random
        random.seed(4)
        out = []
        for i in range(pontos):
            d = d0 + i * passo
            out.append({"banda": banda, "lat": -27.7300 + d / 111320.0,
                        "lon": -50.0700,
                        "sinal": A - 10 * n * math.log10(d)
                                 + (random.gauss(0, ruido) if ruido else 0)})
        return out

    def test_recupera_os_parametros(self):
        c = rm.calibrar_banda(self._amostras(-4.0, 2.55), self.FIXOS, "5.8 GHz")
        self.assertTrue(c["ok"])
        self.assertAlmostEqual(c["A"], -4.0, delta=0.5)
        self.assertAlmostEqual(c["n"], 2.55, delta=0.05)

    def test_alcance_e_onde_cruza_menos_85(self):
        # A=-4, n=2.55 -> -85 dBm em ~1500 m
        c = rm.calibrar_banda(self._amostras(-4.0, 2.55), self.FIXOS, "5.8 GHz")
        self.assertAlmostEqual(c["alcance_m"], 1500, delta=120)

    def test_rejeita_poucos_pontos(self):
        c = rm.calibrar_banda(self._amostras(-4.0, 2.55, pontos=10),
                              self.FIXOS, "5.8 GHz")
        self.assertFalse(c["ok"])
        self.assertIn("insuficientes", c["motivo"])

    def test_rejeita_expoente_implausivel(self):
        for n in (1.2, 5.5):
            c = rm.calibrar_banda(self._amostras(-40.0, n), self.FIXOS, "5.8 GHz")
            self.assertFalse(c["ok"], f"n={n} deveria ser rejeitado")
            self.assertIn("implausível", c["motivo"])

    def test_sem_repetidora_nao_calibra(self):
        c = rm.calibrar_banda(self._amostras(-4.0, 2.55), {}, "5.8 GHz")
        self.assertFalse(c["ok"])

    def test_bandas_calibram_separado(self):
        # 2,4 e 5,8 atenuam diferente; um n único não serve para as duas.
        am = (self._amostras(-4.0, 2.2, "2.4 GHz")
              + self._amostras(-4.0, 3.1, "5.8 GHz"))
        t = rm.calibrar_todas_bandas(am, self.FIXOS)
        self.assertTrue(t["2.4 GHz"]["ok"]); self.assertTrue(t["5.8 GHz"]["ok"])
        self.assertAlmostEqual(t["2.4 GHz"]["n"], 2.2, delta=0.1)
        self.assertAlmostEqual(t["5.8 GHz"]["n"], 3.1, delta=0.1)

    def test_banda_sem_dado_cai_no_padrao_com_motivo(self):
        t = rm.calibrar_todas_bandas(self._amostras(-4.0, 2.55), self.FIXOS)
        self.assertTrue(t["2.4 GHz"].get("padrao"))
        self.assertIn("motivo", t["2.4 GHz"])
        # o padrão é conservador: melhor mancha menor que promessa falsa
        self.assertLessEqual(t["2.4 GHz"]["alcance_m"], 1000)

    def test_descarta_perto_e_longe_demais(self):
        # < 20 m (campo próximo) e > 6 km (quase certo que veio de outro rádio)
        am = self._amostras(-4.0, 2.55, pontos=40, d0=1, passo=0.3)
        c = rm.calibrar_banda(am, self.FIXOS, "5.8 GHz")
        self.assertFalse(c["ok"])

    def test_normaliza_nome_da_banda(self):
        self.assertEqual(rm._norm_banda("5GHz"),    "5.8 GHz")
        self.assertEqual(rm._norm_banda("5.8 GHz"), "5.8 GHz")
        self.assertEqual(rm._norm_banda("2.4GHz"),  "2.4 GHz")
        self.assertEqual(rm._norm_banda("2,4 GHz"), "2.4 GHz")
        self.assertIsNone(rm._norm_banda(""))


# ══════════════════════════════════════════════════════════════════
# 19. FUNDO DE SATÉLITE E BBOX
# ══════════════════════════════════════════════════════════════════
class TestFundoEBbox(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cwd = os.getcwd(); os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.cwd); shutil.rmtree(self.tmp, ignore_errors=True)

    BBOX = {"norte": -27.72, "sul": -27.74, "leste": -50.05, "oeste": -50.08}

    def test_bbox_invalido_recusado(self):
        r, motivo = rm.fundo_satelite({"norte": -27.74, "sul": -27.72,
                                       "leste": -50.08, "oeste": -50.05})
        self.assertIsNone(r); self.assertIn("inválido", motivo)

    def test_usa_cache_sem_baixar(self):
        import hashlib, pathlib
        ch = hashlib.md5(f"{-50.08:.6f},{-27.74:.6f},{-50.05:.6f},"
                         f"{-27.72:.6f},1600x1200".encode()).hexdigest()[:16]
        p = pathlib.Path(rm.FUNDO_CACHE_DIR); p.mkdir(exist_ok=True)
        (p / f"sat_{ch}.png").write_bytes(b"\x89PNG" + b"x" * 4000)
        caminho, credito = rm.fundo_satelite(self.BBOX)
        self.assertIsNotNone(caminho)
        self.assertIn("Esri", credito)

    def _cfg(self, **kv):
        cfg = rm.configparser.ConfigParser(); cfg.add_section("relatorio")
        for k, v in kv.items():
            cfg.set("relatorio", k, v)
        return cfg

    def test_fallback_para_ortofoto_local(self):
        pathlib.Path("orto.png").write_bytes(b"\x89PNG" + b"y" * 100)
        cfg = self._cfg(fundo_local="orto.png",
                        fundo_bbox="-27.72,-27.74,-50.05,-50.08")
        caminho, ext, credito, aviso = rm.resolver_fundo(self.BBOX, cfg)
        self.assertEqual(caminho, "orto.png")
        self.assertIn("Ortofoto", credito)
        self.assertIsNotNone(aviso, "o aviso precisa chegar ao rodapé da imagem")
        self.assertEqual(ext["norte"], -27.72)

    def test_ortofoto_local_sem_bbox_e_recusada(self):
        # Sem georreferência a imagem seria esticada até o retângulo dos
        # dados: mapa bonito e errado, que é pior que mapa sem fundo.
        pathlib.Path("orto.png").write_bytes(b"\x89PNG" + b"y" * 100)
        caminho, ext, credito, aviso = rm.resolver_fundo(
            self.BBOX, self._cfg(fundo_local="orto.png"))
        self.assertIsNone(caminho)
        self.assertIn("fundo_bbox", aviso)

    def test_bbox_cfg_recusa_retangulo_impossivel(self):
        # norte ao sul do sul: alguém trocou a ordem dos campos.
        self.assertIsNone(rm._bbox_cfg(
            self._cfg(fundo_bbox="-27.74,-27.72,-50.05,-50.08")))
        self.assertIsNone(rm._bbox_cfg(self._cfg(fundo_bbox="-27.72,-27.74")))
        self.assertIsNone(rm._bbox_cfg(self._cfg()))

    def test_kmz_tem_prioridade_e_traz_a_propria_extensao(self):
        # GroundOverlay já vem georreferenciado pelo LatLonBox — é o único
        # caminho em que a mina não precisa medir nada à mão.
        pathlib.Path("orto.png").write_bytes(b"\x89PNG" + b"y" * 100)
        kmz = self._kmz("mina.kmz")
        caminho, ext, credito, aviso = rm.resolver_fundo(
            self.BBOX, self._cfg(fundo_local="orto.png",
                                 fundo_bbox="-27.72,-27.74,-50.05,-50.08"),
            kmz=kmz)
        self.assertTrue(caminho.endswith("cava.png"))
        self.assertIn("KMZ", credito)
        self.assertEqual(ext, {"norte": -27.725, "sul": -27.740,
                               "leste": -50.058, "oeste": -50.076})

    def test_kmz_sem_groundoverlay_cai_para_o_proximo(self):
        kmz = self._kmz("pontos.kmz", overlay=False)
        img, ext, motivo = rm.fundo_do_kmz(kmz)
        self.assertIsNone(img); self.assertIsNotNone(motivo)

    def _kmz(self, nome, overlay=True):
        import zipfile
        box = ("<GroundOverlay><Icon><href>cava.png</href></Icon><LatLonBox>"
               "<north>-27.7250</north><south>-27.7400</south>"
               "<east>-50.0580</east><west>-50.0760</west>"
               "</LatLonBox></GroundOverlay>") if overlay else \
              "<Placemark><name>x</name></Placemark>"
        with zipfile.ZipFile(nome, "w") as z:
            z.writestr("doc.kml", '<?xml version="1.0"?><kml><Document>'
                       + box + "</Document></kml>")
            z.writestr("cava.png", b"\x89PNG" + b"z" * 500)
        return nome

    def test_sem_fundo_nao_levanta(self):
        # Mapa sem imagem ainda é útil; survey que falha não é.
        caminho, ext, credito, aviso = rm.resolver_fundo(self.BBOX)
        self.assertIsNone(caminho); self.assertIsNotNone(aviso)

    def test_bbox_descarta_hemisferio_invertido(self):
        # KML do cliente vem com longitude de sinal trocado; um ponto
        # desses estica a área por milhares de km.
        pts = [(-27.73, -50.07), (-27.74, -50.06), (-27.72, -50.08),
               (-27.73, +50.07)]
        b = rm._bbox_de(pts)
        self.assertLess(b["leste"], 0, "ponto com longitude positiva passou")
        self.assertLess(abs(b["leste"] - b["oeste"]), 1.0)

    def test_distancia_em_metros(self):
        # 0,001 grau de latitude ≈ 111 m
        d = rm._dist_m(-27.7300, -50.0700, -27.7310, -50.0700)
        self.assertAlmostEqual(d, 111.0, delta=3)


# ══════════════════════════════════════════════════════════════════
# 20. COMPARAÇÃO ENTRE SURVEYS E MEDIÇÕES MANUAIS
# ══════════════════════════════════════════════════════════════════
class TestComparacao(unittest.TestCase):
    """Alimenta as colunas 'Semana anterior' e 'Tendência' do PPT."""

    def _res(self, sinal_ok, snr_ok=50.0):
        return {"amostras": 100, "radios": 4,
                "sinal": {"pct_ok": sinal_ok, "rotulo": "RSSI",
                          "unidade": "dBm", "limite": -75.0},
                "snr":   {"pct_ok": snr_ok, "rotulo": "SNR",
                          "unidade": "dB", "limite": 20.0}}

    def test_melhora_e_piora(self):
        c = rm.comparar_surveys(self._res(90.0), self._res(60.0))
        self.assertEqual(c["sinal"]["delta"], 30.0)
        self.assertEqual(c["sinal"]["direcao"], "melhorou")
        c2 = rm.comparar_surveys(self._res(60.0), self._res(90.0))
        self.assertEqual(c2["sinal"]["direcao"], "piorou")

    def test_variacao_pequena_e_estavel(self):
        c = rm.comparar_surveys(self._res(90.0), self._res(90.2))
        self.assertEqual(c["sinal"]["direcao"], "estável")

    def test_sem_anterior_deixa_em_branco(self):
        # Coluna vazia, não zero: não houve semana anterior.
        c = rm.comparar_surveys(self._res(90.0), None)
        self.assertIsNone(c["sinal"]["antes"])
        self.assertIsNone(c["sinal"]["delta"])
        self.assertIsNone(c["sinal"]["direcao"])

    def test_campo_nao_medido_agora(self):
        atual = {"amostras": 10, "radios": 1, "sinal": None}
        c = rm.comparar_surveys(atual, self._res(80.0))
        self.assertIsNone(c["sinal"]["agora"])
        self.assertIsNone(c["sinal"]["delta"])

    def test_setas(self):
        self.assertEqual(rm.SETA["melhorou"], "▲")
        self.assertEqual(rm.SETA["piorou"],   "▼")
        self.assertEqual(rm.SETA[None],       "—")


class TestCSVManual(unittest.TestCase):
    """iperf e trace são lançados à mão: a BC API não mede throughput
    (as tarefas possíveis são só REBOOT/INSTALL/SNAPSHOT/ZEROIZE/FCC/
    CTM/TRACE/CLEAR/KICK)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.con = rm.banco(os.path.join(self.tmp, "t.db"))
        self.sid = rm.survey_criar(self.con, "S", 1000.0, 30, ["CA-1"])

    def tearDown(self):
        self.con.close(); shutil.rmtree(self.tmp, ignore_errors=True)

    def test_template_iperf_importa_as_duas_linhas(self):
        # O próprio template precisa passar pelo importador — se a linha
        # de exemplo estiver desalinhada, o usuário copia o erro.
        n, erros = rm.importar_csv_manual(self.con, self.sid, "iperf",
                                          rm.template_csv_manual("iperf"))
        self.assertEqual(n, 2, f"erros: {erros}")
        self.assertEqual(erros, [])

    def test_template_trace_importa(self):
        n, erros = rm.importar_csv_manual(self.con, self.sid, "trace",
                                          rm.template_csv_manual("trace"))
        self.assertEqual(n, 1); self.assertEqual(erros, [])

    def test_coordenada_e_opcional(self):
        rm.importar_csv_manual(self.con, self.sid, "iperf",
                               rm.template_csv_manual("iperf"))
        ms = rm.manual_listar(self.con, self.sid, "iperf")
        sem_coord = [m for m in ms if m["lat"] is None]
        self.assertEqual(len(sem_coord), 1)

    def test_linha_sem_throughput_e_reportada(self):
        csv_ = "data;local;lat;lon;banda;mbps;obs\n2026-05-15;X;;;5.8 GHz;;\n"
        n, erros = rm.importar_csv_manual(self.con, self.sid, "iperf", csv_)
        self.assertEqual(n, 0)
        self.assertTrue(any("sem throughput" in e for e in erros))

    def test_aceita_virgula_decimal(self):
        csv_ = "data;local;lat;lon;banda;mbps;obs\n2026-05-15;X;;;5.8 GHz;42,5;\n"
        rm.importar_csv_manual(self.con, self.sid, "iperf", csv_)
        self.assertEqual(rm.manual_listar(self.con, self.sid)[0]["mbps"], 42.5)

    def test_csv_sem_coluna_obrigatoria(self):
        n, erros = rm.importar_csv_manual(self.con, self.sid, "iperf",
                                          "foo;bar\n1;2\n")
        self.assertEqual(n, 0)
        self.assertTrue(any("obrigatória" in e for e in erros))


class TestSelecaoPorTag(unittest.TestCase):
    """150 nós: checkbox individual não escala. A seleção é por prefixo
    da tag do rádio, não por EQMPTID do Dispatch."""

    NOMES = ["CA-1010", "CA-1042", "PF-1803", "ERM-20", "ERM-31",
             "ERB-04", "EH-220"]

    def test_casa_por_prefixo(self):
        casados, faltando = rm.casar_tags(self.NOMES, ["CA", "ERM"])
        self.assertEqual(casados, ["CA-1010", "CA-1042", "ERM-20", "ERM-31"])
        self.assertEqual(faltando, [])

    def test_casa_nome_exato(self):
        casados, _ = rm.casar_tags(self.NOMES, ["ERB-04"])
        self.assertEqual(casados, ["ERB-04"])

    def test_reporta_o_que_nao_encontrou(self):
        # Colar 40 tags e medir 37 em silêncio seria pior que falhar.
        casados, faltando = rm.casar_tags(self.NOMES, ["CA", "XX-999"])
        self.assertIn("XX-999", faltando)
        self.assertTrue(casados)

    def test_case_insensitive(self):
        casados, _ = rm.casar_tags(self.NOMES, ["ca-1010"])
        self.assertEqual(casados, ["CA-1010"])

    def test_prefixos_e_perfis_do_config(self):
        cfg = rm.configparser.ConfigParser(); rm.cfg_relatorio(cfg)
        pref = dict((p, r) for r, p in rm.prefixos_frota(cfg))
        self.assertIn("CA", pref); self.assertIn("ERM", pref)
        perfis = rm.perfis_frota(cfg)
        self.assertTrue(any("Repetidora" in k or "Repetidoras" in k
                            for k in perfis))

    def test_resumo_conta_sem_gps(self):
        itens = [{"nome": "A", "gps": True,  "online": True},
                 {"nome": "B", "gps": False, "online": True},
                 {"nome": "C", "gps": True,  "online": False}]
        r = rm.resumo_selecao(itens, ["A", "B", "C"])
        self.assertEqual(r["total"], 3)
        self.assertEqual(r["com_gps"], 2)
        self.assertEqual(r["sem_gps"], 1)
        self.assertEqual(r["offline"], 1)


# ══════════════════════════════════════════════════════════════════
# 21. ENDPOINTS DO SURVEY (servidor real, sem mock)
# ══════════════════════════════════════════════════════════════════
class TestEndpointsSurvey(unittest.TestCase):
    """Sobe o servidor de verdade: rota que só existe no código-fonte não
    prova nada."""

    @classmethod
    def setUpClass(cls):
        import threading, urllib.request
        from http.server import HTTPServer
        cls.tmp = tempfile.mkdtemp()
        cls.cwd = os.getcwd(); os.chdir(cls.tmp)

        class Col:
            interval = 60; falhas_limite = 3; manter_s = 300
            bcs = [f"10.0.0.{i}" for i in range(1, 8)]
            nomes = {f"10.0.0.{i}": n for i, n in enumerate(
                ["CA-1010", "CA-1042", "PF-1803", "ERM-20", "ERM-31",
                 "ERB-04", "TT-500"], start=1)}
        rm.COLETOR_ATUAL["ref"] = Col()
        for i, ip in enumerate(Col.bcs):
            e = rm.estado(ip, 3, 300)
            e.ultima_dados = {"sistema": {"nome": Col.nomes[ip],
                                          "gps_fix": 1 if i % 3 else 0},
                              "radios": []}
        cls.cfg = rm.carregar_config(); rm.cfg_relatorio(cls.cfg)
        cls.srv = HTTPServer(("127.0.0.1", 0), rm.criar_handler(cls.cfg))
        cls.porta = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); os.chdir(cls.cwd)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _get(self, caminho):
        import urllib.request
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.porta}{caminho}", timeout=10) as r:
            return json.loads(r.read())

    def _post(self, caminho, corpo, ctype="application/json"):
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.porta}{caminho}",
            data=corpo.encode(), headers={"Content-Type": ctype})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def test_bcs_traz_tags_com_contador(self):
        # Com 150 nós, checkbox individual não escala: os botões de grupo
        # dependem dessa contagem por prefixo.
        d = self._get("/bcs")
        self.assertEqual(d["tags"]["CA"], 2)
        self.assertEqual(d["tags"]["ERM"], 2)
        self.assertEqual(d["ciclo_s"], 60)
        self.assertTrue(all("tag" in b for b in d["bcs"]))

    def test_tag_e_o_prefixo_do_nome(self):
        d = self._get("/bcs")
        por_nome = {b["nome"]: b["tag"] for b in d["bcs"]}
        self.assertEqual(por_nome["ERM-20"], "ERM")
        self.assertEqual(por_nome["ERB-04"], "ERB")

    def test_perfis_salvar_abrir_excluir(self):
        import urllib.parse as up
        self._get("/perfis/salvar?nome=" + up.quote("Cava Norte")
                  + "&ips=10.0.0.1,10.0.0.4")
        self.assertIn("Cava Norte", self._get("/bcs")["perfis"])
        self.assertEqual(self._get("/perfis/abrir?nome=" + up.quote("Cava Norte"))
                         ["ips"], ["10.0.0.1", "10.0.0.4"])
        # configparser rebaixa o nome da opção — a busca não pode depender disso
        self.assertEqual(len(self._get("/perfis/abrir?nome=cava%20norte")["ips"]), 2)
        self._get("/perfis/excluir?nome=" + up.quote("CAVA NORTE"))
        self.assertNotIn("Cava Norte", self._get("/bcs")["perfis"])

    def _survey_pronto(self, nome="S", t0=None):
        t0 = t0 or (time.time() - 3600)
        con = rm.banco()
        try:
            sid = rm.survey_criar(con, nome, t0, 30, ["CA-1010"], alcance=900)
            rm.amostras_gravar(con, sid, [
                {"radio": "CA-1010", "ts": t0, "lat": -27.73, "lon": -50.07,
                 "sinal": -62, "snr": 33, "rtt": 8, "perda": 0.2},
                {"radio": "CA-1010", "ts": t0+30, "lat": -27.74, "lon": -50.06,
                 "sinal": -80, "snr": 14, "rtt": 120, "perda": 4.0}])
            am = rm.survey_amostras(con, sid)
            rm.survey_fechar(con, sid, t0+60, rm.survey_resumo(am), {}, 1, 0)
        finally:
            con.close()
        return sid

    def test_gerar_aceita_survey_id_do_historico(self):
        # A pagina manda survey_id quando o usuario clica "Usar no
        # relatorio". O endpoint so lia sv/svini/svfim e IGNORAVA o
        # survey_id: o PPT saia com os slides EM BRANCO do template, sem
        # dizer por que. Era o "relatorio vindo o antigo".
        fonte = inspect.getsource(rm.criar_handler)
        self.assertIn('q.get("survey_id"', fonte)
        i_id = fonte.index('q.get("survey_id"')
        i_sv = fonte.index('elif q.get("sv", ["0"])[0] == "1":')
        self.assertLess(i_id, i_sv, "survey gravado tem de vir antes")

    def test_falha_do_survey_chega_a_pagina(self):
        # O survey pode falhar sem derrubar o relatorio, mas quem pediu
        # precisa saber: antes so ia para o log do servidor.
        self.assertIn("X-Survey-Aviso", inspect.getsource(rm.criar_handler))
        self.assertIn("ultimo_aviso", inspect.getsource(rm.gerar_ppt))
        self.assertIn("X-Survey-Aviso", rm.PAGINA_HTML)

    def test_endpoint_kml_devolve_kmz_valido(self):
        import urllib.request, zipfile, io as _io
        from xml.etree import ElementTree as ET
        sid = self._survey_pronto("Para o Earth")
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.porta}/survey/kml?id={sid}&campo=snr",
                timeout=20) as r:
            self.assertEqual(r.headers["Content-Type"],
                             "application/vnd.google-earth.kmz")
            self.assertIn(".kmz", r.headers["Content-Disposition"])
            dados = r.read()
        kml = zipfile.ZipFile(_io.BytesIO(dados)).read("doc.kml").decode()
        ET.fromstring(kml)                       # XML mal formado levanta aqui
        self.assertIn("CA-1010", kml)

    def test_endpoint_kml_recusa_campo_sem_escala(self):
        import urllib.request, urllib.error
        sid = self._survey_pronto("Campo ruim")
        with self.assertRaises(urllib.error.HTTPError) as c:
            urllib.request.urlopen(
                f"http://127.0.0.1:{self.porta}/survey/kml?id={sid}&campo=vazao",
                timeout=20)
        self.assertEqual(c.exception.code, 500)
        self.assertIn("escala de cor", c.exception.read().decode())

    def test_historico_traz_indicadores(self):
        sid = self._survey_pronto("Turno tarde")
        s = [x for x in self._get("/surveys")["surveys"] if x["id"] == sid][0]
        self.assertEqual(s["n_amostras"], 2)
        self.assertEqual(s["resumo"]["sinal"]["pct_ok"], 50.0)

    def test_survey_completo_e_exclusao(self):
        sid = self._survey_pronto("Para excluir")
        d = self._get(f"/survey?id={sid}")
        self.assertEqual(len(d["amostras"]), 2)
        self._get(f"/survey/excluir?id={sid}")
        ids = [x["id"] for x in self._get("/surveys")["surveys"]]
        self.assertNotIn(sid, ids)

    def test_comparar_sem_anterior_fica_em_branco(self):
        # Nunca zero: não houve semana anterior.
        con = rm.banco()
        try: con.execute("DELETE FROM survey"); con.commit()
        finally: con.close()
        sid = self._survey_pronto("Primeiro", time.time() - 100)
        d = self._get(f"/survey/comparar?id={sid}")
        self.assertIsNone(d["anterior"])
        self.assertIsNone(d["delta"]["sinal"]["antes"])

    def test_comparar_com_anterior(self):
        con = rm.banco()
        try: con.execute("DELETE FROM survey"); con.commit()
        finally: con.close()
        self._survey_pronto("Semana 1", time.time() - 86400*7)
        s2 = self._survey_pronto("Semana 2", time.time() - 3600)
        d = self._get(f"/survey/comparar?id={s2}")
        self.assertEqual(d["anterior_nome"], "Semana 1")
        self.assertIsNotNone(d["delta"]["sinal"]["delta"])

    def test_medicoes_ciclo_completo(self):
        sid = self._survey_pronto("Com medicoes")
        r = self._post("/medicoes/adicionar", json.dumps(
            {"survey_id": sid, "tipo": "iperf", "data": "2026-05-15",
             "local": "Praça 3", "banda": "5.8 GHz", "mbps": "42,5"}))
        self.assertIsNotNone(r["id"])
        ms = self._get(f"/medicoes/listar?survey_id={sid}")["medicoes"]
        self.assertEqual(ms[0]["mbps"], 42.5)   # vírgula decimal aceita
        self._get(f"/medicoes/excluir?id={ms[0]['id']}")
        self.assertEqual(
            self._get(f"/medicoes/listar?survey_id={sid}")["medicoes"], [])

    def test_importar_csv_em_lote(self):
        sid = self._survey_pronto("Import")
        r = self._post(f"/medicoes/importar?survey_id={sid}&tipo=iperf",
                       rm.template_csv_manual("iperf"), "text/csv")
        self.assertEqual(r["inseridas"], 2)
        self.assertEqual(r["erros"], [])

    def test_linha_ruim_nao_aborta_o_lote(self):
        sid = self._survey_pronto("Import parcial")
        csv_ = ("data;local;lat;lon;banda;mbps;obs\n"
                "2026-05-15;Bom;;;5.8 GHz;10;\n"
                "2026-05-15;Ruim;;;5.8 GHz;;\n"
                "2026-05-15;Bom2;;;5.8 GHz;20;\n")
        r = self._post(f"/medicoes/importar?survey_id={sid}&tipo=iperf",
                       csv_, "text/csv")
        self.assertEqual(r["inseridas"], 2)
        self.assertEqual(len(r["erros"]), 1)

    def test_modelo_csv_baixa(self):
        import urllib.request
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.porta}/medicoes/modelo?tipo=trace",
                timeout=10) as r:
            self.assertIn("modelo_trace.csv",
                          r.headers.get("Content-Disposition", ""))
            self.assertIn("origem", r.read().decode("utf-8-sig"))

    def test_capturas_ativas_para_reconexao(self):
        # Fechar a aba não interrompe a captura; ao reabrir, a página
        # precisa reencontrar o que está rodando.
        self.assertIn("capturas", self._get("/captura/ativas"))


class TestZipImagens(unittest.TestCase):
    """As imagens são REGERADAS do histórico, não guardadas: o modelo de
    propagação e o estilo do mapa mudam, e as amostras é que são a fonte."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cwd = os.getcwd(); os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.cwd); shutil.rmtree(self.tmp, ignore_errors=True)

    def test_zip_do_survey(self):
        import math, random, zipfile, io
        random.seed(5)
        con = rm.banco()
        t0 = time.time() - 3600
        sid = rm.survey_criar(con, "Zip", t0, 30, ["CA-1"], alcance=900)
        am = []
        for i in range(60):
            la = -27.7345 + i * 0.0002
            d = max(50, rm._dist_m(la, -50.07, -27.7300, -50.0700))
            am.append({"radio": "CA-1", "ts": t0 + i*30, "lat": la,
                       "lon": -50.07, "banda": "5.8 GHz",
                       "sinal": -4 - 25.5*math.log10(d) + random.gauss(0, 2),
                       "snr": 30.0, "rtt": 9.0, "perda": 0.1})
        rm.amostras_gravar(con, sid, am)
        rm.survey_fechar(con, sid, t0+1800, rm.survey_resumo(am), {}, 1, 0)
        con.close()
        dados, nome = rm.zip_imagens_survey(sid, banda="5.8 GHz")
        self.assertTrue(nome.endswith(".zip"))
        z = zipfile.ZipFile(io.BytesIO(dados))
        nomes = z.namelist()
        self.assertTrue(any("rota" in n for n in nomes), nomes)
        self.assertTrue(any("medido_sinal" in n for n in nomes), nomes)

    def test_survey_sem_amostras_falha_com_motivo(self):
        con = rm.banco()
        sid = rm.survey_criar(con, "Vazio", time.time(), 30, ["X"])
        con.close()
        with self.assertRaises(RuntimeError) as ctx:
            rm.zip_imagens_survey(sid)
        self.assertIn("sem amostras", str(ctx.exception))

    def test_kmz_do_survey_pelo_banco(self):
        import zipfile, io
        con = rm.banco()
        t0 = time.time() - 3600
        sid = rm.survey_criar(con, "KML", t0, 10, ["CA-1", "ERB-9"])
        rm.amostras_gravar(con, sid, [
            {"radio": "CA-1", "ts": t0 + i * 10, "lat": -27.73 + i * 1e-4,
             "lon": -50.07 + i * 1e-4, "sinal": -62 - i, "snr": 30 - i,
             "banda": "5.8 GHz", "fonte": "direto"} for i in range(20)])
        rm.survey_fechar(con, sid, t0 + 200, intervalo_efetivo_s=11.0)
        con.close()
        dados, nome = rm.kml_do_survey(sid, "sinal")
        self.assertTrue(nome.endswith(".kmz"))
        kml = zipfile.ZipFile(io.BytesIO(dados)).read("doc.kml").decode()
        self.assertIn("CA-1", kml)
        # O intervalo efetivo é a resolução real do trajeto: precisa
        # viajar junto com o mapa, não só com o PPT.
        self.assertIn("intervalo efetivo", kml)


class TestSlidesSurvey(unittest.TestCase):
    """Os heatmaps precisam entrar no slide certo: o gerador novo produz
    chaves `medido_*` e o antigo `heatmap_*`."""

    def test_mapeamento_aceita_as_duas_familias(self):
        fonte = inspect.getsource(rm.inserir_imagens_survey)
        self.assertIn("medido_sinal", fonte)
        self.assertIn("heatmap_rssi", fonte)

    def test_escolhe_a_primeira_chave_presente(self):
        fonte = inspect.getsource(rm.inserir_imagens_survey)
        self.assertIn("next((c for c in chaves if c in imgs)", fonte)


class _BCFalso:
    """Rádio de mentira. `falhas` diz quantas consultas seguidas quebram."""
    def __init__(self, falhas=0, alcancavel=True, autentica=True, estado="ok"):
        self.falhas = falhas; self.alcancavel = alcancavel
        self.autentica = autentica; self._estado = estado
        self.logins = 0; self.consultas = 0; self.fechado = False

    def reachable(self):    return self.alcancavel
    def authenticate(self):
        self.logins += 1
        return self.autentica

    def get_state(self):
        self.consultas += 1
        if self.falhas > 0:
            self.falhas -= 1
            raise OSError("conexao caiu")
        return self._estado

    def close(self): self.fechado = True


class TestSessaoRadio(unittest.TestCase):
    """Sessão persistente: autenticar por amostra é o que mais pesa no BC."""

    def _sessao(self, bc, **kw):
        s = rm.SessaoRadio("10.0.0.1", 2300, "VIEW", "x", **kw)
        s._abrir = lambda: setattr(s, "bc", bc) or bc
        return s

    def test_autentica_uma_vez_e_reusa(self):
        bc = _BCFalso()
        s = self._sessao(bc)
        for _ in range(5):
            self.assertEqual(s.estado_bruto(), "ok")
        self.assertEqual(bc.consultas, 5)
        self.assertEqual(s.consultas, 5)

    def test_falha_isolada_nao_descarta_o_radio(self):
        bc = _BCFalso(falhas=1)
        s = self._sessao(bc, falhas_max=3)
        with self.assertRaises(OSError):
            s.estado_bruto()
        self.assertFalse(s.desistiu)
        # A sessão foi jogada fora para reautenticar do zero.
        self.assertIsNone(s.bc)
        self.assertEqual(s.estado_bruto(), "ok")
        self.assertEqual(s.falhas, 0, "sucesso zera o contador")

    def test_desiste_apos_n_falhas_seguidas(self):
        bc = _BCFalso(falhas=99)
        s = self._sessao(bc, falhas_max=3)
        for _ in range(3):
            with self.assertRaises(OSError): s.estado_bruto()
        self.assertTrue(s.desistiu)
        # Rádio descartado não é mais tentado: insistir atrasa o ciclo
        # inteiro, e o ciclo define a resolução do trajeto.
        antes = bc.consultas
        self.assertIsNone(s.estado_bruto())
        self.assertEqual(bc.consultas, antes)


class TestFiltroDeEstado(unittest.TestCase):
    """A rajant-api nem sempre expõe filtro de caminho e o nome do
    parâmetro muda entre versões — por isso é detectado, não assumido."""

    def setUp(self):  rm._FILTRO_ESTADO["param"] = None
    def tearDown(self): rm._FILTRO_ESTADO["param"] = None

    def test_sem_filtro_puxa_o_estado_inteiro(self):
        bc = _BCFalso()
        self.assertEqual(rm._get_state_filtrado(bc), "ok")
        self.assertEqual(rm._FILTRO_ESTADO["param"], "")

    def test_usa_o_filtro_quando_existe(self):
        vistos = {}
        class BC:
            def get_state(self, stateFilterPath=None):
                vistos["p"] = stateFilterPath; return "ok"
        self.assertEqual(rm._get_state_filtrado(BC()), "ok")
        self.assertEqual(rm._FILTRO_ESTADO["param"], "stateFilterPath")
        self.assertEqual(vistos["p"], list(rm.CAMINHOS_ESTADO))
        for ramo in ("gps", "wireless", "system"):
            self.assertIn(ramo, vistos["p"])

    def test_filtro_recusado_desliga_de_vez(self):
        # Aceita o argumento mas recusa o formato: não pode repetir o erro
        # a cada amostra de cada rádio.
        chamadas = []
        class BC:
            def get_state(self, path=None):
                chamadas.append(path)
                if path is not None: raise ValueError("formato invalido")
                return "ok"
        self.assertEqual(rm._get_state_filtrado(BC()), "ok")
        self.assertEqual(rm._FILTRO_ESTADO["param"], "")
        self.assertEqual(len(chamadas), 2)


class TestCapturaDireta(unittest.TestCase):
    """A captura consulta os rádios no intervalo pedido; o cache do
    exporter é recurso de degradação, não a fonte."""

    class _Col:
        interval = 60; falhas_limite = 3; manter_s = 300
        port = 2300; role = "VIEW"; password = ""
        nomes = {"10.0.0.1": "CA-1001"}

    def _job(self, intervalo=10, **cfgkv):
        cfg = rm.configparser.ConfigParser(); cfg.add_section("survey")
        for k, v in cfgkv.items(): cfg.set("survey", k, str(v))
        return rm.CapturaGPS("id1", ["10.0.0.1"], 1, intervalo, self._Col(),
                             cfg=cfg)

    def test_intervalo_respeita_o_piso_configurado(self):
        # Pedir 1 s com 150 rádios derruba a malha.
        self.assertEqual(self._job(intervalo=1, min_intervalo_s=5).intervalo, 5)
        self.assertEqual(self._job(intervalo=30, min_intervalo_s=5).intervalo, 30)

    def test_teto_de_threads_vem_do_config(self):
        self.assertEqual(self._job(max_threads=4).max_thr, 4)
        self.assertEqual(self._job().max_thr, 12)

    def test_amostra_direta_marca_a_fonte(self):
        job = self._job()
        ses = rm.SessaoRadio("10.0.0.1", 2300, "VIEW", "")
        ses._abrir = lambda: setattr(ses, "bc", _BCFalso()) or ses.bc
        job.sessoes["10.0.0.1"] = ses
        with mock.patch.object(rm, "parse_state",
                               return_value={"sistema": {}, "radios": []}), \
             mock.patch.object(rm, "calcular_taxas", side_effect=lambda i, d, t: d):
            d, fonte = job._dados_do_radio("10.0.0.1")
        self.assertEqual(fonte, "direto")

    def test_cai_para_o_cache_marcando_a_amostra(self):
        job = self._job()
        ses = rm.SessaoRadio("10.0.0.1", 2300, "VIEW", "")
        ses.desistiu = True                      # rádio já descartado
        job.sessoes["10.0.0.1"] = ses
        e = rm.estado("10.0.0.1", 3, 300)
        e.ultima_dados = {"sistema": {"nome": "CA-1001"}, "radios": []}
        d, fonte = job._dados_do_radio("10.0.0.1")
        self.assertEqual(fonte, "cache",
                         "ponto defasado precisa vir identificado")
        self.assertIsNotNone(d)

    def test_cache_desligado_devolve_buraco_em_vez_de_ponto_velho(self):
        job = self._job(usar_cache_fallback="false")
        ses = rm.SessaoRadio("10.0.0.1", 2300, "VIEW", "")
        ses.desistiu = True
        job.sessoes["10.0.0.1"] = ses
        e = rm.estado("10.0.0.1", 3, 300)
        e.ultima_dados = {"sistema": {"nome": "CA-1001"}, "radios": []}
        self.assertEqual(job._dados_do_radio("10.0.0.1"), (None, None))

    def test_ciclo_que_estoura_nao_acumula_fila(self):
        fonte = inspect.getsource(rm.CapturaGPS._rodar)
        self.assertIn("if espera <= 0:", fonte)
        self.assertIn("continue", fonte)

    def test_status_expoe_intervalo_efetivo(self):
        job = self._job(); job.efetivo_s = 14.0
        s = job.status()
        self.assertEqual(s["intervalo_efetivo_s"], 14.0)
        self.assertEqual(s["intervalo_s"], 10)


class TestPersistenciaFonteEIntervalo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.con = rm.banco(str(Path(self.tmp) / "s.db"))

    def tearDown(self):
        self.con.close(); shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fonte_persiste_por_amostra(self):
        sid = rm.survey_criar(self.con, "s", time.time(), 10, ["CA-1"])
        rm.amostras_gravar(self.con, sid, [
            {"radio": "CA-1", "ts": 1, "lat": -27.7, "lon": -50.0,
             "fonte": "direto"},
            {"radio": "CA-1", "ts": 2, "lat": -27.7, "lon": -50.0,
             "fonte": "cache"}])
        fontes = [a["fonte"] for a in rm.survey_amostras(self.con, sid)]
        self.assertEqual(fontes, ["direto", "cache"])

    def test_intervalo_efetivo_persiste_no_fechamento(self):
        sid = rm.survey_criar(self.con, "s", time.time(), 10, ["CA-1"])
        rm.survey_fechar(self.con, sid, time.time(), intervalo_efetivo_s=14.2)
        self.assertEqual(rm.survey_obter(self.con, sid)["intervalo_efetivo_s"],
                         14.2)

    def test_banco_antigo_ganha_as_colunas_novas(self):
        # Migração: CREATE TABLE IF NOT EXISTS não acrescenta coluna.
        p = str(Path(self.tmp) / "velho.db")
        antigo = sqlite3.connect(p)
        antigo.executescript(
            "CREATE TABLE survey (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " nome TEXT NOT NULL, inicio REAL NOT NULL, fim REAL,"
            " intervalo_s INTEGER, radios TEXT, resumo TEXT,"
            " calibracao TEXT, criado_em REAL NOT NULL);"
            "CREATE TABLE amostra (survey_id INTEGER NOT NULL, radio TEXT,"
            " ts REAL, lat REAL, lon REAL);")
        antigo.commit(); antigo.close()
        con = rm.banco(p)
        try:
            cols = {r["name"] for r in con.execute("PRAGMA table_info(survey)")}
            self.assertIn("intervalo_efetivo_s", cols)
            cols_a = {r["name"] for r in con.execute("PRAGMA table_info(amostra)")}
            self.assertIn("fonte", cols_a)
        finally:
            con.close()


class TestInterferencia(unittest.TestCase):
    """A BC API nao tem varredura de espectro — verificado nos .proto. O
    que ela da e airtime, e dele sai a ocupacao por transmissor alheio."""

    IM = {k: 0 for k in ("pkt_tx", "pkt_drop", "pkt_rx", "arp",
                         "bytes_tx", "bytes_rx")}

    def _radio(self, ativo, busy, rx, tx):
        return {"nome": "wlan0", "ch_active": ativo, "ch_busy": busy,
                "ch_rx": rx, "ch_tx": tx, "rx_bytes": 0, "tx_bytes": 0,
                "rx_pkts": 0, "tx_pkts": 0, "canal": 149}

    def _pacote(self, r):
        return {"radios": [r], "ethernet": [],
                "sistema": {"idle": 0, "uptime": 0},
                "instamesh": dict(self.IM)}

    def _delta(self, ip, antes, agora, dt=60.0):
        t = time.monotonic()
        rm._ultimo[ip] = {"d": self._pacote(self._radio(*antes)), "ts": t - dt}
        return rm.calcular_taxas(ip, self._pacote(self._radio(*agora)),
                                 t)["radios"][0]

    def test_proto_nao_tem_varredura_de_espectro(self):
        # Se um dia a Rajant publicar isso, este teste avisa que da para
        # trocar a estimativa por medicao de verdade.
        raiz = pathlib.Path("bcapi-ref/proto")
        if not raiz.exists():
            self.skipTest("protos nao disponiveis")
        texto = " ".join(p.read_text(errors="ignore").lower()
                         for p in raiz.glob("*.proto"))
        for termo in ("spectrum", "spectral", "sweep"):
            self.assertNotIn(termo, texto,
                             f"'{termo}' apareceu no proto — reavaliar")

    def test_trafego_proprio_nao_conta_como_interferencia(self):
        # O caso que confunde: canal 50% ocupado, mas ocupado por NOS.
        r = self._delta("10.9.0.1", (1000,1000,1000,1000),
                        (61000, 31000, 21000, 10000))
        self.assertAlmostEqual(r["busy_pct"], 50.0, places=1)
        self.assertLess(r["interf_pct"], 5.0)

    def test_ocupacao_alheia_vira_interferencia(self):
        r = self._delta("10.9.0.2", (1000,1000,1000,1000),
                        (61000, 40000, 3000, 2000))
        self.assertAlmostEqual(r["interf_pct"], 60.0, places=1)

    def test_primeira_coleta_fica_sem_valor_nao_zero(self):
        # Contadores sao cumulativos desde o boot: sem delta, a razao
        # seria a media da vida inteira do radio.
        d = rm.calcular_taxas("10.9.0.3",
                              self._pacote(self._radio(61000,40000,3000,2000)),
                              time.monotonic())
        self.assertIsNone(d["radios"][0].get("interf_pct"))

    def test_parse_state_nao_inventa_valor_cumulativo(self):
        fonte = inspect.getsource(rm.parse_state)
        self.assertIn('"interf_pct":   None', fonte)

    def test_nao_negativa_quando_contadores_desalinham(self):
        # rx+tx > busy acontece com arredondamento do firmware.
        r = self._delta("10.9.0.4", (1000,1000,1000,1000),
                        (61000, 5000, 4000, 4000))
        self.assertGreaterEqual(r["interf_pct"], 0.0)

    def test_nao_e_requisito_modular_mas_e_analisavel(self):
        # Pô-la em REQUISITOS faria o resumo reportá-la como exigência
        # do cliente, o que ela não é.
        self.assertNotIn("interf", rm.REQUISITOS)
        op, lim, un, rot = rm.limite_de("interf")
        self.assertEqual((op, lim, un), ("<", 20.0, "%"))

    def test_limite_de_devolve_none_para_campo_sem_escala(self):
        self.assertIsNone(rm.limite_de("custo"))

    def test_tem_escala_de_mapa_e_de_kml(self):
        self.assertIn("interf", rm.ESCALAS)
        self.assertIn("interf", rm.FAIXAS_KML)
        self.assertEqual(rm.ESCALAS["interf"]["melhor"], "baixo")

    def test_zonas_de_interferencia(self):
        am = [{"radio": "CA-1", "ts": k, "lat": -27.7360 + k*0.0006,
               "lon": -50.07, "interf": 70.0 if 5 <= k <= 10 else 3.0}
              for k in range(30)]
        zs = rm.zonas_problema(am, "interf", 50.0)
        self.assertEqual(len(zs), 1)
        self.assertGreater(zs[0]["valor_mediano"], 20)

    def test_amostra_do_survey_guarda_interferencia_e_canal(self):
        fonte = inspect.getsource(rm.amostras_gravar)
        self.assertIn("interf", fonte)
        self.assertIn("canal", fonte)


class TestDescobertaEFiltro(unittest.TestCase):
    """O exporter mostrava IP e MAC que nao sao equipamento da frota."""

    ST = '''
    wireless { name: "wlan0" channel: 149 noise: -95
      peer { mac: "AA:BB:CC:DD:EE:FF" enabled: true rssi: 40 signal: -60
             cost: 10 rate: 1300 }
      peer { ipv4Address: "10.0.0.7" mac: "11:22:33:44:55:66" enabled: true
             rssi: 42 signal: -58 cost: 8 rate: 1300 }
      peer { ipv4Address: "0.0.0.0" enabled: true rssi: 10 signal: -80
             cost: 99 rate: 65 }
    }
    '''

    def test_peer_sem_ip_nao_entra_na_descoberta(self):
        # `mac:AA:BB:...` nao e endereco: ia para a fila, falhava a
        # conexao, era marcado OFFLINE e virava equipamento fantasma.
        d = rm.parse_state(self.ST)
        self.assertEqual(d["peers_ips"], {"10.0.0.7"})

    def test_mas_o_enlace_do_peer_sem_ip_e_preservado(self):
        # Descartar o peer inteiro perderia SNR, custo e taxa do enlace —
        # foi por isso que o identificador por MAC existe.
        d = rm.parse_state(self.ST)
        ids = {p["ip"] for p in d["radios"][0]["peers"]}
        self.assertIn("mac:AA:BB:CC:DD:EE:FF", ids)
        self.assertEqual(len(d["radios"][0]["peers"]), 2)

    def test_ip_zerado_tambem_fica_fora(self):
        d = rm.parse_state(self.ST)
        self.assertNotIn("0.0.0.0", d["peers_ips"])

    def _col(self, prefs):
        c = rm.RajantCollector.__new__(rm.RajantCollector)
        c.prefixos_tag = prefs; c.sem_tag = set()
        return c

    def test_sem_prefixos_configurados_aceita_tudo(self):
        # O filtro e opcional: nao pode passar a descartar por omissao.
        c = self._col(None)
        for n in ("CA-1001", "qualquer-coisa", "", "10.0.0.9"):
            self.assertTrue(c.tem_tag(n), n)

    def test_com_prefixos_so_passa_a_frota(self):
        c = self._col(["CA", "PA", "PF", "TT", "EH", "ERM", "ERB"])
        for n in ("CA-1001", "ERM-20", "ERB-04", "pf-77"):
            self.assertTrue(c.tem_tag(n), n)
        for n in ("BC-XYZ", "", "10.0.0.99", "teste-lab", None):
            self.assertFalse(c.tem_tag(n), n)

    def test_descarte_e_auditavel(self):
        # Sumir com equipamento calado seria pior que mostrar de mais.
        fonte = inspect.getsource(rm.RajantCollector._coletar_bc)
        self.assertIn("m_bc_sem_tag", fonte)
        self.assertIn("ignorado", fonte)

    def test_descoberta_desligada_nao_expande_no_ciclo(self):
        # Sem esta guarda o ciclo reintroduzia pela porta dos fundos o
        # que a descoberta deixou de fora.
        fonte = inspect.getsource(rm.RajantCollector.ciclo)
        self.assertIn("if peers and self.descoberta:", fonte)

    def test_descoberta_desligada_mantem_seed_offline(self):
        # Um BC que caiu agora tem de continuar monitorado, senao ele
        # some da metrica justamente quando cai.
        fonte = inspect.getsource(rm.RajantCollector.descobrir)
        self.assertIn("alvos = set(self.seeds)", fonte)

    def test_cache_desligado_nao_escreve_disco(self):
        tmp = tempfile.mkdtemp()
        try:
            cam = Path(tmp) / "c.json"
            c = rm.CacheIPs(str(cam), ativo=False)
            c.adicionar("10.0.0.1", "CA-1001")
            self.assertFalse(cam.exists(), "escreveu com o cache desligado")
            self.assertEqual(c.total(), 1, "deveria guardar em memoria")
            c2 = rm.CacheIPs(str(cam), ativo=True)
            c2.adicionar("10.0.0.2", "CA-1002")
            self.assertTrue(cam.exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_config_traz_as_tres_chaves(self):
        for chave in ("descoberta", "usar_cache", "somente_com_tag"):
            self.assertIn(chave, rm.DEFAULT_CONFIG, chave)


class TestShimSslWrapSocket(unittest.TestCase):
    """A rajant-api 0.1.1 faz `from ssl import wrap_socket`, removido no
    Python 3.12. O shim do topo do modulo e o que mantem o programa
    rodando em 3.12+ — sem ele, nao importa."""

    def test_shim_existe_no_topo_do_modulo(self):
        # Tem de vir ANTES do `from rajant_api import Breadcrumb`, senao
        # a biblioteca ja quebrou quando o shim seria instalado.
        fonte = pathlib.Path("rajant_monitor.py").read_text()
        i_shim = fonte.index("ssl.wrap_socket = _wrap")
        i_imp = fonte.index("from rajant_api import Breadcrumb")
        self.assertLess(i_shim, i_imp, "shim depois do import: tarde demais")

    def test_shim_reproduz_o_comportamento_antigo(self):
        # ssl.wrap_socket() com os padroes NAO validava certificado — os
        # BreadCrumbs usam autoassinado. O shim precisa manter isso, ou a
        # conexao passa a falhar onde antes funcionava.
        import ssl as _ssl
        fonte = pathlib.Path("rajant_monitor.py").read_text()
        trecho = fonte[:fonte.index("import re, json")]
        self.assertIn("cert_reqs=ssl.CERT_NONE", trecho)
        self.assertIn("check_hostname = False", trecho)

    def test_shim_nao_sobrescreve_quando_ja_existe(self):
        # Em Python <=3.11 a funcao original tem de continuar valendo.
        fonte = pathlib.Path("rajant_monitor.py").read_text()
        self.assertIn("if not hasattr(ssl, 'wrap_socket'):", fonte)

    def test_shim_faz_handshake_de_verdade(self):
        """Importar nao prova nada: o que importa e a conexao TLS."""
        import ssl as _ssl, socket, threading, tempfile, subprocess
        if not hasattr(_ssl, "wrap_socket"):
            self.skipTest("Python sem wrap_socket original para comparar")
        d = tempfile.mkdtemp()
        cert, chave = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
        r = subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout",
             chave, "-out", cert, "-days", "1", "-nodes", "-subj",
             "/CN=bc-teste"], capture_output=True)
        if r.returncode != 0:
            self.skipTest("openssl indisponivel")

        ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, chave)
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0)); srv.listen(1)
        porta = srv.getsockname()[1]

        def atender():
            try:
                c, _ = srv.accept()
                with ctx.wrap_socket(c, server_side=True) as t:
                    t.sendall(b"OI")
            except Exception:
                pass
        threading.Thread(target=atender, daemon=True).start()

        # Reconstrói o shim isoladamente, do jeito que o módulo faz.
        def _wrap(sock, cert_reqs=_ssl.CERT_NONE, **kw):
            c = _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
            c.check_hostname = False; c.verify_mode = cert_reqs
            return c.wrap_socket(sock)
        soc = socket.socket(); soc.settimeout(5)
        con = _wrap(soc)
        try:
            con.connect(("127.0.0.1", porta))
            self.assertEqual(con.recv(16), b"OI")
            self.assertIsNotNone(con.cipher())
        finally:
            con.close(); srv.close(); shutil.rmtree(d, ignore_errors=True)


class TestNavegacaoEAnalise(unittest.TestCase):
    """O Site Survey virou 2/3 do deck atras de UM botao do menu, e o
    slide de fecho ficava em branco."""

    def test_indice_lista_todos_os_slides_da_secao(self):
        self.assertEqual(len(rm._ORDEM_SURVEY), 12)
        for t in ("Rota Percorrida", "Intensidade de Sinal (RSSI)",
                  "Interferência de Canal",
                  "Análise, Recomendações e Conclusão"):
            self.assertIn(t, rm._ORDEM_SURVEY)

    def test_indice_e_construido_depois_dos_slides(self):
        # Linkar antes de os slides existirem daria um indice de itens
        # mortos.
        fonte = inspect.getsource(rm.anexar_survey_ao_ppt)
        self.assertLess(fonte.index("preencher_slides_survey"),
                        fonte.index("construir_indice_survey"))

    def test_menu_principal_aponta_para_o_indice(self):
        fonte = inspect.getsource(rm.atualizar_navegacao)
        self.assertIn("TITULO_INDICE", fonte)
        self.assertIn('"5. Site Survey"', fonte)

    def _deck(self):
        """Deck mínimo com um botão que já tem link NO TEXTO, como os do
        template do cliente."""
        from pptx import Presentation
        from pptx.util import Inches
        from pptx.enum.shapes import MSO_SHAPE
        p = Presentation()
        lay = p.slide_layouts[6]
        a, b, c = (p.slides.add_slide(lay) for _ in range(3))
        bt = a.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(1),
                                Inches(1), Inches(2), Inches(0.5))
        r = bt.text_frame.paragraphs[0].add_run(); r.text = "5. Site Survey"
        # link ANTIGO, no texto e na forma, apontando para o slide errado
        bt.click_action.target_slide = b
        r.hyperlink.address = "https://exemplo.invalido/antigo"
        return p, bt, b, c

    def test_retargetar_remove_o_link_do_texto(self):
        # O botão do template carrega DOIS links: um na forma e outro no
        # run. Clicando no TEXTO — que é o que a pessoa faz — vale o do
        # run. Trocar só o da forma deixava o botão indo para o alvo
        # antigo, e o python-pptx não mostrava o conflito.
        A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
        p, bt, antigo, novo = self._deck()
        self.assertTrue(any(True for _ in bt._element.iter(A + "hlinkClick")))
        rm._link_para(bt, novo)
        nos_runs = [hl for hl in bt._element.iter(A + "hlinkClick")
                    if hl.getparent().tag == A + "rPr"]
        self.assertEqual(nos_runs, [], "link do texto sobreviveu")
        self.assertIs(bt.click_action.target_slide, novo)

    def test_limpar_link_de_texto_conta_o_que_removeu(self):
        p, bt, antigo, novo = self._deck()
        self.assertGreaterEqual(rm._limpar_link_de_texto(bt), 1)
        self.assertEqual(rm._limpar_link_de_texto(bt), 0, "idempotente")

    def test_barra_do_survey_tem_indice_e_troca_de_banda(self):
        fonte = inspect.getsource(rm.barra_navegacao_survey)
        self.assertIn("▤ ÍNDICE", fonte)
        self.assertIn("⇄", fonte)
        # Não pode duplicar a barra se o deck for processado de novo.
        self.assertIn('"ÍNDICE" in (sh.text_frame.text or "")', fonte)

    def test_analise_usa_o_que_ja_foi_calculado(self):
        # Zonas, cobertura por BC e ping-pong eram calculados e so iam
        # para o log; o slide de fecho seguia "preencher apos consolidacao".
        fonte = inspect.getsource(rm.preencher_analise_survey)
        for termo in ("ANÁLISE DOS RESULTADOS", "RECOMENDAÇÕES", "CONCLUSÃO",
                      "texto_zona", "pingpong", "cobertura"):
            self.assertIn(termo, fonte)

    def test_conclusao_sem_zona_nao_inventa_problema(self):
        # Deck sem zona reprovada nao pode sugerir acao que nao existe.
        fonte = inspect.getsource(rm.preencher_analise_survey)
        self.assertIn("sem zona contígua reprovada", fonte)
        self.assertIn("Manter o plano de manutenção atual", fonte)

    def test_sem_medicao_a_conclusao_fica_pendente(self):
        fonte = inspect.getsource(rm.preencher_analise_survey)
        self.assertIn("pendente de nova coleta", fonte)


class TestMolduraParaColar(unittest.TestCase):
    """O PPT sai com molduras vazias: o print vem do Google Earth, que tem
    o satelite que o PNG nao tem enquanto a rede bloquear os tiles."""

    def test_padrao_e_moldura_nao_imagem(self):
        cfg = rm.configparser.ConfigParser()
        with mock.patch.object(rm, "CONFIG_FILE",
                               str(Path(tempfile.mkdtemp()) / "c.ini")):
            rm.cfg_relatorio(cfg)
        self.assertFalse(cfg.getboolean("relatorio", "imagens_no_ppt"))

    def test_cada_chave_aponta_para_o_kmz_certo(self):
        # A moldura precisa dizer QUAL arquivo abrir; com doze KMZ na
        # pasta, "cole o mapa aqui" vira adivinhacao.
        self.assertEqual(rm._KMZ_DA_CHAVE["medido_snr"][0], "snr")
        self.assertEqual(rm._KMZ_DA_CHAVE["medido_ruido"][0], "ruido")
        self.assertEqual(rm._KMZ_DA_CHAVE["medido_interf"][0], "interf")
        self.assertEqual(rm._KMZ_DA_CHAVE["heatmap_rssi"][0], "sinal")

    def test_mapa_da_rede_nao_e_por_banda(self):
        # Passe de cada banda achava o slide pelo titulo sem sufixo e o
        # reescrevia: a moldura acabava rotulada com a ultima banda.
        fonte = inspect.getsource(rm.inserir_imagens_survey)
        self.assertIn('if titulo == "Mapa da Rede" and banda:', fonte)

    def test_moldura_existe_mesmo_sem_imagem_gerada(self):
        fonte = inspect.getsource(rm.inserir_imagens_survey)
        self.assertIn('if modo == "moldura" and chave is None:', fonte)


class TestTodosOsKmz(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cwd = os.getcwd(); os.chdir(self.tmp)
        con = rm.banco()
        t0 = time.time() - 3600
        self.sid = rm.survey_criar(con, "Todos", t0, 10, ["CA-1"])
        linhas = []
        for b in ("2.4 GHz", "5.8 GHz"):
            for i in range(12):
                linhas.append({
                    "radio": "CA-1", "ts": t0 + i*10,
                    "lat": -27.73 + i*1e-4, "lon": -50.07 + i*1e-4,
                    "sinal": -62 - i, "snr": 30 - i, "ruido": -95 + i*0.2,
                    "rtt": 8 + i, "perda": 0.1 * i, "interf": 3.0 + i,
                    "banda": b, "servidor": "ERB-01", "fonte": "direto"})
        rm.amostras_gravar(con, self.sid, linhas)
        rm.survey_fechar(con, self.sid, t0 + 200)
        con.close()

    def tearDown(self):
        os.chdir(self.cwd); shutil.rmtree(self.tmp, ignore_errors=True)

    def test_um_kmz_por_banda_com_todas_as_abas(self):
        # Um arquivo por GRANDEZA obrigava a fechar e reabrir o Earth a
        # cada print; com abas, e um clique no painel de camadas.
        import zipfile, io as _io
        from xml.etree import ElementTree as ET
        NS = "{http://www.opengis.net/kml/2.2}"
        dados, nome = rm.gerar_todos_kmz(self.sid)
        self.assertTrue(nome.endswith(".zip"))
        z = zipfile.ZipFile(_io.BytesIO(dados))
        kmz = [n for n in z.namelist() if n.endswith(".kmz")]
        self.assertEqual(len(kmz), 2, "um por banda")
        k = zipfile.ZipFile(_io.BytesIO(z.read(kmz[0]))).read("doc.kml")
        raiz = ET.fromstring(k)
        abas = [f.find(NS+"name").text
                for f in raiz.find(NS+"Document").findall(NS+"Folder")]
        for rot in ("RSSI", "SNR", "Ruído", "Latência", "Perda",
                    "Interferência"):
            self.assertIn(rot, abas)

    def test_banda_no_nome_para_nao_sobrescrever(self):
        # Sem a banda no nome, o de 5.8 GHz sobrescreveria o de 2.4 GHz.
        import zipfile, io as _io
        z = zipfile.ZipFile(_io.BytesIO(rm.gerar_todos_kmz(self.sid)[0]))
        nomes = [n for n in z.namelist() if n.endswith(".kmz")]
        self.assertEqual(len(nomes), len(set(nomes)))
        self.assertTrue(any("24GHz" in n for n in nomes))
        self.assertTrue(any("58GHz" in n for n in nomes))

    def test_leiame_diz_qual_arquivo_vai_em_qual_slide(self):
        import zipfile, io as _io
        z = zipfile.ZipFile(_io.BytesIO(rm.gerar_todos_kmz(self.sid)[0]))
        txt = z.read("LEIA-ME.txt").decode()
        self.assertIn("Intensidade de Sinal (RSSI)", txt)
        self.assertIn("Noise Floor", txt)
        self.assertIn("Interferência de Canal", txt)

    def test_kml_filtra_por_banda(self):
        con = rm.banco()
        am = rm.survey_amostras(con, self.sid)
        sv = rm.survey_obter(con, self.sid); con.close()
        import zipfile, io as _io
        d24, _ = rm.gerar_kml_survey(sv, am, campo="sinal", banda="2.4 GHz")
        k = zipfile.ZipFile(_io.BytesIO(d24)).read("doc.kml").decode()
        self.assertIn("2.4 GHz", k)
        self.assertNotIn("5.8 GHz", k, "amostra da outra banda vazou")

    def test_ruido_tem_escala_de_cor(self):
        # Sem ela o slide de Noise Floor ficava sem KMZ para o print.
        self.assertIn("ruido", rm.FAIXAS_KML)
        self.assertEqual(rm._bucket_cor(-98, rm.FAIXAS_KML["ruido"]), "27AE60")
        self.assertEqual(rm._bucket_cor(-70, rm.FAIXAS_KML["ruido"]), "C0392B")

    def test_grandeza_sem_medicao_e_pulada_com_motivo(self):
        con = rm.banco()
        sid2 = rm.survey_criar(con, "Parcial", time.time(), 10, ["CA-9"])
        rm.amostras_gravar(con, sid2, [
            {"radio": "CA-9", "ts": 1, "lat": -27.7, "lon": -50.0,
             "sinal": -70, "banda": "5.8 GHz"}])
        con.close()
        import zipfile, io as _io
        dados, _ = rm.gerar_todos_kmz(sid2)
        z = zipfile.ZipFile(_io.BytesIO(dados))
        # Arquivo vazio no zip so faria perder tempo abrindo.
        self.assertTrue(any(n.endswith(".kmz") for n in z.namelist()))
        self.assertIn("LEIA-ME.txt", z.namelist())


class TestSeparacaoDosRelatorios(unittest.TestCase):
    """Survey e relatorio semanal viraram entregas separadas, com
    publicos diferentes."""

    def test_semanal_nao_anexa_survey_por_padrao(self):
        fonte = inspect.getsource(rm.gerar_ppt)
        self.assertIn('"survey_no_semanal", fallback=False', fonte)
        i_rem = fonte.index("remover_slides_survey(p)")
        i_anexa = fonte.index("anexar_survey_ao_ppt(p, cfg, survey)")
        self.assertLess(i_rem, i_anexa, "remove depois de anexar nao adianta")

    def test_remocao_tira_os_slides_do_template_tambem(self):
        # So deixar de ANEXAR nao basta: o template do cliente ja traz a
        # secao, e ela ficaria em branco no arquivo.
        fonte = inspect.getsource(rm.remover_slides_survey)
        self.assertIn("_eh_titulo_survey", fonte)
        self.assertIn("site survey", fonte)

    def test_navegacao_nao_e_removida(self):
        # O slide de Navegacao lista TODAS as secoes; casaria com o
        # filtro e sumiria junto.
        fonte = inspect.getsource(rm.remover_slides_survey)
        self.assertIn('startswith("navega")', fonte)

    def test_botao_do_menu_deixa_de_apontar_para_o_vazio(self):
        # Link para slide removido nao acusa erro: so nao vai a lugar
        # nenhum.
        fonte = inspect.getsource(rm.remover_slides_survey)
        self.assertIn("relatório separado", fonte)
        self.assertIn("_limpar_link_de_texto", fonte)

    def test_remove_de_tras_para_frente(self):
        # Remover pelo indice crescente bagunçaria os seguintes.
        fonte = inspect.getsource(rm.remover_slides_survey)
        self.assertIn("for i in reversed(alvos):", fonte)

    def test_config_permite_voltar_ao_deck_unico(self):
        self.assertIn("survey_no_semanal", rm.DEFAULTS_RELATORIO)
        self.assertEqual(rm.DEFAULTS_RELATORIO["survey_no_semanal"], "false")


class TestIdentidadeAnglo(unittest.TestCase):
    """As medidas vieram do Dashboard_Transporte do cliente: logo, titulo
    e regua caem nos mesmos pontos, senao os decks nao parecem da mesma
    familia."""

    def test_paleta_e_do_template_do_cliente(self):
        self.assertEqual(rm.ANGLO["azul"], "031795")
        self.assertEqual(rm.ANGLO["fonte"], "Calibri")

    def test_geometria_igual_a_do_template(self):
        self.assertEqual(rm.ANGLO["logo"], (0.30, 0.20, 1.50, 0.59))
        self.assertEqual(rm.ANGLO["titulo"], (1.80, 0.20, 9.73, 0.70))
        self.assertEqual(rm.ANGLO["regua_y"], 1.00)

    def test_sem_a_pasta_marca_o_slide_sai_sem_logo_em_vez_de_estourar(self):
        tmp = tempfile.mkdtemp(); cwd = os.getcwd()
        try:
            os.chdir(tmp)
            with mock.patch.object(rm, "DIR_MARCA", "pasta_que_nao_existe"):
                self.assertIsNone(rm._logo_anglo())
                from pptx import Presentation
                from pptx.util import Inches
                p = Presentation()
                p.slide_width = Inches(13.333); p.slide_height = Inches(7.5)
                s = rm.slide_anglo(p, "Teste")   # nao pode levantar
                self.assertTrue(any(sh.has_text_frame for sh in s.shapes))
        finally:
            os.chdir(cwd); shutil.rmtree(tmp, ignore_errors=True)


class TestDeckSurveyAnglo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cwd = os.getcwd(); os.chdir(self.tmp)
        con = rm.banco(); t0 = time.time() - 3600
        self.sid = rm.survey_criar(con, "Turno teste", t0, 10, ["CA-1"])
        linhas = []
        for b in ("2.4 GHz", "5.8 GHz"):
            for i in range(14):
                linhas.append({
                    "radio": "CA-1", "ts": t0 + i*10,
                    "lat": -27.73 + i*3e-4, "lon": -50.07 + i*3e-4,
                    "sinal": -60 - i*2.2, "snr": 32 - i*1.4, "ruido": -95,
                    "rtt": 8 + i, "perda": 0.1*i, "interf": 3.0 + i,
                    "banda": b, "servidor": "ERB-01", "fonte": "direto"})
        rm.amostras_gravar(con, self.sid, linhas)
        am = rm.survey_amostras(con, self.sid)
        rm.survey_fechar(con, self.sid, t0+200, rm.survey_resumo(am), {},
                         1, 0, intervalo_efetivo_s=11.0)
        con.close()

    def tearDown(self):
        os.chdir(self.cwd); shutil.rmtree(self.tmp, ignore_errors=True)

    def _deck(self):
        from pptx import Presentation
        import io as _io
        dados, nome = rm.ppt_survey_anglo(self.sid)
        self.assertTrue(nome.endswith(".pptx"))
        return Presentation(_io.BytesIO(dados)), nome

    def test_deck_e_so_de_survey(self):
        # Separado do relatorio semanal a pedido: nada de KPI, backbone
        # ou manutencao aqui.
        p, _ = self._deck()
        txt = " ".join(sh.text_frame.text for s in p.slides
                       for sh in s.shapes if sh.has_text_frame)
        self.assertIn("Site Survey", txt)
        for fora in ("Backbone", "Manutenções", "Change Management",
                     "Inventário", "KPIs da Semana"):
            self.assertNotIn(fora, txt, fora)

    def test_dimensao_e_identidade(self):
        from pptx.util import Inches
        p, _ = self._deck()
        self.assertEqual(p.slide_width, Inches(13.333))
        self.assertEqual(p.slide_height, Inches(7.5))
        fontes = {r.font.name for s in p.slides for sh in s.shapes
                  if sh.has_text_frame for par in sh.text_frame.paragraphs
                  for r in par.runs if r.font.name}
        self.assertEqual(fontes, {"Calibri"})

    def test_todo_slide_tem_logo(self):
        p, _ = self._deck()
        sem = [i for i, s in enumerate(p.slides)
               if not any(sh.shape_type == 13 for sh in s.shapes)]
        self.assertEqual(sem, [], f"slides sem logo: {sem}")

    def _tit(self, s):
        return next((sh.text_frame.text.replace("\n", " ")
                     for sh in s.shapes if sh.has_text_frame
                     and sh.text_frame.text.strip()
                     and sh.text_frame.text.strip()[0] not in "◂›"), "")

    def test_capa_indice_e_sumario_nessa_ordem(self):
        # O indice entra logo apos a capa: e por ele que se navega.
        p, _ = self._deck()
        self.assertIn("Site Survey de Rede", self._tit(p.slides[0]))
        self.assertIn("Índice", self._tit(p.slides[1]))
        self.assertIn("Sumário", self._tit(p.slides[2]))

    def test_graficos_nativos_com_medicao_real(self):
        # Nativo e nao imagem: quem recebe pode editar os dados. E com
        # MEDICAO, nao com o valor de exemplo que o template trazia.
        p, _ = self._deck()
        graf = [sh.chart for s in p.slides for sh in s.shapes
                if getattr(sh, "has_chart", False)]
        self.assertTrue(graf, "nenhum grafico nativo")
        for ch in graf:
            vals = [v or 0 for v in ch.series[0].values]
            self.assertAlmostEqual(sum(vals), 100.0, delta=0.6)

    def test_distribuicao_soma_cem_e_respeita_as_faixas(self):
        am = [{"sinal": v} for v in (-92, -83, -78, -73, -68, -63, -58, -48)]
        rot, pct, un = rm.distribuicao(am, "sinal")
        self.assertEqual(un, "dBm")
        self.assertEqual(len(rot), len(rm.FAIXAS_KML["sinal"]))
        self.assertAlmostEqual(sum(pct), 100.0, delta=0.2)

    def test_distribuicao_sem_medicao_nao_inventa(self):
        rot, pct, un = rm.distribuicao([{"sinal": None}], "sinal")
        self.assertEqual(pct, [])

    def test_indice_liga_todos_os_slides(self):
        p, _ = self._deck()
        A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
        R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
        sl = list(p.slides)
        def alvo(sh, s):
            for hl in sh._element.iter(A + "hlinkClick"):
                rid = hl.get(R + "id")
                if not rid: continue
                try:
                    tp = s.part.rels[rid].target_part
                    return next(j for j, x in enumerate(sl) if x.part is tp)
                except Exception:
                    return "quebrado"
            return None
        idx = sl[1]
        alvos = [alvo(sh, idx) for sh in idx.shapes]
        alvos = [a for a in alvos if a is not None]
        self.assertGreaterEqual(len(alvos), len(sl) - 2)
        self.assertNotIn("quebrado", alvos)

    def test_todo_slide_tem_volta_para_o_indice(self):
        p, _ = self._deck()
        sem = [i for i, s in enumerate(p.slides) if i > 1 and not any(
            sh.has_text_frame and "ÍNDICE" in sh.text_frame.text
            for sh in s.shapes)]
        self.assertEqual(sem, [], f"slides sem volta: {sem}")

    def test_survey_sem_amostras_falha_com_motivo(self):
        con = rm.banco()
        sid = rm.survey_criar(con, "Vazio", time.time(), 10, ["X"])
        con.close()
        with self.assertRaises(RuntimeError) as c:
            rm.ppt_survey_anglo(sid)
        self.assertIn("sem amostras", str(c.exception))


class TestMarcaDemo(unittest.TestCase):
    """Deck bonito no template do cliente, com números plausíveis, circula
    internamente e vira 'o survey da mina'. A marca evita isso."""

    def tearDown(self):
        rm.MARCA_DEMO["ativa"] = False

    def test_desligada_por_padrao(self):
        self.assertFalse(rm.MARCA_DEMO["ativa"],
                         "relatório real não pode sair carimbado")

    def test_marca_entra_no_rodape_da_imagem(self):
        import matplotlib; matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        rm.MARCA_DEMO["ativa"] = True
        fig, ax = plt.subplots()
        rm._moldura(ax, "t", "sub")
        textos = " ".join(t.get_text() for t in ax.texts)
        plt.close(fig)
        self.assertIn("SINTÉTICOS", textos)

    def test_sem_marca_quando_desligada(self):
        import matplotlib; matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots()
        rm._moldura(ax, "t", "sub")
        textos = " ".join(t.get_text() for t in ax.texts)
        plt.close(fig)
        self.assertNotIn("SINTÉTICOS", textos)


class TestServidorPorAmostra(unittest.TestCase):
    """Sem saber QUEM serviu cada ponto não dá para dizer 'esta área é
    servida pelo ERB-03' — que é a frase que o survey existe para produzir."""

    def _am(self, seq, radio="CA-01"):
        return [{"radio": radio, "ts": 1000 + i*10, "lat": -27.73, "lon": -50.07,
                 "sinal": -70, "servidor": s} for i, s in enumerate(seq)]

    def test_amostra_grava_o_servidor(self):
        # A coluna tem de existir no INSERT, senão o dado morre na gravação.
        fonte = inspect.getsource(rm.amostras_gravar)
        self.assertIn("servidor", fonte)

    def test_melhor_enlace_preserva_quem_serviu(self):
        fonte = inspect.getsource(rm.CapturaGPS._amostra)
        self.assertIn('"servidor"', fonte)

    def test_estavel_nao_gera_handover(self):
        r = rm.analisar_servidores(self._am(["A"]*6))
        self.assertEqual(r["n_handovers"], 0)
        self.assertEqual(r["n_pingpong"], 0)

    def test_troca_de_servidor_e_handover(self):
        r = rm.analisar_servidores(self._am(["A","A","B","B"]))
        self.assertEqual(r["n_handovers"], 1)
        self.assertEqual(r["handovers"][0]["de"], "A")
        self.assertEqual(r["handovers"][0]["para"], "B")

    def test_pingpong_e_ida_e_volta_nao_cadeia(self):
        # A→B→A é disputa de área; A→B→C é só o veículo andando.
        self.assertEqual(rm.analisar_servidores(
            self._am(["A","B","A"]))["n_pingpong"], 1)
        self.assertEqual(rm.analisar_servidores(
            self._am(["A","B","C"]))["n_pingpong"], 0)

    def test_cobertura_por_bc_com_percentual(self):
        r = rm.analisar_servidores(self._am(["A","A","A","B"]))
        self.assertEqual(r["cobertura"]["A"]["amostras"], 3)
        self.assertEqual(r["cobertura"]["A"]["pct"], 75.0)

    def test_radio_sem_servidor_e_listado(self):
        r = rm.analisar_servidores(
            [{"radio": "X", "ts": 1, "lat": -27.7, "lon": -50.0, "sinal": -70}])
        self.assertEqual(r["radios_sem_servidor"], ["X"])

    def test_handover_de_um_radio_nao_contamina_outro(self):
        am = self._am(["A", "A"], "CA-01") + self._am(["B", "B"], "CA-02")
        self.assertEqual(rm.analisar_servidores(am)["n_handovers"], 0)


class TestMargemEGrade(unittest.TestCase):
    def test_margem_classifica_ok_marginal_fora(self):
        self.assertEqual(rm.margem_requisito(-60, "sinal"), (15.0, "ok"))
        # -73 passa contra -75, mas cai na primeira chuva.
        self.assertEqual(rm.margem_requisito(-73, "sinal"), (2.0, "marginal"))
        self.assertEqual(rm.margem_requisito(-80, "sinal"), (-5.0, "fora"))

    def test_margem_inverte_para_campo_de_menor_e_melhor(self):
        self.assertEqual(rm.margem_requisito(20, "rtt")[1], "ok")
        self.assertEqual(rm.margem_requisito(90, "rtt")[1], "marginal")
        self.assertEqual(rm.margem_requisito(150, "rtt")[1], "fora")

    def test_sem_medicao_nao_reprova(self):
        self.assertEqual(rm.margem_requisito(None, "sinal"), (None, None))
        self.assertEqual(rm.margem_requisito(-70, "vazao"), (None, None))

    def test_grade_usa_mediana_e_resiste_a_outlier(self):
        # Uma leitura absurda no meio não pode derrubar a célula.
        am = [{"radio": "CA-1", "ts": i, "lat": -27.7300, "lon": -50.0700,
               "sinal": v} for i, v in enumerate([-60, -61, -62, -63, -95])]
        c = rm.agregar_em_grade(am, "sinal", 50.0)
        self.assertEqual(len(c), 1)
        self.assertEqual(c[0]["valor"], -62)
        self.assertEqual(c[0]["n"], 5)
        self.assertEqual(c[0]["classe"], "ok")
        self.assertEqual(c[0]["pior"], -95)

    def test_grade_registra_o_servidor_dominante(self):
        am = [{"radio": "CA-1", "ts": i, "lat": -27.73, "lon": -50.07,
               "sinal": -65, "servidor": s}
              for i, s in enumerate(["A", "A", "B"])]
        self.assertEqual(rm.agregar_em_grade(am, "sinal", 50.0)[0]["servidor"],
                         "A")

    def test_grade_separa_celulas_distantes(self):
        am = [{"radio": "CA-1", "ts": 1, "lat": -27.7300, "lon": -50.0700,
               "sinal": -60},
              {"radio": "CA-1", "ts": 2, "lat": -27.7350, "lon": -50.0700,
               "sinal": -60}]      # ~555 m ao sul
        self.assertEqual(len(rm.agregar_em_grade(am, "sinal", 50.0)), 2)

    def test_grade_invalida_falha_em_vez_de_dividir_por_zero(self):
        with self.assertRaises(ValueError):
            rm.agregar_em_grade([], "sinal", 0)


class TestZonasProblema(unittest.TestCase):
    """A zona é a entrega que separa '34% dentro do requisito' de um
    survey de verdade: onde, quanto, quem servia e o que fazer."""

    LA0, PASSO = -27.7360, 0.0006          # ~66 m por passo

    def _malha(self, *faixas, servidor="ERB-02"):
        """Corredor de 40 passos; os passos das faixas saem reprovados.

        Aceita várias faixas para montar sombras separadas numa passada
        só: montar em duas chamadas misturaria amostra boa e ruim na
        mesma célula e a mediana esconderia a sombra.
        """
        am = []
        for k in range(40):
            la = self.LA0 + k * self.PASSO
            ruim = any(de <= k <= ate for de, ate in faixas)
            for r in ("CA-01", "CA-02"):
                am.append({"radio": r, "ts": 1000 + k*10,
                           "lat": la, "lon": -50.0700,
                           "sinal": -84.0 if ruim else -62.0,
                           "servidor": servidor if ruim else "ERB-01"})
        return am

    def test_sem_ponto_ruim_nao_inventa_zona(self):
        self.assertEqual(rm.zonas_problema(self._malha(), "sinal", 50.0), [])

    def test_acha_a_zona_onde_ela_foi_plantada(self):
        zs = rm.zonas_problema(self._malha((10, 16)), "sinal", 50.0)
        self.assertEqual(len(zs), 1)
        z = zs[0]
        # centro esperado: passo 13 do corredor
        esperado = self.LA0 + 13 * self.PASSO
        self.assertAlmostEqual(z["lat"], esperado, delta=self.PASSO * 1.5)
        self.assertEqual(z["servidor"], "ERB-02")
        self.assertEqual(z["n_radios"], 2)
        self.assertLess(z["valor_mediano"], -75)

    def test_ponto_ruim_isolado_nao_vira_zona(self):
        # Uma leitura ruim num ponto é ruído, não sombra.
        am = self._malha()
        am.append({"radio": "CA-01", "ts": 9999, "lat": -27.7100,
                   "lon": -50.0500, "sinal": -90.0, "servidor": "ERB-03"})
        self.assertEqual(rm.zonas_problema(am, "sinal", 50.0), [])

    def test_duas_sombras_separadas_viram_duas_zonas(self):
        am = self._malha((5, 9), (25, 29))
        self.assertEqual(len(rm.zonas_problema(am, "sinal", 50.0)), 2)

    def test_zonas_saem_ordenadas_da_maior_para_a_menor(self):
        am = self._malha((3, 5), (20, 30))
        zs = rm.zonas_problema(am, "sinal", 50.0)
        self.assertGreaterEqual(zs[0]["area_m2"], zs[-1]["area_m2"])

    def test_mancha_grande_e_marcada_como_sistemica(self):
        # 265.000 m² pode ser um bolsão ou a cava inteira — sugerir "um
        # rádio aqui" para o segundo caso engana a operação.
        zs = rm.zonas_problema(self._malha((0, 38)), "sinal", 50.0)
        self.assertTrue(zs[0]["sistemico"])
        self.assertGreater(zs[0]["pct_area"], 35)
        self.assertIn("replanejamento", rm.texto_zona(zs[0]))
        self.assertNotIn("Sugestão: avaliar rádio", rm.texto_zona(zs[0]))

    def test_bolsao_pequeno_recebe_sugestao_pontual(self):
        zs = rm.zonas_problema(self._malha((10, 13)), "sinal", 50.0)
        self.assertFalse(zs[0]["sistemico"])
        self.assertIn("Sugestão: avaliar rádio", rm.texto_zona(zs[0]))

    def test_texto_da_zona_traz_o_acionavel(self):
        z = rm.zonas_problema(self._malha((10, 14)), "sinal", 50.0)[0]
        t = rm.texto_zona(z)
        for parte in ("m de extensão", "% da área percorrida", "ERB-02",
                      "limite -75 dBm", "equipamento(s) afetado(s)"):
            self.assertIn(parte, t)

    def test_acao_recomendada_depende_da_grandeza(self):
        # Recomendar "mais um rádio" para zona de INTERFERÊNCIA está
        # errado: adensar não tira do ar quem ocupa o canal, e ainda soma
        # um transmissor na mesma faixa.
        am = [{"radio": "CA-1", "ts": k, "lat": -27.7360 + k*0.0006,
               "lon": -50.07, "interf": 70.0 if 5 <= k <= 12 else 2.0}
              for k in range(30)]
        t = rm.texto_zona(rm.zonas_problema(am, "interf", 50.0)[0], "interf")
        self.assertIn("troca de canal", t)
        self.assertNotIn("avaliar rádio em", t)

        z = rm.zonas_problema(self._malha((10, 14)), "sinal", 50.0)[0]
        self.assertIn("avaliar rádio em", rm.texto_zona(z, "sinal"))

    def test_zona_sistemica_de_interferencia_nao_manda_por_mais_radio(self):
        am = [{"radio": "CA-1", "ts": k, "lat": -27.7360 + k*0.0006,
               "lon": -50.07, "interf": 70.0} for k in range(30)]
        t = rm.texto_zona(rm.zonas_problema(am, "interf", 50.0)[0], "interf")
        self.assertIn("NÃO resolve", t)
        self.assertNotIn("densidade de malha", t)

    def test_milhar_nao_estraga_a_pontuacao_da_frase(self):
        # Um replace cego de vírgula levaria junto o separador das
        # coordenadas e as vírgulas do texto.
        self.assertEqual(rm._milhar(265000), "265.000")
        z = rm.zonas_problema(self._malha((10, 14)), "sinal", 50.0)[0]
        self.assertRegex(rm.texto_zona(z), r"-?\d+\.\d+, -?\d+\.\d+\.$")

    def test_zonas_por_latencia_usam_o_outro_sentido(self):
        am = [{"radio": "CA-1", "ts": k, "lat": -27.7360 + k*0.0006,
               "lon": -50.07, "rtt": 300.0 if 5 <= k <= 9 else 10.0}
              for k in range(30)]
        zs = rm.zonas_problema(am, "rtt", 50.0)
        self.assertEqual(len(zs), 1)
        self.assertGreater(zs[0]["valor_mediano"], 100)


class TestKmlSurvey(unittest.TestCase):
    """O KML existe porque a rede da mina bloqueia o satélite do PNG; o
    Google Earth já traz o dele."""

    NS = "{http://www.opengis.net/kml/2.2}"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cwd = os.getcwd(); os.chdir(self.tmp)
        self.cfg = rm.configparser.ConfigParser()
        rm.cfg_relatorio(self.cfg)
        self.sv = {"nome": "Turno manhã", "inicio": 1_700_000_000,
                   "fim": 1_700_003_600, "intervalo_s": 10,
                   "intervalo_efetivo_s": 14}
        self.am = []
        for k in range(12):
            self.am.append({
                "radio": "CA-1001", "ts": 1_700_000_000 + k * 10,
                "lat": -27.7300 + k * 1e-4, "lon": -50.0700 + k * 1e-4,
                "sinal": -60 - k * 2.5, "snr": 32 - k * 1.5,
                "ruido": -95, "rtt": 8 + k, "perda": 0.1 * k,
                "banda": "5.8 GHz",
                "fonte": "cache" if k == 5 else "direto"})
        self.am.append({"radio": "ERB-01", "ts": 1_700_000_000,
                        "lat": -27.7295, "lon": -50.0710, "sinal": -48,
                        "snr": 40, "banda": "5.8 GHz", "fonte": "direto"})

    def tearDown(self):
        os.chdir(self.cwd); shutil.rmtree(self.tmp, ignore_errors=True)

    def _arvore(self, **kw):
        import zipfile, io as _io
        from xml.etree import ElementTree as ET
        dados, nome = rm.gerar_kml_survey(
            self.sv, self.am, {"ERB-01": (-27.7295, -50.0710)},
            cfg=self.cfg, **kw)
        if nome.endswith(".kmz"):
            txt = zipfile.ZipFile(_io.BytesIO(dados)).read("doc.kml").decode()
        else:
            txt = dados.decode()
        return ET.fromstring(txt), txt, nome

    def test_xml_bem_formado_e_kmz_zipado(self):
        raiz, txt, nome = self._arvore()
        self.assertTrue(nome.endswith(".kmz"))
        self.assertEqual(raiz.tag, self.NS + "kml")

    def test_coordenadas_em_lon_lat(self):
        # Trocar a ordem joga a mina para o outro lado do planeta — é o
        # erro clássico de KML e não aparece em nenhuma validação de XML.
        raiz, _, _ = self._arvore()
        for c in raiz.iter(self.NS + "coordinates"):
            for tr in (c.text or "").split():
                lon, lat = (float(x) for x in tr.split(",")[:2])
                self.assertTrue(-51 < lon < -49, f"lon fora: {lon}")
                self.assertTrue(-28 < lat < -27, f"lat fora: {lat}")

    def _subpastas(self, raiz, aba):
        doc = raiz.find(self.NS + "Document")
        for f in doc.findall(self.NS + "Folder"):
            if (f.find(self.NS + "name").text or "").startswith(aba):
                return [g.find(self.NS + "name").text
                        for g in f.findall(self.NS + "Folder")]
        return []

    def test_pastas_esperadas(self):
        raiz, _, _ = self._arvore()
        doc = raiz.find(self.NS + "Document")
        nomes = [f.find(self.NS + "name").text
                 for f in doc.findall(self.NS + "Folder")]
        # Equipamento NAO entra por padrão: com uma dezena de BCs a camada
        # de alfinetes cobre justamente a medição.
        self.assertFalse(any(n.startswith("BreadCrumbs") for n in nomes), nomes)
        # A grandeza virou ABA; rotas e medições são subpastas dela.
        self.assertIn("RSSI", nomes)
        sub = self._subpastas(raiz, "RSSI")
        # Rotas saiu do padrão: o rastro agora e o GroundOverlay do calor.
        self.assertTrue(any(n.startswith("Medições") for n in sub), sub)
        self.assertTrue(any(n.startswith("Fora do requisito") for n in sub), sub)

    def test_equipamento_volta_quando_pedido(self):
        # Quem quer o inventario no mesmo arquivo liga a chave; o padrao e
        # nao poluir o mapa.
        self.cfg.set("relatorio", "kmz_com_equipamentos", "true")
        try:
            raiz, _, _ = self._arvore()
        finally:
            self.cfg.set("relatorio", "kmz_com_equipamentos", "false")
        doc = raiz.find(self.NS + "Document")
        nomes = [f.find(self.NS + "name").text
                 for f in doc.findall(self.NS + "Folder")]
        self.assertTrue(any(n.startswith("BreadCrumbs") for n in nomes), nomes)

    def test_uma_aba_por_grandeza_so_a_primeira_visivel(self):
        # Ligadas juntas, os pontos de seis grandezas se empilham no mesmo
        # lugar e o mapa nao diz nada.
        raiz, _, _ = self._arvore(campos=["sinal", "snr", "rtt"])
        doc = raiz.find(self.NS + "Document")
        vis = []
        for f in doc.findall(self.NS + "Folder"):
            v = f.find(self.NS + "visibility")
            if v is not None:
                vis.append((f.find(self.NS + "name").text, v.text))
        self.assertEqual([n for n, _ in vis], ["RSSI", "SNR", "Latência"])
        self.assertEqual([x for _, x in vis], ["1", "0", "0"])

    def test_campo_com_operador_menor_nao_quebra_o_xml(self):
        # "Fora do requisito — < 20 %" sem escape torna o arquivo INTEIRO
        # ilegivel, e nenhuma grandeza de "menor e melhor" abria.
        for campo in ("rtt", "perda", "interf", "ruido"):
            raiz, txt, _ = self._arvore(campo=campo)
            self.assertNotIn("— < ", txt)
            self.assertIsNotNone(raiz)

    def test_pasta_fora_do_requisito_conta_certo(self):
        raiz, _, _ = self._arvore(campo="sinal")
        esperado = sum(1 for a in self.am
                       if a.get("sinal") is not None and a["sinal"] <= -75)
        doc = raiz.find(self.NS + "Document")
        for aba in doc.findall(self.NS + "Folder"):
            for f in aba.findall(self.NS + "Folder"):
                nome = f.find(self.NS + "name").text or ""
                if nome.startswith("Fora do requisito"):
                    n = sum(1 for _ in f.iter(self.NS + "Placemark"))
                    self.assertEqual(n, esperado)
                    return
        self.assertTrue(esperado == 0, "pasta Fora sumiu tendo pontos ruins")

    def test_fora_do_requisito_comeca_desligada(self):
        # Ligada por padrão ela cobre os pontos bons e o mapa fica só
        # vermelho — a leitura honesta é ligar quando se quer triar.
        _, txt, _ = self._arvore()
        self.assertIn("<visibility>0</visibility>", txt)

    def test_cor_segue_a_faixa_da_grandeza(self):
        # Escala alinhada aos survey comerciais: -90..-45 dBm, cortes em
        # -50/-60/-67/-70/-80, e o -75 do requisito com faixa propria.
        for v, esperado in ((-95, "8B1A1A"), (-83, "C0392B"),
                            (-77, "E74C3C"), (-72, "E67E22"),
                            (-68, "F1C40F"), (-63, "9ACD32"),
                            (-55, "27AE60"), (-45, "1E8449")):
            self.assertEqual(rm._bucket_cor(v, rm.FAIXAS_KML["sinal"]),
                             esperado, f"{v} dBm")

    def test_faixas_sao_monotonas(self):
        # Limites fora de ordem fariam _bucket_cor devolver a cor errada
        # sem erro nenhum.
        for campo, faixas in rm.FAIXAS_KML.items():
            lims = [l for l, _ in faixas]
            self.assertEqual(lims, sorted(lims), campo)

    def test_sem_medicao_fica_cinza_nao_verde(self):
        self.assertEqual(rm._bucket_cor(None, rm.FAIXAS_KML["sinal"]), "808080")

    def test_balao_omite_campo_sem_medicao(self):
        b = rm._balao_amostra({"radio": "CA-1", "sinal": -70, "snr": None})
        self.assertIn("RSSI", b)
        self.assertNotIn("SNR", b, "campo sem medição virou linha na tabela")

    def test_balao_marca_amostra_vinda_do_cache(self):
        b = rm._balao_amostra({"radio": "CA-1", "sinal": -70, "fonte": "cache"})
        self.assertIn("cache do exporter", b)

    def test_balao_marca_valor_fora_do_requisito(self):
        self.assertIn("(fora)", rm._balao_amostra({"radio": "x", "sinal": -80}))
        self.assertNotIn("(fora)", rm._balao_amostra({"radio": "x", "sinal": -60}))

    def test_linha_do_tempo_em_cada_medicao(self):
        raiz, _, _ = self._arvore()
        n = sum(1 for _ in raiz.iter(self.NS + "TimeStamp"))
        self.assertEqual(n, len(self.am))

    def test_campo_invalido_falha_dizendo_o_que_vale(self):
        with self.assertRaises(ValueError) as c:
            rm.gerar_kml_survey(self.sv, self.am, cfg=self.cfg, campo="vazao")
        self.assertIn("sinal", str(c.exception))

    def test_survey_sem_posicao_falha_com_motivo(self):
        with self.assertRaises(RuntimeError) as c:
            rm.gerar_kml_survey(self.sv, [{"radio": "x", "ts": 1}],
                                cfg=self.cfg)
        self.assertIn("georreferenciado", str(c.exception))

    def test_decimacao_preserva_ordem_e_informa_o_passo(self):
        pts = list(range(100))
        saida, passo = rm._decimar(pts, 10)
        self.assertEqual(passo, 10)
        self.assertLessEqual(len(saida), 10)
        self.assertEqual(saida, sorted(saida))
        self.assertEqual(rm._decimar(pts, 0)[1], 1, "teto 0 = sem decimação")
        self.assertEqual(rm._decimar(pts, 500)[1], 1)

    def test_nome_com_aspas_nao_quebra_o_xml(self):
        from xml.etree import ElementTree as ET
        self.sv["nome"] = 'Cava <Norte> & "Sul"'
        raiz, _, _ = self._arvore()      # levantaria ParseError se escapasse mal
        self.assertIn("Cava", raiz.find(self.NS + "Document")
                      .find(self.NS + "name").text)

    def test_rota_sai_como_mapa_de_calor(self):
        # A SUPERFICIE de cobertura continua fora: ela pintava terreno
        # onde ninguem passou. O que entrou e o RASTRO em calor, com raio
        # limitado em volta de cada medicao — pedido depois de ver que a
        # linha ficava fina no satelite e ligava pontos distantes por
        # retas que ninguem percorreu.
        import zipfile, io as _io
        dados, _ = rm.gerar_kml_survey(self.sv, self.am, cfg=self.cfg,
                                       campo="sinal")
        z = zipfile.ZipFile(_io.BytesIO(dados))
        pngs = [n for n in z.namelist() if n.endswith(".png")]
        self.assertTrue(pngs, "KMZ saiu sem o raster do calor")
        doc = z.read("doc.kml").decode()
        self.assertIn("<GroundOverlay>", doc)
        self.assertIn("<LatLonBox>", doc)   # sem georreferencia ele escorrega

    def test_calor_so_onde_passou(self):
        # O que separa "medi aqui" de "acho que la deve dar": fora do raio
        # das amostras o raster tem de ser TRANSPARENTE.
        import zipfile, io as _io, numpy as np
        import matplotlib; matplotlib.use("Agg")
        import matplotlib.image as mpimg
        dados, _ = rm.gerar_kml_survey(self.sv, self.am, cfg=self.cfg,
                                       campo="sinal")
        z = zipfile.ZipFile(_io.BytesIO(dados))
        png = [n for n in z.namelist() if n.endswith(".png")][0]
        img = mpimg.imread(_io.BytesIO(z.read(png)))
        alfa = img[..., 3]
        self.assertGreater((alfa < 0.02).mean(), 0.4,
                           "o calor cobriu quase tudo: virou cobertura")
        self.assertGreater(alfa.max(), 0.5, "o rastro saiu apagado")

    def test_radio_parado_nao_vira_bola_no_calor(self):
        # Um BC fixo dá dezenas de amostras no MESMO ponto: virava uma
        # bola isolada, e como ele enxerga o vizinho de perto, saía verde.
        # Eram as "bolas espalhadas e desconectadas" — e boa parte do
        # verde que nao batia com a mina.
        import zipfile, io as _io, math
        import matplotlib; matplotlib.use("Agg")
        import matplotlib.image as mpimg
        am = []
        for k in range(30):        # movel RUIM, andando
            am.append({"radio": "CA-1", "lat": -27.730 + k*3e-4,
                       "lon": -50.070, "ts": k*5, "banda": "5.8 GHz",
                       "sinal": -80.0, "snr": 12.0, "ruido": -92.0})
        for k in range(30):        # fixo OTIMO, parado num ponto
            am.append({"radio": "ERB-9", "lat": -27.7255, "lon": -50.0669,
                       "ts": k*5, "banda": "5.8 GHz",
                       "sinal": -50.0, "snr": 42.0, "ruido": -95.0})
        dados, _ = rm.gerar_kml_survey(
            {"id": 1, "nome": "T", "inicio": 1756000000},
            am, {"ERB-9": (-27.7255, -50.0669)}, cfg=self.cfg, campo="sinal")
        z = zipfile.ZipFile(_io.BytesIO(dados))
        png = [n for n in z.namelist() if n.endswith(".png")][0]
        img = mpimg.imread(_io.BytesIO(z.read(png)))
        rgb, alfa = img[..., :3], img[..., 3]
        pintado = alfa > 0.4
        self.assertTrue(pintado.any(), "nao pintou nada")
        # Onde o BC PARADO esta, o raster tem de ser transparente: ele nao
        # percorreu nada. Olhar a media do quadro inteiro nao serve — o
        # rastro do movel tem area muito maior e afogaria a bola.
        doc = z.read("doc.kml").decode()
        import re as _re
        cx = {t: float(_re.search(rf"<{t}>([-\d.]+)</{t}>", doc).group(1))
              for t in ("north", "south", "east", "west")}
        n = img.shape[0]
        # O PNG vai com norte no topo (flipud na gravacao).
        li = int((cx["north"] + 27.7255) / (cx["north"] - cx["south"]) * (n - 1))
        co = int((-50.0669 - cx["west"]) / (cx["east"] - cx["west"]) * (n - 1))
        if 0 <= li < n and 0 <= co < n:
            janela = alfa[max(0, li-6):li+7, max(0, co-6):co+7]
            self.assertLess(janela.max(), 0.25,
                            "o radio parado pintou uma bola no mapa "
                            f"(alfa {janela.max():.2f} na posicao dele)")
        # E o rastro do movel, a -80 dBm (abaixo do requisito -75), tem de
        # sair quente.
        med = rgb[pintado].mean(axis=0)
        self.assertGreater(med[0], med[1],
                           f"rastro saiu esverdeado (RGB {med})")

    def test_raio_acompanha_o_espacamento(self):
        # Raio menor que o vao entre amostras => nucleos nao se encontram
        # e o rastro vira colar de contas.
        fonte = inspect.getsource(rm.gerar_kml_survey)
        self.assertIn("espacamento_tipico(amostras_aba)", fonte)
        i = fonte.index("espacamento_tipico(amostras_aba)")
        trecho = fonte[i:i+260]
        self.assertIn("raio", trecho)

    def test_rotas_desligadas_por_padrao(self):
        # A linha inventava aresta reta entre pontos distantes.
        _, txt, _ = self._arvore()
        self.assertNotIn("<name>Rotas", txt)

    def test_rota_volta_quando_pedida(self):
        self.cfg.set("relatorio", "kmz_com_rotas", "true")
        try:
            _, txt, _ = self._arvore()
        finally:
            self.cfg.set("relatorio", "kmz_com_rotas", "false")
        self.assertIn("<name>Rotas", txt)

    def test_cada_medicao_tem_a_cor_da_escala(self):
        # Cor por AMOSTRA, no gradiente continuo — nao mais oito faixas
        # fixas, em que -74,9 e -75,1 dBm saiam em cores diferentes.
        e = rm.ESCALAS["sinal"]
        cores = [rm.cor_continua(v, e) for v in (-90, -80, -70, -60)]
        self.assertEqual(len(set(cores)), 4, "faixas distintas colidiram")
        # extremos batem com as pontas da escala
        self.assertEqual(rm.cor_continua(-95, e), rm.cor_continua(-90, e))
        self.assertEqual(rm.cor_continua(-40, e), rm.cor_continua(-55, e))
        self.assertIsNone(rm.cor_continua(None, e))

    def test_gradiente_e_monotono(self):
        # Sinal melhor nao pode voltar para a cor de sinal pior.
        e = rm.ESCALAS["sinal"]
        vistos = [rm.cor_continua(v, e) for v in range(-90, -54, 3)]
        self.assertEqual(len(vistos), len(set(vistos)) if
                         len(set(vistos)) == len(vistos) else len(vistos))
        self.assertNotEqual(vistos[0], vistos[-1])

    def test_campo_de_menor_e_melhor_inverte_o_gradiente(self):
        # Latencia alta tem de ser vermelha, nao verde.
        rtt = rm.ESCALAS["rtt"]
        self.assertEqual(rm.cor_continua(0, rtt), rm.cor_continua(-10, rtt))
        self.assertNotEqual(rm.cor_continua(10, rtt), rm.cor_continua(190, rtt))

    def test_todo_estilo_usado_na_rota_existe(self):
        # styleUrl sem Style faz o Google Earth desenhar linha branca.
        import re as _re
        _, txt, _ = self._arvore()
        usados = set(_re.findall(r"<styleUrl>#([rq][0-9A-F]{6})</styleUrl>", txt))
        defin = set(_re.findall(r'<Style id="([rq][0-9A-F]{6})">', txt))
        self.assertTrue(usados)
        self.assertEqual(usados - defin, set())

    def test_so_as_cores_usadas_viram_estilo(self):
        # Emitir as 40 do gradiente vezes seis grandezas encheria o
        # arquivo de estilo morto.
        import re as _re
        _, txt, _ = self._arvore()
        usados = set(_re.findall(r"<styleUrl>#r([0-9A-F]{6})</styleUrl>", txt))
        defin = set(_re.findall(r'<Style id="r([0-9A-F]{6})">', txt))
        self.assertEqual(defin - usados, set(), "estilo definido sem uso")

    def test_rota_funde_trechos_de_mesma_cor(self):
        # Um Placemark por PAR de pontos dava mais de mil objetos por
        # radio, pesados de abrir e com emenda visivel entre segmentos.
        _, txt, _ = self._arvore()
        import re as _re
        comp = [len(c.split()) for c in
                _re.findall(r"<coordinates>([^<]*)</coordinates>", txt)]
        self.assertTrue(any(c > 2 for c in comp),
                        "nenhuma polilinha com mais de 2 pontos")

    def test_rota_tem_contorno_escuro(self):
        # Truque de cartografia: contorno por baixo deixa a linha legivel
        # tanto sobre satelite claro quanto escuro. So vale com a linha
        # ligada — no padrao o rastro e o calor.
        self.cfg.set("relatorio", "kmz_com_rotas", "true")
        try:
            _, txt, _ = self._arvore()
        finally:
            self.cfg.set("relatorio", "kmz_com_rotas", "false")
        self.assertIn('<Style id="lcontorno">', txt)
        self.assertIn("#lcontorno", txt)

    def test_rota_nao_marca_inicio_nem_fim(self):
        # Retirados a pedido: com uma dezena de equipamentos, viravam duas
        # dezenas de alfinetes sem informacao de medicao nenhuma.
        _, txt, _ = self._arvore()
        self.assertNotIn("— inicio", txt)
        self.assertNotIn("— fim", txt)
        self.assertNotIn("rotaIni", txt)
        self.assertNotIn("rotaFim", txt)

    def test_cor_da_rota_nao_e_suavizada(self):
        # Foi pedido explicitamente: cada medicao com a SUA cor. A
        # mediana movel que existia aqui suavizava o tracado e escondia
        # a variacao ponto a ponto.
        fonte = inspect.getsource(rm.gerar_kml_survey)
        self.assertNotIn("mediana movel", fonte.replace("ó", "o"))
        self.assertIn("cor_continua(v, esc_a)", fonte)

    def test_rota_ligada_sai_como_fita_visivel(self):
        # Quando se pede a linha, ela tem de LER sobre o satelite da cava,
        # que e claro e cheio de textura: com 2,6 px e alfa baixo parecia
        # risco de GPS, nao medicao.
        self.cfg.set("relatorio", "kmz_com_rotas", "true")
        try:
            _, txt, _ = self._arvore()
        finally:
            self.cfg.set("relatorio", "kmz_com_rotas", "false")
        self.assertIn("<width>7</width>", txt)
        self.assertIn('id="lcontorno"', txt)

    def test_interpolacao_nao_inventa_cobertura(self):
        # Fora do raio fica NaN -> transparente. Sem isso o mapa pintaria
        # area onde a frota nunca passou.
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy indisponivel")
        try:
            import scipy  # noqa: F401
        except ImportError:
            self.skipTest("scipy indisponivel")
        bb = {"sul": -27.74, "norte": -27.72, "oeste": -50.08, "leste": -50.05}
        G = rm.grade_idw_m([-50.07], [-27.73], [-60.0], bb, -27.73,
                           raio_m=100.0, n=60)
        self.assertTrue(np.isnan(G).any(), "preencheu tudo, sem buraco")
        self.assertFalse(np.isnan(G).all(), "nao preencheu nada")

    def test_kml_sem_compressao_quando_pedido(self):
        dados, nome = rm.gerar_kml_survey(self.sv, self.am, cfg=self.cfg,
                                          comprimir=False)
        self.assertTrue(nome.endswith(".kml"))
        self.assertTrue(dados.startswith(b"<?xml"))


class TestLimpezaLocalidade(unittest.TestCase):
    """Os campos manuais de local/cava/áreas saem dos slides — mas só
    eles: '% da área' e 'Área ≥ -75 dBm' são cobertura medida."""

    def test_rotulo_de_localidade_e_reconhecido(self):
        for t in ("ÁREAS PERCORRIDAS", "Areas percorridas", "LOCALIDADES"):
            self.assertTrue(rm._eh_bloco_de_localidade(t), t)

    def test_lista_de_bolinhas_vazias_e_reconhecida(self):
        self.assertTrue(rm._eh_bloco_de_localidade(
            "●  ____________________\n●  ____________________"))

    def test_bolinha_com_conteudo_real_e_preservada(self):
        self.assertFalse(rm._eh_bloco_de_localidade(
            "●  Cava Norte\n●  ____________________"))

    def test_cobertura_medida_nao_e_localidade(self):
        # Apagar estes tiraria número medido do relatório.
        for t in ("____ % da área dentro dos valores requeridos (RSSI > -75 dBm).",
                  "Área ≥ -75 dBm: ______ %",
                  "Perda de pacotes acima de 2% em ____ % da área percorrida.",
                  "RSSI previsto em toda a área a partir das posições reais"):
            self.assertFalse(rm._eh_bloco_de_localidade(t), t)

    def test_linha_de_percurso_e_reconhecida_dentro_do_bloco(self):
        self.assertTrue(rm._RE_LINHA_LOCALIDADE.search(
            "•  Kit fixado ao teto de veículo 4x4; percurso pelas"))
        self.assertTrue(rm._RE_LINHA_LOCALIDADE.search(
            "    áreas de operação: ____________________"))
        self.assertFalse(rm._RE_LINHA_LOCALIDADE.search(
            "•  Período da coleta: ____ / ____ a ____ / ____"))

    def test_titulo_de_survey_nunca_e_sobrescrito(self):
        # O título contém a palavra procurada ("… — Metodologia e
        # Requisitos"); a busca ingênua o apagava.
        for t in ("5. Site Survey — Metodologia e Requisitos — 5.8 GHz",
                  "5. Site Survey — Cobertura Estimada da Mina",
                  "Base: survey passivo (adaptador -99 dBm) + ativo",
                  "Banda: ______  |  Data: ____/____/____"):
            self.assertTrue(rm._eh_titulo_survey(t), t)
        self.assertFalse(rm._eh_titulo_survey(
            "•  Survey passivo: adaptador wireless externo"))

    def test_rotulo_de_secao_nao_recebe_o_texto(self):
        self.assertTrue(rm._eh_rotulo_secao("METODOLOGIA"))
        self.assertTrue(rm._eh_rotulo_secao("REQUISITOS MODULAR MINING"))
        self.assertFalse(rm._eh_rotulo_secao(
            "Fora dos requisitos, as aplicações Dispatch degradam"))


class TestMapaDaRede(unittest.TestCase):
    def _cfg(self):
        cfg = rm.configparser.ConfigParser()
        with mock.patch.object(rm, "CONFIG_FILE",
                               str(Path(tempfile.mkdtemp()) / "c.ini")):
            return rm.cfg_relatorio(cfg)

    def test_conta_por_tipo_e_sem_posicao(self):
        am = [{"radio": "ERB-01", "lat": -27.7, "lon": -50.0, "ts": 1},
              {"radio": "ERM-02", "lat": -27.7, "lon": -50.0, "ts": 1},
              {"radio": "CA-1001", "lat": -27.7, "lon": -50.0, "ts": 1},
              {"radio": "CA-1002", "lat": None, "lon": None, "ts": 1}]
        r = rm.contar_rede(am, ["ERB-01", "ERM-02", "CA-1001", "CA-1002",
                                "PF-09"], self._cfg())
        self.assertEqual(r["tipos"]["ERB"], 1)
        self.assertEqual(r["tipos"]["ERM"], 1)
        self.assertEqual(r["tipos"]["Móvel"], 1)
        self.assertEqual(r["com_gps"], 3)
        # Quem participou e nunca reportou fix — dado de manutenção.
        self.assertEqual(r["sem_posicao"], ["CA-1002", "PF-09"])

    def test_participante_ausente_das_amostras_conta_como_sem_posicao(self):
        r = rm.contar_rede([], ["CA-1", "CA-2"], self._cfg())
        self.assertEqual(r["sem_posicao"], ["CA-1", "CA-2"])
        self.assertEqual(r["com_gps"], 0)

    def test_gera_png_com_marcadores_e_rotas(self):
        tmp = tempfile.mkdtemp()
        try:
            am = []
            for k in range(6):
                am.append({"radio": "CA-1001", "ts": k,
                           "lat": -27.730 + k*1e-4, "lon": -50.070 + k*1e-4})
            am.append({"radio": "ERM-01", "ts": 0, "lat": -27.7315,
                       "lon": -50.0685})
            caminho, resumo = rm.gerar_mapa_rede(
                {"nome": "S", "inicio": time.time()}, am,
                {"ERB-01": (-27.7295, -50.0710)}, tmp, cfg=self._cfg(),
                radios_survey=["CA-1001", "ERM-01", "ERB-01", "PF-77"])
            self.assertTrue(Path(caminho).exists())
            self.assertGreater(Path(caminho).stat().st_size, 5000)
            self.assertEqual(resumo["rotas"], 1)
            self.assertEqual(resumo["tipos"]["ERB"], 1)
            self.assertEqual(resumo["sem_posicao"], ["PF-77"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_sem_posicao_nenhuma_nao_levanta(self):
        tmp = tempfile.mkdtemp()
        try:
            self.assertEqual(
                rm.gerar_mapa_rede({"nome": "S"}, [], {}, tmp,
                                   cfg=self._cfg()), (None, {}))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_aspecto_geografico_vem_depois_do_imshow(self):
        # Cada imshow(aspect="auto") reseta o aspecto dos eixos.
        fonte = inspect.getsource(rm.gerar_mapa_rede)
        self.assertLess(fonte.index("imshow"), fonte.index("_aspecto_geo"))

    def test_rotas_sem_cor_de_qualidade(self):
        # Colorir aqui competiria com os slides de RSSI/SNR.
        fonte = inspect.getsource(rm.gerar_mapa_rede)
        self.assertIn('color="#B8C4D8"', fonte)
        self.assertNotIn("LineCollection", fonte)


class TestBlocoDeTexto(unittest.TestCase):
    """_ppt_set preenche lacuna; bloco de vários parágrafos precisa de
    _ppt_set_bloco, senão sobra a versão antiga da história."""

    class _Run:
        def __init__(self, t=""):
            self.text = t
            self.font = types.SimpleNamespace(
                name="Calibri", size=None, bold=False,
                color=types.SimpleNamespace(rgb=None))
            self._r = None

    def test_usa_bloco_quando_o_texto_tem_varias_linhas(self):
        fonte = inspect.getsource(rm.preencher_slides_survey)
        self.assertIn("_ppt_set_bloco(tf, novo)", fonte)

    def test_metodologia_ancora_no_conteudo_nao_no_rotulo(self):
        # O corpo não contém a palavra "metodologia"; o rótulo sim.
        fonte = inspect.getsource(rm.preencher_slides_survey)
        self.assertIn('"survey contínuo pelos rádios"', fonte)
        self.assertIn('"survey passivo"', fonte)


class TestConfigSurvey(unittest.TestCase):
    def test_defaults_entram_junto_com_relatorio(self):
        # Em arquivo separado o operador nunca descobriria que existem.
        cfg = rm.configparser.ConfigParser()
        with mock.patch.object(rm, "CONFIG_FILE",
                               str(Path(tempfile.mkdtemp()) / "c.ini")):
            rm.cfg_relatorio(cfg)
        self.assertTrue(cfg.has_section("survey"))
        for k in ("max_threads", "timeout_s", "min_intervalo_s",
                  "falhas_para_pular", "usar_cache_fallback"):
            self.assertTrue(cfg.has_option("survey", k), k)


class TestIdentidadeNoSemanal(unittest.TestCase):
    """O semanal vira Anglo repintando o template do cliente, no fim da
    geração. O que se testa aqui é que a pele muda e o conteúdo não."""

    def _deck(self):
        """Um deck mínimo na paleta do template: capa + um slide de
        conteúdo com cartão, tabela e botão."""
        from pptx import Presentation
        from pptx.util import Inches, Pt
        from pptx.dml.color import RGBColor
        from pptx.enum.shapes import MSO_SHAPE

        p = Presentation()
        p.slide_width, p.slide_height = Inches(13.333), Inches(7.5)
        vazio = p.slide_masters[0].slide_layouts[6]

        def texto(s, x, y, w, h, txt, cor, tam=12):
            tb = s.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
            r = tb.text_frame.paragraphs[0].add_run(); r.text = txt
            r.font.name = "Calibri"; r.font.size = Pt(tam)
            r.font.color.rgb = RGBColor.from_string(cor)
            return tb

        # capa
        capa = p.slides.add_slide(vazio)
        capa.background.fill.solid()
        capa.background.fill.fore_color.rgb = RGBColor.from_string("0D2052")
        texto(capa, 0.9, 2.3, 11.5, 0.9, "RELATÓRIO SEMANAL", "FFFFFF", 40)
        # Subtítulo em tom de corpo: no template ele é A0B0CC, que vira
        # cinza — e cinza sobre o azul da capa some. Está aqui porque foi
        # exatamente o que apareceu no arquivo do cliente.
        texto(capa, 0.9, 3.25, 11.5, 0.45,
              "Infraestrutura de Telecom | Tecnologia de Mina", "A0B0CC", 16)

        # conteúdo
        s = p.slides.add_slide(vazio)
        s.background.fill.solid()
        s.background.fill.fore_color.rgb = RGBColor.from_string("0D2052")
        texto(s, 0.55, 0.32, 11.3, 0.6, "1. Dashboard Executivo", "FFFFFF", 28)
        texto(s, 0.55, 0.92, 11.3, 0.32, "Resumo da semana", "A0B0CC")
        cart = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(0.55),
                                  Inches(1.55), Inches(4.0), Inches(1.5))
        cart.fill.solid(); cart.fill.fore_color.rgb = RGBColor.from_string("1A2744")
        cart.line.color.rgb = RGBColor.from_string("2A3A5C")
        # Rótulo BRANCO em cima do cartão: o cartão clareia, então este
        # branco tem de virar escuro ou o texto some. É o caso dos 174
        # runs brancos do template do cliente.
        texto(s, 0.75, 1.70, 3.6, 0.3, "DISPONIBILIDADE DA MALHA", "FFFFFF", 11)
        texto(s, 0.75, 2.05, 3.6, 0.85, "99,12%", "C8D0E0", 30)
        gf = s.shapes.add_table(2, 2, Inches(0.55), Inches(3.3),
                                Inches(6.0), Inches(0.8))
        for j, cab in enumerate(("Indicador", "Valor")):
            cel = gf.table.cell(0, j)
            cel.fill.solid(); cel.fill.fore_color.rgb = RGBColor.from_string("1A3A7A")
            r = cel.text_frame.paragraphs[0].add_run(); r.text = cab
            r.font.color.rgb = RGBColor.from_string("FFFFFF")
        for j, v in enumerate(("Disponibilidade", "OK")):
            cel = gf.table.cell(1, j)
            cel.fill.solid(); cel.fill.fore_color.rgb = RGBColor.from_string("152238")
            r = cel.text_frame.paragraphs[0].add_run(); r.text = v
            r.font.color.rgb = RGBColor.from_string("27AE60")
        bt = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(12.05),
                                Inches(0.28), Inches(1.05), Inches(0.34))
        bt.fill.solid(); bt.fill.fore_color.rgb = RGBColor.from_string("1A2744")
        r = bt.text_frame.paragraphs[0].add_run(); r.text = "◂ MENU"
        r.font.color.rgb = RGBColor.from_string("FFFFFF")
        bt.click_action.target_slide = capa
        return p

    # ── a pele muda ───────────────────────────────────────────
    def test_fundo_de_conteudo_vira_branco_e_a_capa_vira_azul(self):
        p = self._deck(); rm.aplicar_identidade_anglo(p)
        self.assertEqual(str(p.slides[0].background.fill.fore_color.rgb),
                         rm.ANGLO["azul"])
        self.assertEqual(str(p.slides[1].background.fill.fore_color.rgb),
                         rm.ANGLO["fundo"])

    def test_nao_sobra_cor_do_template_escuro(self):
        import io, re, zipfile, collections
        p = self._deck(); rm.aplicar_identidade_anglo(p)
        buf = io.BytesIO(); p.save(buf)
        z = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
        achadas = collections.Counter()
        for n in z.namelist():
            if not re.match(r"ppt/(slides|charts)/.*\.xml$", n): continue
            txt = z.read(n).decode("utf8")
            for c in re.findall(r'srgbClr val="([0-9A-Fa-f]{6})"', txt):
                achadas[c.upper()] += 1
        antigas = set(rm.ANGLO_FUNDOS) | set(rm.ANGLO_TEXTOS)
        # As cores de status voltam a valer sobre fundo escuro (a capa),
        # então elas podem reaparecer; as estruturais, não.
        estruturais = set(rm.ANGLO_FUNDOS)
        sobrou = {c: k for c, k in achadas.items() if c in estruturais}
        self.assertEqual(sobrou, {}, f"cor do template escuro sobrou: {sobrou}")
        self.assertTrue(antigas, "mapa de cores vazio invalida o teste")

    def test_texto_claro_vira_escuro_sobre_o_fundo_branco(self):
        p = self._deck(); rm.aplicar_identidade_anglo(p)
        achou = False
        for sh in p.slides[1].shapes:
            if sh.has_text_frame and "99,12" in sh.text_frame.text:
                cor = str(sh.text_frame.paragraphs[0].runs[0].font.color.rgb)
                self.assertEqual(cor, rm.ANGLO["texto"]); achou = True
        self.assertTrue(achou, "o valor do cartão sumiu do slide")

    def test_branco_continua_branco_onde_o_fundo_seguiu_escuro(self):
        # Na capa o fundo vira azul institucional: virar o texto escuro
        # apagaria o título.
        p = self._deck(); rm.aplicar_identidade_anglo(p)
        for sh in p.slides[0].shapes:
            if sh.has_text_frame and "RELATÓRIO" in sh.text_frame.text:
                self.assertEqual(
                    str(sh.text_frame.paragraphs[0].runs[0].font.color.rgb),
                    "FFFFFF")
                return
        self.fail("titulo da capa sumiu")

    def test_borda_de_celula_e_repintada(self):
        # As bordas moram em lnL/lnR/lnT/lnB dentro do tcPr e NÃO passam
        # pelo python-pptx. Sem tratá-las, sobra a grade azul-escura
        # riscando o fundo branco.
        from pptx.util import Inches
        from lxml import etree
        p = self._deck()
        tab = [sh for sh in p.slides[1].shapes if sh.has_table][0].table
        tc = tab.cell(0, 0)._tc
        tcPr = tc.find(f"{rm._A_NS}tcPr")
        if tcPr is None:
            tcPr = etree.SubElement(tc, f"{rm._A_NS}tcPr")
        ln = etree.SubElement(tcPr, f"{rm._A_NS}lnL")
        sf = etree.SubElement(ln, f"{rm._A_NS}solidFill")
        etree.SubElement(sf, f"{rm._A_NS}srgbClr").set("val", "2A3A5C")
        rm.aplicar_identidade_anglo(p)
        cores = [c.get("val") for c in tab._tbl.iter(f"{rm._A_NS}srgbClr")]
        self.assertNotIn("2A3A5C", cores)
        self.assertIn(rm.ANGLO["linha"], cores)

    # ── o conteúdo não muda ───────────────────────────────────
    def test_o_link_do_botao_sobrevive(self):
        p = self._deck(); rm.aplicar_identidade_anglo(p)
        ligados = 0
        for sh in p.slides[1].shapes:
            try:
                if sh.click_action.target_slide is not None: ligados += 1
            except Exception:
                pass
        self.assertEqual(ligados, 1, "a repintura levou o hyperlink junto")

    def test_nem_um_texto_se_perde(self):
        p = self._deck()
        antes = sorted(sh.text_frame.text for s in p.slides
                       for sh in s.shapes if sh.has_text_frame)
        rm.aplicar_identidade_anglo(p)
        depois = sorted(sh.text_frame.text for s in p.slides
                        for sh in s.shapes if sh.has_text_frame)
        # A repintura só ACRESCENTA (logo e régua não têm texto).
        for t in antes:
            self.assertIn(t, depois)

    def test_botao_de_voltar_fica_azul_com_texto_branco(self):
        p = self._deck(); rm.aplicar_identidade_anglo(p)
        for sh in p.slides[1].shapes:
            if sh.has_text_frame and sh.text_frame.text.strip().startswith("◂"):
                self.assertEqual(str(sh.fill.fore_color.rgb), rm.ANGLO["azul"])
                self.assertEqual(
                    str(sh.text_frame.paragraphs[0].runs[0].font.color.rgb),
                    rm.ANGLO["fundo"])
                return
        self.fail("botao de voltar sumiu")

    # ── a marca ───────────────────────────────────────────────
    def test_cada_slide_de_conteudo_ganha_regua(self):
        p = self._deck(); rm.aplicar_identidade_anglo(p)
        self.assertTrue(any(sh.name == "anglo_regua"
                            for sh in p.slides[1].shapes))
        # a capa não leva régua: ela é toda azul
        self.assertFalse(any(sh.name == "anglo_regua"
                             for sh in p.slides[0].shapes))

    def test_regua_nao_entra_por_cima_de_conteudo(self):
        from pptx.util import Inches
        from pptx.enum.shapes import MSO_SHAPE
        p = self._deck()
        s = p.slides[1]
        intruso = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.55),
                                     Inches(rm.ANGLO_SEMANAL["regua_y"] - 0.05),
                                     Inches(5.0), Inches(0.3))
        rm.aplicar_identidade_anglo(p)
        self.assertFalse(any(sh.name == "anglo_regua" for sh in s.shapes),
                         "regua desenhada em cima do conteudo")

    def test_slide_que_ja_nasceu_anglo_nao_ganha_segunda_marca(self):
        # No modo unido o deck de survey é anexado já na identidade: um
        # segundo logo cairia em cima do primeiro.
        p = self._deck()
        n1 = rm.aplicar_identidade_anglo(p)["marcas"]
        n2 = rm.aplicar_identidade_anglo(p)["marcas"]
        self.assertGreater(n1, 0)
        self.assertEqual(n2, 0, "a segunda passagem marcou de novo")

    def test_titulo_sai_da_area_do_logo(self):
        from pptx.util import Emu
        p = self._deck(); rm.aplicar_identidade_anglo(p)
        for sh in p.slides[1].shapes:
            if sh.has_text_frame and "Dashboard" in sh.text_frame.text:
                self.assertAlmostEqual(Emu(sh.left).inches,
                                       rm.ANGLO_SEMANAL["titulo"][0], places=2)
                self.assertEqual(
                    str(sh.text_frame.paragraphs[0].runs[0].font.color.rgb),
                    rm.ANGLO["azul"])
                return
        self.fail("titulo sumiu")

    # ── contraste ─────────────────────────────────────────────
    def test_todo_texto_fica_legivel_sobre_o_que_ficou_atras(self):
        def lum(h):
            def canal(i):
                v = int(h[i:i+2], 16) / 255
                return v/12.92 if v <= 0.03928 else ((v+0.055)/1.055)**2.4
            return 0.2126*canal(0) + 0.7152*canal(2) + 0.0722*canal(4)
        def razao(a, b):
            la, lb = lum(a), lum(b)
            return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)

        p = self._deck(); rm.aplicar_identidade_anglo(p)
        piores = []
        for s in p.slides:
            formas = list(rm._formas_planas(s.shapes))
            fundo_s = str(s.background.fill.fore_color.rgb).upper()
            alvos = []
            for sh in formas:
                if sh.has_text_frame:
                    f = rm._hex_do_fill(sh)
                    if f is None:
                        atras = rm._cartao_atras(sh, formas)
                        f = rm._hex_do_fill(atras) if atras is not None else fundo_s
                    alvos.append((sh.text_frame, f))
                if sh.has_table:
                    for row in sh.table.rows:
                        for cel in row.cells:
                            alvos.append((cel.text_frame,
                                          rm._hex_do_fill(cel) or fundo_s))
            for tf, fd in alvos:
                for par in tf.paragraphs:
                    for r in par.runs:
                        try: hx = str(r.font.color.rgb).upper()
                        except Exception: continue
                        rz = razao(hx, fd or "FFFFFF")
                        if rz < 3.0:
                            piores.append((round(rz, 2), hx, fd, r.text[:20]))
        self.assertEqual(piores, [], f"texto ilegivel apos a repintura: {piores}")

    # ── ligação com a geração ─────────────────────────────────
    def test_gerar_ppt_aplica_por_padrao_e_por_ultimo(self):
        fonte = inspect.getsource(rm.gerar_ppt)
        self.assertIn('"identidade_anglo", fallback=True', fonte)
        # Repintar antes de preencher deixaria de fora o verde/ambar/
        # vermelho do status e os slides tecnicos.
        self.assertLess(fonte.index("_slides_tecnicos(p, d, cfg"),
                        fonte.index("aplicar_identidade_anglo(p)"))
        self.assertLess(fonte.index("anexar_survey_ao_ppt(p, cfg, survey)"),
                        fonte.index("aplicar_identidade_anglo(p)"))

    def test_pode_ser_desligada(self):
        cfg = rm.configparser.ConfigParser()
        with mock.patch.object(rm, "CONFIG_FILE",
                               str(Path(tempfile.mkdtemp()) / "c.ini")):
            rm.cfg_relatorio(cfg)
        self.assertTrue(cfg.has_option("relatorio", "identidade_anglo"))
        self.assertTrue(cfg.getboolean("relatorio", "identidade_anglo"))


def _deck_de_secoes(titulos_e_subtitulos):
    """Deck com um slide por (título, subtítulo), no leiaute do template:
    o botão ◂ MENU em 0,28" — ACIMA do título, em 0,32"."""
    from pptx import Presentation
    from pptx.util import Inches
    p = Presentation()
    p.slide_width, p.slide_height = Inches(13.333), Inches(7.5)
    vazio = p.slide_masters[0].slide_layouts[6]
    for titulo, sub in titulos_e_subtitulos:
        s = p.slides.add_slide(vazio)
        bt = s.shapes.add_textbox(Inches(12.05), Inches(0.28),
                                  Inches(1.05), Inches(0.34))
        bt.text_frame.paragraphs[0].add_run().text = "◂ MENU"
        t = s.shapes.add_textbox(Inches(0.55), Inches(0.32),
                                 Inches(11.3), Inches(0.6))
        t.text_frame.paragraphs[0].add_run().text = titulo
        u = s.shapes.add_textbox(Inches(0.55), Inches(0.92),
                                 Inches(11.3), Inches(0.32))
        u.text_frame.paragraphs[0].add_run().text = sub
    return p


class TestLeituraContinua(unittest.TestCase):
    """O contorno de tudo que fazia a leitura sair espacada."""

    def _cap(self, intervalo, **kw):
        cfg = rm.configparser.ConfigParser(); cfg.add_section("survey")
        for k, v in kw.items(): cfg.set("survey", k, str(v))
        return rm.CapturaGPS("t", ["10.0.0.1"], minutos=1,
                             intervalo_s=intervalo, coletor_ref=None, cfg=cfg)

    def test_intervalo_zero_e_continuo_sem_pausa(self):
        c = self._cap(0)
        self.assertTrue(c.continuo)
        self.assertEqual(c.piso_cont, 0.0)
        # Sobram so os 50 ms de guarda contra laco vazio.
        self.assertLessEqual(c.intervalo, 0.05)

    def test_intervalo_normal_respeita_o_piso(self):
        c = self._cap(20)
        self.assertFalse(c.continuo)
        self.assertEqual(c.intervalo, 20)

    def test_a_pagina_deixa_pedir_continuo(self):
        # O backend tinha o modo continuo e o campo da pagina era min="5":
        # dava para configurar e NAO dava para usar. O rastro continuava
        # espacado e nada no codigo acusava.
        i = rm.PAGINA_HTML.index('id="intervalo"') if hasattr(rm, "PAGINA_HTML") else -1
        fonte = rm.criar_handler.__doc__ or ""
        alvo = None
        for nome in dir(rm):
            v = getattr(rm, nome)
            if isinstance(v, str) and 'id="intervalo"' in v:
                alvo = v; break
        if alvo is None:
            alvo = inspect.getsource(rm)
        m = re.search(r'id="intervalo"[^>]*min="(\d+)"', alvo)
        self.assertIsNotNone(m, "campo de intervalo nao encontrado")
        self.assertEqual(m.group(1), "0",
                         "a pagina nao deixa pedir intervalo 0 (continuo)")

    def test_ping_nao_segura_o_ciclo(self):
        # O ping do Windows custa ~3 s (`ping -n 4` espera ~1 s entre
        # envios e nao aceita intervalo). Preso ao ciclo, impunha esse
        # piso tambem a POSICAO, que e o que desenha o rastro.
        c = self._cap(0, ping_a_cada_s=15)
        c._eleger_pings()
        self.assertTrue(c._toca_pingar("10.0.0.1"), "o primeiro ping tem de sair")
        c._eleger_pings()          # ciclo seguinte, logo em seguida
        self.assertFalse(c._toca_pingar("10.0.0.1"),
                         "pingou dois ciclos seguidos: voltou a segurar o ciclo")

    def test_ping_a_cada_zero_volta_ao_comportamento_antigo(self):
        c = self._cap(0, ping_a_cada_s=0)
        for _ in range(2):
            c._eleger_pings()
            self.assertTrue(c._toca_pingar("10.0.0.1"))

    def test_ping_e_por_radio(self):
        # A cadencia e por radio: um nao pode consumir a vez do outro.
        c = self._cap(0, ping_a_cada_s=15)
        c.ips = ["10.0.0.1", "10.0.0.2"]
        c._eleger_pings()
        self.assertTrue(c._toca_pingar("10.0.0.1"))
        self.assertTrue(c._toca_pingar("10.0.0.2"))

    def test_ciclo_sem_ping_nao_repete_a_leitura_anterior(self):
        # rtt/perda sem ping tem de sair None. Repetir a ultima leitura
        # numa posicao nova inventaria medicao onde nao houve.
        fonte = inspect.getsource(rm.CapturaGPS._amostra)
        i = fonte.index("rtt = perda = None")
        j = fonte.index("return {", i)
        self.assertNotIn("self._ultimo_rtt", fonte[i:j])
        self.assertIn("_toca_pingar", fonte[i:j])

    def test_ping_tem_orcamento_por_ciclo(self):
        # A cadencia por radio nao basta: medido em campo com 159 radios o
        # ciclo dava 46 s, e como 46 s > ping_a_cada_s TODO radio vivia
        # vencido — o ping voltava a ser de todos, todo ciclo.
        c = self._cap(0, ping_a_cada_s=15)
        c.ips = [f"10.0.0.{i}" for i in range(1, 160)]
        c._eleger_pings()
        self.assertLessEqual(len(c._pingar_agora), c.ping_max)
        self.assertGreater(len(c._pingar_agora), 0)

    def test_orcamento_roda_por_toda_a_frota(self):
        # Teto sem rodizio deixaria os mesmos radios sempre sem ping.
        c = self._cap(0, ping_a_cada_s=0.0001)
        c.ips = [f"10.0.0.{i}" for i in range(1, 60)]
        vistos = set()
        for _ in range(10):
            c._eleger_pings(); vistos |= c._pingar_agora
        self.assertEqual(len(vistos), len(c.ips),
                         "o rodizio nao cobriu a frota")

    def test_orcamento_escolhe_os_mais_atrasados(self):
        # Quando TODOS estao vencidos — o caso de campo, com o ciclo maior
        # que ping_a_cada_s — o teto sozinho pegaria sempre os mesmos
        # primeiros da lista e os do fim nunca seriam pingados. Quem
        # garante a vez de cada um e a ordenacao por atraso.
        c = self._cap(0, ping_a_cada_s=1)
        c.ips = [f"10.0.0.{i}" for i in range(1, 31)]
        agora = time.time()
        # os 5 ultimos da lista sao os mais atrasados
        for i, ip in enumerate(c.ips):
            c._ultimo_ping[ip] = agora - 100 - (i * 10)
        c.ping_max = 5
        c._eleger_pings()
        self.assertEqual(c._pingar_agora, set(c.ips[-5:]),
                         "nao escolheu os mais atrasados")

    def test_orcamento_padrao_segue_o_teto_de_threads(self):
        c = self._cap(0)
        self.assertEqual(c.ping_max, c.max_thr)

    def test_orcamento_invalido_no_config_nao_derruba(self):
        c = self._cap(0, ping_max_por_ciclo="abc")
        self.assertEqual(c.ping_max, c.max_thr)

    def test_perfil_do_ciclo_e_publicado(self):
        # "Esta espacado" sem numero e chute.
        fonte = inspect.getsource(rm.CapturaGPS)
        self.assertIn('"ping_s"', fonte)
        self.assertIn('"consulta_s"', fonte)
        self.assertIn('"perfil": self.perfil', fonte)


class TestMeshMapper(unittest.TestCase):
    """Leitura da captura do MeshMapper (a ferramenta da propria Rajant).

    A fixture e um recorte do arquivo REAL do cliente: 8 pontos, 3 peers
    por radio. Inventar o formato levaria a um leitor que so funciona
    contra a minha imaginacao.
    """
    EXEMPLO = Path(__file__).resolve().parent / "exemplos" / "meshmapper_exemplo.json"

    def setUp(self):
        if not self.EXEMPLO.exists():
            self.skipTest("fixture do MeshMapper ausente")
        self.sv, self.am, self.pr = rm.ler_meshmapper(str(self.EXEMPLO))

    def test_le_pontos_e_vizinhos(self):
        self.assertEqual(len(self.am), 8)
        self.assertTrue(self.pr)
        self.assertEqual(self.sv["movel"], "CA-1006")

    def test_signal_e_rssi_nao_sao_trocados(self):
        # No arquivo, "rssi" e SNR em dB e "signal" e RSSI em dBm. Trocar
        # os dois inverteria a escala inteira do relatorio.
        a = self.am[0]
        self.assertLess(a["sinal"], -30)      # dBm e negativo e grande
        self.assertGreater(a["snr"], 0)       # dB e positivo e pequeno
        self.assertLess(a["snr"], 60)

    def test_ruido_e_recuperado_de_signal_menos_snr(self):
        # Nao e estimativa: e o piso que o radio usou para calcular o SNR.
        for a in self.am:
            if a["sinal"] is None or a["snr"] is None: continue
            self.assertEqual(a["ruido"], a["sinal"] - a["snr"])

    def test_ponto_sem_enlace_nao_vira_sinal_zero(self):
        # Type "N/A" com custo INT_MAX significa SEM ROTA. Virar 0 dBm
        # seria publicar "medi e deu otimo" onde nao houve enlace.
        falso = {"signal": 0, "rssi": 0, "cost": rm.CUSTO_SEM_ROTA,
                 "rate": 0, "channel": 0, "freq": 0}
        a = rm._mm_amostra("X", 1.0, -18.9, -43.4, None, falso, None)
        self.assertIsNone(a["sinal"])
        self.assertIsNone(a["custo"])
        self.assertIsNone(a["ruido"])

    def test_banda_sai_da_frequencia(self):
        bandas = {a["banda"] for a in self.am if a["banda"]}
        self.assertTrue(bandas <= {"2.4 GHz", "5.8 GHz"}, bandas)

    def test_timestamp_e_utc(self):
        # O sufixo "UTC" do CSV e literal; tratar como hora local moveria
        # o trajeto no tempo.
        t = rm._mm_ts("2026-09-10 15:59:27 UTC")
        self.assertAlmostEqual(t, 1789055967.0, delta=1)

    def test_limiares_do_meshmapper_sao_preservados(self):
        # A regua que a propria ferramenta usou entra no relatorio, em vez
        # de impormos a nossa e chamarmos de "o que o MeshMapper mostrou".
        self.assertIn("goodRSSI", self.sv["limiares_mm"])

    def test_campos_sem_medicao_sao_identificados(self):
        campos = rm.campos_com_medicao(self.am)
        self.assertIn("sinal", campos)
        self.assertIn("snr", campos)
        # O MeshMapper nao fornece nenhum destes.
        for c in ("rtt", "perda", "interf"):
            self.assertNotIn(c, campos)

    def test_grandeza_sem_medicao_nao_vira_slide(self):
        fonte = inspect.getsource(rm.ppt_survey_anglo)
        self.assertIn("if not any(a.get(campo) is not None for a in am_b):",
                      fonte)

    def test_importa_para_o_banco_e_gera_kmz(self):
        import tempfile, zipfile, io as _io
        tmp = tempfile.mkdtemp()
        con = rm.banco(str(Path(tmp) / "s.db"))
        try:
            sid, sv, am, pr = rm.importar_meshmapper(str(self.EXEMPLO), con)
            self.assertGreater(sid, 0)
            gravadas = rm.survey_amostras(con, sid)
            self.assertEqual(len(gravadas), len(am))
            cfg = rm.configparser.ConfigParser()
            with mock.patch.object(rm, "CONFIG_FILE",
                                   str(Path(tmp) / "c.ini")):
                rm.cfg_relatorio(cfg)
            dados, nome = rm.gerar_kml_survey(
                sv, am, cfg=cfg, campos=rm.campos_com_medicao(am))
            z = zipfile.ZipFile(_io.BytesIO(dados))
            self.assertIn("doc.kml", z.namelist())
            self.assertTrue([n for n in z.namelist() if n.endswith(".png")],
                            "KMZ do MeshMapper saiu sem o raster do calor")
        finally:
            con.close()

    def test_le_sem_a_rajant_api(self):
        # O gerador roda no notebook de quem mediu, sem a biblioteca e
        # sem rede. Sair com erro no import impediria justamente isso.
        fonte = inspect.getsource(rm)
        i = fonte.index("from rajant_api import Breadcrumb")
        j = fonte.index("def exigir_rajant_api")
        self.assertNotIn("sys.exit(1)", fonte[i:j])


class TestCoberturaDisponivel(unittest.TestCase):
    """Duas perguntas diferentes que viviam no mesmo numero.

    "existe sinal servivel aqui?"  -> melhor vizinho de INFRAESTRUTURA
    "a aplicacao funcionou aqui?"  -> enlace que o InstaMesh usou

    No arquivo do cliente elas divergiam muito: enlace entregue com
    mediana -88 dBm e 81% fora do requisito, contra cobertura de -66 dBm
    e 100% dentro. Reportar so a primeira leva a conclusao errada de que
    falta radio na area.
    """

    def _dados(self):
        am = [{"radio": "CA-1", "ts": 1.0, "lat": -18.92, "lon": -43.42,
               "sinal": -88.0, "snr": 20.0}]
        pr = [{"ponto": 1, "nome": "ERB-07", "sinal": -66.0, "snr": 30.0},
              {"ponto": 1, "nome": "CA-1021", "sinal": -40.0, "snr": 50.0},
              {"ponto": 1, "nome": "ERM-03", "sinal": -72.0, "snr": 25.0}]
        return am, pr

    def test_cobertura_usa_infra_e_nao_o_vizinho_mais_forte(self):
        # O mais forte (-40 dBm) e outro CAMINHAO: some quando ele sai, e
        # por isso nao caracteriza cobertura da area.
        am, pr = self._dados()
        rm.cobertura_disponivel(am, pr)
        self.assertEqual(am[0]["sinal_cob"], -66.0)
        self.assertEqual(am[0]["servidor_cob"], "ERB-07")
        self.assertTrue(am[0]["cob_e_infra"])

    def test_delta_mostra_o_que_ficou_na_mesa(self):
        am, pr = self._dados()
        rm.cobertura_disponivel(am, pr)
        self.assertAlmostEqual(am[0]["delta_cob"], 22.0, places=1)

    def test_sem_infra_cai_para_o_melhor_e_MARCA(self):
        # Sem ERB/ERM visivel, usa o melhor vizinho qualquer — mas
        # sinalizado. Silenciar isso apresentaria um caminhao de
        # passagem como cobertura da area.
        am = [{"radio": "CA-1", "ts": 1.0, "lat": -18.92, "lon": -43.42,
               "sinal": -88.0}]
        pr = [{"ponto": 1, "nome": "CA-1021", "sinal": -40.0, "snr": 50.0}]
        r = rm.cobertura_disponivel(am, pr)
        self.assertEqual(am[0]["sinal_cob"], -40.0)
        self.assertFalse(am[0]["cob_e_infra"])
        self.assertEqual(r["sem_infra"], 1)
        self.assertEqual(r["com_infra"], 0)

    def test_ponto_sem_vizinho_nao_inventa_cobertura(self):
        am = [{"radio": "CA-1", "ts": 1.0, "lat": -18.92, "lon": -43.42,
               "sinal": -88.0}]
        rm.cobertura_disponivel(am, [])
        self.assertIsNone(am[0].get("sinal_cob"))
        self.assertIsNone(am[0].get("delta_cob"))

    def test_cobertura_entra_no_resumo(self):
        am, pr = self._dados()
        rm.cobertura_disponivel(am, pr)
        r = rm.survey_resumo(am)
        self.assertIsNotNone(r.get("sinal_cob"))
        self.assertEqual(r["sinal_cob"]["pct_ok"], 100.0)   # -66 > -75
        self.assertEqual(r["sinal"]["pct_ok"], 0.0)         # -88 < -75

    def test_cobertura_e_a_primeira_aba_do_kmz(self):
        # Abrir pelo enlace entregue faz o leitor concluir "falta radio"
        # onde o problema e outro.
        self.assertEqual(rm.CAMPOS_KMZ[0], "sinal_cob")

    def test_cobertura_tem_escala_e_faixas_proprias_registradas(self):
        self.assertIn("sinal_cob", rm.ESCALAS)
        self.assertIn("sinal_cob", rm.FAIXAS_KML)
        # Mesma regua do RSSI: e a mesma grandeza, de outra fonte.
        self.assertEqual(rm.ESCALAS["sinal_cob"]["req"],
                         rm.ESCALAS["sinal"]["req"])
        self.assertEqual(rm.FAIXAS_KML["sinal_cob"], rm.FAIXAS_KML["sinal"])

    def test_banco_guarda_as_colunas_de_cobertura(self):
        import tempfile
        con = rm.banco(str(Path(tempfile.mkdtemp()) / "s.db"))
        try:
            cols = {r["name"] for r in con.execute("PRAGMA table_info(amostra)")}
            for c in ("sinal_cob", "snr_cob", "servidor_cob", "delta_cob"):
                self.assertIn(c, cols)
        finally:
            con.close()

    def test_varios_arquivos_nao_cruzam_vizinhos(self):
        # Os numeros de ponto se repetem entre capturas: cruza-los ligaria
        # o vizinho de uma ao trajeto de outra.
        fonte = inspect.getsource(rm.importar_meshmapper_varios)
        self.assertIn("cobertura_disponivel(am_i, pr_i)", fonte)


class TestValorDoPixel(unittest.TestCase):
    """Como o raster escolhe o valor de cada pixel.

    A regra e vizinho mais proximo, nao media. Medido contra o arquivo do
    cliente: as amostras cruas davam 81% fora do requisito, o vizinho
    mais proximo 92% (area, nao tempo — sao grandezas diferentes) e a
    media ponderada dizia 100%, porque APAGAVA as poucas leituras boas ao
    promedia-las com as vizinhas ruins.
    """

    def _bb(self, pts, raio):
        import math
        las = [p["lat"] for p in pts]; los = [p["lon"] for p in pts]
        dlat = raio / 111320.0
        dlon = raio / (111320.0 * math.cos(math.radians(sum(las)/len(las))))
        return {"sul": min(las)-dlat, "norte": max(las)+dlat,
                "oeste": min(los)-dlon, "leste": max(los)+dlon}

    def _linha(self, valores, passo=0.0002):
        return [{"lat": -18.92 + i*passo, "lon": -43.42, "ts": float(i),
                 "sinal": v, "radio": "CA-1"} for i, v in enumerate(valores)]

    def test_todo_pixel_e_uma_leitura_real(self):
        # O que separa mapa de laudo: nenhum pixel pode mostrar um valor
        # que nao foi medido em lugar nenhum.
        import numpy as np
        pts = self._linha([-50.0, -90.0, -55.0, -85.0, -60.0])
        v, a = rm._calor_da_rota(pts, "sinal", self._bb(pts, 40.0), 40.0,
                                 n=200, esc=rm.ESCALAS["sinal"])
        self.assertIsNotNone(v)
        no_mapa = set(np.round(v[np.isfinite(v)], 3))
        medidos = {round(float(p["sinal"]), 3) for p in pts}
        self.assertEqual(no_mapa - medidos, set(),
                         "o raster inventou valor que nao foi medido")

    def test_leitura_boa_isolada_nao_e_apagada(self):
        # Era o defeito da media: uma leitura otima cercada de ruins
        # sumia. E justamente o ponto que interessa achar num survey.
        import numpy as np
        pts = self._linha([-90.0, -90.0, -45.0, -90.0, -90.0])
        v, a = rm._calor_da_rota(pts, "sinal", self._bb(pts, 40.0), 40.0,
                                 n=200, esc=rm.ESCALAS["sinal"])
        self.assertGreater(np.nanmax(v), -50.0,
                           "a leitura boa foi apagada pela vizinhanca")

    def test_mesmo_lugar_medido_duas_vezes_fica_a_pior(self):
        # Passar duas vezes no mesmo ponto: a operacao enfrenta as duas
        # leituras, e e a ruim que para o caminhao.
        import numpy as np
        pts = [{"lat": -18.92, "lon": -43.42, "ts": 1.0, "sinal": -50.0,
                "radio": "CA-1"},
               {"lat": -18.92, "lon": -43.42, "ts": 99.0, "sinal": -92.0,
                "radio": "CA-1"},
               {"lat": -18.9203, "lon": -43.42, "ts": 2.0, "sinal": -70.0,
                "radio": "CA-1"}]
        v, a = rm._calor_da_rota(pts, "sinal", self._bb(pts, 40.0), 40.0,
                                 n=200, esc=rm.ESCALAS["sinal"])
        # No pixel do ponto repetido tem de estar a leitura RUIM.
        i = np.unravel_index(np.nanargmin(v), v.shape)
        self.assertAlmostEqual(float(v[i]), -92.0, places=1)

    def test_pior_respeita_o_sentido_da_grandeza(self):
        # Em ruido, latencia, perda e interferencia o pior e o MAIOR.
        import numpy as np
        pts = [{"lat": -18.92, "lon": -43.42, "ts": 1.0, "rtt": 10.0,
                "radio": "CA-1"},
               {"lat": -18.92, "lon": -43.42, "ts": 99.0, "rtt": 180.0,
                "radio": "CA-1"},
               {"lat": -18.9203, "lon": -43.42, "ts": 2.0, "rtt": 30.0,
                "radio": "CA-1"}]
        v, a = rm._calor_da_rota(pts, "rtt", self._bb(pts, 40.0), 40.0,
                                 n=200, esc=rm.ESCALAS["rtt"])
        self.assertAlmostEqual(float(np.nanmax(v)), 180.0, places=1)

    def test_empate_e_na_resolucao_da_grade(self):
        # Com tolerancia grande, o empate disparava entre amostras
        # CONSECUTIVAS e o mapa inteiro pendia para o lado ruim: 95,8% da
        # area fora do requisito contra 91,9% pelo vizinho puro. Isso nao
        # e ser conservador, e distorcer.
        fonte = inspect.getsource(rm._calor_da_rota)
        self.assertIn("tol = max(px, py)", fonte)

    def test_nao_usa_media_ponderada(self):
        fonte = inspect.getsource(rm._calor_da_rota)
        self.assertNotIn("soma[i0:i1", fonte)
        self.assertIn("dmin", fonte)


class TestGeradorMeshMapper(unittest.TestCase):
    """A ferramenta separada: varios arquivos, juntos ou separados."""

    EXEMPLO = Path(__file__).resolve().parent / "exemplos" / "meshmapper_exemplo.json"

    def setUp(self):
        if not self.EXEMPLO.exists():
            self.skipTest("fixture do MeshMapper ausente")
        import importlib
        self.sm = importlib.import_module("survey_meshmapper")
        self.tmp = tempfile.mkdtemp()

    def test_junta_varios_num_relatorio_so(self):
        con = rm.banco(str(Path(self.tmp) / "s.db"))
        try:
            sid, sv, am, pr, org = rm.importar_meshmapper_varios(
                [str(self.EXEMPLO), str(self.EXEMPLO)], con)
            self.assertEqual(len(org), 2)
            self.assertEqual(len(am), 16)      # 8 + 8
            self.assertTrue(all(o["ok"] for o in org))
        finally:
            con.close()

    def test_arquivo_ruim_nao_derruba_os_outros(self):
        # Relatorio que engole arquivo ilegivel em silencio e pior que
        # relatorio que falta: a falha entra na lista de origens.
        con = rm.banco(str(Path(self.tmp) / "s.db"))
        try:
            sid, sv, am, pr, org = rm.importar_meshmapper_varios(
                [str(self.EXEMPLO), str(Path(self.tmp) / "nao_existe.kmz")], con)
            self.assertEqual(len(am), 8)
            ruins = [o for o in org if not o["ok"]]
            self.assertEqual(len(ruins), 1)
            self.assertIn("nao encontrado", ruins[0]["erro"])
        finally:
            con.close()

    def test_todos_ruins_falha_com_motivo(self):
        con = rm.banco(str(Path(self.tmp) / "s.db"))
        try:
            with self.assertRaises(RuntimeError):
                rm.importar_meshmapper_varios(
                    [str(Path(self.tmp) / "x.kmz")], con)
        finally:
            con.close()

    def test_amostra_guarda_de_qual_arquivo_veio(self):
        con = rm.banco(str(Path(self.tmp) / "s.db"))
        try:
            _, _, am, _, _ = rm.importar_meshmapper_varios(
                [str(self.EXEMPLO)], con)
            self.assertTrue(all(a.get("arquivo") for a in am))
        finally:
            con.close()

    def test_modo_separado_nao_sobrescreve(self):
        # Duas capturas do MESMO veiculo na MESMA hora produzem nomes de
        # saida iguais. Sem subpasta, uma sobrescrevia a outra em
        # silencio: a contagem dizia 6 relatorios e o disco tinha 3.
        import shutil
        a = Path(self.tmp) / "cap_a.json"
        b = Path(self.tmp) / "cap_b.json"
        shutil.copy(self.EXEMPLO, a); shutil.copy(self.EXEMPLO, b)
        saida = Path(self.tmp) / "out"
        feitos = self.sm.gerar([str(a), str(b)], str(saida), juntar=False,
                               fazer_ppt=False, fazer_excel=False,
                               aviso=lambda t: None)
        self.assertEqual(len(feitos), len({str(f) for f in feitos}),
                         "dois arquivos gravaram no mesmo caminho")
        self.assertTrue(all(f.exists() for f in feitos))

    def test_sem_arquivo_reclama_em_vez_de_estourar(self):
        with self.assertRaises(RuntimeError):
            self.sm.gerar([], self.tmp, aviso=lambda t: None)

    def test_sem_argumento_abre_a_janela(self):
        # O duplo clique no .exe caia no erro de uso do argparse e a
        # janela do console fechava na hora.
        fonte = inspect.getsource(self.sm.main)
        self.assertIn("if not a.arquivos:", fonte)
        self.assertIn("interface()", fonte)

    def test_gera_com_o_tkinter_bloqueado(self):
        # A geracao tem de rodar SEM interface: e assim na linha de
        # comando e num servidor sem tcl/tk.
        #
        # Em SUBPROCESSO de proposito. A primeira versao bloqueava o
        # tkinter no sys.modules deste processo, e isso quebrava QUATRO
        # testes de matplotlib depois — ele resolve o backend olhando o
        # sys.modules e guardava o estado corrompido para o resto da
        # sessao. O sintoma (ValueError em gridspec) nao lembrava nem de
        # longe a causa.
        import subprocess, textwrap
        saida = Path(self.tmp) / "sem_tk"
        codigo = textwrap.dedent(f"""
            import sys
            sys.modules["tkinter"] = None
            sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})
            import survey_meshmapper as sm
            f = sm.gerar([{str(self.EXEMPLO)!r}], {str(saida)!r},
                         fazer_ppt=False, fazer_excel=False,
                         aviso=lambda t: None)
            assert f, "nao gerou nada"
            assert all(x.exists() for x in f), "arquivo prometido nao existe"
            print("OK", len(f))
        """)
        r = subprocess.run([sys.executable, "-c", codigo],
                           capture_output=True, text=True, timeout=180)
        self.assertEqual(r.returncode, 0,
                         f"gerar falhou sem tkinter:\n{r.stdout}\n{r.stderr}")
        self.assertIn("OK", r.stdout)

    def test_interface_avisa_quando_falta_tkinter(self):
        # Sem tcl/tk, a janela nao abre — mas o programa tem de dizer o
        # que fazer, nao morrer com stack trace.
        with mock.patch.dict(sys.modules, {"tkinter": None}):
            self.assertEqual(self.sm.interface(), 1)

    def test_spec_do_exe_nao_exclui_tkinter(self):
        # Herdar o exclude do spec do rajant_monitor geraria um .exe que
        # abre e fecha na hora, com o erro so no console que ninguem ve.
        spec = Path(__file__).resolve().parent / "build" / "survey_meshmapper.spec"
        if not spec.exists():
            self.skipTest("spec ausente")
        txt = spec.read_text(encoding="utf-8")
        i = txt.index("excludes=[")
        j = txt.index("]", i)
        self.assertNotIn("tkinter", txt[i:j])


class TestTituloDoSlide(unittest.TestCase):
    def test_botao_de_menu_nao_vira_titulo(self):
        # O ◂ MENU fica em 0,28" e o título em 0,32": pegar "o texto mais
        # alto" sem filtrar botão devolveria o botão, e aí TODO slide se
        # chamaria "◂ MENU".
        p = _deck_de_secoes([("1. Dashboard Executivo", "resumo")])
        self.assertEqual(rm._titulo_do_slide(list(p.slides)[0]),
                         "1. Dashboard Executivo")

    def test_slide_sem_texto_nao_estoura(self):
        from pptx import Presentation
        p = Presentation()
        s = p.slides.add_slide(p.slide_masters[0].slide_layouts[6])
        self.assertEqual(rm._titulo_do_slide(s), "")


class TestRemocaoDaSecaoDeSurvey(unittest.TestCase):
    """O semanal remove a seção de survey que vem no template. O que se
    testa aqui é que ele remove SÓ ela."""

    SECOES = [
        ("Navegação", "Clique numa seção para ir ao slide"),
        ("1. Dashboard Executivo", "Resumo da saúde da infraestrutura"),
        ("4. Saúde da Mesh Rajant — Links Críticos (SNR)",
         "10 piores enlaces do período — candidatos a realinhamento / "
         "site survey  |  gerado automaticamente"),
        ("4. Saúde da Mesh Rajant — Espectro por Canal",
         "Ocupação e ruído agregados por canal — todos os rádios ativos"),
        ("5. Site Survey — Intensidade de Sinal (RSSI) — 2.4 GHz", "medições"),
        ("5. Site Survey — Análise, Recomendações e Conclusão", "conclusão"),
        ("6. Manutenções", "preventivas e corretivas do período"),
    ]

    def _restantes(self):
        p = _deck_de_secoes(self.SECOES)
        n = rm.remover_slides_survey(p)
        return n, [rm._titulo_do_slide(s) for s in p.slides]

    def test_tira_os_slides_de_survey(self):
        n, restantes = self._restantes()
        self.assertEqual(n, 2)
        self.assertFalse([t for t in restantes if t.startswith("5. Site Survey")])

    def test_prosa_sobre_survey_no_subtitulo_nao_apaga_o_slide(self):
        # A REGRESSÃO: o filtro casava "site survey" em QUALQUER texto do
        # slide. O subtítulo de Links Críticos diz "candidatos a
        # realinhamento / site survey" e o slide sumia do semanal, que
        # ficava com um único slide de gráficos.
        _, restantes = self._restantes()
        self.assertIn("4. Saúde da Mesh Rajant — Links Críticos (SNR)",
                      restantes)

    def test_espectro_por_canal_fica_no_semanal(self):
        # A outra metade: o slide vinha titulado "5. Site Survey —
        # Espectro por Canal" e ia junto, mas os números são dos
        # contadores do exporter.
        _, restantes = self._restantes()
        self.assertIn("4. Saúde da Mesh Rajant — Espectro por Canal",
                      restantes)

    def test_navegacao_sobrevive(self):
        _, restantes = self._restantes()
        self.assertIn("Navegação", restantes)

    def test_o_resto_do_relatorio_fica_de_pe(self):
        _, restantes = self._restantes()
        for t in ("1. Dashboard Executivo", "6. Manutenções"):
            self.assertIn(t, restantes)

    def test_deck_sem_survey_nao_perde_nada(self):
        p = _deck_de_secoes([("1. Dashboard Executivo", "resumo"),
                             ("6. Manutenções", "preventivas")])
        self.assertEqual(rm.remover_slides_survey(p), 0)
        self.assertEqual(len(list(p.slides)), 2)


class TestSlidesTecnicosNoSemanal(unittest.TestCase):
    def test_espectro_pertence_a_secao_4(self):
        # Os números vêm dos contadores do EXPORTER — todos os rádios
        # ativos, o período inteiro —, não de uma captura de survey.
        fonte = inspect.getsource(rm._slides_tecnicos)
        self.assertIn('"4. Saúde da Mesh Rajant — Espectro por Canal"', fonte)
        self.assertNotIn('"5. Site Survey — Espectro por Canal"', fonte)

    def test_espectro_ancora_em_slide_que_existe_no_semanal(self):
        # Ancorado só em slides de survey, ele ficava solto no fim do
        # deck quando a seção 5 não existia.
        fonte = inspect.getsource(rm._slides_tecnicos)
        trecho = fonte[fonte.index("Espectro por Canal"):]
        chamada = trecho[trecho.index("mover_apos_titulo("):]
        chamada = chamada[:chamada.index(")")]
        self.assertTrue("Ethernet e Quedas" in chamada
                        or "Links Críticos" in chamada,
                        f"ancora so em slide de survey: {chamada}")


class TestZeroDeVizinhoNaoEMedicao(unittest.TestCase):
    """O MeshMapper usa 0 para "conheco este vizinho mas ainda nao medi".

    A limpeza existia para o enlace servidor e NAO para a lista de
    vizinhos. Medido no arquivo real do CA-1006: o ERB-11 L2 aparecia com
    0 dBm em tres leituras, ganhava a eleicao de `cobertura_disponivel`
    (que escolhe por `max(sinal)`) e pintava tres pontos do trajeto de
    verde maximo, com "87 dB disponiveis e nao usados" no laudo.
    """

    def _json(self, sinal_viz, snr_viz=0):
        return {"bcc_version": "11.29.1",
                "configuration": {"interval": 1,
                                  "crumbMeta": {"name": "CA-1", "serialStr": "S1"}},
                "points": [{"unixTimeStamp": 1789148756000,
                            "gpsLat": -18.92, "gpsLong": -43.42, "gpsAlt": 800.0,
                            "numActivePeers": 2,
                            "traceInfo": {"path": {"name": "ERB-01", "signal": -88,
                                                   "rssi": 20, "cost": 5000,
                                                   "frequency": 5785, "channel": 157}},
                            "wlanPeers": {"wlan0": [
                                {"name": "ERB-11 L2", "signal": sinal_viz,
                                 "rssi": snr_viz, "cost": 4000,
                                 "frequency": 5785, "channel": 157,
                                 "ipaddr": "10.0.0.1", "mac": "aa:bb",
                                 "serialNumber": "S9"}]}}]}

    def test_vizinho_com_zero_vira_ausencia_e_nao_sinal_perfeito(self):
        sv, am, pr = rm._mm_do_json(self._json(0), "t")
        self.assertIsNone(pr[0]["sinal"])
        self.assertIsNone(pr[0]["snr"])
        self.assertIsNone(pr[0]["ruido"])

    def test_zero_nao_ganha_a_eleicao_da_cobertura(self):
        sv, am, pr = rm._mm_do_json(self._json(0), "t")
        rm.cobertura_disponivel(am, pr)
        # Sem vizinho medido, nao ha cobertura a declarar — e nunca 0 dBm.
        self.assertIsNone(am[0].get("sinal_cob"))
        self.assertIsNone(am[0].get("delta_cob"))

    def test_vizinho_medido_de_verdade_continua_passando(self):
        sv, am, pr = rm._mm_do_json(self._json(-66, 30), "t")
        self.assertEqual(pr[0]["sinal"], -66)
        rm.cobertura_disponivel(am, pr)
        self.assertEqual(am[0]["sinal_cob"], -66)
        self.assertAlmostEqual(am[0]["delta_cob"], 22.0, places=1)

    def test_custo_int_max_do_vizinho_tambem_e_ausencia(self):
        d = self._json(-66, 30)
        d["points"][0]["wlanPeers"]["wlan0"][0]["cost"] = rm.CUSTO_SEM_ROTA
        sv, am, pr = rm._mm_do_json(d, "t")
        self.assertIsNone(pr[0]["custo"])

    def test_peer_sabe_quem_o_capturou(self):
        # Sem isso, juntar duas capturas daria a cada radio a vizinhanca
        # da outra: os numeros de ponto se repetem entre arquivos.
        sv, am, pr = rm._mm_do_json(self._json(-66, 30), "t")
        self.assertEqual(pr[0]["movel"], "CA-1")


class TestCapturaParada(unittest.TestCase):
    """Captura feita de um ponto fixo nao e trajeto.

    O MeshMapper ligado numa repetidora produz um arquivo com a MESMA
    estrutura do de um veiculo. A diferenca e o significado: 115 leituras
    empilhadas dentro de 0,5 m sao uma janela de TEMPO num lugar, nao um
    percurso. Rastro de calor disso sai como uma mancha de um pixel
    pintada com a escala de area — parece mapa e nao e.
    """

    def _am(self, n, passo_graus):
        return [{"radio": "R", "ts": 1789148756.0 + i, "sinal": -70.0,
                 "snr": 25.0, "banda": "5.8 GHz",
                 "lat": -18.894 + i * passo_graus, "lon": -43.4309}
                for i in range(n)]

    def test_radio_imovel_e_reconhecido_como_parado(self):
        # ~0,5 m de oscilacao de GPS, que foi o medido na ERM-12 real.
        am = self._am(115, 0.000002)
        self.assertTrue(rm.captura_parada(am))
        self.assertLess(rm.extensao_da_captura(am)["raio_m"], rm.RAIO_PARADO_M)

    def test_veiculo_andando_nao_e_parado(self):
        am = self._am(55, 0.00005)          # ~300 m de trajeto
        self.assertFalse(rm.captura_parada(am))

    def test_raio_e_a_maior_distancia_e_nao_o_desvio(self):
        # Ida e volta tem desvio pequeno e mesmo assim cobriu distancia.
        am = self._am(20, 0.0001) + self._am(20, 0.0001)[::-1]
        self.assertGreater(rm.extensao_da_captura(am)["raio_m"], 50)

    def test_sem_posicao_nao_inventa_extensao(self):
        self.assertIsNone(rm.extensao_da_captura(
            [{"radio": "R", "lat": None, "lon": None}]))
        self.assertFalse(rm.captura_parada([]))

    def test_nada_em_deslocamento_nao_gera_rastro_de_calor(self):
        # A regressao concreta: `moveis or amostras_aba` fazia a captura
        # parada cair de volta no conjunto inteiro e pintar tudo.
        import inspect
        fonte = inspect.getsource(rm.gerar_kml_survey)
        i = fonte.index("if not andou:")
        self.assertIn("return \"\"", fonte[i:i + 700],
                      "captura parada voltou a virar mancha de um pixel")


class TestCensoDeVizinhos(unittest.TestCase):
    """O produto de uma captura parada: quem fala com aquele radio."""

    def _peers(self):
        # CA-9 e forte porem passageiro; ERB-1 e fraco porem permanente.
        p = []
        for ponto in range(1, 11):
            p.append({"movel": "ERM-12", "ponto": ponto, "ts": 100.0 + ponto,
                      "nome": "ERB-1", "sinal": -93.0, "snr": 16.0,
                      "custo": 5000.0, "banda": "5.8 GHz", "canal": 157,
                      "wlan": "wlan0", "ip": "10.0.0.1", "mac": "a", "serie": "S1"})
            if ponto <= 3:
                p.append({"movel": "ERM-12", "ponto": ponto, "ts": 100.0 + ponto,
                          "nome": "CA-9", "sinal": -58.0, "snr": 38.0,
                          "custo": 6000.0, "banda": "2.4 GHz", "canal": 6,
                          "wlan": "wlan2", "ip": "10.0.0.2", "mac": "b", "serie": "S2"})
        return p

    def test_uma_linha_por_vizinho_e_nao_por_leitura(self):
        c = rm.censo_vizinhos(self._peers(), 10)
        self.assertEqual(len(c), 2)
        self.assertEqual({x["nome"] for x in c}, {"ERB-1", "CA-9"})

    def test_presenca_separa_o_permanente_do_passageiro(self):
        c = {x["nome"]: x for x in rm.censo_vizinhos(self._peers(), 10)}
        # A mediana sozinha diria que o CA-9 e o melhor vizinho. A
        # presenca mostra que ele esteve em 30% dos pontos e foi embora.
        self.assertEqual(c["CA-9"]["presenca"], 30.0)
        self.assertEqual(c["ERB-1"]["presenca"], 100.0)

    def test_ordena_do_sinal_mais_forte_para_o_mais_fraco(self):
        c = rm.censo_vizinhos(self._peers(), 10)
        self.assertEqual(c[0]["nome"], "CA-9")

    def test_infra_e_marcada_pelo_padrao(self):
        c = {x["nome"]: x for x in rm.censo_vizinhos(self._peers(), 10)}
        self.assertTrue(c["ERB-1"]["infra"])
        self.assertFalse(c["CA-9"]["infra"])

    def test_vizinho_sem_leitura_valida_nao_recebe_percentual(self):
        # 0% se leria como "medi e reprovou", que e o oposto de "nao medi".
        c = rm.censo_vizinhos([{"movel": "E", "ponto": 1, "nome": "X",
                                "sinal": None, "snr": None, "custo": None}], 1)
        self.assertIsNone(c[0]["sinal"])
        self.assertIsNone(c[0]["pct_ok"])

    def test_sem_leitura_vai_para_o_fim_da_lista(self):
        pr = self._peers() + [{"movel": "ERM-12", "ponto": 1, "nome": "Z",
                               "sinal": None, "snr": None, "custo": None}]
        self.assertEqual(rm.censo_vizinhos(pr, 10)[-1]["nome"], "Z")

    def test_resumo_conta_infra_moveis_e_quem_passa_do_requisito(self):
        r = rm.resumo_do_censo(rm.censo_vizinhos(self._peers(), 10))
        self.assertEqual(r["vizinhos"], 2)
        self.assertEqual(r["infra"], 1)
        self.assertEqual(r["moveis"], 1)
        self.assertEqual(r["acima_req"], 1)      # so o CA-9, a -58 dBm
        self.assertEqual(r["constantes"], 1)     # so o ERB-1, em 100%

    def test_sitio_so_leva_os_peers_da_propria_captura(self):
        # Juntar duas capturas dava a cada radio a vizinhanca da outra.
        am = [{"radio": "ERM-12", "ts": 1.0 + i, "lat": -18.894, "lon": -43.43}
              for i in range(10)]
        pr = self._peers() + [{"movel": "OUTRO", "ponto": 1, "nome": "INTRUSO",
                               "sinal": -50.0, "snr": 40.0, "custo": 1.0}]
        s = rm.sitios_parados(am, pr)
        self.assertEqual(len(s), 1)
        self.assertNotIn("INTRUSO", {c["nome"] for c in s[0]["censo"]})


class TestKmlDePontosFixos(unittest.TestCase):
    """O mapa so desenha o que tem posicao medida.

    Um BreadCrumb NAO reporta onde estao os vizinhos dele — conferido nos
    arquivos reais do CA-1006 e da ERM-12: os campos de um peer sao
    channel, cost, encap, filtered, frequency, ipaddr, mac, name, rssi,
    serialNumber e signal. Desenhar o vizinho num lugar qualquer seria
    inventar geometria que ninguem mediu.
    """

    def _sitio(self, nome, lat, lon, viz):
        censo = [{"nome": n, "infra": True, "sinal": s, "snr": 20.0,
                  "presenca": 100.0, "bandas": ["5.8 GHz"], "canais": [157],
                  "interfaces": ["wlan0"], "ip": None, "mac": None,
                  "serie": None, "leituras": 1, "pontos": 1, "n_sinal": 1,
                  "sinal_p10": s, "sinal_p90": s, "sinal_min": s,
                  "sinal_max": s, "custo": 1.0, "pct_ok": 100.0,
                  "inicio": 1.0, "fim": 2.0} for n, s in viz]
        return {"nome": nome, "lat": lat, "lon": lon, "raio_m": 0.5,
                "pontos": 100, "censo": censo,
                "resumo": rm.resumo_do_censo(censo),
                "inicio": 1789148756.0, "fim": 1789148870.0}

    def _kml(self, sitios):
        import zipfile, io
        dados, nome = rm.gerar_kml_pontos_fixos(sitios)
        return zipfile.ZipFile(io.BytesIO(dados)).read("doc.kml").decode()

    def test_o_kml_e_xml_valido(self):
        # `&harr;` derrubava o documento inteiro: KML e XML, e XML so
        # conhece as cinco entidades predefinidas. Fora de CDATA nao vale.
        from xml.dom.minidom import parseString
        kml = self._kml([self._sitio("A", -18.89, -43.43, [("B", -70.0)]),
                         self._sitio("B", -18.90, -43.44, [("A", -84.0)])])
        parseString(kml)

    def test_vizinho_sem_captura_propria_nao_vira_ponto(self):
        kml = self._kml([self._sitio("A", -18.89, -43.43,
                                     [("SEM-POSICAO", -60.0)])])
        self.assertNotIn("<name>SEM-POSICAO</name>", kml)
        self.assertIn("nao no mapa", kml)      # declarado, nao escondido

    def test_enlace_entre_dois_capturados_e_desenhado(self):
        kml = self._kml([self._sitio("A", -18.89, -43.43, [("B", -70.0)]),
                         self._sitio("B", -18.90, -43.44, [("A", -70.0)])])
        self.assertIn("<LineString>", kml)
        self.assertIn("A ↔ B", kml)

    def test_enlace_assimetrico_fica_com_a_PIOR_das_duas_pontas(self):
        # E a ponta ruim que limita o enlace e decide se falta radio.
        kml = self._kml([self._sitio("A", -18.89, -43.43, [("B", -70.0)]),
                         self._sitio("B", -18.90, -43.44, [("A", -84.0)])])
        self.assertIn("RSSI -84 dBm", kml)
        self.assertNotIn("RSSI -70 dBm", kml)

    def test_um_sitio_so_nao_inventa_enlace(self):
        kml = self._kml([self._sitio("A", -18.89, -43.43, [("B", -70.0)])])
        self.assertNotIn("<LineString>", kml)

    def test_sem_posicao_nenhuma_falha_com_motivo(self):
        with self.assertRaises(RuntimeError):
            rm.gerar_kml_pontos_fixos([])


class TestSaidasDaCapturaParada(unittest.TestCase):
    """Ponta a ponta: o que sai quando o arquivo e de uma repetidora."""

    def _kmz(self, destino, parado=True):
        """Escreve um .kmz do MeshMapper de mentira e devolve o caminho."""
        import json as _json, zipfile
        passo = 0.000002 if parado else 0.00005
        pontos = []
        for i in range(12):
            pontos.append({
                "unixTimeStamp": 1789148756000 + i * 1000,
                "gpsLat": -18.894 + i * passo, "gpsLong": -43.4309,
                "gpsAlt": 890.0, "numActivePeers": 2,
                "traceInfo": {"path": {"name": "ERB-01", "signal": -80,
                                       "rssi": 22, "cost": 5000,
                                       "frequency": 5785, "channel": 157}},
                "wlanPeers": {"wlan0": [
                    {"name": "ERB-07", "signal": -70, "rssi": 30,
                     "cost": 4000, "frequency": 5785, "channel": 157,
                     "ipaddr": "10.0.0.7", "mac": "a", "serialNumber": "S7"},
                    {"name": "CA-1010", "signal": -67, "rssi": 29,
                     "cost": 8000, "frequency": 2437, "channel": 6,
                     "ipaddr": "10.0.0.8", "mac": "b", "serialNumber": "S8"}]}})
        d = {"bcc_version": "11.29.1",
             "configuration": {"interval": 1,
                               "crumbMeta": {"name": "ERM-12", "serialStr": "S0"}},
             "points": pontos}
        alvo = destino / ("parado.kmz" if parado else "andando.kmz")
        with zipfile.ZipFile(alvo, "w") as z:
            z.writestr("data.json", _json.dumps(d))
        return str(alvo)

    def test_repetidora_nunca_gera_rastro_de_calor(self):
        # A regra que nao depende de chave nenhuma: 115 leituras
        # empilhadas em meio metro NAO viram mapa de area.
        import tempfile, shutil, sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent))
        import survey_meshmapper as sm
        tmp = Path(tempfile.mkdtemp())
        try:
            arq = self._kmz(tmp, parado=True)
            feitos = sm.gerar([arq], str(tmp / "out"), aviso=lambda t: None)
            nomes = [f.name for f in feitos]
            self.assertFalse(any(n.startswith("Survey_2") and n.endswith(".kmz")
                                 for n in nomes),
                             f"captura parada gerou rastro de calor: {nomes}")
            # Sem a chave, tambem nao sai o KMZ de vizinhanca: o laudo do
            # dia a dia e o de trajeto.
            self.assertFalse(any(n.startswith("Vizinhanca_") for n in nomes),
                             f"vizinhanca saiu sem a chave: {nomes}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_vizinhanca_sai_quando_a_chave_esta_ligada(self):
        import tempfile, shutil, sys as _sys, configparser
        from unittest import mock
        _sys.path.insert(0, str(Path(__file__).resolve().parent))
        import survey_meshmapper as sm
        tmp = Path(tempfile.mkdtemp())
        try:
            arq = self._kmz(tmp, parado=True)
            cfg = rm.cfg_relatorio(configparser.ConfigParser())
            cfg.set("relatorio", "vizinhanca", "true")
            with mock.patch.object(rm, "carregar_config", lambda *a, **k: cfg):
                feitos = sm.gerar([arq], str(tmp / "out"),
                                  aviso=lambda t: None)
            nomes = [f.name for f in feitos]
            self.assertTrue(any(n.startswith("Vizinhanca_") for n in nomes),
                            f"sem KMZ de vizinhanca: {nomes}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_veiculo_continua_gerando_o_rastro(self):
        import tempfile, shutil, sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent))
        import survey_meshmapper as sm
        tmp = Path(tempfile.mkdtemp())
        try:
            arq = self._kmz(tmp, parado=False)
            feitos = sm.gerar([arq], str(tmp / "out"), aviso=lambda t: None)
            nomes = [f.name for f in feitos]
            self.assertTrue(any(n.endswith(".kmz") and "Vizinhanca" not in n
                                for n in nomes), f"sem rastro: {nomes}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_planilha_declara_que_a_captura_foi_parada(self):
        import tempfile, shutil, io as _io
        from openpyxl import load_workbook
        tmp = Path(tempfile.mkdtemp())
        try:
            import configparser
            sv, am, pr = rm.ler_meshmapper(self._kmz(tmp, parado=True))
            sv["cobertura"] = rm.cobertura_disponivel(am, pr)
            # O tipo de captura fica no Resumo SEMPRE: e o que impede ler
            # um laudo de ponto fixo como se fosse de area.
            dados, _ = rm.excel_do_meshmapper(sv, am, pr)
            wb = load_workbook(_io.BytesIO(dados))
            texto = " ".join(str(c.value) for c in
                             wb["Resumo"]["B"][:20] if c.value)
            self.assertIn("PARADA", texto)
            self.assertNotIn("Censo de Vizinhos", wb.sheetnames)
            cfg = rm.cfg_relatorio(configparser.ConfigParser())
            cfg.set("relatorio", "vizinhanca", "true")
            dados, _ = rm.excel_do_meshmapper(sv, am, pr, cfg)
            self.assertIn("Censo de Vizinhos",
                          load_workbook(_io.BytesIO(dados)).sheetnames)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestCamposDescartados(BaseParser):
    """Campos que o rádio mandava e o parser jogava fora.

    Os três primeiros são o que permite medir sinal na posição certa em
    vez de na posição de antes. Conferidos contra `bcapi-ref/proto/`.
    """

    def test_gpstime_e_lido(self):
        # Gps.proto: GPSPositionReport.gpsTime = 1 (float).
        self.assertEqual(self.s["gps_time"], 143025.0)

    def test_gpstime_vira_segundos_do_dia(self):
        # 14:30:25 = 14*3600 + 30*60 + 25
        self.assertEqual(self.s["gps_time_s"], 52225.0)

    def test_gpsquality_e_lido(self):
        # NMEA GGA: 0 inválido, 1 GPS, 2 DGPS, 4/5 RTK.
        self.assertEqual(self.s["gps_qual"], 1.0)

    def test_sem_gpstime_fica_none_nao_zero(self):
        st = STATE.replace("    gpsTime: 143025.0\n", "")
        d = rm.parse_state(st)["sistema"]
        self.assertIsNone(d["gps_time"])
        self.assertIsNone(d["gps_time_s"])


class TestGpsTimeParaSegundos(unittest.TestCase):
    """`Gps.proto` declara `optional float gpsTime = 1;` e NÃO diz a
    unidade. Por isso a conversão discrimina em vez de supor — e devolve
    None no que não encaixa, em vez de inventar uma hora."""

    def test_hora_nmea_hhmmss(self):
        self.assertEqual(rm.gps_time_para_segundos(174551.25), 63951.25)
        self.assertEqual(rm.gps_time_para_segundos(235959.99), 86399.99)

    def test_epoca_unix_e_reconhecida(self):
        # Improvável num float de 32 bits, mas reconhecida em vez de
        # virar hora absurda.
        self.assertEqual(rm.gps_time_para_segundos(1789148756.0), 63956.0)

    def test_valor_sem_unidade_conhecida_nao_vira_hora(self):
        for v in (999999.0, 126000.0, 246000.0):
            self.assertIsNone(rm.gps_time_para_segundos(v), f"aceitou {v}")

    def test_ausencia_e_zero_nao_viram_medida(self):
        for v in (None, 0, 0.0, "", "abc"):
            self.assertIsNone(rm.gps_time_para_segundos(v), f"aceitou {v!r}")

    def test_comparar_o_bruto_dispensa_a_conversao(self):
        # O uso principal — "a posição mudou?" — compara o valor bruto e
        # funciona em qualquer formato. Se esta invariante cair, a captura
        # passa a depender de adivinhar a unidade.
        a = rm.parse_state(STATE)["sistema"]["gps_time"]
        b = rm.parse_state(STATE.replace("gpsTime: 143025.0",
                                         "gpsTime: 143026.0"))["sistema"]["gps_time"]
        self.assertNotEqual(a, b)


class TestIdentidadeDoVizinho(unittest.TestCase):
    """State.Peer NÃO tem name nem serialNumber — conferido no
    `State.proto`: mac, enabled, cost, rate, rssi, signal, age, stats,
    encapId, ipv4Address. Quem fecha o nome é o `encapId`, que é a parte
    numérica do número de série.

    Verificado nos 40 vizinhos da captura real da ERM-12: 40 de 40.
    """

    ST = """
manufacturer {
  model: "ES1-2450CS"
  serial: 113345
}
wireless {
  name: "wlan0"
  channel: 157
  noise: -109
  peer {
    mac: "aa"
    enabled: true
    cost: 8523
    rate: 650
    rssi: 21
    signal: -88
    age: 3
    encapId: 97128
    ipv4Address: "10.188.97.170"
  }
}
"""

    def test_encapid_do_vizinho_e_lido(self):
        d = rm.parse_state(self.ST)
        self.assertEqual(d["radios"][0]["peers"][0]["encap"], 97128)

    def test_serial_numerico_do_proprio_radio_e_lido(self):
        d = rm.parse_state(self.ST)["sistema"]
        self.assertEqual(d["serial_num"], 113345)
        self.assertEqual(d["modelo_fab"], "ES1-2450CS")

    def test_sem_encapid_fica_none_nao_zero(self):
        st = self.ST.replace("    encapId: 97128\n", "")
        self.assertIsNone(rm.parse_state(st)["radios"][0]["peers"][0]["encap"])

    def test_coleta_filtrada_nao_traz_manufacturer(self):
        # `manufacturer` é o campo 190 do State, fora de gps/wireless/
        # system. Numa coleta filtrada ele não vem — e o certo é None,
        # não 0: serial 0 seria um rádio existente e errado.
        st = self.ST[self.ST.index("wireless {"):]
        d = rm.parse_state(st)["sistema"]
        self.assertIsNone(d["serial_num"])
        self.assertIsNone(d["modelo_fab"])

    def test_encapid_bate_com_o_serial_no_arquivo_real(self):
        # A prova que sustenta a chave. Se um firmware mudar isso, este
        # teste cai antes de o relatório sair com nome trocado.
        exemplo = (Path(__file__).resolve().parent / "exemplos"
                   / "meshmapper_repetidora.json")
        if not exemplo.exists():
            self.skipTest("exemplos/meshmapper_repetidora.json ausente")
        d = json.loads(exemplo.read_text(encoding="utf-8"))
        vistos = {}
        for p in d["points"]:
            for lst in p["wlanPeers"].values():
                for q in lst:
                    vistos[q["serialNumber"]] = q["encap"]
        self.assertGreater(len(vistos), 10)
        for ser, enc in vistos.items():
            self.assertTrue(ser.endswith(str(enc)),
                            f"encap {enc} nao e sufixo de {ser}")


class TestLaudoDeTrajeto(unittest.TestCase):
    """O laudo que se usa todo dia, e só ele.

    RSSI, SNR e ruído por banda, coloridos pela MELHOR ERB/ERM visível em
    cada ponto. Latência, perda e interferência não saem porque o
    MeshMapper não as mede — e página vazia com escala e requisito lê-se
    como "medi e deu tudo fora", que é o oposto.

    As abas de vizinhança respondem outra pergunta; misturadas com estas,
    confundem. Ficaram atrás de [relatorio] vizinhanca, desligada.
    """

    EXEMPLO = Path(__file__).resolve().parent / "exemplos" / "meshmapper_exemplo.json"

    def _gerar(self, destino, cfg=None):
        import sys as _s
        _s.path.insert(0, str(Path(__file__).resolve().parent))
        import survey_meshmapper as sm
        from unittest import mock
        if cfg is None:
            return sm.gerar([str(self.EXEMPLO)], str(destino),
                            aviso=lambda t: None)
        with mock.patch.object(rm, "carregar_config", lambda *a, **k: cfg):
            return sm.gerar([str(self.EXEMPLO)], str(destino),
                            aviso=lambda t: None)

    def test_um_kmz_por_banda(self):
        # 2,4 e 5,8 GHz sao malhas diferentes no mesmo terreno: num
        # arquivo so, a banda boa tapa a ruim e o mapa deixa de dizer qual
        # das duas esta servindo.
        import tempfile, shutil
        _sv, am, _pr = rm.ler_meshmapper(str(self.EXEMPLO))
        bandas = sorted({rm._norm_banda(a.get("banda")) for a in am
                         if a.get("banda")})
        self.assertTrue(bandas, "o exemplo perdeu a banda")
        tmp = Path(tempfile.mkdtemp())
        try:
            nomes = [f.name for f in self._gerar(tmp) if f.suffix == ".kmz"]
            self.assertEqual(len(nomes), len(bandas),
                             f"{len(bandas)} banda(s), {len(nomes)} arquivo(s)")
            for b in bandas:
                suf = b.replace(" ", "").replace(".", "")
                self.assertTrue(any(suf in n for n in nomes),
                                f"{b} sem arquivo proprio: {nomes}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_duas_bandas_dao_dois_arquivos(self):
        # A regra com as duas bandas presentes, que o exemplo de 8 pontos
        # nao tem: sem isso o teste acima passaria com um arquivo so.
        import tempfile, shutil
        am = [{"radio": "CA-1", "ts": 100.0 + i,
               "lat": -18.90 + i * 0.0004, "lon": -43.43,
               "sinal": -70.0, "snr": 25.0,
               "banda": "2.4 GHz" if i % 2 else "5.8 GHz"}
              for i in range(20)]
        sv = {"nome": "t", "inicio": 100.0, "fim": 120.0}
        tmp = Path(tempfile.mkdtemp())
        try:
            import sys as _s
            _s.path.insert(0, str(Path(__file__).resolve().parent))
            import survey_meshmapper as sm
            feitos = sm._kmz_por_banda(sv, am, [], tmp, None, ["sinal"],
                                       lambda t: None)
            nomes = [f.name for f in feitos]
            self.assertEqual(len(nomes), 2, nomes)
            self.assertTrue(any("24GHz" in n for n in nomes), nomes)
            self.assertTrue(any("58GHz" in n for n in nomes), nomes)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_cor_vem_da_melhor_erb_ou_erm_do_ponto(self):
        # `sinal_cob` e a primeira aba de proposito: e a pergunta do
        # laudo — havia sinal servivel ali?
        import tempfile, shutil, zipfile
        tmp = Path(tempfile.mkdtemp())
        try:
            kmz = [f for f in self._gerar(tmp) if f.suffix == ".kmz"][0]
            kml = zipfile.ZipFile(kmz).read("doc.kml").decode()
            self.assertIn("Cobertura disponível", kml)
            i = kml.index("Cobertura disponível")
            for outra in ("RSSI", "SNR"):
                j = kml.find(f"<name>{outra}</name>")
                if j > -1:
                    self.assertLess(i, j, f"{outra} veio antes da cobertura")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_sem_vizinhanca_no_laudo_padrao(self):
        import tempfile, shutil, zipfile
        from openpyxl import load_workbook
        tmp = Path(tempfile.mkdtemp())
        try:
            feitos = self._gerar(tmp)
            nomes = [f.name for f in feitos]
            self.assertFalse(any(n.startswith("Vizinhanca_") for n in nomes),
                             nomes)
            xls = [f for f in feitos if f.suffix == ".xlsx"][0]
            abas = load_workbook(xls).sheetnames
            for proibida in ("Censo de Vizinhos", "Por Repetidora", "Vizinhos"):
                self.assertNotIn(proibida, abas)
            for kmz in (f for f in feitos if f.suffix == ".kmz"):
                kml = zipfile.ZipFile(kmz).read("doc.kml").decode()
                self.assertNotIn("Por repetidora", kml)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_latencia_e_perda_nao_viram_pagina_vazia(self):
        # O MeshMapper nao mede. Pagina com escala e grafico vazio le-se
        # como "medi e deu tudo fora".
        import tempfile, shutil, zipfile
        tmp = Path(tempfile.mkdtemp())
        try:
            for kmz in (f for f in self._gerar(tmp) if f.suffix == ".kmz"):
                kml = zipfile.ZipFile(kmz).read("doc.kml").decode()
                for ausente in ("Latência", "Packet Loss", "Interferência"):
                    self.assertNotIn(f"<name>{ausente}", kml)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_chave_devolve_a_vizinhanca(self):
        import tempfile, shutil, configparser
        from openpyxl import load_workbook
        tmp = Path(tempfile.mkdtemp())
        try:
            cfg = rm.cfg_relatorio(configparser.ConfigParser())
            cfg.set("relatorio", "vizinhanca", "true")
            xls = [f for f in self._gerar(tmp, cfg) if f.suffix == ".xlsx"][0]
            self.assertIn("Censo de Vizinhos", load_workbook(xls).sheetnames)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestPegadaPorRepetidora(unittest.TestCase):
    """A pegada MEDIDA de cada ERB/ERM.

    A camada de cobertura disponível mistura as repetidoras — ela é o
    MELHOR vizinho de cada ponto. Não dá para perguntar "até onde a
    ERM-28 alcança". Esta responde, e sem estimar nada: a posição é a do
    veículo e o sinal é o dele para aquela repetidora, da mesma leitura.
    """

    def _dados(self, n=12):
        am = [{"radio": "CA-1", "ts": 100.0 + i, "lat": -18.90 + i * 0.0005,
               "lon": -43.43, "sinal": -88.0, "banda": "5.8 GHz"}
              for i in range(n)]
        pr = []
        for i in range(1, n + 1):
            pr.append({"movel": "CA-1", "ponto": i, "nome": "ERB-07",
                       "sinal": -70.0 - i, "snr": 30.0, "custo": 5000.0,
                       "banda": "5.8 GHz", "canal": 157})
            pr.append({"movel": "CA-1", "ponto": i, "nome": "CA-9",
                       "sinal": -50.0, "snr": 40.0, "custo": 900.0,
                       "banda": "2.4 GHz", "canal": 6})
        return am, pr

    def test_so_infraestrutura_ganha_pegada(self):
        # Caminhão dá sinal ótimo e vai embora: não caracteriza cobertura.
        d = rm.amostras_por_repetidora(*self._dados())
        self.assertIn("ERB-07", d)
        self.assertNotIn("CA-9", d)

    def test_posicao_e_do_veiculo_e_sinal_e_do_enlace(self):
        am, pr = self._dados()
        d = rm.amostras_por_repetidora(am, pr)
        prim = d["ERB-07"][0]
        self.assertEqual(prim["lat"], am[0]["lat"])
        self.assertEqual(prim["lon"], am[0]["lon"])
        self.assertEqual(prim["sinal"], -71.0)       # o do vizinho
        self.assertEqual(prim["radio"], "CA-1")      # quem mediu

    def test_repetidora_pouco_ouvida_nao_vira_mapa(self):
        # Poucos pontos viram bolha solta, que se lê como "medi esta
        # área" onde o certo é "passei perto uma vez".
        am, pr = self._dados()
        pr = [p for p in pr if p["nome"] != "ERB-07" or p["ponto"] <= 3]
        self.assertNotIn("ERB-07", rm.amostras_por_repetidora(am, pr))
        self.assertIn("ERB-07",
                      rm.amostras_por_repetidora(am, pr, min_pontos=3))

    def test_duas_bandas_no_mesmo_ponto_contam_uma_vez(self):
        # A mesma repetidora em 2.4 e 5.8 GHz é UM equipamento. Fica o
        # melhor do ponto; somar os dois contaria em dobro.
        #
        # Testa NAS DUAS ORDENS de propósito: guardando por ponto num
        # dicionário, o último a chegar vence sozinho. Com o pior vindo
        # depois, um teste de ordem única passa mesmo sem a regra — foi
        # o que aconteceu, e o mutante sobreviveu.
        for pior_primeiro in (True, False):
            am, pr = self._dados()
            extra = {"movel": "CA-1", "ponto": 1, "nome": "ERB-07",
                     "sinal": -60.0, "snr": 35.0, "custo": 4000.0,
                     "banda": "2.4 GHz", "canal": 6}
            if pior_primeiro:
                pr.append(extra)                      # -71 e depois -60
            else:
                # -60 antes do -71 do _dados(): só a regra segura o melhor
                pr.insert(0, extra)
            d = rm.amostras_por_repetidora(am, pr)
            do_ponto1 = [a for a in d["ERB-07"] if a["ts"] == 100.0]
            self.assertEqual(len(do_ponto1), 1,
                             f"contou em dobro (pior_primeiro={pior_primeiro})")
            self.assertEqual(do_ponto1[0]["sinal"], -60.0,
                             f"nao ficou o melhor (pior_primeiro={pior_primeiro})")

    def test_vizinho_sem_amostra_correspondente_e_ignorado(self):
        am, pr = self._dados()
        pr.append({"movel": "CA-1", "ponto": 999, "nome": "ERB-07",
                   "sinal": -40.0, "snr": 45.0, "custo": 10.0})
        d = rm.amostras_por_repetidora(am, pr)
        self.assertNotIn(-40.0, [a["sinal"] for a in d["ERB-07"]])

    def test_kmz_ganha_uma_pasta_por_repetidora(self):
        import zipfile, io as _io
        am, pr = self._dados(20)
        sv = {"nome": "t", "inicio": 100.0, "fim": 120.0}
        dados, _ = rm.gerar_kml_survey(sv, am, campos=["sinal"], peers=pr)
        kml = zipfile.ZipFile(_io.BytesIO(dados)).read("doc.kml").decode()
        self.assertIn("Por repetidora", kml)
        self.assertIn("ERB-07", kml)

    def test_sem_peers_o_kmz_sai_como_antes(self):
        # A sondagem por API devolve só o enlace que atendeu; sem a
        # vizinhança não há o que desenhar, e inventar pasta vazia seria
        # pior que não ter.
        import zipfile, io as _io
        am, _ = self._dados(20)
        sv = {"nome": "t", "inicio": 100.0, "fim": 120.0}
        dados, _n = rm.gerar_kml_survey(sv, am, campos=["sinal"])
        kml = zipfile.ZipFile(_io.BytesIO(dados)).read("doc.kml").decode()
        self.assertNotIn("Por repetidora", kml)

    def test_cada_repetidora_tem_seu_proprio_raster(self):
        # Todos os rasters se chamariam calor_sinal.png e um
        # sobrescreveria o outro dentro do zip: sobraria um mapa só,
        # repetido em todas as pastas.
        import zipfile, io as _io
        am = [{"radio": "CA-1", "ts": 100.0 + i, "lat": -18.90 + i * 0.0005,
               "lon": -43.43, "sinal": -88.0, "banda": "5.8 GHz"}
              for i in range(20)]
        pr = []
        for i in range(1, 21):
            pr.append({"movel": "CA-1", "ponto": i, "nome": "ERB-07",
                       "sinal": -60.0, "snr": 30.0, "banda": "5.8 GHz"})
            pr.append({"movel": "CA-1", "ponto": i, "nome": "ERB-02",
                       "sinal": -80.0, "snr": 18.0, "banda": "5.8 GHz"})
        sv = {"nome": "t", "inicio": 100.0, "fim": 120.0}
        dados, _ = rm.gerar_kml_survey(sv, am, campos=["sinal"], peers=pr)
        z = zipfile.ZipFile(_io.BytesIO(dados))
        pngs = [n for n in z.namelist() if "calor_sinal_" in n]
        self.assertEqual(len(pngs), len(set(pngs)))
        self.assertGreaterEqual(len(pngs), 2, f"rasters colidiram: {pngs}")
        # -60 dBm e -80 dBm caem em cores diferentes da escala: se as
        # imagens saírem iguais, o sufixo voltou a colidir.
        self.assertNotEqual(z.read(pngs[0]), z.read(pngs[1]))

    def test_planilha_ganha_a_aba(self):
        import io as _io
        from openpyxl import load_workbook
        am, pr = self._dados(12)
        sv = {"nome": "t", "movel": "CA-1", "inicio": 100.0, "fim": 112.0}
        # Recurso OPCIONAL desde que o laudo do dia a dia foi enxugado:
        # sem a chave, as abas de vizinhanca nao entram.
        import configparser
        cfg = rm.cfg_relatorio(configparser.ConfigParser())
        cfg.set("relatorio", "vizinhanca", "true")
        dados, _ = rm.excel_do_meshmapper(sv, am, pr, cfg)
        wb = load_workbook(_io.BytesIO(dados))
        self.assertIn("Por Repetidora", wb.sheetnames)
        ws = wb["Por Repetidora"]
        linhas = [r[0] for r in ws.iter_rows(min_row=5, max_col=1,
                                             values_only=True) if r[0]]
        self.assertIn("ERB-07", linhas)
        self.assertNotIn("CA-9", linhas)


def _rajant_falsa(frota, t0, latencia=0.0, gps_hz=10.0):
    """Fábrica de BreadCrumb falso para testar coleta sem rádio.

    `frota`: {ip: (nome, vel_kmh ou None para sem GPS, rumo_graus)}.
    A posição avança com o tempo real, e `gpsTime` só anda a `gps_hz` —
    é assim que se reproduz o piso do módulo, que é o que separa
    "medi aqui" de "medi com a posição de antes".
    """
    import math, time as _t

    def _nmea(v, casas):
        h = ("S" if v < 0 else "N") if casas == 2 else ("W" if v < 0 else "E")
        v = abs(v); d = int(v)
        return f"{d:0{casas}d}{(v - d) * 60:07.4f}{h}"

    def _state(ip):
        nome, vel, rumo = frota[ip]
        if vel is None:
            g = ""
        else:
            dt = _t.time() - t0[0]
            d = (vel / 3.6) * dt
            lat = -18.894 + (d * math.cos(math.radians(rumo))) / 111320.0
            lon = -43.431 + ((d * math.sin(math.radians(rumo)))
                             / (111320.0 * math.cos(math.radians(18.9))))
            gt = 143025.0 + int(dt * gps_hz) / gps_hz
            g = (f'gps {{\n  gpsSwitch {{\n    enabled: true\n  }}\n'
                 f'  gpsPos {{\n    gpsTime: {gt}\n'
                 f'    gpsLat: "{_nmea(lat, 2)}"\n'
                 f'    gpsLong: "{_nmea(lon, 3)}"\n'
                 f'    gpsQuality: 1.0\n    gpsSatsInView: 11\n  }}\n'
                 f'  gpsVel {{\n    gpsSpeedKph: {vel}\n  }}\n}}\n')
        ps = ""
        for oip, (on, _v, _r) in frota.items():
            if oip == ip: continue
            sig = -60 - (abs(hash(ip + oip)) % 35)
            ps += (f'  peer {{\n    ipv4Address: "{oip}"\n'
                   f'    signal: {sig}\n    rssi: {max(5, sig + 100)}\n'
                   f'    cost: 5000\n    rate: 650\n'
                   f'    encapId: {90000 + abs(hash(oip)) % 9999}\n  }}\n')
        return (f'configuration {{\n  saved {{\n    general {{\n'
                f'      name: "{nome}"\n    }}\n  }}\n}}\n'
                f'{g}wireless {{\n  name: "wlan0"\n  channel: 157\n'
                f'  noise: -95\n{ps}}}\n')

    class FakeBC:
        def __init__(self, host, port=None, role=None, password=None):
            self.h = host
        def reachable(self):   return self.h in frota
        def authenticate(self): return True
        def get_state(self, *a, **k):
            if latencia: _t.sleep(latencia)
            return _state(self.h)
    return FakeBC


class TestDescobrirMalha(unittest.TestCase):
    """Descoberta sem o peso do coletor: sem métrica, sem cache, sem
    filtro de tag. O app de coleta precisa ver TUDO que responde para o
    usuário decidir."""

    FROTA = {"10.0.0.1": ("ERB-07", 0.0, 0.0),
             "10.0.0.2": ("CA-1006", 40.0, 0.0),
             "10.0.0.3": ("PCP-002", None, 0.0)}   # sem GPS

    def setUp(self):
        import time as _t
        self._orig = rm.Breadcrumb
        rm.Breadcrumb = _rajant_falsa(self.FROTA, [_t.time()])

    def tearDown(self):
        rm.Breadcrumb = self._orig

    # `incluir_conhecidos=False` nestes: aqui se testa a MECÂNICA de
    # seguir peers. A lista embutida da mina real entraria junto e
    # afogaria a malha de três rádios do teste.
    def test_segue_os_peers_a_partir_do_seed(self):
        r = rm.descobrir_malha(["10.0.0.1"], incluir_conhecidos=False)
        self.assertEqual(set(r), set(self.FROTA))

    def test_marca_quem_nao_tem_gps(self):
        # Quem não sabe onde está não vai para o mapa. Dizer isso na
        # lista evita o usuário marcar e descobrir no fim que não saiu
        # ponto nenhum.
        r = rm.descobrir_malha(["10.0.0.1"], incluir_conhecidos=False)
        self.assertFalse(r["10.0.0.3"]["tem_gps"])
        self.assertTrue(r["10.0.0.2"]["tem_gps"])

    def test_radio_que_nao_responde_entra_com_o_motivo(self):
        r = rm.descobrir_malha(["10.9.9.9"], incluir_conhecidos=False)
        self.assertIn("10.9.9.9", r)
        self.assertTrue(r["10.9.9.9"]["erro"])

    def test_sem_seguir_peers_fica_so_no_seed(self):
        r = rm.descobrir_malha(["10.0.0.1"], seguir_peers=False,
                               incluir_conhecidos=False)
        self.assertEqual(set(r), {"10.0.0.1"})

    def test_limite_impede_varredura_sem_fim(self):
        r = rm.descobrir_malha(["10.0.0.1"], limite=2,
                               incluir_conhecidos=False)
        self.assertLessEqual(len(r), 2)


class TestMalhaConhecida(unittest.TestCase):
    """Partir de um seed só não basta.

    `ipv4Address` é OPCIONAL no State.Peer. Um rádio cujos vizinhos só
    trazem MAC não leva a busca a lugar nenhum — em campo, partindo do
    10.188.96.140, a descoberta achou UM equipamento numa malha de 150.
    Por isso cada rádio conhecido é um ponto de partida próprio.
    """

    # Vizinho SEM ipv4Address: reproduz o caso real.
    ST = ('configuration {\n  saved {\n    general {\n      name: "%s"\n'
          '    }\n  }\n}\nwireless {\n  name: "wlan0"\n  channel: 157\n'
          '  peer {\n    mac: "aa:bb"\n    signal: -70\n    rssi: 25\n  }\n}\n')

    def setUp(self):
        self._orig = rm.Breadcrumb
        vivos = dict(list(rm.REDE_CONHECIDA.items())[:5])
        st = self.ST

        class F:
            def __init__(s, host, port=None, role=None, password=None):
                s.h = host
            def reachable(s):    return s.h in vivos
            def authenticate(s): return True
            def get_state(s, *a, **k): return st % (vivos[s.h] or s.h)
        rm.Breadcrumb = F
        self.vivos = vivos

    def tearDown(self):
        rm.Breadcrumb = self._orig

    def test_a_lista_embutida_tem_a_frota(self):
        self.assertGreater(len(rm.REDE_CONHECIDA), 100)
        nomes = [v for v in rm.REDE_CONHECIDA.values() if v]
        self.assertTrue(any(n.startswith("ERB") for n in nomes))
        self.assertTrue(any(n.startswith("CA-") for n in nomes))

    def test_seed_sozinho_com_vizinho_sem_ip_nao_expande(self):
        # A regressão de campo, reproduzida: sem a lista, acha um só.
        um = next(iter(self.vivos))
        r = rm.descobrir_malha([um], incluir_conhecidos=False)
        self.assertEqual(len(r), 1)

    def test_com_a_lista_embutida_acha_todos_os_que_respondem(self):
        r = rm.descobrir_malha([], incluir_conhecidos=True)
        resp = {v["nome"] for v in r.values() if not v["erro"]}
        self.assertEqual(len(resp), len(self.vivos))

    def test_quem_nao_responde_aparece_com_nome_e_motivo(self):
        # Uma tela com dezenas de linhas de IP cru não diz quais
        # equipamentos estão fora.
        r = rm.descobrir_malha([], incluir_conhecidos=True)
        fora = [v for v in r.values() if v["erro"]]
        self.assertTrue(fora)
        com_nome = [v for v in fora if v["nome"] != v["ip"]]
        self.assertTrue(com_nome, "radio fora do ar perdeu o nome conhecido")
        self.assertTrue(all(v["erro"] for v in fora))

    def test_conta_vizinhos_com_e_sem_ip_separado(self):
        # "achei só o seed" e "achei vizinhos mas nenhum tinha IP" sao
        # problemas diferentes; sem separar, viram o mesmo sintoma.
        um = next(iter(self.vivos))
        r = rm.descobrir_malha([um], incluir_conhecidos=False)
        v = r[um]
        self.assertEqual(v["vizinhos"], 1)
        self.assertEqual(v["vizinhos_com_ip"], 0)
        self.assertGreater(v["bytes_state"], 0)

    def test_sem_partida_nenhuma_falha_com_motivo(self):
        with self.assertRaises(RuntimeError):
            rm.descobrir_malha([], incluir_conhecidos=False)


class TestGetStateDevolveObjeto(unittest.TestCase):
    """`bc.get_state()` devolve o OBJETO State, não texto.

    Quem converte é o `parse_state`, com `str()`. Medir o tamanho do
    retorno antes disso estourou em campo com

        TypeError: object of type 'State' has no len()

    e a exceção matou as 156 threads de uma vez: a tela ficou vazia e o
    traceback saiu no console, que quem usa a janela não vê.
    """

    class FakeState:
        """Objeto sem __len__, como o State do protobuf."""
        def __init__(self, texto): self._t = texto
        def __str__(self):  return self._t

    def setUp(self):
        self._orig = rm.Breadcrumb
        vivos = dict(list(rm.REDE_CONHECIDA.items())[:3])
        FS = self.FakeState
        st = ('configuration {\n  saved {\n    general {\n      name: "%s"\n'
              '    }\n  }\n}\nwireless {\n  name: "wlan0"\n  channel: 157\n'
              '  peer {\n    ipv4Address: "10.0.0.9"\n    signal: -70\n'
              '    rssi: 25\n  }\n}\n')

        class F:
            def __init__(s, host, port=None, role=None, password=None):
                s.h = host
            def reachable(s):    return s.h in vivos
            def authenticate(s): return True
            def get_state(s, *a, **k):
                return FS(st % (vivos[s.h] or s.h))
        rm.Breadcrumb = F
        self.vivos = vivos

    def tearDown(self):
        rm.Breadcrumb = self._orig

    def test_descoberta_lida_com_o_objeto_state(self):
        r = rm.descobrir_malha(list(self.vivos), incluir_conhecidos=False)
        resp = [v for v in r.values() if not v.get("erro")]
        self.assertEqual(len(resp), len(self.vivos),
                         f"nenhum respondeu: {[v.get('erro') for v in r.values()]}")
        self.assertTrue(all(v["bytes_state"] > 0 for v in resp))

    def test_falha_interna_vira_linha_na_tela_e_nao_thread_morta(self):
        # A propriedade que faltava: erro de programação dentro do worker
        # tem de aparecer como motivo naquele rádio, não sumir.
        class Explode:
            def __init__(s, host, port=None, role=None, password=None): pass
            def reachable(s):    return True
            def authenticate(s): return True
            def get_state(s, *a, **k):
                class SemNada:
                    def __str__(s2): raise RuntimeError("boom interno")
                return SemNada()
        rm.Breadcrumb = Explode
        r = rm.descobrir_malha(["10.0.0.1"], incluir_conhecidos=False)
        self.assertIn("10.0.0.1", r)
        self.assertTrue(r["10.0.0.1"]["erro"],
                        "erro interno sumiu em vez de virar motivo")


class TestListaDeIpsDeArquivo(unittest.TestCase):
    """Outra mina, outra lista: a de arquivo continua aceita."""

    def _tmp(self, nome, texto):
        import tempfile
        d = Path(tempfile.mkdtemp()); p = d / nome
        p.write_text(texto, encoding="utf-8")
        return p

    def test_le_o_cache_do_coletor(self):
        p = self._tmp("c.json", json.dumps({
            "10.0.0.1": {"nome": "ERB-07", "falhas": 2},
            "10.0.0.2": {"nome": "CA-1006", "falhas": 0}}))
        self.assertEqual(rm.ler_lista_de_ips(p),
                         {"10.0.0.1": "ERB-07", "10.0.0.2": "CA-1006"})

    def test_le_texto_com_um_ip_por_linha(self):
        p = self._tmp("c.txt", "# comentario\n10.0.0.1, ERB-07\n10.0.0.2\n\n")
        self.assertEqual(rm.ler_lista_de_ips(p),
                         {"10.0.0.1": "ERB-07", "10.0.0.2": "10.0.0.2"})

    def test_arquivo_sem_ip_falha_com_motivo(self):
        p = self._tmp("c.txt", "nada aqui\noutra linha\n")
        with self.assertRaises(RuntimeError):
            rm.ler_lista_de_ips(p)

    def test_arquivo_inexistente_falha_com_motivo(self):
        with self.assertRaises(RuntimeError):
            rm.ler_lista_de_ips("/nao/existe/lista.json")


class TestAgendaPorDeslocamento(unittest.TestCase):
    """Ler todo mundo a cada N segundos gasta o orçamento em quem está
    parado: a captura real da ERM-12 tem 1.095 leituras e ZERO metro
    coberto. Aqui a pergunta é quem já andou o suficiente."""

    def setUp(self):
        import coleta_rajant as col
        self.col = col
        self.ag = col.Agenda(passo_m=15.0, parado_s=30.0)

    def test_radio_nunca_lido_tem_prioridade_maxima(self):
        self.assertEqual(self.ag.prioridade("novo", 0.0), float("inf"))

    def test_quem_anda_rapido_vence_antes(self):
        self.ag.registrar("rapido", 0.0, -18.9, -43.4, 40.0)
        self.ag.registrar("lento", 0.0, -18.9, -43.4, 5.0)
        # 1,35 s a 40 km/h = 15 m; a 5 km/h seriam quase 11 s.
        self.assertGreater(self.ag.prioridade("rapido", 1.4), 1.0)
        self.assertLess(self.ag.prioridade("lento", 1.4), 1.0)

    def test_parado_ainda_e_lido_de_vez_em_quando(self):
        # Prioridade zero nunca seria eleita, e o censo de vizinhos dele
        # morreria junto.
        self.ag.registrar("parado", 0.0, -18.9, -43.4, 0.0)
        self.assertLess(self.ag.prioridade("parado", 10.0), 1.0)
        self.assertGreaterEqual(self.ag.prioridade("parado", 31.0), 1.0)

    def test_so_elege_quem_ja_venceu_o_alvo(self):
        self.ag.registrar("a", 0.0, -18.9, -43.4, 40.0)
        self.assertEqual(self.ag.eleger(["a"], 0.1, 5), [])
        self.assertEqual(self.ag.eleger(["a"], 2.0, 5), ["a"])

    def test_orcamento_por_ciclo_e_respeitado(self):
        for i in range(20):
            self.ag.registrar(f"r{i}", 0.0, -18.9, -43.4, 40.0)
        self.assertEqual(len(self.ag.eleger([f"r{i}" for i in range(20)],
                                            5.0, 6)), 6)


class TestColetaAoVivo(unittest.TestCase):
    """A coleta ponta a ponta, contra uma malha falsa que se move."""

    FROTA = {"10.0.0.10": ("CA-1006", 40.0, 0.0),
             "10.0.0.90": ("ERB-07", 0.0, 0.0)}

    def setUp(self):
        import time as _t, coleta_rajant as col
        self.col = col
        self.t0 = [_t.time()]
        self._orig = rm.Breadcrumb

    def tearDown(self):
        rm.Breadcrumb = self._orig

    def _rodar(self, segundos=3.0, passo_m=15.0, gps_hz=10.0):
        import time as _t, threading as _th
        rm.Breadcrumb = _rajant_falsa(self.FROTA, self.t0, latencia=0.01,
                                      gps_hz=gps_hz)
        c = self.col.Coleta({ip: v[0] for ip, v in self.FROTA.items()},
                            passo_m=passo_m, aviso=lambda t: None)
        th = _th.Thread(target=c.rodar, args=(0,), daemon=True)
        th.start(); _t.sleep(segundos); c.parar(); th.join(10)
        return c

    def test_coleta_amostras_com_posicao_e_vizinhos(self):
        c = self._rodar()
        self.assertGreater(len(c.amostras), 0)
        self.assertGreater(len(c.peers), 0)
        self.assertTrue(all(a["lat"] is not None for a in c.amostras))

    def test_posicao_repetida_nao_vira_amostra(self):
        # O caso que enche o arquivo de ponto empilhado: o módulo não
        # atualizou, a leitura traz a posição velha. Com gpsTime a 1 Hz e
        # alvo de 1 m, a coleta pede muito mais rápido que o GPS entrega.
        c = self._rodar(segundos=3.0, passo_m=1.0, gps_hz=1.0)
        self.assertGreater(c.n_repetidos, 0,
                           "nenhuma posicao repetida foi descartada")
        self.assertLess(len(c.amostras), c.n_lidos)
        # Cada amostra gravada tem um gpsTime diferente da anterior.
        vistos = {}
        for a in c.amostras:
            vistos.setdefault(a["radio"], []).append((a["lat"], a["lon"]))
        for r, pts in vistos.items():
            self.assertEqual(len(pts), len(set(pts)),
                             f"{r} gravou a mesma posicao duas vezes")

    def test_gps_rapido_nao_descarta_nada(self):
        c = self._rodar(segundos=3.0, passo_m=1.0, gps_hz=20.0)
        self.assertEqual(c.n_repetidos, 0)

    def test_passo_real_acompanha_o_alvo(self):
        c = self._rodar(segundos=6.0, passo_m=15.0)
        passo = c._passo_real()
        self.assertIsNotNone(passo)
        # Folga generosa: o que se afirma é que o alvo governa o
        # espaçamento, não que ele seja exato.
        self.assertLess(passo, 15.0 * 3)

    def test_radio_parado_nao_domina_as_amostras(self):
        # A ERB fixa não pode gerar tanta amostra quanto o caminhão: era
        # exatamente isso que enchia o arquivo sem cobrir metro nenhum.
        c = self._rodar(segundos=6.0, passo_m=15.0)
        por = {}
        for a in c.amostras:
            por[a["radio"]] = por.get(a["radio"], 0) + 1
        self.assertGreater(por.get("CA-1006", 0), por.get("ERB-07", 0))

    def test_parar_no_meio_preserva_o_que_ja_foi_coletado(self):
        c = self._rodar(segundos=2.0)
        n = len(c.amostras)
        self.assertGreater(n, 0)
        self.assertIsNotNone(c.fim)

    def test_sem_alvo_nenhum_falha_com_motivo(self):
        with self.assertRaises(RuntimeError):
            self.col.Coleta({}, aviso=lambda t: None).rodar(0)

    def test_conecta_com_porta_usuario_e_senha_nos_lugares_certos(self):
        """A troca de argumentos que zerou uma coleta inteira.

        `SessaoRadio(ip, porta, role, senha, ...)` recebia
        `(ip, role, senha, porta)`: a porta virava "VIEW", o usuário
        virava a senha e a senha virava 2300. Toda conexão falhava — 139
        falhas e zero amostra — e nada acusava, porque o Breadcrumb falso
        aceitava qualquer coisa sem olhar.

        Agora ele CONFERE. É o que transforma um fake em teste.
        """
        import time as _t, threading as _th
        recebidos = []

        class FakeBC:
            def __init__(s, host, port=None, role=None, password=None):
                recebidos.append({"host": host, "port": port,
                                  "role": role, "password": password})
                s.h = host
            def reachable(s):    return True
            def authenticate(s): return True
            def get_state(s, *a, **k):
                return ('configuration {\n  saved {\n    general {\n'
                        '      name: "CA-1"\n    }\n  }\n}\n'
                        'wireless {\n  name: "wlan0"\n  channel: 157\n}\n')
        rm.Breadcrumb = FakeBC
        c = self.col.Coleta({"10.0.0.1": "CA-1"}, role="co", senha="segredo",
                            porta=2300, aviso=lambda t: None)
        th = _th.Thread(target=c.rodar, args=(0,), daemon=True)
        th.start(); _t.sleep(1.0); c.parar(); th.join(10)

        self.assertTrue(recebidos, "nem tentou conectar")
        r = recebidos[0]
        self.assertEqual(r["host"], "10.0.0.1")
        self.assertEqual(r["port"], 2300, f"porta errada: {r}")
        self.assertEqual(r["role"], "co", f"usuario errado: {r}")
        self.assertEqual(r["password"], "segredo", f"senha errada: {r}")

    def test_radio_descartado_volta_a_ser_tentado(self):
        """`SessaoRadio` desiste após 3 falhas e nunca mais tenta.

        Para o survey curto de onde ela veio isso está certo; numa coleta
        de turno, não: um caminhão que passa vinte segundos atrás de uma
        bancada sairia do levantamento para o resto do dia.
        """
        import time as _t, threading as _th
        estado = {"vivo": False, "tentativas": 0}
        ST = ('configuration {\n  saved {\n    general {\n      name: "CA-1"\n'
              '    }\n  }\n}\ngps {\n  gpsSwitch {\n    enabled: true\n  }\n'
              '  gpsPos {\n    gpsTime: 143025.0\n    gpsLat: "1853.6443S"\n'
              '    gpsLong: "04325.8538W"\n  }\n}\n'
              'wireless {\n  name: "wlan0"\n  channel: 157\n}\n')

        class Intermitente:
            def __init__(s, host, port=None, role=None, password=None):
                estado["tentativas"] += 1
            def reachable(s):    return estado["vivo"]
            def authenticate(s): return True
            def get_state(s, *a, **k): return ST
        rm.Breadcrumb = Intermitente
        # Tempos curtos para o teste não levar minutos. `parado_s` também:
        # depois de uma falha a agenda só reexamina o rádio a cada
        # `parado_s`, então sem encurtá-lo o reencontro nunca chega a ser
        # tentado dentro do teste.
        c = self.col.Coleta({"10.0.0.1": "CA-1"}, reencontro_s=0.3,
                            parado_s=0.3, aviso=lambda t: None)
        th = _th.Thread(target=c.rodar, args=(0,), daemon=True); th.start()
        _t.sleep(1.0)
        self.assertEqual(len(c.amostras), 0, "deveria estar falhando")
        estado["vivo"] = True          # o caminhão saiu de trás da bancada
        _t.sleep(2.0)
        c.parar(); th.join(10)
        self.assertGreater(len(c.amostras), 0,
                           "radio descartado nunca mais foi tentado")

    def test_motivo_da_falha_chega_a_tela(self):
        # Um contador de falhas subindo sem motivo escondeu a troca de
        # argumentos por quase uma hora.
        import time as _t, threading as _th
        linhas = []

        class Recusa:
            def __init__(s, host, port=None, role=None, password=None): pass
            def reachable(s): return False
            def authenticate(s): return False
            def get_state(s, *a, **k): raise AssertionError("nao deveria chegar")
        rm.Breadcrumb = Recusa
        c = self.col.Coleta({"10.0.0.1": "CA-1"}, aviso=linhas.append)
        th = _th.Thread(target=c.rodar, args=(0,), daemon=True)
        th.start(); _t.sleep(1.0); c.parar(); th.join(10)

        self.assertGreater(c.n_falhas, 0)
        texto = " ".join(linhas)
        self.assertIn("falha em CA-1", texto, f"motivo nao apareceu: {linhas}")
        self.assertTrue(c.motivos, "nenhum motivo foi registrado")

    def test_relatorios_saem_da_coleta(self):
        import tempfile, shutil
        c = self._rodar(segundos=4.0)
        tmp = Path(tempfile.mkdtemp())
        try:
            feitos = self.col.gravar_e_gerar(c, str(tmp), aviso=lambda t: None)
            self.assertTrue(feitos)
            self.assertTrue(all(f.exists() for f in feitos))
            self.assertTrue(any(f.suffix == ".xlsx" for f in feitos))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestJanelaUnica(unittest.TestCase):
    """A janela junta coleta e arquivos; a lógica fica fora dela."""

    def test_site_survey_importa_sem_abrir_janela(self):
        # Importar não pode abrir tela nem exigir tkinter: é o que
        # permite testar o resto sem display.
        import subprocess, sys as _s
        r = subprocess.run(
            [_s.executable, "-c",
             "import sys; sys.argv=['x'];"
             "sys.path.insert(0, %r);" % str(Path(__file__).resolve().parent)
             + "import site_survey; print('ok')"],
            capture_output=True, text=True, timeout=90)
        self.assertIn("ok", r.stdout, r.stderr[-600:])

    def test_verificar_nao_abre_janela_e_devolve_codigo(self):
        # É o que o build usa para PROVAR que o exe funciona. Sem isto, a
        # única forma de saber era dar duplo clique — e se falhasse, o
        # console fechava no mesmo instante: virava "não abre", sem pista.
        import subprocess, sys as _s
        aqui = str(Path(__file__).resolve().parent)
        r = subprocess.run(
            [_s.executable, "site_survey.py", "--verificar"],
            capture_output=True, text=True, timeout=120, cwd=aqui)
        self.assertIn("Verificação do site_survey", r.stdout, r.stderr[-500:])
        # Sem tkinter tem de FALHAR, não passar calado: é exatamente o
        # caso que fazia o exe fechar sozinho.
        try:
            import tkinter  # noqa: F401
            esperado = 0
        except ImportError:
            esperado = 1
        self.assertEqual(r.returncode, esperado, r.stdout[-500:])

    def test_erro_de_partida_vai_para_arquivo(self):
        # Console de exe fecha no instante em que o programa termina. Se
        # o motivo não for para o disco, o usuário fica sem nada.
        fonte = (Path(__file__).resolve().parent / "site_survey.py").read_text(
            encoding="utf-8")
        self.assertIn("site_survey_erro.txt", fonte)
        # Congelado, __file__ aponta para o pacote temporário; o arquivo
        # tem de ir para a pasta do EXE, que é a que o usuário enxerga.
        self.assertTrue('"frozen"' in fonte or "'frozen'" in fonte,
                        "erro iria para a pasta errada quando congelado")
        # Os módulos chamam sys.exit(1) quando falta dependência, e
        # SystemExit não é Exception: com `except Exception` o processo
        # morria antes de gravar o arquivo. Foi assim que um
        # prometheus_client faltando no exe virou "não abre".
        self.assertIn("except BaseException", fonte,
                      "sys.exit no import escaparia do tratamento de erro")

    def test_verificar_nunca_espera_tecla(self):
        # O build roda `--verificar` de dentro do .bat. Um input() ali
        # penduraria a compilação para sempre, sem ninguém para apertar.
        import subprocess, sys as _s, tempfile, shutil
        aqui = Path(__file__).resolve().parent
        tmp = Path(tempfile.mkdtemp())
        erro = aqui / "site_survey_erro.txt"
        if erro.exists(): erro.unlink()
        try:
            cod = (
                "import sys\n"
                "class _Nao:\n"
                "    def find_spec(self, n, c=None, a=None):\n"
                "        if n == 'prometheus_client':\n"
                "            raise ImportError('bloqueado')\n"
                "        return None\n"
                "sys.meta_path.insert(0, _Nao())\n"
                f"sys.path.insert(0, {str(aqui)!r})\n"
                "sys.argv = ['site_survey', '--verificar']\n"
                "import site_survey\n")
            # Sem timeout generoso isto passaria por engano num ambiente
            # onde stdin já não é tty; o que se testa é que NÃO pendura.
            r = subprocess.run([_s.executable, "-c", cod],
                               capture_output=True, text=True, timeout=60,
                               cwd=str(tmp))
            self.assertNotEqual(r.returncode, 0)
            self.assertNotIn("Pressione ENTER", r.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            if erro.exists(): erro.unlink()

    def test_sys_exit_no_import_ainda_grava_o_erro(self):
        import subprocess, sys as _s, tempfile, shutil
        aqui = Path(__file__).resolve().parent
        tmp = Path(tempfile.mkdtemp())
        # Rodando como script o arquivo vai para a pasta do .py; dentro do
        # exe, para a pasta do executável (ver _pasta_do_exe). O que se
        # afirma aqui é que ele é GRAVADO, não onde.
        erro = aqui / "site_survey_erro.txt"
        if erro.exists(): erro.unlink()
        try:
            # Bloqueia prometheus_client: o rajant_monitor faz sys.exit(1).
            cod = (
                "import sys\n"
                "class _Nao:\n"
                "    def find_spec(self, n, c=None, a=None):\n"
                "        if n == 'prometheus_client':\n"
                "            raise ImportError('bloqueado')\n"
                "        return None\n"
                "sys.meta_path.insert(0, _Nao())\n"
                f"sys.path.insert(0, {str(aqui)!r})\n"
                "import site_survey\n")
            r = subprocess.run([_s.executable, "-c", cod],
                               capture_output=True, text=True, timeout=120,
                               cwd=str(tmp), input="\n")
            self.assertNotEqual(r.returncode, 0)
            self.assertTrue(erro.exists(),
                            f"nao gravou o motivo:\n{(r.stdout+r.stderr)[-400:]}")
            self.assertIn("pip install", erro.read_text(encoding="utf-8"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            if erro.exists(): erro.unlink()

    def test_nenhum_exclude_do_spec_quebra_o_programa(self):
        """Excluir do exe um módulo que é importado no topo quebra tudo.

        Aconteceu de verdade: `prometheus_client` entrou nos excludes
        porque este exe não expõe /metrics — mas o rajant_monitor o
        importa no import do módulo e SAI se faltar. O cliente recebeu

            ERRO: pip install prometheus-client

        com o pacote instalado na máquina; ele só não estava DENTRO do
        exe. E como o console fecha sozinho, o sintoma foi "não abre".

        Aqui cada nome da lista de excludes é bloqueado de verdade e se
        confere que o site_survey ainda importa.
        """
        import ast, subprocess, sys as _s
        aqui = Path(__file__).resolve().parent
        spec = (aqui / "build" / "site_survey.spec").read_text(encoding="utf-8")
        # O .spec não é importável (o PyInstaller injeta Analysis e cia.),
        # então lê-se a lista pelo AST em vez de executar o arquivo.
        alvo = None
        for no in ast.walk(ast.parse(spec)):
            if isinstance(no, ast.keyword) and no.arg == "excludes":
                alvo = [e.value for e in no.value.elts
                        if isinstance(e, ast.Constant)]
        self.assertTrue(alvo, "não achei a lista de excludes no .spec")

        bloqueio = (
            "import sys\n"
            "class _Nao:\n"
            "    def find_module(self, nome, caminho=None):\n"
            "        if nome == %r or nome.startswith(%r + '.'):\n"
            "            raise ImportError('bloqueado pelo teste: ' + nome)\n"
            "        return None\n"
            "    def find_spec(self, nome, caminho=None, alvo=None):\n"
            "        return self.find_module(nome, caminho)\n"
            "sys.meta_path.insert(0, _Nao())\n"
            "sys.path.insert(0, %r)\n"
            "import site_survey\n"
            "print('IMPORTOU')\n")
        for nome in alvo:
            r = subprocess.run(
                [_s.executable, "-c", bloqueio % (nome, nome, str(aqui))],
                capture_output=True, text=True, timeout=120, cwd=str(aqui))
            self.assertIn("IMPORTOU", r.stdout,
                          f"excluir '{nome}' do exe impede o programa de "
                          f"abrir:\n{(r.stdout + r.stderr)[-400:]}")

    def test_janela_nasce_cabendo_na_tela(self):
        # 820 px de altura fixa numa tela de 768 jogou o rodapé — com os
        # botões de Iniciar e Parar — para fora do monitor, e o programa
        # ficou sem como ser usado.
        fonte = (Path(__file__).resolve().parent / "site_survey.py").read_text(
            encoding="utf-8")
        self.assertIn("winfo_screenheight", fonte,
                      "a janela voltou a ter altura fixa")
        self.assertNotIn('jan.geometry("1200x820")', fonte)
        self.assertIn("jan.minsize", fonte)

    def test_rodape_e_reservado_antes_do_notebook_expandir(self):
        """`pack` serve quem chega primeiro.

        Um Notebook com expand=True empacotado antes do rodapé toma a
        janela inteira e espreme o resto até sumir. A ordem no código é
        a garantia — não há como afirmar isso sem display.
        """
        fonte = (Path(__file__).resolve().parent / "site_survey.py").read_text(
            encoding="utf-8")
        for antes, depois, oquê in (
                ('rod.pack(side="bottom"', 'nb.pack(fill="both", expand=True',
                 "rodapé (Salvar em / KMZ / PPT / Excel)"),
                ('txt.pack(side="bottom"', 'nb.pack(fill="both", expand=True',
                 "registro de andamento"),
                ('baixo.pack(side="bottom"', 'qd.pack(fill="both", expand=True',
                 "botões de Iniciar e Parar")):
            i, j = fonte.find(antes), fonte.find(depois)
            self.assertGreater(i, -1, f"sumiu: {antes}")
            self.assertGreater(j, -1, f"sumiu: {depois}")
            self.assertLess(i, j, f"{oquê} pode ser empurrado para fora da tela")

    def test_controles_da_coleta_ficam_no_bloco_reservado(self):
        # Se voltarem para `ab_col`, a lista de equipamentos volta a poder
        # empurrá-los para fora.
        fonte = (Path(__file__).resolve().parent / "site_survey.py").read_text(
            encoding="utf-8")
        self.assertIn("lf = ttk.Frame(baixo)", fonte,
                      "os botões de Iniciar/Parar sairam do bloco reservado")
        self.assertIn("lc = ttk.Frame(baixo)", fonte)

    def test_ha_um_unico_gerador_de_executavel(self):
        # Dois .bat e dois .spec na mesma pasta geravam dois exes, e a
        # duvida de qual abrir.
        b = Path(__file__).resolve().parent / "build"
        specs = sorted(p.name for p in b.glob("*.spec"))
        self.assertNotIn("survey_meshmapper.spec", specs,
                         f"voltou a haver dois exes de survey: {specs}")
        self.assertFalse((b / "gerar_exe_survey.bat").exists())

    def test_a_logica_nao_mora_na_janela(self):
        # Se a coleta voltar para dentro do arquivo da interface, ela
        # deixa de ser testável sem display.
        fonte = (Path(__file__).resolve().parent / "site_survey.py").read_text(
            encoding="utf-8")
        self.assertNotIn("class Coleta", fonte)
        self.assertNotIn("def descobrir_malha", fonte)


if __name__ == "__main__":
    unittest.main(verbosity=2)
