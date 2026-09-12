# Site Survey — Rajant

Gera os KMZ com rastro de calor, o PPT na identidade Anglo e o Excel.

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

## Entradas aceitas

| arquivo | observação |
|---|---|
| `Meshmapper_*.kmz` | o melhor: traz altitude e contagem de vizinhos ativos |
| `data.json` | o mesmo conteúdo, se extraído do KMZ |
| `Meshmapper_*_trace_path.csv` | o par de CSVs; o `_peer_info.csv` é lido junto |

## Saídas

| arquivo | o que tem |
|---|---|
| `Survey_*_24GHz.kmz` | rastro de calor de 2,4 GHz, uma aba por grandeza |
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

**A cor do mapa é exatamente a da legenda.** O rastro é pintado em
degraus por faixa, não em gradiente contínuo: um pixel de −78 dBm sai com
a mesma cor que a legenda mostra para a faixa −80 a −75. Antes o raster
interpolava 256 tons entre −90 e −55, e cor conferida contra legenda dava
outra coisa.

| faixa | leitura |
|---|---|
| acima de −50 dBm | excelente |
| −60 a −50 | muito bom |
| −67 a −60 | bom — limiar clássico de voz e vídeo |
| −70 a −67 | aceitável |
| **−75 a −70** | **limite do requisito Modular** |
| −80 a −75 | fraco |
| −85 a −80 | muito fraco |
| abaixo de −85 | inutilizável |

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
| produto | rastro de calor + PPT + Excel | censo de vizinhos + pino no mapa |
| pergunta que responde | como está a cobertura **nesta rota** | quem fala com **este rádio** e como |

Captura parada **não vira rastro de calor**. Sairia uma mancha de um
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
