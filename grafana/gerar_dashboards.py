#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gera os dashboards Grafana do rajant_monitor.

Os JSON são gerados (e não escritos à mão) por dois motivos:

1. os nomes de métrica e de label vêm do inventário real do exporter — se uma
   métrica for removida ou renomeada, ajusta-se aqui e regera, e nenhum painel
   fica consultando série que não existe (foi exatamente o problema que a
   auditoria contra os .proto do bcapi resolveu);
2. o posicionamento é calculado por um gerenciador de layout, então não há
   painel sobreposto nem buraco na grade.

    python3 grafana/gerar_dashboards.py
"""
import json, pathlib

SAIDA = pathlib.Path(__file__).parent
DS = "${DS_PROMETHEUS}"
F_BC = 'bc=~"$bc"'

# ══════════════════════════════════════════════════════════════════
# SISTEMA VISUAL
# ══════════════════════════════════════════════════════════════════
# Cores semânticas: verde = dentro do esperado, laranja = atenção,
# vermelho = fora do limiar. Azul/roxo só para séries neutras (tráfego,
# contagens), nunca para estado.
OK, ATEN, CRIT, NEUTRO = "green", "orange", "red", "blue"

ALT_KPI = 4      # altura dos cartões de topo
ALT_SERIE = 8    # altura padrão de série temporal
ALT_TABELA = 10  # altura padrão de tabela

_id = [0]


def _proximo():
    _id[0] += 1
    return _id[0]


class Layout:
    """Empilha painéis numa grade de 24 colunas sem sobreposição."""

    def __init__(self):
        self.y = 0
        self.paineis = []

    def secao(self, titulo, recolhida=False):
        self.paineis.append({
            "id": _proximo(), "type": "row", "title": titulo,
            "gridPos": {"x": 0, "y": self.y, "w": 24, "h": 1},
            "collapsed": recolhida, "panels": []})
        self.y += 1
        return self

    def fila(self, *itens, h=ALT_SERIE):
        """itens = (largura, construtor). O construtor recebe (x, y, w, h)."""
        total = sum(i[0] for i in itens)
        if total != 24:
            raise ValueError(f"fila soma {total} colunas, deveria somar 24")
        x = 0
        for w, construtor in itens:
            self.paineis.append(construtor(x, self.y, w, h))
            x += w
        self.y += h
        return self

    def inteiro(self, construtor, h=ALT_SERIE):
        return self.fila((24, construtor), h=h)


# ══════════════════════════════════════════════════════════════════
# HELPERS DE PAINEL
# ══════════════════════════════════════════════════════════════════
def ds():
    return {"type": "prometheus", "uid": DS}


def alvo(expr, legenda="", ref="A", instant=False, formato="time_series"):
    return {"datasource": ds(), "editorMode": "code", "expr": expr,
            "legendFormat": legenda or "__auto", "range": not instant,
            "instant": instant, "refId": ref, "format": formato}


def alvos(*pares, instant=False, formato="time_series"):
    return [alvo(e, l, chr(65 + i), instant, formato)
            for i, (e, l) in enumerate(pares)]


def deg(steps):
    return {"mode": "absolute",
            "steps": [{"color": c, "value": v} for c, v in steps]}


def mapa(pares):
    return [{"type": "value",
             "options": {str(v): {"text": t, "color": c, "index": i}
                         for i, (v, t, c) in enumerate(pares)}}]


def campo(unidade=None, dec=None, min_=None, max_=None, mapas=None,
          limiares=None, cor="palette-classic", preenchimento=10,
          largura=1, suavizar=True, pontos=False, sem_valor="—"):
    custom = {
        "drawStyle": "line",
        "lineInterpolation": "smooth" if suavizar else "linear",
        "lineWidth": largura, "fillOpacity": preenchimento,
        "showPoints": "auto" if pontos else "never", "pointSize": 5,
        "spanNulls": False, "axisPlacement": "auto", "axisLabel": "",
        "axisBorderShow": False, "axisColorMode": "text",
        "scaleDistribution": {"type": "linear"},
        "gradientMode": "opacity",
        "stacking": {"mode": "none", "group": "A"},
        "hideFrom": {"legend": False, "tooltip": False, "viz": False},
        "thresholdsStyle": {"mode": "off"},
        "insertNulls": False, "barAlignment": 0,
    }
    d = {"color": {"mode": cor}, "custom": custom, "mappings": mapas or [],
         "thresholds": limiares or deg([(OK, None)]), "noValue": sem_valor}
    if unidade: d["unit"] = unidade
    if dec is not None: d["decimals"] = dec
    if min_ is not None: d["min"] = min_
    if max_ is not None: d["max"] = max_
    return {"defaults": d, "overrides": []}


def _painel(tipo, titulo, x, y, w, h, targets=None, fc=None, options=None,
            desc="", transf=None):
    p = {"id": _proximo(), "type": tipo, "title": titulo,
         "gridPos": {"x": x, "y": y, "w": w, "h": h}, "datasource": ds()}
    if desc: p["description"] = desc
    if targets is not None: p["targets"] = targets
    if fc is not None: p["fieldConfig"] = fc
    if options is not None: p["options"] = options
    if transf: p["transformations"] = transf
    return p


def serie(titulo, targets, fc=None, desc="", legenda="list", calcs=None):
    """Série temporal. Com calcs, a legenda vira tabela automaticamente."""
    modo = "table" if calcs else legenda
    return lambda x, y, w, h: _painel(
        "timeseries", titulo, x, y, w, h, targets, fc or campo(),
        {"legend": {"displayMode": modo, "placement": "bottom",
                    "showLegend": True, "calcs": calcs or []},
         "tooltip": {"mode": "multi", "sort": "desc"}}, desc)


def cartao(titulo, targets, fc=None, desc="", calc="lastNotNull",
           grafico="area", modo="auto"):
    return lambda x, y, w, h: _painel(
        "stat", titulo, x, y, w, h, targets, fc or campo(),
        {"reduceOptions": {"calcs": [calc], "fields": "", "values": False},
         "orientation": "auto", "textMode": modo, "colorMode": "value",
         "graphMode": grafico, "justifyMode": "auto",
         "wideLayout": True, "showPercentChange": False,
         "percentChangeColorMode": "standard"}, desc)


def medidor(titulo, targets, fc=None, desc="", calc="lastNotNull"):
    return lambda x, y, w, h: _painel(
        "gauge", titulo, x, y, w, h, targets, fc or campo(),
        {"reduceOptions": {"calcs": [calc], "fields": "", "values": False},
         "orientation": "auto", "showThresholdLabels": False,
         "showThresholdMarkers": True, "minVizWidth": 75,
         "minVizHeight": 75, "sizing": "auto"}, desc)


def barras(titulo, targets, fc=None, desc="", ordem="desc"):
    """Bar gauge — ideal para ranking (top N piores/melhores)."""
    return lambda x, y, w, h: _painel(
        "bargauge", titulo, x, y, w, h, targets, fc or campo(),
        {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "",
                           "values": False},
         "orientation": "horizontal", "displayMode": "gradient",
         "valueMode": "color", "showUnfilled": True, "sizing": "auto",
         "minVizWidth": 8, "minVizHeight": 16,
         "namePlacement": "left", "maxVizHeight": 300}, desc)


def linha_tempo(titulo, targets, fc=None, desc="", mostrar_valor="auto"):
    return lambda x, y, w, h: _painel(
        "state-timeline", titulo, x, y, w, h, targets, fc or campo(),
        {"showValue": mostrar_valor, "rowHeight": 0.9, "mergeValues": True,
         "alignValue": "center", "perPage": 20,
         "legend": {"displayMode": "list", "placement": "bottom",
                    "showLegend": True},
         "tooltip": {"mode": "single", "sort": "none"}}, desc)


def mapa_calor(titulo, targets, fc=None, desc="", unidade="percent"):
    return lambda x, y, w, h: _painel(
        "heatmap", titulo, x, y, w, h, targets, fc or campo(unidade),
        {"calculate": False,
         "color": {"mode": "scheme", "scheme": "Turbo", "steps": 64,
                   "reverse": False, "exponent": 0.5, "fill": "dark-orange"},
         "yAxis": {"unit": unidade, "axisPlacement": "left"},
         "cellGap": 1, "cellValues": {},
         "filterValues": {"le": 1e-9},
         "legend": {"show": True},
         "exemplars": {"color": "rgba(255,0,255,0.7)"},
         "rowsFrame": {"layout": "auto"},
         "tooltip": {"mode": "single", "showColorScale": True,
                     "yHistogram": False}}, desc)


def tabela(titulo, targets, fc=None, desc="", transf=None, ordenar=None,
           larguras=None):
    op = {"showHeader": True, "cellHeight": "sm",
          "footer": {"show": False, "reducer": ["sum"], "countRows": False,
                     "fields": ""}}
    if ordenar:
        op["sortBy"] = [{"displayName": ordenar[0], "desc": ordenar[1]}]

    def construir(x, y, w, h):
        p = _painel("table", titulo, x, y, w, h, targets, fc or campo(), op,
                    desc, transf)
        if larguras:
            p["fieldConfig"]["overrides"] = [
                {"matcher": {"id": "byName", "options": nome},
                 "properties": [{"id": "custom.width", "value": lg}]}
                for nome, lg in larguras.items()]
        return p
    return construir


def texto(titulo, md):
    return lambda x, y, w, h: _painel("text", titulo, x, y, w, h, None, None,
                                      {"mode": "markdown", "content": md})


def limpar(n, extras=None, manter=None):
    """Transformação `organize` para tabelas com N queries unidas.

    Remove Time/__name__/job/instance de todas as queries e os labels
    duplicados das queries 2..N.
    """
    ex = {}
    for i in range(1, n + 1):
        for campo_ in ("Time", "__name__", "job", "instance"):
            ex[f"{campo_} {i}"] = True
    for rot in (manter or []):
        for i in range(2, n + 1):
            ex[f"{rot} {i}"] = True
    ex.update(extras or {})
    return ex


def juntar(por, n, renomear, manter=None, extras=None):
    return [{"id": "joinByField", "options": {"byField": por, "mode": "outer"}},
            {"id": "organize", "options": {
                "excludeByName": limpar(n, extras, manter),
                "renameByName": renomear, "indexByName": {}}}]


# ══════════════════════════════════════════════════════════════════
# VARIÁVEIS E ESQUELETO
# ══════════════════════════════════════════════════════════════════
def var_ds():
    return {"current": {}, "hide": 0, "includeAll": False, "multi": False,
            "name": "DS_PROMETHEUS", "label": "Fonte de dados", "options": [],
            "query": "prometheus", "refresh": 1, "regex": "",
            "skipUrlSync": False, "type": "datasource"}


def var_query(nome, rotulo, consulta):
    return {"name": nome, "label": rotulo, "type": "query", "datasource": ds(),
            "definition": consulta,
            "query": {"query": consulta, "refId": nome},
            "refresh": 2, "sort": 1, "multi": True, "includeAll": True,
            "allValue": ".*",
            "current": {"selected": True, "text": ["All"],
                        "value": ["$__all"]},
            "options": [], "hide": 0, "skipUrlSync": False}


def dashboard(uid, titulo, layout, variaveis, desc="", janela="now-6h"):
    return {
        "annotations": {"list": [{
            "builtIn": 1,
            "datasource": {"type": "grafana", "uid": "-- Grafana --"},
            "enable": True, "hide": True,
            "iconColor": "rgba(0, 211, 255, 1)",
            "name": "Annotations & Alerts", "type": "dashboard"}]},
        "description": desc, "editable": True, "fiscalYearStartMonth": 0,
        "graphTooltip": 1, "id": None,
        "links": [{"asDropdown": True, "icon": "external link",
                   "includeVars": True, "keepTime": True, "tags": ["rajant"],
                   "targetBlank": False, "title": "Dashboards Rajant",
                   "tooltip": "", "type": "dashboards", "url": ""}],
        "panels": layout.paineis, "preload": False, "refresh": "1m",
        "schemaVersion": 39, "tags": ["rajant"],
        "templating": {"list": variaveis},
        "time": {"from": janela, "to": "now"}, "timepicker": {},
        "timezone": "browser", "title": titulo, "uid": uid, "version": 1,
        "weekStart": "",
    }


# ══════════════════════════════════════════════════════════════════
# TEXTOS DA AUDITORIA
# ══════════════════════════════════════════════════════════════════
NOTA_AUDITORIA = """
### Séries removidas na auditoria contra os `.proto` do bcapi

