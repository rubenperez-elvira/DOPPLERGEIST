# AeroDoppler v14 Cloud

Aplicación web móvil, en español, para un solo usuario autenticado. Adapta el
enfoque de DOPPLERGEIST a clips cortos y a Render Free. No sustituye el servicio
local `dopplergeist serve` ni toca `https://aero-doppler-v13.onrender.com`.

Required Notice: Copyright 2026 Cinna Davis. Licencia del software: PolyForm
Noncommercial 1.0.0 (`LICENSE`). No es una licencia permisiva. El alojamiento
previsto es personal y no comercial; un uso comercial necesitaría otra
autorización.

## Qué hace

- **Dónde se grabó.** El formulario pide el audio y una ventana de tiempo. No pide coordenadas, centro ni radio. La pista regional es opcional («no sé» o «España peninsular y Baleares»); si no se da, el área es la misma. Esa área son círculos de 110 km y no cabe en Render Free: el servicio no lanza las consultas y ofrece `python -m aerodoppler.wide_search --plan` en el ordenador, sin descargar el archivo diario de adsb.lol. El veredicto (`ok` / `weak` / `ambiguous` / `failed`) y las regiones del 68 % y del 90 % se mantienen. Ningún punto se marca como seguro.
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

47 pruebas pasaron y 1 se omitió porque la grabación privada no está montada.
En sintético, con la misma física del buscador, un centro de búsqueda a 2,5 km al
norte y 2,0 km al este del micrófono, y una rejilla de 1,5 km afinada a 375 m:
tres pasadas que se cruzan y reloj conocido dieron veredicto compatible, error
177 m, radio del 68 % 842 m y del 90 % 1353 m. Dos pasadas con el reloj
desfasado 18 s recuperaron esos 18 s; el veredicto fue ambiguo y no se marcó un
punto (radios 1886 m y 3876 m). Dos pasadas paralelas quedaron ambiguas. Un
reloj fuera de la ventana quedó insuficiente, sin región. Es consistencia
interna, no una precisión sobre grabaciones reales. El área por defecto, sin
pista regional, son 21 teselas de 110 km: 84 llamadas a adsb.aero en el peor
caso y una cota de 2100 s de red. El servicio gratuito no las ejecuta. Esa
cifra no es un error de localización. Una provincia o una comunidad cabe en la
web solo si un círculo de 110 km cubre su polígono: 4 llamadas y hasta 112 s.
Badajoz, Cáceres, Ciudad Real, Toledo, Zaragoza, Baleares, Las Palmas, Santa
Cruz de Tenerife y las comunidades de más de un círculo quedan en el modo local.

Pendiente, y no sustituible por esta VM: Safari en un iPhone real (botones,
selector de archivos y subida) y el despliegue real en Render con sus registros.
Esta VM no es un iPhone ni el servicio desplegado.

## Límites

Clips de hasta 90 s y 8 MiB. Una análisis pesada a la vez. Cada consulta histórica
sigue limitada a 120 km y, por defecto, a 360 min. España peninsular y Baleares
no caben en una consulta: el plan gratuito no recorre esas teselas. El modo local
sí puede pedirlas una a una, solo con área y tiempo, y se detiene al agotar su
presupuesto. La historia de adsb.aero mira salidas desde 72 h antes y como máximo
3 páginas. La cobertura de receptores sigue siendo desconocida.
El archivo diario global de ADSB.lol (3,40 GB el 7 de noviembre de 2026) no se
descarga ni se consulta desde Render Free.
