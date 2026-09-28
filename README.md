# rajant_monitor

Exportador Prometheus, gerador de relatórios e ferramenta de site survey para
malha Rajant BreadCrumb em mina a céu aberto (~150 nós).

Binário único: `rajant_monitor.py`. Sem framework, sem serviço externo além do
Prometheus. Roda em rede isolada.

## Por onde começar

```bash
python3 rajant_monitor.py                      # sobe exportador + página web
python3 rajant_monitor.py --testar-fundo       # confere o fundo dos mapas
python3 teste_parser.py                        # 629 testes
```

A página web fica em `http://<servidor>:<porta_relatorio>/` com quatro abas:
**Relatórios**, **Survey**, **Histórico** e **Medições**.

## O que tem aqui

| arquivo | o que é |
|---|---|
| `rajant_monitor.py` | tudo: coleta, métricas, relatórios, survey, página web |
| `survey_meshmapper.py` | gerador de relatório a partir da captura do MeshMapper (não usa rede) |
| `teste_parser.py` | 629 testes; roda sem rádio e sem a lib `rajant_api` |
| `ARQUITETURA.md` | como funciona por dentro: camadas, threads, banco, invariantes |
| `AUDITORIA_METRICAS.md` | as ~103 métricas conferidas campo a campo contra os `.proto` |
| `SITE_SURVEY.md` | o módulo de survey: captura, análise, PPT, KML |
| `bcapi-ref/proto/` | os `.proto` do bcapi, referência de tudo que se afirma sobre a API |
| `grafana/` | 7 dashboards em JSON + gerador e validador |
| `painel/` | dashboard HTML/CSS/JS próprio, zero dependência externa |
| `marca/` | logos Anglo American usados nos decks gerados |

## Regras que atravessam o código

Três decisões explicam a maior parte das escolhas:

**1. Nunca publicar 0 no lugar de "não medi".** Zero é indistinguível de
"medido e deu zero". Campo sem medição vira `None` e a série é omitida. Foi o
que fazia painel de erro de ethernet e CPU parecerem saudáveis.

**2. Nada se afirma sobre a BC API sem conferir o `.proto`.** A auditoria
removeu 19 séries que não tinham campo correspondente e corrigiu conversões
que estavam erradas havia tempo (SNR, GPS em NMEA, temperatura). Há teste que
falha se um `.proto` passar a expor varredura de espectro.

**3. Mapa errado é pior que mapa sem fundo.** A imagem de fundo é posicionada
pelo retângulo *dela*, não pelo dos dados; sem georreferência ela é recusada.
Mapa esticado mente sobre distância.

## Configuração

`config.ini` é criado no primeiro uso com os padrões comentados. As seções que
mais importam:

```ini
[coleta]
intervalo_moveis_segundos = 20     # coleta acelerada só dos móveis

[survey]
max_threads         = 12           # teto de consultas simultâneas
min_intervalo_s     = 5            # piso do intervalo pela página
piso_continuo_s     = 0            # 0 = contínuo sem pausa alguma
ping_a_cada_s       = 15           # ping tem cadência própria, não a do ciclo
ping_max_por_ciclo  =              # vazio = teto de threads
falhas_para_pular   = 3            # desiste do rádio após N falhas
usar_cache_fallback = true

[relatorio]
survey_kmz   = /caminho/levantamento.kmz    # fundo georreferenciado
fundo_local  = orto.png                     # alternativa; exige fundo_bbox
fundo_bbox   = -27.7250,-27.7400,-50.0580,-50.0760
zonas_grade_m = 50
imagens_no_ppt = false             # false = molduras vazias p/ colar print
kmz_com_equipamentos = false       # true = devolve os alfinetes dos BCs
```

## Duas ferramentas

O projeto tem **dois executáveis**, com públicos e dependências distintos:

| | `rajant_monitor` | `survey_meshmapper` |
|---|---|---|
| o que faz | sonda a malha, exporta métricas, gera o semanal | lê a captura do MeshMapper e gera o relatório de survey |
| precisa de rede? | sim, fala com os BCs | **não** |
| precisa da `rajant-api`? | sim | **não** |
| onde roda | servidor, o tempo todo | notebook de quem mediu |

