"""Loader POI stratégiques : amenities OSM + BDNB ERP → fonction par bâtiment.

Requête Overpass pour les équipements stratégiques (hôpital, mairie, caserne,
police, gare, stade, culte, EHPAD, centre commercial…), appariement au
footprint BD TOPO, puis enrichissement optionnel via BDNB ERP.

Produit un **dict ID bâtiment → fonction** (str) consommé par `build_env_layer`
pour remplir la colonne `fonction` et les flags `is_strategic` / `is_education`.
"""

import logging
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point

from .cache import ensure_cached, valid_geofile
from .osm import _run_overpass, _bbox_hash

logger = logging.getLogger(__name__)

# ── Taxonomie OSM → fonction ─────────────────────────────────────────────────

# Mapping tag OSM → fonction normalisée. Clé = (tag_key, tag_value).
_OSM_TAG_TO_FONCTION: dict[tuple[str, str], str] = {
    ("amenity", "hospital"): "hopital",
    ("amenity", "clinic"): "hopital",
    ("amenity", "townhall"): "mairie",
    ("amenity", "fire_station"): "caserne",
    ("amenity", "police"): "police",
    ("amenity", "place_of_worship"): "culte",
    ("amenity", "community_centre"): "gymnase",
    ("office", "government"): "prefecture",
    ("leisure", "stadium"): "stade",
    ("leisure", "sports_centre"): "gymnase",
    ("railway", "station"): "gare",
    ("public_transport", "station"): "gare",
    ("social_facility", "*"): "ehpad",
    ("shop", "mall"): "centre_commercial",
    ("shop", "supermarket"): "centre_commercial",
}

# Tags Overpass à requêter (dédupliqués par clé pour limiter la taille).
_OVERPASS_TAGS: list[tuple[str, str]] = [
    ("amenity", "hospital"),
    ("amenity", "clinic"),
    ("amenity", "townhall"),
    ("amenity", "fire_station"),
    ("amenity", "police"),
    ("amenity", "place_of_worship"),
    ("amenity", "community_centre"),
    ("office", "government"),
    ("leisure", "stadium"),
    ("leisure", "sports_centre"),
    ("railway", "station"),
    ("public_transport", "station"),
    ("social_facility", ""),  # any value
    ("shop", "mall"),
    ("shop", "supermarket"),
]

# Fonctions considérées comme "éducation" (pour le flag is_education).
EDUCATION_FONCTIONS = frozenset({"ecole", "college", "lycee", "creche"})

# Fonctions considérées comme "stratégique" (pour le flag is_strategic).
# = tout ce qui n'est pas éducation.
STRATEGIC_FONCTIONS = frozenset({
    "hopital", "mairie", "caserne", "police", "culte", "gymnase",
    "prefecture", "stade", "gare", "ehpad", "centre_commercial",
})


# ── Requête Overpass ─────────────────────────────────────────────────────────

def _build_overpass_query(bbox: tuple[float, float, float, float]) -> str:
    """Construit la requête Overpass pour les POI stratégiques (ways + nodes)."""
    minx, miny, maxx, maxy = bbox
    bb = f"{miny},{minx},{maxy},{maxx}"

    lines = []
    for key, value in _OVERPASS_TAGS:
        filt = f'["{key}"="{value}"]' if value else f'["{key}"]'
        lines.append(f"  way{filt}({bb});")
        lines.append(f"  node{filt}({bb});")
        lines.append(f"  relation{filt}({bb});")

    body = "\n".join(lines)
    return f"""
[out:json][timeout:180];
(
{body}
);
out body;
>;
out skel qt;
"""


