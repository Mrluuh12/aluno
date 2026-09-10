#!/usr/bin/env python3
"""
Gerador de relatório de site survey a partir de uma captura do MeshMapper.

Não fala com rádio nenhum. Entra o arquivo que o MeshMapper gravou, saem
o KMZ com o rastro de calor, o PPT na identidade Anglo, o Excel e os
gráficos.

    survey_meshmapper.exe Meshmapper_2026-09-10_12-59-11.kmz

Também aceita os CSVs, quando só eles foram guardados:

    survey_meshmapper.exe Meshmapper_..._trace_path.csv

Por que existe separado do rajant_monitor: o monitor sonda a malha o
tempo todo e precisa da rajant-api, de credencial e de rede. Este aqui é
só arquivo entra / arquivo sai — roda no notebook de quem fez a medição,
sem acesso à mina.
"""
import sys, argparse, traceback
from pathlib import Path

# O motor de saída (KMZ, PPT, Excel, gráficos) é o do rajant_monitor: são
# ~4 mil linhas já corrigidas contra armadilhas reais — ordem dos
# elementos no KML, escape do XML, borda de célula no PPTX, transparência
# do raster. Reescrever aqui seria refazer os mesmos bugs.
try:
    import rajant_monitor as rm
except ImportError:
    print("ERRO: rajant_monitor.py precisa estar na mesma pasta.")
    sys.exit(1)


def gerar(caminho, saida=None, bandas=None, nome=None, sem_ppt=False,
          sem_excel=False, sem_kmz=False):
    saida = Path(saida or ".").resolve()
    saida.mkdir(parents=True, exist_ok=True)
    cfg = rm.carregar_config(); rm.cfg_relatorio(cfg)

    print(f"Lendo {Path(caminho).name} …")
    sid, sv, amostras, peers = rm.importar_meshmapper(caminho, nome=nome)

    ini = rm.datetime.utcfromtimestamp(sv["inicio"]) if sv.get("inicio") else None
    dur = ((sv["fim"] - sv["inicio"]) / 60.0) if sv.get("fim") and sv.get("inicio") else 0
    campos = rm.campos_com_medicao(amostras)
    faltando = [c for c in rm.CAMPOS_KMZ if c not in campos]

    print(f"  móvel .............. {sv['movel']}")
    print(f"  survey no banco .... #{sid}")
    if ini: print(f"  início ............. {ini:%d/%m/%Y %H:%M} UTC")
    print(f"  duração ............ {dur:.1f} min")
    print(f"  amostras ........... {len(amostras)}")
    print(f"  vizinhos lidos ..... {len(peers)}")
    print(f"  intervalo real ..... {rm._mm_intervalo_real(amostras)} s")
    print(f"  grandezas medidas .. {', '.join(campos) or 'nenhuma'}")
    if faltando:
        # Dizer o que NAO veio evita a leitura errada de "mediu e deu
        # ruim" onde o certo e "o MeshMapper nao mede isso".
        print(f"  sem medição ........ {', '.join(faltando)} "
              f"(o MeshMapper não fornece)")
    if sv.get("limiares_mm"):
        print(f"  régua do MeshMapper  {sv['limiares_mm']}")

    feitos = []
    if not sem_kmz:
        if campos:
            dados, nome_kmz = rm.gerar_kml_survey(sv, amostras, cfg=cfg,
                                                  campos=campos)
            alvo = saida / nome_kmz
            alvo.write_bytes(dados); feitos.append(alvo)
        else:
            print("  ! sem grandeza medida: KMZ não gerado")

    if not sem_ppt:
        try:
            dados, nome_ppt = rm.ppt_survey_anglo(sid, cfg, bandas)
            alvo = saida / nome_ppt
            alvo.write_bytes(dados); feitos.append(alvo)
        except Exception as e:
            print(f"  ! PPT não gerado: {e}")

    if not sem_excel:
        try:
            dados, nome_xls = rm.excel_do_meshmapper(sv, amostras, peers, cfg)
            alvo = saida / nome_xls
            alvo.write_bytes(dados); feitos.append(alvo)
        except Exception as e:
            print(f"  ! Excel não gerado: {e}")

    print()
    for f in feitos:
        print(f"  → {f}")
    if not feitos:
        print("  nada foi gerado.")
    return feitos


def main():
    ap = argparse.ArgumentParser(
        prog="survey_meshmapper",
        description="Relatório de site survey a partir de uma captura do "
                    "MeshMapper (KMZ, data.json ou os CSVs).")
    ap.add_argument("arquivo", help="o .kmz do MeshMapper, o data.json ou "
                                    "um dos CSVs (_trace_path / _peer_info)")
    ap.add_argument("-o", "--saida", help="pasta de saída (padrão: atual)")
    ap.add_argument("-n", "--nome", help="nome do survey no relatório")
    ap.add_argument("--bandas", help="bandas a detalhar no PPT "
                                     "(padrão: as que aparecerem)")
    ap.add_argument("--sem-ppt", action="store_true")
    ap.add_argument("--sem-excel", action="store_true")
    ap.add_argument("--sem-kmz", action="store_true")
    a = ap.parse_args()

    bandas = [b.strip() for b in a.bandas.split(",")] if a.bandas else None
    try:
        gerar(a.arquivo, a.saida, bandas, a.nome,
              a.sem_ppt, a.sem_excel, a.sem_kmz)
    except Exception as e:
        print(f"\nERRO: {e}")
        if "--debug" in sys.argv: traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
