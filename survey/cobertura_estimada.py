#!/usr/bin/env python3
"""
Cobertura estimada pelo relevo — o LAS de cada área, as ERB/ERM com
posição, altura e potência, e o survey para calibrar.

Não é medição. O laudo continua sendo só o que foi medido; isto sai num
deck próprio ("Cobertura estimada"), com os mapas sobre as imagens das
áreas e o erro do modelo contra o survey escrito na primeira página.

O modelo, em cada ponto:

    RSSI = potência + ganho TX + ganho RX − perda de cabo
           − espaço livre − difração no relevo + correção da calibração

  • espaço livre pela distância 3D e pela frequência da banda;
  • difração no OBSTÁCULO DOMINANTE do perfil do terreno entre a antena
    da ERB/ERM e a do veículo (gume de faca, ITU-R P.526), com a
    curvatura da Terra (k = 4/3);
  • setor com azimute e abertura, quando informados;
  • a correção é a mediana de (medido − estimado) nas leituras do survey
    em que os veículos ouviram aquela banda das ERB/ERM — o que absorve
    ganho de antena e perda de cabo que ninguém informou. O que sobra
    depois dela é o erro do modelo, em dB, e vai para o relatório.

O terreno vem do LAS: cada célula fica com o ponto MAIS ALTO dela (o que
barra o sinal é a crista da bancada, a pilha, o prédio), sem os pontos
de ruído (classes 7 e 18).
"""
import math, csv, re, json
from pathlib import Path

import motor_survey as rm

EXT_LAS = (".las", ".laz")
INFRA_PADRAO = "infraestrutura.csv"

# O que vale quando a ERB/ERM não informa. Vai escrito nas premissas do
# deck, com quantas usaram o padrão.
PADROES = {
    "altura_tx_m": 9.0,      # mastro da ERB/ERM, acima do terreno
    "altura_rx_m": 4.0,      # antena no veículo, acima do terreno
    "ganho_tx_dbi": 6.0,
    "ganho_rx_dbi": 5.0,
    "perda_cabo_db": 1.0,
    "potencia_dbm": 24.0,
    "raio_m": 3000.0,        # até onde se calcula a partir de cada ERB/ERM
    "celula_m": 3.0,         # grade do terreno
    "k": 4.0 / 3.0,          # raio efetivo da Terra
    "min_calibracao": 20,    # leituras para calibrar uma banda
}
FREQ_MHZ = {"2.4 GHz": 2437.0, "5.8 GHz": 5785.0}
COLUNAS_INFRA = ["nome", "lat", "lon", "altura_m", "potencia_dbm",
                 "ganho_dbi", "perda_cabo_db", "banda", "azimute",
                 "abertura", "proposta"]


def e_las(caminho):
    return Path(str(caminho)).suffix.lower() in EXT_LAS


def _laspy():
    try:
        import laspy
        return laspy
    except ImportError:
        raise RuntimeError("para ler LAS/LAZ falta o pacote laspy "
                           "(pip install \"laspy[lazrs]\")")


def parametros(cfg=None):
    """Os padrões, com o que o [cobertura] do config trocar."""
    prm = dict(PADROES)
    if cfg is not None and cfg.has_section("cobertura"):
        for k in prm:
            v = cfg.get("cobertura", k, fallback="").strip().replace(",", ".")
            if v:
                try:
                    prm[k] = type(prm[k])(float(v))
                except ValueError:
                    pass
    return prm


# ══════════════════════════════════════════════════════════════
# TERRENO
# ══════════════════════════════════════════════════════════════
def _crs_do_las(header):
    """O sistema de coordenadas gravado no LAS (WKT ou GeoKeys), ou None."""
    from rasterio.crs import CRS
    vlrs = list(getattr(header, "vlrs", []) or []) + list(
        getattr(header, "evlrs", None) or [])
    for v in vlrs:
        wkt = getattr(v, "string", None)
        if wkt and type(v).__name__.startswith("Wkt"):
            try:
                return CRS.from_wkt(wkt.strip("\x00").strip())
            except Exception:
                pass
    for v in vlrs:
        if type(v).__name__ == "GeoKeyDirectoryVlr":
            chaves = {k.id: k.value_offset for k in getattr(v, "geo_keys", [])}
            for kid in (3072, 2048):            # projetado, depois geográfico
                cod = chaves.get(kid)
                if cod and cod != 32767:
                    try:
                        return CRS.from_epsg(int(cod))
                    except Exception:
                        pass
    return None


def ler_terreno_las(caminho, celula_m=None, crs=None, max_celulas=40e6):
    """A grade do terreno de um LAS/LAZ: o ponto mais alto de cada célula.

    Lido em pedaços: um LAS de centenas de milhões de pontos não cabe na
    memória de uma vez. O resultado fica em `fundo_cache/`.

    Devolve {"z" (linhas × colunas, norte em cima, NaN sem ponto),
    "transform", "crs", "arquivo", "caminho"}."""
    import numpy as np
    from affine import Affine
    from rasterio.crs import CRS
    laspy = _laspy()
    p = Path(caminho)
    if not p.exists():
        raise RuntimeError(f"LAS não encontrado: {p}")
    celula_m = float(celula_m or PADROES["celula_m"])
    cache = rm._cache_imagem(p, "terreno", celula_m).with_suffix(".npz")
    if cache.exists():
        try:
            z = np.load(cache, allow_pickle=False)
            return {"z": z["z"], "transform": Affine(*z["transform"].tolist()),
                    "crs": CRS.from_wkt(str(z["crs"])), "arquivo": p.name,
                    "caminho": str(p.resolve())}
        except Exception:
            pass
    try:
        f = laspy.open(str(p))
    except Exception as e:
        raise RuntimeError(f"{p.name}: não abriu como LAS ({e})")
    with f:
        h = f.header
        crs_las = _crs_do_las(h) or (CRS.from_user_input(crs) if crs else None)
        if crs_las is None:
            raise RuntimeError(
                f"{p.name}: o LAS não traz o sistema de coordenadas. Ponha a "
                f"imagem (GeoTIFF) da mesma área junto, ou informe no "
                f"config.ini: [cobertura] crs_las = EPSG:31983")
        x0, y0 = float(h.mins[0]), float(h.mins[1])
        x1, y1 = float(h.maxs[0]), float(h.maxs[1])
        cel = celula_m
        while ((x1 - x0) / cel) * ((y1 - y0) / cel) > max_celulas:
            cel *= 1.5
        nx = max(1, int(math.ceil((x1 - x0) / cel)))
        ny = max(1, int(math.ceil((y1 - y0) / cel)))
        z = np.full(ny * nx, -np.inf, dtype=np.float64)
        for pts in f.chunk_iterator(2_000_000):
            xs = np.asarray(pts.x); ys = np.asarray(pts.y)
            zs = np.asarray(pts.z)
            try:
                cl = np.asarray(pts.classification)
                ok = (cl != 7) & (cl != 18)
                xs, ys, zs = xs[ok], ys[ok], zs[ok]
            except Exception:
                pass
            if not len(xs):
                continue
            c = np.clip(((xs - x0) / cel).astype(np.int64), 0, nx - 1)
            l = np.clip(((y1 - ys) / cel).astype(np.int64), 0, ny - 1)
            idx = l * nx + c
            ordem = np.argsort(idx, kind="stable")
            idx, zs = idx[ordem], zs[ordem]
            u, ini = np.unique(idx, return_index=True)
            z[u] = np.maximum(z[u], np.maximum.reduceat(zs, ini))
    z = z.reshape(ny, nx)
    z[~np.isfinite(z)] = np.nan
    z = _fechar_buracos(z, 3)
    tr = Affine(cel, 0, x0, 0, -cel, y1)
    out = {"z": z.astype("float32"), "transform": tr, "crs": crs_las,
           "arquivo": p.name, "caminho": str(p.resolve())}
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(cache, z=out["z"], transform=np.array(tuple(tr)[:6]),
                            crs=np.array(crs_las.to_wkt()))
    except Exception:
        pass
    return out


