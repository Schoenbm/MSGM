import logging
from pathlib import Path

import geopandas as gpd
import pandas as pd

from src.loaders.buildings import _compute_nb_etages
from src.matching.workplaces import DEFAULT_WORKPLACE_USAGES

logger = logging.getLogger(__name__)

# Columns dropped from the export (internal pipeline artifacts)
_DROP_COLS = ["cell_idx"]


# Shapefile column names are limited to 10 characters — map long names to short ones.
_SHP_RENAME: dict[str, str] = {
    "population_allouee": "pop_allou",
    "menages_alloues": "men_allou",
    "annee_construction": "annee_con",
    "is_residential": "is_resid",
    "is_workplace": "is_workpl",
    "is_strategic": "is_strat",
    "is_education": "is_educ",
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
    """Exporte la couche bâtiment complète (GeoJSON + CSV + Shapefile).

    Superset d'inspection (QGIS) et source de population pour `casualties.py` :
    tous les bâtiments, toutes colonnes (population_allouee = 0 si non-logement,
    flag `residentiel`, `usage_bdnb`, CSP/âge, matériaux/période BDNB…).

    Le **contrat de simulation** (curaté, sans population) est produit séparément
    par `export_env`.

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

    logger.info(
        "Export bâtiments dans %s  (%d bâtiments, %d colonnes ; dont %d logements)",
        output_dir, len(full), len(full.columns), int(buildings["residentiel"].sum()),
    )


def build_env_layer(
    buildings: gpd.GeoDataFrame,
    workplace_usages: "tuple[str, ...] | list[str]" = DEFAULT_WORKPLACE_USAGES,
    fonctions: "dict[str, str] | None" = None,
) -> gpd.GeoDataFrame:
    """Compose le **contrat d'environnement** : faits physiques du bâtiment, sans
    population ni dépendance à la crise.

    Colonnes : ID, geometry, flags de rôle (`is_residential`, `is_workplace`,
    `is_education`, `is_strategic`), `fonction` (str détaillé : hopital, mairie…),
    profil vertical (`n_etages`, `hauteur`, `emprise_m2`, `z_min_sol`, `z_max_toit`
    → capacité de refuge calculée en aval selon la cote d'inondation), vulnérabilité
    (`mat_mur`, `mat_toit`, `annee_construction` — BDNB ffo, consommés par le NN
    D1-D5).

    Args:
        buildings:        GeoDataFrame des bâtiments (merge_buildings ou all_buildings).
        workplace_usages: usages BD TOPO considérés comme lieux de travail.
        fonctions:        dict ID → fonction (str) issu de merge_fonctions (poi.py).
    """
    from src.loaders.poi import EDUCATION_FONCTIONS, STRATEGIC_FONCTIONS

    b = buildings
    idx = b.index
    usage_usages = tuple(workplace_usages)
    u1 = b.get("USAGE1", pd.Series(index=idx, dtype="object"))
    u2 = b.get("USAGE2", pd.Series(index=idx, dtype="object"))
    usage_bdnb = b.get("usage_bdnb", pd.Series(index=idx, dtype="object"))

    env = gpd.GeoDataFrame({"ID": b["ID"].values}, geometry=b.geometry.values, crs=b.crs)
    env["is_residential"] = (
        b["residentiel"].astype(bool).values if "residentiel" in b.columns else False
    )
    env["is_workplace"] = (
        u1.isin(usage_usages) | u2.isin(usage_usages) | (usage_bdnb == "travail")
    ).values

    # Fonction et flags dérivés (stratégique / éducation)
    fonctions = fonctions or {}
    env["fonction"] = b["ID"].map(fonctions)
    env["is_education"] = env["fonction"].isin(EDUCATION_FONCTIONS)
    env["is_strategic"] = env["fonction"].isin(STRATEGIC_FONCTIONS)

    env["n_etages"] = _compute_nb_etages(b).round().astype(int).values
    env["hauteur"] = b.get("HAUTEUR", pd.Series(index=idx, dtype="float64")).values
    env["emprise_m2"] = b.geometry.area.round(1).values
    env["z_min_sol"] = b.get("Z_MIN_SOL", pd.Series(index=idx, dtype="float64")).values
    env["z_max_toit"] = b.get("Z_MAX_TOIT", pd.Series(index=idx, dtype="float64")).values
    for col in ("mat_mur", "mat_toit", "annee_construction"):
        env[col] = b.get(col, pd.Series(index=idx, dtype="object")).values
    return env


def export_env(
    buildings: gpd.GeoDataFrame,
    output_dir: str | Path,
    workplace_usages: "tuple[str, ...] | list[str]" = DEFAULT_WORKPLACE_USAGES,
    fonctions: "dict[str, str] | None" = None,
) -> None:
    """Exporte le contrat d'environnement `env.{geojson,csv,shp}` (cf. build_env_layer)."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    env = _sanitize_for_export(build_env_layer(buildings, workplace_usages, fonctions=fonctions))
    _write_geojson(env, output_dir / "env.geojson")
    _write_csv(env.drop(columns=["geometry"]), output_dir / "env.csv")
    _write_shp(env, output_dir / "env.shp")
    n_strat = int(env["is_strategic"].sum())
    n_educ = int(env["is_education"].sum())
    logger.info(
        "Export env (contrat simu) : %d bâtiments  |  %d travail, %d résidentiel, "
        "%d stratégique, %d éducation",
        len(env), int(env["is_workplace"].sum()), int(env["is_residential"].sum()),
        n_strat, n_educ,
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
