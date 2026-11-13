"""Default search region when the user does not know where the audio was made.

The application speaks Spanish and reads times as Europe/Madrid. The useful
default is therefore peninsular Spain and the Balearic Islands, not western
Europe: France, Italy and the Maghreb would multiply provider queries without
any hint that the recording was made there. The region is the union of 110 km
circles on the places below. It is not an administrative border. Circles can
include a strip of Portugal or France near the boundary.
"""

from __future__ import annotations

import math

# Under the 120 km history cap, so one tile is one adsb.aero circle.
TILE_RADIUS_KM = 110.0
ASSUMED_ALTITUDE_M = 600.0

# Places that have to fall inside some tile. Spacing is intentionally tighter
# than two radii so the union does not leave a large hole in the peninsula.
PLACES = (
    ("Fisterra", 42.90, -9.27),
    ("A Coruña", 43.37, -8.40),
    ("Lugo", 43.01, -7.56),
    ("Oviedo", 43.36, -5.85),
    ("Santander", 43.46, -3.80),
    ("Bilbao", 43.26, -2.93),
    ("San Sebastián", 43.32, -1.98),
    ("Pamplona", 42.82, -1.64),
    ("Huesca", 42.14, -0.41),
    ("Girona", 41.98, 2.82),
    ("Barcelona", 41.39, 2.17),
    ("Ourense", 42.34, -7.86),
    ("Ponferrada", 42.55, -6.59),
    ("León", 42.60, -5.57),
    ("Burgos", 42.34, -3.70),
    ("Soria", 41.76, -2.46),
    ("Zaragoza", 41.65, -0.88),
    ("Lleida", 41.62, 0.62),
    ("Vigo", 42.23, -8.72),
    ("Zamora", 41.50, -5.75),
    ("Valladolid", 41.65, -4.72),
    ("Madrid", 40.42, -3.70),
    ("Cuenca", 40.07, -2.13),
    ("Teruel", 40.34, -1.11),
    ("Castellón", 39.99, -0.04),
    ("Valencia", 39.47, -0.38),
    ("Salamanca", 40.97, -5.66),
    ("Cáceres", 39.48, -6.37),
    ("Badajoz", 38.88, -6.97),
    ("Huelva", 37.26, -6.95),
    ("Sevilla", 37.39, -5.98),
    ("Cádiz", 36.53, -6.29),
    ("Algeciras", 36.13, -5.45),
    ("Málaga", 36.72, -4.42),
    ("Granada", 37.18, -3.60),
    ("Jaén", 37.77, -3.79),
    ("Córdoba", 37.88, -4.78),
    ("Ciudad Real", 38.99, -3.93),
    ("Albacete", 38.99, -1.86),
    ("Almería", 36.84, -2.46),
    ("Murcia", 37.98, -1.13),
    ("Alicante", 38.35, -0.48),
    ("Toledo", 39.86, -4.02),
    ("Palma", 39.57, 2.65),
    ("Maó", 39.89, 4.26),
    ("Ibiza", 38.91, 1.43),
)

WHY = (
    "Sin una pista más fina, el área por defecto es España peninsular y Baleares. "
    "La aplicación está en español y la hora se lee en Europe/Madrid. "
    "Europa occidental entera multiplicaría las consultas a adsb.aero sin un indicio de que el audio sea de allí."
)


def resolve_region(value: str | None) -> str:
    """Blank, «no sé» and the Iberian name are the same wide default."""
    text = (value or "").strip().lower()
    if text in {"", "unsure", "no-se", "nose", "no se", "no sé", "iberia", "espana", "españa"}:
        return "iberia"
    raise ValueError("La pista regional no se reconoce. Puedes dejarla en «no sé».")


def region_tiles(region_id: str | None = "iberia", radius_km: float = TILE_RADIUS_KM) -> dict:
    if resolve_region(region_id) != "iberia":
        raise ValueError("La pista regional no se reconoce.")
    if not 1 <= radius_km <= 120:
        raise ValueError("Cada tesela admite de 1 a 120 km.")
    tiles = _cover(PLACES, radius_km)
    lats = [place[1] for place in PLACES]
    lons = [place[2] for place in PLACES]
    center_lat = (min(lats) + max(lats)) / 2
    center_lon = (min(lons) + max(lons)) / 2
    radius = 0.0
    for lat, lon in ((min(lats), min(lons)), (min(lats), max(lons)), (max(lats), min(lons)), (max(lats), max(lons))):
        radius = max(radius, _km(center_lat, center_lon, lat, lon))
    return {
        "id": "iberia",
        "label": "España peninsular y Baleares",
        "why": WHY,
        "optional": True,
        "role": "área donde buscar, no el lugar de la grabación",
        "tile_radius_km": radius_km,
        "tiles": tiles,
        "tile_count": len(tiles),
        "places": len(PLACES),
        "map": {"lat": center_lat, "lon": center_lon, "radius_km": math.ceil(radius + 30)},
        "assumed_altitude_m": ASSUMED_ALTITUDE_M,
        "altitude_note": "Si no indicas altitud, se suponen 600 m en toda la zona. No es una cota medida ni un modelo de terreno.",
    }


def _cover(places, radius_km: float) -> list[dict]:
    remaining = list(places)
    tiles = []
    while remaining:
        best_center = None
        best_hit = []
        for candidate in places:
            hit = [place for place in remaining if _km(candidate[1], candidate[2], place[1], place[2]) <= radius_km + 1e-6]
            if len(hit) > len(best_hit):
                best_center = candidate
                best_hit = hit
        name, lat, lon = best_center
        tiles.append({
            "name": name,
            "lat": lat,
            "lon": lon,
            "radius_km": radius_km,
            "covers": [place[0] for place in best_hit],
            "role": "tesela de consulta, no micrófono",
        })
        covered = {place[0] for place in best_hit}
        remaining = [place for place in remaining if place[0] not in covered]
    return tiles


def _km(lat1, lon1, lat2, lon2) -> float:
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))
