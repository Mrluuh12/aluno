# Site Survey — relatórios a partir do MeshMapper

Gera o KMZ com rastro de calor, o PPT na identidade Anglo e o Excel, a
partir das capturas que o MeshMapper grava.

Não fala com rádio, não precisa de rede, não precisa da `rajant-api`.

## Usar

**Duplo clique** no `survey_meshmapper.exe`. Abre a janela:

1. **Escolher arquivos…** — pode selecionar quantos quiser
2. **Onde salvar**
3. **Gerar relatórios**

Por linha de comando também funciona:

```
survey_meshmapper.exe captura1.kmz captura2.kmz -o relatorios
survey_meshmapper.exe *.kmz --separado        # um relatório por arquivo
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
| `Survey_*.kmz` | rastro de calor por grandeza, uma aba cada, para o Google Earth |
| `Vizinhanca_*.kmz` | pino de cada rádio capturado parado e os enlaces medidos entre eles |
| `Site_Survey_*.pptx` | capa, índice, sumário, zonas-problema e uma página por grandeza/banda |
| `Survey_*.xlsx` | origens, resumo, por grandeza, amostras, **censo de vizinhos**, **por repetidora** e todos os vizinhos ponto a ponto |

**Juntar tudo num relatório só** (padrão) produz um conjunto com todas as
capturas — é o caso da campanha cobrindo a mina. Desmarcado, sai um
conjunto por arquivo, cada um em sua subpasta.

## As três camadas do mapa

Todas medidas. Nenhuma estimada.

| camada | o que mostra | pergunta que responde |
|---|---|---|
| **Serviço entregue** | o enlace que o InstaMesh escolheu | a aplicação funcionou aqui? |
| **Cobertura disponível** | o melhor ERB/ERM visível no ponto | existe sinal servível aqui? |
| **Por repetidora** | a pegada de cada ERB/ERM | até onde a ERM-28 alcança? |

As duas primeiras podem divergir muito. No arquivo do CA-1006 a diferença
foi de **22 dB na mediana** — o enlace entregue a −88 dBm enquanto havia
um ERB a −66 dBm no mesmo ponto, sem uso. Isso muda o laudo: a área tinha
cobertura, o caminho escolhido é que era ruim. Repetidora nova não
resolveria.

A terceira existe porque as outras duas misturam as repetidoras. A
cobertura disponível é *o melhor de cada ponto* — não dá para perguntar
por uma em específico.

**Como ela é montada:** cada leitura de um veículo traz a posição dele **e
o sinal para todas as repetidoras que ele ouve** — 18 na mediana, medido
no trajeto real. Uma passagem de um caminhão alimenta 18 mapas ao mesmo
tempo. A posição é a do veículo, o sinal é o dele para aquela repetidora,
os dois da mesma leitura.

A pasta nasce recolhida e desligada: dezoito rastros ligados juntos se
empilham e o mapa não diz nada.

> Duas repetidoras inteiramente abaixo de −90 dBm saem com a **mesma cor**,
> porque toda essa faixa satura no fundo da escala. É correto — no mapa as
> duas são "não serve aqui". Os números que as separam estão na descrição
> da pasta e na aba **Por Repetidora**.

## Captura de veículo e captura de repetidora

A ferramenta reconhece sozinha qual é qual, pela posição do próprio
arquivo, e entrega o produto certo para cada uma.

| | veículo andando | MeshMapper ligado numa repetidora |
|---|---|---|
| o que o arquivo é | um **trajeto**: cada ponto é um lugar | uma **janela de tempo** num lugar só |
| produto | rastro de calor + zonas-problema | censo de vizinhos + pino no mapa |
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
build\gerar_exe_survey.bat
```

Gera `dist\survey_meshmapper\survey_meshmapper.exe`. A pasta `marca/` fica
ao lado do executável — sem ela o deck sai sem logo, em vez de falhar.

Para rodar direto pelo Python:

```
pip install matplotlib numpy scipy python-pptx openpyxl lxml
python survey_meshmapper.py
```

## Arquivos

| | |
|---|---|
| `survey_meshmapper.py` | a ferramenta: interface e linha de comando |
| `rajant_monitor.py` | o motor de KMZ, PPT e Excel |
| `marca/` | logos usados nos decks |
| `exemplos/` | uma captura de veículo e uma de repetidora, usadas pelos testes |
| `build/` | spec do PyInstaller e o `.bat` |

O `rajant_monitor.py` vem junto porque é ele que gera KMZ, PPT e Excel —
são milhares de linhas já corrigidas contra armadilhas do formato (ordem
dos elementos no KML, escape do XML, borda de célula no PPTX). Aqui ele é
usado só como biblioteca: nada de rede é acionado.
