# Site Survey Contínuo

A frota móvel já é um enxame de sondas: cada equipamento reporta **posição +
qualidade de RF** a cada ciclo de coleta. Este documento descreve como
transformar esse histórico em mapas de cobertura sem campanha dedicada.

## Por que só agora

O módulo de survey (heatmaps, histogramas, KMZ, modelo de propagação) já
existia, mas dependia de um CSV produzido manualmente. Três campos que o
survey contínuo precisa estavam mortos até a auditoria contra os `.proto` do
bcapi:

| campo | antes | agora |
|---|---|---|
| SNR | `rssi - abs(ruido)` → sempre negativo | `signal - noise`, em dB |
| velocidade | lia `speed`, que não existe → sempre 0 | `gpsSpeedKph`, já em km/h |
| rumo | calculado e **nunca publicado** (não havia `Gauge`) | `gpsTrackDegreesTrue` |

Sem SNR não há o que mapear. Sem velocidade não dá para descartar equipamento
parado. Sem rumo não dá para investigar sombreamento por orientação.

## Gerar os mapas

```bash
python3 rajant_monitor.py --survey-do-prometheus \
        --survey-periodo 2026-07-01 2026-07-31 \
        --survey-passo 20 \
        --survey-vel-min 1 \
        --survey-kmz mina.kmz
```

| flag | padrão | para que serve |
|---|---|---|
| `--survey-do-prometheus` | — | monta o CSV a partir das métricas coletadas |
| `--survey-periodo INI FIM` | — | obrigatório; `AAAA-MM-DD` ou `'AAAA-MM-DD HH:MM'` |
| `--survey-passo` | 60 s | passo da consulta; use o mesmo do intervalo de coleta |
| `--survey-vel-min` | 1,0 km/h | descarta amostras de equipamento parado |
| `--survey-grade-m` | 0 (desligado) | agrega amostras em células de N metros |
| `--survey-radio` | todos | analisa só um rádio (ex.: `wlan0`) |

### Recortar por turno

O período aceita hora, que é o recorte natural numa mina:

```bash
# turno da manhã de um dia específico
--survey-periodo '2026-07-15 06:00' '2026-07-15 14:00'
```

Só data continua valendo — o fim vai até o último segundo do dia.

### Escolher o rádio

Sem `--survey-radio`, o melhor enlace de cada banda vence, seja qual for o
rádio. Com a flag, só o rádio indicado entra — útil quando o equipamento tem
dois rádios na mesma banda e você quer avaliar um de cada vez. O filtro vale
para SNR, RSSI e ruído.

Saída em `survey_imgs/`: `rota_*.png`, `heatmap_{snr,rssi,ruido,perda,latencia}_*.png`,
`hist_*.png` e, com `--survey-kmz`, o mapa de cobertura da mina. O CSV
intermediário fica em `survey_imgs/survey_prometheus.csv` e pode ser inspecionado.

**Dependências:** `matplotlib` e `numpy` para as imagens; `scipy` é opcional
mas recomendado — sem ele o heatmap cai para nuvem de pontos em vez de
interpolação por área.

## Como o CSV é montado

O schema é exatamente o que `ler_csv_survey` já esperava da campanha manual,
então todo o pipeline de renderização funciona sem alteração:

| coluna | origem |
|---|---|
| `lat` / `lon` | `rajant_gps_lat` / `rajant_gps_lon` |
| `snr` | melhor enlace de `rajant_peer_snr_db` |
| `rssi` | melhor enlace de `rajant_peer_sinal_dbm` |
| `ruido` | `avg by (bc, freq)` de `rajant_radio_ruido_dbm` |
| `perda` | `avg by (bc)` de `rajant_im_perda_pct` |
| `latencia` | `avg by (bc)` de `rajant_ping_rtt_ms` |
| `banda` | label `freq` |
| **`servidor`** | label `peer` do melhor enlace — **qual BC cobria o ponto** |
| **`radio`** | label `radio` do melhor enlace |

`rajant_peer_*` não carrega o label `freq` (os labels são `bc,ip,radio,peer`).
O join que traz a banda é:

```promql
rajant_peer_snr_db
* on (bc, ip, radio) group_left(freq) (rajant_radio_canal * 0 + 1)
```

O `* 0 + 1` puxa o label sem alterar o valor da métrica.

