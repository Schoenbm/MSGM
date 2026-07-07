import logging

import geopandas as gpd
import pandas as pd

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
    demo_cols = [c for c in insee.columns if c.startswith("csp_") or c.startswith("age_")]
    insee_cols = ["Ind_total", "geometry"] + demo_cols
    # Colonnes de cellule forwardées telles quelles (PAS allouées par bâtiment) :
    # ménages + composition des ménages (men_* = HH_TYPE_COLS de households.py,
    # cibles IPU de niveau ménage lues par IRIS dans generate_household_agents).
    for col in ("P22_MEN", "taille_moy_menage", "men_seul", "men_sans_fam",
                "men_couple_senf", "men_couple_aenf", "men_mono"):
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

    # 7. L'IRIS d'allocation fait foi : la population et les marges d'un bâtiment
    # viennent de sa cellule de jointure, pas de l'attribut code_iris du shapefile
    # BD TOPO (lacunaire : mesuré 23/552 bâtiments peuplés sans code sur la zone
    # test, 0 désaccord quand les deux existent). On réécrit `code_iris` depuis la
    # cellule (str 9 caractères) ; hors grille, l'attribut d'origine est conservé
    # au même format. Consommé par generate_household_agents (marges par IRIS) et
    # casualties.py (agrégation).
    if "CODE_IRIS" in insee.columns:
        cell_code = joined["cell_idx"].map(insee["CODE_IRIS"].astype(str))
        if "code_iris" in joined.columns:
            orig = joined["code_iris"].map(
                lambda x: str(int(float(x))).zfill(9) if pd.notna(x) else pd.NA
            )
        else:
            orig = pd.Series(pd.NA, index=joined.index, dtype="object")
        joined["code_iris"] = cell_code.fillna(orig)

    return joined
