# Dashboards Grafana — Rajant

Seis dashboards alinhados ao `rajant_monitor.py` **depois** da auditoria das
métricas contra os `.proto` do bcapi. Consultam 99 das 101 métricas que o
exporter publica hoje, e nenhuma das 19 removidas.

| # | arquivo | uid | painéis | do que trata |
|---|---|---|---|---|
| 1 | `rajant-visao-geral.json` | `rajant-visao-geral` | 17 | disponibilidade, mesh score, inventário, mapa GPS |
| 2 | `rajant-rf.json` | `rajant-rf` | 22 | ruído, ocupação de canal, TX power, clientes por SSID |
| 3 | `rajant-enlaces.json` | `rajant-enlaces` | 14 | SNR, sinal, taxa e custo InstaMesh por enlace |
| 4 | `rajant-ethernet.json` | `rajant-ethernet` | 14 | APT state, link, throughput e peers por porta |
| 5 | `rajant-saude.json` | `rajant-saude` | 27 | CPU, temperatura, memória, bateria, InstaMesh |
| 6 | `rajant-coletor.json` | `rajant-coletor` | 15 | saúde do próprio exporter: frescor, falhas, seeds |

## Importar

**Pela interface:** *Dashboards → New → Import → Upload JSON file*. Cada
dashboard pede a fonte de dados Prometheus na importação (variável
`DS_PROMETHEUS`).

**Por provisionamento**, copie os JSON e o `provisioning-dashboards.yaml`:

```bash
sudo mkdir -p /var/lib/grafana/dashboards/rajant
sudo cp grafana/rajant-*.json /var/lib/grafana/dashboards/rajant/
sudo cp grafana/provisioning-dashboards.yaml \
        /etc/grafana/provisioning/dashboards/rajant.yaml
sudo systemctl restart grafana-server
```

No Windows, a pasta de provisionamento é `<grafana>\conf\provisioning\dashboards\`;
copie o `.yaml` para lá e troque `path` pela pasta onde ficaram os JSON (ex.:
`C:/grafana/dashboards/rajant`).

O dashboard *7 · Site Survey Contínuo* saiu junto com o survey. Quem o tiver
importado pode apagá-lo pela interface; por provisionamento, apague o
`rajant-survey.json` da pasta.

No provisionamento, `DS_PROMETHEUS` resolve pela fonte de dados padrão. Se a
sua não for a padrão, ajuste `DS_PROMETHEUS` no seletor do topo de cada
dashboard.

## Variáveis

Todos têm `BreadCrumb` (`$bc`), multi-seleção com *All*. Conforme o dashboard,
também há `$radio`, `$freq` e `$porta`. Todas as consultas já vêm filtradas por
elas, então dá para isolar um BC ou uma banda sem editar painel.

## ⚠️ Antes de reaproveitar painel antigo

**`rajant_eth_apt_state` mudou de numeração.** O exporter antigo publicava
`LINK=2, NONE=3`; o enum de `State.proto` é o inverso:

```
APT_STATE_MASTER = 0    APT_STATE_SLAVE = 1
APT_STATE_NONE   = 2    APT_STATE_LINK  = 3
```

Estes dashboards já usam a numeração oficial. Painel antigo que traduza 2/3
está mostrando LINK onde é NONE e vice-versa.

**Estas séries não existem mais** — painel que as consulte fica vazio, e isso
é o comportamento correto, não falha de coleta:

`rajant_voltagem_v` · `rajant_voltagem_min_v` · `rajant_voltagem_max_v` ·
`rajant_bateria_v` · `rajant_session_state` · `rajant_im_unicast_drop` ·
`rajant_im_overflows` · `rajant_im_undeliv_rx` · `rajant_im_undeliv_tx` ·
`rajant_radio_radar_detec` · `rajant_radio_radar_pulsos` ·
`rajant_radio_phy_erros` · `rajant_peer_txpower_dbm` · `rajant_eth_rx_erros` ·
`rajant_eth_tx_erros` · `rajant_eth_rx_crc` · `rajant_eth_rx_drop` ·
`rajant_eth_tx_drop` · `rajant_eth_mudancas`

O motivo de cada remoção está em `../AUDITORIA_METRICAS.md`.

## Como ler os painéis que mudaram de natureza

- **CPU** é derivada de `System.idle` (não existe `cpuLoad` no protocolo).
  Precisa de duas coletas, é média do intervalo e **buraco na série significa
  "não deu para medir", nunca "CPU em zero"**.
- **Bateria em %** substituiu as métricas de tensão. Carga não é tensão, e
  modelos sem bateria simplesmente não publicam a série.
- **Link de ethernet** é inferido de `aptState` + peers — o campo `linkup` que
  o parser procurava não existe.
- **SNR** agora é `signal - noise`. A conta antiga (`rssi - abs(noise)`) dava
  sempre negativo e zerava `rajant_radio_good_peers` na rede inteira.
- **Tempo de canal** (`rajant_radio_ch_*_ms`): o sufixo `_ms` é suposição — o
  `.proto` diz só `uint64`. As porcentagens derivadas são razões, então não
  dependem da unidade.

## Buraco, não linha reta

Rádio que para de responder **sai** do `/metrics` na coleta seguinte (as
medições; `rajant_online` vai a 0), e vizinho que some da tabela de peers
também. O Prometheus encerra a série e o painel mostra o **buraco**. Antes o
exporter repetia a última leitura e o gráfico virava uma linha reta.

Os painéis de série ligam buracos de até **3 minutos** (`spanNulls`), para
uma raspagem perdida não quebrar a linha; buraco maior aparece. O valor fica
em `LIGA_BURACO_MS` no gerador.

O buraco **não** é pintado de 0: 0 dBm ou 0 °C seriam leituras, e erradas. A
queda está em `rajant_online` e em *Falhas consecutivas* (dashboard 6).

## Só equipamento com tag

O exporter só publica rádio com tag no nome (CA-1035, ERM-22 PTP CAM,
RED065...), e o rótulo `peer` das séries de enlace é a **tag** do vizinho,
não o IP. Por isso as variáveis e legendas mostram só equipamento da frota.

Séries antigas, com IP no rótulo, continuam no histórico do Prometheus até
saírem do intervalo de tempo escolhido no painel. O cartão *Ignorados sem
tag* (dashboard 6) conta quem respondeu e foi deixado de fora; os nomes estão
no log do exporter.

## Regenerar

Os JSON são **gerados**, não editados à mão:

```bash
python3 grafana/gerar_dashboards.py     # escreve os 6 JSON
python3 grafana/validar_dashboards.py   # confere contra o exporter
```

O validador falha se algum painel consultar métrica removida ou inexistente,
se houver painéis sobrepostos na grade ou IDs repetidos. Ele também lista as
métricas publicadas que ficaram sem painel — hoje, `rajant_gps_qualidade` e
`rajant_radio_interf_pct`.

Editar pela interface do Grafana funciona, mas o próximo `gerar_dashboards.py`
sobrescreve. Para manter uma mudança, faça-a no gerador.
