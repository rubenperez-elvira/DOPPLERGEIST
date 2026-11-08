# Auditoría previa y resultado, AeroDoppler v14

Fecha de la auditoría: 2026-11-08. No se modificó `main` ni el repositorio
`rperezel/Aero-Doppler-v13`. No se desplegó nada en Render.

## Revisiones auditadas

- Fork `rubenperez-elvira/DOPPLERGEIST`, `main` = `18e0acdb192f89e2ad2f15f4e320572f10ab456f`.
  Mensaje: Merge pull request #438 from 1337isnot1337/design/site-refinement-20261108.
  Fecha del commit: 2026-11-07 21:34:18 -0600. Autor del merge: Cinna Davis.
- Upstream declarado: https://github.com/1337isnot1337/DOPPLERGEIST. Ese commit es
  el merge de la PR #438 y es el `HEAD` con el que arranca esta rama.
- La rama de trabajo es `cursor/aerodoppler-v14-cloud-edcb`. El paquete nuevo está
  en `aerodoppler/` y no cambia el comportamiento de `src/dopplergeist`.

## Licencia

`LICENSE` es PolyForm Noncommercial 1.0.0, con el aviso obligatorio
`Required Notice: Copyright 2026 Cinna Davis`. GitHub la muestra como Other /
source-available. No se ha tratado como MIT.

El texto permite uso, modificación y distribución para fines no comerciales, y
el uso personal de investigación, experimento y prueba. También obliga a
entregar la licencia y el aviso. Esta adaptación es un servicio privado, sin
cobro y con acceso cerrado. El aviso y `LICENSE` viajan en la imagen Docker y
en `/license.txt`. `licenses/MIT-legacy.txt` se deja como está: no relicencia
el código posterior al cambio de licencia.

Los datos de vuelo no quedan cubiertos por PolyForm. adsb.aero documenta que
sus trayectorias proceden del archivo adsb.lol bajo ODbL 1.0. No se redistribuye
el archivo diario ni una base derivada; la aplicación consulta la API en el
momento de uso.

Conclusión: la adaptación y el alojamiento no comercial no están prohibidos.
Se continuó la implementación.

## Código y despliegue existentes

DOPPLERGEIST ya tiene CLI, interfaz local (`dopplergeist serve`, puerto 8765),
STFT/descubrimiento, ajuste Doppler con retardo y lectura del archivo
`globe_history`. Esa pila usa Numba, rasterio, pandas y índices de varios GB.
El `Dockerfile` de la raíz construye esa aplicación, no un servicio Render Free
de 512 MB. AeroDoppler v14 es un proceso aparte: FastAPI, NumPy, ffmpeg y sin
esas dependencias pesadas.

## Render Free, comprobado en la documentación oficial

Fuentes: https://render.com/docs/free, https://render.com/docs/compute-plans y
https://render.com/pricing, leídas el 2026-11-08.

- Plan `free`: 0,1 CPU y 512 MB de RAM.
- Disco efímero. Se pierde al redeploy, al reinicio y al dormir.
- Duerme tras 15 min sin HTTP ni mensajes WebSocket. El despertar ronda 1 min.
- 750 h de instancia al mes por espacio de trabajo. El ancho de banda Hobby
  incluido es 5 GB; el exceso se factura a 0,15 USD/GB o, sin medio de pago,
  suspende los servicios gratuitos.
- Sin disco persistente, sin SSH y sin más de una instancia.
- La alternativa de pago más pequeña publicada es `0.5c-512mb`, 7 USD/mes y
  512 MB. Sigue siendo poca RAM para el archivo global. No se contrata nada.

En esta VM, no en Render, un clip sintético de 60 s a 16 kHz tardó 0,36 s y el
proceso llegó a 164 MB de RSS máximo. 30 s: 0,21 s y 92 MB. Importar el código:
28 MB. Cabe en 512 MB para un solo clip corto. El límite de CPU de Render no se
midió; 0,36 s aquí no es el tiempo del plan gratuito. El presupuesto de análisis
del servicio es 40 s.

## Proveedores, comprobados

| Fuente | Qué se verificó | Historia automática acotada |
| --- | --- | --- |
| ADSB.lol API, https://api.adsb.lol/api/openapi.json | OpenAPI 3.1, licencia ODbL. Rutas en vivo `/v2/point` y similares. `POST /v2/history` respondió 405. | No. En vivo no es historia. |
| Consulta en vivo 40.97786, -5.60845, 43 NM | HTTP 200 el 2026-11-08. Había aeronaves en ese instante. | No describe el 7 de noviembre. |
| adsb.aero, https://adsb.aero/llms.txt y `/api/v1/data-range` | Sin clave. `first_date=2025-05-05`, `last_date=2026-11-08`. Datos del archivo adsb.lol, ODbL. Pide no saturar el único servidor. | Sí. Es la consulta usada. |
| FlyItalyADSB, https://flyitalyadsb.com/en/data/api/docs/history/ | Documenta `/v2/history` y `/v2/overflights` con `X-Api-Key`. Plan gratuito: 10 km y 6 h. CC BY-SA 4.0. Una llamada sin clave recibió 403 de Cloudflare. No se insistió. | Bloqueada: falta clave y 80 km excede el plan gratuito documentado. |
| `adsblol/globe_history_2026`, release `v2026.11.07-planes-readsb-prod-0` | ODbL. Activos exactos: `tar.aa` 2000000000 bytes y `tar.ab` 1404765184 bytes. Publicado 2026-11-08T03:26:49Z. | No hay API por lugar. No se descargó. No cabe en Render Free. |

