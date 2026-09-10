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
| `Site_Survey_*.pptx` | capa, índice, sumário, zonas-problema e uma página por grandeza/banda |
| `Survey_*.xlsx` | origens, resumo, por grandeza, amostras e **todos os vizinhos ponto a ponto** |

**Juntar tudo num relatório só** (padrão) produz um conjunto com todas as
capturas — é o caso da campanha cobrindo a mina. Desmarcado, sai um
conjunto por arquivo, cada um em sua subpasta.

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
| `exemplos/` | captura de exemplo, usada pelos testes |
| `build/` | spec do PyInstaller e o `.bat` |

O `rajant_monitor.py` vem junto porque é ele que gera KMZ, PPT e Excel —
são milhares de linhas já corrigidas contra armadilhas do formato (ordem
dos elementos no KML, escape do XML, borda de célula no PPTX). Aqui ele é
usado só como biblioteca: nada de rede é acionado.
