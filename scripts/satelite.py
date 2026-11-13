"""Superficie de agua de los embalses a partir de imágenes Sentinel-2 (Copernicus/ESA).

Usa el catálogo público Earth Search (AWS, sin clave) y lee solo el recorte
de cada embalse. El agua se detecta con el índice MNDWI (verde vs. infrarrojo
de onda corta), método estándar en teledetección.
"""
import io, os, json, datetime as dt
import numpy as np
import requests
import rasterio
from rasterio.windows import from_bounds, Window
from rasterio.warp import transform_bounds
from rasterio.enums import Resampling
from scipy import ndimage

STAC = "https://earth-search.aws.element84.com/v1/search"
GDAL_ENV = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", AWS_NO_SIGN_REQUEST="YES",
                CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif", GDAL_HTTP_MAX_RETRY="3",
                GDAL_HTTP_RETRY_DELAY="2", VSI_CACHE="TRUE")
NUBES = (8, 9, 10)          # clases SCL: nubes medias, altas y cirrus
if os.environ.get("SSL_CERT_FILE"):       # redes con certificado propio (proxy)
    GDAL_ENV["CURL_CA_BUNDLE"] = os.environ["SSL_CERT_FILE"]
MIN_PIXELES = 40            # descarta manchas menores a 1,6 ha (píxeles de 20 m)


def buscar_escenas(bbox, desde, hasta, max_nubes=60):
    """Lista de escenas Sentinel-2 L2A que cubren el recuadro, de más vieja a más nueva."""
    cuerpo = {"collections": ["sentinel-2-l2a"], "bbox": bbox, "limit": 100,
              "datetime": f"{desde}T00:00:00Z/{hasta}T23:59:59Z",
              "query": {"eo:cloud_cover": {"lt": max_nubes}}}
    items, url = [], STAC
    while url:
        r = requests.post(url, json=cuerpo, timeout=60)
        r.raise_for_status()
        d = r.json()
        items += d["features"]
        sig = [l for l in d.get("links", []) if l["rel"] == "next"]
        if sig:
            url, cuerpo = sig[0]["href"], sig[0].get("body", cuerpo)
        else:
            url = None
    items.sort(key=lambda f: f["properties"]["datetime"])
    return items


_FS = None


def _abrir(href):
    """Abre un GeoTIFF remoto leyendo solo los bloques necesarios (peticiones HTTP parciales)."""
    global _FS
    if _FS is None:
        import fsspec
        _FS = fsspec.filesystem("http", client_kwargs={"trust_env": True},
                                block_size=512 * 1024, cache_type="blockcache")
    return rasterio.open(href, opener=_FS.open)


def _ventana20(src20, bbox):
    """Ventana del recuadro en la grilla de 20 m de la escena: (col, fila, ancho, alto)."""
    l, b, r, t = transform_bounds("EPSG:4326", src20.crs, *bbox)
    w = from_bounds(l, b, r, t, src20.transform).round_offsets().round_lengths()
    return int(w.col_off), int(w.row_off), int(w.width), int(w.height)


def _leer(href, ventana, resampling=Resampling.average):
    """Lee una banda sobre la ventana (en grilla de 20 m). Fuera de la imagen devuelve ceros."""
    col, fila, ancho, alto = ventana
    out = np.zeros((alto, ancho), dtype="uint16")
    with _abrir(href) as src:
        k = round(20 / abs(src.transform.a))          # 1 para bandas de 20 m, 2 para las de 10 m
        W, H = src.width // k, src.height // k
        c0, f0, c1, f1 = max(col, 0), max(fila, 0), min(col + ancho, W), min(fila + alto, H)
        if c1 <= c0 or f1 <= f0:
            return out
        win = Window(c0 * k, f0 * k, (c1 - c0) * k, (f1 - f0) * k)
        out[f0 - fila:f1 - fila, c0 - col:c1 - col] = src.read(
            1, window=win, out_shape=(f1 - f0, c1 - c0), resampling=resampling)
    return out


def _bandas(item, bbox, claves):
    a = item["assets"]
    with _abrir(a["swir16"]["href"]) as s:
        ventana, crs = _ventana20(s, bbox), str(s.crs)
    # lectura secuencial: el lector remoto no es seguro con varios hilos a la vez
    return crs, {k: _leer(a[k]["href"], ventana,
                          Resampling.nearest if k == "scl" else Resampling.average) for k in claves}


def _offset(item, bandas):
    """Offset radiométrico a restar (0 o 1000).

    Desde 2022 ESA suma 1000 a los valores; el catálogo a veces ya lo descuenta y su
    indicador no es confiable, así que se verifica con los datos: si los píxeles más
    oscuros están muy por debajo de 1000, el offset ya fue descontado.
    """
    try:
        if float(str(item["properties"].get("s2:processing_baseline", "0"))) < 4.0:
            return 0.0
    except ValueError:
        return 0.0
    oscuro = min(float(np.percentile(b[b > 0], 1)) for b in bandas if (b > 0).any())
    return 1000.0 if oscuro >= 900 else 0.0


def _reflectancia(dn, off):
    return np.clip((dn.astype("float32") - off) / 10000.0, 0, None)


