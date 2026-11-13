# Site Survey — Rajant

Gera os KMZ com o trajeto colorido, o PPT na identidade Anglo e o Excel,
separados por **área demarcada**.

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
survey_meshmapper captura1.kmz captura2.kmz -o relatorios --areas "Cava Norte.tif" Britagem.tif
survey_meshmapper *.kmz --separado          # um relatório por arquivo
coleta_rajant --seeds 10.188.96.140 --minutos 30 -o relatorios --areas areas.kmz
```

## Áreas demarcadas

O laudo sai **por área**. Na janela, embaixo (vale para as duas abas):
**Áreas → Adicionar áreas…**. Aceita três tipos de arquivo, juntos ou
separados:

| arquivo | o que é a área |
|---|---|
| **GeoTIFF** (`.tif`) de cada área — ortofoto de drone, satélite | **o nome do arquivo é o nome da área** (`Cava Norte.tif` → "Cava Norte") e o contorno é a parte da imagem que tem dado: o que estiver sem dado (transparente/nodata) em volta fica fora. Imagem sem nodata vale o retângulo inteiro |
| **KMZ/KML** do Google Earth | um polígono por área, desenhado com **Adicionar polígono**; o nome do polígono é o nome da área. Dois polígonos com o mesmo nome viram uma área em duas partes; buraco no polígono fica fora |
| **LAS/LAZ** (nuvem de pontos do terreno) de cada área | o **terreno** da área, para a cobertura estimada (abaixo). Vai para a área de mesmo nome (`Cava Norte.las` com `Cava Norte.tif`) ou para a que ele cobre; sozinho, ele mesmo é a área |

Com os dois juntos, **quem demarca são os polígonos**, e cada imagem vira
o fundo da área que ela cobre.

A janela lê os arquivos logo ao adicionar, sem travar — GeoTIFF de drone
tem gigabytes — e mostra as áreas achadas. A primeira leitura de um
arquivo grande pode demorar (num teste com 16000 × 16000 pixels, sem
pirâmides, foram ~4 s); ela fica guardada em `fundo_cache/`, ao lado do
programa, e a mesma imagem não é relida a cada survey (muda o arquivo,
ele é lido de novo). A memória usada na leitura é limitada pelo próprio
leitor de GeoTIFF, qualquer que seja o tamanho do arquivo.

Também vale pela linha de comando (`--areas` com um ou vários arquivos)
ou fixo no `config.ini`, com `;` entre os arquivos:

```ini
[survey]
areas = C:\Survey\Cava Norte.tif ; C:\Survey\Britagem.tif
```

Com áreas:

| | o que muda |
|---|---|
| leituras | cada uma leva a área em que caiu; **as de fora de todas as áreas são descartadas** (o andamento, a Metodologia e o Excel dizem quantas) |
| sobreposição | a leitura que cai em duas áreas fica na **primeira**; o andamento avisa |
| KMZ | em cada aba de grandeza, **uma pasta por área**, com o calor recortado no contorno, o trajeto, as medições e o fora do requisito daquela área; e a pasta **Áreas**, com o contorno e o nome de cada uma — clicando, os números dela |
| PPT | **Resumo por área** (área × banda: amostras, RSSI e SNR contra o requisito) e uma **seção por área**: a abertura com os números e os botões para as páginas dela, e as páginas de grandeza da área. O Índice leva às áreas; cada página tem **◂ ÍNDICE** e **◂ ÁREA** |
| Excel | coluna **Área** nas amostras e a aba **Por Área** |

O Sumário continua sendo do survey inteiro — do que caiu dentro das áreas.
Sem nenhuma área, tudo sai como antes, do survey inteiro.

Na coleta ao vivo as áreas são lidas **antes** de começar: arquivo errado
aparece na hora, não depois de meia hora de coleta. Se no fim nenhuma
leitura cair dentro das áreas, o laudo sai sem áreas — a coleta não se
perde.

### O mapa sobre a imagem da área

Com GeoTIFF, o programa **pinta a medição em cima da imagem**, com as
mesmas cores e a mesma régua do KMZ (o mapa de calor por faixa, recortado
no contorno da área), mais o contorno, a barra de escala e o norte.

| onde | o que sai |
|---|---|
| **PPT** | cada página de grandeza da área já vem com o mapa pronto, no lugar da moldura "colar aqui o print do Google Earth". Embaixo, de qual imagem ele saiu |
| **pasta `mapas/`** | o mesmo mapa como **GeoTIFF**, um por área, grandeza e banda (`Cava-Norte_sinal_cob_58GHz.tif`), no **mesmo sistema de coordenadas da imagem** (UTM, graus…): abre por cima das camadas no QGIS/ArcGIS. Fora da área fica transparente |

A caixa **Mapas GeoTIFF** (ao lado de KMZ, PPT e Excel) liga e desliga a
pasta `mapas/`; o PPT leva o mapa sempre. Área de polígono sem imagem que
a cubra continua com a moldura para o print.

A imagem é lida **reduzida** (até 3000 pixels no lado maior) e só na
janela da área: o mapa de uma área de 1,5 km sai com ~0,5 m por pixel,
mais que o suficiente para o calor, que é calculado a cada 1 m ou mais.
Imagem de satélite em 16 bits é esticada para a tela; imagem em tons de
cinza ou com paleta também serve.

## Cobertura estimada pelo relevo (LAS)

Com o LAS das áreas, o programa estima a cobertura das ERB/ERM pelo
relevo e pinta como **mapa de calor sobre os tif das áreas**. **Não é
medição**: sai num deck próprio (`Cobertura_Estimada_*.pptx`), separado
do laudo, que continua sendo só o que foi medido.

**Por área, as ERB/ERM que estão dentro dela** — todas:

| página | o que mostra |
|---|---|
| abertura da área | as ERB/ERM que estão na área, com a altura do mastro e quanto da área cada uma cobre acima de −75 dBm, por banda; botões para as páginas |
| **uma por ERB/ERM**, por banda | o sinal só daquela ERB/ERM em toda a área; ela em amarelo, as outras em cinza |
| **conjunto**, por banda | as ERB/ERM da área juntas: em cada ponto, o sinal mais forte entre elas |

ERB/ERM fora da área não entra nela. Área sem nenhuma dentro tem a
abertura dizendo isso; para simular uma, ponha-a na planilha com
`proposta = sim`.

**O mapa de calor.** A cor é **contínua** nas cores da régua do RSSI —
do roxo (−95) ao verde-escuro (−55), com cada cor no meio da sua faixa e
passando de uma para a outra sem degrau —, e a **linha branca** marca o
requisito, −75 dBm. As cores saem da própria régua do medido: mudou a
régua, o estimado muda junto. O
**relevo do LAS sombreia a cor**: a bancada, a encosta e a crista
aparecem por baixo do calor, e dá para ver onde o morro faz sombra. Abaixo
de −95 dBm a cor some aos poucos e fica só a imagem. A escala do slide é a
mesma barra contínua.

**Como o número sai.** Em cada ponto:

    RSSI = potência + ganho TX + ganho RX − perda de cabo
           − espaço livre − difração no relevo + correção da calibração

- o **terreno** é o ponto mais alto de cada célula de 3 m do LAS (o que
  barra o sinal é a crista da bancada, a pilha, o prédio), sem os pontos
  de ruído;
- a **difração** soma as **duas cristas** que mais barram o caminho entre
  a antena da ERB/ERM e a do veículo (método de Deygout, ITU-R P.526): a
  dominante vista da antena e, entre ela e o veículo, a próxima — numa
  cava o sinal costuma passar por mais de uma bancada. Curvatura da Terra
  incluída;
- o **diagrama vertical** da antena da ERB/ERM, quando a planilha informa
  a abertura vertical: perde 12·(desvio/abertura)², até 20 dB
  (3GPP TR 36.814). Com uma omni de 15°, meia abertura (7,5° abaixo da
  antena) já custa 3 dB, e o veículo várias bancadas abaixo, perto da
  ERM, fica fora do feixe. Sem a abertura vertical, a antena é vista igual
  em qualquer elevação — e a cava perto do mastro sai otimista;
- a **calibração** compara o estimado com o que os veículos mediram de
  cada ERB/ERM no survey e corrige a diferença mediana, por banda. O que
  sobra é o **erro do modelo, em dB**, escrito nas premissas e em cada
  mapa. Com menos de 20 leituras numa banda ela fica sem correção; e uma
  correção acima de **15 dB é rejeitada** — ganho de antena e cabo não
  explicam tanto, e o deck manda conferir posição, altura e potência das
  ERB/ERM.

**As ERB/ERM.** A posição, as bandas e a potência de cada uma vêm da
busca (o GPS e os rádios de cada BreadCrumb). O resto fica na planilha
`infraestrutura.csv`, **na mesma pasta do programa** (junto do
`site_survey.exe` e do `config.ini`). O botão **ERB/ERM…** na janela
cria a planilha se ela não existir e a abre no Excel — é o jeito certo de
saber qual arquivo o programa está usando.

| coluna | o que é |
|---|---|
| `nome` | como aparece na rede (ERM-08 PTP CAM); espaço no fim não importa |
| `lat`, `lon` | posição, em graus com vírgula (-18,8940720); vazio = a do GPS do rádio |
| `altura_m` | **altura da antena acima do terreno** — o que mais pesa |
| `potencia_dbm` | potência de TX para as duas bandas; vazio = a que o rádio informa |
| `potencia_24_dbm`, `potencia_58_dbm` | potência de TX de uma banda só; vale mais que a geral |
| `ganho_dbi`, `perda_cabo_db` | ganho da antena e perda no cabo |
| `ganho_24_dbi`, `ganho_58_dbi` | ganho de uma banda só; vale mais que o geral |
| `banda` | se a linha vale só para uma banda (2.4 ou 5.8) |
| `azimute`, `abertura` | antena setorial: para onde aponta e a abertura **horizontal**, em graus |
| `abertura_vertical`, `inclinacao` | abertura **vertical** da antena e inclinação para baixo, em graus |
| `abertura_vertical_24`, `abertura_vertical_58` | abertura vertical de uma banda só; vale mais que a geral |

Antena dual band tem ganho e abertura vertical diferentes em cada banda.
A Rajant RFI-2450-5-7-NM, por exemplo, tem 5 dBi e 25° em 2,4 GHz e
7 dBi e 15° em 5 GHz — nas colunas `_24` e `_58`. O deck mostra, por
ERB/ERM, o ganho e a abertura usados em cada banda.
| `proposta` | `sim` = ERB/ERM que ainda não existe: o "e se" de pôr uma nova ali |

Coluna a mais (uma de observação, por exemplo) é ignorada e fica onde
está. Pode salvar pelo Excel como "CSV (separado por vírgulas)": o Excel
em português grava com ";", e o programa lê nos dois formatos de texto
que o Excel usa.

A busca e o survey **acrescentam** à planilha as ERB/ERM que acharem e,
nas linhas que já existem, **só preenchem o que estiver vazio** — posição
do GPS e potência de cada banda. O que alguém digitou fica. O que não for
informado usa os padrões do `[cobertura]` do `config.ini` (mastro de 9 m,
antena no veículo a 4 m, 24 dBm…), e o deck diz quantas usaram o padrão.

**Os arquivos:**

| | |
|---|---|
| `Cobertura_Estimada_*.pptx` | premissas e erro do modelo, a tabela das ERB/ERM e uma seção por área |
| `estimativa/cobertura_estimada_*.tif` | o RSSI estimado em **dBm** (número, não cor), do conjunto de todas as ERB/ERM, em toda a área dos LAS — para estilizar no QGIS/ArcGIS |
| `estimativa/ERB-ERM/<nome>_*.tif` | o mesmo, de cada ERB/ERM sozinha, em dBm |
| `estimativa/<área>/` | os mapas de calor coloridos sobre o tif da área: o conjunto e um por ERB/ERM |

Área sem tif ganha o relevo sombreado do próprio LAS de fundo. O modelo
não vê veículos nem equipamentos (não estão no LAS), reflexão nas paredes
da cava nem a cava que mudou depois do LAS — onde estimado e medido
divergem, vale o medido, e a divergência aponta altura, potência ou
obstáculo a conferir.

## Coleta ao vivo: densidade de leituras

O que se escolhe na tela é o **passo em metros** entre leituras do mesmo
equipamento — padrão **10 m**, a densidade do MeshMapper (1 leitura por
segundo a ~40 km/h). Parado, o rádio é lido a cada 10 s, só para
perceber quando sair.

**Não desça abaixo de ~5 m.** O GPS do rádio erra alguns metros; com passo
de 1 m a coleta relê o rádio antes de ele sair do lugar dentro do erro do
GPS. Numa coleta de campo com passo 1 m, o passo real ficou em 0,7 m e 793
leituras caíram na mesma posição da anterior — pontos a mais no mapa sem
informação nova, e leitura tirada de rádio que estava andando.

Até a versão anterior o espaçamento real ficava em 60–80 m com o alvo
pedindo 15 m. Três causas, todas corrigidas:

| causa | efeito | agora |
|---|---|---|
| leitura em **lotes**: esperava o rádio mais lento do lote | um rádio fora de alcance travava todos por até 6 s — 67 m de caminhão | 48 leituras independentes; cada uma já pega o próximo rádio |
| `gpsVel` é opcional no `Gps.proto` | sem velocidade, o caminhão caía no ritmo de parado (30 s) | velocidade estimada pelo próprio deslocamento |
| parado lido a cada 30 s | até 300 m sem leitura na arrancada | 10 s |

A linha de status mostra **ms/leitura** ao lado do **passo real**. Se o
passo real ficar acima do pedido com leitura lenta, o gargalo é a rede
até os rádios, não a ferramenta.

## Leituras de equipamento parado

Equipamento parado — carregadeira no banco, caminhão na fila, ERM no poste
— dava uma leitura a cada 10 s no mesmo lugar, e cada uma pesava nos
percentuais, nas medianas e no mapa como um trecho de estrada medido. Na
coleta de 26/09 (13:38–13:54) eram **46% das leituras em 5,8 GHz e 56% em
2,4 GHz**.

Agora fica **uma leitura por parada**: a leitura é descartada quando o
rádio está a menos de **10 m** da última leitura guardada dele **e** a
menos de **3,6 km/h** (1 m/s). A velocidade separa o parado do caminhão
que anda devagar — esse, com leituras a 5 m, não está no mesmo ponto. Sem
velocidade (MeshMapper), ela sai do deslocamento desde a leitura anterior.
Compara-se com a última GUARDADA, e o caminhão que volta ao mesmo lugar
mais tarde conta de novo.

Vale na coleta ao vivo (o parado também não recebe trace, que só
ocuparia a tarefa do rádio) e nos arquivos do MeshMapper, antes do banco:
mapa, PPT e Excel usam o mesmo conjunto. O quadro da coleta mostra quantas
foram descartadas ("Parados"), e o fim da coleta também.

## Coleta ao vivo: o enlace de saída

O laudo **não usa custo** — nem do enlace, nem do caminho. O TRACE fica
só para saber **por qual vizinho o rádio sai**: nos dados do CA-1006, o
vizinho de sinal mais forte era a saída real em só 5 de 53 pontos. É o
que o MeshMapper grava em `traceInfo`.

A coleta faz o trace em cada leitura, na mesma sessão e no mesmo instante
do State (nada de defasagem), até o destino da tela — padrão
`10.188.96.11`, o mesmo dos seus arquivos do MeshMapper. Com ele, cada
amostra traz, como no arquivo:

| campo | de onde |
|---|---|
| RSSI do enlace, SNR, banda, canal | o enlace de **saída** |
| Servidor | o vizinho por onde saiu |

Sem trace (rádio que recusa, rádio sem rota, ou caixa desmarcada), a
amostra sai como antes — vizinho de sinal mais forte.

**A saída tem de ser a nossa.** O rádio roda uma tarefa por vez, de
qualquer sessão; se o MeshMapper ou o BC|Commander pedirem um TRACE no
mesmo rádio no meio do nosso, a saída que chega pode ser a deles. A coleta
confere que a saída é da **nossa tarefa** e do **nosso destino**; se não
for, conta como TRACE ocupado e a próxima leitura tenta de novo.

Ao rádio são pedidas só duas tarefas: o TRACE e o IPERF3 (abaixo). As
outras que a mesma mensagem da API aceita (REBOOT, ZEROIZE, CLEAR, KICK,
INSTALL…) não existem no código — a ação é sempre uma constante, e há
teste que confere.

## Testes ativos: latência, perda e throughput

Na aba **Coleta**, linha **Testes ativos**.

**Latência e perda (ping)** — ligado por padrão. O computador da coleta
manda um ping a cada 2 s para cada equipamento (leve: um pacote pequeno
por vez). Cada amostra leva a **mediana da latência e a perda dos pings
dos 10 s anteriores**, e as duas viram aba no KMZ, página no PPT e coluna
no Excel, contra o requisito Modular (latência < 100 ms, perda < 2%).
Mede o caminho do computador até o equipamento: com o computador na rede
do servidor, é o mesmo caminho que o Dispatch usa. Não precisa de
administrador no Windows.

Equipamento que não responde a **nenhum** ping a coleta inteira, mas
responde à BCAPI, bloqueia ICMP: fica sem latência e sem perda (não
medido) e o andamento avisa — "100% de perda" nele seria mentira.

**Throughput (iperf3)** — desligado por padrão. É o mesmo "Test
Performance" do BC|Commander: o **próprio rádio** roda o iperf3 até o
servidor escolhido (outro BreadCrumb da lista — ERB/ERM aparecem primeiro
— ou o IP de um servidor iperf3).

Os testes rodam **em paralelo e fora das leituras**: cada um na sua
thread e na sua sessão com o rádio, e a coleta segue lendo todos os
equipamentos — inclusive o que está em teste — enquanto o iperf3 roda.
Como o teste pesa na malha, vários ao mesmo tempo só entre equipamentos
**longe um do outro**:

| regra | padrão | por quê |
|---|---|---|
| **testes simultâneos** | 3 | ajustável na tela |
| **distância mínima** entre dois em teste ao mesmo tempo | 300 m | perto, dividem o mesmo canal: um mediria o outro, e a latência de quem está em volta subiria |
| dois ao mesmo tempo **nunca pela mesma ERM** | — | carregariam o mesmo enlace |
| **intervalo entre um início e o próximo** | 10 s | espalha a carga |
| cada rádio no máximo a cada | 5 min | o teste se espalha pela frota |
| **duração** | 5 s | ajustável |
| sentido | subida, descida ou os dois (alternando por rádio) | |
| **pausa** com a latência mediana do ping acima de 200 ms | — | rede carregada: o teste só pioraria |
| durante o teste, o rádio fica **sem TRACE** | — | o rádio roda uma tarefa por vez |
| **espera pelo resultado** depois do fim do teste | 30 s + 2× a duração | o rádio demora para montar a saída (9 s num teste de 5 s; mais de 15 s num de 10 s); passou disso, "indisponível: UNAVAILABLE" |

**O ping do rádio em teste não conta** na latência e na perda dele (nem
na mediana que decide a pausa): ele mediria o próprio teste. As amostras
dele nesse intervalo ficam com a latência dos pings de fora do teste.

Quem recusa o teste sai dele (o andamento diz o motivo); com 4 rádios
recusando e nenhum sucesso, o throughput desliga para a coleta toda — o
problema aí é o servidor ou o papel, não um rádio. O resultado de cada
teste é o que **chegou** do outro lado, em Mbps, com a posição do veículo
no início do teste e por onde ele saía.

Onde aparece: um ponto por teste no KMZ (pasta "Throughput (iperf3)",
azul na subida, laranja na descida), uma página por área no PPT — com o
mapa sobre o tif da área quando há —, e a aba **Throughput** do Excel.
Não há requisito de throughput no contrato, então nada ali é "dentro" ou
"fora": é o medido.

**Testar iperf** (botão ao lado) faz um teste num rádio selecionado na
lista e grava `teste_iperf_<ip>_<data>.txt` com tudo o que o rádio
respondeu. O rádio **não** devolve o texto do iperf3: devolve a mensagem
`Iperf3Result` da rajant-api (confirmado no CA-1017 em 09/11/2026) —
quem enviou e quem recebeu, a taxa (bits/s), retransmissões, jitter e
perda de cada lado, mais latência e utilização. O arquivo mostra essa
mensagem já legível. Jitter e perda só valem em UDP e retransmissões só
em TCP: o que o modo não mede fica vazio, não zero. Pela linha de comando:

```
site_survey.exe --testar-iperf 10.188.99.6 --iperf-servidor 10.188.96.30 --senha <senha>
```

**A falha é decidida rádio a rádio.** Na v16, a primeira falha de trace de
qualquer rádio, antes de algum sucesso, desligava o trace da coleta
inteira: com 48 rádios lidos ao mesmo tempo, um rádio sem rota ou lento
derrubava o de todos — foi o "trace desligado" da primeira coleta. Agora:

| situação | o que acontece |
|---|---|
| o rádio **recusa** a tarefa | só ele sai do trace; os demais seguem |
| **TRACE ocupado** ("task TRACE is already running") | só aquela amostra fica sem; a próxima leitura tenta de novo — nunca tira o rádio |
| 3 falhas seguidas sem nunca ter dado certo | só ele sai do trace |
| falha avulsa num rádio que já deu certo | só aquela amostra fica sem |
| 5 rádios fora e **nenhum** sucesso na coleta | trace desligado para todos (papel sem permissão ou destino errado) |

**TRACE ocupado.** O rádio roda uma tarefa por vez (`State.task`), de
qualquer sessão. Ela fica ocupada por um TRACE nosso que passou do prazo,
pelo de uma sessão que caiu no meio, ou pelo de outro cliente ligado no
rádio — MeshMapper, BC|Commander. Na v17 isso contava como recusa, e
EH-6001, CA-1013, CA-1020 e EH-6102 saíram do trace na primeira vez. Se
um rádio ficar ocupado 10 leituras seguidas sem nenhum sucesso, a tela
avisa uma vez: é outro cliente tracejando nele o tempo todo.

A sessão derrubada agora **fecha o socket**. A `rajant-api` não tem
`close()`, e soltar a sessão deixava-a aberta no rádio, com o TRACE dela
ocupando a tarefa. E a sessão se identifica no login como `site_survey`
(campo `userAgent` do `Auth`): no rádio, em `State.adminSessions`, dá
para separar a sessão desta ferramenta da de um MeshMapper.

Cada motivo novo aparece uma vez no andamento, com o nome do rádio — e,
quando o rádio responde algo sem enlace de saída, o começo do texto que
ele mandou.
A linha de status mostra `trace N ok · K ocupado · M rádio(s) sem`; no
fim da coleta sai o total e os motivos mais frequentes (agrupados sem o
número da sessão, que o rádio põe na mensagem e muda a cada vez).

## Usuário e senha

O campo **Usuário** é o papel da BCAPI: `VIEW`, `CO`, `ADMIN`, `LOCAL`
ou `JOIN`. Pode ser digitado em minúsculo — o programa passa para
maiúsculo. Até a v17 não passava: a `rajant-api` procura o papel num
dicionário só com maiúsculas, `co` dava erro dentro dela, e ela devolvia
só "falhou" — a busca mostrava "autenticacao falhou" nos 156 rádios com a
senha certa.

Agora a falha de login diz o motivo:

| mensagem | o que é |
|---|---|
| `usuário 'X' não existe na BCAPI — use VIEW, …` | nome de papel errado; dito antes de ir à rede |
| `o rádio recusou o login de CO (…) — confira usuário e senha` | senha errada, ou papel sem acesso naquele rádio; entre parênteses, o que o rádio respondeu |
| `…; a senha digitada tem espaço no começo ou no fim` | espaço colado junto com a senha |
| `sem resposta no login: a conexão TLS não completou em 5 s` | rede até o rádio, ou o rádio sem atender a BCAPI |
| `sem resposta no login: conectou, mas o rádio não mandou o desafio` | o rádio aceitou a conexão e não seguiu — sessões demais abertas nele, ou rádio sobrecarregado |
| `sem resposta no login: o rádio não respondeu à senha` | parou depois da senha enviada |

O prazo de cada etapa é **5 s** (a biblioteca usa 2 s fixos, com o aperto
de mão TLS dentro — pouco para rádio a vários saltos). Se o login desta
ferramenta falhar por rede, ela tenta o `authenticate()` da biblioteca
antes de desistir: se a biblioteca entrar, a coleta segue e o log registra
— é a proteção contra um defeito no login daqui.

No fim da busca, o andamento lista os motivos mais frequentes: "156x o
rádio recusou o login" numa linha, em vez de 156 linhas vermelhas.

**A leitura também ficou mais leve.** A `rajant-api` lia a resposta do
rádio com um único `recv`: resposta maior que um registro TLS (~16 KB),
comum em rádio com muitos vizinhos, falhava como "sem resposta". E
pedia o State inteiro, configuração incluída, com um ping antes de cada
leitura. Agora a mensagem é lida até o fim, sem ping, e só com GPS, rádio
e sistema. Se o rádio não entender o filtro, volta sozinha ao State
inteiro; se a leitura direta não funcionar, volta ao modo da biblioteca.

**Nome dos vizinhos.** O rádio não informa o nome de cada vizinho — só o
IP (nem sempre) e o *encap*, que é o final do número de série. Até a v14
o vizinho entrava com o **IP** no lugar do nome, e a eleição da melhor
ERB/ERM, que reconhece infraestrutura pelo nome, nunca achava nenhuma: o
mapa ao vivo saía com o melhor vizinho de qualquer tipo, caminhão
incluído. Agora o nome vem da busca (nome e serial de cada rádio que
respondeu) e da lista de 156 rádios da mina embutida no programa.

### Teste de campo

Feito em 26/09/2026 no rádio 10.188.99.4, com o papel VIEW:

| o que | resultado |
|---|---|
| autenticação | ok, 270 ms |
| State inteiro | 9318 bytes na rede, 221 ms |
| filtro `gps` (e `/gps`) | **funciona**: 2307 bytes, só GPS, rádio e sistema |
| filtro `State.gps`, `state.gps` | vazio |
| TRACE | aceito com VIEW e com CO; pronto em ~0,8 s |
| saída do TRACE | **texto** do `imtrace` em gzip — não a mensagem binária |

O diagnóstico grava o **ping** antes do login (o `authenticate()` da
biblioteca fazia por dentro, e na v18 sumiu: um rádio fora de alcance
virava "sem resposta no login"), o tempo de cada etapa do login e, se o
login daqui falhar, o resultado do da biblioteca.

No 10.188.99.9 (CO), com a coleta rodando nele ao mesmo tempo, o TRACE
voltou "task TRACE is already running": a tarefa era da coleta. O
diagnóstico agora espera até 10 s pela tarefa livre e grava de quem ela
é (sessão, endereço, idade, cliente).

A saída daquele rádio: saída pela `wlan0wds50` em 2,4 GHz canal 6,
−42 dBm, SNR 53. O interpretador foi escrito e testado com esses bytes
exatos. A linha crua do rádio, gravada no arquivo do diagnóstico, traz
também o custo; a parte interpretada, não.

Para repetir o teste em outro rádio:

1. Na aba **Coleta**, **Procurar equipamentos** e **clicar no nome** de
   um rádio da lista (não na caixinha de marcar).
2. **Testar no selecionado** (ao lado do destino do trace).
3. Sai um `teste_trace_<ip>_<data>.txt` na pasta de saída. Mande o
   arquivo.

Ou pela linha de comando:

```
site_survey.exe --testar-trace 10.188.99.6 --senha <senha> --role co
```

Só leitura: o teste pede o State e uma consulta de rota (TRACE). As outras
tarefas que a mesma mensagem da API aceita — REBOOT, ZEROIZE, CLEAR — não
existem no código.

## Entradas aceitas

| arquivo | observação |
|---|---|
| `Meshmapper_*.kmz` | o melhor: traz altitude e contagem de vizinhos ativos |
| `data.json` | o mesmo conteúdo, se extraído do KMZ |
| `Meshmapper_*_trace_path.csv` | o par de CSVs; o `_peer_info.csv` é lido junto |

## Saídas

| arquivo | o que tem |
|---|---|
| `Survey_*_24GHz.kmz` | trajeto de 2,4 GHz, uma aba por grandeza (com uma pasta por área), a pasta Áreas e a legenda na tela |
| `Survey_*_58GHz.kmz` | idem, 5,8 GHz |
| `mapas/*.tif` | com GeoTIFF nas áreas: o mapa de cada área, grandeza e banda sobre a imagem, georreferenciado |
| `Site_Survey_*.pptx` | capa, índice, sumário, metodologia, resumo por área, uma seção por área (com uma página por grandeza/banda e a de throughput) e duas páginas em branco para diagnóstico e conclusões |
| `Survey_*.xlsx` | origens, resumo, por grandeza, por área, throughput e amostras |
| `Cobertura_Estimada_*.pptx`, `estimativa/*.tif` | com LAS nas áreas: a cobertura estimada pelo relevo (ver acima) |

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

Com áreas demarcadas, cada aba tem uma pasta por área.

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

## O mapa: calor por faixa

O mapa abre na aba **RSSI**. A aba de enlaces bons pela régua de custo
da Rajant saiu do laudo junto com o custo.

**O calor:** a contagem de cada faixa é suavizada em ~4 m antes de
escolher a cor — a fronteira entre faixas sai sem lascas — e a borda
esfuma em ~6 m. A cor continua sendo a faixa da maioria, nunca uma média.
Com áreas, o calor de cada área é **recortado no polígono**: não
transborda para a área vizinha nem pinta o buraco.

O KMZ abre no **mapa de calor por faixa**, no estilo do KMZ de referência
de 27/09/2026:

- **Cor = a faixa da legenda com mais trecho medido num raio de 40 m.**
  O trecho é o caminho do veículo entre duas leituras, e cada leitura é
  dona da metade dele até a vizinha — nenhum valor é inventado entre duas
  leituras. Contar só as leituras deixava o mapa em contas soltas: depois
  do descarte dos parados, um caminhão a 40 km/h fica com leituras a
  20–100 m uma da outra. Empate vai para a faixa pior.
- **Transparente** onde não há leitura por perto; **cinza claro** onde só
  há leitura sem valor naquela grandeza. A cor é a da
  régua, exata — só a transparência varia —, com sombra suave fora da
  mancha para ela se separar do chão da cava.
- **ERB/ERM ficam fora do calor** (a leitura delas é o enlace de uma torre
  com outra) e aparecem como **quadrados** na cor da mediana das leituras
  de cada uma; o nome aparece ao passar o mouse, e o balão diz por onde ela
  sai.
- **Uma grandeza por vez**: no painel, RSSI, SNR e Ruído são botões de
  rádio — ligar uma desliga a outra.
- **Legenda em cartão escuro na tela**, com a banda, a linha do requisito
  entre as faixas que atendem e as que não, e o cinza.
- **Trilha** (uma linha por caminho) e **Medições** (um ponto por leitura,
  com balão) continuam na aba, desligadas, para consulta.
- **Sem barra de tempo**: as leituras não levam TimeStamp — com ele, o
  Google Earth escondia o que ficava fora do intervalo escolhido. A hora
  está no balão.
- **Ícones dentro do KMZ**: os do servidor do Google não abrem na mina sem
  internet. O arquivo abre já enquadrado na área medida.


## A régua de cores: de onde vem cada valor

A fonte de cada régua vai impressa no rodapé da legenda. Os cortes sem
fonte publicada estão marcados como tal: só dividem faixas que ficavam
largas demais para o que o rádio mede.

**Cores** — as oficiais do Rajant MeshMapper, lidas do `doc.kml` que o
BC|Commander 11.29.1 gerou na mina: `FF0000` (poor), `F26A00` (good),
`3EAD30` (great). O amarelo `DD9F17` e o vermelho-escuro `B70404` são os
tons dos alfinetes do mesmo MeshMapper. Três tons não vêm da Rajant, todos
nas pontas: `5C0000` (o vermelho dela escurecido), `1E6B1E` (o verde dela
escurecido, a melhor faixa do RSSI) e `4A0E4E` (roxo-escuro, a pior do
RSSI — um terceiro vermelho escuro não se distinguia sobre a imagem da
cava). SNR e ruído usam só cores que o RSSI também usa.

**RSSI (dBm)** — o MeshMapper **não** classifica o Signal: no balão dele o
dBm sai sem cor (conferido em 11.216 células). A régua é a convenção de
Wi-Fi mais citada, MetaGeek/Oscium (−67 muito bom, mínimo para voz e
vídeo, a mesma borda de célula do guia de site survey da Cisco; −70 ok;
−80 conectividade básica; −90 inutilizável), mais o −75 do requisito
Modular. Os cortes em −85 e −60 não têm fonte: dividem as duas faixas mais
largas onde o rádio de fato mede. Nos arquivos do MeshMapper da mina o
melhor vizinho fica entre −84 e −86 dBm em 5,8 GHz e entre −61 e −53 dBm
em 2,4 GHz — antes, tudo isso saía em duas cores só.

| RSSI (dBm) | cor | leitura |
|---|---|---|
| ≥ −60 | verde-escuro `1E6B1E` | excelente |
| −67 a −61 | verde `3EAD30` | muito bom |
| −70 a −68 | amarelo `DD9F17` | ok |
| −74 a −71 | laranja `F26A00` | atende o requisito |
| **−80 a −75** | vermelho `FF0000` | **reprovado** |
| −85 a −81 | `B70404` | fraco |
| −89 a −86 | `5C0000` | muito fraco |
| ≤ −90 | roxo `4A0E4E` | inutilizável |

O RSSI sozinho não diz se o enlace funciona: depende do ruído da banda. Na
mina o piso medido é −107 a −109 dBm em 5,8 GHz e −93 a −98 dBm em
2,4 GHz, então −86 dBm em 5,8 GHz ainda chega com SNR 23. Para a
qualidade do enlace, o mapa que vale é o de SNR; o de RSSI é o do
requisito Modular.

**SNR (dB)** — parte da régua **oficial da Rajant**, gravada pelo
MeshMapper no `data.json` (`goodRSSI = 20`, `greatRSSI = 30`; no
vocabulário Rajant "RSSI" é o SNR em dB) e conferida célula a célula nos
seus arquivos. Os dois cortes dela continuam; entre eles e abaixo entram
os da Cisco Meraki (alertas de SNR em 10 e 15 dB; 25 dB como sinal bom).
Nenhum atravessa um corte da Rajant nem o requisito Modular (> 20).

| SNR (dB) | cor | leitura |
|---|---|---|
| ≥ 30 | verde `3EAD30` | ótimo |
| 25 a 29 | amarelo `DD9F17` | bom |
| 21 a 24 | laranja `F26A00` | atende, pouca margem |
| **15 a 20** | vermelho `FF0000` | **reprovado** |
| 10 a 14 | `B70404` | ruim |
| ≤ 9 | `5C0000` | muito ruim |

**Ruído (dBm)** — não existe régua oficial. É **derivado** das duas acima:
um sinal no mínimo Modular (−75 dBm) sobre um ruído N chega com
SNR = −75 − N, e leva a cor desse SNR. Cada corte de SNR vira um de ruído.

| Ruído (dBm) | cor | um sinal de −75 dBm chegaria com |
|---|---|---|
| ≤ −105 | verde | SNR ≥ 30 |
| −104 a −100 | amarelo | SNR 25 a 29 |
| −99 a −96 | laranja | SNR 21 a 24 |
| **−95 a −90** | vermelho | **SNR 15 a 20** |
| −89 a −85 | `B70404` | SNR 10 a 14 |
| ≥ −84 | `5C0000` | SNR ≤ 9 |

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
o requisito Modular Mining, as áreas demarcadas (e quantas leituras
ficaram fora delas) e o que **não** foi medido — mais essa mesma régua de
cores com a classificação de cada faixa.

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

Latência, perda de pacotes, interferência e throughput — na coleta ao
vivo, as quatro são medidas. Grandeza sem medição **não vira slide nem
aba**: uma página com escala, requisito e gráfico vazio lê-se como *"medi
e deu tudo fora"*, que é o oposto de *"não medi"*.

## Instalação

```
build\gerar_exe_site_survey.bat
```

Gera `dist\site_survey\site_survey.exe`. A pasta `marca/` fica ao lado do
executável — sem ela o deck sai sem logo, em vez de falhar.

O build **falha se o exe não for abrir**: o último passo roda
`site_survey.exe --verificar`, que confere módulos, tcl/tk e dependências
sem abrir janela — inclusive a conversão de coordenadas do GeoTIFF, que
no exe depende dos dados de projeção irem junto. Se algo der errado depois, rode isso no Prompt e mande
a saída; o erro também fica em `site_survey_erro.txt`, ao lado do exe.

Para rodar direto pelo Python:

```
pip install matplotlib numpy scipy python-pptx openpyxl lxml rasterio "laspy[lazrs]"
pip install rajant-api --no-deps && pip install "protobuf==4.23.4"
python site_survey.py
```

## Arquivos

| | |
|---|---|
| `site_survey.py` | a janela: só a interface |
| `coleta_rajant.py` | a coleta ao vivo dos rádios |
| `survey_meshmapper.py` | a leitura de arquivos do MeshMapper |
| `motor_survey.py` | o motor de KMZ, PPT e Excel, a leitura dos rádios e das áreas, o ping e o iperf3 |
| `cobertura_estimada.py` | a cobertura estimada pelo relevo (LAS) |
| `infraestrutura.csv` | as ERB/ERM da estimativa (criada pelo programa; você completa a altura do mastro) |
| `marca/` | logos usados nos decks |
| `exemplos/` | uma captura de veículo e uma de repetidora, usadas pelos testes |
| `build/` | spec do PyInstaller e o `.bat` |

O `motor_survey.py` é o motor do survey, separado do monitor
(`rajant_monitor.py`, o exportador para o Grafana): os dois projetos não
dependem um do outro.
