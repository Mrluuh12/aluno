# Site Survey — Rajant

Gera os KMZ com o trajeto colorido, o PPT na identidade Anglo e o Excel.

**Duplo clique** no `site_survey.exe`. A janela tem duas abas:

| aba | o que faz |
|---|---|
| **Coleta** | fala com os rádios, descobre a malha, você marca os equipamentos e coleta ao vivo |
| **Arquivos do MeshMapper** | lê capturas que alguém já trouxe |

A aba Coleta precisa de rede até a malha e da `rajant-api`. Sem elas, ela
se explica em vez de falhar e a de Arquivos continua funcionando — o
mesmo executável serve as duas máquinas.

Pela linha de comando, os módulos funcionam soltos:

```
survey_meshmapper captura1.kmz captura2.kmz -o relatorios
survey_meshmapper *.kmz --separado          # um relatório por arquivo
coleta_rajant --seeds 10.188.96.140 --minutos 30 -o relatorios
```

## Coleta ao vivo: densidade de leituras

O que se escolhe na tela é o **passo em metros** entre leituras do mesmo
equipamento — padrão **10 m**, a densidade do MeshMapper (1 leitura por
segundo a ~40 km/h). Parado, o rádio é lido a cada 10 s, só para
perceber quando sair.

Até a versão anterior o espaçamento real ficava em 60–80 m com o alvo
pedindo 15 m. Três causas, todas corrigidas:

| causa | efeito | agora |
|---|---|---|
| leitura em **lotes**: esperava o rádio mais lento do lote | um rádio fora de alcance travava todos por até 6 s — 67 m de caminhão | 24 leituras independentes; cada uma já pega o próximo rádio |
| `gpsVel` é opcional no `Gps.proto` | sem velocidade, o caminhão caía no ritmo de parado (30 s) | velocidade estimada pelo próprio deslocamento |
| parado lido a cada 30 s | até 300 m sem leitura na arrancada | 10 s |

A linha de status mostra **ms/leitura** ao lado do **passo real**. Se o
passo real ficar acima do pedido com leitura lenta, o gargalo é a rede
até os rádios, não a ferramenta.

## Entradas aceitas

| arquivo | observação |
|---|---|
| `Meshmapper_*.kmz` | o melhor: traz altitude e contagem de vizinhos ativos |
| `data.json` | o mesmo conteúdo, se extraído do KMZ |
| `Meshmapper_*_trace_path.csv` | o par de CSVs; o `_peer_info.csv` é lido junto |

## Saídas

| arquivo | o que tem |
|---|---|
| `Survey_*_24GHz.kmz` | trajeto de 2,4 GHz, uma aba por grandeza, com a legenda na tela |
| `Survey_*_58GHz.kmz` | idem, 5,8 GHz |
| `Site_Survey_*.pptx` | capa, índice, sumário, metodologia, uma página por grandeza/banda e duas páginas em branco para diagnóstico e conclusões |
| `Survey_*.xlsx` | origens, resumo, por grandeza e amostras |

**Juntar tudo num relatório só** (padrão) produz um conjunto com todas as
capturas — é o caso da campanha cobrindo a mina. Desmarcado, sai um
conjunto por arquivo, cada um em sua subpasta.

## O laudo: um arquivo por banda

**2,4 e 5,8 GHz saem em KMZs separados.** São malhas diferentes no mesmo
terreno — num arquivo só, a banda boa tapa a ruim e o mapa deixa de dizer
qual das duas está servindo.

Dentro de cada arquivo, uma aba por grandeza medida:

| aba | o que é |
|---|---|
| **RSSI (melhor ERB/ERM do ponto)** | a **cor do laudo** |
| SNR | relação sinal/ruído do enlace |
| Ruído | piso de ruído, recuperado de `signal − snr` |
| Custo do caminho | o *Trace Path Cost* do MeshMapper: custo InstaMesh até o destino do trace |

**RSSI aqui é o da melhor ERB/ERM visível no ponto**, não o do enlace que
o InstaMesh escolheu. Outro caminhão passando dá sinal ótimo e vai
embora; só a infraestrutura caracteriza cobertura.