O campo que alimentava cada uma **não existe** na BC API. Elas publicavam `0`
permanente — indistinguível de "medido e sem ocorrência", que num painel se lê
como equipamento saudável.

`rajant_voltagem_v` · `rajant_voltagem_min_v` · `rajant_voltagem_max_v` ·
`rajant_bateria_v` · `rajant_session_state` · `rajant_im_unicast_drop` ·
`rajant_im_overflows` · `rajant_im_undeliv_rx` · `rajant_im_undeliv_tx` ·
`rajant_radio_radar_detec` · `rajant_radio_radar_pulsos` ·
`rajant_radio_phy_erros` · `rajant_peer_txpower_dbm` · `rajant_eth_rx_erros` ·
`rajant_eth_tx_erros` · `rajant_eth_rx_crc` · `rajant_eth_rx_drop` ·
`rajant_eth_tx_drop` · `rajant_eth_mudancas`

Painel antigo que consulte uma delas fica vazio — é esperado, não é falha de
coleta.

### ⚠️ `rajant_eth_apt_state` mudou de numeração

O exportador antigo usava `LINK=2, NONE=3`. O enum de `State.proto` é o
inverso — `MASTER=0, SLAVE=1, NONE=2, LINK=3`. Estes dashboards já usam a
numeração oficial.

### Substituições

| saiu | entrou |
|---|---|
| `rajant_voltagem_v` (volts) | `rajant_bateria_pct` — **carga em %, não tensão** |
| contadores inventados do InstaMesh | `rajant_im_source_floods_drop`, `rajant_im_pkt_multicast` |
| — | `rajant_gps_rumo_graus`, `_satelites`, `_altitude_m`, `_precisao_h` |
| — | `rajant_best_radio_rate`, `rajant_alertas_ativos` |
| — | `rajant_eth_peers` (tinha Gauge, nunca era publicada) |
"""

AVISO_APT = """
## ⚠️ `rajant_eth_apt_state` mudou de numeração

O exportador antigo publicava `LINK=2` e `NONE=3`. O enum de `State.proto` é:

```
APT_STATE_MASTER = 0
APT_STATE_SLAVE  = 1
APT_STATE_NONE   = 2
APT_STATE_LINK   = 3
```

Os painéis desta pasta já usam a numeração oficial. **Painel antigo que traduza
2/3 está invertido** — ajuste antes de confiar nele.

## Não existe contador de erro por porta

`State.Wired.stats` é do tipo `CommStats`, com exatamente quatro campos:
`rxBytes`, `rxPackets`, `txBytes`, `txPackets`.

Não há erro, CRC, descarte nem contagem de mudanças de link no protocolo — por
isso as seis séries correspondentes foram removidas. O sinal de **link é
inferido** de `aptState` + presença de peer, já que o campo `linkup` que o
parser procurava também não existe.

Para instabilidade, o que existe é `rajant_link_changes` (peers que mudaram
entre coletas), no dashboard **Visão Geral**.
"""

NOTA_SAUDE = """
### CPU é **derivada**, não medida

Não existe `cpuLoad` em `.proto` nenhum. `State.System` oferece `idle` +
`uptime` — o mesmo par do `/proc/uptime`. A carga sai de

```
100 * (1 - Δidle / Δuptime)
```

Como ler o painel:

- **precisa de duas coletas** — o primeiro ponto após o exportador subir não
  existe (buraco na série, não zero);
- é a **média do intervalo**, não o pico instantâneo;
- com `idle` por núcleo o resultado sai de `[0,100]` e o exportador **não
  publica**, em vez de publicar número errado.

**Buraco aqui significa "não deu para medir", nunca "CPU em zero".**

### Bateria substituiu tensão

`rajant_voltagem_v` e companhia saíram: não existe bloco de sensores na BC API.
O que existe é `State.Battery` — e **carga em % não é tensão**. Modelos sem
bateria não publicam nenhuma dessas séries (o painel fica vazio de propósito).

### Temperatura

