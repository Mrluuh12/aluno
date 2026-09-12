# Auditoria das métricas do `rajant_monitor.py` contra a BC API

Fonte da verdade: `bcapi-ref/proto/` (`State.proto`, `Gps.proto`, `Common.proto`,
`Hardware.proto`, `Config.proto`). Unidades conferidas contra
`bcapi-master/src/bc_livestats.py`.

**Placar:** 103 métricas auditadas → **19 removidas**, **14 acrescentadas**,
**98 publicadas hoje**. 58 testes em `teste_parser.py` (32 deles falham no
código antigo).

---

## 0. O bug que derrubava a coleta inteira

Antes da auditoria, `parse_state()` já marcava alguns campos inexistentes como
`None` (radar, PHY, erros de ethernet). Mas `publicar()` continuava chamando
`.set(None)`, e `prometheus_client` faz `float(valor)` — o que levanta
`TypeError`. Como `_coletar_bc()` embrulha tudo num `except Exception`, o
resultado era:

1. `publicar()` abortava no meio, com metade das séries já escritas;
2. as 3 tentativas falhavam igual;
3. o BreadCrumb era marcado **OFFLINE**.

Ou seja: a correção parcial anterior não deixava métricas zeradas — deixava a
frota inteira aparecendo como fora do ar. Agora existe o helper `pub()`, que
simplesmente não publica quando o valor é `None`.

---

## 1. Sistema / BreadCrumb

| métrica | campo no proto | existe? | unidade/escala | status |
|---|---|---|---|---|
| `rajant_online` | — (derivada do coletor) | n/a | 0/1 | ✅ verificado |
| `rajant_uptime_s` | `State.System.uptime` | ✅ | float ÷1000 | ⚠️ escala não confirmada |
| `rajant_temperatura_c` | `State.System.temperature` | ✅ | int32 | 🔧 corrigido — divisor adaptativo |
| `rajant_temperatura_classe` | derivada de `temperature` | ✅ | 0–4 | ✅ verificado |
| `rajant_boot_counter` | `State.System.bootCounter` | ✅ | uint32 | ✅ verificado |
| `rajant_reboot_needed` | `State.System.reboot` | ✅ | bool | ✅ verificado (`rebootRequired` era fallback inútil, mantido inofensivo) |
| `rajant_memoria_livre_kb` | `State.System.freeMemory` | ✅ | int32 ÷1024 | ✅ verificado |
| `rajant_bridge_ativa` | `State.System.bridgeup` | ✅ | bool | ✅ verificado |
| `rajant_cpu_load_pct` | ❌ `cpuLoad`/`cpuUsage`/`cpu` | ❌ | — | 🔧 **derivada** de `System.idle` + `System.uptime` |
| `rajant_apt_master` | ❌ `aptMaster`/`isMaster` | ❌ | — | 🔧 **derivada** de `State.Wired.aptState == APT_STATE_MASTER` |
| `rajant_session_state` | ❌ `sessionState`/`connectionState` | ❌ | — | 🗑️ **REMOVIDA** |
| `rajant_voltagem_v` | ❌ bloco `sensors` | ❌ | — | 🗑️ **REMOVIDA** |
| `rajant_voltagem_min_v` | ❌ bloco `sensors` | ❌ | — | 🗑️ **REMOVIDA** |
| `rajant_voltagem_max_v` | ❌ bloco `sensors` | ❌ | — | 🗑️ **REMOVIDA** |
| `rajant_bateria_v` | ❌ bloco `sensors` | ❌ | — | 🗑️ **REMOVIDA** → substituída por `rajant_bateria_pct` |
| `rajant_bc_info` | `System.platform`, `Build.version`, `Config.General.groups` | ✅ | label-only | 🔧 `nome` agora vem de `configuration.saved.general.name` |
| `rajant_falhas_consecutivas` | — (coletor) | n/a | contador | ✅ verificado |
| `rajant_ultima_coleta_ts` | — (coletor) | n/a | timestamp | ✅ verificado |
| `rajant_disponibilidade_1h_pct` | — (coletor) | n/a | % | ✅ verificado |
| `rajant_disponibilidade_24h_pct` | — (coletor) | n/a | % | ✅ verificado |
| `rajant_disponibilidade_7d_pct` | — (coletor) | n/a | % | ✅ verificado |
| `rajant_ping_rtt_ms` | — (ICMP local) | n/a | ms | ✅ verificado |
| `rajant_ping_ok` | — (ICMP local) | n/a | 0/1 | ✅ verificado |
| `rajant_link_changes` | — (derivada entre coletas) | n/a | contagem | ✅ verificado |
| `rajant_mesh_score` | — (composta) | n/a | 0–100 | ✅ verificado |
| `rajant_bc_primario` | — (agrupamento por IP) | n/a | 0/1 | ✅ verificado |
| `rajant_bc_interfaces` | — (agrupamento por IP) | n/a | contagem | ✅ verificado |
| `rajant_nos_fisicos_total` | — (agrupamento por IP) | n/a | contagem | ✅ verificado |
| `rajant_cache_total` | — (cache local) | n/a | contagem | ✅ verificado |
| `rajant_cache_online` | — (cache local) | n/a | contagem | ✅ verificado |
| `rajant_seed_status` | — (coletor) | n/a | 0/1 | ✅ verificado |