Havia uma segunda aba com o enlace escolhido. Saiu: duas abas chamadas
RSSI no painel de camadas obrigavam a lembrar qual era qual, e o laudo
responde uma pergunta só — *quanto sinal há neste ponto da mina*. O
enlace escolhido continua na aba **Amostras** do Excel, na coluna
*RSSI do enlace (dBm)*, ao lado da *Δ não usado (dB)*.

**A eleição acontece dentro da banda.** O mapa de 5,8 GHz só considera
vizinhos de 5,8 GHz. No arquivo do CA-1006 o mesmo ponto via uma ERM a
−71 dBm em 2,4 GHz e outra a −86 dBm em 5,8 GHz; sem o recorte, o mapa de
5,8 GHz saía pintado com os −71 dBm da outra banda — 15 dB, a distância
entre aprovado e reprovado. Ponto sem vizinho na banda fica **sem cor**.

Quanto o enlace escolhido deixou na mesa é a coluna *Δ não usado (dB)* do
Excel. Vale conferi-la: delta grande com cobertura boa não é falta de
rádio, é escolha de caminho, e repetidora nova não resolveria.

## O mapa: trajeto contínuo, como o MeshMapper

O rastro é uma **fita contínua** por onde o rádio passou, como o
MeshMapper da Rajant desenha — não mais bolhas de calor. Com amostras
espaçadas, o calor de raio fixo saía em bolhas soltas e borradas, com o
chão da cava tingindo a cor pela transparência.

- **Cada trecho mostra a leitura real mais próxima.** A amostra é dona do
  caminho até o ponto médio com a vizinha; nenhum trecho mostra média nem
  cor interpolada entre duas leituras.
- **A fita parte onde houve buraco** de medição (tempo ou salto de
  posição), em vez de traçar uma reta por onde ninguém passou.
- **Opaca, com contorno escuro por baixo**: a cor vista é a cor da faixa.
- **A legenda vai na tela**, dentro de cada aba. O print do Google Earth
  que vai para o slide leva junto a régua com que foi pintado.

O calor continua disponível, desligado: `[relatorio] kmz_com_calor = true`.

## A régua de cores: de onde vem cada valor

Nada é inventado; a fonte de cada régua vai impressa no rodapé da legenda.

**Cores** — as oficiais do Rajant MeshMapper, lidas do `doc.kml` que o
BC|Commander 11.29.1 gerou na mina: `FF0000` (poor), `F26A00` (good),
`3EAD30` (great). O amarelo `DD9F17` e o vermelho-escuro `B70404` são os
tons dos alfinetes do mesmo MeshMapper. O `5C0000` da pior faixa é o único
tom que não vem da Rajant: o vermelho dela escurecido.

**RSSI (dBm)** — o MeshMapper **não** classifica o Signal: no balão dele o
dBm sai sem cor (conferido em 11.216 células). A régua é a convenção de
Wi-Fi mais citada, MetaGeek/Oscium (−67 muito bom, mínimo para voz e
vídeo, a mesma borda de célula do guia de site survey da Cisco; −70 ok;
−80 conectividade básica; −90 inutilizável), mais o −75 do requisito
Modular.

| RSSI (dBm) | cor | leitura |
|---|---|---|
| ≥ −67 | verde `3EAD30` | muito bom |
| −70 a −68 | amarelo `DD9F17` | ok |
| −74 a −71 | laranja `F26A00` | atende o requisito |
| **−80 a −75** | vermelho `FF0000` | **reprovado** |
| −89 a −81 | `B70404` | abaixo da conectividade básica |
| ≤ −90 | `5C0000` | inutilizável |

**SNR (dB)** — a régua **oficial da Rajant**, gravada pelo MeshMapper no
`data.json` (`goodRSSI = 20`, `greatRSSI = 30`; no vocabulário Rajant
"RSSI" é o SNR em dB) e conferida célula a célula nos seus arquivos.

| SNR (dB) | cor | leitura |
|---|---|---|
| ≥ 30 | verde `3EAD30` | ótimo |
| 21 a 29 | laranja `F26A00` | bom |
| ≤ 20 | vermelho `FF0000` | ruim |

