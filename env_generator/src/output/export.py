import logging
from pathlib import Path

import geopandas as gpd
import pandas as pd

logger = logging.getLogger(__name__)

# Columns kept in the lightweight export
_LIGHT_COLS = ["ID", "geometry", "population_allouee"]

# Columns dropped from the complete export (internal pipeline artifacts)
_DROP_COLS = ["cell_idx"]


# Shapefile column names are limited to 10 characters — map long names to short ones.
_SHP_RENAME: dict[str, str] = {
    "population_allouee": "pop_allou",
}


def export_results(result: gpd.GeoDataFrame, output_dir: str | Path) -> None:
    """Export allocation results to GeoJSON, CSV, and Shapefile.

    Produces six files in output_dir:
    - buildings_light.geojson  : ID + geometry + population_allouee
    - buildings_light.csv      : ID + population_allouee (no geometry)
    - buildings_full.geojson   : all columns
    - buildings_full.csv       : all columns except geometry
    - buildings_light.shp (+sidecar files) : ID + geometry + pop_allou
    - buildings_full.shp  (+sidecar files) : all columns, noms tronqués si > 10 chars

    Args:
        result: GeoDataFrame with population_allouee column.
        output_dir: Directory where output files are written (created if absent).
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # --- Lightweight ---
    csp_cols = [c for c in result.columns if c.startswith("csp_")]
    light_cols = _LIGHT_COLS + csp_cols
    light = result[light_cols].copy()
    _write_geojson(light, output_dir / "buildings_light.geojson")
    _write_csv(light.drop(columns=["geometry"]), output_dir / "buildings_light.csv")
    _write_shp(light, output_dir / "buildings_light.shp")

    # --- Complete ---
    full = result.drop(columns=[c for c in _DROP_COLS if c in result.columns])
    full = _sanitize_for_export(full)
    _write_geojson(full, output_dir / "buildings_full.geojson")
    _write_csv(full.drop(columns=["geometry"]), output_dir / "buildings_full.csv")
    _write_shp(full, output_dir / "buildings_full.shp")

    logger.info(
        "Export terminé dans %s  (light: %d lignes, full: %d colonnes)",
        output_dir,
        len(light),
        len(full.columns),
    )


def _write_geojson(gdf: gpd.GeoDataFrame, path: Path) -> None:
    gdf_wgs84 = gdf.to_crs(epsg=4326)
    gdf_wgs84.to_file(path, driver="GeoJSON")
    logger.debug("GeoJSON écrit : %s", path)


def _write_csv(df: pd.DataFrame, path: Path) -> None:
    df.to_csv(path, index=False)
    logger.debug("CSV écrit : %s", path)


def _write_shp(gdf: gpd.GeoDataFrame, path: Path) -> None:
    # Shapefile column names are limited to 10 characters.
    rename = {col: _SHP_RENAME.get(col, col[:10]) for col in gdf.columns if col != "geometry"}
    gdf_shp = gdf.rename(columns=rename)
    gdf_shp.to_file(path, driver="ESRI Shapefile")
    logger.debug("Shapefile écrit : %s", path)


def export_all_buildings(buildings: gpd.GeoDataFrame, output_dir: str | Path) -> None:
    """Export all buildings (residential + non-residential) as Shapefile and GeoJSON.

    Produces two files in output_dir:
    - buildings_all.shp (+sidecar files) : tous les bâtiments de la zone d'étude
    - buildings_all.geojson              : idem en GeoJSON

    Args:
        buildings: GeoDataFrame de tous les bâtiments (issu de buildings_all.gpkg).
        output_dir: Répertoire de sortie (créé si absent).
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    buildings = _sanitize_for_export(buildings)
    _write_shp(buildings, output_dir / "buildings_all.shp")
    _write_geojson(buildings, output_dir / "buildings_all.geojson")

    logger.info(
        "Export tous bâtiments : %d bâtiments -> %s",
        len(buildings),
        output_dir,
    )


def export_agents(agents: gpd.GeoDataFrame, output_dir: str | Path) -> None:
    """Export la population d'agents (domicile + âge + CSP + lieu de travail).

    Produit dans output_dir :
    - agents.geojson : points domicile + attributs (age, csp, activity, dest_id, ...)
    - agents.csv     : mêmes attributs sans géométrie (dest_x/dest_y conservés)

    Args:
        agents:     GeoDataFrame issu de generate_agents.
        output_dir: répertoire de sortie (créé si absent).
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if agents.empty:
        logger.warning("Aucun agent à exporter — fichiers agents non écrits")
        return

    agents = _sanitize_for_export(agents)
    _write_geojson(agents, output_dir / "agents.geojson")
    _write_csv(agents.drop(columns=["geometry"]), output_dir / "agents.csv")
    logger.info("Export agents : %d lignes -> %s", len(agents), output_dir)


def _sanitize_for_export(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Convert problematic dtypes (datetime, object with mixed types) for GeoJSON."""
    result = gdf.copy()
    for col in result.columns:
        if col == "geometry":
            continue
        if pd.api.types.is_datetime64_any_dtype(result[col]):
            result[col] = result[col].astype(str)
    return result
