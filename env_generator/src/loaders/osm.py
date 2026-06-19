"""Téléchargement et mise en cache des bâtiments OSM, appariement avec BD_TOPO."""
import hashlib
import json
import logging
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from .cache import ensure_cached, valid_geofile

logger = logging.getLogger(__name__)

OVERLAP_THRESHOLD: float = 0.5  # fraction de la surface OSM devant chevaucher le footprint BD_TOPO


# ── Cache ──────────────────────────────────────────────────────────────────────

def _bbox_hash(bbox: tuple[float, float, float, float]) -> str:
    """Hash court d'une bbox WGS84 arrondie à ~1 km (0.01°) pour nommer le cache."""
    rounded = [round(v, 2) for v in bbox]
    return hashlib.md5(json.dumps(rounded).encode()).hexdigest()[:10]


def fetch_osm_buildings(
    study_area: gpd.GeoDataFrame,
    cache_dir: str | Path = Path("data/processed"),
) -> gpd.GeoDataFrame:
    """Télécharge les bâtiments OSM dans la zone d'étude, avec cache GeoJSON local.

    Colonnes du GeoDataFrame retourné (CRS = EPSG:4326) :
      - osm_id          : identifiant OSM (int)
      - building        : valeur du tag ``building``
      - building_flats  : int ou NaN  (tag ``building:flats``)
      - building_levels : float ou NaN (tag ``building:levels``)
      - geometry        : polygone

    Le cache est nommé d'après la bbox WGS84 arrondie à 0.01°.
    Pour forcer un re-téléchargement, supprimer le fichier ``osm_buildings_<hash>.geojson``
    dans ``data/processed/``.
    """
    cache_dir = Path(cache_dir)
    bbox = tuple(study_area.to_crs(4326).total_bounds)  # (minx, miny, maxx, maxy)
    h = _bbox_hash(bbox)
    cache_path = cache_dir / f"osm_buildings_{h}.geojson"

    def _produce(tmp: Path) -> None:
        logger.info(
            "Telechargement OSM via Overpass -- bbox WGS84 : [%.4f, %.4f] -> [%.4f, %.4f]",
            bbox[0], bbox[1], bbox[2], bbox[3],
        )
        gdf = _query_overpass(bbox)
        gdf.to_file(tmp, driver="GeoJSON")
        logger.info("Bâtiments OSM téléchargés : %d", len(gdf))

    # Pipeline de cache unique : réutilise le GeoJSON s'il est lisible, sinon
    # (re)télécharge atomiquement (un cache corrompu est détecté et régénéré).
    ensure_cached(cache_path, produce=_produce, validate=valid_geofile, label=cache_path.name)

    gdf = gpd.read_file(cache_path)
    n_flats = gdf["building_flats"].notna().sum() if "building_flats" in gdf.columns else 0
    n_levels = gdf["building_levels"].notna().sum() if "building_levels" in gdf.columns else 0
    logger.info(
        "%d bâtiments OSM chargés (building:flats=%d, building:levels=%d)",
        len(gdf), n_flats, n_levels,
    )
    return gdf


# ── Équipements éducatifs (crèches, écoles) ─────────────────────────────────────

# Tags OSM des équipements éducatifs scolarisant des mineurs (≤ 17 ans).
# NB : l'enseignement supérieur (amenity=college/university) est volontairement
# exclu — nos agents scolarisés ont 3-17 ans, personne n'y va.
_EDU_CRECHE_TAGS = frozenset({"kindergarten"})    # crèche / maternelle
_EDU_SCHOOL_TAGS = frozenset({"school"})          # école / collège / lycée (indistincts dans OSM)

# Mots-clés (noms d'établissement français) pour départager les niveaux, faute de
# tag fiable dans OSM. amenity=school couvre primaire ET secondaire.
_EDU_LYCEE_KEYWORDS = ("lycee", "lycée")
_EDU_COLLEGE_KEYWORDS = ("college", "collège")