`State.System.temperature` é `int32` sem unidade documentada. O exportador
decide pela magnitude (`|v| > 200` → centi-grau). Confirme com
`--dump-state <ip>` num BC da frota.
"""

MAPA_APT = mapa([(0, "MASTER", OK), (1, "SLAVE", NEUTRO),
                 (2, "NONE", "text"), (3, "LINK", ATEN)])
MAPA_LINK = mapa([(0, "DOWN", CRIT), (1, "UP", OK)])
MAPA_SIM_NAO = mapa([(0, "não", "text"), (1, "sim", OK)])


# ══════════════════════════════════════════════════════════════════
# 1. VISÃO GERAL
# ══════════════════════════════════════════════════════════════════
def dash_visao_geral():
    _id[0] = 0
    L = Layout()

    L.secao("Indicadores da malha")
    L.fila(
        (3, cartao("BCs online",
                   alvos((f'sum(rajant_online{{{F_BC}}})', "online")),
                   campo(dec=0, cor="thresholds",
                         limiares=deg([(CRIT, None), (OK, 1)])),
                   "Soma de rajant_online agora.")),
        (3, cartao("Nós físicos",
                   alvos(('rajant_nos_fisicos_total', "nós")),
                   campo(dec=0, cor="fixed", preenchimento=0),
                   "BreadCrumbs distintos — IPs do mesmo nó são agrupados.",
                   grafico="none")),
        (4, medidor("Disponibilidade 24 h",
                    alvos((f'avg(rajant_disponibilidade_24h_pct{{{F_BC}}})',
                           "24h")),
                    campo("percent", 2, 90, 100, cor="thresholds",
                          limiares=deg([(CRIT, None), (ATEN, 99), (OK, 99.9)])),
                    "Média da frota filtrada. Meta BCE: 99,9 %.")),
        (4, medidor("Disponibilidade 7 d",
                    alvos((f'avg(rajant_disponibilidade_7d_pct{{{F_BC}}})',
                           "7d")),
                    campo("percent", 2, 90, 100, cor="thresholds",
                          limiares=deg([(CRIT, None), (ATEN, 99), (OK, 99.9)])),
                    "Janela de 7 dias — a metodologia BCE.")),
        (3, cartao("Alertas ativos",
                   alvos((f'sum(rajant_alertas_ativos{{{F_BC}}})', "alertas")),
                   campo(dec=0, cor="thresholds",
                         limiares=deg([(OK, None), (ATEN, 1), (CRIT, 10)])),
                   "AlertSystem.alerts — série nova da auditoria.")),
        (3, cartao("Pedem reboot",
                   alvos((f'sum(rajant_reboot_needed{{{F_BC}}})', "reboot")),
                   campo(dec=0, cor="thresholds",
                         limiares=deg([(OK, None), (CRIT, 1)])),
                   "State.System.reboot.")),
        (4, cartao("Latência média",
                   alvos((f'avg(rajant_ping_rtt_ms{{{F_BC}}})', "rtt")),
                   campo("ms", 1, cor="thresholds",
                         limiares=deg([(OK, None), (ATEN, 10), (CRIT, 50)])))),
        h=ALT_KPI)

    L.secao("Disponibilidade e estabilidade")
    L.fila(
        (12, serie("Disponibilidade por BC (1 h · 24 h)",
                   alvos((f'rajant_disponibilidade_1h_pct{{{F_BC}}}',
                          "{{bc}} · 1h"),
                         (f'rajant_disponibilidade_24h_pct{{{F_BC}}}',
                          "{{bc}} · 24h")),
                   campo("percent", 2, 90, 100),
                   calcs=["mean", "min"])),
        (12, serie("Mudanças de link (instabilidade)",
                   alvos((f'rajant_link_changes{{{F_BC}}}', "{{bc}}")),
                   campo(dec=0, preenchimento=20),
                   "Peers que mudaram entre coletas. É a aproximação de "
                   "'flap' possível: a BC API não tem contador de state "
                   "changes por porta.",
                   calcs=["sum", "max"])),
        h=ALT_SERIE)

    L.fila(
        (12, serie("Mesh score por BC",
                   alvos((f'rajant_mesh_score{{{F_BC}}}', "{{bc}}")),
                   campo("none", 1, 0, 100, preenchimento=12,
                         cor="thresholds",
                         limiares=deg([(CRIT, None), (ATEN, 55), (OK, 70)])),
                   "Score composto de qualidade da malha (0–100).",
                   calcs=["mean", "min"])),
        (12, serie("Latência ICMP por BC",
                   alvos((f'rajant_ping_rtt_ms{{{F_BC}}}', "{{bc}}")),
                   campo("ms", 1, preenchimento=8),
                   calcs=["mean", "max"])),
        h=ALT_SERIE)

    L.fila(
        (10, barras("Piores disponibilidades (24 h)",
                    alvos((f'bottomk(10, rajant_disponibilidade_24h_pct'
                           f'{{{F_BC}}})', "{{bc}}")),
                    campo("percent", 2, 90, 100, cor="thresholds",
                          limiares=deg([(CRIT, None), (ATEN, 99), (OK, 99.9)])),
                    "Os 10 candidatos a investigação.")),
        (14, tabela("BCs com falhas de coleta",
                    alvos((f'rajant_falhas_consecutivas{{{F_BC}}} > 0',
                           "{{bc}}"), instant=True, formato="table"),
                    campo(dec=0, cor="thresholds",
                          limiares=deg([(ATEN, None), (CRIT, 3)])),
                    "Tabela vazia = nenhuma falha, que é o esperado.",
                    transf=[{"id": "organize", "options": {
                        "excludeByName": limpar(1),
                        "renameByName": {"Value": "falhas consecutivas",
                                         "bc": "BreadCrumb", "ip": "IP"}}}],
                    ordenar=("falhas consecutivas", True),
                    larguras={"IP": 130, "falhas consecutivas": 150})),
        h=ALT_TABELA)

    L.secao("Inventário")
    L.inteiro(
        tabela("Modelo, firmware e grupos",
               alvos((f'rajant_bc_info{{{F_BC}}}', ""),
                     instant=True, formato="table"),
               campo(),
               "O label 'bc' agora vem de configuration.saved.general.name — "
               "antes o parser pegava o primeiro name: da configuração, que "
               "podia ser nome de grupo ou de porta.",
               transf=[{"id": "organize", "options": {
                   "excludeByName": {**limpar(1), "Value": True},
                   "renameByName": {"bc": "BreadCrumb", "ip": "IP",
                                    "modelo": "Modelo",
                                    "firmware": "Firmware",
                                    "grupos": "Grupos"}}}],
               ordenar=("BreadCrumb", False),
               larguras={"IP": 140, "Modelo": 120, "Firmware": 120}),
        h=ALT_TABELA)

    L.secao("Localização (GPS)")
    L.fila(
        (14, lambda x, y, w, h: _painel(
            "geomap", "Posição dos BreadCrumbs", x, y, w, h,
            [alvo(f'rajant_gps_lat{{{F_BC}}}', "{{bc}}", "A", True, "table"),
             alvo(f'rajant_gps_lon{{{F_BC}}}', "{{bc}}", "B", True, "table")],
            campo(),
            {"view": {"id": "fit", "lat": 0, "lon": 0, "zoom": 4},
             "controls": {"showZoom": True, "showAttribution": True,
                          "mouseWheelZoom": True, "showScale": False,
                          "showMeasure": False, "showDebug": False},
             "basemap": {"type": "default", "name": "Basemap", "config": {}},
             "tooltip": {"mode": "details"},
             "layers": [{
                 "type": "markers", "name": "BreadCrumbs", "tooltip": True,
                 "location": {"mode": "coords", "latitude": "latitude",
                              "longitude": "longitude"},
                 "config": {"style": {
                     "size": {"fixed": 7, "max": 15, "min": 2},
                     "color": {"fixed": OK},
                     "opacity": 0.8, "rotation": {"fixed": 0},
                     "symbol": {"mode": "fixed",
                                "fixed": "img/icons/marker/circle.svg"},
                     "symbolAlign": {"horizontal": "center",
                                     "vertical": "center"},
                     "textConfig": {"fontSize": 10, "offsetX": 0,
                                    "offsetY": 0, "textAlign": "center",
                                    "textBaseline": "middle"}},
                     "showLegend": False}}]},
            "Junta rajant_gps_lat e rajant_gps_lon pelo label 'bc'. Só "
            "aparecem BCs com fix válido — gpsSwitch desabilitado invalida "
            "a posição.",
            juntar("bc", 2,
                   {"Value #A": "latitude", "Value #B": "longitude",
                    "ip 1": "IP"},
                   manter=["ip"]))),
        (10, tabela("Qualidade do fix",
                    [alvo(f'rajant_gps_fix{{{F_BC}}}', "", "A", True,
                          "table"),
                     alvo(f'rajant_gps_satelites{{{F_BC}}}', "", "B", True,
                          "table"),
                     alvo(f'rajant_gps_precisao_h{{{F_BC}}}', "", "C", True,
                          "table"),
                     alvo(f'rajant_gps_altitude_m{{{F_BC}}}', "", "D", True,
                          "table"),
                     alvo(f'rajant_gps_vel_kmh{{{F_BC}}}', "", "E", True,
                          "table"),
                     alvo(f'rajant_gps_rumo_graus{{{F_BC}}}', "", "F", True,
                          "table")],
                    campo(dec=1),
                    "Séries novas: gpsSatsInView, gpsPrecisionH, gpsAlt, "
                    "gpsSpeedKph e gpsTrackDegreesTrue. A velocidade já vem "
                    "em km/h — o parser antigo lia um campo 'speed' "
                    "inexistente e ainda reconvertia de nós; o rumo era "
                    "calculado e nunca publicado.",
                    transf=juntar("bc", 6,
                                  {"Value #A": "fix",
                                   "Value #B": "satélites",
                                   "Value #C": "precisão H",
                                   "Value #D": "altitude (m)",
                                   "Value #E": "velocidade (km/h)",
                                   "Value #F": "rumo (°)",
                                   "bc": "BreadCrumb"},
                                  manter=["ip"],
                                  extras={"ip 1": True}),
                    ordenar=("satélites", False))),
        h=ALT_TABELA + 1)

    L.secao("Notas da auditoria", recolhida=False)
    L.inteiro(texto("O que mudou nas métricas", NOTA_AUDITORIA), h=13)

    return dashboard(
        "rajant-visao-geral", "Rajant · 1 · Visão Geral da Malha", L,
        [var_ds(), var_query("bc", "BreadCrumb",
                             "label_values(rajant_online, bc)")],
        "Panorama da malha Rajant. Métricas alinhadas aos .proto do bcapi.")


# ══════════════════════════════════════════════════════════════════
# 2. RÁDIO / RF
# ══════════════════════════════════════════════════════════════════
def dash_rf():
    _id[0] = 0
    F = f'{F_BC}, radio=~"$radio", freq=~"$freq"'
    L = Layout()

    L.secao("Indicadores de RF")
    L.fila(
        (4, cartao("Rádios",
                   alvos((f'count(rajant_radio_canal{{{F}}})', "rádios")),
                   campo(dec=0, cor="fixed", preenchimento=0),
                   grafico="none")),
        (4, medidor("Ruído médio",
                    alvos((f'avg(rajant_radio_ruido_dbm{{{F}}})', "ruído")),
                    campo("dBm", 1, -120, -40, cor="thresholds",
                          limiares=deg([(OK, None), (ATEN, -100), (CRIT, -90)])),
                    "BCE: bom ≤ -80 dBm. Valor maior (menos negativo) é pior.")),
        (4, medidor("Ocupação do canal",
                    alvos((f'avg(rajant_radio_busy_pct{{{F}}})', "busy")),
                    campo("percent", 1, 0, 100, cor="thresholds",
                          limiares=deg([(OK, None), (ATEN, 50), (CRIT, 70)])),
                    "Razão channelBusyTime/channelActiveTime — adimensional, "
                    "logo não depende da unidade dos contadores.")),
        (4, cartao("Peers ativos",
                   alvos((f'sum(rajant_radio_peers_ativos{{{F}}})', "peers")),
                   campo(dec=0, cor="fixed"))),
        (4, cartao("Clientes WiFi",
                   alvos((f'sum(rajant_radio_clientes{{{F}}})', "clientes")),
                   campo(dec=0, cor="fixed"),
                   "Contagem dos blocos AP.client. Antes o parser procurava "
                   "clientCount (inexistente) e ainda exigia um campo "
                   "'enabled' que não existe em State.Wireless.AP — nenhum "
                   "AP entrava na lista.")),
        (4, cartao("Melhor taxa de rádio",
                   alvos((f'max(rajant_best_radio_rate{{{F_BC}}})', "best")),
                   campo(dec=0, cor="fixed"),
                   "AlertSystem.bestRadioRate — série nova.")),
        h=ALT_KPI)

    L.secao("Espectro")
    L.fila(
        (12, serie("Ruído por rádio",
                   alvos((f'rajant_radio_ruido_dbm{{{F}}}',
                          "{{bc}} · {{radio}} · ch{{canal}}")),
                   campo("dBm", 1, preenchimento=6),
                   calcs=["mean", "max"])),
        (12, mapa_calor("Distribuição da ocupação de canal",
                        alvos((f'rajant_radio_busy_pct{{{F}}}',
                               "{{bc}} · {{radio}}")),
                        desc="Concentração à direita = espectro saturado.")),
        h=ALT_SERIE + 1)

    L.fila(
        (12, serie("Ocupação detalhada",
                   alvos((f'avg(rajant_radio_busy_pct{{{F}}})', "busy"),
                         (f'avg(rajant_radio_rx_pct{{{F}}})', "RX"),
                         (f'avg(rajant_radio_tx_pct{{{F}}})', "TX"),
                         (f'avg(rajant_radio_idle_pct{{{F}}})', "idle")),
                   campo("percent", 1, 0, 100, preenchimento=15),
                   calcs=["mean"])),
        (12, serie("Potência de transmissão",
                   alvos((f'rajant_radio_txpower_dbm{{{F}}}',
                          "{{bc}} · {{radio}}")),
                   campo("dBm", 0, preenchimento=0, largura=2),
                   "State.Wireless.txpower — do rádio local. A potência do "
                   "peer remoto não existe no protocolo; "
                   "rajant_peer_txpower_dbm foi removida.",
                   calcs=["last"])),
        h=ALT_SERIE)

    L.fila(
        (12, barras("Canais mais poluídos (pior ruído)",
                    alvos((f'topk(10, max by (canal, freq) '
                           f'(rajant_radio_ruido_dbm{{{F}}}))',
                           "ch{{canal}} · {{freq}}")),
                    campo("dBm", 1, -120, -40, cor="thresholds",
                          limiares=deg([(OK, None), (ATEN, -100),
                                        (CRIT, -90)])),
                    "Maior valor em dBm = mais ruidoso.")),
        (12, barras("Rádios mais ocupados",
                    alvos((f'topk(10, rajant_radio_busy_pct{{{F}}})',
                           "{{bc}} · {{radio}}")),
                    campo("percent", 1, 0, 100, cor="thresholds",
                          limiares=deg([(OK, None), (ATEN, 50), (CRIT, 70)])))),
        h=ALT_SERIE + 1)

    L.secao("Tráfego e peers")
    L.fila(
        (12, serie("Throughput RF",
                   alvos((f'sum by (bc) (rajant_radio_rx_mbps{{{F}}})',
                          "{{bc}} RX"),
                         (f'sum by (bc) (rajant_radio_tx_mbps{{{F}}})',
                          "{{bc}} TX")),
                   campo("Mbits", 2, preenchimento=15),
                   calcs=["mean", "max"])),
        (12, serie("Peers ativos vs. peers bons (SNR > 30 dB)",
                   alvos((f'sum by (bc) (rajant_radio_peers_ativos{{{F}}})',
                          "{{bc}} ativos"),
                         (f'sum by (bc) (rajant_radio_good_peers{{{F}}})',
                          "{{bc}} bons")),
                   campo(dec=0, preenchimento=8),
                   "good_peers depende do SNR, corrigido para signal - noise. "
                   "A conta antiga (rssi - abs(noise)) era sempre negativa e "
                   "zerava esta série na rede toda.",
                   calcs=["mean"])),
        h=ALT_SERIE)

    L.secao("Detalhe por rádio")
    L.inteiro(
        tabela("Rádios — visão instantânea",
               [alvo(f'rajant_radio_canal{{{F}}}', "", "A", True, "table"),
                alvo(f'rajant_radio_ruido_dbm{{{F}}}', "", "B", True, "table"),
                alvo(f'rajant_radio_busy_pct{{{F}}}', "", "C", True, "table"),
                alvo(f'rajant_radio_peers_ativos{{{F}}}', "", "D", True,
                     "table"),
                alvo(f'rajant_radio_clientes{{{F}}}', "", "E", True, "table"),
                alvo(f'rajant_radio_txpower_dbm{{{F}}}', "", "F", True,
                     "table")],
               campo(dec=1),
               transf=juntar("radio", 6,
                             {"bc 1": "BreadCrumb", "ip 1": "IP",
                              "canal 1": "canal", "freq 1": "banda",
                              "radio": "rádio",
                              "Value #A": "canal nº",
                              "Value #B": "ruído (dBm)",
                              "Value #C": "busy %",
                              "Value #D": "peers ativos",
                              "Value #E": "clientes",
                              "Value #F": "TX (dBm)"},
                             manter=["ip", "bc", "canal", "freq"]),
               ordenar=("busy %", True),
               larguras={"IP": 130, "banda": 90, "rádio": 90}),
        h=ALT_TABELA)

    L.secao("Clientes por SSID")
    L.inteiro(
        serie("Clientes por SSID",
              alvos((f'rajant_ap_clientes{{{F_BC}}}',
                     "{{bc}} · {{essid}} ({{freq}})")),
              campo(dec=0, preenchimento=15),
              "Contagem dos blocos State.Wireless.AP.client.",
              calcs=["mean", "max"]),
        h=ALT_SERIE)

    L.secao("Contadores brutos", recolhida=True)
    L.fila(
        (12, serie("Tempo de canal (contadores acumulados)",
                   alvos((f'rajant_radio_ch_active_ms{{{F}}}',
                          "{{bc}} · {{radio}} ativo"),
                         (f'rajant_radio_ch_busy_ms{{{F}}}',
                          "{{bc}} · {{radio}} busy"),
                         (f'rajant_radio_ch_rx_ms{{{F}}}',
                          "{{bc}} · {{radio}} RX"),
                         (f'rajant_radio_ch_tx_ms{{{F}}}',
                          "{{bc}} · {{radio}} TX")),
                   campo(dec=0, preenchimento=0),
                   "channelActiveTime e afins são uint64 sem unidade "
                   "documentada no .proto — o sufixo _ms do nome da métrica é "
                   "suposição. As porcentagens acima são razões entre eles, "
                   "então não dependem disso.",
                   calcs=["last"])),
        (12, serie("Classificação do ruído",
                   alvos((f'rajant_radio_ruido_classe{{{F}}}',
                          "{{bc}} · {{radio}}")),
                   campo(dec=0, min_=0, max_=2, preenchimento=15,
                         mapas=mapa([(0, "poor", CRIT), (1, "fair", ATEN),
                                     (2, "good", OK)]),
                         cor="thresholds",
                         limiares=deg([(CRIT, None), (ATEN, 1), (OK, 2)])),
                   "BCE pg.90: good ≤ -80 dBm · fair ≤ -40 · poor > -40.",
                   calcs=["last"])),
        h=ALT_SERIE)
    L.fila(
        (12, serie("Bytes acumulados por rádio",
                   alvos((f'rajant_radio_rx_bytes{{{F}}}',
                          "{{bc}} · {{radio}} RX"),
                         (f'rajant_radio_tx_bytes{{{F}}}',
                          "{{bc}} · {{radio}} TX")),
                   campo("bytes", 0, preenchimento=0), calcs=["last"])),
        (12, serie("Pacotes por rádio",
                   alvos((f'rajant_radio_rx_pps{{{F}}}',
                          "{{bc}} · {{radio}} RX/s"),
                         (f'rajant_radio_tx_pps{{{F}}}',
                          "{{bc}} · {{radio}} TX/s")),
                   campo("pps", 1, preenchimento=8), calcs=["mean"])),
        h=ALT_SERIE)
    L.fila(
        (12, serie("Pacotes acumulados por rádio",
                   alvos((f'rajant_radio_rx_pacotes{{{F}}}',
                          "{{bc}} · {{radio}} RX"),
                         (f'rajant_radio_tx_pacotes{{{F}}}',
                          "{{bc}} · {{radio}} TX")),
                   campo(dec=0, preenchimento=0), calcs=["last"])),
        (12, serie("Peers totais vs. ativos",
                   alvos((f'rajant_radio_peers_total{{{F}}}',
                          "{{bc}} · {{radio}} total"),
                         (f'rajant_radio_peers_ativos{{{F}}}',
                          "{{bc}} · {{radio}} ativos")),
                   campo(dec=0, preenchimento=8),
                   "A diferença entre as duas séries são peers conhecidos "
                   "mas com State.Peer.enabled = false.",
                   calcs=["mean"])),
        h=ALT_SERIE)

    return dashboard(
        "rajant-rf", "Rajant · 2 · Rádio e Espectro", L,
        [var_ds(),
         var_query("bc", "BreadCrumb", "label_values(rajant_radio_canal, bc)"),
         var_query("radio", "Rádio", "label_values(rajant_radio_canal, radio)"),
         var_query("freq", "Banda", "label_values(rajant_radio_canal, freq)")],
        "Espectro, ocupação de canal e clientes. Sem radar/DFS e sem erros de "
        "PHY: não existem campos correspondentes em State.Wireless.")


# ══════════════════════════════════════════════════════════════════
# 3. ENLACES
# ══════════════════════════════════════════════════════════════════
def dash_enlaces():
    _id[0] = 0
    F = f'{F_BC}, radio=~"$radio"'
    L = Layout()

    L.secao("Indicadores dos enlaces")
    L.fila(
        (4, cartao("Enlaces ativos",
                   alvos((f'sum(rajant_peer_ativo{{{F}}})', "ativos")),
                   campo(dec=0, cor="fixed"))),
        (4, medidor("SNR médio",
                    alvos((f'avg(rajant_peer_snr_db{{{F}}})', "snr")),
                    campo("none", 1, 0, 60, cor="thresholds",
                          limiares=deg([(CRIT, None), (ATEN, 15), (OK, 30)])),
                    "SNR = signal - noise (dBm). A conta antiga era "
                    "rssi - abs(noise) e devolvia sempre negativo.")),
        (4, cartao("Enlaces com SNR < 15 dB",
                   alvos((f'count(rajant_peer_snr_db{{{F}}} < 15) or vector(0)',
                          "ruins")),
                   campo(dec=0, cor="thresholds",
                         limiares=deg([(OK, None), (ATEN, 1), (CRIT, 5)])))),
        (4, cartao("Taxa média",
                   alvos((f'avg(rajant_peer_taxa_mbps{{{F}}})', "taxa")),
                   campo("Mbits", 1, cor="fixed"),
                   "State.Peer.rate ÷ 10 — confirmado no bc_livestats.py.")),
        (4, cartao("Custo médio InstaMesh",
                   alvos((f'avg(rajant_peer_custo{{{F}}})', "custo")),
                   campo(dec=0, cor="fixed"), "Menor = rota melhor.")),
        (4, cartao("First hop médio",
                   alvos((f'avg(rajant_first_hop_cost{{{F}}})', "hop")),
                   campo(dec=0, cor="fixed"))),
        h=ALT_KPI)

    L.secao("Piores enlaces agora")
    L.fila(
        (12, barras("Menores SNR",
                    alvos((f'bottomk(10, rajant_peer_snr_db{{{F}}})',
                           "{{bc}} · {{radio}} → {{peer}}")),
                    campo("none", 1, 0, 60, cor="thresholds",
                          limiares=deg([(CRIT, None), (ATEN, 15), (OK, 30)])),
                    "Candidatos a realinhamento / site survey.")),
        (12, barras("Maiores custos InstaMesh",
                    alvos((f'topk(10, rajant_peer_custo{{{F}}})',
                           "{{bc}} · {{radio}} → {{peer}}")),
                    campo(dec=0, cor="continuous-YlRd"),
                    "Custo alto = rota cara para a malha.")),
        h=ALT_SERIE + 1)

    L.secao("Séries por enlace")
    L.fila(
        (12, serie("SNR por enlace",
                   alvos((f'rajant_peer_snr_db{{{F}}}',
                          "{{bc}} · {{radio}} → {{peer}}")),
                   campo("none", 1, preenchimento=6),
                   "Limiar BCE: bom > 30 dB.", calcs=["mean", "min"])),
        (12, serie("Sinal por enlace",
                   alvos((f'rajant_peer_sinal_dbm{{{F}}}',
                          "{{bc}} · {{radio}} → {{peer}}")),
                   campo("dBm", 0, preenchimento=6),
                   "State.Peer.signal, em dBm absoluto. rajant_peer_rssi é a "
                   "escala relativa do Rajant — são grandezas diferentes.",
                   calcs=["mean", "min"])),
        h=ALT_SERIE + 1)

    L.fila(
        (12, serie("Taxa por enlace",
                   alvos((f'rajant_peer_taxa_mbps{{{F}}}',
                          "{{bc}} · {{radio}} → {{peer}}")),
                   campo("Mbits", 1, preenchimento=8),
                   calcs=["mean", "min"])),
        (12, serie("Custo InstaMesh por enlace",
                   alvos((f'rajant_peer_custo{{{F}}}',
                          "{{bc}} · {{radio}} → {{peer}}")),
                   campo(dec=0, preenchimento=0, largura=2),
                   calcs=["mean", "max"])),
        h=ALT_SERIE + 1)

    L.secao("Disponibilidade dos enlaces")
    L.inteiro(
        linha_tempo("Enlace ativo ao longo do tempo",
                    alvos((f'rajant_peer_ativo{{{F}}}',
                           "{{bc}} · {{radio}} → {{peer}}")),
                    campo(dec=0, mapas=MAPA_SIM_NAO, cor="thresholds",
                          limiares=deg([("text", None), (OK, 1)])),
                    "State.Peer.enabled. Faixa escura = enlace caiu.",
                    mostrar_valor="never"),
        h=ALT_SERIE + 1)

    L.secao("Detalhe por enlace")
    L.inteiro(
        tabela("Enlaces ordenados pelo pior SNR",
               [alvo(f'rajant_peer_snr_db{{{F}}}', "", "A", True, "table"),
                alvo(f'rajant_peer_sinal_dbm{{{F}}}', "", "B", True, "table"),
                alvo(f'rajant_peer_rssi{{{F}}}', "", "C", True, "table"),
                alvo(f'rajant_peer_taxa_mbps{{{F}}}', "", "D", True, "table"),
                alvo(f'rajant_peer_custo{{{F}}}', "", "E", True, "table"),
                alvo(f'rajant_peer_ativo{{{F}}}', "", "F", True, "table")],
               campo(dec=1),
               "Peer identificado por MAC quando o BC não publica "
               "ipv4Address — o campo é `optional` em State.Peer, e exigi-lo "
               "descartava o enlace inteiro.",
               transf=juntar("peer", 6,
                             {"bc 1": "BreadCrumb", "ip 1": "IP",
                              "radio 1": "rádio", "peer": "peer",
                              "Value #A": "SNR (dB)",
                              "Value #B": "sinal (dBm)",
                              "Value #C": "RSSI",
                              "Value #D": "taxa (Mbps)",
                              "Value #E": "custo",
                              "Value #F": "ativo"},
                             manter=["ip", "bc", "radio"]),
               ordenar=("SNR (dB)", False),
               larguras={"IP": 130, "rádio": 90, "ativo": 80}),
        h=ALT_TABELA + 1)

    return dashboard(
        "rajant-enlaces", "Rajant · 3 · Enlaces da Malha", L,
        [var_ds(),
         var_query("bc", "BreadCrumb", "label_values(rajant_peer_snr_db, bc)"),
         var_query("radio", "Rádio",
                   "label_values(rajant_peer_snr_db, radio)")],
        "Um enlace rádio↔peer por série. SNR corrigido para signal - noise.")


# ══════════════════════════════════════════════════════════════════
# 4. ETHERNET
# ══════════════════════════════════════════════════════════════════
def dash_ethernet():
    _id[0] = 0
    F = f'{F_BC}, porta=~"$porta"'
    L = Layout()

    L.secao("Indicadores das portas")
    L.fila(
        (4, cartao("Portas monitoradas",
                   alvos((f'count(rajant_eth_link{{{F}}})', "portas")),
                   campo(dec=0, cor="fixed", preenchimento=0),
                   grafico="none")),
        (4, cartao("Portas com link",
                   alvos((f'sum(rajant_eth_link{{{F}}})', "up")),
                   campo(dec=0, cor="thresholds",
                         limiares=deg([(CRIT, None), (OK, 1)])),
                   "Inferido de aptState + presença de peer — o campo "
                   "`linkup` que o parser procurava não existe.")),
        (4, cartao("Portas APT Master",
                   alvos((f'count(rajant_eth_apt_state{{{F}}} == 0) '
                          f'or vector(0)', "master")),
                   campo(dec=0, cor="fixed"),
                   "aptState == 0 (APT_STATE_MASTER) na numeração oficial.")),
        (4, cartao("Peers em portas APT",
                   alvos((f'sum(rajant_eth_peers{{{F}}})', "peers")),
                   campo(dec=0, cor="fixed"),
                   "rajant_eth_peers tinha Gauge no exportador mas nunca era "
                   "publicada — a auditoria ligou a série.")),
        (4, cartao("Throughput RX",
                   alvos((f'sum(rajant_eth_rx_mbps{{{F}}})', "rx")),
                   campo("Mbits", 1, cor="fixed"))),
        (4, cartao("Throughput TX",
                   alvos((f'sum(rajant_eth_tx_mbps{{{F}}})', "tx")),
                   campo("Mbits", 1, cor="fixed"))),
        h=ALT_KPI)

    L.secao("Estado das portas")
    L.inteiro(
        linha_tempo("APT state por porta",
                    alvos((f'rajant_eth_apt_state{{{F}}}',
                           "{{bc}} · {{porta}}")),
                    campo(dec=0, mapas=MAPA_APT, cor="thresholds",
                          limiares=deg([(OK, None), (NEUTRO, 1),
                                        ("text", 2), (ATEN, 3)])),
                    "Numeração oficial do enum: 0=MASTER, 1=SLAVE, 2=NONE, "
                    "3=LINK. O exportador antigo trocava LINK e NONE."),
        h=ALT_SERIE + 1)

    L.fila(
        (12, linha_tempo("Link por porta",
                         alvos((f'rajant_eth_link{{{F}}}',
                                "{{bc}} · {{porta}}")),
                         campo(dec=0, mapas=MAPA_LINK, cor="thresholds",
                               limiares=deg([(CRIT, None), (OK, 1)])),
                         mostrar_valor="never")),
        (12, serie("Peers por porta",
                   alvos((f'rajant_eth_peers{{{F}}}', "{{bc}} · {{porta}}")),
                   campo(dec=0, preenchimento=15),
                   calcs=["mean", "max"])),
        h=ALT_SERIE)

    L.secao("Tráfego")
    L.fila(
        (12, serie("Throughput por porta",
                   alvos((f'rajant_eth_rx_mbps{{{F}}}',
                          "{{bc}} · {{porta}} RX"),
                         (f'rajant_eth_tx_mbps{{{F}}}',
                          "{{bc}} · {{porta}} TX")),
                   campo("Mbits", 2, preenchimento=15),
                   calcs=["mean", "max"])),
        (12, serie("Pacotes por segundo",
                   alvos((f'rate(rajant_eth_rx_pacotes{{{F}}}[5m])',
                          "{{bc}} · {{porta}} RX"),
                         (f'rate(rajant_eth_tx_pacotes{{{F}}}[5m])',
                          "{{bc}} · {{porta}} TX")),
                   campo("pps", 1, preenchimento=8),
                   "rate() sobre os contadores de CommStats.",
                   calcs=["mean"])),
        h=ALT_SERIE)

    L.secao("Detalhe por porta")
    L.inteiro(
        tabela("Portas — visão instantânea",
               [alvo(f'rajant_eth_apt_state{{{F}}}', "", "A", True, "table"),
                alvo(f'rajant_eth_link{{{F}}}', "", "B", True, "table"),
                alvo(f'rajant_eth_peers{{{F}}}', "", "C", True, "table"),
                alvo(f'rajant_eth_rx_mbps{{{F}}}', "", "D", True, "table"),
                alvo(f'rajant_eth_tx_mbps{{{F}}}', "", "E", True, "table")],
               campo(dec=2, mapas=MAPA_APT),
               transf=juntar("porta", 5,
                             {"bc 1": "BreadCrumb", "ip 1": "IP",
                              "porta": "porta",
                              "Value #A": "APT", "Value #B": "link",
                              "Value #C": "peers",
                              "Value #D": "RX (Mbps)",
                              "Value #E": "TX (Mbps)"},
                             manter=["ip", "bc"]),
               ordenar=("BreadCrumb", False),
               larguras={"IP": 130, "porta": 90, "APT": 100, "link": 80}),
        h=ALT_TABELA)

    L.secao("Contadores brutos", recolhida=True)
    L.inteiro(
        serie("Bytes acumulados por porta",
              alvos((f'rajant_eth_rx_bytes{{{F}}}',
                     "{{bc}} · {{porta}} RX"),
                    (f'rajant_eth_tx_bytes{{{F}}}',
                     "{{bc}} · {{porta}} TX")),
              campo("bytes", 0, preenchimento=0),
              "State.Wired.stats — os quatro únicos contadores que CommStats "
              "oferece são estes bytes e os pacotes. Não há erro, CRC nem "
              "descarte no protocolo.",
              calcs=["last"]),
        h=ALT_SERIE)

    L.secao("Limitações do protocolo")
    L.inteiro(texto("Leia antes de cobrar erro de porta", AVISO_APT), h=13)

    return dashboard(
        "rajant-ethernet", "Rajant · 4 · Portas Ethernet", L,
        [var_ds(),
         var_query("bc", "BreadCrumb", "label_values(rajant_eth_link, bc)"),
         var_query("porta", "Porta", "label_values(rajant_eth_link, porta)")],
        "Portas wired. Enum APT alinhado ao State.proto; sem contadores de "
        "erro, que não existem na BC API.")


# ══════════════════════════════════════════════════════════════════
# 5. SAÚDE
# ══════════════════════════════════════════════════════════════════
def dash_saude():
    _id[0] = 0
    L = Layout()

    L.secao("Indicadores de saúde")
    L.fila(
        (4, medidor("CPU média",
                    alvos((f'avg(rajant_cpu_load_pct{{{F_BC}}})', "cpu")),
                    campo("percent", 1, 0, 100, cor="thresholds",
                          limiares=deg([(OK, None), (ATEN, 60), (CRIT, 85)])),
                    "Derivada de System.idle — ver a nota no fim do "
                    "dashboard. Sem valor = não deu para medir.")),
        (4, medidor("Temperatura máxima",
                    alvos((f'max(rajant_temperatura_c{{{F_BC}}})', "temp")),
                    campo("celsius", 1, 0, 100, cor="thresholds",
                          limiares=deg([(OK, None), (ATEN, 65), (CRIT, 75)])))),
        (4, cartao("Memória livre mínima",
                   alvos((f'min(rajant_memoria_livre_kb{{{F_BC}}}) * 1024',
                          "mem")),
                   campo("bytes", 0, cor="thresholds",
                         limiares=deg([(CRIT, None), (ATEN, 20000000),
                                       (OK, 50000000)])))),
        (4, medidor("Bateria mínima",
                    alvos((f'min(rajant_bateria_pct{{{F_BC}}})', "bat")),
                    campo("percent", 0, 0, 100, cor="thresholds",
                          limiares=deg([(CRIT, None), (ATEN, 30), (OK, 60)])),
                    "State.Battery.capacityPercent. Vazio em modelos sem "
                    "bateria — é esperado.")),
        (4, cartao("Uptime mínimo",
                   alvos((f'min(rajant_uptime_s{{{F_BC}}})', "uptime")),
                   campo("s", 0, cor="fixed"),
                   "Escala não confirmada: o exportador assume ms em "
                   "System.uptime. Confirme com --dump-state.")),
        (4, cartao("Perda InstaMesh",
                   alvos((f'avg(rajant_im_perda_pct{{{F_BC}}})', "perda")),
                   campo("percent", 2, cor="thresholds",
                         limiares=deg([(OK, None), (ATEN, 1), (CRIT, 5)])))),
        h=ALT_KPI)

    L.secao("Processamento, memória e temperatura")
    L.fila(
        (12, serie("CPU por BC",
                   alvos((f'rajant_cpu_load_pct{{{F_BC}}}', "{{bc}}")),
                   campo("percent", 1, 0, 100, preenchimento=12),
                   "Buraco na série = não deu para derivar (1ª coleta, "
                   "reboot, ou idle por núcleo). Não é zero.",
                   calcs=["mean", "max"])),
        (12, serie("Temperatura por BC",
                   alvos((f'rajant_temperatura_c{{{F_BC}}}', "{{bc}}")),
                   campo("celsius", 1, preenchimento=12),
                   calcs=["mean", "max"])),
        h=ALT_SERIE)

    L.fila(
        (12, serie("Memória livre",
                   alvos((f'rajant_memoria_livre_kb{{{F_BC}}} * 1024',
                          "{{bc}}")),
                   campo("bytes", 0, preenchimento=12),
                   calcs=["mean", "min"])),
        (12, serie("Latência ICMP",
                   alvos((f'rajant_ping_rtt_ms{{{F_BC}}}', "{{bc}}")),
                   campo("ms", 1, preenchimento=8),
                   calcs=["mean", "max"])),
        h=ALT_SERIE)

    L.fila(
        (12, barras("BCs mais quentes",
                    alvos((f'topk(10, rajant_temperatura_c{{{F_BC}}})',
                           "{{bc}}")),
                    campo("celsius", 1, 0, 100, cor="thresholds",
                          limiares=deg([(OK, None), (ATEN, 65), (CRIT, 75)])))),
        (12, barras("BCs com maior CPU",
                    alvos((f'topk(10, rajant_cpu_load_pct{{{F_BC}}})',
                           "{{bc}}")),
                    campo("percent", 1, 0, 100, cor="thresholds",
                          limiares=deg([(OK, None), (ATEN, 60), (CRIT, 85)])))),
        h=ALT_SERIE + 1)

    L.secao("Bateria (State.Battery)")
    L.fila(
        (8, serie("Carga",
                  alvos((f'rajant_bateria_pct{{{F_BC}}}', "{{bc}}")),
                  campo("percent", 0, 0, 100, preenchimento=15),
                  "capacityPercent. É carga, não tensão — a métrica de volts "
                  "foi removida porque não existe na BC API.",
                  calcs=["min"])),
        (8, serie("Corrente",
                  alvos((f'rajant_bateria_ma{{{F_BC}}}', "{{bc}}")),
                  campo("mamp", 0, preenchimento=8), calcs=["mean"])),
        (8, serie("Temperatura da bateria",
                  alvos((f'rajant_bateria_temp_c{{{F_BC}}}', "{{bc}}")),
                  campo("celsius", 0, preenchimento=8), calcs=["max"])),
        h=7)

    L.fila(
        (8, serie("Autonomia estimada",
                  alvos((f'rajant_bateria_autonomia_min{{{F_BC}}}', "{{bc}}")),
                  campo("m", 0, preenchimento=8),
                  "dischargeTimeMinutes.", calcs=["min"])),
        (8, serie("Tempo até recarregar",
                  alvos((f'rajant_bateria_recarga_min{{{F_BC}}}', "{{bc}}")),
                  campo("m", 0, preenchimento=8),
                  "chargeTimeMinutes.", calcs=["min"])),
        (8, linha_tempo("Carregando",
                        alvos((f'rajant_bateria_carregando{{{F_BC}}}',
                               "{{bc}}")),
                        campo(dec=0,
                              mapas=mapa([(0, "descarregando", ATEN),
                                          (1, "carregando", OK)]),
                              cor="thresholds",
                              limiares=deg([(ATEN, None), (OK, 1)])),
                        mostrar_valor="never")),
        h=7)

    L.secao("InstaMesh")
    L.fila(
        (12, serie("Pacotes por segundo",
                   alvos((f'rajant_im_tx_pps{{{F_BC}}}', "{{bc}} TX"),
                         (f'rajant_im_rx_pps{{{F_BC}}}', "{{bc}} RX"),
                         (f'rajant_im_drop_pps{{{F_BC}}}', "{{bc}} drop")),
                   campo("pps", 1, preenchimento=10), calcs=["mean"])),
        (12, serie("Descartes e floods",
                   alvos((f'rate(rajant_im_pkt_drop{{{F_BC}}}[5m])',
                          "{{bc}} drop"),
                         (f'rate(rajant_im_floods_drop{{{F_BC}}}[5m])',
                          "{{bc}} floods"),
                         (f'rate(rajant_im_source_floods_drop{{{F_BC}}}[5m])',
                          "{{bc}} source floods")),
                   campo("pps", 2, preenchimento=10),
                   "rajant_im_source_floods_drop (sourceFloodsDropped) é série "
                   "nova — substitui os contadores inventados unicastDropped / "
                   "overflows / undeliverables.",
                   calcs=["mean"])),
        h=ALT_SERIE)

    L.fila(
        (12, serie("Multicast e ARP",
                   alvos((f'rate(rajant_im_pkt_multicast{{{F_BC}}}[5m])',
                          "{{bc}} multicast"),
                         (f'rajant_im_arp_pps{{{F_BC}}}', "{{bc}} ARP/s")),
                   campo("pps", 2, preenchimento=10),
                   "rajant_im_pkt_multicast (packetsMulticast) existia no "
                   "protocolo e não era aproveitado.",
                   calcs=["mean"])),
        (12, serie("Descobertas de rota",
                   alvos((f'rate(rajant_im_disc_sourced{{{F_BC}}}[5m])',
                          "{{bc}} originadas"),
                         (f'rate(rajant_im_disc_passed{{{F_BC}}}[5m])',
                          "{{bc}} repassadas")),
                   campo("pps", 2, preenchimento=10), calcs=["mean"])),
        h=ALT_SERIE)

    L.secao("Reinicializações e papéis")
    L.fila(
        (12, tabela("Boot counter, reboot e uptime",
                    [alvo(f'rajant_boot_counter{{{F_BC}}}', "", "A", True,
                          "table"),
                     alvo(f'rajant_reboot_needed{{{F_BC}}}', "", "B", True,
                          "table"),
                     alvo(f'rajant_uptime_s{{{F_BC}}}', "", "C", True,
                          "table")],
                    campo(dec=0),
                    "rajant_reboot_needed vem de State.System.reboot.",
                    transf=juntar("bc", 3,
                                  {"bc": "BreadCrumb", "ip 1": "IP",
                                   "Value #A": "boots",
                                   "Value #B": "pede reboot",
                                   "Value #C": "uptime (s)"},
                                  manter=["ip"]),
                    ordenar=("boots", True),
                    larguras={"IP": 130})),
        (12, serie("Bridge ativa e APT master",
                   alvos((f'rajant_bridge_ativa{{{F_BC}}}', "{{bc}} bridge"),
                         (f'rajant_apt_master{{{F_BC}}}',
                          "{{bc}} APT master")),
                   campo(dec=0, min_=0, max_=1, preenchimento=15),
                   "rajant_apt_master é derivada de "
                   "State.Wired.aptState == APT_STATE_MASTER — os campos "
                   "aptMaster/isMaster que o parser procurava não existem.",
                   calcs=["last"])),
        h=ALT_TABELA)

    L.secao("Contadores brutos", recolhida=True)
    L.fila(
        (12, serie("InstaMesh — pacotes acumulados",
                   alvos((f'rajant_im_pkt_tx{{{F_BC}}}', "{{bc}} TX"),
                         (f'rajant_im_pkt_rx{{{F_BC}}}', "{{bc}} RX"),
                         (f'rajant_im_arp_total{{{F_BC}}}', "{{bc}} ARP")),
                   campo(dec=0, preenchimento=0), calcs=["last"])),
        (12, serie("Floods por segundo e classificação térmica",
                   alvos((f'rajant_im_floods_pps{{{F_BC}}}',
                          "{{bc}} floods/s"),
                         (f'rajant_temperatura_classe{{{F_BC}}}',
                          "{{bc}} classe térmica")),
                   campo(dec=1, preenchimento=8),
                   "Classe térmica: 0=fria · 1=normal · 2=morna · 3=quente · "
                   "4=crítica.",
                   calcs=["last"])),
        h=ALT_SERIE)

    L.secao("Notas da auditoria")
    L.inteiro(texto("Como ler CPU, bateria e temperatura", NOTA_SAUDE), h=15)

    return dashboard(
        "rajant-saude", "Rajant · 5 · Saúde dos BreadCrumbs", L,
        [var_ds(), var_query("bc", "BreadCrumb",
                             "label_values(rajant_online, bc)")],
        "Sistema, bateria e InstaMesh. CPU derivada de System.idle; métricas "
        "de tensão removidas por não existirem na BC API.")


# ══════════════════════════════════════════════════════════════════
# 6. OPERAÇÃO DO COLETOR
# ══════════════════════════════════════════════════════════════════
NOTA_COLETOR = """
### Este dashboard mede o **exportador**, não os BreadCrumbs

