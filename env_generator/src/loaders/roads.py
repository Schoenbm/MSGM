"""Téléchargement du réseau routier OSM (piéton + voiture) pour une emprise.

Pour chaque type de réseau demandé (``walk``, ``drive``…), télécharge le graphe
OSM via osmnx dans l'emprise fournie, le convertit en arêtes (GeoDataFrame),
reprojette en Lambert-93 (EPSG:2154) et met en cache un GeoPackage local.

L'emprise attendue est celle de la zone *region* (voir iris.resolve_zone), de
préférence bufferisée pour récupérer le réseau de bordure.

osmnx est importé paresseusement (``_ox``) pour que le module reste importable
sans la dépendance, et pour faciliter les tests sans réseau.
"""
import hashlib
import json
import logging
from pathlib import Path

import geopandas as gpd
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .cache import ensure_cached, valid_geofile

logger = logging.getLogger(__name__)

TARGET_CRS = "EPSG:2154"        # Lambert-93, imposé (DT + standard France)
_DEFAULT_NETWORKS = ("walk", "drive")
_PROCESSED_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "processed"

# Colonnes OSM utiles à conserver dans les arêtes (si présentes).
_KEEP_COLS = ["geometry", "length", "highway", "name", "oneway", "reversed", "maxspeed", "lanes"]
# Colonnes pouvant contenir des listes OSM → à stringifier pour l'export.
_LIST_COLS = ["highway", "name", "oneway", "reversed", "maxspeed", "lanes"]


def _ox():
    """Import paresseux d'osmnx (dépendance lourde, optionnelle au chargement)."""
    try:
        import osmnx as ox
    except ImportError as e:  # pragma: no cover - dépend de l'environnement
        raise ImportError(
            "osmnx est requis pour le réseau routier : pip install osmnx"
        ) from e
    return ox


def _area_hash(poly_wgs84: BaseGeometry) -> str:
    """Hash court de la bbox WGS84 (arrondie à ~1 km) pour nommer le cache."""
    rounded = [round(v, 2) for v in poly_wgs84.bounds]
    return hashlib.md5(json.dumps(rounded).encode()).hexdigest()[:10]


def _to_wgs84_polygon(area: "BaseGeometry | gpd.GeoDataFrame") -> BaseGeometry:
    """Convertit l'emprise (géométrie Lambert-93 ou GeoDataFrame) en polygone WGS84."""
    if isinstance(area, gpd.GeoDataFrame):
        geom = unary_union(area.geometry.values)
        src_crs = area.crs or TARGET_CRS
    else:
        geom = area
        src_crs = TARGET_CRS
    return gpd.GeoSeries([geom], crs=src_crs).to_crs(4326).iloc[0]


def _cache_path(cache_dir: Path, network_type: str, h: str) -> Path:
    return cache_dir / f"roads_{network_type}_{h}.gpkg"


def fetch_road_network(
    area: "BaseGeometry | gpd.GeoDataFrame",
    network_types: "tuple[str, ...]" = _DEFAULT_NETWORKS,
    cache_dir: "str | Path" = _PROCESSED_DIR,
    simplify: bool = True,
) -> "dict[str, gpd.GeoDataFrame]":
    """Télécharge le réseau routier OSM pour chaque type demandé.

    Args:
        area:          emprise (géométrie Lambert-93 ou GeoDataFrame). Convertie
                       en WGS84 pour la requête osmnx.
        network_types: types de réseau osmnx (``walk``, ``drive``, ``bike``…).
        cache_dir:     répertoire de cache (un GeoPackage par type).
        simplify:      simplification topologique osmnx (recommandé).

    Returns:
        dict {network_type: GeoDataFrame d'arêtes en Lambert-93}.
    """
    cache_dir = Path(cache_dir)
    poly = _to_wgs84_polygon(area)
    h = _area_hash(poly)
    out: "dict[str, gpd.GeoDataFrame]" = {}
    for nt in network_types:
        out[nt] = _fetch_one(poly, nt, cache_dir, h, simplify)
    return out


def _fetch_one(
    poly_wgs84: BaseGeometry,
    network_type: str,
    cache_dir: Path,
    h: str,
    simplify: bool,
) -> gpd.GeoDataFrame:
    cache_path = _cache_path(cache_dir, network_type, h)

    def _produce(tmp: Path) -> None:
        ox = _ox()
        logger.info("Téléchargement OSM réseau '%s'...", network_type)
        graph = ox.graph_from_polygon(poly_wgs84, network_type=network_type, simplify=simplify)
        edges = ox.graph_to_gdfs(graph, nodes=False)

        edges = edges.to_crs(TARGET_CRS)
        keep = [c for c in _KEEP_COLS if c in edges.columns]
        edges = edges[keep].copy()
        for col in _LIST_COLS:
            if col in edges.columns:
                edges[col] = edges[col].astype(str)
        edges = edges.reset_index(drop=True)
        edges.to_file(tmp, driver="GPKG")
        logger.info("Réseau '%s' : %d arêtes -> %s", network_type, len(edges), cache_path)

    # Pipeline de cache unique : réutilise le GeoPackage s'il est lisible, sinon
    # (re)télécharge atomiquement (un .gpkg corrompu par une écriture interrompue
    # est détecté par valid_geofile et régénéré).
    ensure_cached(
        cache_path,
        produce=_produce,
        validate=valid_geofile,
        label=f"routes {network_type} ({cache_path.name})",
    )
    return gpd.read_file(cache_path)