def _fechar_buracos(z, max_celulas):
    """Célula sem ponto entre células com ponto (sombra do scanner, um
    pixel sem retorno) pega a vizinha mais próxima — até `max_celulas`
    de distância. Buraco maior fica sem terreno."""
    import numpy as np
    from scipy.ndimage import distance_transform_edt
    vazio = ~np.isfinite(z)
    if not vazio.any() or vazio.all():
        return z
    d, (li, ci) = distance_transform_edt(vazio, return_indices=True)
    perto = vazio & (d <= max_celulas)
    z = z.copy()
    z[perto] = z[li[perto], ci[perto]]
    return z


def crs_padrao_las(cfg=None):
    """O [cobertura] crs_las do config, ou None."""
    try:
        cfg = cfg if cfg is not None else rm.carregar_config()
        v = cfg.get("cobertura", "crs_las", fallback="").strip()
        return v or None
    except Exception:
        return None


def area_do_las(caminho, celula_m=None, crs=None):
    """O LAS como área, quando não há imagem nem polígono dela: o nome do
    arquivo é o da área, o contorno é onde há terreno. `crs`: o sistema
    a usar se o LAS não trouxer o dele."""
    import numpy as np
    from rasterio import features, warp
    from affine import Affine
    t = ler_terreno_las(caminho, celula_m, crs=crs or crs_padrao_las())
    z = t["z"]
    f = max(1, int(math.ceil(max(z.shape) / 1200)))
    ok = np.isfinite(z[::f, ::f])
    tr = t["transform"] * Affine.scale(f, f)
    lado = abs(tr.a)
    polis = []
    for geo, _v in features.shapes(ok.astype("uint8"), mask=ok, transform=tr):
        aneis = [[tuple(q) for q in anel[:-1]] for anel in geo["coordinates"]]
        if rm._area_do_anel(aneis[0]) < 30 * lado * lado:
            continue
        ext = rm._simplificar(aneis[0] + aneis[0][:1], 1.5 * lado)[:-1]
        bur = [rm._simplificar(b + b[:1], 1.5 * lado)[:-1] for b in aneis[1:]
               if rm._area_do_anel(b) >= 30 * lado * lado]

        def _g(pts):
            lon, lat = warp.transform(t["crs"], "EPSG:4326",
                                      [q[0] for q in pts], [q[1] for q in pts])
            return list(zip(lat, lon))
        if len(ext) >= 3:
            polis.append({"externo": _g(ext),
                          "buracos": [_g(b) for b in bur if len(b) >= 3]})
    if not polis:
        raise RuntimeError(f"{Path(caminho).name}: o LAS não tem pontos de terreno")
    lat = [q[0] for g in polis for q in g["externo"]]
    lon = [q[1] for g in polis for q in g["externo"]]
    p = Path(caminho)
    return {"nome": " ".join(p.stem.replace("_", " ").split()) or p.stem,
            "poligonos": polis, "arquivo": p.name,
            "caixa": (min(lat), max(lat), min(lon), max(lon)),
            "terreno": str(p.resolve()), "crs_terreno": t["crs"].to_wkt()}


def caixa_do_las(caminho):
    """A extensão do LAS em graus, pelo cabeçalho (rápido)."""
    from rasterio import warp
    t = ler_terreno_las(caminho)
    tr, (ny, nx) = t["transform"], t["z"].shape
    xs = [tr.c, tr.c + tr.a * nx]
    ys = [tr.f, tr.f + tr.e * ny]
    lon, lat = warp.transform(t["crs"], "EPSG:4326",
                              [xs[0], xs[1], xs[0], xs[1]], [ys[0], ys[0], ys[1], ys[1]])
    return (min(lat), max(lat), min(lon), max(lon))


