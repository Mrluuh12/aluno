# rajant_monitor

Exportador Prometheus e gerador do relatório semanal para malha Rajant
BreadCrumb em mina a céu aberto (~150 nós), com os dashboards do Grafana.

Binário único: `rajant_monitor.py` (no Windows, `rajant_monitor.exe`). Sem
framework, sem serviço externo além de Prometheus e Grafana. Roda em rede
isolada.

> O site survey foi descontinuado: saíram o `site_survey.exe`, a leitura de
> arquivos do MeshMapper, a aba Survey da página, os comandos `--survey-*`,
> os slides de survey do semanal e o dashboard *Site Survey Contínuo*. O
> código continua no histórico do git.

## Por onde começar

```bash
python3 rajant_monitor.py                      # exportador + Prometheus + Grafana, em segundo plano
python3 rajant_monitor.py --exportador         # só exportador + página web, nesta janela
python3 rajant_monitor.py --gerar-relatorio 2026-10-08 2026-10-14 --demo
python3 teste_parser.py                        # 188 testes
```

A página web fica em `http://<servidor>:<porta_relatorio>/` (padrão 8010):
período, **Excel** ou **PPT** do semanal, link para o **Painel** e o
diagnóstico de um BreadCrumb pelo IP.

## O que tem aqui

| arquivo | o que é |
|---|---|
| `rajant_monitor.py` | coleta, métricas, relatório semanal (Excel/PPT), página web |
| `servico_rajant.py` | modo serviço: sobe e vigia exportador, Prometheus e Grafana |
| `teste_parser.py` | 188 testes; roda sem rádio e sem a lib `rajant_api` |
| `build/` | geração do `.exe` (PyInstaller), `parar_tudo.bat` e o `prometheus.yml` de exemplo |
| `ARQUITETURA.md` | como funciona por dentro: camadas, threads, invariantes |
| `AUDITORIA_METRICAS.md` | as métricas conferidas campo a campo contra os `.proto` |
| `bcapi-ref/proto/` | os `.proto` do bcapi, referência de tudo que se afirma sobre a API |
| `grafana/` | 6 dashboards em JSON + gerador e validador |
| `painel/` | dashboard HTML/CSS/JS próprio, zero dependência externa |
| `marca/` | logos Anglo American usados no deck semanal |

## Regras que atravessam o código

**1. Nunca publicar 0 no lugar de "não medi".** Zero é indistinguível de
"medido e deu zero". Campo sem medição vira `None` e a série é omitida. Foi o
que fazia painel de erro de ethernet e CPU parecerem saudáveis.

**2. Nada se afirma sobre a BC API sem conferir o `.proto`.** A auditoria
removeu 19 séries que não tinham campo correspondente e corrigiu conversões
que estavam erradas havia tempo (SNR, GPS em NMEA, temperatura). Há teste que
falha se um `.proto` passar a expor varredura de espectro.

**3. Dado que parou de chegar não pode parecer dado.** Rádio que não
responde sai do `/metrics` (ver abaixo); o Grafana mostra o buraco, nunca uma
linha reta no último valor.

## Rodar em segundo plano (Windows)

**Dois cliques no `rajant_monitor.exe`.** Ele sobe o exportador, o
Prometheus e o Grafana **ocultos**, mostra numa janela o que subiu e onde
abrir, e a janela fecha sozinha (qualquer tecla fecha antes):

```
Rajant Monitor rodando em segundo plano (pid 4812):
  Prometheus  C:\Users\SEU_USUARIO\prometheus-2.53\prometheus.exe
  Grafana     C:\Users\SEU_USUARIO\grafana-11.2\bin\grafana-server.exe
  Exportador  C:\Users\SEU_USUARIO\RajantMonitor\rajant_monitor.exe
  Grafana:     http://localhost:3000
  Relatórios:  http://localhost:8010
  Para parar:  parar_tudo.bat  (ou rajant_monitor.exe --parar)
```

Não pede administrador. Depois de reiniciar a máquina, é dar os dois cliques
de novo. Dois cliques com ele já rodando não abrem um segundo — a janela só
diz que já está no ar.

