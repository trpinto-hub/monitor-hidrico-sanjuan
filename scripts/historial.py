"""Carga inicial del historial satelital de embalses. Uso: python scripts/historial.py 2017-01-01"""
import sys, json, pathlib, datetime as dt
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import satelite

RAIZ = pathlib.Path(__file__).resolve().parent.parent
ARCH = RAIZ / "docs" / "data" / "embalses.json"

if __name__ == "__main__":
    desde = sys.argv[1] if len(sys.argv) > 1 else "2017-01-01"
    espaciado = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    cfg = json.loads((RAIZ / "scripts" / "config.json").read_text(encoding="utf-8"))
    datos = json.loads(ARCH.read_text(encoding="utf-8")) if ARCH.exists() else {}
    hoy = dt.date.today().isoformat()
    solo = sys.argv[3].split(",") if len(sys.argv) > 3 else None
    for e in cfg["embalses"]:
        if solo and e["id"] not in solo:
            continue
        serie, n = satelite.actualizar_serie(datos.get(e["id"], []), e["bbox"], desde, hoy,
                                             espaciado_dias=espaciado, procesos=20)
        datos[e["id"]] = serie
        ARCH.write_text(json.dumps(datos, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print(e["id"], "fechas revisadas:", len(n), "observaciones:", len(serie), flush=True)