def fetch_osm_education(
    study_area: gpd.GeoDataFrame,
    cache_dir: str | Path = Path("data/processed"),
) -> gpd.GeoDataFrame:
    """Télécharge les équipements éducatifs OSM (crèches, écoles) de la zone.

    Requête Overpass distincte de ``fetch_osm_buildings`` : ici on cible les tags
    ``amenity``/``building`` éducatifs (souvent absents du jeu bâti filtré sur
    building:flats/levels). Chaque équipement est ramené à un **point** (le nœud,
    ou le centre d'une emprise via ``out center``).

    Colonnes (CRS = EPSG:4326) :
      - osm_id   : identifiant OSM (str, préfixé n/w pour éviter les collisions)
      - kind     : "creche", "ecole", "college" ou "lycee"
      - geometry : Point

    Cache GeoJSON nommé d'après la bbox WGS84 arrondie. Le suffixe ``v2`` reflète
    le schéma de classification (4 niveaux) : changer la logique de classement
    impose de bumper ce suffixe pour invalider les anciens caches.
    """
    cache_dir = Path(cache_dir)
    bbox = tuple(study_area.to_crs(4326).total_bounds)
    h = _bbox_hash(bbox)
    cache_path = cache_dir / f"osm_education_v2_{h}.geojson"

    def _produce(tmp: Path) -> None:
        logger.info("Téléchargement OSM équipements éducatifs -- bbox %s", [round(v, 4) for v in bbox])
        gdf = _query_overpass_education(bbox)
        gdf.to_file(tmp, driver="GeoJSON")
        logger.info("Équipements éducatifs OSM téléchargés : %d", len(gdf))

    ensure_cached(cache_path, produce=_produce, validate=valid_geofile, label=cache_path.name)

    gdf = gpd.read_file(cache_path)
    if "kind" in gdf.columns:
        logger.info("Équipements éducatifs OSM : %s",
                    gdf["kind"].value_counts().to_dict())
    return gdf


def _classify_education(tags: dict) -> "str | None":
    """Classe un équipement en 'creche', 'ecole', 'college', 'lycee' ou None.

    crèche : amenity/building = kindergarten.
    écoles/collèges/lycées : amenity/building = school, départagés par le nom de
    l'établissement (« Lycée … », « Collège … ») puis par ``isced:level``
    (2 = collège, 3 = lycée). À défaut → 'ecole' (primaire, cas le plus fréquent).
    """
    amenity = str(tags.get("amenity", "")).lower()
    building = str(tags.get("building", "")).lower()

    if amenity in _EDU_CRECHE_TAGS or building in _EDU_CRECHE_TAGS:
        return "creche"
    if amenity not in _EDU_SCHOOL_TAGS and building not in _EDU_SCHOOL_TAGS:
        return None  # supérieur (college/university) ou non éducatif → ignoré

    name = str(tags.get("name", "")).lower()
    if any(k in name for k in _EDU_LYCEE_KEYWORDS):
        return "lycee"
    if any(k in name for k in _EDU_COLLEGE_KEYWORDS):
        return "college"

    isced = str(tags.get("isced:level", ""))
    isced_levels = set(isced.replace("-", ";").replace(",", ";").split(";"))
    if "3" in isced_levels:
        return "lycee"
    if "2" in isced_levels:
        return "college"
    return "ecole"


def _query_overpass_education(bbox: tuple[float, float, float, float]) -> gpd.GeoDataFrame:
    """Interroge Overpass pour les équipements éducatifs (nœuds + emprises)."""
    try:
        import overpy
    except ImportError:
        raise ImportError("Le module overpy est requis : pip install overpy")

    from shapely.geometry import Point

    minx, miny, maxx, maxy = bbox
    amenity_re = "school|kindergarten"
    building_re = "school|kindergarten"
    query = f"""
[out:json][timeout:180];
(
  node["amenity"~"{amenity_re}"]({miny},{minx},{maxy},{maxx});
  way["amenity"~"{amenity_re}"]({miny},{minx},{maxy},{maxx});
  node["building"~"{building_re}"]({miny},{minx},{maxy},{maxx});
  way["building"~"{building_re}"]({miny},{minx},{maxy},{maxx});
);
out center;
"""
    result = _run_overpass(query)

    rows = []
    for node in result.nodes:
        kind = _classify_education(node.tags)
        if kind is None:
            continue
        rows.append({"osm_id": f"n{node.id}", "kind": kind,
                     "geometry": Point(float(node.lon), float(node.lat))})
    for way in result.ways:
        kind = _classify_education(way.tags)
        if kind is None:
            continue
        lat = getattr(way, "center_lat", None)
        lon = getattr(way, "center_lon", None)
        if lat is None or lon is None:
            continue
        rows.append({"osm_id": f"w{way.id}", "kind": kind,
                     "geometry": Point(float(lon), float(lat))})

    if not rows:
        logger.warning("Aucun équipement éducatif OSM trouvé dans la zone")
        return gpd.GeoDataFrame(columns=["osm_id", "kind", "geometry"], crs=4326)
    return gpd.GeoDataFrame(rows, crs=4326)