**Novas:**

| métrica | campo no proto | unidade |
|---|---|---|
| `rajant_bateria_pct` | `State.Battery.capacityPercent` | % de carga |
| `rajant_bateria_ma` | `State.Battery.milliamps` | mA |
| `rajant_bateria_carregando` | `State.Battery.charging` | 0/1 |
| `rajant_bateria_temp_c` | `State.Battery.temperatureCelsius` | °C |
| `rajant_bateria_autonomia_min` | `State.Battery.dischargeTimeMinutes` | min |
| `rajant_bateria_recarga_min` | `State.Battery.chargeTimeMinutes` | min |
| `rajant_best_radio_rate` | `AlertSystem.bestRadioRate` | taxa |
| `rajant_alertas_ativos` | contagem de `AlertSystem.alerts` | contagem |

> **Atenção:** capacidade em % **não é tensão**. A aba *Saúde BreadCrumbs* trocou
> as colunas "Volt mín/máx (V)" por "Bateria mín/méd %", e o slide de KPI passou a
> contar BCs com bateria < 30 % em vez de tensão fora de 18–30 V. Modelos sem
> bateria não trazem o bloco → nenhuma série publicada (em vez de uma fila de zeros).

---

## 2. GPS

| métrica | campo no proto | existe? | unidade/escala | status |
|---|---|---|---|---|
| `rajant_gps_lat` | `GPS.gpsPos.gpsLat` | ✅ | NMEA DDMM.mmmm | ✅ verificado |
| `rajant_gps_lon` | `GPS.gpsPos.gpsLong` | ✅ | NMEA DDDMM.mmmm | ✅ verificado |
| `rajant_gps_fix` | derivada de lat/lon + `gpsSwitch.enabled` | ✅ | 0/1 | ✅ verificado |
| `rajant_gps_vel_kmh` | ❌ `speed` → ✅ `GPS.gpsVel.gpsSpeedKph` | ❌→✅ | km/h **direto** | 🔧 corrigido |

O parser lia `speed` (inexistente) e ainda multiplicava por 1,852 supondo nós.
`gpsSpeedKph` já vem em km/h; `gpsSpeedKnots` ficou como fallback com a conversão
correta. Sem nenhum dos dois → `None`, não 0.

**Novas:** `rajant_gps_rumo_graus` (`gpsVel.gpsTrackDegreesTrue`, com
`gpsTrackDegreesMag` de reserva), `rajant_gps_satelites` (`gpsPos.gpsSatsInView`),
`rajant_gps_altitude_m` (`gpsPos.gpsAlt`), `rajant_gps_precisao_h`
(`gpsPos.gpsPrecisionH`).