**Ruído (dBm)** — não existe régua oficial. É **derivado** das duas acima:
um sinal no mínimo Modular (−75 dBm) sobre um ruído N chega com
SNR = −75 − N, e leva a cor desse SNR. O corte antigo, −85, era incoerente
com os próprios requisitos: −75 sobre −85 é SNR 10.

| Ruído (dBm) | cor | um sinal de −75 dBm chegaria com |
|---|---|---|
| ≤ −105 | verde | SNR ≥ 30 |
| −104 a −96 | laranja | SNR 21 a 29 |
| ≥ −95 | vermelho | SNR ≤ 20 |

**Custo do caminho** — a régua **oficial da Rajant** (`greatPath = 10000`,
`goodPath = 20000` no `data.json`), a mesma pela qual o MeshMapper pinta a
linha do trajeto. Não é requisito da Modular: slides, legenda e pasta
dizem "referência Rajant".

| Custo do caminho | cor | leitura |
|---|---|---|
| ≤ 10000 | verde | ótimo |
| 10001 a 20000 | laranja | bom |
| ≥ 20001 ou **sem rota** | vermelho | ruim |

É o custo **total** até o destino do trace (o MeshMapper grava o host em
`traceInfo.host`; nos seus arquivos, `10.188.96.11`, que vai escrito no
slide de Metodologia). Resume num número os saltos, a taxa e o SNR de cada
enlace do caminho — é a medida mais próxima de "o Dispatch funciona aqui".

- **Sem rota** (`2147483647`) é medição, a pior possível: sai vermelho,
  como no MeshMapper, e escrito "sem rota" — nunca o número.
- A estatística usa a **mediana**: um único ponto sem rota leva a média do
  custo a dez dígitos.
- O **custo do enlace** (primeiro salto, `hopcost`) continua na coluna
  *Custo do enlace* do Excel; *Custo do caminho* fica ao lado.
- A **coleta ao vivo não mede** o custo do caminho: ele vem de uma tarefa
  TRACE do rádio, e a `rajant-api` 0.1.1 não tem método para ela. A aba e
  o slide simplesmente não aparecem.

### A fronteira exata

O rádio reporta dBm e dB **inteiros**: −75 exato é uma leitura comum. O
requisito é RSSI **> −75**, então −75 é reprovado — e sai vermelho. Antes
ele caía no laranja de "atende" enquanto o Sumário o contava como
reprovado. Agora cada faixa contém só aprovados ou só reprovados, e a
legenda escreve o intervalo exato em inteiros.

Um ponto a confirmar com a Modular: **SNR 20 exato**. O código segue o
requisito como está escrito, "> 20", e pinta 20 de vermelho; o MeshMapper
da Rajant considera 20 "good". Se o documento da Modular disser "≥ 20",
basta trocar o operador em `REQUISITOS` e a cor muda junto com a contagem.

O PPT traz um slide de **Metodologia** logo após o sumário: ficha técnica
do levantamento — instrumento, número de equipamentos e amostras, a
grandeza de referência, como a posição foi obtida e validada, as bandas,
o requisito Modular Mining e o que **não** foi medido — mais essa mesma
régua de cores com a classificação de cada faixa.

## Diagnóstico e conclusões

O deck termina com dois slides **em branco**, para o analista preencher:

| slide | o que vai nele |
|---|---|
| **Diagnóstico** | áreas críticas identificadas e causa provável |
| **Conclusões e Ações** | tabela de ação / local / responsável / prazo |

Ficam em branco de propósito. A ferramenta mede; onde instalar rádio,
qual o orçamento e quem executa são decisões de projeto, e sair impressa
uma sugestão ao lado do que foi medido faz uma passar pela outra.

Pelo mesmo motivo saiu a antiga seção de **zonas-problema**, que marcava
regiões no mapa e propunha coordenada para rádio novo. A sub-pasta *Fora
do requisito* continua dentro de cada grandeza no KMZ: ela mostra os
pontos reprovados, que é medição, sem propor o que fazer com eles.

## As abas de vizinhança

Ficam **desligadas**. Censo de vizinhos, pegada por repetidora e o KMZ de
vizinhança respondem outra pergunta — *"quem fala com este rádio"* — e
misturadas com o laudo de trajeto atrapalham mais do que informam.

