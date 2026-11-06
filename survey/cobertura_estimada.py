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
    "max_correcao_db": 15.0, # correção maior que isto não é calibração: é dado errado
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
    distância), as duas cristas que mais barram o caminho até o veículo
    (Deygout): a dominante vista da antena e, entre ela e o veículo, a
    dominante vista da primeira. Numa cava, o sinal costuma passar por
    mais de uma bancada. Não depende da banda — é calculado uma vez por
    ERB/ERM."""
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
    del xs, ys, c, l, dentro
    # Curvatura: o terreno "desce" d²/(2kR) visto da antena.
    z_eff = z - d[None, :] ** 2 / (2 * prm["k"] * 6371000.0)
    ok = np.isfinite(z_eff)
    ar = np.arange(K)[None, :]
    # ── 1ª crista: a de maior inclinação vista da antena ──
    s_ = np.where(ok, (z_eff - h_tx) / d[None, :], -np.inf)
    cm = np.maximum.accumulate(s_, axis=1)
    J = np.maximum.accumulate(np.where(s_ >= cm, ar, 0), axis=1)
    M_ant = np.concatenate([np.full((n_rad, 1), -np.inf), cm[:, :-1]], axis=1)
    J_ant = np.concatenate([np.zeros((n_rad, 1), dtype=np.int64), J[:, :-1]], axis=1)
    h_rx = z_eff + prm["altura_rx_m"]
    s_rx = (h_rx - h_tx) / d[None, :]
    d1 = (J_ant + 1) * cel
    d2 = np.maximum(d[None, :] - d1, cel)
    # ν sem o comprimento de onda: ν = h·sqrt(2/λ·(1/d1 + 1/d2)).
    nu1 = np.where(np.isfinite(M_ant),
                   d1 * (M_ant - s_rx) * np.sqrt(2 * (1 / d1 + 1 / d2)), -np.inf)
    # ── 2ª crista: entre a 1ª e o veículo, a de maior inclinação vista
    # do topo da 1ª. Os pontos com a mesma 1ª crista formam um trecho
    # contínuo do raio; o máximo acumulado recomeça a cada trecho.
    zm = np.take_along_axis(np.where(ok, z_eff, -1e6), J_ant, axis=1)
    dm = d[J_ant]
    # Pelo menos 30 m depois da 1ª: o topo plano da própria crista (a
    # berma) não é uma segunda crista.
    sep = max(30.0, 3 * cel)
    t = np.where(ok & (d[None, :] - dm >= sep),
                 (z_eff - zm) / np.maximum(d[None, :] - dm, cel), -np.inf)
    muda = np.concatenate([np.ones((n_rad, 1), bool), J_ant[:, 1:] != J_ant[:, :-1]], axis=1)
    grp = np.cumsum(muda, axis=1).astype("float64")
    G = 1e5
    tt = np.clip(np.where(np.isfinite(t), t, -4e4), -4e4, 4e4) + G * grp
    cmg = np.maximum.accumulate(tt, axis=1)
    J2 = np.maximum.accumulate(np.where(tt >= cmg, ar, 0), axis=1)
    mesmo = ~muda
    T_ant = np.concatenate([np.full((n_rad, 1), -np.inf), cmg[:, :-1]], axis=1) - G * grp
    T_ant = np.where(mesmo & (T_ant > -3.9e4), T_ant, -np.inf)
    J2_ant = np.concatenate([np.zeros((n_rad, 1), dtype=np.int64), J2[:, :-1]], axis=1)
    d_j2 = (J2_ant + 1) * cel
    t_rx = (h_rx - zm) / np.maximum(d[None, :] - dm, cel)
    a1 = np.maximum(d_j2 - dm, cel)
    a2 = np.maximum(d[None, :] - d_j2, cel)
    nu2 = np.where(np.isfinite(T_ant),
                   a1 * (T_ant - t_rx) * np.sqrt(2 * (1 / a1 + 1 / a2)), -np.inf)
    d3 = np.sqrt(d[None, :] ** 2 + (h_rx - h_tx) ** 2)
    return {"n_rad": n_rad, "K": K, "cel": cel, "d3": d3, "nu1": nu1, "nu2": nu2,
            "ok": ok, "ang": ang}


def _rssi_polar(geo, f_mhz, base_db, azimute=None, abertura=None):
    import numpy as np
    raiz = math.sqrt(299.792458 / f_mhz)
    fspl = 20 * np.log10(geo["d3"]) + 20 * math.log10(f_mhz) - 27.55
    nu1 = geo["nu1"] / raiz
    # A 2ª crista só conta quando a 1ª bloqueia de fato (Deygout).
    perda = perda_difracao_db(nu1) + np.where(nu1 > 0, perda_difracao_db(geo["nu2"] / raiz), 0.0)
    rssi = base_db - fspl - perda
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
    v = polar[ri, ki].astype("float32")
    return np.where(d <= geo["K"] * geo["cel"], v, np.nan)


def calcular(mos, txs, bandas, prm, medidas=None, aviso=None):
    """O sinal de CADA ERB/ERM, por banda, em coordenadas polares em volta
    dela (sem calibração).

    `medidas`: [(nome, banda, x, y, RSSI medido, …)] — o estimado em cada
    uma volta junto, para a calibração.

    Devolve ({nome: {"xy", "geo", banda: polar}}, [(nome, banda, medido,
    estimado)], usadas)."""
    import numpy as np
    from rasterio import warp
    aviso = aviso or (lambda t: None)
    polares, pares, usados = {}, [], []
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
        info = {"xy": xy, "geo": {k: geo[k] for k in ("n_rad", "K", "cel")}}
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
            ms = [m for m in por_tx.get(tx["nome"].casefold(), []) if m[1] == b]
            if ms:
                est = _no_polar(info["geo"], xy, [m[2] for m in ms], [m[3] for m in ms], polar)
                pares += [(tx["nome"], b, m[4], float(e)) for m, e in zip(ms, est)
                          if math.isfinite(e)]
            # float16 basta (décimo de dB) e é metade da memória.
            info[b] = polar.astype("float16")
        del geo
        polares[tx["nome"]] = info
    return polares, pares, usados


def grade_de(mos, info, banda, correcao=0.0, destino=None):
    """O sinal de uma ERB/ERM (`info` de `calcular`) na grade do terreno,
    ou em `destino` (uma grade melhor já existente: fica o maior)."""
    import numpy as np
    tr = mos["transform"]
    g = info["geo"]
    R = g["K"] * g["cel"]
    xy = info["xy"]
    c0, l0 = ~tr * (xy[0] - R, xy[1] + R)
    c1, l1 = ~tr * (xy[0] + R, xy[1] - R)
    c0, l0 = max(0, int(c0)), max(0, int(l0))
    c1 = min(mos["z"].shape[1], int(math.ceil(c1)))
    l1 = min(mos["z"].shape[0], int(math.ceil(l1)))
    out = destino if destino is not None else np.full(mos["z"].shape, np.nan, dtype="float32")
    if c1 <= c0 or l1 <= l0 or banda not in info:
        return out
    cc, ll = np.meshgrid(np.arange(c0, c1), np.arange(l0, l1))
    v = _no_polar(g, xy, tr.c + (cc + 0.5) * tr.a, tr.f + (ll + 0.5) * tr.e,
                  info[banda]) + correcao
    jan = out[l0:l1, c0:c1]
    out[l0:l1, c0:c1] = np.where(np.isfinite(jan), np.fmax(jan, v), v)
    return out


def estimar(mos, txs, bandas, prm, medidas=None, aviso=None):
    """O conjunto das ERB/ERM em cada célula do terreno (o sinal mais forte
    entre elas), por banda, sem calibração.

    Devolve ({banda: grade}, [(nome, banda, medido, estimado)], usadas)."""
    polares, pares, usados = calcular(mos, txs, bandas, prm, medidas, aviso)
    grades = {}
    for b in bandas:
        g = None
        for info in polares.values():
            g = grade_de(mos, info, b, destino=g)
        grades[b] = g
    return grades, pares, usados


def medidas_do_survey(amostras, peers, crs):
    """As leituras em que um veículo ouviu uma ERB/ERM: (nome, banda, x,
    y, RSSI, lat, lon), com a posição do veículo no sistema do terreno."""
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
    return [(m[0], m[1], x, y, m[4], m[3], m[2]) for m, x, y in zip(brutas, xs, ys)]


def calibrar(pares, bandas, minimo=20, max_correcao=15.0):
    """Por banda: a correção (mediana de medido − estimado) e o erro que
    sobra (RMS), e o mesmo por ERB/ERM. Com menos de `minimo` leituras a
    banda fica sem correção — e o relatório diz.

    Correção acima de `max_correcao` dB não é aplicada: ganho de antena e
    perda de cabo não explicam tanto, e o que sobra é posição, altura ou
    potência de ERB/ERM errada. Fica registrada como rejeitada."""
    import numpy as np
    out = {}
    for b in bandas:
        r = np.array([m - e for _n, bb, m, e in pares if bb == b])
        c = float(np.median(r)) if len(r) else 0.0
        if len(r) >= minimo and abs(c) > max_correcao:
            out[b] = {"correcao": 0.0, "rejeitada": round(c, 1), "n": int(len(r)),
                      "calibrado": False,
                      "rms": round(float(np.sqrt(np.mean(r ** 2))), 1)}
        elif len(r) >= minimo:
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
# O MAPA DE CALOR — sobre a imagem da área, com o relevo
# ══════════════════════════════════════════════════════════════
# A cor é contínua, nas cores da régua do RSSI (a mesma do medido),
# passando de uma faixa para a outra sem degrau; a linha branca marca o
# requisito (−75 dBm). O relevo do LAS sombreia a cor: a bancada e a
# encosta continuam visíveis por baixo do calor.
def _ancoras_da_regua(campo="sinal"):
    """Cada cor da régua no meio dos inteiros da sua faixa; as duas faixas
    abertas, 5 dB para fora do último corte. Sai da própria FAIXAS_KML:
    mudou a régua do medido, o calor do estimado muda junto."""
    fx = rm.FAIXAS_KML[campo]
    baixo = rm.limites_na_faixa_de_baixo(campo)
    teto = [l if l in baixo else l - 1 for l, _c in fx[:-1]]
    piso = [l + 1 if l in baixo else l for l, _c in fx[:-1]]
    out = [(float(teto[0] - 5), fx[0][1])]
    out += [((piso[k - 1] + teto[k]) / 2.0, fx[k][1]) for k in range(1, len(fx) - 1)]
    out.append((float(piso[-1] + 5), fx[-1][1]))
    return tuple(out)


_ANCORAS_CALOR = _ancoras_da_regua()
_ALFA_CALOR_EST = 0.72


def cor_continua(v):
    """RGB (float, 0–255) do RSSI `v` (array), na escala contínua."""
    import numpy as np
    xs = np.array([a for a, _c in _ANCORAS_CALOR])
    cs = np.array([[int(c[i:i + 2], 16) for i in (0, 2, 4)] for _a, c in _ANCORAS_CALOR],
                  dtype="float32")
    v = np.clip(np.asarray(v, dtype="float32"), xs[0], xs[-1])
    return np.stack([np.interp(v, xs, cs[:, k]) for k in range(3)], axis=-1)


def _relevo(z, lado_m=1.0):
    """Sombreamento do relevo (sol a noroeste), 0–1."""
    import numpy as np
    fin = np.isfinite(z)
    zz = np.where(fin, z, np.nanmedian(z) if fin.any() else 0.0)
    gy, gx = np.gradient(zz, lado_m)
    az, alt = math.radians(315), math.radians(45)
    dec = np.arctan(np.hypot(gx, gy))
    asp = np.arctan2(-gx, gy)
    return np.clip(np.sin(alt) * np.cos(dec)
                   + np.cos(alt) * np.sin(dec) * np.cos(az - asp), 0, 1)


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
    cinza = (_relevo(z, abs(mos["transform"].a) * f) * 200 + 40).astype("uint8")
    tr = windows.transform(win, mos["transform"]) * Affine.scale(f, f)
    return {"rgb": np.dstack([cinza] * 3), "valido": np.isfinite(z),
            "transform": tr, "crs": mos["crs"], "arquivo": "relevo do LAS"}


class GradeDaArea:
    """Tudo o que o mapa de uma área precisa, calculado uma vez: as
    coordenadas de cada pixel da imagem no sistema do terreno, o contorno
    da área nessa grade e o relevo sombreado."""

    def __init__(self, area, fundo, mos):
        import numpy as np
        from rasterio import warp, features
        from rasterio.enums import Resampling
        self.area, self.fundo, self.mos = area, fundo, mos
        h, w = fundo["rgb"].shape[:2]
        tr = fundo["transform"]
        # Coordenadas dos pixels no sistema do terreno. No mesmo sistema
        # é a conta direta; noutro, de 16 em 16 pixels e interpolado.
        cc, ll = np.meshgrid(np.arange(w) + 0.5, np.arange(h) + 0.5)
        X, Y = tr.c + cc * tr.a, tr.f + ll * tr.e
        if fundo["crs"] != mos["crs"]:
            from scipy.ndimage import map_coordinates
            pc, pl = np.arange(0, w + 16, 16), np.arange(0, h + 16, 16)
            gc, gl = np.meshgrid(pc + 0.5, pl + 0.5)
            gx, gy = warp.transform(fundo["crs"], mos["crs"],
                                    (tr.c + gc * tr.a).ravel(), (tr.f + gl * tr.e).ravel())
            gx = np.array(gx).reshape(gl.shape); gy = np.array(gy).reshape(gl.shape)
            coords = [ll / 16.0, cc / 16.0]
            X = map_coordinates(gx, coords, order=1, mode="nearest")
            Y = map_coordinates(gy, coords, order=1, mode="nearest")
        self.X, self.Y = X.astype("float64"), Y.astype("float64")
        geoms = []
        for g in area["poligonos"]:
            aneis = []
            for anel in [g["externo"]] + g["buracos"]:
                xs, ys = warp.transform("EPSG:4326", fundo["crs"],
                                        [q[1] for q in anel], [q[0] for q in anel])
                aneis.append(list(zip(xs, ys)) + [(xs[0], ys[0])])
            geoms.append({"type": "Polygon", "coordinates": aneis})
        self.dentro = features.rasterize(((gg, 1) for gg in geoms), out_shape=(h, w),
                                         transform=tr, fill=0, dtype="uint8").astype(bool)
        z = np.full((h, w), np.nan, dtype="float32")
        warp.reproject(mos["z"], z, src_transform=mos["transform"], src_crs=mos["crs"],
                       dst_transform=tr, dst_crs=fundo["crs"], src_nodata=np.nan,
                       dst_nodata=np.nan, resampling=Resampling.bilinear)
        self.sombra = _relevo(z, rm._metros_por_pixel(fundo))
        self.m_px = rm._metros_por_pixel(fundo)

    def valores(self, info, banda, correcao=0.0):
        """O sinal de uma ERB/ERM em cada pixel da área, suavizado na
        escala da célula do terreno (sem o degrau dos raios)."""
        import numpy as np
        from scipy.ndimage import gaussian_filter
        if banda not in info:
            return None
        v = _no_polar(info["geo"], info["xy"], self.X, self.Y, info[banda]) + correcao
        sig = max(0.5, 0.6 * info["geo"]["cel"] / max(self.m_px, 1e-6))
        fin = np.isfinite(v)
        if not fin.any():
            return v
        # Média ponderada só entre pixels com valor: a borda do raio de
        # cálculo não puxa o calor para baixo.
        num = gaussian_filter(np.where(fin, v, 0.0), sig)
        den = gaussian_filter(fin.astype("float32"), sig)
        return np.where(fin, num / np.maximum(den, 1e-3), np.nan).astype("float32")


def mapa_de_calor(grade, val, txs, destaque=None, req=None):
    """O mapa de calor sobre a imagem: a cor contínua da régua, o relevo
    sombreando a cor, a linha branca do requisito, o contorno da área e
    as ERB/ERM (a `destaque` em amarelo, as outras em cinza). RGBA."""
    import numpy as np
    from rasterio import warp
    from scipy.ndimage import binary_erosion, binary_dilation
    from PIL import Image, ImageDraw
    fundo = grade.fundo
    h, w = fundo["rgb"].shape[:2]
    req = float(rm.ESCALAS["sinal_cob"]["req"]) if req is None else req
    ok = np.isfinite(val) & grade.dentro
    # Abaixo de −95 dBm a cor some aos poucos: ali já não há o que pintar.
    alfa = np.where(ok, _ALFA_CALOR_EST * np.clip((val + 108.0) / 13.0, 0, 1), 0.0)
    cor = cor_continua(np.where(ok, val, -95.0))
    cor = cor * (0.62 + 0.38 * grade.sombra)[..., None]
    rgb = fundo["rgb"].astype("float32") * (1 - alfa[..., None]) + cor * alfa[..., None]
    # A linha do requisito, branca sobre uma sombra escura.
    acima = ok & (val > req)
    larg = max(2, round(max(w, h) / 700))
    borda = acima & ~binary_erosion(acima, iterations=larg)
    sombra_l = binary_dilation(borda, iterations=1) & ~borda
    rgb[sombra_l] = rgb[sombra_l] * 0.35
    rgb[borda] = 255.0
    out = np.dstack([np.clip(rgb, 0, 255).astype("uint8"),
                     np.where(fundo["valido"] | ok, 255, 0).astype("uint8")])
    im = Image.fromarray(out, "RGBA")
    rm._desenhar_contorno(im, grade.area, fundo)
    dr = ImageDraw.Draw(im)
    fonte = rm._fonte_pil(max(14, round(max(w, h) / 70)))
    inv = ~fundo["transform"]
    for tx in txs:
        if tx.get("lat") is None:
            continue
        xs, ys = warp.transform("EPSG:4326", fundo["crs"], [tx["lon"]], [tx["lat"]])
        c, l = inv * (xs[0], ys[0])
        if not (0 <= c < w and 0 <= l < h):
            continue
        em = destaque is None or tx["nome"] == destaque
        lado = max(7, round(max(w, h) / (140 if em else 220)))
        dr.polygon([(c, l - lado), (c - lado, l + lado * 0.8), (c + lado, l + lado * 0.8)],
                   fill=(241, 196, 15, 255) if em else (150, 150, 150, 255),
                   outline=(255, 255, 255, 255) if tx.get("proposta") else (20, 20, 20, 255),
                   width=2)
        if em:
            dr.text((c + lado + 3, l - lado),
                    tx["nome"] + (" (proposta)" if tx.get("proposta") else ""),
                    fill=(26, 26, 26, 255), font=fonte, stroke_width=2,
                    stroke_fill=(255, 255, 255, 255))
    return np.array(im)


def legenda_continua_png(largura=900, altura=46):
    """A barra da escala contínua, para o slide."""
    import numpy as np, io as _io
    from PIL import Image
    v = np.linspace(-100.0, -60.0, largura)
    cor = cor_continua(v)
    im = np.repeat(cor[None, :, :], altura, axis=0).astype("uint8")
    req = float(rm.ESCALAS["sinal_cob"]["req"])
    x = int(round((req + 100.0) / 40.0 * (largura - 1)))
    im[:, max(0, x - 2):x + 2] = 255
    buf = _io.BytesIO(); Image.fromarray(im, "RGB").save(buf, "PNG")
    return buf.getvalue()


def _escala_calor(s, x, y, w):
    from pptx.util import Inches
    import io as _io
    s.shapes.add_picture(_io.BytesIO(legenda_continua_png()), Inches(x), Inches(y + 0.25),
                         Inches(w), Inches(0.26))
    rm._txt_anglo(s, x, y - 0.05, w, 0.3, "RSSI estimado (dBm) — escala contínua",
                  9, True, rm.ANGLO["texto"])
    for v in (-100, -90, -80, -75, -70, -60):
        px = x + (v + 100.0) / 40.0 * w
        rm._txt_anglo(s, px - 0.3, y + 0.52, 0.6, 0.25, f"{v}", 8.5, v == -75,
                      rm.ANGLO["azul"] if v == -75 else rm.ANGLO["suave"])
    rm._txt_anglo(s, x, y + 0.78, w, 0.3, "linha branca no mapa: o requisito, −75 dBm",
                  8.5, False, rm.ANGLO["suave"])


# ══════════════════════════════════════════════════════════════
# O RELATÓRIO
# ══════════════════════════════════════════════════════════════
def gerar(amostras, peers, areas, destino, cfg=None, malha=None, nome=None,
          aviso=None, infra=None, quando=None):
    """Cobertura estimada das áreas com LAS: o deck próprio e os GeoTIFF
    em `destino`/estimativa. Cada área considera as ERB/ERM que estão
    DENTRO dela: um mapa de cada uma e o do conjunto delas. Devolve os
    arquivos gravados (vazio sem LAS ou sem ERB/ERM com posição)."""
    import numpy as np
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
    polares, pares, usados = calcular(mos, txs, bandas, prm, medidas, aviso)
    cal = calibrar(pares, bandas, prm["min_calibracao"], prm["max_correcao_db"])
    for b in bandas:
        aviso(f"    {b}: {_txt_calibracao(cal[b], prm)}")
    pasta = Path(destino) / "estimativa"
    pasta.mkdir(parents=True, exist_ok=True)
    feitos = []
    suf = {b: b.replace(" ", "").replace(".", "") for b in bandas}
    # Em dBm, para o QGIS/ArcGIS: cada ERB/ERM e o conjunto de todas.
    (pasta / "ERB-ERM").mkdir(exist_ok=True)
    for b in bandas:
        conj = None
        for n_tx, info in polares.items():
            g = grade_de(mos, info, b, cal[b]["correcao"])
            alvo = pasta / "ERB-ERM" / f"{rm._slug_arquivo(n_tx)[:50]}_{suf[b]}.tif"
            _gravar_grade(alvo, g, mos, f"RSSI estimado de {n_tx} (dBm)")
            feitos.append(alvo)
            conj = g if conj is None else np.where(np.isfinite(conj), np.fmax(conj, g), g)
        if conj is not None:
            alvo = pasta / f"cobertura_estimada_{suf[b]}.tif"
            _gravar_grade(alvo, conj, mos, "RSSI estimado (dBm), conjunto das ERB/ERM")
            feitos.append(alvo)
    req = float(rm.ESCALAS["sinal_cob"]["req"])
    secoes = []
    for area in areas:
        dentro_tx = [t for t in usados if rm._na_area(area, t["lat"], t["lon"])]
        sec = {"area": area, "txs": dentro_tx, "paginas": []}
        secoes.append(sec)
        if not dentro_tx:
            aviso(f"  · {area['nome']}: nenhuma ERB/ERM dentro da área")
            continue
        try:
            fundo = rm.fundo_da_area(area) if area.get("imagem") else None
        except Exception as e:
            aviso(f"  ! imagem de {area['nome']}: {e}")
            fundo = None
        fundo = fundo or fundo_do_terreno(area, mos)
        if fundo is None:
            continue
        grade = GradeDaArea(area, fundo, mos)
        am_ar = [a for a in amostras or [] if a.get("area") == area["nome"]]
        pasta_a = pasta / rm._slug_arquivo(area["nome"])[:50]
        pasta_a.mkdir(exist_ok=True)
        for b in bandas:
            vals = {t["nome"]: grade.valores(polares[t["nome"]], b, cal[b]["correcao"])
                    for t in dentro_tx if t["nome"] in polares}
            vals = {k: v for k, v in vals.items() if v is not None}
            if not vals:
                continue
            conj = np.fmax.reduce([v for v in vals.values()]) if len(vals) > 1 \
                else next(iter(vals.values()))
            am_b = [a for a in am_ar if rm._norm_banda(a.get("banda")) == b]
            medido = (rm.survey_resumo(am_b) or {}).get("sinal_cob") if am_b else None

            def _pct(v):
                vv = v[grade.dentro & np.isfinite(v)]
                return (round(float((vv > req).mean() * 100), 1) if vv.size else None,
                        round(float(np.median(vv)), 1) if vv.size else None)
            pc, md = _pct(conj)
            mapa = mapa_de_calor(grade, conj, usados, None, req)
            sec["paginas"].append({
                "tipo": "conjunto", "banda": b, "png": rm.png_do_mapa(mapa, fundo),
                "imagem": fundo["arquivo"], "pct_acima": pc, "mediana": md,
                "medido_pct": medido["pct_ok"] if medido else None,
                "n_tx": len(vals)})
            _gravar_mapa(pasta_a / f"conjunto_{suf[b]}.tif", mapa, fundo, feitos, aviso)
            for t in dentro_tx:
                v = vals.get(t["nome"])
                if v is None:
                    continue
                pc_t, md_t = _pct(v)
                med_t = [m[4] for m in medidas if m[0].casefold() == t["nome"].casefold()
                         and m[1] == b and rm._na_area(area, m[5], m[6])]
                mapa_t = mapa_de_calor(grade, v, usados, t["nome"], req)
                sec["paginas"].append({
                    "tipo": "erb", "tx": t, "banda": b,
                    "png": rm.png_do_mapa(mapa_t, fundo), "imagem": fundo["arquivo"],
                    "pct_acima": pc_t, "mediana": md_t,
                    "medido_mediana": (round(float(np.median(med_t)), 1) if med_t else None),
                    "medido_n": len(med_t),
                    "erro": cal["por_tx"].get((t["nome"], b))})
                _gravar_mapa(pasta_a / f"{rm._slug_arquivo(t['nome'])[:50]}_{suf[b]}.tif",
                             mapa_t, fundo, feitos, aviso)
    try:
        dados, nome_ppt = ppt_estimativa(nome or "Site Survey", secoes, bandas,
                                         usados, cal, prm, mos, quando)
        alvo = Path(destino) / nome_ppt
        alvo.write_bytes(dados); feitos.append(alvo)
    except Exception as e:
        aviso(f"  ! deck da cobertura estimada não gerado: {e}")
    return feitos


def _gravar_mapa(alvo, mapa, fundo, feitos, aviso):
    try:
        rm.gravar_geotiff_do_mapa(alvo, mapa, fundo)
        feitos.append(alvo)
    except Exception as e:
        aviso(f"  ! GeoTIFF {alvo.name}: {e}")


def _gravar_grade(caminho, grade, mos, descricao="RSSI estimado (dBm)"):
    """A estimativa em dBm, float, para o QGIS/ArcGIS estilizar."""
    rio = rm._rasterio()
    import numpy as np
    with rio.open(caminho, "w", driver="GTiff", width=grade.shape[1],
                  height=grade.shape[0], count=1, dtype="float32", crs=mos["crs"],
                  transform=mos["transform"], nodata=np.nan, compress="deflate",
                  tiled=True) as d:
        d.write(grade.astype("float32"), 1)
        d.set_band_description(1, descricao)


def ppt_estimativa(nome_sv, secoes, bandas, usados, cal, prm, mos, quando=None):
    """O deck da cobertura estimada, na identidade Anglo: premissas, as
    ERB/ERM, e uma seção por área — a abertura com as ERB/ERM que estão
    nela, o mapa do conjunto e um mapa de cada uma, por banda."""
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
    sem = [sec["area"]["nome"] for sec in secoes if not sec["txs"]]
    ficha = [
        ["Terreno", f"{', '.join(mos['arquivos'])} · célula de "
                    f"{abs(mos['transform'].a):g} m (ponto mais alto da célula)"],
        ["Modelo", "espaço livre + difração nas duas cristas que mais barram o "
                   "caminho (Deygout, ITU-R P.526), curvatura da Terra k = 4/3"],
        ["Antena no veículo", f"{prm['altura_rx_m']:g} m acima do terreno, "
                              f"{prm['ganho_rx_dbi']:g} dBi"],
        ["ERB/ERM", f"{len(usados)} no cálculo"
                    + (f" · {n_pad} com altura padrão ({prm['altura_tx_m']:g} m)"
                       if n_pad else "")],
        ["Por área", "cada área considera as ERB/ERM que estão dentro dela: um "
                     "mapa de cada uma e o do conjunto (em cada ponto, o sinal "
                     "mais forte entre elas)"
                     + (f" · sem ERB/ERM dentro: {', '.join(sem)}" if sem else "")],
        ["Raio de cálculo", f"{prm['raio_m']:g} m a partir de cada ERB/ERM"],
    ]
    for b in bandas:
        ficha.append([f"Calibração {b}", _txt_calibracao(cal.get(b) or {}, prm)])
    ficha.append(["O que o modelo não vê", "veículos e equipamentos (não estão no "
                  "LAS), reflexão nas paredes da cava, mudança da cava depois do LAS"])
    rm._tabela_anglo(s, 0.55, 1.5, 12.25, ["Premissa", "Valor"], ficha, tam=10)

    linhas = []
    for t in usados:
        pot = " / ".join(f"{b.split()[0]}: {v:g}" for b, v in sorted((t.get("potencia") or {}).items()))
        err = " · ".join(f"{b.split()[0]}: {cal['por_tx'][(t['nome'], b)]['rms']:g} dB "
                         f"({cal['por_tx'][(t['nome'], b)]['n']})"
                         for b in bandas if (t["nome"], b) in cal.get("por_tx", {}))
        area_t = next((sec["area"]["nome"] for sec in secoes if t in sec["txs"]), "—")
        linhas.append([t["nome"] + (" (proposta)" if t.get("proposta") else ""), area_t,
                       f"{t['altura_usada']:g} m" + (" (padrão)" if t.get("altura_padrao") else ""),
                       pot or f"{prm['potencia_dbm']:g} (padrão)",
                       f"{t['azimute']:g}° / {t['abertura']:g}°" if t.get("azimute") is not None
                       and t.get("abertura") else "omni",
                       err or "—"])
    for k in range(0, max(1, len(linhas)), 14):
        s = rm.slide_anglo(p, "ERB/ERM no cálculo" + (f" ({k // 14 + 1})" if len(linhas) > 14 else ""),
                           "Área, altura do mastro, potência por banda (dBm), setor e o "
                           "erro do modelo em cada uma (leituras)")
        rm._tabela_anglo(s, 0.55, 1.5, 12.25,
                         ["ERB/ERM", "Área", "Altura", "Potência (dBm)", "Setor",
                          "Erro do modelo"], linhas[k:k + 14], tam=9.5)

    navegar = {}
    for sec in secoes:
        nome_a = sec["area"]["nome"]
        cab = rm.slide_anglo(p, nome_a,
                             f"Cobertura estimada — {len(sec['txs'])} ERB/ERM dentro da área"
                             if sec["txs"] else "Cobertura estimada — nenhuma ERB/ERM "
                             "dentro da área")
        if not sec["txs"]:
            rm._txt_anglo(cab, 0.55, 1.6, 12.25, 0.8,
                          "A estimativa por área considera as ERB/ERM que estão dentro "
                          "dela. Para incluir uma que ainda não existe, ponha-a na "
                          "planilha infraestrutura.csv com proposta = sim.",
                          11, False, rm.ANGLO["suave"])
            continue
        lin = []
        for t in sec["txs"]:
            cel = [t["nome"] + (" (proposta)" if t.get("proposta") else ""),
                   f"{t['altura_usada']:g} m"]
            for b in bandas:
                pg = next((x for x in sec["paginas"] if x["tipo"] == "erb"
                           and x["tx"]["nome"] == t["nome"] and x["banda"] == b), None)
                cel.append(f"{pg['pct_acima']:.0f}%" if pg and pg["pct_acima"] is not None
                           else "—")
            lin.append(cel)
        rm._tabela_anglo(cab, 0.55, 1.45, 7.6,
                         ["ERB/ERM", "Mastro"] + [f"Área > {req:g} dBm — {b}" for b in bandas],
                         lin[:13], tam=9.5)
        paginas = []
        for pg in sorted(sec["paginas"], key=lambda x: (x["banda"], x["tipo"] != "conjunto",
                                                        (x.get("tx") or {}).get("nome", ""))):
            b = pg["banda"]
            if pg["tipo"] == "conjunto":
                tit = f"{nome_a} · Conjunto das ERB/ERM — {b}"
                sub = (f"ESTIMATIVA — {pg['n_tx']} ERB/ERM da área; em cada ponto, o "
                       f"sinal mais forte entre elas")
                cart = [(f"Área acima de {req:g} dBm", _pct_txt(pg["pct_acima"])),
                        ("Mediana estimada", _dbm_txt(pg["mediana"])),
                        ("Medido nas rotas — dentro", _pct_txt(pg.get("medido_pct"))),
                        ("Erro do modelo (RMS)", _db_txt((cal.get(b) or {}).get("rms")))]
            else:
                t = pg["tx"]
                tit = f"{nome_a} · {t['nome']} — {b}"
                sub = (f"ESTIMATIVA — só {t['nome']}, mastro de {t['altura_usada']:g} m"
                       + (" (padrão)" if t.get("altura_padrao") else ""))
                e = pg.get("erro") or {}
                cart = [(f"Área acima de {req:g} dBm", _pct_txt(pg["pct_acima"])),
                        ("Mediana estimada", _dbm_txt(pg["mediana"])),
                        (f"Medido dela na área ({pg['medido_n']})",
                         _dbm_txt(pg.get("medido_mediana"))),
                        ("Erro do modelo nela", _db_txt(e.get("rms")))]
            s = rm.slide_anglo(p, tit, sub)
            rm._mapa_anglo(s, 0.55, 1.45, 7.3, 5.4, pg["png"],
                           f"Imagem: {pg['imagem']}   ·   relevo do LAS   ·   {b}")
            for k, (rot, val) in enumerate(cart):
                rm._cartao_anglo(s, 8.05 + (k % 2) * 2.45, 1.45 + (k // 2) * 0.9,
                                 2.3, 0.8, rot, val)
            rm._txt_anglo(s, 8.05, 3.4, 4.75, 2.0,
                          "Estimado ≠ medido. A cor diz o que o relevo permite com a "
                          "altura e a potência informadas; o survey diz o que a rede "
                          "entregou nas rotas. Onde os dois divergem, vale o medido — "
                          "e a divergência aponta altura, potência ou obstáculo a "
                          "conferir.", 9.5, False, rm.ANGLO["suave"])
            _escala_calor(s, 8.05, 5.7, 4.75)
            paginas.append(s)
        rm._indice_da_area(cab, paginas, nome_a)
        for sl in paginas:
            navegar[sl.slide_id] = cab
    rm._navegacao_anglo(p, secoes=navegar)
    buf = _io.BytesIO(); p.save(buf)
    seguro = re.sub(r"[^\w\-]+", "_", nome_sv)[:40]
    return buf.getvalue(), f"Cobertura_Estimada_{seguro}_{quando:%Y%m%d}.pptx"


def _txt_calibracao(c, prm):
    if c.get("calibrado"):
        return (f"correção {c['correcao']:+g} dB · erro (RMS) {c['rms']:g} dB em "
                f"{c['n']} leituras do survey")
    if c.get("rejeitada") is not None:
        return (f"correção de {c['rejeitada']:+g} dB REJEITADA (acima de "
                f"{prm['max_correcao_db']:g} dB): estimado e medido não batem — "
                f"confira posição, altura e potência das ERB/ERM · {c['n']} leituras")
    return (f"sem calibração: {c.get('n', 0)} leituras do survey "
            f"(mínimo {prm['min_calibracao']})")


def _pct_txt(v):
    return f"{v:.1f}%" if v is not None else "—"


def _dbm_txt(v):
    return f"{v:g} dBm" if v is not None else "—"


def _db_txt(v):
    return f"{v:g} dB" if v is not None else "—"
