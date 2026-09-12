# rajant_monitor

Exportador Prometheus, gerador de relatórios e ferramenta de site survey para
malha Rajant BreadCrumb em mina a céu aberto (~150 nós).

Binário único: `rajant_monitor.py`. Sem framework, sem serviço externo além do
Prometheus. Roda em rede isolada.

## Por onde começar

```bash
python3 rajant_monitor.py                      # sobe exportador + página web
python3 rajant_monitor.py --testar-fundo       # confere o fundo dos mapas
python3 teste_parser.py                        # 499 testes
```

A página web fica em `http://<servidor>:<porta_relatorio>/` com quatro abas:
**Relatórios**, **Survey**, **Histórico** e **Medições**.

## O que tem aqui

| arquivo | o que é |
|---|---|
| `rajant_monitor.py` | tudo: coleta, métricas, relatórios, survey, página web |
| `survey_meshmapper.py` | gerador de relatório a partir da captura do MeshMapper (não usa rede) |
| `teste_parser.py` | 499 testes; roda sem rádio e sem a lib `rajant_api` |
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
kmz_com_rotas        = false       # true = também a linha ligando amostras
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

Saem três arquivos: o **KMZ** com o rastro de calor, o **PPT** na
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

### Cobertura disponível × serviço entregue

São **duas perguntas diferentes**, e o relatório passou a responder as
duas separadamente:

| pergunta | o que se mede |
|---|---|
| *"existe sinal servível aqui?"* | o melhor vizinho de **infraestrutura** (ERB/ERM) visível no ponto |
| *"a aplicação funcionou aqui?"* | o enlace que o **InstaMesh usou** |

No arquivo real do cliente, no mesmo trajeto e ao mesmo tempo:

| | mediana | fora do requisito (> −75 dBm) |
|---|---|---|
| **cobertura disponível** | −66 dBm | **0%** |
| enlace que atendeu | −88 dBm | **81,1%** |
| RSSI disponível e não usado | 20 dB (mediana) | |

**A área tem cobertura. O caminho escolhido é que era ruim.** Isso muda a
recomendação do laudo: repetidora nova não resolveria.

Por que **infraestrutura** e não o vizinho mais forte: o mais forte é
quase sempre outro caminhão encostado (−40 dBm no arquivo do cliente).
Ele some quando o caminhão sai, então não caracteriza cobertura da área.
Quando não há nenhum ERB/ERM visível, cai para o melhor vizinho qualquer
— **e o relatório diz isso**, em vez de apresentar veículo de passagem
como cobertura.

A aba de cobertura é a **primeira** do KMZ. Abrir pelo enlace entregue faz
o leitor concluir "falta rádio" onde o problema é outro.

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

## O KMZ: rastro em calor, não linha

A rota sai como **mapa de calor**, e só onde o rádio passou: cada amostra
pinta um núcleo de raio limitado à sua volta e o resto do raster fica
transparente.

Isso **não** é a superfície de cobertura que foi retirada a pedido. Aquela
interpolava valor sobre terreno onde ninguém passou — afirmava sinal em
lugar não medido. Esta só pinta o que foi medido, e há teste exigindo que a
maior parte da imagem continue transparente. A diferença entre *"medi aqui
e deu isto"* e *"acho que lá deve dar aquilo"* é o que separa um laudo de
um chute.

**Só entra quem andou.** Rádio parado dá dezenas de amostras no mesmo ponto:
virava uma bola isolada no mapa e, como BC fixo enxerga o vizinho de perto,
saía verde. Eram essas as bolas espalhadas e desconectadas — e boa parte do
verde que não batia com a realidade da mina.

O raio sai do **espaçamento real das amostras** (0,9×, entre 25 e 150 m).
Aqui há um limite físico, não de desenho: **com amostras a 200 m não existe
faixa estreita e contínua**. Ou saem contas separadas, ou sai um borrão
largo afirmando medição a centenas de metros da estrada. O jeito de ter
rastro fino *e* contínuo é baixar o intervalo — a 1 s são ~11 m entre
amostras e o raio cai para o piso.

A opacidade vem do núcleo **mais forte** que cobre o pixel, não da soma
deles: pela soma, um equipamento parado ficava sólido e ainda puxava a
referência para cima, apagando o rastro de quem andou.

### Cada pixel mostra uma leitura real

O valor do pixel é o da **amostra mais próxima** — não uma média.

Isso foi decidido medindo, não por gosto. No mesmo trajeto de um arquivo
real:

| regra | % da imagem fora do requisito |
|---|---|
| amostras cruas (referência) | 81,1% *(estatística de tempo)* |
| **vizinho mais próximo** | **92,1%** *(estatística de área)* |
| média ponderada | 100,0% |

A média não escondia problema: ela **apagava o que era bom**. As poucas
leituras de −45 dBm sumiam ao serem promediadas com as vizinhas ruins, e
o mapa dizia que 100% do trajeto reprovava quando as medições diziam 81%.

> Área e tempo não são comparáveis: veículo parado gera muitas amostras
> num ponto só, e trecho percorrido rápido cobre área com poucas
> amostras. Os dois números respondem perguntas diferentes.

**Passar duas vezes no mesmo lugar:** vale a pior das leituras. A operação
enfrenta as duas, e é a ruim que para o caminhão. O empate é aferido na
**resolução da grade** — com tolerância maior, ele disparava entre
amostras consecutivas e o mapa inteiro pendia para o lado ruim (95,8%
contra 92,1%), o que não é ser conservador, é distorcer.

A linha continua disponível em `kmz_com_rotas = true`, e quando ligada ela
é **partida nos buracos de medição** — o limiar vem da mediana do próprio
survey, não de número mágico. Ligar duas amostras distantes é afirmar que
o veículo passou pela reta entre elas; com amostragem espaçada isso virava
aresta reta cortando a cava.

> Detalhe que custou depuração: o contorno escuro da rota era **uma linha
> por trajeto**. Mesmo quebrando os segmentos coloridos, ele sozinho
> redesenhava a reta que a quebra tinha acabado de tirar.

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
> Verificado com o programa inteiro em Python 3.13: 499 testes, geração de PPT
> e build do PyInstaller, tudo passando.

O shim reproduz o comportamento antigo, inclusive **sem validação de
certificado** — que é o que a `ssl.wrap_socket()` fazia por padrão e o que os
BreadCrumbs exigem, já que usam certificado autoassinado.

## Onde ler mais

- métricas e o que **não** dá para obter da API → `AUDITORIA_METRICAS.md`
- survey, zonas-problema, interferência, KML → `SITE_SURVEY.md`
- dashboards Grafana → `grafana/README.md`
- painel HTML próprio → `painel/README.md`