**A escolha do melhor enlace é feita em Python, não com `max by` no PromQL.**
Agregar no servidor descartaria `peer` e `radio` — e é justamente o `peer` que
diz qual BreadCrumb estava cobrindo aquele ponto.

## Células de cobertura e handover

Com a coluna `servidor`, o CSV deixa de ser só "qualidade aqui" e passa a
responder **"quem cobre aqui"**:

- **mapa de células** — agrupando por `servidor`, sai a área real de cobertura
  de cada BreadCrumb, medida em campo e não estimada por modelo de propagação;
- **handover** — toda troca de `servidor` entre amostras consecutivas é uma
  transição de célula;
- **fronteira instável** — trocas repetidas no mesmo trecho (`A → B → A → B`)
  são ping-pong: dois BCs com sinal equivalente disputando o equipamento. É
  candidato a ajuste de potência ou de posicionamento.

Com `--survey-grade-m`, cada célula da grade guarda o servidor **dominante**
(quem serviu mais vezes ali) e `_servidores`, a contagem de BCs distintos que
serviram naquele ponto — valor alto indica região de fronteira.

O dashboard 7 mostra o mesmo ao vivo, na seção *Células de cobertura e
handover*.

Amostras descartadas, com o motivo, aparecem no log: `sem_fix` (gpsSwitch
desligado ou sem fix), `parado` (abaixo de `--survey-vel-min`) e `sem_rf`
(GPS sem métrica de rádio no mesmo instante).

## ⚠️ Limite de resolução espacial

A distância entre amostras é **velocidade × intervalo de coleta**:

| intervalo | a 25 km/h | a 40 km/h |
|---|---|---|
| 60 s (ciclo típico do exporter) | ~420 m | ~670 m |
| **10 s** (captura pela página) | ~70 m | ~110 m |
| 300 s (recomendação BCE p/ redes grandes) | ~2,1 km | ~3,3 km |

Duas fontes, com resoluções diferentes:

- **survey do Prometheus** (`--survey-do-prometheus`) é limitado pelo ciclo do
  exporter — pedir passo menor que o ciclo só repete o mesmo ponto;
- **captura pela página** consulta os rádios direto, no intervalo pedido, e
  não tem esse teto. É o caminho recomendado quando a resolução importa.

O intervalo que de fato ocorreu é gravado em `intervalo_efetivo_s` e aparece no
relatório: se um ciclo estourar o intervalo pedido, o número que vale é o real.

**Isto serve para achar buraco de cobertura. Não substitui survey fino de
posicionamento de antena.** Trate o heatmap como indicação de onde investigar,
não como medida.

## Captura direta pela página

Durante um survey iniciado pela página, cada rádio é consultado pela BC API no
intervalo escolhido, em vez de reaproveitar o cache do exporter.

| cuidado | como é tratado |
|---|---|
| autenticar pesa no BC | uma sessão por rádio, reusada; reautentica só na falha |
| rádio morto trava o ciclo | descartado após `falhas_para_pular` falhas seguidas |
| 150 conexões simultâneas | teto em `max_threads` |
| ciclo mais lento que o intervalo | não acumula fila; grava o intervalo efetivo |
| rádio indisponível num instante | cai para o cache do exporter, amostra marcada `fonte=cache` |

```ini
[survey]
max_threads         = 12
timeout_s           = 6
min_intervalo_s     = 5
falhas_para_pular   = 3
usar_cache_fallback = true
```

O exporter **continua rodando em paralelo** — a carga na malha é a soma dos
dois. A página mostra a carga estimada (`34 rádios a cada 10 s ≈ 3,4
consultas/s`) e avisa em amarelo acima de ~10 consultas/s.

## Coleta acelerada dos móveis

Para melhorar a resolução sem multiplicar o volume da frota inteira, o coletor
aceita um intervalo separado para os equipamentos móveis:

```ini
[coleta]
intervalo_segundos        = 60
intervalo_moveis_segundos = 20     ; 0 = mesmo intervalo para todos
```

A classificação usa `[relatorio] padrao_movel` — a mesma expressão que separa
as categorias nos relatórios, para as duas não divergirem:

```ini
[relatorio]
padrao_movel = ^CA ; ^PA ; ^PF ; ^TT ; ^EH ; CAMINH ; ESCAV ; PERFURA
```

Como funciona: o laço passa a girar em `intervalo_moveis_segundos` e os BCs
fixos entram a cada `passo_fixos` voltas, preservando o intervalo original
deles. Com 60/20, os móveis são coletados 3× mais que os fixos.