Quem fica rodando escondido é um supervisor (`rajant_monitor.exe --servico`)
que:

- confere a cada 5 s se os três seguem de pé;
- reergue quem cair, com espera crescente (5, 10, 30, 60 s) para não ficar
  em laço com um programa que cai na partida;
- se a porta já estiver atendida por outro processo (ex.: Grafana instalado
  como serviço do Windows), não sobe um segundo — só passa a cuidar dele se
  aquele cair.

| comando | o que faz |
|---|---|
| dois cliques no `rajant_monitor.exe` | sobe tudo em segundo plano |
| `parar_tudo.bat` | para supervisor, exportador, Prometheus e Grafana |
| `rajant_monitor.exe --status` | se o supervisor está no ar e se cada porta responde |
| `rajant_monitor.exe --exportador` | só o exportador e a página, nesta janela (para ver o log na tela) |

### Sem administrador

- **A pasta do monitor tem de ser sua.** Ele grava `config.ini`, logs e
  cache ao lado do `.exe`. Use algo como `C:\Users\SEU_USUARIO\RajantMonitor`,
  não `Program Files`. Se a pasta não aceitar gravação, a janela diz isso em
  vez de ir para o fundo.
- **Prometheus e Grafana na versão zip**, extraídos numa pasta sua (os dois
  gravam em `data\` dentro da própria pasta). O instalador `.msi` do Grafana
  pede administrador. Pondo as pastas `prometheus-*` e `grafana-*` dentro ou
  ao lado da pasta do monitor, ele as acha sozinho; a janela avisa se uma
  delas faltar ou estiver numa pasta onde não dá para gravar.
- **Sair da conta (logoff) encerra tudo.** Bloquear a tela (Win+L) não para
  nada.
- **Firewall:** na primeira vez, o Windows pode perguntar se libera o acesso
  à rede. Sem administrador, cancele: tudo continua funcionando nesta
  máquina (o Prometheus lê o exportador por `localhost`). O que fica
  bloqueado é abrir o Grafana ou a página de relatórios **de outro
  computador** — para isso a TI precisa liberar as portas 3000 e 8010.

Onde estão os programas — vazio = procurar na pasta do monitor, na pasta de
cima, na pasta do usuário, em `AppData\Local\Programs`, em `C:\`, `D:\` e
`Program Files`:

```ini
[servicos]
prometheus_exe     = C:\prometheus\prometheus.exe
prometheus_args    = --config.file=prometheus.yml
grafana_exe        = C:\grafana\bin\grafana-server.exe
iniciar_grafana    = true     ; false se o Grafana já é serviço do Windows
```

A seção entra sozinha no `config.ini` na primeira vez. O `prometheus.yml` é
lido da pasta do Prometheus; `prometheus_exemplo.yml` tem o scrape do
exportador (`localhost:8000`, 15 s) pronto para copiar.

Logs, todos ao lado do `.exe`:

| arquivo | o que tem |
|---|---|
| `servico.log` | o supervisor: o que achou, o que subiu, quem caiu e quando (5 MB × 3) |
| `rajant_monitor.log` | o exportador (10 MB × 5) |
| `logs\prometheus.log`, `logs\grafana.log` | saída dos dois (recomeça ao passar de 20 MB) |
| `logs\exportador_erros.log` | só o que escapar do log do exportador |

`config.ini`, cache e logs ficam **sempre** na pasta do executável, de onde
quer que ele seja chamado (atalho na Área de Trabalho, outra pasta).

## Grafana: dado que parou de chegar

Antes, rádio que parava de responder seguia no `/metrics` com a última
leitura, e vizinho que saía da tabela de peers nunca era retirado. O
Prometheus continuava raspando o mesmo número e o gráfico virava uma linha
reta.

Agora, a cada coleta, **sai do `/metrics` toda série do BC que não foi
medida nessa coleta**:

- vizinho que sumiu da tabela de peers → as séries daquele enlace saem;
- BC que não respondeu → saem as medições; ficam `rajant_online = 0`,
  falhas, última coleta e disponibilidade.

O Prometheus encerra a série e o Grafana desenha o buraco. Não vira **0**
(regra 1): 0 dBm de sinal ou 0 °C seriam leituras, e erradas. A queda
aparece em `rajant_online`.

Os painéis ligam buracos curtos de até **3 min** (uma raspagem perdida não
quebra a linha); buraco maior aparece.

Quem preferir segurar a série por um tempo depois da falha:

```ini
[coleta]
segurar_serie_s = 0     ; segundos; 0 = sai na primeira coleta que falha
```

A chave antiga `manter_ultimo_valor_s` é ignorada (o log avisa): o
`config.ini` instalado tinha 300 nela, que era a linha reta de 5 minutos.

Também corrigido: `rajant_ultima_coleta_ts` agora é hora real (epoch) —
antes era um relógio interno que não dizia hora nenhuma.

## Só equipamento com tag

O Grafana mostrava IP sem nome (`10.188.99.217`) e rádio que não é da
frota. Agora só entra no `/metrics` quem tem **tag** no nome — inclusive
como vizinho de outro rádio:

```ini
[coleta]
filtrar_tag = true
padrao_tag  = ^[A-Z]{2,5}[- ]?\d{2,}     ; CA-1035, ERM-22 PTP CAM, RED065, PCP-002
```

- O nome vem do próprio rádio; se ele não respondeu ainda, do cache e da
  lista da malha da mina (`REDE_CONHECIDA`). IP que nunca deu nome não
  aparece nem como offline.
- O vizinho é identificado pelo IP ou, quando o peer não traz IP, pelo
  `encapId` (o sufixo do número de série). O rótulo `peer` das séries de
  enlace passou a ser a **tag** do vizinho, não o IP.
- O descarte é auditável: vai para o log (`ignorado: nome sem tag`) e para
  `rajant_bc_sem_tag`.
- As 155 entradas nomeadas da malha casam com o padrão; o teste confere.

As séries antigas, com IP no lugar do nome, ficam no histórico do
Prometheus até saírem do intervalo de tempo do painel.

As chaves antigas `somente_com_tag` e `prefixos_frota` saíram; `filtrar_tag`
é a nova (o log avisa se encontrar a antiga).

## Quem entra na coleta

Por padrão o exporter **descobre** a malha: parte dos seeds e segue os peers
de cada BC.

```ini
[coleta]
descoberta  = true    ; false = só os IPs de [rede] seeds
usar_cache  = true    ; false = não lê nem escreve o cache em disco
```

**Lista fixa.** Com `descoberta = false`, ponha todos os IPs em
`[rede] seeds` (separados por vírgula) e o exporter coleta exatamente
aqueles. Seed que não responde **continua na lista**: um BC precisa seguir
monitorado justamente quando cai.

**Cache desligado.** Ele guarda o que a descoberta já viu um dia e
ressuscita endereço que saiu da rede. Com lista fixa, não serve para nada.

## Relatório semanal

Excel e PPT do período, gerados sob demanda a partir do Prometheus — pela
página web, por `/gerar?ini=AAAA-MM-DD&fim=AAAA-MM-DD&fmt=pptx` ou, o Excel,
por `--gerar-relatorio INI FIM`.

O template do cliente (`Relatorio_Semanal_Rede.pptx`, ao lado do
executável) ainda traz a seção *5. Site Survey*. O semanal **remove esses
slides**, senão ficariam em branco no arquivo, e o botão do menu passa a
dizer *"5. Site Survey (descontinuado)"* em vez de apontar para um slide que
não existe mais.

O que decide a remoção é **o título do slide, não o texto dele**: o
subtítulo de *Links Críticos (SNR)* diz *"candidatos a realinhamento / site
survey"*, e casar o texto inteiro levava o slide junto. *Espectro por Canal*
é saúde da malha (contadores do exporter, todos os rádios, o período
inteiro) e fica na seção 4.

### Identidade Anglo

O semanal sai na identidade Anglo American: fundo branco, azul
institucional `#031795`, Calibri, logo e régua em cada slide, capa azul com
o logo branco. O template do cliente é azul-escuro, com menus, botões e
campos preenchidos à mão; em vez de trocar o arquivo, o deck é **repintado
no fim da geração**, depois de preenchido — nada muda de lugar, só a pele.
Repintar no fim pega também o verde/âmbar/vermelho do status e os slides
técnicos gerados por código.

```ini
[relatorio]
identidade_anglo = false    ; visual original do template
```

Os logos ficam em `marca/`. Sem essa pasta o deck sai sem logo, em vez de
estourar.

## Instalar a rajant-api

Ela declara três dependências, mas **só usa uma**:

```
grpcio == 1.56.2        declarada, NUNCA importada
grpcio-tools == 1.56.2  declarada, NUNCA importada
protobuf == 4.23.4      esta sim
```

`grpcio 1.56.2` não tem wheel para Python 3.12+, então instalar as
dependências declaradas falha tentando compilar C++. Instale assim:

```bash
pip install rajant-api --no-deps
pip install "protobuf==4.23.4"
```

Se `import rajant_api` falhar com `No module named 'google'`, é o protobuf
que falta.

### O que o exportador faz por cima dela

Lido no código da 0.1.1:

- **`get_state()` lê a resposta com um único `recv`** — pelo TLS, no máximo
  um registro de ~16 KB. O State de rádio com muitos vizinhos passa disso,
  e ela devolve `False`. Esse `False` passava pelo parser como um State
  vazio: rádio sem nome, tratado como leitura boa — e, com o filtro de tag,
  tirado do Grafana como "sem tag". O exportador agora faz o mesmo pedido
  pela sessão autenticada da biblioteca e lê a mensagem **inteira**, do
  tamanho que o cabeçalho declara (`ler_estado`). State que não veio é
  **falha de coleta**, nunca State vazio.
- **`authenticate()` devolve `False` para tudo.** O login daqui faz os
  mesmos passos e diz o motivo: usuário que não existe na BCAPI, senha
  recusada (com a descrição do rádio e aviso de espaço na senha), etapa em
  que parou. `co` em minúsculo funciona (a biblioteca só conhece `CO`). Se
  este caminho falhar por rede, tenta o da biblioteca antes de desistir.
- **A `Breadcrumb` não tem `close()`.** A sessão é fechada depois de cada
  leitura, em vez de ficar aberta no rádio até o coletor de lixo passar.
- O `get_state()` pingava o rádio antes de cada consulta; a coleta já testou
  o alcance, então o ping extra saiu.

Com a biblioteca instalada, a suíte roda também os testes do protocolo real
contra um rádio simulado (mensagens dela, em pedaços, como pelo TLS); sem
ela, esses são pulados.

### Python 3.12 ou mais novo

A `rajant-api` 0.1.1 faz `from ssl import wrap_socket`, e essa função foi
**removida no Python 3.12**. O `rajant_monitor.py` instala um shim compatível
**antes** de importar a biblioteca, então o programa funciona em 3.12 e 3.13.
Só não funciona importar a `rajant_api` sozinha. Para conferir a instalação:

```bash
python -c "import sys; sys.argv=['x']; import rajant_monitor as m; print(m.Breadcrumb)"
```

O shim reproduz o comportamento antigo, inclusive **sem validação de
certificado** — que é o que a `ssl.wrap_socket()` fazia por padrão e o que os
BreadCrumbs exigem, já que usam certificado autoassinado.

## Segurança na malha

O exportador só **lê**: login e `State`. Nenhuma tarefa é enviada aos
rádios (REBOOT, ZEROIZE, CLEAR, KICK, INSTALL, SNAPSHOT, TRACE — nenhuma).
A sessão se identifica no rádio como `rajant_monitor` (aparece em
`State.adminSessions`).

## Onde ler mais

- métricas e o que **não** dá para obter da API → `AUDITORIA_METRICAS.md`
- como funciona por dentro → `ARQUITETURA.md`
- dashboards Grafana → `grafana/README.md`
- painel HTML próprio → `painel/README.md`