```bash
survey_meshmapper Meshmapper_2026-09-10_12-59-11.kmz
```

Saem três arquivos: o **KMZ** com o trajeto colorido, o **PPT** na
identidade Anglo e o **Excel** — este com uma aba que só existe aqui, a de
todos os vizinhos visíveis ponto a ponto.

### Por que ler o arquivo em vez de sondar

O MeshMapper é a ferramenta da própria Rajant, roda no notebook dentro do
veículo e grava **a cada segundo** a posição e todos os vizinhos visíveis.
Isso resolve de uma vez o que a sondagem por API não resolvia:

- a captura é contínua de verdade, sem um segundo sondador competindo com
  o Dispatch na malha que se está medindo;
- traz **todos** os peers por ponto, não só o que atendeu — dá para dizer
  *"estava ligado no X e havia um Y melhor ao lado"*;
- quem gera o relatório não precisa de rede, credencial nem biblioteca.

### Qual RSSI o laudo reporta

São **duas perguntas diferentes** e dois números diferentes:

| pergunta | o que se mede |
|---|---|
| *"existe sinal servível aqui?"* | o melhor vizinho de **infraestrutura** (ERB/ERM) visível no ponto |
| *"a aplicação funcionou aqui?"* | o enlace que o **InstaMesh usou** |

**O site survey reporta a primeira.** Ele caracteriza o terreno: quanto
sinal há naquele ponto da mina. Um caminhão passando dá enlace ótimo e
vai embora no minuto seguinte; só a infraestrutura caracteriza cobertura.

A eleição acontece **dentro da banda da amostra**. No recorte do arquivo
do cliente o mesmo ponto enxerga:

| vizinho | banda | RSSI |
|---|---|---|
| ERM-28 PTP | 2,4 GHz | −71 dBm |
| ERM-09 PTP CAM | 5,8 GHz | −86 dBm |
| enlace que atendeu (`wlan0wds33`) | 5,8 GHz | −89 dBm |

A amostra é de 5,8 GHz — é a banda do enlace que atendeu —, então o mapa
de 5,8 GHz reporta os **−86 dBm**, não os −71 dBm de 2,4 GHz. Separar as
duas bandas em arquivos não adianta se a leitura de uma entra no mapa da
outra; 15 dB é a distância entre aprovado e reprovado.

Ponto sem nenhum vizinho na banda da amostra fica **sem cor** — buraco no
mapa lê-se como "não medi", pintado com a outra banda lê-se como medição.

> A divergência de **22 dB na mediana** entre cobertura e enlace, citada
> antes a partir da captura completa do CA-1006, foi apurada **antes**
> deste recorte por banda e portanto misturava as duas. No recorte de 8
> pontos o delta por banda cai para 3 dB. O número da campanha inteira
> precisa ser reapurado sobre o arquivo completo, que não está aqui.

O enlace que atendeu não vira camada no mapa — duas abas "RSSI" no
Google Earth obrigavam a lembrar qual era qual. Ele fica na aba
**Amostras** do Excel, coluna *RSSI do enlace (dBm)*, ao lado da *Δ não
usado (dB)*, que é onde a divergência entre os dois vira diagnóstico.

Por que **infraestrutura** e não o vizinho mais forte: o mais forte é
quase sempre outro caminhão encostado (−40 dBm no arquivo do cliente).
Ele some quando o caminhão sai, então não caracteriza cobertura da área.
Quando não há nenhum ERB/ERM visível, cai para o melhor vizinho qualquer
— **e o relatório diz isso**, em vez de apresentar veículo de passagem
como cobertura.

É a **primeira aba** do KMZ e a única que nasce visível. Abrir o laudo
pelo enlace entregue faz o leitor concluir "falta rádio" onde o problema
é outro.

O critério de infraestrutura é o padrão `^\s*(ERB|ERM)\b` e vai escrito no
Excel, ao lado dos números.

### O que o MeshMapper não fornece

Latência, perda de pacotes e interferência **não vêm** no arquivo. Elas não
viram slide nem aba: página com escala e requisito e gráfico vazio lê-se
como *"medi e deu tudo fora"*, que é o oposto de *"não medi"*.