# ── Requête Overpass ───────────────────────────────────────────────────────────

_OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.karte.io/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]


def _run_overpass(query: str):
    """Exécute une requête Overpass, en basculant sur 3 endpoints si surcharge."""
    try:
        import overpy
    except ImportError:
        raise ImportError("Le module overpy est requis : pip install overpy")

    last_exc: Exception = RuntimeError("Aucun endpoint Overpass disponible")
    for endpoint in _OVERPASS_ENDPOINTS:
        try:
            api = overpy.Overpass(url=endpoint)
            logger.debug("Tentative Overpass : %s", endpoint)
            result = api.query(query)
            logger.debug("Succes via %s", endpoint)
            return result
        except Exception as exc:
            logger.warning("Endpoint %s indisponible (%s), essai suivant...", endpoint, type(exc).__name__)
            last_exc = exc
    raise RuntimeError(
        "Tous les serveurs Overpass sont surchargés. Réessayez dans quelques minutes."
    ) from last_exc


def _query_overpass(bbox: tuple[float, float, float, float]) -> gpd.GeoDataFrame:
    """Interroge l'API Overpass pour tous les polygones bâtiments dans la bbox.

    Tente jusqu'à 3 endpoints en cas de surcharge serveur (OverpassGatewayTimeout).
    """
    minx, miny, maxx, maxy = bbox
    # On ne récupère que les bâtiments portant building:flats ou building:levels
    # pour maintenir la taille de la réponse raisonnable sur de grandes zones.
    # Les bâtiments sans ces tags seront estimés via BD_TOPO (NB_ETAGES / HAUTEUR).
    query = f"""
[out:json][timeout:180];
(
  way["building"]["building:flats"]({miny},{minx},{maxy},{maxx});
  way["building"]["building:levels"]({miny},{minx},{maxy},{maxx});
);
out body;
>;
out skel qt;
"""
    result = _run_overpass(query)

    from shapely.geometry import Polygon

    rows = []
    for way in result.ways:
        tags = way.tags
        try:
            coords = [(float(n.lon), float(n.lat)) for n in way.nodes]
            if len(coords) < 4:  # polygone = au moins 3 points + fermeture
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
            "building": tags.get("building", "yes"),
            "building_flats": _to_int(tags.get("building:flats")),
            "building_levels": _to_float(tags.get("building:levels")),
            "geometry": geom,
        })

    if not rows:
        logger.warning("Aucun bâtiment OSM trouvé dans la zone")
        return gpd.GeoDataFrame(
            columns=["osm_id", "building", "building_flats", "building_levels", "geometry"],
            crs=4326,
        )

    gdf = gpd.GeoDataFrame(rows, crs=4326)
    n_flats = gdf["building_flats"].notna().sum()
    n_levels = gdf["building_levels"].notna().sum()
    logger.info(
        "%d bâtiments OSM téléchargés — building:flats : %d, building:levels : %d",
        len(gdf), n_flats, n_levels,
    )
    return gdf


# ── Appariement OSM ↔ BD_TOPO ─────────────────────────────────────────────────

