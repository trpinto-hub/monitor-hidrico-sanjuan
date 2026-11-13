"""Monitor Hídrico San Juan — actualización automática de datos.

Lo ejecuta GitHub Actions cada pocas horas. Reúne:
  1. Superficie de agua de los embalses (satélite Sentinel-2, Copernicus/ESA).
  2. Caudal estimado y pronosticado de los ríos (modelo GloFAS de Copernicus, vía Open-Meteo).
  3. Lluvia pronosticada por localidad (Open-Meteo).
  4. Alertas oficiales vigentes del Servicio Meteorológico Nacional (SMN) para San Juan.
Con eso arma la lista de alertas, escribe docs/data/datos.json (+ datos.js) y,
si hay alertas nuevas y Telegram está configurado, las envía al canal.
"""
import os, re, sys, json, math, hashlib, pathlib, datetime as dt
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
import requests

sys.path.insert(0, str(pathlib.Path(__file__).parent))

RAIZ = pathlib.Path(__file__).resolve().parent.parent
DATA = RAIZ / "docs" / "data"
CFG = json.loads((RAIZ / "scripts" / "config.json").read_text(encoding="utf-8"))
UMB = CFG["umbrales"]
TZ = dt.timezone(dt.timedelta(hours=-3))          # hora oficial argentina
AHORA = dt.datetime.now(TZ)
HOY = AHORA.date()
INVIERNO = (5, 6, 7, 8)                            # meses de sol bajo: sombras largas en los cañones
NIVELES = {"amarillo": 1, "naranja": 2, "rojo": 3}
UA = {"User-Agent": "monitor-hidrico-sanjuan/1.0 (+github)"}
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def pedir(url, intentos=4, **kw):
    """GET con reintentos (las fuentes públicas a veces cortan la conexión)."""
    import time
    kw.setdefault("timeout", 60)
    kw.setdefault("headers", UA)
    for i in range(intentos):
        try:
            r = requests.get(url, **kw)
            if r.status_code >= 500 or r.status_code == 429:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r
        except Exception:
            if i == intentos - 1:
                raise
            time.sleep(3 * (i + 1))


def leer(nombre, defecto):
    p = DATA / nombre
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else defecto


def escribir(nombre, obj, compacto=True):
    DATA.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(obj, ensure_ascii=False, separators=(",", ":") if compacto else None,
                     indent=None if compacto else 1)
    (DATA / nombre).write_text(txt, encoding="utf-8")
    return txt


def fecha_larga(iso):
    d = dt.date.fromisoformat(iso[:10])
    return f"{d.day} {MESES[d.month - 1]} {d.year}"


def num(x, dec=1):
    """Número con coma decimal (formato argentino)."""
    return f"{x:,.{dec}f}".replace(",", "§").replace(".", ",").replace("§", ".")


# ───────────────────────────── 1. EMBALSES (satélite) ─────────────────────────────