### Armadilhas do formato, conferidas no arquivo real

- **`RSSI (SNR)` é SNR em dB; `Signal` é o RSSI em dBm.** Os nomes das
  colunas trocam os dois — trocá-los inverteria a escala inteira.
- **O ruído não vem, mas é recuperável:** `ruído = signal − snr`. Não é
  estimativa: em 5450 amostras deu 6 valores distintos, agrupados em
  −109 dBm (5,8 GHz) e −94 dBm (2,4 GHz), que é o piso que o rádio usou
  para calcular o SNR.
- **`Rate (Kb/s)` traz 65, 130, 195, 260** — as taxas MCS de 802.11n em
  **Mbps**. O rótulo da coluna está errado.
- **Ponto sem enlace** vem com tipo `N/A` e custo 2147483647 (INT_MAX).
  Vira amostra *sem sinal*, não amostra com sinal ruim.

## O KMZ: mapa de calor por faixa

**A aba que abre: Enlaces bons (Rajant).** É a régua do MeshMapper da
Rajant, conferida no KMZ oficial de 28/09/2026: em cada ponto, quantos
enlaces são **bons** — custo ≤ 10000 **e** SNR ≥ 20 — ou **ótimos** —
custo ≤ 5000 **e** SNR ≥ 30 —, somando todas as WLANs do rádio, como o
MeshMapper conta. Nos arquivos do MeshMapper vale a régua gravada no
próprio arquivo (`configuration`). Na aba:

- **calor** pela contagem: 0 vermelho, 1 laranja, 2 amarelo, 3+ verde
  (as cores medidas nos pinos oficiais);
- **pinos numerados**, como os do MeshMapper — gota com o número de
  enlaces bons, estrela com o de ótimos, "!" sem vizinho. Um pino a cada
  ~30 m de estrada, mostrando uma leitura real do trecho (a do meio, pelo
  lado pior); o balão lista os enlaces bons pelo nome, com custo e SNR.
  Um pino por leitura, com 29 veículos, viraria um tapete por cima do
  calor;
- **linha do trajeto colorida pelo custo do caminho**, como o Trace Path
  do MeshMapper (≤ 10000, ≤ 20000, acima).

No KMZ oficial de 28/09 (CA-1024), o gerador daqui conta os mesmos pinos
que a Rajant desenhou: 25 com 1, 12 com 2 e 4 com 0.

RSSI, SNR, Ruído e Custo do caminho seguem como abas, cada uma com o seu
calor. **O calor ficou mais limpo:** a contagem de cada faixa é suavizada
em ~4 m antes de escolher a cor — a fronteira entre faixas sai sem lascas
— e a borda esfuma em ~6 m. A cor continua sendo a faixa da maioria,
nunca uma média.

O KMZ abre no **mapa de calor por faixa**, no estilo do KMZ de referência
de 27/09/2026:

- **Cor = a faixa da legenda com mais trecho medido num raio de 40 m.**
  O trecho é o caminho do veículo entre duas leituras, e cada leitura é
  dona da metade dele até a vizinha — nenhum valor é inventado entre duas
  leituras. Contar só as leituras deixava o mapa em contas soltas: depois
  do descarte dos parados, um caminhão a 40 km/h fica com leituras a
  20–100 m uma da outra. Empate vai para a faixa pior.
- **Transparente** onde não há leitura por perto; **cinza claro** onde só
  há leitura sem valor (no custo do caminho: sem trace). A cor é a da
  régua, exata — só a transparência varia —, com sombra suave fora da
  mancha para ela se separar do chão da cava.
- **ERB/ERM ficam fora do calor** (a leitura delas é o enlace de uma torre
  com outra) e aparecem como **quadrados** na cor da mediana das leituras
  de cada uma; o nome aparece ao passar o mouse, e o balão diz por onde ela
  sai.
- **Uma grandeza por vez**: no painel, RSSI, SNR, Ruído e Custo do caminho
  são botões de rádio — ligar uma desliga a outra.
- **Legenda em cartão escuro na tela**, com a banda, a linha do requisito
  entre as faixas que atendem e as que não, e o cinza.