def match_osm_to_bdtopo(
    buildings: gpd.GeoDataFrame,
    osm: gpd.GeoDataFrame,
    overlap_threshold: float = OVERLAP_THRESHOLD,
) -> gpd.GeoDataFrame:
    """Associe les données OSM aux bâtiments BD_TOPO par chevauchement spatial.

    Règle de sélection : un polygone OSM est retenu pour un bâtiment BD_TOPO
    si et seulement si au moins ``overlap_threshold`` de sa propre surface est
    contenu dans le footprint BD_TOPO. Cela évite le double-comptage de
    bâtiments OSM adjacents qui ne touchent qu'à la bordure.

    Colonnes ajoutées à ``buildings`` :
      - ``osm_building`` : str ou NaN  — tag ``building`` le plus fréquent parmi les polygones OSM retenus
      - ``osm_flats``    : int ou NaN  — somme des ``building:flats`` des polygones OSM retenus
      - ``osm_levels``   : float ou NaN — max des ``building:levels`` des polygones OSM retenus
    """
    buildings = buildings.copy()
    buildings["osm_building"] = np.nan
    buildings["osm_building"] = buildings["osm_building"].astype(object)
    buildings["osm_flats"] = np.nan
    buildings["osm_levels"] = np.nan

    if osm is None or osm.empty:
        logger.info("Appariement OSM ignoré (données OSM vides)")
        return buildings

    if "building" not in osm.columns:
        osm = osm.copy()
        osm["building"] = np.nan

    if osm.crs != buildings.crs:
        osm = osm.to_crs(buildings.crs)

    # Travailler avec des index positionnels 0..n pour éviter toute confusion d'index pandas
    bld = buildings[["geometry"]].copy()
    bld.index = range(len(bld))

    osm_w = osm[["building", "building_flats", "building_levels", "geometry"]].copy()
    osm_w.index = range(len(osm_w))

    # Étape 1 : paires candidates via sjoin (intersects)
    candidates = bld.sjoin(osm_w[["geometry"]], how="inner", predicate="intersects")
    # candidates.index         = position dans bld (0..n-1)
    # candidates["index_right"] = position dans osm_w (0..m-1)

    if candidates.empty:
        logger.info("Appariement OSM : aucune paire candidate trouvée")
        return buildings

    bld_pos = candidates.index.values            # array de positions BD_TOPO
    osm_pos = candidates["index_right"].values   # array de positions OSM

    # Étape 2 : ratio chevauchement = intersection_area / osm_area  (shapely 2.x vectorisé)
    from shapely import intersection as _inter, area as _area

    bld_geoms = bld.geometry.iloc[bld_pos].values
    osm_geoms = osm_w.geometry.iloc[osm_pos].values
    inter_areas = _area(_inter(bld_geoms, osm_geoms))
    osm_areas = _area(osm_geoms)
    ratios = np.where(osm_areas > 0, inter_areas / osm_areas, 0.0)

    # Étape 3 : filtrer par seuil
    keep = ratios >= overlap_threshold
    if not keep.any():
        logger.info(
            "Appariement OSM : aucun chevauchement >= %.0f%% — osm_flats/osm_levels = NaN pour tous",
            overlap_threshold * 100,
        )
        return buildings

    keep_idx = osm_pos[keep]
    df = pd.DataFrame({
        "_bld_pos": bld_pos[keep],
        "building": osm_w["building"].iloc[keep_idx].values,
        "building_flats": osm_w["building_flats"].iloc[keep_idx].values,
        "building_levels": osm_w["building_levels"].iloc[keep_idx].values,
    })

    # Étape 4 : agrégation par bâtiment BD_TOPO
    agg = df.groupby("_bld_pos").agg(
        osm_building=("building", _mode_not_null),
        osm_flats=("building_flats", _sum_not_null),
        osm_levels=("building_levels", _max_not_null),
    )

    # Étape 5 : remettre sur l'index original de buildings
    orig_index = buildings.index
    matched_orig = orig_index[agg.index.values]
    buildings.loc[matched_orig, "osm_building"] = agg["osm_building"].values
    buildings.loc[matched_orig, "osm_flats"] = agg["osm_flats"].values
    buildings.loc[matched_orig, "osm_levels"] = agg["osm_levels"].values

    n_building = buildings["osm_building"].notna().sum()
    n_flats = buildings["osm_flats"].notna().sum()
    n_levels = buildings["osm_levels"].notna().sum()
    logger.info(
        "Appariement OSM (seuil %.0f%%) : %d avec osm_building, %d avec osm_flats, %d avec osm_levels",
        overlap_threshold * 100, n_building, n_flats, n_levels,
    )
    return buildings


# ── Helpers ────────────────────────────────────────────────────────────────────

def _mode_not_null(s: pd.Series) -> "str | float":
    valid = s.dropna()
    return str(valid.mode().iloc[0]) if not valid.empty else np.nan


def _sum_not_null(s: pd.Series) -> "int | float":
    valid = s.dropna()
    return int(valid.sum()) if not valid.empty else np.nan


def _max_not_null(s: pd.Series) -> "float":
    valid = s.dropna()
    return float(valid.max()) if not valid.empty else np.nan


def _to_int(val: object) -> "int | None":
    try:
        return int(float(val))  # float() d'abord pour accepter "3.0"
    except (TypeError, ValueError):
        return None


def _to_float(val: object) -> "float | None":
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
