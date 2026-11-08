# AeroDoppler v14 Cloud

Aplicación web móvil, en español, para un solo usuario autenticado. Adapta el
enfoque de DOPPLERGEIST a clips cortos y a Render Free. No sustituye el servicio
local `dopplergeist serve` ni toca `https://aero-doppler-v13.onrender.com`.

Required Notice: Copyright 2026 Cinna Davis. Licencia del software: PolyForm
Noncommercial 1.0.0 (`LICENSE`). No es una licencia permisiva. El alojamiento
previsto es personal y no comercial; un uso comercial necesitaría otra
autorización.

## Qué hace

- **Posición.** Con el audio, una hora de inicio incierta y una zona amplia, puntúa celdas contra trayectorias históricas no reactor. El centro de la zona no es el resultado. Una sola pasada queda ambigua. Si el audio o los aviones no bastan, no hay coordenada.
- **A.** Extrae el audio de WAV, MP3, M4A o MP4, calcula un espectrograma STFT,
  sigue una pista tonal y ajusta un paso recto con retardo acústico. Rechaza
  silencio, ruido y un descenso que no se distinga de una recta. No llama
  aeronave a una frecuencia descendente.
- **B.** Consulta historia real en adsb.aero dentro de un radio y una ventana
  Europe/Madrid. Separa cero respuestas, error, historia no soportada, consulta
  no soportada y cobertura desconocida. El tráfico en vivo de ADSB.lol no se
  usa como historia.
- **C.** Compara trayectorias con la pista: distancia 3D, retardo `distancia/c`,
  velocidad radial cuando los vértices vecinos están a menos de 70 s, y residuo
  del modelo. Varias compatibilidades siguen visibles. No hay identificación
  única. Reactores y tipos no confirmados quedan fuera del ranking acústico y
  se listan aparte.

El audio vive en un directorio temporal y se borra al terminar la petición. No
hay disco persistente. Render Free duerme a los 15 minutos.

## Arranque

```bash
export AERDOPPLER_ACCESS_TOKEN='un-codigo-de-al-menos-20-caracteres'
export AERDOPPLER_COOKIE_SECURE=1   # en HTTPS
cd aerodoppler
PYTHONPATH=src python -m aerodoppler
```

Render inyecta `PORT`. Salud: `GET /healthz`. Listo para tráfico útil: `GET /readyz`
responde 503 hasta que existan el token y ffmpeg. Sin token, el análisis y las
consultas responden 503.

El blueprint está en `render.yaml`. Crea solo el servicio nuevo Free
`aerodoppler-v14` en Frankfurt, con `autoDeployTrigger: off`. Render genera
`AERDOPPLER_ACCESS_TOKEN` y no lo guarda en el repositorio. Tras aprobar el
blueprint, el valor se consulta en Environment de ese servicio. No aplicar el
blueprint sobre el servicio v13.

## Pruebas ejecutadas aquí

```bash
PYTHONPATH=aerodoppler/src pytest -q aerodoppler/tests
```

20 pruebas pasaron en la VM de desarrollo. El detalle medido está en `AUDIT.md`.

Pendiente, y no sustituible por esta VM: Safari en un iPhone real (botones,
selector de archivos y subida) y el despliegue real en Render con sus registros.
Esta VM no es un iPhone ni el servicio desplegado.

## Límites

Clips de hasta 90 s y 8 MiB. Una análisis pesada a la vez. Radio hasta 120 km y
ventana hasta 180 min. La historia de adsb.aero mira salidas desde 72 h antes y
como máximo 3 páginas. La cobertura de receptores sigue siendo desconocida.
El archivo diario global de ADSB.lol (3,40 GB el 7 de noviembre de 2026) no se
descarga ni se consulta desde Render Free.