> O rumo já era calculado no parser antigo (`gps_rumo`) mas **não tinha `Gauge`** —
> era computado e jogado fora.

### `gpsTime` — o campo que faltava para medir no lugar certo

| campo no proto | lido? | onde aparece |
|---|---|---|
| `GPS.gpsPos.gpsTime` | ✅ | `gps_time` (bruto) e `gps_time_s` (segundos) |
| `GPS.gpsPos.gpsQuality` | ✅ | `gps_qual`, com `rajant_gps_qualidade` |
| `GPS.gpsPos.gpsGeoidalSep` | ❌ | sem uso definido — não lido de propósito |

O State devolve a posição que o módulo tem **no momento da consulta**. Se o GPS
atualiza a 1 Hz e se pergunta a 5 Hz, quatro das cinco respostas repetem a mesma
posição. Sem `gpsTime` isso é invisível e vira ponto duplicado no mapa; com ele,
dá para não gravar amostra cuja posição não mudou — e para **medir** o intervalo
de atualização do módulo em vez de supor.

> O `Gps.proto` declara `optional float gpsTime = 1;` e **não diz a unidade**.
> `gps_time_para_segundos()` discrimina entre hora NMEA `hhmmss.ss` e época Unix,
> e devolve `None` no que não encaixa — número sem unidade conhecida não vira
> medida de tempo. O uso principal (*"a posição mudou?"*) compara o valor **bruto**
> e não depende dessa conversão.

### Identidade do vizinho: `encapId`

`State.Peer` **não tem** `name` nem `serialNumber`. Os campos são, no
`State.proto`: `mac, enabled, cost, rate, rssi, signal, age, stats, encapId,
ipv4Address`. O MeshMapper mostra nome e série porque resolve por fora — e a
chave é o `encapId`, que é a **parte numérica do número de série**:

| serial | encapId | nome |
|---|---|---|
| `ES1-2450CS-113187` | `113187` | ERB-11 L1 |
| `FE1-2255B-107805` | `107805` | ERB-02 |

Verificado nos **40 de 40** vizinhos da captura real da ERM-12; há teste que
refaz a conferência sobre `exemplos/meshmapper_repetidora.json`.

O outro lado da chave é `State.Manufacturer.serial` (uint32), agora lido como
`serial_num`. Atenção: `manufacturer` é o campo **190**, um ramo à parte de
`gps`/`wireless`/`system` — `CAMINHOS_ESTADO` não o pede, então numa coleta
filtrada ele vem vazio. Como serial e modelo não mudam, o certo é ler uma vez
por rádio e guardar.

`channel` e `frequency` que o MeshMapper mostra por vizinho vêm do **rádio pai**
(`State.Wireless.channel`), não do peer.

---

## 3. InstaMesh

| métrica | campo no proto | existe? | status |
|---|---|---|---|
| `rajant_im_pkt_tx` | `State.InstaMesh.packetsSent` | ✅ | ✅ verificado |
| `rajant_im_pkt_rx` | `State.InstaMesh.packetsReceived` | ✅ | ✅ verificado |
| `rajant_im_pkt_drop` | `State.InstaMesh.packetsDropped` | ✅ | ✅ verificado |
| `rajant_im_floods_drop` | `State.InstaMesh.floodsDropped` | ✅ | ✅ verificado |
| `rajant_im_arp_total` | `State.InstaMesh.arpTotal` | ✅ | ✅ verificado |
| `rajant_im_disc_sourced` | `State.InstaMesh.discoveriesSourced` | ✅ | ✅ verificado |
| `rajant_im_disc_passed` | `State.InstaMesh.discoveriesPassed` | ✅ | ✅ verificado |
| `rajant_im_tx_pps` / `rx_pps` / `drop_pps` / `floods_pps` / `arp_pps` | derivadas (Δ/Δt) | ✅ | ✅ verificado |
| `rajant_im_perda_pct` | derivada de `packetsDropped`/`packetsSent` | ✅ | ✅ verificado |
| `rajant_im_unicast_drop` | ❌ `unicastDropped` | ❌ | 🗑️ **REMOVIDA** |
| `rajant_im_overflows` | ❌ `overflows` | ❌ | 🗑️ **REMOVIDA** |
| `rajant_im_undeliv_rx` | ❌ `undeliverablesReceived` | ❌ | 🗑️ **REMOVIDA** |
| `rajant_im_undeliv_tx` | ❌ `undeliverableTransmitFailures` | ❌ | 🗑️ **REMOVIDA** |