- **Trilha** (uma linha por caminho) e **Medições** (um ponto por leitura,
  com balão) continuam na aba, desligadas, para consulta.
- **Sem barra de tempo**: as leituras não levam TimeStamp — com ele, o
  Google Earth escondia o que ficava fora do intervalo escolhido. A hora
  está no balão.
- **Ícones dentro do KMZ**: os do servidor do Google não abrem na mina sem
  internet. O arquivo abre já enquadrado na área medida.


### A régua de cores

Cores do Rajant MeshMapper; faixas de RSSI da convenção MetaGeek/Oscium
mais o −75 Modular; SNR pela régua oficial da Rajant (20/30); ruído
derivado das duas; custo do caminho (*Trace Path Cost*) pela régua oficial
da Rajant (10000/20000). Tabelas e fontes em `LEIAME_SURVEY.md`.

O rádio reporta inteiro e o requisito é estrito (RSSI **> −75**): o −75
exato é reprovado e sai vermelho. Cada faixa contém só aprovados ou só
reprovados — há teste percorrendo todos os inteiros de cada grandeza.

## Captura contínua

Pedindo intervalo **0**, o ciclo seguinte sai no instante em que o anterior
termina — sem espera nenhuma, com `piso_continuo_s = 0` (o padrão). A
cadência passa a ser só o tempo de ida e volta ao rádio. É o que aproxima o
traçado de uma linha em vez de uma sequência de pontos:

| intervalo | a 40 km/h, distância entre amostras |
|---|---|
| 60 s | ~660 m |
| 20 s | ~220 m |
| 1 s | ~11 m |

Não é grátis: cada amostra é uma ida e volta ao rádio **pela própria malha
que se está medindo**. Com poucos veículos o custo é baixo e o ganho é
grande; com a frota inteira o ciclo já se alonga sozinho pelo teto de
threads — e é por isso que o aviso da página trata "consultas/s" no
contínuo como **teto**, não como taxa. O intervalo que de fato aconteceu
vai para `intervalo_efetivo_s` e para o relatório.

O único mínimo que resta são 50 ms, e não é freio de carga: é proteção
contra laço vazio. Se todos os rádios falharem na hora, sem ele o processo
giraria a 100% de CPU sem medir nada.

### O ping tinha de sair do caminho

Posição e RF (RSSI, SNR, ruído, interferência) vêm do `get_state`, que é
rápido. Latência e perda vêm do ICMP — e o **ping do Windows não aceita
intervalo**: `ping -n 4` espera ~1 s entre os envios e custa ~3 s por
rádio. Preso ao ciclo, ele impunha esse piso também à posição, que é o que
desenha o rastro.

Medido com rádio simulado, 20 ciclos:

| | por ciclo | a 40 km/h |
|---|---|---|
| ping em todo ciclo | 3,15 s | ~35 m entre amostras |
| `ping_a_cada_s = 15` | 0,30 s | ~3 m entre amostras |

Nos ciclos sem ping, `rtt` e `perda` saem **`None`** — não medido é `None`,
nunca a leitura anterior repetida. Carregar o último valor para a posição
nova inventaria medição onde não houve.

O status da captura publica `perfil` com `ciclo_s`, `ping_s` e
`consulta_s`: "está espaçado" sem número é chute.

**E a cadência por rádio não bastou.** Medido em campo com **159 rádios**:
o ciclo deu **46,3 s**. Como 46 s é maior que `ping_a_cada_s`, todo rádio
vivia vencido — o ping voltava a ser de todos, todo ciclo, e sozinho
respondia por ~90% do tempo (159 × 3 s ÷ 12 threads ≈ 40 s).

Por isso há um **orçamento**: no máximo `ping_max_por_ciclo` rádios por
ciclo, os mais atrasados primeiro. O custo do ping deixa de crescer com a
frota e passa a ser ~uma leva de threads:

| 159 rádios | ciclo | a 40 km/h |
|---|---|---|
| sem orçamento | ~44 s | ~485 m entre amostras |
| com orçamento | ~7 s | ~77 m |
| **só os 8 do trajeto** | **~3 s** | **~36 m** |

