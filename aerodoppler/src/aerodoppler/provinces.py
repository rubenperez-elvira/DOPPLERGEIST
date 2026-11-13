"""Provincial search areas from embedded Eurostat GISCO polygons.

Each province is covered by circles of at most 110 km placed on sample points
of its real outline and interior. A centroid and a radius are not used: a
vertex that falls outside every circle fails the cover. Islands stay separate
polygons, so Canarias is not one disk drawn across the ocean.
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

from .regions import ASSUMED_ALTITUDE_M, TILE_RADIUS_KM

_DATA = Path(__file__).with_name("data") / "provinces.json"
_STEP_KM = 18.0


def load_provinces() -> dict:
    return json.loads(_DATA.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _index():
    raw = load_provinces()
    provinces = {item["id"]: item for item in raw["provinces"]}
    communities = []
    for community in raw["communities"]:
        members = [item["id"] for item in raw["provinces"] if item["community"] == community["id"]]
        communities.append({**community, "provinces": members})
    neighbors = _neighbors(provinces)
    return {"raw": raw, "provinces": provinces, "communities": communities, "neighbors": neighbors}


def attribution() -> dict:
    raw = _index()["raw"]
    return {"source": raw["source"], "license": raw["license"], "url": raw["url"], "changes": raw["changes"]}


def communities() -> list[dict]:
    return _index()["communities"]


def province_record(province_id: str) -> dict:
    try:
        return _index()["provinces"][province_id]
    except KeyError as error:
        raise ValueError("Esa provincia no está en la lista.") from error


def neighbors_of(province_id: str) -> list[str]:
    province_record(province_id)
    return list(_index()["neighbors"].get(province_id, ()))


def selection_from_spec(spec: str | None) -> dict:
    """Resolve a regional hint to polygons. Blank and «no sé» stay the wide default."""
    text = (spec or "").strip().lower()
    if text in {"", "unsure", "no-se", "nose", "no se", "no sé", "iberia", "espana", "españa"}:
        return {"kind": "iberia", "ids": []}
    if text.startswith("cc:"):
        community_id = text[3:].upper()
        community = next((item for item in communities() if item["id"] == community_id), None)
        if community is None:
            raise ValueError("Esa comunidad no está en la lista.")
        return {"kind": "community", "id": community_id, "label": community["name"], "ids": list(community["provinces"])}
    ids = [part.strip() for part in text.removeprefix("pr:").split(",") if part.strip()]
    if not ids:
        raise ValueError("Elige una provincia o deja «no sé».")
    for province_id in ids:
        province_record(province_id)
    if len(ids) > 1 and not _connected(ids):
        raise ValueError("Esas provincias no forman un grupo contiguo.")
    names = [province_record(province_id)["name"] for province_id in ids]
    label = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " y " + names[-1]
    return {"kind": "provinces", "id": ",".join(ids), "label": label, "ids": ids}


@lru_cache(maxsize=128)
def cover_selection(spec: str | None) -> dict:
    chosen = selection_from_spec(spec)
    if chosen["kind"] == "iberia":
        return {"kind": "iberia"}
    rings = []
    for province_id in chosen["ids"]:
        rings.extend(province_record(province_id)["rings"])
    samples = _samples(rings)
    tiles = _cover(samples, TILE_RADIUS_KM)
    lats = [point[1] for point in samples]
    lons = [point[0] for point in samples]
    center_lat = (min(lats) + max(lats)) / 2
    center_lon = (min(lons) + max(lons)) / 2
    radius = 0.0
    for lat, lon in ((min(lats), min(lons)), (min(lats), max(lons)), (max(lats), min(lons)), (max(lats), max(lons))):
        radius = max(radius, _km(center_lat, center_lon, lat, lon))
    return {
        "kind": chosen["kind"],
        "id": chosen.get("id") or chosen.get("ids", [""])[0],
        "label": chosen["label"],
        "ids": chosen["ids"],
        "why": (
            f"La búsqueda se limita a {chosen['label']}. "
            "Si el audio se grabó fuera de esa zona, el resultado será insuficiente o erróneo."
        ),
        "role": "área donde buscar, no el lugar de la grabación",
        "tile_radius_km": TILE_RADIUS_KM,
        "tiles": tiles,
        "tile_count": len(tiles),
        "samples": len(samples),
        "map": {"lat": center_lat, "lon": center_lon, "radius_km": max(30, math.ceil(radius + 20))},
        "assumed_altitude_m": ASSUMED_ALTITUDE_M,
        "altitude_note": "Si no indicas altitud, se suponen 600 m en toda la zona. No es una cota medida ni un modelo de terreno.",
        "optional": True,
    }


def _samples(rings) -> list[tuple[float, float]]:
    points = []
    for ring in rings:
        dense = _densify(ring, _STEP_KM)
        points.extend(dense)
        lons = [point[0] for point in dense]
        lats = [point[1] for point in dense]
        step_lat = _STEP_KM / 110.57
        lat = min(lats)
        while lat <= max(lats) + 1e-9:
            step_lon = _STEP_KM / max(20.0, 111.32 * math.cos(math.radians(lat)))
            lon = min(lons)
            while lon <= max(lons) + 1e-9:
                if _inside(lon, lat, rings):
                    points.append((lon, lat))
                lon += step_lon
            lat += step_lat
    unique = []
    seen = set()
    for lon, lat in points:
        key = (round(lon, 3), round(lat, 3))
        if key not in seen:
            seen.add(key)
            unique.append((lon, lat))
    if not unique:
        raise ValueError("La provincia no tiene geometría utilizable.")
    return unique


def _densify(ring, step_km: float) -> list[tuple[float, float]]:
    if not ring:
        return []
    origin = ring[0]
    closed = ring if ring[0] == ring[-1] else [*ring, origin]
    out = []
    for start, end in zip(closed, closed[1:]):
        dist = _km(start[1], start[0], end[1], end[0])
        pieces = max(1, int(math.ceil(dist / step_km)))
        for index in range(pieces):
            weight = index / pieces
            out.append((start[0] + weight * (end[0] - start[0]), start[1] + weight * (end[1] - start[1])))
    return out


def _cover(samples, radius_km: float) -> list[dict]:
    remaining = set(range(len(samples)))
    tiles = []
    while remaining:
        best_at = None
        best_hit: list[int] = []
        for center in remaining:
            lon, lat = samples[center]
            hit = [index for index in remaining if _km(lat, lon, samples[index][1], samples[index][0]) <= radius_km + 1e-6]
            if len(hit) > len(best_hit):
                best_at = center
                best_hit = hit
        lon, lat = samples[best_at]
        tiles.append({
            "name": f"{lat:.2f},{lon:.2f}",
            "lat": lat,
            "lon": lon,
            "radius_km": radius_km,
            "role": "tesela de consulta, no micrófono",
        })
        remaining.difference_update(best_hit)
    return tiles


def _inside(lon: float, lat: float, rings) -> bool:
    return any(_ring_contains(lon, lat, ring) for ring in rings)


def _ring_contains(lon: float, lat: float, ring) -> bool:
    inside = False
    for start, end in zip(ring, ring[1:] + ring[:1]):
        x1, y1 = start
        x2, y2 = end
        if (y1 > lat) == (y2 > lat):
            continue
        cross = x1 + (lat - y1) / (y2 - y1) * (x2 - x1)
        if cross > lon:
            inside = not inside
    return inside


def _neighbors(provinces: dict) -> dict[str, list[str]]:
    bounds = {}
    clouds = {}
    for province_id, record in provinces.items():
        pts = [(point[0], point[1]) for ring in record["rings"] for point in _densify(ring, 8.0)]
        clouds[province_id] = pts
        lons = [point[0] for point in pts]
        lats = [point[1] for point in pts]
        bounds[province_id] = (min(lons), max(lons), min(lats), max(lats))
    graph = {province_id: [] for province_id in provinces}
    ids = list(provinces)
    for left_at, left in enumerate(ids):
        for right in ids[left_at + 1 :]:
            if not _boxes_near(bounds[left], bounds[right]):
                continue
            if _clouds_near(clouds[left], clouds[right], 3.0):
                graph[left].append(right)
                graph[right].append(left)
    return graph


def _boxes_near(left, right) -> bool:
    # Expand by about 0.15 degrees so a shared border is not missed.
    return not (left[1] < right[0] - 0.15 or right[1] < left[0] - 0.15 or left[3] < right[2] - 0.15 or right[3] < left[2] - 0.15)


def _clouds_near(left, right, max_km: float) -> bool:
    for lon, lat in left:
        for other_lon, other_lat in right:
            if abs(lat - other_lat) > 0.2 or abs(lon - other_lon) > 0.25:
                continue
            if _km(lat, lon, other_lat, other_lon) <= max_km:
                return True
    return False


def _connected(ids: list[str]) -> bool:
    graph = _index()["neighbors"]
    seen = {ids[0]}
    stack = [ids[0]]
    wanted = set(ids)
    while stack:
        current = stack.pop()
        for nxt in graph.get(current, ()):
            if nxt in wanted and nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen == wanted


def _km(lat1, lon1, lat2, lon2) -> float:
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(max(0.0, a))))