Salvaguardas: BC com nome ainda desconhecido conta como móvel (para não ficar
de fora do primeiro ciclo); `padrao_movel` vazio ou regex inválida desliga a
aceleração e registra aviso; intervalo de móveis maior ou igual ao geral
também desliga.

**Custo:** o volume no Prometheus cresce proporcionalmente à fração móvel da
frota. Com 40 móveis de 150 BCs indo de 60 s para 20 s, o acréscimo é de
~53 % das séries desses 40 — não da frota toda.

## Análise: quem serve, margem, zonas

O percentual de aprovação diz **quanto** está ruim; não diz **onde** nem o que
fazer. Três camadas de análise cobrem isso.

**Servidor por amostra.** Cada amostra guarda qual BC atendeu naquele ponto.
Daí saem a cobertura por BC, os **handovers** (troca de servidor entre amostras
consecutivas) e o **ping-pong** (A→B→A: dois BCs disputando a mesma área, que é
problema de projeto, não de rádio). Cadeia A→B→C não conta — é só o veículo
andando.

**Margem, não só passa/reprova.** `-73 dBm` aprova contra `-75` e cai na
primeira chuva ou quando o caminhão vira a carroceria para a antena. A
classificação é **ok / marginal / fora**, com a folga em dB.

**Grade espacial.** A mediana por célula substitui o ponto cru, o que corrige
dois vieses:

- equipamento **parado** despeja dezenas de amostras no mesmo lugar (a fila da
  britagem viraria a média da cava);
- a leitura instantânea varia ±5–10 dB por multipercurso, jogando pontos
  vizinhos para lados opostos do requisito.

> A grade **nunca fica mais fina que o espaçamento real das amostras**. Se
> ficasse, pontos consecutivos cairiam em células não adjacentes, a mancha
> fragmentaria e o survey diria "nenhuma zona" com meia cava fora do requisito.
> O ajuste vai para o log.

**Zonas-problema.** As células reprovadas contíguas viram zonas, com extensão,
área, servidor dominante, equipamentos afetados e o pior ponto como sugestão de
local para avaliar rádio.

> **Fora do laudo de site survey.** Não vira mais slide nem camada no KML:
> a sugestão de coordenada para rádio novo é decisão de projeto de RF, e
> impressa ao lado do que foi medido uma passava pela outra. `zonas_problema()`
> segue no módulo e no **relatório semanal**, que é outro produto e tem outro
> leitor. Ver "Diagnóstico e conclusões", adiante.

A ação recomendada **depende da grandeza** — recomendar "mais um rádio" para
zona de interferência estaria errado, porque adensar não tira do ar quem ocupa
o canal:

| grandeza | recomendação |
|---|---|
| RSSI / SNR | avaliar rádio no ponto pior |
| Interferência | trocar canal e localizar o emissor |
| Latência | verificar saltos, backhaul e custo dos enlaces |
| Perda | verificar retransmissão e saturação antes da cobertura |

Mancha acima de 35% da área percorrida é marcada como **sistêmica** e recebe
outra recomendação: sugerir "um rádio aqui" para algo que cobre meia cava
enganaria a operação.

## Interferência sem analisador de espectro

A BC API **não expõe varredura de espectro** — não há mensagem `spectrum`,
`scan` ou `sweep` em nenhum `.proto` do bcapi (há um teste que falha se um dia
houver). O que ela dá são os contadores 802.11 de airtime, e deles sai:

```
interferência = channelBusyTime − channelReceiveTime − channelTransmitTime
```

Tempo de antena consumido por transmissor que **não é nosso**. Isso separa o
caso que confunde a operação:

| situação | busy | interferência |
|---|---|---|
| canal 50% ocupado **pelo nosso tráfego** | 50% | 1,7% — normal |
| canal 65% ocupado **por terceiro** | 65% | 60% — problema |

Um analisador de espectro não faz essa distinção: ele vê energia, não dono do
tráfego. E este mede a cava inteira, continuamente, sem hardware novo.

> Derivada só do **delta** entre coletas. Os contadores são cumulativos desde o
> boot; a razão entre eles daria a média da vida inteira do rádio. Sem delta,
> fica sem série — nunca 0.

Publicada como `rajant_radio_interf_pct`, gravada na amostra (com o canal) e
mapeada como qualquer outra grandeza. **Não** entra em `REQUISITOS`: aqueles
são os cinco da Modular Mining, e pôr interferência ali faria o resumo
reportá-la como exigência do cliente.