Serve para responder "o dado que estou vendo é confiável agora?" antes de
concluir qualquer coisa sobre a malha.

- **`rajant_ultima_coleta_ts`** — quanto tempo faz que o BC respondeu. Se a
  idade cresce sem parar, os painéis de rádio/enlace estão mostrando o último
  valor conhecido, não o estado atual.
- **`rajant_falhas_consecutivas`** — tentativas seguidas que falharam. Ao
  atingir `falhas_antes_offline` (padrão 3), o BC é marcado offline.
- **`rajant_ping_ok`** — ICMP responde mas a BCAPI não? Então é
  autenticação/porta 2300, não queda de link.
- **`rajant_bc_primario` / `rajant_bc_interfaces`** — um BreadCrumb com vários
  IPs aparece uma vez por interface. O primário é o de menor IP; contar séries
  sem filtrar por primário infla o total de nós.

### Por que isso ganhou dashboard

Durante a auditoria, `publicar()` estourava `TypeError` ao tentar publicar
`None` em campos que não existem no protocolo. A exceção era engolida pelo
`except Exception` do coletor e o BreadCrumb inteiro virava **offline** — com
metade das séries já escritas. Os painéis abaixo tornam esse tipo de falha
visível: falha de coleta com ICMP respondendo é a assinatura do problema.
"""


def dash_coletor():
    _id[0] = 0
    L = Layout()

    L.secao("Estado do coletor")
    L.fila(
        (4, cartao("IPs no cache",
                   alvos(('rajant_cache_total', "cache")),
                   campo(dec=0, cor="fixed", preenchimento=0),
                   "Cache em disco de IPs descobertos.", grafico="none")),
        (4, cartao("IPs online",
                   alvos(('rajant_cache_online', "online")),
                   campo(dec=0, cor="fixed"))),
        (4, medidor("Cobertura da coleta",
                    alvos(('rajant_cache_online / rajant_cache_total * 100',
                           "cobertura")),
                    campo("percent", 1, 0, 100, cor="thresholds",
                          limiares=deg([(CRIT, None), (ATEN, 90), (OK, 98)])),
                    "Fração do cache que respondeu no último ciclo.")),
        (4, cartao("Seeds OK",
                   alvos(('sum(rajant_seed_status)', "seeds")),
                   campo(dec=0, cor="thresholds",
                         limiares=deg([(CRIT, None), (OK, 1)])),
                   "Seeds de descoberta acessíveis.")),
        (4, cartao("BCs com falha de coleta",
                   alvos((f'count(rajant_falhas_consecutivas{{{F_BC}}} > 0) '
                          f'or vector(0)', "falhas")),
                   campo(dec=0, cor="thresholds",
                         limiares=deg([(OK, None), (ATEN, 1), (CRIT, 5)])))),
        (4, cartao("ICMP sem resposta",
                   alvos((f'count(rajant_ping_ok{{{F_BC}}} == 0) or vector(0)',
                          "sem ping")),
                   campo(dec=0, cor="thresholds",
                         limiares=deg([(OK, None), (ATEN, 1), (CRIT, 5)])))),
        h=ALT_KPI)

    L.secao("Frescor do dado")
    L.fila(
        (12, serie("Idade da última coleta",
                   alvos((f'time() - rajant_ultima_coleta_ts{{{F_BC}}}',
                          "{{bc}}")),
                   campo("s", 0, preenchimento=8),
                   "Cresce em rampa = o BC parou de responder e os painéis "
                   "estão exibindo o último valor conhecido.",
                   calcs=["max"])),
        (12, serie("Falhas consecutivas",
                   alvos((f'rajant_falhas_consecutivas{{{F_BC}}}', "{{bc}}")),
                   campo(dec=0, preenchimento=20, cor="thresholds",
                         limiares=deg([(OK, None), (ATEN, 1), (CRIT, 3)])),
                   "Ao atingir falhas_antes_offline (padrão 3) o BC é "
                   "marcado offline.",
                   calcs=["max"])),
        h=ALT_SERIE)

    L.fila(
        (12, linha_tempo("Online (BCAPI)",
                         alvos((f'rajant_online{{{F_BC}}}', "{{bc}}")),
                         campo(dec=0, mapas=MAPA_LINK, cor="thresholds",
                               limiares=deg([(CRIT, None), (OK, 1)])),
                         "Coleta via BCAPI bem-sucedida.",
                         mostrar_valor="never")),
        (12, linha_tempo("Responde a ICMP",
                         alvos((f'rajant_ping_ok{{{F_BC}}}', "{{bc}}")),
                         campo(dec=0, mapas=MAPA_LINK, cor="thresholds",
                               limiares=deg([(CRIT, None), (OK, 1)])),
                         "ICMP verde + BCAPI vermelho = problema de "
                         "autenticação ou porta 2300, não queda de link.",
                         mostrar_valor="never")),
        h=ALT_SERIE)

    L.secao("Topologia de endereçamento")
    L.fila(
        (10, barras("BCs com mais interfaces",
                    alvos((f'topk(10, rajant_bc_interfaces{{{F_BC}}})',
                           "{{bc}}")),
                    campo(dec=0, cor="continuous-BlPu"),
                    "Quantos IPs pertencem ao mesmo nó físico.")),
        (14, tabela("IP primário por nó",
                    [alvo(f'rajant_bc_primario{{{F_BC}}}', "", "A", True,
                          "table"),
                     alvo(f'rajant_bc_interfaces{{{F_BC}}}', "", "B", True,
                          "table"),
                     alvo(f'rajant_online{{{F_BC}}}', "", "C", True,
                          "table")],
                    campo(dec=0, mapas=MAPA_SIM_NAO),
                    "Um BreadCrumb com vários IPs aparece uma vez por "
                    "interface. Contar séries sem filtrar por primário infla "
                    "o total de nós.",
                    transf=juntar("ip", 3,
                                  {"bc 1": "BreadCrumb", "ip": "IP",
                                   "Value #A": "é primário",
                                   "Value #B": "interfaces",
                                   "Value #C": "online"},
                                  manter=["bc"]),
                    ordenar=("interfaces", True),
                    larguras={"IP": 140, "é primário": 110,
                              "interfaces": 110, "online": 100})),
        h=ALT_TABELA)

    L.secao("Seeds de descoberta")
    L.inteiro(
        linha_tempo("Status dos seeds",
                    alvos(('rajant_seed_status', "{{seed}}")),
                    campo(dec=0, mapas=MAPA_LINK, cor="thresholds",
                          limiares=deg([(CRIT, None), (OK, 1)])),
                    "Seed inacessível impede a redescoberta — a frota "
                    "continua sendo coletada pelo cache em disco, mas BCs "
                    "novos não entram.",
                    mostrar_valor="never"),
        h=6)

    L.secao("Notas")
    L.inteiro(texto("Como usar este dashboard", NOTA_COLETOR), h=13)

    return dashboard(
        "rajant-coletor", "Rajant · 6 · Operação do Coletor", L,
        [var_ds(), var_query("bc", "BreadCrumb",
                             "label_values(rajant_online, bc)")],
        "Saúde do próprio exportador: frescor do dado, falhas de coleta, "
        "seeds e agrupamento de IPs por nó físico.")


# ══════════════════════════════════════════════════════════════════
# 7. SITE SURVEY CONTÍNUO
# ══════════════════════════════════════════════════════════════════
NOTA_SURVEY = """
### A frota móvel é um enxame de sondas