## Caso Cabrerizos

Coordenadas pedidas: 40.97786, -5.60845. Centro 7 de noviembre de 2026, 23:30
Europe/Madrid. `zoneinfo` convierte ese instante a 22:30 UTC. La ventana de
±30 min es 22:00–23:00 UTC. El cambio de hora de 2026 fue el 25 de octubre, así
que esa noche es CET (UTC+1).

Open-Meteo, el 2026-11-08, devolvió elevación 827 m y un único «Cabrerizos» en
Salamanca (40.97808, -5.60907). La elevación es del terreno, no del micrófono,
y no está corregida al elipsoide.

Consulta real `POST https://adsb.aero/api/v1/query`, círculo de 80 km, intersección
entre 22:00 y 23:00 UTC, `include_path=true`. Respuesta HTTP 200, 14 vuelos,
`cursor` nulo, una página, unos 1,1 s desde esta VM. Archivo declarado que
incluye la fecha. Cobertura de receptores: desconocida. No es «cero vuelos».

Los 14 son reactores confirmados por designador OACI (A319, A320, A20N, A21N,
A306, B738, B38M). El ranking acústico los excluye y los muestra como tales.
No se fabricó ningún vuelo no reactor para rellenar el resultado.

Distancia 3D del vértice más cercano dentro de esa hora, observador a 827 m,
ECEF WGS84. La altitud del avión es presión más la corrección QNH de adsb.aero,
en metros. El retardo es `distancia/343`. Son vértices simplificados, no una
interpolación continua. EJU74WN entra porque el proveedor marca intersección del
tramo, pero sus vértices guardados quedan a 102 km.

| Indicativo | Tipo | Distancia 3D | Retardo |
| --- | --- | --- | --- |
| EJU41DC | A319 | 26,1 km | 76 s |
| BCS2RA | A306 | 27,4 km | 80 s |
| RYR18PW | B738 | 33,0 km | 96 s |
| RYR3JC | B38M | 35,8 km | 104 s |
| BEL3LI | A20N | 40,0 km | 117 s |
| CXI1B | B738 | 42,4 km | 124 s |
| CXI37WP | B738 | 45,4 km | 133 s |
| WMT6501 | A21N | 50,0 km | 146 s |
| RYR48LQ | B38M | 55,8 km | 163 s |
| JAF9JD | B738 | 57,4 km | 167 s |
| RYR700Y | B738 | 67,9 km | 198 s |
| DLH89J | A20N | 71,1 km | 207 s |
| RYR3X | B738 | 79,2 km | 231 s |
| EJU74WN | A320 | 102,2 km | 298 s |

No había una grabación real de esa hora. El modo C calculó la geometría y dejó
el ajuste acústico sin puntuar. No se presenta como una identificación.

## Pruebas de esta VM

`PYTHONPATH=aerodoppler/src pytest -q aerodoppler/tests`: 20 pasaron.

Incluyen el acuerdo entre la fórmula cerrada y el retardo numérico, el ajuste
de un Doppler sintético de 12 s, el rechazo de silencio, ruido y chirp lineal,
la decodificación ffmpeg de WAV, MP3 y M4A, los estados de historia, la
autenticación cerrada, el límite de tamaño y que el secreto no sale en la
cookie ni en el HTML.

Aparte de pytest, el mismo analizador sobre un tono sintético de 60 s tardó
0,36 s en esta VM, con RMSE 0,98 Hz y R² 0,9997. El RSS máximo del proceso fue
164 MB. Ese tiempo no está medido en una instancia Render de 0,1 CPU.

## Navegador de esta VM

Chrome headless, no Safari y no un iPhone, abrió `GET /login` a 390×844 y a
1280×900. Se ve el formulario «Acceso restringido», el botón «Entrar» y el aviso
PolyForm / Copyright 2026 Cinna Davis. Con la cookie de sesión, el HTML de `/`
contiene «Analizar audio», «Consultar historia» y «Comparar pista y trayectorias».
El proceso local, con el token solo en el entorno, respondió `unsupported_history`
para ADSB.lol en la ventana 22:00–23:00 UTC y `zero_flights_claim: false`.
Docker no está instalado en esta VM: la imagen no se construyó aquí. El
`Dockerfile` y `render.yaml` quedan listos para el servicio nuevo.

## Pendiente de verdad

- Safari en iPhone: botones, selector y subida real.
- Crear el servicio Render, poner el token, revisar el despliegue y los registros.
  El padre todavía no ha configurado el acceso a Render.
- Una grabación real de la ventana, si se quiere puntuar compatibilidad acústica
  con estos 14 vuelos. Con la política de no reactores, el ranking acústico de
  este caso seguiría vacío aunque exista la grabación.
