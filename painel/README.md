# Painel HTML — Rajant

Dashboard em HTML + CSS + JavaScript puro, para substituir/complementar o
Grafana quando você quer controle total do visual.

**Zero dependências externas.** Nada de CDN, biblioteca de gráficos ou fonte
remota — os gráficos são SVG desenhados à mão. Funciona em rede isolada, que é
o caso de uma mina.

```
painel/
└── visao-geral.html    equivalente ao dashboard 1 do Grafana
```

## Como abrir

### 1. Servido pelo exporter (recomendado)

```
http://<servidor>:<porta_relatorio>/painel
```

Nesse modo o painel usa o proxy `/api/q` do próprio exporter. Duas vantagens:
não há problema de CORS no navegador, e a URL do Prometheus fica no servidor —
o painel não precisa saber onde ele está.

### 2. Abrindo o arquivo direto

Funciona clicando duas vezes no `.html`, mas aí o navegador fala direto com o
Prometheus e você precisa informar onde ele está:

```
visao-geral.html?prom=http://10.0.0.5:9090
```

Ou edite a constante no topo do `<script>`:

```js
const CFG = {
  prometheus: "http://localhost:9090",
  atualizaMs:  30000,     // de quanto em quanto tempo recarrega
  janelaS:     21600,     // janela inicial (6 h)
};
```

Nesse modo o Prometheus precisa aceitar CORS (por padrão ele aceita).

### 3. Modo demo — para mexer no visual sem dados reais

```
visao-geral.html?demo=1
```

Gera dados sintéticos plausíveis. Use enquanto estiver ajustando cores,
espaçamento e layout: não depende de Prometheus nem de rede. O rodapé avisa em
vermelho que os dados não são reais, para ninguém confundir.

## Deixar com a sua cara

### Cores

Tudo está em variáveis CSS no topo do arquivo. Mude ali e o painel inteiro
acompanha:

```css
:root{
  --fundo:    #08142e;   /* fundo da página            */
  --cartao:   #14213f;   /* fundo dos cartões          */
  --borda:    #22345c;   /* borda dos cartões          */
  --texto:    #e8edf7;   /* texto principal            */
  --ok:       #2ecc71;   /* dentro do esperado         */
  --atencao:  #f0a640;   /* atenção                    */
  --critico:  #e8544a;   /* fora do limiar             */
  --raio:     14px;      /* arredondamento dos cartões */
}
```

As cores base são as mesmas do template PPT (`0D2052` / `1A2744`), então já sai
coerente com o material que vocês entregam.

Para tema claro, troque `--fundo` por um tom claro, `--texto` por escuro e
remova os `radial-gradient` do `body`.

### Logo

No `<header>` há um SVG genérico. Troque por:

```html
<div class="logo"><img src="logo.png" style="width:100%;height:100%;
     object-fit:contain;border-radius:11px"></div>
```

### Título

```html
<h1>Visão Geral da Malha</h1>
```

## Como acrescentar um painel

Três passos. Digamos que você queira a temperatura máxima:

**1.** Some a consulta na lista do `Promise.all` em `carregar()`:

```js
const [online, nos, /* … */, temp] = await Promise.all([
  pedir("sum(rajant_online)"),
  /* … */
  pedir("max(rajant_temperatura_c)"),
]);
```

**2.** Para um cartão de indicador:

```js
kpis.append(
  cartaoKPI({rot:"Temperatura máxima", val: fmt(primeiro(temp),1), un:"°C",
             nota:"limiar BCE 65 °C",
             cls: classe(primeiro(temp), 65, 75, true)}),
);
```

**3.** Para um gráfico:

```js
const gTemp = grafico(serieDe(sTemp), {unid:" °C"});
corpo.append(bloco("Temperatura por BC", "máxima da frota", "c6",
                   [gTemp.no, gTemp.leg]));
```

### Peças disponíveis

| função | para que serve |
|---|---|
| `cartaoKPI({rot,val,un,nota,cls,serie})` | cartão de indicador, com sparkline opcional |
| `grafico(series, {unid,casas,faixa,zero})` | série temporal SVG com tooltip e mira |
| `barras(itens, {unid,aten,crit,inv})` | ranking horizontal |
| `tabela(colunas, linhas, vazio)` | tabela com cabeçalho fixo |
| `mapa(pontos)` | dispersão lat/lon colorida por SNR |
| `bloco(titulo, dica, coluna, conteudo)` | o cartão que envolve qualquer um acima |

Largura: `c3`, `c4`, `c6`, `c8`, `c12` numa grade de 12 colunas.

### Cores por limiar

```js
classe(valor, atencao, critico, inverso)
```

- **`inverso = false`** — maior é melhor (disponibilidade, SNR). Passe
  `critico < atencao`: `classe(99.42, 99.9, 99)` → laranja.
- **`inverso = true`** — menor é melhor (latência, alertas). Passe
  `atencao < critico`: `classe(7.4, 10, 50, true)` → verde.

Errar a ordem pinta tudo de vermelho — foi o primeiro bug que apareceu aqui.

## O que você perde em relação ao Grafana

Vale saber antes de migrar de vez:

- **alertas** — Grafana dispara notificação; aqui é só visualização;
- **exploração ad-hoc** — não dá para escrever PromQL na hora e ver o resultado;
- **variáveis de template** — o filtro por BC/rádio teria de ser codificado;
- **controle de acesso** — quem alcança a porta vê o painel;
- **exportar PNG/PDF**, snapshots, playlists e o ecossistema de plugins.

O que você ganha é exatamente o que pediu: controle total do visual, sem
depender de plugin ou de tema do Grafana.

Uma combinação que costuma funcionar: Grafana para investigar e alertar, painel
HTML para o telão da sala de controle e para quem só precisa olhar.

## Métricas usadas neste painel

`rajant_online` · `rajant_nos_fisicos_total` ·
`rajant_disponibilidade_1h_pct` · `rajant_disponibilidade_24h_pct` ·
`rajant_disponibilidade_7d_pct` · `rajant_alertas_ativos` ·
`rajant_reboot_needed` · `rajant_ping_rtt_ms` · `rajant_uptime_s` ·
`rajant_mesh_score` · `rajant_link_changes` · `rajant_falhas_consecutivas` ·
`rajant_bc_info` · `rajant_gps_lat` · `rajant_gps_lon` · `rajant_peer_snr_db`

Todas conferidas contra os `.proto` do bcapi — ver `../AUDITORIA_METRICAS.md`.
Nenhuma das 19 séries removidas na auditoria é consultada aqui.