def analizar(items, bbox, con_imagen=False):
    """Superficie de agua (km²) de un recuadro a partir de las escenas de una misma fecha.

    `items` son las escenas del mismo día (el recuadro puede caer entre dos "tiles"
    vecinos: se unen). Devuelve None si la cobertura es incompleta o hay nubes.
    Se trabaja a 20 m por píxel.
    """
    if isinstance(items, dict):
        items = [items]
    claves = ["swir16", "scl", "green", "nir"] + (["red", "blue"] if con_imagen else [])
    with rasterio.Env(**GDAL_ENV):
        item, crs0, b = items[0], None, None
        for it in items:
            crs, nuevo = _bandas(it, bbox, claves)
            if b is None:
                crs0, b = crs, nuevo
            elif crs == crs0 and nuevo["swir16"].shape == b["swir16"].shape:
                falta = b["swir16"] == 0
                for k in claves:
                    b[k][falta] = nuevo[k][falta]
            if b["swir16"].size and (b["swir16"] > 0).mean() >= 0.995:
                break
        if b is None or b["swir16"].size == 0 or (b["swir16"] > 0).mean() < 0.995:
            return None                      # el satélite no cubrió todo el recuadro ese día
        scl, swir_dn, verde_dn, nir_dn = b["scl"], b["swir16"], b["green"], b["nir"]
        extra = [b[k] for k in ("red", "blue")] if con_imagen else []
        nubes = float(np.isin(scl, NUBES).mean())
        if nubes > 0.03:
            return None
        off = _offset(item, (verde_dn, swir_dn, nir_dn))
        verde, swir, nir = (_reflectancia(x, off) for x in (verde_dn, swir_dn, nir_dn))
        mndwi = (verde - swir) / (verde + swir + 1e-6)
        claro = (mndwi > 0.15) & (nir < 0.20) & (verde > 0.006) & ((verde - swir) > 0.006) & (scl != 11)
        # agua muy oscura (invierno, sol bajo): casi negra en todas las bandas. Solo se
        # acepta si está unida a agua detectada por el índice, para no confundir sombras.
        oscuro = (swir < 0.012) & (nir < 0.025) & (verde < 0.03) & (swir_dn > 0)
        etiquetas, n = ndimage.label(claro | oscuro)
        agua = np.zeros_like(claro)
        if n:
            tam = ndimage.sum(claro, etiquetas, range(1, n + 1))
            agua = np.isin(etiquetas, [i + 1 for i, t in enumerate(tam) if t >= MIN_PIXELES])
            agua = ndimage.binary_fill_holes(agua)
        res = {"fecha": item["properties"]["datetime"][:10],
               "km2": round(float(agua.sum()) * 400 / 1e6, 3),
               "nubes": round(nubes, 3), "escena": item["id"]}
        if con_imagen:
            rojo, azul = (_reflectancia(x, off) for x in extra)
            res["_rgb"], res["_agua"] = np.dstack([rojo, verde, azul]), agua
    return res


def guardar_imagen(res, ruta, ancho=640):
    """Imagen color real con el borde del agua detectada marcado en celeste."""
    from PIL import Image
    rgb = np.clip(res["_rgb"] / 0.30, 0, 1) ** (1 / 1.6)
    img = (rgb * 255).astype("uint8")
    borde = res["_agua"] ^ ndimage.binary_erosion(res["_agua"], iterations=1)
    img[borde] = (0, 229, 255)
    im = Image.fromarray(img)
    im = im.resize((ancho, max(1, round(ancho * im.height / im.width))), Image.LANCZOS)
    im.save(ruta, quality=82)


def _analizar_seguro(args):
    grupo, bbox = args
    err = None
    for intento in range(2):
        try:
            return analizar(grupo, bbox)
        except Exception as e:          # una escena dañada no debe frenar el resto
            err = e
    print("  aviso: no se pudo leer", grupo[0]["id"], type(err).__name__, flush=True)
    return None


def agrupar_por_fecha(items):
    g = {}
    for it in items:
        g.setdefault(it["properties"]["datetime"][:10], []).append(it)
    return g


def actualizar_serie(serie, bbox, desde, hasta, espaciado_dias=0, procesos=1, omitir=()):
    """Agrega a `serie` (lista de {fecha, km2}) las observaciones nuevas del período."""
    hechas = {p["fecha"] for p in serie}
    grupos, ultima = [], None
    for f, grupo in sorted(agrupar_por_fecha(buscar_escenas(bbox, desde, hasta)).items()):
        if f in hechas or f in omitir:
            continue
        d = dt.date.fromisoformat(f)
        if espaciado_dias and ultima and (d - ultima).days < espaciado_dias:
            continue
        grupos.append(grupo)
        ultima = d
    if procesos > 1 and len(grupos) > 1:
        from multiprocessing import Pool
        with Pool(procesos, maxtasksperchild=20) as p:
            pend = [p.apply_async(_analizar_seguro, ((g, bbox),)) for g in grupos]
            res = []
            for h in pend:
                try:
                    res.append(h.get(timeout=240))
                except Exception:           # proceso caído o colgado: se omite esa fecha
                    res.append(None)
    else:
        res = [_analizar_seguro((g, bbox)) for g in grupos]
    por_fecha = {p["fecha"]: p for p in serie}
    for r in res:
        if r:
            por_fecha[r["fecha"]] = {"fecha": r["fecha"], "km2": r["km2"], "nubes": r["nubes"]}
    fechas = [g[0]["properties"]["datetime"][:10] for g in grupos]
    return sorted(por_fecha.values(), key=lambda p: p["fecha"]), fechas