def _parse_overpass_result(result) -> gpd.GeoDataFrame:
    """Parse le résultat Overpass en GeoDataFrame de POI (points + polygones)."""
    from shapely.geometry import Polygon

    rows = []

    # Nœuds (points)
    for node in result.nodes:
        tags = node.tags
        fonction = _resolve_fonction(tags)
        if fonction is None:
            continue
        rows.append({
            "osm_id": int(node.id),
            "osm_type": "node",
            "fonction": fonction,
            "name": tags.get("name", ""),
            "geometry": Point(float(node.lon), float(node.lat)),
        })

    # Ways (polygones)
    for way in result.ways:
        tags = way.tags
        fonction = _resolve_fonction(tags)
        if fonction is None:
            continue
        try:
            coords = [(float(n.lon), float(n.lat)) for n in way.nodes]
            if len(coords) < 4:
                continue
            geom = Polygon(coords)
            if not geom.is_valid:
                geom = geom.buffer(0)
            if geom.is_empty:
                continue
        except Exception:
            continue
        rows.append({
            "osm_id": int(way.id),
            "osm_type": "way",
            "fonction": fonction,
            "name": tags.get("name", ""),
            "geometry": geom,
        })

    if not rows:
        return gpd.GeoDataFrame(
            columns=["osm_id", "osm_type", "fonction", "name", "geometry"],
            geometry="geometry", crs="EPSG:4326",
        )
    return gpd.GeoDataFrame(rows, crs="EPSG:4326")


def _resolve_fonction(tags: dict) -> "str | None":
    """Détermine la fonction d'un élément OSM à partir de ses tags."""
    for (key, value), fonction in _OSM_TAG_TO_FONCTION.items():
        tag_val = tags.get(key)
        if tag_val is None:
            continue
        if value == "*" or tag_val == value:
            return fonction
    return None


# ── Fetch + cache ────────────────────────────────────────────────────────────

def fetch_osm_pois(
    study_area: gpd.GeoDataFrame,
    cache_dir: "str | Path" = Path("data/processed"),
) -> gpd.GeoDataFrame:
    """Télécharge les POI stratégiques OSM dans la zone d'étude, avec cache.

    Returns:
        GeoDataFrame (EPSG:4326) avec colonnes osm_id, osm_type, fonction, name,
        geometry (points et polygones mélangés).
    """
    cache_dir = Path(cache_dir)
    bbox = tuple(study_area.to_crs(4326).total_bounds)
    h = _bbox_hash(bbox)
    cache_path = cache_dir / f"osm_pois_{h}.geojson"

    def _produce(tmp: Path) -> None:
        logger.info(
            "Telechargement POI OSM via Overpass -- bbox WGS84 : [%.4f, %.4f] -> [%.4f, %.4f]",
            bbox[0], bbox[1], bbox[2], bbox[3],
        )
        query = _build_overpass_query(bbox)
        result = _run_overpass(query)
        gdf = _parse_overpass_result(result)
        gdf.to_file(tmp, driver="GeoJSON")
        logger.info("POI OSM téléchargés : %d", len(gdf))

    ensure_cached(cache_path, produce=_produce, validate=valid_geofile, label=cache_path.name)

    gdf = gpd.read_file(cache_path)
    if gdf.empty:
        logger.info("Aucun POI stratégique OSM dans la zone")
    else:
        logger.info("POI OSM chargés : %s", gdf["fonction"].value_counts().to_dict())
    return gdf


# ── Appariement POI → footprint BD TOPO ──────────────────────────────────────