## Vieses a ter em mente

- **SNR é por enlace, não por lugar.** O que se mapeia é o *melhor enlace
  disponível* naquele ponto — o proxy correto para "qualidade da cobertura
  aqui", mas não uma medida do meio.
- **Só quem tem GPS contribui.** A cobertura mapeada é a das rotas percorridas,
  não da mina inteira. Os rádios que participaram e nunca reportaram fix saem
  listados como "sem posição" no Mapa da Rede — é dado de manutenção.
- **Vazão é tráfego, não capacidade.** `vazao` vem do delta de bytes: mede o que
  passou, não o que o enlace aguenta. O requisito "throughput > 1 Mbps" só é
  coberto pelo iperf manual.
- **Retenção do Prometheus** limita a janela do caminho `--survey-do-prometheus`.
  A captura pela página não depende disso, mas grava no SQLite local.

## Pela página de relatórios

Na página web do exporter (porta `porta_relatorio`), marque **"Anexar slides de
site survey"** antes de gerar o PPT. O survey tem **período próprio**, separado
do período do relatório — porque o relatório costuma cobrir a semana enquanto a
análise de cobertura interessa num turno:

| campo | o que faz |
|---|---|
| Survey de / até | data **e hora**; botões de atalho para os turnos 06–14, 14–22 e 22–06 |
| Rádio | lista carregada do Prometheus; vazio = todos |
| Banda | 2.4 GHz / 5 GHz; vazio = todas |
| Passo | intervalo da consulta; a página mostra a resolução resultante |
| Vel. mín | descarta equipamento parado |
| Grade | agrega em células de N metros |

A página calcula e exibe a resolução espacial conforme você muda o passo
(«a ~25 km/h, o passo de 60 s dá ~420 m entre amostras»), justamente para o
mapa não ser lido como survey fino.

Os parâmetros de survey **só valem para o PPT** — o Excel não tem slides de
survey e os ignora.

### Anexar o survey ao relatório

Dois caminhos, e eles não se misturam:

| caminho | como | o que usa |
|---|---|---|
| **Survey gravado** | botão **Usar no relatório** no Histórico | as amostras já no banco |
| **Recorte por período** | marcar o survey no formulário, com data/hora | consulta o Prometheus |

Se nenhum dos dois for pedido, o PPT sai com os slides de survey **em branco**,
do jeito que estão no template — não é erro, é o template. O nome do arquivo
diz qual foi o caso: só ganha `_com_Survey` quando slides foram de fato
gerados.

Falha do survey **não derruba o relatório** — o PPT sai sem ele — mas o motivo
volta no cabeçalho `X-Survey-Aviso` e a página mostra em vermelho.

### O que entra no PPT

Ao gerar com a opção marcada, na ordem:

1. **limpa os campos de localidade** dos slides de survey (`ÁREAS
   PERCORRIDAS`, linha de percurso) e alarga o mapa no espaço liberado —
   antes das imagens, porque a inserção herda a geometria da moldura;
2. **Mapa da Rede**: um slide, não por banda, com todos os BCs por tipo,
   rotas em cinza e tabela de composição da malha;
3. **Zonas-Problema e Ações**: a lista acionável;
4. por banda: gera os PNGs, troca as molduras pelas imagens e preenche os
   números medidos.

O arquivo sai como `..._com_Survey.pptx`. Se o survey falhar (sem GPS no
período, Prometheus fora), **o PPT sai mesmo assim** sem os slides de survey e
o motivo fica no log — a falha do survey não derruba o relatório inteiro.

A imagem é encaixada na moldura **preservando a proporção**: forçar largura e
altura esticava o mapa, e mapa esticado mente sobre distância — dois pontos a
300 m parecem a 500 m conforme a direção.

### Dados de demonstração

Um deck no template do cliente, com números plausíveis, circula internamente e
vira "o survey da mina". Para material de exemplo:

```python
rm.MARCA_DEMO["ativa"] = True
```

Carimba as imagens (diagonal e rodapé) e os slides — inclusive o de zonas, que
é só tabela e por isso é o que mais parece laudo. **Desligado por padrão**:
relatório de medição real não pode sair carimbado.

## Fundo dos mapas

Três caminhos, em ordem de preferência. Teste antes de gerar o relatório:

```bash
python3 rajant_monitor.py --testar-fundo
```

