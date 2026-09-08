#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Valida os dashboards contra o exporter.

Checagens:
  1. JSON íntegro e campos obrigatórios presentes;
  2. nenhuma métrica removida na auditoria aparece em consulta;
  3. toda métrica consultada existe de fato no rajant_monitor.py;
  4. nenhum painel sobreposto na grade de 24 colunas;
  5. IDs de painel únicos dentro do dashboard.

    python3 grafana/validar_dashboards.py
"""
import json, re, pathlib, sys

RAIZ = pathlib.Path(__file__).parent
EXPORTER = RAIZ.parent / "rajant_monitor.py"

REMOVIDAS = {
    "rajant_voltagem_v", "rajant_voltagem_min_v", "rajant_voltagem_max_v",
    "rajant_bateria_v", "rajant_session_state", "rajant_im_unicast_drop",
    "rajant_im_overflows", "rajant_im_undeliv_rx", "rajant_im_undeliv_tx",
    "rajant_radio_radar_detec", "rajant_radio_radar_pulsos",
    "rajant_radio_phy_erros", "rajant_peer_txpower_dbm",
    "rajant_eth_rx_erros", "rajant_eth_tx_erros", "rajant_eth_rx_crc",
    "rajant_eth_rx_drop", "rajant_eth_tx_drop", "rajant_eth_mudancas",
}

PUBLICADAS = set(re.findall(r'Gauge\("([a-z0-9_]+)"',
                            EXPORTER.read_text(encoding="utf-8")))


def consultas(painel):
    return [t.get("expr", "") for t in painel.get("targets", []) or []]


def metricas(expr):
    # nomes rajant_* que aparecem fora de bloco de código markdown
    return set(re.findall(r'\brajant_[a-z0-9_]+\b', expr))


def sobrepoe(a, b):
    ax, ay, aw, ah = a["x"], a["y"], a["w"], a["h"]
    bx, by, bw, bh = b["x"], b["y"], b["w"], b["h"]
    return not (ax + aw <= bx or bx + bw <= ax or
                ay + ah <= by or by + bh <= ay)


def validar(caminho):
    erros, avisos = [], []
    d = json.loads(caminho.read_text(encoding="utf-8"))

    for campo in ("uid", "title", "panels", "templating", "schemaVersion"):
        if campo not in d:
            erros.append(f"falta o campo obrigatório '{campo}'")

    paineis = d.get("panels", [])
    ids, grades, usadas = [], [], set()

    for p in paineis:
        ids.append(p.get("id"))
        grades.append((p.get("title", "?"), p["gridPos"]))
        for e in consultas(p):
            usadas |= metricas(e)

    # 2 + 3 — métricas
    for m in sorted(usadas):
        if m in REMOVIDAS:
            erros.append(f"consulta métrica REMOVIDA na auditoria: {m}")
        elif m not in PUBLICADAS:
            erros.append(f"consulta métrica inexistente no exporter: {m}")

    # 4 — sobreposição
    for i in range(len(grades)):
        for j in range(i + 1, len(grades)):
            if sobrepoe(grades[i][1], grades[j][1]):
                erros.append(f"painéis sobrepostos: "
                             f"'{grades[i][0]}' e '{grades[j][0]}'")

    # largura da grade
    for titulo, g in grades:
        if g["x"] + g["w"] > 24:
            erros.append(f"'{titulo}' ultrapassa a coluna 24 "
                         f"(x={g['x']} w={g['w']})")

    # 5 — ids únicos
    if len(ids) != len(set(ids)):
        erros.append("há IDs de painel repetidos")

    # avisos: variáveis usadas mas não declaradas
    declaradas = {v["name"] for v in d["templating"]["list"]}
    for p in paineis:
        for e in consultas(p):
            for v in re.findall(r'\$(\w+)', e):
                if v not in declaradas and v != "__all":
                    avisos.append(f"'{p.get('title')}' usa $"
                                  f"{v}, não declarada")

    return d, sorted(usadas), erros, sorted(set(avisos))


def main():
    arquivos = sorted(RAIZ.glob("rajant-*.json"))
    if not arquivos:
        print("nenhum dashboard encontrado"); return 1
    total_err = 0
    todas = set()
    for f in arquivos:
        d, usadas, erros, avisos = validar(f)
        todas |= set(usadas)
        n = len([p for p in d["panels"] if p["type"] != "row"])
        marca = "OK " if not erros else "ERRO"
        print(f"[{marca}] {f.name:<28} {n:>2} painéis · "
              f"{len(usadas):>2} métricas")
        for e in erros:
            print(f"         ✗ {e}"); total_err += 1
        for a in avisos:
            print(f"         ! {a}")

    print(f"\nMétricas distintas consultadas: {len(todas)} de "
          f"{len(PUBLICADAS)} publicadas pelo exporter")
    naousadas = sorted(PUBLICADAS - todas)
    if naousadas:
        print(f"Publicadas e sem painel ({len(naousadas)}):")
        for m in naousadas:
            print(f"   · {m}")
    print(f"\n{'FALHOU' if total_err else 'TUDO OK'} — {total_err} erro(s)")
    return 1 if total_err else 0


if __name__ == "__main__":
    sys.exit(main())