Para ligar, no `config.ini`:

```ini
[relatorio]
vizinhanca = true
```

Vale quando a pergunta for mesmo essa, que é o caso de uma captura feita
com o MeshMapper parado numa repetidora.

## Captura de veículo e captura de repetidora

A ferramenta reconhece sozinha qual é qual, pela posição do próprio
arquivo, e entrega o produto certo para cada uma.

| | veículo andando | MeshMapper ligado numa repetidora |
|---|---|---|
| o que o arquivo é | um **trajeto**: cada ponto é um lugar | uma **janela de tempo** num lugar só |
| produto | trajeto colorido + PPT + Excel | censo de vizinhos + pino no mapa |
| pergunta que responde | como está a cobertura **nesta rota** | quem fala com **este rádio** e como |

Captura parada **não vira trajeto**. Sairia uma mancha de um
pixel pintada com a escala de área — parece um mapa e não é. No lugar
dela vai o censo: uma linha por vizinho, com RSSI, SNR, banda e
**presença** (em que fração dos pontos aquele vizinho esteve visível).

A presença é o que separa `-58 dBm em 35% dos pontos` — um veículo que
passou perto — de `-93 dBm em 100%` — um vizinho permanente e fraco. A
mediana sozinha não distingue os dois, e eles pedem decisões opostas.

### O que a captura da repetidora não pode dar

**A posição dos vizinhos.** Um BreadCrumb reporta de cada vizinho apenas:

```
channel, cost, encap, filtered, frequency, ipaddr, mac,
name, rssi, serialNumber, signal
```

Não há latitude nem longitude. As coordenadas que aparecem no
`_peer_info.csv` são as **de quem capturou**, repetidas para cada
vizinho. Por isso o censo é uma tabela, e no mapa aparece só o pino do
rádio que fez a captura.

**Com mais de uma captura parada isso muda.** Cada arquivo traz a posição
do próprio rádio; juntando vários, a ferramenta desenha os **enlaces**
entre os que se enxergam, coloridos pelo RSSI. Enlace assimétrico fica
com a pior das duas pontas — é ela que limita.

## O que o MeshMapper não fornece

Latência, perda de pacotes e interferência. Essas grandezas **não viram
slide nem aba**: uma página com escala, requisito e gráfico vazio lê-se
como *"medi e deu tudo fora"*, que é o oposto de *"não medi"*.

## Instalação

```
build\gerar_exe_site_survey.bat
```

Gera `dist\site_survey\site_survey.exe`. A pasta `marca/` fica ao lado do
executável — sem ela o deck sai sem logo, em vez de falhar.

O build **falha se o exe não for abrir**: o último passo roda
`site_survey.exe --verificar`, que confere módulos, tcl/tk e dependências
sem abrir janela. Se algo der errado depois, rode isso no Prompt e mande
a saída; o erro também fica em `site_survey_erro.txt`, ao lado do exe.

Para rodar direto pelo Python:

```
pip install matplotlib numpy scipy python-pptx openpyxl lxml
pip install rajant-api --no-deps && pip install "protobuf==4.23.4"
python site_survey.py
```

## Arquivos

| | |
|---|---|
| `site_survey.py` | a janela: só a interface |
| `coleta_rajant.py` | a coleta ao vivo dos rádios |
| `survey_meshmapper.py` | a leitura de arquivos do MeshMapper |
| `rajant_monitor.py` | o motor de KMZ, PPT e Excel |
| `bcapi-ref/proto/` | os `.proto` do bcapi: fonte de tudo que se afirma sobre a API |
| `marca/` | logos usados nos decks |
| `exemplos/` | uma captura de veículo e uma de repetidora, usadas pelos testes |
| `build/` | spec do PyInstaller e o `.bat` |

O `rajant_monitor.py` vem junto porque é ele que gera KMZ, PPT e Excel —
são milhares de linhas já corrigidas contra armadilhas do formato (ordem
dos elementos no KML, escape do XML, borda de célula no PPTX). Aqui ele é
usado só como biblioteca: nada de rede é acionado.