def limpiar_serie(serie):
    """Quita lecturas anómalas (sombras de invierno, nubes o bruma no detectadas).

    Una lectura se descarta si difiere más de 25 % tanto de la mediana de las 3
    anteriores como de la mediana de las 3 siguientes. Un cambio real de nivel
    coincide con al menos uno de los dos lados y se conserva. La lectura más
    reciente, si es anómala, queda en espera hasta que la confirme la pasada siguiente.
    """
    def mediana(v):
        v = sorted(v)
        return (v[len(v) // 2] + v[(len(v) - 1) // 2]) / 2 if v else None

    def lejos(x, ref):
        return ref is not None and abs(x - ref) > max(0.25 * ref, 0.03)

    k = [p["km2"] for p in serie]
    out = []
    for i, p in enumerate(serie):
        antes, despues = mediana(k[max(0, i - 3):i]), mediana(k[i + 1:i + 4])
        if antes is None and despues is None:
            out.append(p)
        elif antes is None:
            if not lejos(p["km2"], despues):
                out.append(p)
        elif despues is None:
            if not lejos(p["km2"], antes):
                out.append(p)
        elif not (lejos(p["km2"], antes) and lejos(p["km2"], despues)):
            out.append(p)
    return out


def valor_en(serie, fecha, tolerancia=25):
    """Observación más cercana a `fecha` (dentro de la tolerancia en días)."""
    mejor = None
    for p in serie:
        dd = abs((dt.date.fromisoformat(p["fecha"]) - fecha).days)
        if dd <= tolerancia and (mejor is None or dd < mejor[0]):
            mejor = (dd, p)
    return mejor[1] if mejor else None


def embalses():
    import satelite
    crudo = leer("embalses.json", {})
    meta = leer("embalses_meta.json", {})
    salida, alertas, ok = [], [], True
    for e in CFG["embalses"]:
        serie = crudo.get(e["id"], [])
        desde = (HOY - dt.timedelta(days=30)).isoformat() if serie else "2017-01-01"
        m = meta.setdefault(e["id"], {})
        # fechas ya revisadas sin resultado (nubes, cobertura parcial): no se vuelven a bajar,
        # salvo las de los últimos 2 días, que el catálogo puede completar más tarde
        limite = (HOY - dt.timedelta(days=2)).isoformat()
        vistas = {f for f in m.get("revisadas", []) if f >= desde}
        try:
            serie, fechas = satelite.actualizar_serie(serie, e["bbox"], desde, HOY.isoformat(),
                                                      espaciado_dias=0 if serie else 12,
                                                      omitir={f for f in vistas if f < limite})
            crudo[e["id"]] = serie
            m["revisadas"] = sorted(vistas | set(fechas))
            print(f"embalse {e['id']}: {len(fechas)} fechas nuevas revisadas, {len(serie)} observaciones")
        except Exception as ex:
            ok = False
            print(f"embalse {e['id']}: ERROR satélite {type(ex).__name__}: {ex}")
        limpia = limpiar_serie(serie)
        if not limpia:
            continue
        ult = limpia[-1]
        f_ult = dt.date.fromisoformat(ult["fecha"])
        # el máximo se toma fuera del invierno: con sol bajo las sombras pueden agrandar la medición
        sin_invierno = [p for p in limpia if int(p["fecha"][5:7]) not in INVIERNO] or limpia
        maximo = max(sin_invierno, key=lambda p: p["km2"])
        pct = min(100.0, round(100 * ult["km2"] / maximo["km2"], 1)) if maximo["km2"] else None
        ant = limpia[-2] if len(limpia) > 1 else None
        hace30 = valor_en(limpia[:-1], f_ult - dt.timedelta(days=30), 12)
        hace1a = valor_en(limpia[:-1], f_ult - dt.timedelta(days=365), 25)
        var = lambda p: round(100 * (ult["km2"] - p["km2"]) / p["km2"], 1) if p and p["km2"] else None
        v30 = var(hace30)
        tendencia = "estable" if v30 is None or abs(v30) < 2 else ("creciendo" if v30 > 0 else "bajando")

        # imagen satelital de la última observación
        if m.get("imagen_fecha") != ult["fecha"]:
            try:
                grupo = satelite.buscar_escenas(e["bbox"], ult["fecha"], ult["fecha"])
                r = satelite.analizar(grupo, e["bbox"], con_imagen=True) if grupo else None
                if r:
                    (DATA / "img").mkdir(parents=True, exist_ok=True)
                    satelite.guardar_imagen(r, DATA / "img" / f"{e['id']}.jpg")
                    m["imagen_fecha"] = ult["fecha"]
            except Exception as ex:
                print(f"embalse {e['id']}: no se pudo generar la imagen ({ex})")

        salida.append({**{k: e[k] for k in ("id", "nombre", "rio", "depto", "lat", "lon")},
                       "serie": [[p["fecha"], p["km2"]] for p in limpia],
                       "ultimo": {"fecha": ult["fecha"], "km2": ult["km2"]},
                       "maximo": {"fecha": maximo["fecha"], "km2": maximo["km2"]},
                       "pct_maximo": pct, "var_30d": v30, "var_anual": var(hace1a),
                       "var_anterior": var(ant), "anterior_fecha": ant["fecha"] if ant else None,
                       "tendencia": tendencia, "imagen_fecha": m.get("imagen_fecha")})

        # alertas propias del embalse (solo con observaciones recientes y sin nubes)
        reciente = ((HOY - f_ult).days <= 12 and ult.get("nubes", 0) < 0.005
                    and f_ult.month not in INVIERNO)
        if reciente and pct is not None and pct >= UMB["embalse_pct_maximo"] and len(limpia) > 30:
            alertas.append(alerta("Embalse", False, "amarillo",
                f"{e['nombre']}: espejo de agua cerca del máximo observado",
                f"El {fecha_larga(ult['fecha'])} el satélite midió {num(ult['km2'], 2)} km², "
                f"{num(pct, 0)} % del máximo registrado desde 2017. Puede haber erogaciones mayores "
                f"aguas abajo; consulte la información del operador del dique.",
                [e["depto"]], ult["fecha"], None, clave=f"embalse-{e['id']}-max-{ult['fecha'][:7]}"))
        base = valor_en(limpia[:-1], f_ult - dt.timedelta(days=UMB["embalse_crecimiento_dias"]), 7)
        if reciente and base and base["km2"] > 0.05:
            sube = 100 * (ult["km2"] - base["km2"]) / base["km2"]
            if sube >= UMB["embalse_crecimiento_pct"]:
                alertas.append(alerta("Embalse", False, "amarillo",
                    f"{e['nombre']}: crecimiento rápido del espejo de agua",
                    f"La superficie pasó de {num(base['km2'], 2)} km² ({fecha_larga(base['fecha'])}) a "
                    f"{num(ult['km2'], 2)} km² ({fecha_larga(ult['fecha'])}): +{num(sube, 0)} %.",
                    [e["depto"]], ult["fecha"], None, clave=f"embalse-{e['id']}-sube-{ult['fecha']}"))
    escribir("embalses.json", crudo)
    escribir("embalses_meta.json", meta)
    return salida, alertas, ok


# ───────────────────────────── 2. RÍOS (modelo GloFAS) ─────────────────────────────

FLOOD = "https://flood-api.open-meteo.com/v1/flood"


def gumbel(maximos, T):
    """Caudal de período de retorno T años (ajuste Gumbel por momentos)."""
    n = len(maximos)
    media = sum(maximos) / n
    desv = math.sqrt(sum((x - media) ** 2 for x in maximos) / (n - 1))
    beta = desv * math.sqrt(6) / math.pi
    mu = media - 0.5772 * beta
    return mu - beta * math.log(-math.log(1 - 1 / T))


def clima_rios():
    """Umbrales y valores normales de cada río, calculados una sola vez con la serie 1984→."""
    clima = leer("rios_clima.json", {})
    for r in CFG["rios"]:
        if r["id"] in clima:
            continue
        fin = dt.date(HOY.year - 1, 12, 31).isoformat()
        d = pedir(FLOOD, params={"latitude": r["lat"], "longitude": r["lon"],
                                 "daily": "river_discharge", "start_date": "1984-01-01",
                                 "end_date": fin}, timeout=120).json()["daily"]
        por_anio, por_mes = {}, {}
        for f, q in zip(d["time"], d["river_discharge"]):
            if q is None:
                continue
            por_anio[f[:4]] = max(por_anio.get(f[:4], 0), q)
            por_mes.setdefault(int(f[5:7]), []).append(q)
        mx = list(por_anio.values())
        t = UMB["rio_periodo_retorno_anios"]
        clima[r["id"]] = {"amarillo": round(gumbel(mx, t["amarillo"]), 1),
                          "naranja": round(gumbel(mx, t["naranja"]), 1),
                          "rojo": round(gumbel(mx, t["rojo"]), 1),
                          "normal_mes": [round(sum(por_mes[m]) / len(por_mes[m]), 1) for m in range(1, 13)],
                          "periodo": f"1984-{fin[:4]}"}
        print(f"río {r['id']}: umbrales calculados {clima[r['id']]['amarillo']}/"
              f"{clima[r['id']]['naranja']}/{clima[r['id']]['rojo']} m³/s")
    escribir("rios_clima.json", clima, compacto=False)
    return clima


def nivel_deshielo(iso0_max):
    u = UMB.get("deshielo_isoterma0_m", {"moderado": 3000, "alto": 4200})
    if iso0_max is None:
        return None
    return "alto" if iso0_max >= u["alto"] else "moderado" if iso0_max >= u["moderado"] else "bajo"


def deshielo_cuencas():
    """Presión de deshielo en las nacientes de los ríos nivales.

    Toma de Open-Meteo, en puntos de alta cordillera sobre las cuencas, la
    temperatura y la altura de la isoterma de 0 °C pronosticadas. Cuanto más alto
    sube la isoterma de 0 °C, mayor es la superficie del manto de nieve que queda
    por encima de ella y, por lo tanto, se derrite y aporta caudal. Es un indicador
    orientativo del empuje del deshielo, complementario al caudal del río.
    """
    coords = []
    for r in CFG["rios"]:
        if r.get("alta") and tuple(r["alta"]) not in coords:
            coords.append(tuple(r["alta"]))
    if not coords:
        return {}
    resp = pedir("https://api.open-meteo.com/v1/forecast", params={
        "latitude": ",".join(str(la) for la, lo in coords),
        "longitude": ",".join(str(lo) for la, lo in coords),
        "hourly": "temperature_2m,freezing_level_height",
        "forecast_days": 7, "timezone": "America/Argentina/San_Juan"}, timeout=60).json()
    if isinstance(resp, dict):
        resp = [resp]
    por_punto = {}
    for (la, lo), d in zip(coords, resp):
        h = d.get("hourly", {})
        diario = {}
        for t, T, iso in zip(h.get("time", []), h.get("temperature_2m", []), h.get("freezing_level_height", [])):
            e = diario.setdefault(t[:10], {"tmax": None, "iso": None})
            if T is not None:
                e["tmax"] = T if e["tmax"] is None else max(e["tmax"], T)
            if iso is not None:
                e["iso"] = iso if e["iso"] is None else max(e["iso"], iso)
        dias = [[f, diario[f]["iso"], diario[f]["tmax"]] for f in sorted(diario) if f >= HOY.isoformat()][:5]
        por_punto[(la, lo)] = {"elev": d.get("elevation"), "dias": dias}
    salida = {}
    for r in CFG["rios"]:
        if not r.get("alta"):
            continue
        p = por_punto.get(tuple(r["alta"]))
        isos = [d[1] for d in p["dias"] if d[1] is not None] if p else []
        if not isos:
            continue
        iso_max = max(isos)
        pico = max((d for d in p["dias"] if d[1] is not None), key=lambda d: d[1])
        t_max = max((d[2] for d in p["dias"] if d[2] is not None), default=None)
        salida[r["id"]] = {
            "nivel": nivel_deshielo(iso_max), "iso0_max": round(iso_max), "iso0_fecha": pico[0],
            "t_max": round(t_max, 1) if t_max is not None else None,
            "alta_elev": round(p["elev"]) if p["elev"] is not None else None,
            "dias": [[f, round(i) if i is not None else None, round(tt, 1) if tt is not None else None]
                     for f, i, tt in p["dias"]]}
        print(f"deshielo {r['id']}: isoterma0 máx {salida[r['id']]['iso0_max']} m → {salida[r['id']]['nivel']}")
    return salida


def rios():
    clima = clima_rios()
    rs = CFG["rios"]
    try:
        des = deshielo_cuencas()
    except Exception as ex:
        print(f"deshielo: ERROR {type(ex).__name__}: {ex}")
        des = {}
    resp = pedir(FLOOD, params={
        "latitude": ",".join(str(r["lat"]) for r in rs),
        "longitude": ",".join(str(r["lon"]) for r in rs),
        "daily": "river_discharge,river_discharge_max", "past_days": 92, "forecast_days": 16,
        "timezone": "America/Argentina/San_Juan"}, timeout=90).json()
    if isinstance(resp, dict):
        resp = [resp]
    salida, alertas = [], []
    for r, d in zip(rs, resp):
        c, dd = clima[r["id"]], d["daily"]
        filas = [(f, q, qx) for f, q, qx in zip(dd["time"], dd["river_discharge"], dd["river_discharge_max"])
                 if q is not None]
        hoy_iso = HOY.isoformat()
        pasado = [[f, round(q, 1)] for f, q, _ in filas if f <= hoy_iso]
        futuro = [[f, round(q, 1), round(qx if qx is not None else q, 1)] for f, q, qx in filas if f > hoy_iso]
        if not pasado:
            continue
        q_hoy = pasado[-1][1]
        q_7 = pasado[-8][1] if len(pasado) > 8 else None
        normal = c["normal_mes"][HOY.month - 1]
        prox = futuro[:7]
        pico = max(prox, key=lambda x: x[1]) if prox else [hoy_iso, q_hoy, q_hoy]
        ref = max(q_hoy, pico[1])
        nivel = next((n for n in ("rojo", "naranja", "amarillo") if ref >= c[n]), None)
        var7 = round(100 * (q_hoy - q_7) / q_7, 0) if q_7 else None
        tendencia = "estable" if var7 is None or abs(var7) < 10 else ("creciendo" if var7 > 0 else "bajando")
        salida.append({**r, "serie": pasado, "pronostico": futuro, "hoy": q_hoy,
                       "pico": {"fecha": pico[0], "q": pico[1]}, "normal": normal,
                       "pct_normal": round(100 * q_hoy / normal) if normal else None,
                       "var_7d": var7, "tendencia": tendencia, "nivel": nivel,
                       "umbrales": {n: c[n] for n in NIVELES}, "deshielo": des.get(r["id"])})
        if nivel:
            anios = UMB["rio_periodo_retorno_anios"][nivel]
            cuando = "hoy" if q_hoy >= pico[1] else f"hacia el {fecha_larga(pico[0])}"
            alertas.append(alerta("Río", False, nivel,
                f"{r['nombre']} — {r['lugar']}: crecida estimada",
                f"El modelo hidrológico GloFAS estima un caudal de {num(ref, 0)} m³/s {cuando}, por encima "
                f"del umbral de crecida de {anios} años ({num(c[nivel], 0)} m³/s). Caudal normal del mes: "
                f"{num(normal, 0)} m³/s. Es una estimación por modelo: no reemplaza las mediciones oficiales.",
                [r["lugar"]], hoy_iso, pico[0],
                clave=f"rio-{r['id']}-{nivel}-{HOY.isocalendar()[0]}s{HOY.isocalendar()[1]}"))
    return salida, alertas


# ───────────────────────────── 3. LLUVIA PRONOSTICADA ─────────────────────────────

def lluvia():
    ls = CFG["localidades"]
    resp = pedir("https://api.open-meteo.com/v1/forecast", params={
        "latitude": ",".join(str(l["lat"]) for l in ls), "longitude": ",".join(str(l["lon"]) for l in ls),
        "daily": "precipitation_sum", "forecast_days": 4, "timezone": "America/Argentina/San_Juan"},
        ).json()
    if isinstance(resp, dict):
        resp = [resp]
    salida, por_dia = [], {}
    u = UMB["lluvia_mm_24h"]
    for l, d in zip(ls, resp):
        dias = [[f, round(mm or 0, 1)] for f, mm in zip(d["daily"]["time"], d["daily"]["precipitation_sum"])]
        salida.append({"nombre": l["nombre"], "lat": l["lat"], "lon": l["lon"], "dias": dias})
        for f, mm in dias[:3]:
            nivel = next((n for n in ("rojo", "naranja", "amarillo") if mm >= u[n]), None)
            if nivel:
                por_dia.setdefault((f, nivel), []).append((l["nombre"], mm))
    alertas = []
    for f in sorted({k[0] for k in por_dia}):
        nivel = max((k[1] for k in por_dia if k[0] == f), key=NIVELES.get)
        lugares = sorted({x for k, v in por_dia.items() if k[0] == f for x in v}, key=lambda x: -x[1])
        alertas.append(alerta("Lluvia", False, nivel,
            f"Lluvia intensa pronosticada para el {fecha_larga(f)}",
            "Acumulados previstos en 24 h: " + "; ".join(f"{n} {num(mm, 0)} mm" for n, mm in lugares) +
            ". En zonas áridas estas lluvias pueden generar crecientes repentinas en ríos secos, "
            "quebradas y badenes. Pronóstico automático: siga los avisos oficiales del SMN.",
            [n for n, _ in lugares], f, f, clave=f"lluvia-{f}-{nivel}"))
    return salida, alertas


# ───────────────────────────── 4. ALERTAS OFICIALES SMN ─────────────────────────────

SMN_FEED = "https://ssl.smn.gob.ar/CAP/AR.php"
CAP = "{urn:oasis:names:tc:emergency:cap:1.2}"
_SJ = json.loads((RAIZ / "scripts" / "san_juan.json").read_text(encoding="utf-8"))
PROVINCIA = [tuple(p) for p in _SJ["contorno"]]          # contorno provincial (lat, lon)
DEPTOS = _SJ["departamentos"]                            # centroides de los 19 departamentos
SEVERIDAD = {"Moderate": "amarillo", "Severe": "naranja", "Extreme": "rojo"}


def dentro(lat, lon, poligono):
    """¿El punto está dentro del polígono? (trazado de rayos)"""
    d, n = False, len(poligono)
    for i in range(n):
        (y1, x1), (y2, x2) = poligono[i], poligono[(i + 1) % n]
        if (x1 > lon) != (x2 > lon) and lat < (y2 - y1) * (lon - x1) / (x2 - x1) + y1:
            d = not d
    return d


def _grilla():
    """Puntos cada ~10 km dentro de la provincia, cada uno con su departamento más cercano."""
    pts = []
    for i in range(int((32.7 - 28.3) / 0.1)):
        for j in range(int((70.6 - 66.6) / 0.1)):
            la, lo = -32.7 + i * 0.1, -70.6 + j * 0.1
            if dentro(la, lo, PROVINCIA):
                d = min(DEPTOS, key=lambda d: (d["lat"] - la) ** 2 + (d["lon"] - lo) ** 2)
                pts.append((la, lo, d["nombre"]))
    return pts


def _cap(url):
    try:
        return url, ET.fromstring(pedir(url, intentos=2, timeout=30).content)
    except Exception:
        return url, None


def smn():
    r = pedir(SMN_FEED, timeout=40)
    feed = ET.fromstring(r.content)
    items = [(i.findtext("title") or "", i.findtext("link") or "", i.findtext("description") or "")
             for i in feed.iter("item")]
    # avisos a muy corto plazo: nombran provincias en el texto
    candidatos = [i for i in items if "avisocortoplazo" not in i[1] or "SAN JUAN:" in i[2].upper()]
    with ThreadPoolExecutor(12) as ex:
        docs = dict(ex.map(_cap, [i[1] for i in candidatos if i[1]]))
    grilla = _grilla()
    vistos, alertas = set(), []
    for titulo, link, desc in candidatos:
        x = docs.get(link)
        if x is None:
            continue
        info = x.find(f"{CAP}info")
        if info is None or (x.findtext(f"{CAP}msgType") or "") == "Cancel":
            continue
        g = lambda k: (info.findtext(f"{CAP}{k}") or "").strip()
        vence = g("expires")
        try:
            if vence and dt.datetime.fromisoformat(vence) < AHORA:
                continue
        except ValueError:
            pass
        corto = "avisocortoplazo" in link
        zonas = []
        if corto:
            m = re.search(r"SAN JUAN:\s*([^.]*)", desc, re.I)
            if not m:
                continue
            zonas = [z.strip() for z in m.group(1).split(" - ") if z.strip()]
        else:
            cubiertos, poligonos = {}, []
            for pol in info.iter(f"{CAP}polygon"):
                try:
                    pts = [tuple(map(float, p.split(","))) for p in (pol.text or "").split()]
                except ValueError:
                    continue
                if len(pts) < 3:
                    continue
                lats, lons = [p[0] for p in pts], [p[1] for p in pts]
                n = 0
                for la, lo, dep in grilla:
                    if min(lats) <= la <= max(lats) and min(lons) <= lo <= max(lons) and dentro(la, lo, pts):
                        cubiertos[dep] = cubiertos.get(dep, 0) + 1
                        n += 1
                if n:
                    paso_p = max(1, len(pts) // 150)
                    poligonos.append([[round(a, 2), round(b, 2)] for a, b in pts[::paso_p]])
            if sum(cubiertos.values()) < 3:        # menos de ~300 km² dentro de San Juan: no aplica
                continue
            zonas = sorted(cubiertos, key=lambda d: -cubiertos[d])
        nivel = SEVERIDAD.get(g("severity"), "amarillo")
        evento = g("event") or titulo
        inicio = g("onset") or g("effective")
        clave = f"smn-{evento}-{nivel}-{'acp' if corto else 'alerta'}"
        if clave in vistos:                       # el SMN publica un archivo por zona y por franja horaria
            previa = next(a for a in alertas if a["_grupo"] == clave)
            previa["zonas"] += [z for z in zonas if z not in previa["zonas"]]
            previa["desde"] = min(previa["desde"], inicio) if inicio else previa["desde"]
            previa["hasta"] = max(previa["hasta"] or "", vence) or None
            if not corto:
                previa["poligonos"] += poligonos
            continue
        vistos.add(clave)
        detalle = g("description") if not corto else f"{titulo.capitalize()}. {desc}"
        alertas.append(alerta("SMN", True, nivel,
            ("Aviso a muy corto plazo: " if corto else "Alerta SMN: ") + evento.capitalize(),
            detalle, zonas, inicio, vence,
            url="https://www.smn.gob.ar/alertas", clave=clave, instruccion=g("instruction")))
        alertas[-1]["_grupo"] = clave
        alertas[-1]["poligonos"] = [] if corto else poligonos
    for a in alertas:                              # un aviso por evento, nivel y día de inicio
        a["clave"] = f"{a.pop('_grupo')}-{(a['desde'] or '')[:10]}"
        a["id"] = hashlib.sha1(a["clave"].encode()).hexdigest()[:12]
    return alertas


# ───────────────────────────── ALERTAS: armado y avisos ─────────────────────────────

def alerta(fuente, oficial, nivel, titulo, detalle, zonas, desde, hasta, url=None, clave=None, instruccion=None):
    clave = clave or titulo
    return {"id": hashlib.sha1(clave.encode()).hexdigest()[:12], "clave": clave, "fuente": fuente,
            "oficial": oficial, "nivel": nivel, "titulo": titulo, "detalle": detalle, "zonas": zonas,
            "desde": desde, "hasta": hasta, "url": url, "instruccion": instruccion}


def telegram(nuevas, url_pagina):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        if nuevas:
            print(f"Telegram sin configurar: {len(nuevas)} alerta(s) nueva(s) no se enviaron.")
        return
    icono = {"amarillo": "🟡", "naranja": "🟠", "rojo": "🔴"}
    for a in nuevas:
        origen = "Fuente oficial: SMN" if a["oficial"] else "Aviso automático (no oficial)"
        txt = (f"{icono[a['nivel']]} <b>ALERTA {a['nivel'].upper()} — San Juan</b>\n<b>{a['titulo']}</b>\n\n"
               f"{a['detalle'][:900]}\n\n" + (f"📍 {', '.join(a['zonas'][:12])}\n" if a["zonas"] else "") +
               f"ℹ️ {origen}" + (f"\n🔗 {url_pagina}" if url_pagina else ""))
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=30,
                              data={"chat_id": chat, "text": txt, "parse_mode": "HTML",
                                    "disable_web_page_preview": "true"})
            print("Telegram:", a["titulo"], "→", r.status_code)
        except Exception as ex:
            print("Telegram: error de envío", type(ex).__name__)


def principal():
    estado = {}
    alertas = []

    def paso(nombre, f, defecto):
        try:
            res = f()
            estado[nombre] = True
            return res
        except Exception as ex:
            estado[nombre] = False
            print(f"ERROR en {nombre}: {type(ex).__name__}: {ex}")
            return defecto

    previo = leer("datos.json", {})
    emb, a_emb, ok_sat = paso("satelite", embalses, (previo.get("embalses", []), [], False))
    estado["satelite"] = estado["satelite"] and ok_sat
    rs, a_rios = paso("rios", rios, (previo.get("rios", []), []))
    ll, a_lluvia = paso("lluvia", lluvia, (previo.get("lluvia", []), []))
    a_smn = paso("smn", smn, None)
    if a_smn is None:            # si el SMN no responde se conservan sus alertas aún vigentes
        a_smn = [a for a in previo.get("alertas", []) if a["fuente"] == "SMN"
                 and a.get("hasta") and dt.datetime.fromisoformat(a["hasta"]) > AHORA]
    alertas = sorted(a_smn + a_rios + a_lluvia + a_emb,
                     key=lambda a: (-NIVELES[a["nivel"]], not a["oficial"], a["desde"] or ""))

    historial = leer("alertas_historial.json", [])
    conocidas = {h["id"] for h in historial}
    nuevas = [a for a in alertas if a["id"] not in conocidas]
    for a in nuevas:
        historial.insert(0, {k: a[k] for k in ("id", "fuente", "oficial", "nivel", "titulo", "zonas", "desde", "hasta")}
                         | {"emitida": AHORA.isoformat(timespec="minutes")})
    historial = historial[:300]
    escribir("alertas_historial.json", historial, compacto=False)

    datos = {"generado": AHORA.isoformat(timespec="minutes"), "fuentes_ok": estado, "alertas": alertas,
             "historial_alertas": historial[:40], "embalses": emb, "rios": rs, "lluvia": ll,
             "umbrales": UMB}
    txt = escribir("datos.json", datos)
    (DATA / "datos.js").write_text("window.DATOS=" + txt + ";", encoding="utf-8")
    telegram(nuevas, os.environ.get("URL_PAGINA", ""))
    print(f"Listo: {len(alertas)} alerta(s) vigente(s), {len(nuevas)} nueva(s). Fuentes: {estado}")


if __name__ == "__main__":
    principal()