`State.InstaMesh` tem exatamente 19 campos e nenhum deles é esses quatro.

**Novas:** `rajant_im_source_floods_drop` (`sourceFloodsDropped`),
`rajant_im_pkt_multicast` (`packetsMulticast`) — existiam e não eram lidos.

---

## 4. Rádio (`State.Wireless`)

| métrica | campo no proto | existe? | status |
|---|---|---|---|
| `rajant_radio_canal` | `State.Wireless.channel` | ✅ | ✅ verificado |
| `rajant_radio_ruido_dbm` | `State.Wireless.noise` | ✅ | ✅ verificado |
| `rajant_radio_ruido_classe` | derivada de `noise` | ✅ | ✅ verificado |
| `rajant_radio_txpower_dbm` | `State.Wireless.txpower` | ✅ | ✅ verificado |
| `rajant_radio_rx_bytes` | `State.Wireless.stats.rxBytes` | ✅ | ✅ verificado |
| `rajant_radio_tx_bytes` | `State.Wireless.stats.txBytes` | ✅ | ✅ verificado |
| `rajant_radio_rx_pacotes` | `State.Wireless.stats.rxPackets` | ✅ | ✅ verificado |
| `rajant_radio_tx_pacotes` | `State.Wireless.stats.txPackets` | ✅ | ✅ verificado |
| `rajant_radio_rx_mbps` / `tx_mbps` / `rx_pps` / `tx_pps` | derivadas (Δ/Δt) | ✅ | ✅ verificado |
| `rajant_radio_ch_active_ms` | `State.Wireless.channelActiveTime` | ✅ | ⚠️ unidade não confirmada |
| `rajant_radio_ch_busy_ms` | `State.Wireless.channelBusyTime` | ✅ | ⚠️ unidade não confirmada |
| `rajant_radio_ch_rx_ms` | `State.Wireless.channelReceiveTime` | ✅ | ⚠️ unidade não confirmada |
| `rajant_radio_ch_tx_ms` | `State.Wireless.channelTransmitTime` | ✅ | ⚠️ unidade não confirmada |
| `rajant_radio_busy_pct` / `rx_pct` / `tx_pct` / `idle_pct` | razões entre os 4 acima | ✅ | ✅ verificado (razão é adimensional) |
| `rajant_radio_peers_ativos` | contagem de `Wireless.peer` com `enabled` | ✅ | ✅ verificado |
| `rajant_radio_peers_total` | contagem de `Wireless.peer` | ✅ | ✅ verificado |
| `rajant_radio_good_peers` | derivada (SNR > 30 dB) | ✅ | 🔧 corrigido junto com o SNR |
| `rajant_radio_clientes` | contagem de `Wireless.AP.client` | ✅ | 🔧 **corrigido** |
| `rajant_radio_radar_detec` | ❌ `radarDetections` | ❌ | 🗑️ **REMOVIDA** |
| `rajant_radio_radar_pulsos` | ❌ `pulseEvents` | ❌ | 🗑️ **REMOVIDA** |
| `rajant_radio_phy_erros` | ❌ `rxPhyErrors` | ❌ | 🗑️ **REMOVIDA** |

O sufixo `_ms` das quatro séries de tempo de canal é uma **suposição** (o
`.proto` diz só `uint64`). As porcentagens derivadas delas são razões, então
continuam corretas seja qual for a unidade.