1. **KMZ do levantamento** (`[relatorio] survey_kmz`) — GroundOverlay já vem
   georreferenciado pelo `LatLonBox`. Numa cava é o único fundo que mostra
   rampa, praça e britador;
2. **satélite** — exige alcançar `services.arcgisonline.com`;
3. **ortofoto local** (`fundo_local`) — **exige** `fundo_bbox = N,S,L,O`. Sem
   as coordenadas a imagem é recusada: seria esticada até o retângulo dos
   dados, e mapa bonito e geograficamente errado é pior que mapa sem fundo.

## KML para o Google Earth

Quando a rede da mina bloqueia os tiles, o PNG sai sem fundo. O **Google Earth
traz o satélite dele** — por isso o KMZ resolve o problema de vez:

```bash
python3 rajant_monitor.py --kml-do-survey 3 --kml-campo snr
```

Ou pelo botão **KML** no histórico da página. Camadas que se liga e desliga:

| pasta | conteúdo |
|---|---|
| BreadCrumbs | subpastas ERB / ERM / Móvel |
| Rotas | cada trecho colorido pela grandeza escolhida |
| Medições | um ponto por amostra; o balão mostra tudo que foi medido ali |
| **ZONAS-PROBLEMA** | polígono + alfinete com a recomendação |
| FORA DO REQUISITO | só os pontos que violam o limite — **começa desligada** |
| Medições manuais | iperf e trace |

Cada ponto leva `TimeStamp`: a barra de tempo do Google Earth **anima o
survey**, que é como se acha o instante em que o sinal caiu.

### Navegação do deck

O Site Survey virou dois terços do relatório, e o menu principal tem um botão
por seção. Um botão para 27 slides significa passar página. Por isso o deck
ganha um **índice da seção 5**, em três colunas — Geral, 2.4 GHz e 5.8 GHz —
com um atalho por slide, e o botão 5 do menu principal passa a apontar para
ele em vez de cair no primeiro slide.

Cada slide da seção ganha uma barra com **▤ ÍNDICE** e um alternador
**⇄ 2.4 / 5.8 GHz**, que leva ao MESMO assunto na outra malha — a comparação
que um relatório de duas bandas pede o tempo todo.

> Ao retargetar um botão do template é preciso remover o hyperlink do **texto**,
> não só o do formato. Um botão carrega os dois, e clicando no texto vale o do
> texto: trocar só o do formato deixa o botão indo para o alvo antigo, sem que
> o python-pptx acuse o conflito.

O slide de **Análise, Recomendações e Conclusão** deixou de sair em branco: é
preenchido com o que já era calculado — percentual dentro de cada requisito,
cobertura por BC, handovers e ping-pong, e as zonas com a ação recomendada.

### Diagnóstico e conclusões

O deck de site survey (`--ppt-survey`, e o que a ferramenta de campo gera)
**termina em dois slides em branco**: *Diagnóstico* — áreas críticas e causa
provável — e *Conclusões e Ações* — uma tabela de ação, local, responsável e
prazo, com sete linhas vazias.

Em branco de propósito. Medição e recomendação são coisas diferentes, e sair
impressa uma proposta de coordenada para rádio novo ao lado do que foi medido
faz uma passar pela outra: quem lê o deck não distingue mais o que o
equipamento mediu do que a ferramenta supôs. Onde instalar rádio, com que
orçamento e quem executa são decisões de projeto de RF, do analista.

Foi por isso que a seção de zonas-problema saiu deste deck e a pasta
correspondente saiu do KMZ. O que sobrou é medição: a sub-pasta *Fora do
requisito* dentro de cada grandeza mostra os pontos reprovados, sem dizer o
que fazer com eles.

Uma consequência prática: célula vazia numa tabela do deck fica **vazia**.
Só `None` vira "—". São coisas diferentes — a tabela de ações é formulário
para preencher, e "—" ali se lê como "não se aplica" em vez de "escreva aqui".

### Montar os slides à mão

Enquanto a rede da mina bloquear os tiles, o PNG sai sem satélite. O caminho
que dá o melhor slide é tirar o print do Google Earth e colar. Para isso:

```bash
python3 rajant_monitor.py --kmz-todos 3
```

Ou o botão **KMZ+** no histórico. Sai um ZIP com **um KMZ por banda**, e dentro
de cada um **uma aba por grandeza** (RSSI, SNR, ruído, latência, perda,
interferência), mais um `LEIA-ME.txt` mapeando arquivo → aba → slide.

