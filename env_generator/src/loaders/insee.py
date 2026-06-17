import logging
from pathlib import Path

import geopandas as gpd

logger = logging.getLogger(__name__)

POP_FALLBACK_COLS: list[str] = [
    "Ind_0_3", "Ind_4_5", "Ind_6_10", "Ind_11_17", "Ind_18_24",
    "Ind_25_39", "Ind_40_54", "Ind_55_64", "Ind_65_79", "Ind_80p", "Ind_inc",
]


def compute_ind_total(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if "Ind" in gdf.columns and not gdf["Ind"].isna().all():
        gdf["Ind_total"] = gdf["Ind"]
    else:
        fallback_cols = [c for c in gdf.columns if c.startswith("Ind_") or c in POP_FALLBACK_COLS]
        gdf["Ind_total"] = gdf[fallback_cols].sum(axis=1)
        logger.warning("Colonne Ind absente — somme des colonnes %s", fallback_cols)

    zero_count = (gdf["Ind_total"] == 0).sum()
    if zero_count > 0:
        logger.warning("%d carreaux avec Ind_total == 0", zero_count)

    return gdf


def load_insee(path: str | Path) -> gpd.GeoDataFrame:
    path = Path(path)
    logger.info("Chargement des carreaux INSEE depuis %s", path)

    gdf = gpd.read_file(path)

    logger.info("CRS : %s", gdf.crs)
    logger.info("%d carreaux chargés", len(gdf))

    gdf = compute_ind_total(gdf)

    logger.info("Ind_total — min=%.1f  max=%.1f  mean=%.1f  total=%.0f",
                gdf["Ind_total"].min(), gdf["Ind_total"].max(),
                gdf["Ind_total"].mean(), gdf["Ind_total"].sum())

    return gdf