---

## 5. Clientes de AP

| métrica | campo no proto | existe? | status |
|---|---|---|---|
| `rajant_ap_clientes` | contagem de `State.Wireless.AP.client` | ✅ | 🔧 **corrigido** |

Dois erros somados aqui:

1. o parser lia `clientCount`/`clients`, que não existem — o certo é contar os
   blocos `client` (`repeated Client client`);
2. o filtro era `if esid and _b(apb,'enabled')`, mas **`State.Wireless.AP` não tem
   campo `enabled`** → `_b()` devolvia `False` sempre → **nenhum AP entrava na
   lista**. `rajant_ap_clientes` não ficava só zerada: ficava sem nenhuma série.

---

## 6. Peers / enlaces (`State.Peer`)

| métrica | campo no proto | existe? | unidade/escala | status |
|---|---|---|---|---|
| `rajant_peer_sinal_dbm` | `State.Peer.signal` | ✅ | dBm | ✅ verificado |
| `rajant_peer_rssi` | `State.Peer.rssi` | ✅ | relativo | ✅ verificado |
| `rajant_peer_snr_db` | derivada `signal - noise` | ✅ | dB | 🔧 **corrigido** |
| `rajant_peer_taxa_mbps` | `State.Peer.rate` ÷ 10 | ✅ | Mbps | ✅ verificado (`bc_livestats.py`) |
| `rajant_peer_custo` | `State.Peer.cost` | ✅ | custo IM | ✅ verificado |
| `rajant_peer_ativo` | `State.Peer.enabled` | ✅ | 0/1 | ✅ verificado |
| `rajant_first_hop_cost` | menor `Peer.cost` ativo | ✅ | custo IM | ✅ verificado |
| `rajant_peer_txpower_dbm` | ❌ `Peer.txpower` | ❌ | — | 🗑️ **REMOVIDA** |

**SNR:** a conta era `rssi - abs(noise)`, misturando escala relativa com dBm. Com
`rssi=42` e `noise=-95` dava `42-95 = -53 dB`. O correto é `signal - noise`
(`-58 - (-95) = 37 dB`). Isso também derrubava `rajant_radio_good_peers`
(SNR > 30) para zero em toda a rede. Sem ruído no state, cai para `rssi`, que no
Rajant já é medido acima do piso de ruído.

`txpower` só existe em `State.Wireless` (rádio local), já publicado em
`rajant_radio_txpower_dbm`. A potência do outro lado do enlace não trafega.

---

## 7. Ethernet (`State.Wired`)

| métrica | campo no proto | existe? | status |
|---|---|---|---|
| `rajant_eth_rx_bytes` | `State.Wired.stats.rxBytes` | ✅ | ✅ verificado |
| `rajant_eth_tx_bytes` | `State.Wired.stats.txBytes` | ✅ | ✅ verificado |
| `rajant_eth_rx_pacotes` | `State.Wired.stats.rxPackets` | ✅ | ✅ verificado |
| `rajant_eth_tx_pacotes` | `State.Wired.stats.txPackets` | ✅ | ✅ verificado |
| `rajant_eth_rx_mbps` / `tx_mbps` | derivadas (Δ/Δt) | ✅ | ✅ verificado |
| `rajant_eth_apt_state` | `State.Wired.aptState` | ✅ | 🔧 **enum realinhado** |
| `rajant_eth_link` | derivada de `aptState` + peers | ✅ | 🔧 corrigido |
| `rajant_eth_peers` | contagem de `Wired.peer` | ✅ | 🔧 **passou a ser publicada** |
| `rajant_eth_rx_erros` | ❌ `rxErrors` | ❌ | 🗑️ **REMOVIDA** |
| `rajant_eth_tx_erros` | ❌ `txErrors` | ❌ | 🗑️ **REMOVIDA** |
| `rajant_eth_rx_crc` | ❌ `rxCrcErrors` | ❌ | 🗑️ **REMOVIDA** |
| `rajant_eth_rx_drop` | ❌ `rxDroppedPackets` | ❌ | 🗑️ **REMOVIDA** |
| `rajant_eth_tx_drop` | ❌ `txDroppedPackets` | ❌ | 🗑️ **REMOVIDA** |
| `rajant_eth_mudancas` | ❌ `stateChanges` | ❌ | 🗑️ **REMOVIDA** |