Só a primeira aba nasce visível: ligadas juntas, os pontos das seis grandezas
se empilham no mesmo lugar e o mapa não diz nada.

### Como o mapa é desenhado

A rota é a leitura: **cada medição pinta o seu trecho** com a cor exata do valor
na escala, num gradiente contínuo de 40 passos. Duas amostras de −74,9 e
−75,1 dBm ficam praticamente na mesma cor, como devem — com faixas fixas elas
caíam em cores diferentes e o traçado virava confete.

O ponto de medição usa **o mesmo gradiente** da linha: ponto e rota discordarem
de cor no mesmo lugar seria confuso.

A linha leva um **contorno escuro discreto** por baixo, que a mantém legível
sobre satélite claro e escuro sem virar malha preta onde uma dezena de
equipamentos cruza a mesma pista. Início e fim de cada trajeto vêm marcados.

O KMZ tem **uma pasta por grandeza** e só a primeira — o RSSI — nasce
visível: ligadas juntas, os pontos de todas se empilham no mesmo lugar e
o mapa não diz nada. Dentro de cada uma:

| camada | estado inicial |
|---|---|
| **Calor** (o rastro) | ligada |
| Fora do requisito | desligada — ligada, cobre os pontos bons |

A **linha** do trajeto vem desligada: era ela que produzia arestas retas
ligando pontos por onde ninguém passou. Quem quiser o traço cru liga em
`[relatorio] kmz_com_rotas = true`.

A pasta **Zonas-problema** saiu do laudo de survey — ver "Diagnóstico e
conclusões", adiante.

### Cores

A escala do RSSI segue a convenção dos survey comerciais (Ekahau, NetSpot):
faixa útil de **−90 a −45 dBm**, gradiente vermelho→verde, com os cortes de
qualidade em −50 / −60 / −67 / −70 / −80 dBm. O −75 do requisito Modular ganha
faixa própria, para aprovado e reprovado não dividirem a mesma cor.

O PPT, por padrão, sai com **molduras vazias** em vez das imagens geradas. Cada
moldura diz qual arquivo abrir e qual camada ligar:

```
COLAR AQUI O PRINT DO GOOGLE EARTH
Survey_*_snr_58GHz.kmz
camada: Medições   ·   5.8 GHz
```

Para voltar a inserir as imagens automaticamente:

```ini
[relatorio]
imagens_no_ppt = true
```

Em modo moldura os PNGs **não são gerados** — renderizar matplotlib para
descartar seria só gasto de CPU. O relatório sai em segundos em vez de minutos.

Survey longo é decimado com teto de pontos, e o passo real vai escrito no KML —
8 h de 150 rádios a 10 s dariam ~430 mil pontos e o Google Earth engasgaria.

## Dashboard

`Rajant · 7 · Site Survey Contínuo` (uid `rajant-survey`) mostra o mesmo dado
ao vivo: **percurso conectado** com setas de sentido e cor por SNR (camada
`route` do geomap, com os pontos por cima), velocidade, rumo, peers ativos em
rota, a linha do tempo de **qual BC está servindo cada equipamento** e a
tabela de candidatos a buraco de cobertura com lat/lon para levar ao Google
Earth.

## O que continua exigindo campanha manual

O survey contínuo não cobre:

- **áreas onde equipamento não circula** — pátios, taludes, áreas novas;
- **medida em altura** — tudo é medido na altura da antena do equipamento;
- **varredura de espectro (FFT)** — a BC API dá noise floor e airtime, dos
  quais sai a *ocupação por terceiro*, mas não o espectro. Para identificar
  emissor não-Wi-Fi (forno, radar, enlace ponto-a-ponto) é preciso hardware:
  HackRF One, Wi-Spy DBx ou RF Explorer 6G. **Cuidado com o RTL-SDR**, que é o
  mais recomendado por ser barato e vai só até 1,7 GHz — não alcança 2,4 nem
  5,8 GHz;
- **capacidade de throughput** — a `vazao` automática é tráfego. Use o iperf
  manual para o requisito de 1 Mbps;
- **resolução sub-70 m**, pelos motivos acima.

Para esses casos o caminho antigo continua valendo: `--survey-csv arquivo.csv`
com o resultado da campanha, e a aba **Medições** da página para lançar iperf
e trace georreferenciados, que entram no PPT e no KML.
