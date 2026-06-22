import logging
from pathlib import Path

import geopandas as gpd
import pandas as pd

logger = logging.getLogger(__name__)

# Columns kept in the lightweight export
_LIGHT_COLS = ["ID", "geometry", "residentiel", "population_allouee"]

# Columns dropped from the export (internal pipeline artifacts)
_DROP_COLS = ["cell_idx"]


# Shapefile column names are limited to 10 characters — map long names to short ones.
_SHP_RENAME: dict[str, str] = {
    "population_allouee": "pop_allou",
    "menages_alloues": "men_allou",
}


def merge_buildings(
    all_buildings: gpd.GeoDataFrame,
    result: gpd.GeoDataFrame,
) -> gpd.GeoDataFrame:
    """Fusionne tous les bâtiments de la région avec l'allocation de population.

    Produit **une seule** couche bâtiment : tous les bâtiments (`all_buildings`,
    résidentiels + non-résidentiels) enrichis des colonnes d'allocation issues de
    `result` (population, ménages, CSP, âge), jointes sur `ID`. Les non-logements
    reçoivent `population = 0` et `residentiel = False`.

    Remplace l'ancien couple `buildings_full` (résidentiels seuls) / `buildings_all`
    (tous, sans population), dont la distinction prêtait à confusion.

    Args:
        all_buildings: tous les bâtiments de la région (géométrie + attributs +
                       `usage_bdnb`), colonne `ID`.
        result:        bâtiments résidentiels avec population allouée, colonne `ID`.

    Returns:
        GeoDataFrame de tous les bâtiments + colonnes d'allocation + `residentiel`.
    """
    res_ids = set(result["ID"])
    demo = [c for c in result.columns if c.startswith("csp_") or c.startswith("age_")]
    alloc_extra = ["population_allouee", "menages_alloues",
                   "NB_LOGTS", "NB_LOGTS_source", "NB_LOGTS_ESTIME"]
    alloc_cols = [c for c in alloc_extra + demo if c in result.columns]

    base = all_buildings.copy()
    # La NB_LOGTS estimée (result) prime sur la valeur brute BD TOPO (base).
    if "NB_LOGTS" in alloc_cols and "NB_LOGTS" in base.columns:
        base = base.drop(columns=["NB_LOGTS"])

    merged = base.merge(result[["ID"] + alloc_cols], on="ID", how="left")
    merged["residentiel"] = merged["ID"].isin(res_ids)

    for c in ["population_allouee", "menages_alloues", *demo]:
        if c in merged.columns:
            merged[c] = merged[c].fillna(0)
    return merged


def export_buildings(buildings: gpd.GeoDataFrame, output_dir: str | Path) -> None:
    """Exporte la couche bâtiment fusionnée (GeoJSON + CSV + Shapefile).

    Produit dans output_dir :
    - buildings.{geojson,csv,shp}        : tous les bâtiments, toutes colonnes
      (population_allouee = 0 si non-logement, flag `residentiel`, `usage_bdnb`…)
    - buildings_light.{geojson,csv,shp}  : ID + géométrie + residentiel +
      population_allouee (+ CSP) — vue allégée.

    Args:
        buildings:  GeoDataFrame issu de merge_buildings.
        output_dir: répertoire de sortie (créé si absent).
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    full = buildings.drop(columns=[c for c in _DROP_COLS if c in buildings.columns])
    full = _sanitize_for_export(full)
    _write_geojson(full, output_dir / "buildings.geojson")
    _write_csv(full.drop(columns=["geometry"]), output_dir / "buildings.csv")
    _write_shp(full, output_dir / "buildings.shp")

    csp_cols = [c for c in buildings.columns if c.startswith("csp_")]
    light_cols = [c for c in _LIGHT_COLS if c in buildings.columns] + csp_cols
    light = buildings[light_cols].copy()
    _write_geojson(light, output_dir / "buildings_light.geojson")
    _write_csv(light.drop(columns=["geometry"]), output_dir / "buildings_light.csv")
    _write_shp(light, output_dir / "buildings_light.shp")

    logger.info(
        "Export bâtiments dans %s  (%d bâtiments, %d colonnes ; dont %d logements)",
        output_dir, len(full), len(full.columns), int(buildings["residentiel"].sum()),
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
