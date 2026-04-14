import logging

import geopandas as gpd

logger = logging.getLogger(__name__)


def join_buildings_to_insee(
    buildings: gpd.GeoDataFrame, insee: gpd.GeoDataFrame
) -> gpd.GeoDataFrame:
    """Join residential buildings to INSEE grid cells via centroid spatial join.

    Args:
        buildings: Residential buildings GeoDataFrame with NB_LOGTS and polygon geometry.
        insee: INSEE grid GeoDataFrame with Ind_total and polygon geometry.

    Returns:
        GeoDataFrame with buildings enriched with INSEE columns (Ind_total).
        Buildings outside any grid cell have Ind_total = NaN.
    """
    # 1. Align CRS if necessary
    if buildings.crs != insee.crs:
        logger.warning(
            "Bâtiments reprojetés de %s vers %s", buildings.crs, insee.crs
        )
        buildings = buildings.to_crs(insee.crs)

    # 2. GeoDataFrame des centroïdes (geometry originale remplacée temporairement)
    centroids = buildings.copy()
    centroids["geometry"] = buildings.geometry.centroid

    # 3. Jointure spatiale : centroïde dans carreau
    insee_cols = ["Ind_total", "geometry"]
    for col in ("P22_MEN", "taille_moy_menage"):
        if col in insee.columns:
            insee_cols.append(col)

    joined = gpd.sjoin(
        centroids,
        insee[insee_cols],
        how="left",
        predicate="within",
    )

    # 4. Restaurer la geometry polygone d'origine
    joined["geometry"] = buildings.geometry.values

    # 5. Log et avertissements
    n_unmatched = joined["index_right"].isna().sum()
    if n_unmatched > 0:
        logger.warning(
            "%d bâtiments hors carreau (centroïde hors grille)", n_unmatched
        )
    logger.info(
        "%d bâtiments joinés sur %d carreaux distincts",
        joined["index_right"].notna().sum(),
        joined["index_right"].nunique(),
    )

    # 6. Renommer l'index de jointure en cell_idx (utilisé par l'allocateur)
    joined = joined.rename(columns={"index_right": "cell_idx"})

    return joined