def match_pois_to_buildings(
    buildings: gpd.GeoDataFrame,
    pois: gpd.GeoDataFrame,
) -> "dict[str, str]":
    """Apparie les POI (points + polygones) aux footprints BD TOPO.

    Stratégie :
    - Polygone OSM → overlap avec le footprint (comme match_osm_to_bdtopo,
      seuil 30% car les POI sont souvent plus grands que le bâtiment).
    - Nœud OSM → point-in-polygon (le point tombe dans le footprint).

    En cas de conflit (plusieurs POI pour un même bâtiment), on garde le premier
    apparié (ordre du GeoDataFrame, donc l'ordre Overpass).

    Args:
        buildings: GeoDataFrame BD TOPO avec colonne `ID`, CRS projeté.
        pois:      GeoDataFrame de POI (points + polygones), n'importe quel CRS.

    Returns:
        dict ID bâtiment → fonction (str).
    """
    if pois is None or pois.empty or buildings.empty:
        return {}

    if "ID" not in buildings.columns:
        logger.warning("match_pois_to_buildings: colonne 'ID' absente des bâtiments")
        return {}

    pois_proj = pois.to_crs(buildings.crs) if pois.crs != buildings.crs else pois.copy()

    result: dict[str, str] = {}

    # Séparer polygones et points
    is_point = pois_proj.geometry.geom_type == "Point"
    poi_points = pois_proj[is_point]
    poi_polys = pois_proj[~is_point]

    # 1. Points → sjoin "within" (point dans le polygone bâtiment)
    if not poi_points.empty:
        joined = buildings[["ID", "geometry"]].sjoin(
            poi_points[["fonction", "geometry"]], how="inner", predicate="contains",
        )
        for _, row in joined.iterrows():
            bid = row["ID"]
            if bid not in result:
                result[bid] = row["fonction"]

    # 2. Polygones → sjoin "intersects" + seuil d'overlap
    if not poi_polys.empty:
        from shapely import intersection as _inter, area as _area

        # Reset index pour que index_right du sjoin soit positionnel
        poi_polys_r = poi_polys[["fonction", "geometry"]].reset_index(drop=True)
        candidates = buildings[["ID", "geometry"]].sjoin(
            poi_polys_r, how="inner", predicate="intersects",
        )
        if not candidates.empty:
            bld_geoms = buildings.set_index("ID").loc[candidates["ID"]].geometry.values
            poi_idx = candidates["index_right"].values
            poi_geoms = poi_polys_r.geometry.iloc[poi_idx].values
            inter_areas = _area(_inter(bld_geoms, poi_geoms))
            bld_areas = _area(bld_geoms)
            # Seuil : au moins 30% du bâtiment est couvert par le POI
            ratios = np.where(bld_areas > 0, inter_areas / bld_areas, 0.0)
            keep = ratios >= 0.3

            for i, (_, row) in enumerate(candidates.iterrows()):
                if keep[i]:
                    bid = row["ID"]
                    if bid not in result:
                        result[bid] = row["fonction"]

    logger.info("POI OSM appariés à %d bâtiments BD TOPO", len(result))
    return result


# ── Matching BPE éducation → footprint BD TOPO ──────────────────────────────

def match_education_to_buildings(
    buildings: gpd.GeoDataFrame,
    education: gpd.GeoDataFrame,
) -> "dict[str, str]":
    """Apparie les points BPE éducation aux footprints BD TOPO (point-in-polygon).

    Args:
        buildings: GeoDataFrame BD TOPO avec colonne `ID`.
        education: GeoDataFrame BPE (points Lambert-93), colonne `kind`.

    Returns:
        dict ID bâtiment → kind ("ecole", "college", "lycee", "creche").
    """
    if education is None or education.empty or buildings.empty:
        return {}

    edu = education.to_crs(buildings.crs) if education.crs != buildings.crs else education

    joined = buildings[["ID", "geometry"]].sjoin(
        edu[["kind", "geometry"]], how="inner", predicate="contains",
    )

    result: dict[str, str] = {}
    for _, row in joined.iterrows():
        bid = row["ID"]
        if bid not in result:
            result[bid] = row["kind"]

    logger.info("BPE éducation appariés à %d bâtiments BD TOPO", len(result))
    return result


# ── Merge des sources (OSM prioritaire, BDNB en fallback) ───────────────────

def merge_fonctions(
    *sources: "dict[str, str]",
) -> "dict[str, str]":
    """Fusionne plusieurs dicts ID→fonction, priorité au premier argument.

    Usage typique : merge_fonctions(osm_fonctions, education_fonctions, bdnb_fonctions)
    → OSM prime, puis éducation, puis BDNB en fallback.
    """
    # On applique les sources de la dernière à la première : la première source
    # de l'appel (OSM) écrase donc les suivantes (a le dernier mot).
    result: dict[str, str] = {}
    for source in reversed(sources):
        result.update(source)
    return result