A última linha é a que importa: **selecione só os veículos do trajeto**.
Contínuo com a frota inteira é a ferramenta errada — o ciclo se alonga
sozinho pelo teto de threads, por mais que o ping saia do caminho.

## Dois relatórios

O survey virou entrega própria, separada do relatório semanal:

| deck | como gerar | conteúdo |
|---|---|---|
| **Site Survey** | `--ppt-survey ID`, botão **PPT** no histórico, `/survey/ppt` | capa, índice, sumário, zonas e uma página por grandeza/banda com moldura + gráfico |
| **Semanal** | aba Relatórios → PPTX, `/gerar?fmt=pptx` | tudo o mais; **sem** a seção de survey |

O semanal não só deixa de anexar o survey: ele **remove os slides de survey
que vêm no template**, senão ficariam em branco no arquivo, parecendo relatório
malfeito. O botão do menu passa a dizer *"5. Site Survey (relatório separado)"*
em vez de apontar para um slide que não existe mais.

Para voltar ao deck único:

```ini
[relatorio]
survey_no_semanal = true
```

### O que decide a fronteira

**O título do slide, não o texto dele.** Casar `"site survey"` em qualquer
lugar do slide levava junto o que só cita survey em prosa: o subtítulo de
*Links Críticos (SNR)* diz *"candidatos a realinhamento / site survey"*, e o
slide inteiro sumia do semanal.

E o título vem de `_titulo_do_slide()`, que é a caixa de texto mais alta
**que não é botão** — o ◂ MENU fica em 0,28" e o título em 0,32", ou seja, o
botão é mais alto que o título e viraria "o título" de todo slide.

**Origem do dado, não o nome da seção.** *Espectro por Canal* se chamava
*"5. Site Survey — Espectro por Canal"* e ia embora com a seção 5, mas seus
números vêm dos contadores do exporter — todos os rádios ativos, o período
inteiro. É saúde da malha: virou seção 4 e fica no semanal, que de outro
modo perdia o único gráfico de espectro que tinha.

## Identidade Anglo

Os dois decks saem na identidade Anglo American: fundo branco, azul
institucional `#031795`, Calibri, logo e régua em cada slide, capa azul com
o logo branco.

O deck de survey nasce assim. O semanal **não**: ele vem de um template do
cliente em azul-escuro, com menus, botões e campos preenchidos à mão. Trocar
esse arquivo por um template Anglo criaria dois templates para manter em
sincronia e jogaria fora o que já está digitado nele. Em vez disso o deck é
**repintado no fim da geração**, depois de preenchido — nada muda de lugar,
nada se perde, só a pele.

Repintar no fim tem um motivo: assim a passagem pega também o que o
relatório acabou de escrever (o verde/âmbar/vermelho do status) e os slides
técnicos gerados por código, que usam a mesma paleta do template. Uma
tabela de cores, dois produtores.

Isso só é seguro porque o template não deixa nada para herdar — conferido no
arquivo do cliente: 874 runs de texto e todos com cor explícita, master e
layout sem forma nenhuma, fonte já Calibri.

Dois detalhes que custaram teste:

- **Branco depende do que ficou atrás.** Na capa o fundo vira azul e o
  título segue branco; sobre um cartão que clareou, o mesmo branco tem de
  virar escuro ou o texto some. A decisão é por luminância do fundo *novo*.
- **Borda de célula não passa pelo python-pptx.** Ela mora em
  `lnL/lnR/lnT/lnB` dentro do `tcPr`. Sem tratá-la no XML sobravam 1248
  traços azul-escuros riscando o fundo branco.

Para sair no visual original do template:

```ini
[relatorio]
identidade_anglo = false
```

Os logos ficam em `marca/`. Sem essa pasta o deck sai sem logo — em vez de
estourar.

## Quem entra na coleta

Por padrão o exporter **descobre** a malha: parte dos seeds e segue os peers de
cada BC. Isso acha tudo que responde — inclusive rádio de teste e equipamento
de terceiro. Três chaves controlam isso:

```ini
[coleta]
descoberta       = true    ; false = só os IPs de [rede] seeds
usar_cache       = true    ; false = não lê nem escreve o cache em disco
somente_com_tag  = false   ; true  = só publica nome com prefixo de frota
```