`State.Wired.stats` é do tipo `CommStats`, que tem **exatamente quatro campos**:
`rxBytes`, `rxPackets`, `txBytes`, `txPackets`. Não há erro, CRC, descarte nem
contagem de mudanças de link em lugar nenhum do protocolo.

### ⚠️ Mudança de semântica em `rajant_eth_apt_state`

O `_APT_MAP` antigo usava `link=2, none=3`. O enum oficial é o contrário:

```
APT_STATE_MASTER = 0;  APT_STATE_SLAVE = 1;
APT_STATE_NONE   = 2;  APT_STATE_LINK  = 3;
```

A métrica agora publica o valor do enum oficial. **Dashboards que traduziam 2/3
precisam ser reajustados.** `rajant_eth_peers` também existia como `Gauge` mas
nunca era publicada — agora é.

---

## 8. O que **não é obtenível** pela BC API

Para o relatório parar de prometer estes números:

| o que se pedia | veredito |
|---|---|
| **Tensão de entrada / bateria em volts** | Não existe bloco de sensores em nenhum `.proto`. O que há é `State.Battery`, com carga em **%**, corrente em mA, temperatura em °C e autonomia em minutos. |
| **Erros, CRC e descartes por porta ethernet** | `CommStats` só tem bytes e pacotes. Nenhum contador de erro por interface no protocolo. |
| **Mudanças de estado de link (flaps) por porta** | Não existe `stateChanges`. O exportador aproxima instabilidade por `rajant_link_changes` (peers que mudaram entre coletas). |
| **Detecções de radar / DFS e eventos de pulso** | Nenhum campo de DFS em `State.Wireless`. |
| **Erros de PHY do rádio** | Não existe `rxPhyErrors`. |
| **Potência de TX do peer remoto** | `txpower` só existe no rádio local. |
| **Carga de CPU instantânea** | Não existe `cpuLoad`. Só dá para derivar de `System.idle` entre duas coletas — é média do intervalo, não pico. |
| **Estado de sessão do BC** | `sessionState`/`connectionState` não existem. `State.AdminSession` descreve sessões administrativas *conectadas ao* BreadCrumb, que é outra coisa. |
| **Contadores unicast/overflow/undeliverable do InstaMesh** | Não constam dos 19 campos de `State.InstaMesh`. |

---

## 9. Pendências que exigem um BreadCrumb real (`--dump-state`)

Três escalas não dá para fechar sem um equipamento da frota. Escolhi, em cada
caso, a interpretação que não produz número absurdo — mas elas continuam
**não confirmadas**:

1. **`System.temperature`** — implementei divisor adaptativo: `|v| > 200` → centi-grau
   (÷100), senão grau inteiro. Funciona nos dois firmwares, mas confirme qual é o
   seu.
2. **`System.uptime`** — o código divide por 1000 (assume ms). Não mexi, mas se o
   campo estiver em segundos, `rajant_uptime_s` está 1000× menor.
3. **`channelActiveTime` e afins** — o sufixo `_ms` é suposição. As porcentagens
   derivadas não dependem disso.

Um `--dump-state <ip>` de qualquer BC resolve os três de uma vez.

---

## 10. Fallbacks mantidos de propósito

As variantes `snake_case` (`rx_bytes`, `packets_sent`…) e SNMP (`ifInOctets`,
`rxOctets`) não estão nos `.proto` e **foram mantidas**: são defesa contra
firmwares que serializem diferente. Em todos os casos o nome camelCase correto do
protocolo é a primeira opção da lista, então o fallback só entra se o campo
oficial faltar.