def mosaico(caminhos, celula_m=None, max_celulas=40e6, crs_de=None):
    """Os terrenos de todas as áreas numa grade só, no sistema de
    coordenadas do primeiro (em metros). O caminho do sinal de uma ERM
    atravessa áreas: o perfil precisa do relevo das duas."""
    import numpy as np
    from rasterio import warp
    from rasterio.crs import CRS
    from rasterio.enums import Resampling
    from affine import Affine
    crs_de = crs_de or {}
    ts = [ler_terreno_las(c, celula_m, crs=crs_de.get(c) or crs_padrao_las())
          for c in dict.fromkeys(caminhos)]
    if not ts:
        return None
    crs = ts[0]["crs"]
    if crs.is_geographic:
        # Grau não é metro: vai para o UTM do centro.
        tr0 = ts[0]["transform"]
        lon = tr0.c + tr0.a * ts[0]["z"].shape[1] / 2
        lat = tr0.f + tr0.e * ts[0]["z"].shape[0] / 2
        zona = int((lon + 180) // 6) + 1
        crs = CRS.from_epsg((32700 if lat < 0 else 32600) + zona)
    cel = max(abs(t["transform"].a) for t in ts if not t["crs"].is_geographic) \
        if any(not t["crs"].is_geographic for t in ts) else float(celula_m or PADROES["celula_m"])
    limites = []
    for t in ts:
        tr, (ny, nx) = t["transform"], t["z"].shape
        xs, ys = warp.transform(t["crs"], crs, [tr.c, tr.c + tr.a * nx] * 2,
                                [tr.f, tr.f, tr.f + tr.e * ny, tr.f + tr.e * ny])
        limites.append((min(xs), min(ys), max(xs), max(ys)))
    x0 = min(b[0] for b in limites); y0 = min(b[1] for b in limites)
    x1 = max(b[2] for b in limites); y1 = max(b[3] for b in limites)
    while ((x1 - x0) / cel) * ((y1 - y0) / cel) > max_celulas:
        cel *= 1.5
    nx, ny = int(math.ceil((x1 - x0) / cel)), int(math.ceil((y1 - y0) / cel))
    tr = Affine(cel, 0, x0, 0, -cel, y1)
    z = np.full((ny, nx), np.nan, dtype="float32")
    for t in ts:
        tmp = np.full((ny, nx), np.nan, dtype="float32")
        warp.reproject(t["z"], tmp, src_transform=t["transform"], src_crs=t["crs"],
                       dst_transform=tr, dst_crs=crs, src_nodata=np.nan,
                       dst_nodata=np.nan, resampling=Resampling.bilinear)
        z = np.where(np.isfinite(z), np.fmax(z, tmp), tmp)
    return {"z": z, "transform": tr, "crs": crs,
            "arquivos": [t["arquivo"] for t in ts]}


def _z_no_ponto(mos, x, y):
    tr = mos["transform"]
    c, l = ~tr * (x, y)
    c, l = int(c), int(l)
    if 0 <= l < mos["z"].shape[0] and 0 <= c < mos["z"].shape[1]:
        v = float(mos["z"][l, c])
        return v if math.isfinite(v) else None
    return None


# ══════════════════════════════════════════════════════════════
# ERB/ERM
# ══════════════════════════════════════════════════════════════
def _num(v):
    if v is None:
        return None
    v = str(v).strip().replace(",", ".")
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def ler_infraestrutura(caminho):
    """A planilha das ERB/ERM (`infraestrutura.csv`, separada por ";" ou
    ","): nome, lat, lon, altura do mastro, potência, ganho, perda de
    cabo, banda, azimute e abertura de setor, e se é proposta. Linha com
    "proposta" = sim é uma ERB/ERM que ainda não existe — o "e se"."""
    p = Path(caminho)
    if not p.exists():
        return []
    txt = p.read_text(encoding="utf-8-sig")
    sep = ";" if txt.count(";") >= txt.count(",") else ","
    linhas = []
    for r in csv.DictReader(txt.splitlines(), delimiter=sep):
        # Linha com mais colunas que o cabeçalho: o que sobra não tem nome
        # e fica de fora.
        r = {k.strip().lower(): (v or "").strip() for k, v in r.items()
             if k is not None and not isinstance(v, list)}
        nome = r.get("nome")
        if not nome:
            continue
        linhas.append({
            "nome": nome, "lat": _num(r.get("lat")), "lon": _num(r.get("lon")),
            "altura_m": _num(r.get("altura_m")),
            "potencia_dbm": _num(r.get("potencia_dbm")),
            "ganho_dbi": _num(r.get("ganho_dbi")),
            "perda_cabo_db": _num(r.get("perda_cabo_db")),
            "banda": rm._norm_banda(r.get("banda")) if r.get("banda") else None,
            "azimute": _num(r.get("azimute")), "abertura": _num(r.get("abertura")),
            "proposta": (r.get("proposta") or "").lower() in ("sim", "s", "1", "x", "yes"),
        })
    return linhas


def completar_infraestrutura(caminho, malha=None, nomes=None):
    """Acrescenta à planilha as ERB/ERM que a busca achou (com a posição
    do GPS delas) e as que o survey ouviu, sem mexer no que já estiver
    preenchido. Cria a planilha se ela não existir. Devolve quantas
    entraram."""
    p = Path(caminho)
    existentes = {l["nome"].casefold() for l in ler_infraestrutura(p)}
    rx = re.compile(rm.PADRAO_INFRA, re.I)
    novas = []
    for ip, v in (malha or {}).items():
        n = (v or {}).get("nome")
        if not n or n == ip or not rx.search(n) or n.casefold() in existentes:
            continue
        existentes.add(n.casefold())
        pot = [w.get("txpwr") for w in (v.get("wlans") or []) if w.get("txpwr")]
        novas.append({"nome": n, "lat": v.get("lat"), "lon": v.get("lon"),
                      "potencia_dbm": max(pot) if pot else None})
    for n in sorted(nomes or []):
        if n and rx.search(n) and n.casefold() not in existentes:
            existentes.add(n.casefold())
            novas.append({"nome": n})
    if not novas:
        return 0
    txt = p.read_text(encoding="utf-8-sig") if p.exists() else ""
    novo = not txt.strip()
    # Segue o cabeçalho e o separador que a planilha já tem: quem a editou
    # pode ter tirado colunas.
    if novo:
        sep, cols = ";", COLUNAS_INFRA
    else:
        prim = txt.splitlines()[0]
        sep = ";" if prim.count(";") >= prim.count(",") else ","
        cols = [c.strip().lower() for c in prim.split(sep)]
    dec = (lambda v: f"{v:.7f}".replace(".", ",")) if sep == ";" else (lambda v: f"{v:.7f}")
    with open(p, "a", encoding="utf-8-sig" if novo else "utf-8", newline="") as fh:
        if not novo and not txt.endswith("\n"):
            fh.write("\n")
        w = csv.writer(fh, delimiter=sep)
        if novo:
            w.writerow(cols)
        for r in novas:
            val = {"nome": r.get("nome"),
                   "lat": "" if r.get("lat") is None else dec(r["lat"]),
                   "lon": "" if r.get("lon") is None else dec(r["lon"]),
                   "potencia_dbm": "" if r.get("potencia_dbm") is None
                   else f"{r['potencia_dbm']:g}"}
            w.writerow([val.get(c, "") for c in cols])
    return len(novas)


def transmissores(malha=None, infra=None, prm=None):
    """As ERB/ERM do cálculo: a planilha manda; a busca completa posição
    e potência por banda que a planilha não tiver. Sem posição, a ERB/ERM
    fica de fora (e é dito)."""
    prm = prm or PADROES
    rx = re.compile(rm.PADRAO_INFRA, re.I)
    da_malha = {}
    for ip, v in (malha or {}).items():
        n = (v or {}).get("nome")
        if n and n != ip and rx.search(n):
            pot = {}
            for w in v.get("wlans") or []:
                b = rm._norm_banda(w.get("freq")) if w.get("freq") else None
                if b and w.get("txpwr"):
                    pot[b] = max(pot.get(b, -999), float(w["txpwr"]))
            da_malha[n.casefold()] = {"nome": n, "lat": v.get("lat"),
                                      "lon": v.get("lon"), "alt": v.get("alt"),
                                      "potencia": pot}
    out, vistos = [], set()
    for l in infra or []:
        m = da_malha.get(l["nome"].casefold(), {})
        pot = dict(m.get("potencia") or {})
        if l.get("potencia_dbm") is not None:
            for b in (l["banda"],) if l.get("banda") else FREQ_MHZ:
                pot[b] = l["potencia_dbm"]
        out.append({
            "nome": l["nome"],
            "lat": l["lat"] if l.get("lat") is not None else m.get("lat"),
            "lon": l["lon"] if l.get("lon") is not None else m.get("lon"),
            "alt": m.get("alt"),
            "altura_m": l.get("altura_m"), "ganho_dbi": l.get("ganho_dbi"),
            "perda_cabo_db": l.get("perda_cabo_db"), "potencia": pot,
            "bandas": [l["banda"]] if l.get("banda") else None,
            "azimute": l.get("azimute"), "abertura": l.get("abertura"),
            "proposta": l.get("proposta", False)})
        vistos.add(l["nome"].casefold())
    for k, m in da_malha.items():
        if k not in vistos:
            out.append(dict(m, altura_m=None, ganho_dbi=None, perda_cabo_db=None,
                            bandas=None, azimute=None, abertura=None, proposta=False))
    return out


# ══════════════════════════════════════════════════════════════
# O MODELO
# ══════════════════════════════════════════════════════════════
def perda_difracao_db(nu):
    """Gume de faca, ITU-R P.526: J(ν) para ν > −0,78; 0 abaixo."""
    import numpy as np
    nu = np.asarray(nu, dtype="float64")
    with np.errstate(invalid="ignore", divide="ignore"):
        nu_ok = np.where(nu > -0.78, nu, 0.0)
        j = 6.9 + 20 * np.log10(np.sqrt((nu_ok - 0.1) ** 2 + 1) + nu_ok - 0.1)
    return np.where(nu > -0.78, j, 0.0)


def _geometria(mos, tx_xy, h_tx, prm):
    """O perfil radial a partir da ERB/ERM: para cada ponto (raio,
    distância), o obstáculo dominante entre ela e o veículo. Não depende
    da banda — é calculado uma vez por ERB/ERM."""
    import numpy as np
    cel = abs(mos["transform"].a)
    R = float(prm["raio_m"])
    K = max(2, int(R / cel))
    n_rad = int(min(3600, max(360, math.ceil(2 * math.pi * R / (2 * cel)))))
    ang = np.arange(n_rad) * (2 * math.pi / n_rad)
    d = (np.arange(1, K + 1) * cel).astype("float64")
    xs = tx_xy[0] + np.cos(ang)[:, None] * d[None, :]
    ys = tx_xy[1] + np.sin(ang)[:, None] * d[None, :]
    c = ((xs - mos["transform"].c) / mos["transform"].a).astype(np.int64)
    l = ((ys - mos["transform"].f) / mos["transform"].e).astype(np.int64)
    ny, nx = mos["z"].shape
    dentro = (c >= 0) & (c < nx) & (l >= 0) & (l < ny)
    z = np.full(xs.shape, np.nan, dtype="float64")
    z[dentro] = mos["z"][l[dentro], c[dentro]]
    # Curvatura: o terreno "desce" d²/(2kR) visto da antena.
    z_eff = z - d[None, :] ** 2 / (2 * prm["k"] * 6371000.0)
    s = np.where(np.isfinite(z_eff), (z_eff - h_tx) / d[None, :], -np.inf)
    cm = np.maximum.accumulate(s, axis=1)
    idx = np.where(s >= cm, np.arange(K)[None, :], 0)
    J = np.maximum.accumulate(idx, axis=1)
    M_ant = np.concatenate([np.full((n_rad, 1), -np.inf), cm[:, :-1]], axis=1)
    J_ant = np.concatenate([np.zeros((n_rad, 1), dtype=np.int64), J[:, :-1]], axis=1)
    h_rx = z_eff + prm["altura_rx_m"]
    s_rx = (h_rx - h_tx) / d[None, :]
    d1 = (J_ant + 1) * cel
    d2 = np.maximum(d[None, :] - d1, cel)
    h_obs = d1 * (M_ant - s_rx)
    # ν sem o comprimento de onda: ν = h·sqrt(2/λ·(1/d1 + 1/d2)).
    nu_base = np.where(np.isfinite(M_ant), h_obs * np.sqrt(2 * (1 / d1 + 1 / d2)),
                       -np.inf)
    d3 = np.sqrt(d[None, :] ** 2 + (h_rx - h_tx) ** 2)
    return {"n_rad": n_rad, "K": K, "cel": cel, "d3": d3, "nu_base": nu_base,
            "ok": np.isfinite(z_eff), "ang": ang}


def _rssi_polar(geo, f_mhz, base_db, azimute=None, abertura=None):
    import numpy as np
    lam = 299.792458 / f_mhz
    nu = geo["nu_base"] / math.sqrt(lam)
    fspl = 20 * np.log10(geo["d3"]) + 20 * math.log10(f_mhz) - 27.55
    rssi = base_db - fspl - perda_difracao_db(nu)
    if azimute is not None and abertura:
        rumo = (90.0 - np.degrees(geo["ang"])) % 360.0
        dif = np.abs((rumo - azimute + 180.0) % 360.0 - 180.0)
        rssi = rssi - np.minimum(12.0 * (dif / abertura) ** 2, 20.0)[:, None]
    return np.where(geo["ok"], rssi, np.nan).astype("float32")


def _no_polar(geo, tx_xy, xs, ys, polar):
    """O valor de `polar` nos pontos (xs, ys)."""
    import numpy as np
    dx, dy = np.asarray(xs) - tx_xy[0], np.asarray(ys) - tx_xy[1]
    d = np.hypot(dx, dy)
    a = np.mod(np.arctan2(dy, dx), 2 * math.pi)
    ri = np.round(a / (2 * math.pi / geo["n_rad"])).astype(np.int64) % geo["n_rad"]
    ki = np.clip(np.round(d / geo["cel"]).astype(np.int64) - 1, 0, geo["K"] - 1)
    v = polar[ri, ki].astype("float64")
    return np.where(d <= geo["K"] * geo["cel"], v, np.nan)


def estimar(mos, txs, bandas, prm, medidas=None, aviso=None):
    """A melhor ERB/ERM em cada célula do terreno, por banda.

    `medidas`: [(nome da ERB/ERM, banda, x, y, RSSI medido)] — o estimado
    em cada uma volta junto, para a calibração.

    Devolve ({banda: grade}, [(nome, banda, medido, estimado)], usados)."""
    import numpy as np
    from rasterio import warp
    aviso = aviso or (lambda t: None)
    melhor = {b: np.full(mos["z"].shape, np.nan, dtype="float32") for b in bandas}
    pares, usados = [], []
    tr = mos["transform"]
    por_tx = {}
    for m in medidas or []:
        por_tx.setdefault(m[0].casefold(), []).append(m)
    for tx in txs:
        if tx.get("lat") is None or tx.get("lon") is None:
            aviso(f"  · {tx['nome']}: sem posição, fora da estimativa")
            continue
        xs, ys = warp.transform("EPSG:4326", mos["crs"], [tx["lon"]], [tx["lat"]])
        xy = (xs[0], ys[0])
        altura = tx.get("altura_m") if tx.get("altura_m") is not None else prm["altura_tx_m"]
        z0 = _z_no_ponto(mos, *xy)
        if z0 is None:
            if tx.get("alt") is None:
                aviso(f"  · {tx['nome']}: fora do terreno e sem altitude do GPS, "
                      f"fora da estimativa")
                continue
            h_tx = float(tx["alt"])            # o GPS já está na antena
        else:
            h_tx = z0 + altura
        geo = _geometria(mos, xy, h_tx, prm)
        usados.append(dict(tx, altura_usada=altura,
                           altura_padrao=tx.get("altura_m") is None))
        R = geo["K"] * geo["cel"]
        c0, l0 = ~tr * (xy[0] - R, xy[1] + R)
        c1, l1 = ~tr * (xy[0] + R, xy[1] - R)
        c0, l0 = max(0, int(c0)), max(0, int(l0))
        c1 = min(mos["z"].shape[1], int(math.ceil(c1)))
        l1 = min(mos["z"].shape[0], int(math.ceil(l1)))
        if c1 <= c0 or l1 <= l0:
            continue
        cc, ll = np.meshgrid(np.arange(c0, c1), np.arange(l0, l1))
        gx = tr.c + (cc + 0.5) * tr.a
        gy = tr.f + (ll + 0.5) * tr.e
        for b in bandas:
            if tx.get("bandas") and b not in tx["bandas"]:
                continue
            pot = (tx.get("potencia") or {}).get(b, prm["potencia_dbm"])
            base = (pot + (tx["ganho_dbi"] if tx.get("ganho_dbi") is not None
                           else prm["ganho_tx_dbi"])
                    + prm["ganho_rx_dbi"]
                    - (tx["perda_cabo_db"] if tx.get("perda_cabo_db") is not None
                       else prm["perda_cabo_db"]))
            polar = _rssi_polar(geo, FREQ_MHZ.get(b, 5785.0), base,
                                tx.get("azimute"), tx.get("abertura"))
            v = _no_polar(geo, xy, gx, gy, polar).astype("float32")
            jan = melhor[b][l0:l1, c0:c1]
            melhor[b][l0:l1, c0:c1] = np.where(np.isfinite(jan), np.fmax(jan, v), v)
            ms = [m for m in por_tx.get(tx["nome"].casefold(), []) if m[1] == b]
            if ms:
                est = _no_polar(geo, xy, [m[2] for m in ms], [m[3] for m in ms], polar)
                pares += [(tx["nome"], b, m[4], float(e)) for m, e in zip(ms, est)
                          if math.isfinite(e)]
    return melhor, pares, usados


def medidas_do_survey(amostras, peers, crs):
    """As leituras em que um veículo ouviu uma ERB/ERM: (nome, banda, x,
    y, RSSI), com a posição do veículo no sistema do terreno."""
    from rasterio import warp
    rx = re.compile(rm.PADRAO_INFRA, re.I)
    por_ponto = {i: a for i, a in enumerate(amostras, start=1)
                 if a.get("lat") is not None and a.get("lon") is not None}
    brutas = []
    for p in peers or []:
        n = (p.get("nome") or "").strip()
        if p.get("sinal") is None or not n or not rx.search(n):
            continue
        a = por_ponto.get(p.get("ponto"))
        b = rm._norm_banda(p.get("banda")) if p.get("banda") else None
        if a is None or not b:
            continue
        brutas.append((n, b, a["lon"], a["lat"], float(p["sinal"])))
    if not brutas:
        return []
    xs, ys = warp.transform("EPSG:4326", crs, [m[2] for m in brutas],
                            [m[3] for m in brutas])
    return [(m[0], m[1], x, y, m[4]) for m, x, y in zip(brutas, xs, ys)]


def calibrar(pares, bandas, minimo=20):
    """Por banda: a correção (mediana de medido − estimado) e o erro que
    sobra (RMS), e o mesmo por ERB/ERM. Com menos de `minimo` leituras a
    banda fica sem correção — e o relatório diz."""
    import numpy as np
    out = {}
    for b in bandas:
        r = np.array([m - e for _n, bb, m, e in pares if bb == b])
        if len(r) >= minimo:
            c = float(np.median(r))
            out[b] = {"correcao": round(c, 1),
                      "rms": round(float(np.sqrt(np.mean((r - c) ** 2))), 1),
                      "n": int(len(r)), "calibrado": True}
        else:
            out[b] = {"correcao": 0.0, "n": int(len(r)), "calibrado": False,
                      "rms": (round(float(np.sqrt(np.mean(r ** 2))), 1)
                              if len(r) else None)}
    por = {}
    for n, b, m, e in pares:
        por.setdefault((n, b), []).append(m - e - out[b]["correcao"])
    out["por_tx"] = {k: {"n": len(v), "vies": round(float(np.mean(v)), 1),
                         "rms": round(float(np.sqrt(np.mean(np.square(v)))), 1)}
                     for k, v in por.items()}
    return out


# ══════════════════════════════════════════════════════════════
# A VISUALIZAÇÃO — sobre a imagem da área
# ══════════════════════════════════════════════════════════════
_ALFA_ESTIMADO = 0.62    # mais transparente que o medido: é estimativa


def _relevo(z):
    """Sombreamento do relevo (sol a noroeste), para a área sem imagem."""
    import numpy as np
    zz = np.where(np.isfinite(z), z, np.nanmedian(z) if np.isfinite(z).any() else 0)
    gy, gx = np.gradient(zz)
    az, alt = math.radians(315), math.radians(45)
    dec = np.arctan(np.hypot(gx, gy))
    asp = np.arctan2(-gx, gy)
    sh = (np.sin(alt) * np.cos(dec) + np.cos(alt) * np.sin(dec) * np.cos(az - asp))
    return (np.clip(sh, 0, 1) * 200 + 40).astype("uint8")


def fundo_do_terreno(area, mos, margem=0.06, max_px=3000):
    """Sem imagem da área: o relevo sombreado do LAS faz de fundo."""
    import numpy as np
    from rasterio import warp, windows
    from affine import Affine
    lat = [q[0] for g in area["poligonos"] for q in g["externo"]]
    lon = [q[1] for g in area["poligonos"] for q in g["externo"]]
    xs, ys = warp.transform("EPSG:4326", mos["crs"], lon, lat)
    l, r_, b, t = min(xs), max(xs), min(ys), max(ys)
    mx, my = (r_ - l) * margem, (t - b) * margem
    ny, nx = mos["z"].shape
    win = windows.from_bounds(l - mx, b - my, r_ + mx, t + my, mos["transform"])
    win = win.intersection(windows.Window(0, 0, nx, ny)).round_offsets().round_lengths()
    if win.width < 2 or win.height < 2:
        return None
    z = mos["z"][win.row_off:win.row_off + win.height, win.col_off:win.col_off + win.width]
    f = max(1, int(math.ceil(max(z.shape) / max_px)))
    z = z[::f, ::f]
    cinza = _relevo(z)
    tr = windows.transform(win, mos["transform"]) * Affine.scale(f, f)
    return {"rgb": np.dstack([cinza] * 3), "valido": np.isfinite(z),
            "transform": tr, "crs": mos["crs"], "arquivo": "relevo do LAS"}


def mapa_estimado(area, grade, mos, fundo, txs):
    """A cobertura estimada sobre a imagem da área, na régua do RSSI,
    recortada na área, com as ERB/ERM marcadas. RGBA."""
    import numpy as np
    from rasterio import warp, features
    from rasterio.enums import Resampling
    from PIL import Image, ImageDraw
    h, w = fundo["rgb"].shape[:2]
    val = np.full((h, w), np.nan, dtype="float32")
    warp.reproject(grade, val, src_transform=mos["transform"], src_crs=mos["crs"],
                   dst_transform=fundo["transform"], dst_crs=fundo["crs"],
                   src_nodata=np.nan, dst_nodata=np.nan,
                   resampling=Resampling.bilinear)
    geoms = []
    for g in area["poligonos"]:
        aneis = []
        for anel in [g["externo"]] + g["buracos"]:
            xs, ys = warp.transform("EPSG:4326", fundo["crs"],
                                    [q[1] for q in anel], [q[0] for q in anel])
            aneis.append(list(zip(xs, ys)) + [(xs[0], ys[0])])
        geoms.append({"type": "Polygon", "coordinates": aneis})
    dentro = features.rasterize(((gg, 1) for gg in geoms), out_shape=(h, w),
                                transform=fundo["transform"], fill=0,
                                dtype="uint8").astype(bool)
    faixas = rm.FAIXAS_KML["sinal_cob"]
    baixo = rm.limites_na_faixa_de_baixo("sinal_cob")
    paleta = np.array([[int(c[i:i + 2], 16) for i in (0, 2, 4)] for _l, c in faixas],
                      dtype="float32")
    ok = np.isfinite(val) & dentro
    k = np.zeros((h, w), dtype=np.int64)
    if ok.any():
        k[ok] = _faixas_vetor(val[ok], faixas, baixo)
    rgb = fundo["rgb"].astype("float32")
    a = np.where(ok, _ALFA_ESTIMADO, 0.0)[..., None]
    rgb = rgb * (1 - a) + paleta[k] * a
    out = np.dstack([np.clip(rgb, 0, 255).astype("uint8"),
                     np.where(fundo["valido"] | ok, 255, 0).astype("uint8")])
    im = Image.fromarray(out, "RGBA")
    rm._desenhar_contorno(im, area, fundo)
    dr = ImageDraw.Draw(im)
    fonte = rm._fonte_pil(max(14, round(max(w, h) / 70)))
    lado = max(7, round(max(w, h) / 160))
    inv = ~fundo["transform"]
    for tx in txs:
        if tx.get("lat") is None:
            continue
        xs, ys = warp.transform("EPSG:4326", fundo["crs"], [tx["lon"]], [tx["lat"]])
        c, l = inv * (xs[0], ys[0])
        if not (0 <= c < w and 0 <= l < h):
            continue
        cor = (255, 255, 255, 255) if tx.get("proposta") else (20, 20, 20, 255)
        dr.polygon([(c, l - lado), (c - lado, l + lado * 0.8), (c + lado, l + lado * 0.8)],
                   fill=(241, 196, 15, 255), outline=cor, width=2)
        dr.text((c + lado + 3, l - lado), tx["nome"] + (" (proposta)" if tx.get("proposta") else ""),
                fill=(26, 26, 26, 255), font=fonte, stroke_width=2,
                stroke_fill=(255, 255, 255, 255))
    return np.array(im), (val, dentro)


def _faixas_vetor(v, faixas, baixo):
    """`_faixa_idx` para um vetor inteiro de uma vez."""
    import numpy as np
    lims = np.array([l for l, _c in faixas], dtype="float64")
    na_baixo = np.array([l in (baixo or set()) for l, _c in faixas])
    k = np.searchsorted(lims, v, side="right")
    # Valor exatamente no limite que pertence à faixa de baixo.
    igual = (k > 0) & np.isclose(v, lims[np.clip(k - 1, 0, len(lims) - 1)])
    k = np.where(igual & na_baixo[np.clip(k - 1, 0, len(lims) - 1)], k - 1, k)
    return np.clip(k, 0, len(faixas) - 1)


# ══════════════════════════════════════════════════════════════
# O RELATÓRIO
# ══════════════════════════════════════════════════════════════
def gerar(amostras, peers, areas, destino, cfg=None, malha=None, nome=None,
          aviso=None, infra=None, quando=None):
    """Cobertura estimada das áreas com LAS: o deck próprio e os GeoTIFF
    em `destino`/estimativa. Devolve os arquivos gravados (vazio sem LAS
    ou sem ERB/ERM com posição)."""
    import numpy as np
    from datetime import datetime
    aviso = aviso or (lambda t: None)
    prm = parametros(cfg)
    caminhos = [a["terreno"] for a in areas or [] if a.get("terreno")]
    if not caminhos:
        return []
    infra = Path(infra or (cfg.get("cobertura", "infraestrutura", fallback="")
                           if cfg is not None and cfg.has_section("cobertura") else "")
                 or INFRA_PADRAO)
    vistos = {(p.get("nome") or "").strip() for p in peers or []}
    novas = completar_infraestrutura(infra, malha, vistos)
    if novas:
        aviso(f"  {infra.name}: {novas} ERB/ERM acrescentada(s) — preencha a "
              f"altura do mastro (e a posição, se faltar) para a estimativa")
    txs = transmissores(malha, ler_infraestrutura(infra), prm)
    if not any(t.get("lat") is not None for t in txs):
        aviso(f"  ! cobertura estimada não gerada: nenhuma ERB/ERM com posição "
              f"em {infra.name}")
        return []
    aviso(f"  cobertura estimada: terreno de {len(caminhos)} LAS, "
          f"{sum(1 for t in txs if t.get('lat') is not None)} ERB/ERM…")
    mos = mosaico(caminhos, prm["celula_m"],
                  crs_de={a["terreno"]: a.get("crs_terreno") for a in areas
                          if a.get("terreno")})
    medidas = medidas_do_survey(amostras, peers, mos["crs"])
    # As bandas da rede: as das amostras e as em que as ERB/ERM foram
    # ouvidas (uma ERM pode só aparecer em 2,4 GHz no survey).
    bandas = sorted(({rm._norm_banda(a.get("banda")) for a in amostras or []
                      if a.get("banda")} | {m[1] for m in medidas}) - {None}) \
        or list(FREQ_MHZ)
    grades, pares, usados = estimar(mos, txs, bandas, prm, medidas, aviso)
    cal = calibrar(pares, bandas, prm["min_calibracao"])
    for b in bandas:
        grades[b] = grades[b] + cal[b]["correcao"]
        c = cal[b]
        aviso(f"    {b}: " + (f"correção {c['correcao']:+g} dB, erro {c['rms']:g} dB "
                              f"em {c['n']} leituras" if c["calibrado"] else
                              f"sem calibração ({c['n']} leituras)"))
    pasta = Path(destino) / "estimativa"
    pasta.mkdir(parents=True, exist_ok=True)
    feitos = []
    for b in bandas:
        suf = b.replace(" ", "").replace(".", "")
        alvo = pasta / f"cobertura_estimada_{suf}.tif"
        _gravar_grade(alvo, grades[b], mos)
        feitos.append(alvo)
    # Mapas por área e banda, sobre a imagem (ou o relevo).
    mapas = {}
    for area in areas:
        try:
            fundo = rm.fundo_da_area(area) if area.get("imagem") else None
        except Exception as e:
            aviso(f"  ! imagem de {area['nome']}: {e}")
            fundo = None
        fundo = fundo or fundo_do_terreno(area, mos)
        if fundo is None:
            continue
        am_ar = [a for a in amostras or [] if a.get("area") == area["nome"]]
        for b in bandas:
            try:
                mapa, (val, dentro) = mapa_estimado(area, grades[b], mos, fundo, usados)
            except Exception as e:
                aviso(f"  ! estimativa de {area['nome']} ({b}): {e}")
                continue
            vv = val[dentro & np.isfinite(val)]
            if not vv.size:
                continue
            req = float(rm.ESCALAS["sinal_cob"]["req"])
            am_b = [a for a in am_ar if rm._norm_banda(a.get("banda")) == b]
            medido = (rm.survey_resumo(am_b) or {}).get("sinal_cob") if am_b else None
            mapas[(area["nome"], b)] = {
                "png": rm.png_do_mapa(mapa, fundo), "imagem": fundo["arquivo"],
                "pct_acima": round(float((vv > req).mean() * 100), 1),
                "mediana": round(float(np.median(vv)), 1),
                "medido_pct": medido["pct_ok"] if medido else None}
            alvo = pasta / f"{rm._slug_arquivo(area['nome'])[:50]}_cobertura_estimada_" \
                           f"{b.replace(' ', '').replace('.', '')}.tif"
            try:
                rm.gravar_geotiff_do_mapa(alvo, mapa, fundo)
                feitos.append(alvo)
            except Exception as e:
                aviso(f"  ! GeoTIFF da estimativa de {area['nome']}: {e}")
    try:
        dados, nome_ppt = ppt_estimativa(nome or "Site Survey", areas, bandas, mapas,
                                         usados, cal, prm, mos, quando)
        alvo = Path(destino) / nome_ppt
        alvo.write_bytes(dados); feitos.append(alvo)
    except Exception as e:
        aviso(f"  ! deck da cobertura estimada não gerado: {e}")
    return feitos


def _gravar_grade(caminho, grade, mos):
    """A estimativa em dBm, float, para o QGIS/ArcGIS estilizar."""
    rio = rm._rasterio()
    import numpy as np
    with rio.open(caminho, "w", driver="GTiff", width=grade.shape[1],
                  height=grade.shape[0], count=1, dtype="float32", crs=mos["crs"],
                  transform=mos["transform"], nodata=np.nan, compress="deflate",
                  tiled=True) as d:
        d.write(grade.astype("float32"), 1)
        d.set_band_description(1, "RSSI estimado (dBm), melhor ERB/ERM")


def ppt_estimativa(nome_sv, areas, bandas, mapas, usados, cal, prm, mos,
                   quando=None):
    """O deck da cobertura estimada, na identidade Anglo."""
    from pptx import Presentation
    from pptx.util import Inches
    from datetime import datetime
    import io as _io
    quando = quando or datetime.now()
    p = Presentation()
    p.slide_width, p.slide_height = Inches(13.333), Inches(7.5)
    rm.capa_anglo(p, f"Cobertura Estimada\n{nome_sv}",
                  f"{quando:%d/%m/%Y} · estimativa pelo relevo (LAS) — não é medição")
    req = float(rm.ESCALAS["sinal_cob"]["req"])

    s = rm.slide_anglo(p, "Premissas da estimativa",
                       "Como o número saiu — e quanto ele erra contra o survey")
    n_pad = sum(1 for t in usados if t.get("altura_padrao"))
    ficha = [
        ["Terreno", f"{', '.join(mos['arquivos'])} · célula de "
                    f"{abs(mos['transform'].a):g} m (ponto mais alto da célula)"],
        ["Modelo", "espaço livre + difração no obstáculo dominante do perfil "
                   "(ITU-R P.526), curvatura da Terra k = 4/3"],
        ["Antena no veículo", f"{prm['altura_rx_m']:g} m acima do terreno, "
                              f"{prm['ganho_rx_dbi']:g} dBi"],
        ["ERB/ERM", f"{len(usados)} no cálculo"
                    + (f" · {n_pad} com altura padrão ({prm['altura_tx_m']:g} m)"
                       if n_pad else "")],
        ["Raio de cálculo", f"{prm['raio_m']:g} m a partir de cada ERB/ERM"],
    ]
    for b in bandas:
        c = cal.get(b) or {}
        ficha.append([f"Calibração {b}",
                      f"correção {c['correcao']:+g} dB · erro (RMS) {c['rms']:g} dB "
                      f"em {c['n']} leituras do survey" if c.get("calibrado") else
                      f"sem calibração: {c.get('n', 0)} leituras do survey "
                      f"(mínimo {prm['min_calibracao']})"])
    ficha.append(["O que o modelo não vê", "veículos e equipamentos (não estão no "
                  "LAS), reflexão nas paredes da cava, mudança da cava depois do LAS"])
    rm._tabela_anglo(s, 0.55, 1.5, 12.25, ["Premissa", "Valor"], ficha, tam=10)

    linhas = []
    for t in usados:
        pot = " / ".join(f"{b.split()[0]}: {v:g}" for b, v in sorted((t.get("potencia") or {}).items()))
        err = " · ".join(f"{b.split()[0]}: {cal['por_tx'][(t['nome'], b)]['rms']:g} dB "
                         f"({cal['por_tx'][(t['nome'], b)]['n']})"
                         for b in bandas if (t["nome"], b) in cal.get("por_tx", {}))
        linhas.append([t["nome"] + (" (proposta)" if t.get("proposta") else ""),
                       f"{t['altura_usada']:g} m" + (" (padrão)" if t.get("altura_padrao") else ""),
                       pot or f"{prm['potencia_dbm']:g} (padrão)",
                       f"{t['azimute']:g}° / {t['abertura']:g}°" if t.get("azimute") is not None
                       and t.get("abertura") else "omni",
                       err or "—"])
    for k in range(0, max(1, len(linhas)), 14):
        s = rm.slide_anglo(p, "ERB/ERM no cálculo" + (f" ({k // 14 + 1})" if len(linhas) > 14 else ""),
                           "Altura do mastro, potência por banda (dBm), setor e o erro "
                           "do modelo em cada uma (leituras)")
        rm._tabela_anglo(s, 0.55, 1.5, 12.25,
                         ["ERB/ERM", "Altura", "Potência (dBm)", "Setor", "Erro do modelo"],
                         linhas[k:k + 14], tam=9.5)

    for area in areas:
        for b in bandas:
            m = mapas.get((area["nome"], b))
            if not m:
                continue
            s = rm.slide_anglo(p, f"{area['nome']} · Cobertura estimada (RSSI) — {b}",
                               f"ESTIMATIVA — melhor ERB/ERM em cada ponto  ·  requisito "
                               f"> {req:g} dBm")
            rm._mapa_anglo(s, 0.55, 1.45, 7.3, 5.4, m["png"],
                           f"Imagem: {m['imagem']}   ·   estimativa pelo relevo   ·   {b}")
            c = cal.get(b) or {}
            cartoes = [(f"Área acima de {req:g} dBm", f"{m['pct_acima']:.1f}%"),
                       ("Mediana estimada", f"{m['mediana']:g} dBm"),
                       ("Medido nas rotas — dentro", f"{m['medido_pct']:.1f}%"
                        if m.get("medido_pct") is not None else "—"),
                       ("Erro do modelo (RMS)", f"{c['rms']:g} dB" if c.get("rms") is not None
                        else "—")]
            for k, (rot, val) in enumerate(cartoes):
                rm._cartao_anglo(s, 8.05 + (k % 2) * 2.45, 1.45 + (k // 2) * 0.9,
                                 2.3, 0.8, rot, val)
            rm._txt_anglo(s, 8.05, 3.4, 4.75, 2.2,
                          "Estimado ≠ medido. A cor diz o que o relevo permite com a "
                          "altura e a potência informadas; o survey diz o que a rede "
                          "entregou nas rotas. Onde os dois divergem, vale o medido — "
                          "e a divergência aponta altura, potência ou obstáculo a "
                          "conferir.", 9.5, False, rm.ANGLO["suave"])
            rm._escala_anglo(s, 8.05, 6.05, 4.75, "sinal_cob")
    rm._navegacao_anglo(p)
    buf = _io.BytesIO(); p.save(buf)
    seguro = re.sub(r"[^\w\-]+", "_", nome_sv)[:40]
    return buf.getvalue(), f"Cobertura_Estimada_{seguro}_{quando:%Y%m%d}.pptx"
