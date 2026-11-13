# Monitor Hídrico San Juan

Página web que informa de manera permanente cómo evolucionan los **ríos y diques de la provincia de San Juan (Argentina)** usando información satelital, y que muestra y difunde las **alertas** para la población.

> **Aviso:** es una herramienta informativa. No reemplaza al Servicio Meteorológico Nacional, a Protección Civil ni a los operadores de los diques. Ante una emergencia: **911** o Protección Civil **103**.

## Qué muestra

| Sección | Fuente | Frecuencia |
|---|---|---|
| **Diques** (Ullum, Punta Negra, Los Caracoles, Cuesta del Viento, San Agustín): superficie del espejo de agua en km², tendencia, comparación con el máximo observado desde 2017 y última imagen satelital | Satélite Sentinel-2 (Copernicus / ESA) | Cada pasada sin nubes (3 a 10 días) |
| **Ríos** (San Juan, Los Patos, Castaño, Jáchal, Bermejo): caudal estimado de los últimos 3 meses y pronóstico a 15 días | Modelo GloFAS (Copernicus), vía Open-Meteo | Diaria |
| **Lluvia pronosticada** en 12 localidades | Open-Meteo | Cada 2 horas |
| **Alertas oficiales** vigentes para San Juan | Servicio Meteorológico Nacional (canal CAP) | Cada 2 horas |
| **Mapa** con imagen satelital del día | NASA GIBS (VIIRS), Esri, OpenStreetMap | Diaria |

## Cuándo se genera una alerta

| Tipo | Amarilla | Naranja | Roja |
|---|---|---|---|
| **SMN (oficial)** | Según el nivel que emite el SMN para zonas de San Juan | ídem | ídem |
| **Río (automática)** | Caudal actual o previsto a 7 días ≥ crecida de 2 años | ≥ crecida de 5 años | ≥ crecida de 20 años |
| **Lluvia (automática)** | ≥ 20 mm en 24 h | ≥ 40 mm | ≥ 70 mm |
| **Embalse (automática)** | Espejo ≥ 97 % del máximo observado, o crecimiento ≥ 10 % en 12 días | — | — |

Los umbrales se cambian en `scripts/config.json` (sección `umbrales`). Los umbrales de cada río están en `docs/data/rios_clima.json`.

## Cómo funciona

1. **GitHub Actions** ejecuta `scripts/actualizar.py` cada 2 horas (archivo `.github/workflows/actualizar.yml`).
2. El script descarga los datos, calcula las alertas y guarda todo en `docs/data/`.
3. La página (`docs/index.html`) lee esos archivos. No hay servidor ni base de datos: todo es gratuito.

Para ejecutarlo a mano: pestaña **Actions → Actualizar datos → Run workflow**.

## Publicar la página (cuando decida hacerla pública)

1. En el repositorio: **Settings → General →** al final, **Change repository visibility → Make public**.
2. **Settings → Pages →** en *Branch* elegir `main` y carpeta `/docs` → **Save**.
3. En un par de minutos la página queda en `https://USUARIO.github.io/monitor-hidrico-sanjuan/`.
4. (Opcional) **Settings → Secrets and variables → Actions → Variables → New repository variable**: nombre `URL_PAGINA`, valor la dirección anterior. Así los mensajes de Telegram incluyen el enlace.

Mientras el repositorio sea privado, la página se puede ver descargando el repositorio (**Code → Download ZIP**) y abriendo `docs/index.html` con doble clic.

## Activar los avisos por Telegram

1. En Telegram, abrir **@BotFather**, enviar `/newbot` y seguir los pasos. Copiar el **token** que entrega.
2. Crear un **canal** (por ejemplo «Alertas hídricas San Juan») y agregar el bot como **administrador**.
3. En GitHub: **Settings → Secrets and variables → Actions → New repository secret** y crear dos secretos:
   - `TELEGRAM_BOT_TOKEN`: el token del paso 1.
   - `TELEGRAM_CHAT_ID`: el nombre del canal con arroba (ejemplo `@alertas_hidricas_sj`). Si el canal es privado, se usa su identificador numérico (empieza con `-100`).
4. Listo: cada alerta **nueva** se envía una sola vez al canal.

## Limitaciones que conviene conocer

- De los diques se mide **superficie**, no cota ni volumen. El «% del máximo» es respecto de la mayor superficie vista por el satélite desde 2017, no de la capacidad de diseño.
- Con nubes sobre el embalse no hay medición ese día.
- En invierno (mayo a agosto) las sombras de los cañones pueden alterar la medición de Punta Negra y Los Caracoles; por eso las lecturas anómalas se descartan, el máximo de referencia se toma fuera del invierno y las alertas automáticas de embalse no se emiten en esos meses.
- Los caudales son **estimaciones de un modelo global**, no aforos. El modelo no representa la regulación de los diques y puede diferir de los valores reales; por eso las alertas de río comparan el modelo contra su propia historia (1984 en adelante).
- GitHub puede demorar algunos minutos las ejecuciones programadas.

## Estructura

```
docs/index.html            Página web
docs/data/                 Datos que genera el robot (no editar a mano)
scripts/actualizar.py      Actualización completa y armado de alertas
scripts/satelite.py        Medición de embalses sobre imágenes Sentinel-2
scripts/historial.py       Carga inicial del historial satelital
scripts/config.json        Diques, ríos, localidades y umbrales
scripts/san_juan.json      Contorno provincial y departamentos
```

Datos: Copernicus Sentinel (ESA), Copernicus Emergency Management Service (GloFAS), Open-Meteo (CC BY 4.0), SMN (CC BY 4.0), NASA GIBS, © OpenStreetMap (ODbL), Georef/IGN.