Cada equipamento móvel reporta **posição + qualidade de RF** a cada ciclo de
coleta. Isso é um site survey contínuo, sem campanha dedicada — e só ficou
possível depois que a auditoria contra os `.proto` do bcapi consertou três
coisas que estavam mortas:

| campo | antes | agora |
|---|---|---|
| SNR | `rssi - abs(ruido)` → sempre negativo | `signal - noise`, em dB |
| velocidade | lia `speed`, inexistente → sempre 0 | `gpsSpeedKph`, já em km/h |
| rumo | calculado e **nunca publicado** | `gpsTrackDegreesTrue` |

### ⚠️ Limite de resolução espacial

A distância entre amostras é **velocidade × intervalo de coleta**:

| intervalo | a 25 km/h | a 40 km/h |
|---|---|---|
| 60 s | ~420 m | ~670 m |
| 20 s (`intervalo_moveis_segundos`) | ~140 m | ~220 m |
| 300 s | ~2,1 km | ~3,3 km |

O raio de interpolação padrão do heatmap é **300 m**. Com coleta de 60 s o
espaçamento já é maior que isso, e o mapa sai com buracos que a interpolação
preenche por conta própria.

**Serve para achar buraco de cobertura. Não substitui survey fino de
posicionamento de antena.**

### Vieses a ter em mente