**Lista fixa.** Com `descoberta = false`, ponha todos os IPs em
`[rede] seeds` (separados por vírgula) e o exporter coleta exatamente
aqueles — sem seguir peer nenhum, e sem o ciclo reintroduzir depois. Seed
que não responde **continua na lista**: um BC precisa seguir monitorado
justamente quando cai.

**Cache desligado.** Ele guarda o que a descoberta já viu um dia e ressuscita
endereço que saiu da rede, mantendo equipamento fantasma na métrica. Com lista
fixa, não serve para nada.

**Filtro por tag.** `somente_com_tag = true` descarta quem responde mas não tem
nome com prefixo de `[relatorio] prefixos_frota` (CA, PA, PF, TT, EH, ERM,
ERB…). O descarte é auditável: vai para o log e para `rajant_bc_sem_tag`. Se
`prefixos_frota` estiver vazio, o filtro se desliga sozinho em vez de descartar
tudo.

## Instalar a rajant-api

Ela declara três dependências, mas **só usa uma**:

```
grpcio == 1.56.2        declarada, NUNCA importada
grpcio-tools == 1.56.2  declarada, NUNCA importada
protobuf == 4.23.4      esta sim
```

Isso não é detalhe: `grpcio 1.56.2` não tem wheel para Python 3.12+, então
instalar as dependências declaradas falha tentando compilar C++. Instale assim:

```bash
pip install rajant-api --no-deps
pip install "protobuf==4.23.4"
```

Se `import rajant_api` falhar com `No module named 'google'`, é o protobuf que
falta — o `--no-deps` não o trouxe.

### O que a coleta de survey faz por cima dela

Lido no código da 0.1.1: `get_message()` lê a resposta com **um** `recv` —
pelo TLS, no máximo um registro de ~16 KB —, então rádio com muitos vizinhos
volta "sem resposta"; `get_state()` pinga o rádio antes de cada consulta; e
não há TRACE. A coleta de survey usa a sessão autenticada da biblioteca e o
mesmo enquadramento dela, mas lê a mensagem inteira, sem ping, pede só GPS,
rádio e sistema e faz o TRACE (custo do caminho e enlace de saída). Recua
sozinha para o `get_state()` da biblioteca se o modo direto não funcionar.
Detalhes em `ARQUITETURA.md`, "BCAPI direto"; teste de campo em
`LEIAME_SURVEY.md`.

Com a biblioteca instalada, a suíte roda também os testes do protocolo real
contra um rádio simulado; sem ela, esses são pulados.

### Python 3.12 ou mais novo

A `rajant-api` 0.1.1 faz `from ssl import wrap_socket`, e essa função foi
**removida no Python 3.12**. Por isso:

```python
>>> from rajant_api import Breadcrumb          # 3.12+
ImportError: cannot import name 'wrap_socket' from 'ssl'
```

O `rajant_monitor.py` instala um shim compatível **antes** de importar a
biblioteca, então o programa funciona normalmente em 3.12 e 3.13. Só não
funciona importar a `rajant_api` sozinha.

Para conferir a instalação, use o caminho do programa:

```bash
python -c "import sys; sys.argv=['x']; import rajant_monitor as m; print(m.Breadcrumb)"
```

> **Trocar para o Python 3.12 não resolve esse erro.** O `ssl.wrap_socket` foi
> removido **na 3.12**, não na 3.13 — as duas dão o mesmo `ImportError`. Só o
> 3.11 e anteriores ainda têm a função. O shim é o que faz as duas versões
> novas funcionarem.
>
> Verificado com o programa inteiro em Python 3.13: 629 testes, geração de PPT
> e build do PyInstaller, tudo passando.

O shim reproduz o comportamento antigo, inclusive **sem validação de
certificado** — que é o que a `ssl.wrap_socket()` fazia por padrão e o que os
BreadCrumbs exigem, já que usam certificado autoassinado.

## Onde ler mais

- métricas e o que **não** dá para obter da API → `AUDITORIA_METRICAS.md`
- survey, zonas-problema, interferência, KML → `SITE_SURVEY.md`
- dashboards Grafana → `grafana/README.md`
- painel HTML próprio → `painel/README.md`
