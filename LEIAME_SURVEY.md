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
| `Site_Survey_*.pptx` | capa, índice, sumário, zonas-problema e uma página por grandeza/banda |
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
| **Cobertura disponível** | a melhor ERB/ERM visível no ponto — é a **cor do laudo** |
| RSSI | o enlace que o InstaMesh de fato escolheu |
| SNR | relação sinal/ruído do enlace |
| Ruído | piso de ruído, recuperado de `signal − snr` |

**A cor de cada ponto vem da melhor ERB/ERM daquele ponto**, não do
enlace que atendeu. Outro caminhão passando dá sinal ótimo e vai embora;
só a infraestrutura caracteriza cobertura. Por isso a aba de cobertura
disponível vem primeiro.

As duas podem divergir muito. No arquivo do CA-1006 a diferença foi de
**22 dB na mediana** — o enlace entregue a −88 dBm enquanto havia um ERB
a −66 dBm no mesmo ponto, sem uso. Isso muda o laudo: a área tinha
cobertura, o caminho escolhido é que era ruim.

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