- **Equipamento parado** despeja centenas de amostras na mesma coordenada. O
  gerador do CSV descarta amostras abaixo de `--survey-vel-min` (1 km/h).
- **SNR é por enlace, não por lugar.** O que se mapeia é o *melhor enlace
  disponível* naquele ponto, que é o proxy certo para "qualidade da cobertura
  aqui".
- **Só a frota móvel tem GPS.** BCs fixos não contribuem amostras.

### Gerar os mapas

```bash
python3 rajant_monitor.py --survey-do-prometheus \\
        --survey-periodo 2026-07-01 2026-07-31 \\
        --survey-passo 20 --survey-vel-min 1 \\
        --survey-kmz mina.kmz
```

Sai em `survey_imgs/`: rota, heatmaps de SNR/RSSI/ruído/perda/latência,
histogramas e o mapa de cobertura — o mesmo pipeline da campanha manual.
"""


def dash_survey():
    _id[0] = 0
    F = f'{F_BC}'
    L = Layout()

    L.secao("Cobertura observada pela frota")
    L.fila(
        (4, cartao("Equipamentos com fix",
                   alvos((f'sum(rajant_gps_fix{{{F}}})', "com fix")),
                   campo(dec=0, cor="thresholds",
                         limiares=deg([(CRIT, None), (OK, 1)])),
                   "Só quem tem fix contribui amostra de survey.")),
        (4, cartao("Em movimento agora",
                   alvos((f'count(rajant_gps_vel_kmh{{{F}}} > 1) or vector(0)',
                          "movendo")),
                   campo(dec=0, cor="fixed"),
                   "Amostra de equipamento parado enviesa o mapa e é "
                   "descartada na geração do CSV.")),
        (4, medidor("SNR mínimo em rota",
                    alvos((f'min(rajant_peer_snr_db{{{F}}})', "snr")),
                    campo("none", 1, 0, 60, cor="thresholds",
                          limiares=deg([(CRIT, None), (ATEN, 15), (OK, 30)])),
                    "O pior enlace da frota móvel agora.")),
        (4, cartao("Sem peer (buraco)",
                   alvos((f'count(sum by (bc) '
                          f'(rajant_radio_peers_ativos{{{F}}}) == 0) '
                          f'or vector(0)', "buracos")),
                   campo(dec=0, cor="thresholds",
                         limiares=deg([(OK, None), (ATEN, 1), (CRIT, 3)])),
                   "Equipamento com GPS mas sem nenhum peer ativo = "
                   "candidato a buraco de cobertura.")),
        (4, cartao("Velocidade média",
                   alvos((f'avg(rajant_gps_vel_kmh{{{F}}} > 1)', "km/h")),
                   campo("velocitykmh", 1, cor="fixed"))),
        (4, cartao("Satélites (mínimo)",
                   alvos((f'min(rajant_gps_satelites{{{F}}})', "sats")),
                   campo(dec=0, cor="thresholds",
                         limiares=deg([(CRIT, None), (ATEN, 5), (OK, 7)])),
                   "Poucos satélites = posição imprecisa, amostra ruim.")),
        h=ALT_KPI)

    L.secao("Rota e cobertura")
    L.fila(
        (24, lambda x, y, w, h: _painel(
            "geomap", "Rota da frota móvel — cor pelo SNR do melhor enlace",
            x, y, w, h,
            [alvo(f'rajant_gps_lat{{{F}}}', "{{bc}}", "A", False, "table"),
             alvo(f'rajant_gps_lon{{{F}}}', "{{bc}}", "B", False, "table"),
             alvo(f'max by (bc) (rajant_peer_snr_db{{{F}}})', "{{bc}}", "C",
                  False, "table")],
            campo("none", 1, 0, 60, cor="continuous-RdYlGr"),
            {"view": {"id": "fit", "lat": 0, "lon": 0, "zoom": 4},
             "controls": {"showZoom": True, "showAttribution": True,
                          "mouseWheelZoom": True, "showScale": True,
                          "showMeasure": True, "showDebug": False},
             "basemap": {"type": "default", "name": "Basemap", "config": {}},
             "tooltip": {"mode": "details"},
             # Camada `route` desenha o traçado LIGADO, na ordem temporal —
             # é o que mostra por onde o equipamento passou. A camada de
             # marcadores sozinha vira nuvem de pontos sem sentido de
             # percurso. Os pontos ficam por cima, para dar a leitura fina.
             "layers": [
                 {"type": "route", "name": "Percurso", "tooltip": True,
                  "location": {"mode": "coords", "latitude": "latitude",
                               "longitude": "longitude"},
                  "config": {"style": {
                      "size": {"fixed": 3, "max": 8, "min": 1},
                      "color": {"field": "SNR (dB)", "fixed": OK},
                      "opacity": 0.9, "lineWidth": 3,
                      "rotation": {"fixed": 0, "max": 360, "min": -360,
                                   "mode": "mod"},
                      "symbol": {"mode": "fixed",
                                 "fixed": "img/icons/marker/circle.svg"},
                      "textConfig": {"fontSize": 10, "offsetX": 0,
                                     "offsetY": 0, "textAlign": "center",
                                     "textBaseline": "middle"}},
                      "arrow": 1, "showLegend": True}},
                 {"type": "markers", "name": "Amostras", "tooltip": True,
                  "location": {"mode": "coords", "latitude": "latitude",
                               "longitude": "longitude"},
                  "config": {"style": {
                      "size": {"fixed": 4, "max": 10, "min": 2},
                      "color": {"field": "SNR (dB)", "fixed": OK},
                      "opacity": 0.75, "rotation": {"fixed": 0},
                      "symbol": {"mode": "fixed",
                                 "fixed": "img/icons/marker/circle.svg"},
                      "symbolAlign": {"horizontal": "center",
                                      "vertical": "center"},
                      "textConfig": {"fontSize": 10, "offsetX": 0,
                                     "offsetY": 0, "textAlign": "center",
                                     "textBaseline": "middle"}},
                      "showLegend": False}}]},
            "Traçado ligado na ordem temporal, com setas de sentido. Cada "
            "ponto é uma coleta; a densidade da trilha é velocidade × "
            "intervalo — veja a nota no fim do dashboard antes de tratar "
            "isso como survey fino. Vermelho = SNR baixo naquele trecho, "
            "candidato a reforço de cobertura.",
            juntar("bc", 3,
                   {"Value #A": "latitude", "Value #B": "longitude",
                    "Value #C": "SNR (dB)", "ip 1": "IP"},
                   manter=["ip"]))),
        h=14)

    L.secao("Movimento")
    L.fila(
        (12, serie("Velocidade",
                   alvos((f'rajant_gps_vel_kmh{{{F}}}', "{{bc}}")),
                   campo("velocitykmh", 1, preenchimento=10),
                   "gpsVel.gpsSpeedKph — já vem em km/h. Antes da auditoria "
                   "esta série era 0 constante.",
                   calcs=["mean", "max"])),
        (12, serie("Rumo verdadeiro",
                   alvos((f'rajant_gps_rumo_graus{{{F}}}', "{{bc}}")),
                   campo("degree", 0, 0, 360, preenchimento=0, largura=2),
                   "gpsVel.gpsTrackDegreesTrue. Mesmo ponto com rumos "
                   "diferentes e SNR diferente indica sombreamento pela "
                   "própria carroceria ou orientação de antena.",
                   calcs=["last"])),
        h=ALT_SERIE)

    L.secao("Células de cobertura e handover")
    L.fila(
        (14, linha_tempo("Qual BreadCrumb está servindo cada equipamento",
                         alvos((f'label_replace('
                                f'topk by (bc) (1, rajant_peer_snr_db{{{F}}}),'
                                f' "servidor", "$1", "peer", "(.*)")',
                                "{{bc}} → {{servidor}}")),
                         campo(dec=0, cor="palette-classic"),
                         "O `peer` do melhor enlace é o BC que cobre o ponto. "
                         "Cada troca de faixa é um handover — trocas seguidas "
                         "no mesmo trecho indicam fronteira de célula instável.",
                         mostrar_valor="never")),
        (10, tabela("Enlace ativo por equipamento",
                    [alvo(f'topk by (bc) (1, rajant_peer_snr_db{{{F}}})', "",
                          "A", True, "table")],
                    campo(dec=1, cor="thresholds",
                          limiares=deg([(CRIT, None), (ATEN, 15), (OK, 30)])),
                    "Quem serve quem, agora. É o mapa de células em forma de "
                    "tabela — a coluna `servidor` do CSV do survey sai daqui.",
                    transf=[{"id": "organize", "options": {
                        "excludeByName": limpar(1),
                        "renameByName": {"bc": "Equipamento",
                                         "peer": "Servido por",
                                         "radio": "rádio",
                                         "ip": "IP",
                                         "Value": "SNR (dB)"}}}],
                    ordenar=("SNR (dB)", False),
                    larguras={"IP": 120, "rádio": 80, "SNR (dB)": 100})),
        h=ALT_SERIE + 1)

    L.secao("Qualidade em rota")
    L.fila(
        (12, serie("SNR do melhor enlace por equipamento",
                   alvos((f'max by (bc) (rajant_peer_snr_db{{{F}}})',
                          "{{bc}}")),
                   campo("none", 1, preenchimento=10, cor="thresholds",
                         limiares=deg([(CRIT, None), (ATEN, 15), (OK, 30)])),
                   "É o que o heatmap de survey mapeia: a qualidade da "
                   "melhor cobertura disponível naquele ponto.",
                   calcs=["mean", "min"])),
        (12, serie("Peers ativos em rota",
                   alvos((f'sum by (bc) (rajant_radio_peers_ativos{{{F}}})',
                          "{{bc}}")),
                   campo(dec=0, preenchimento=15, cor="thresholds",
                         limiares=deg([(CRIT, None), (ATEN, 1), (OK, 2)])),
                   "Cair a zero com o equipamento em movimento é a "
                   "assinatura de buraco de cobertura.",
                   calcs=["min"])),
        h=ALT_SERIE)

    L.secao("Candidatos a buraco de cobertura")
    L.inteiro(
        tabela("Equipamentos móveis com pior cobertura agora",
               [alvo(f'max by (bc) (rajant_peer_snr_db{{{F}}})', "", "A",
                     True, "table"),
                alvo(f'sum by (bc) (rajant_radio_peers_ativos{{{F}}})', "",
                     "B", True, "table"),
                alvo(f'rajant_gps_vel_kmh{{{F}}}', "", "C", True, "table"),
                alvo(f'rajant_gps_lat{{{F}}}', "", "D", True, "table"),
                alvo(f'rajant_gps_lon{{{F}}}', "", "E", True, "table"),
                alvo(f'rajant_gps_satelites{{{F}}}', "", "F", True, "table")],
               campo(dec=4),
               "Ordenado pelo pior SNR. Copie lat/lon para o Google Earth "
               "junto com o KMZ da mina para localizar o ponto.",
               transf=juntar("bc", 6,
                             {"bc": "Equipamento",
                              "Value #A": "SNR (dB)",
                              "Value #B": "peers ativos",
                              "Value #C": "vel (km/h)",
                              "Value #D": "latitude",
                              "Value #E": "longitude",
                              "Value #F": "satélites"},
                             manter=["ip"],
                             extras={"ip 1": True}),
               ordenar=("SNR (dB)", False),
               larguras={"Equipamento": 180, "SNR (dB)": 100,
                         "peers ativos": 110, "vel (km/h)": 100}),
        h=ALT_TABELA + 1)

    L.secao("Como usar e o que não esperar")
    L.inteiro(texto("Site survey contínuo — método e limites", NOTA_SURVEY),
              h=20)

    return dashboard(
        "rajant-survey", "Rajant · 7 · Site Survey Contínuo", L,
        [var_ds(), var_query("bc", "Equipamento",
                             "label_values(rajant_gps_lat, bc)")],
        "Cobertura observada pela frota móvel em movimento. Base do "
        "--survey-do-prometheus.",
        janela="now-24h")


# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    dashes = {
        "rajant-visao-geral.json": dash_visao_geral,
        "rajant-rf.json":          dash_rf,
        "rajant-enlaces.json":     dash_enlaces,
        "rajant-ethernet.json":    dash_ethernet,
        "rajant-saude.json":       dash_saude,
        "rajant-coletor.json":     dash_coletor,
        "rajant-survey.json":      dash_survey,
    }
    for nome, fn in dashes.items():
        d = fn()
        (SAIDA / nome).write_text(
            json.dumps(d, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        n = len([x for x in d["panels"] if x["type"] != "row"])
        s = len([x for x in d["panels"] if x["type"] == "row"])
        print(f"  {nome:<28} {n:>2} painéis · {s} seções · uid={d['uid']}")
