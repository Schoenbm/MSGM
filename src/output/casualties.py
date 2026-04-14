"""Calcul des victimes et sans-abris à partir d'un CSV de dommages bâtiment.

La matrice RISK-UE (adaptée Grenoble) relie le taux de dommage EMS98 (D1..D5)
aux préjudices humains (P0 indemne → P4 mort) et aux sans-abris.
"""

import logging
from pathlib import Path

import geopandas as gpd
import pandas as pd

logger = logging.getLogger(__name__)

# Matrice RISK-UE adaptée Grenoble (page 2 du PDF BRGM)
# Lignes = niveau de préjudice, colonnes = niveau de dommage D1..D5
_DAMAGE_MATRIX: dict[str, dict[str, float]] = {
    "P0": {"D1": 0.99945, "D2": 0.99560, "D3": 0.97796, "D4": 0.87960, "D5": 0.36000},
    "P1": {"D1": 0.00050, "D2": 0.00400, "D3": 0.02000, "D4": 0.10000, "D5": 0.24250},
    "P2": {"D1": 0.00005, "D2": 0.00040, "D3": 0.00200, "D4": 0.02000, "D5": 0.09770},
    "P3": {"D1": 0.00000, "D2": 0.00000, "D3": 0.00002, "D4": 0.00020, "D5": 0.06000},
    "P4": {"D1": 0.00000, "D2": 0.00000, "D3": 0.00002, "D4": 0.00020, "D5": 0.23980},
}

_VALID_DAMAGE_LEVELS = {"D1", "D2", "D3", "D4", "D5"}
_HOMELESS_LEVELS = {"D3", "D4", "D5"}
_CASUALTY_COLS = ["P0", "P1", "P2", "P3", "P4"]


def compute_casualties(
    damage_csv: "str | Path",
    population_gpkg: "str | Path",
    output_dir: "str | Path",
) -> Path:
    """Calcule les victimes et sans-abris par bâtiment et par IRIS.

    Args:
        damage_csv: CSV avec colonnes ID et damage_level (D1..D5).
        population_gpkg: GeoPackage résultat (doit contenir ID, population_allouee,
                         code_iris).
        output_dir: Répertoire de sortie (créé si absent).

    Returns:
        Chemin vers casualties_iris.csv.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    damage = _load_damage_csv(damage_csv)
    pop = _load_population(population_gpkg)

    # Jointure — seuls les bâtiments présents dans les deux sources
    n_pop = len(pop)
    n_dmg = len(damage)
    merged = pop.merge(damage, on="ID", how="inner")
    n_matched = len(merged)
    n_unmatched_pop = n_pop - n_matched
    n_unmatched_dmg = n_dmg - n_matched
    if n_unmatched_pop > 0:
        logger.warning(
            "%d bâtiment(s) avec population non trouvés dans le CSV de dommages "
            "(ignorés)",
            n_unmatched_pop,
        )
    if n_unmatched_dmg > 0:
        logger.warning(
            "%d ID du CSV de dommages sans correspondance dans la population "
            "(ignorés)",
            n_unmatched_dmg,
        )
    logger.info("%d bâtiments croisés sur %d endommagés", n_matched, n_dmg)

    if merged.empty:
        logger.warning("Aucun bâtiment croisé — fichiers de sortie vides.")
        empty_bldg = output_dir / "casualties_buildings.csv"
        empty_iris = output_dir / "casualties_iris.csv"
        pd.DataFrame(
            columns=["ID", "code_iris", "damage_level", "population_allouee"]
            + _CASUALTY_COLS
            + ["sans_abris"]
        ).to_csv(empty_bldg, index=False)
        pd.DataFrame(
            columns=[
                "code_iris", "pop_exposee", "P0_indemnes", "P1_blesses_legers",
                "P2_hospitalises", "P3_blesses_graves", "P4_morts", "sans_abris",
            ]
        ).to_csv(empty_iris, index=False)
        return empty_iris

    result = _apply_damage_matrix(merged)

    # Export bâtiment
    bldg_path = output_dir / "casualties_buildings.csv"
    result.to_csv(bldg_path, index=False)
    logger.info("Export bâtiment : %s  (%d lignes)", bldg_path, len(result))

    # Agrégation IRIS
    iris_df = _aggregate_by_iris(result)
    iris_path = output_dir / "casualties_iris.csv"
    iris_df.to_csv(iris_path, index=False)
    logger.info("Export IRIS     : %s  (%d IRIS)", iris_path, len(iris_df))

    # Synthèse globale
    logger.info(
        "=== Synthèse globale ===\n"
        "  Population exposée : %d\n"
        "  P0 indemnes        : %d\n"
        "  P1 blessés légers  : %d\n"
        "  P2 hospitalisés    : %d\n"
        "  P3 blessés graves  : %d\n"
        "  P4 morts           : %d\n"
        "  Sans-abris         : %d",
        int(result["population_allouee"].sum()),
        int(result["P0"].sum()),
        int(result["P1"].sum()),
        int(result["P2"].sum()),
        int(result["P3"].sum()),
        int(result["P4"].sum()),
        int(result["sans_abris"].sum()),
    )

    return iris_path


# ── Chargement ────────────────────────────────────────────────────────────────

def _load_damage_csv(path: "str | Path") -> pd.DataFrame:
    """Lit le CSV de dommages, valide et normalise."""
    path = Path(path)
    try:
        df = pd.read_csv(path)
    except Exception as exc:
        raise ValueError(f"Impossible de lire le CSV de dommages : {path}") from exc

    if "ID" not in df.columns:
        raise ValueError(
            f"Colonne 'ID' manquante dans {path}. "
            f"Colonnes présentes : {list(df.columns)}"
        )
    if "damage_level" not in df.columns:
        raise ValueError(
            f"Colonne 'damage_level' manquante dans {path}. "
            f"Colonnes présentes : {list(df.columns)}"
        )

    df = df[["ID", "damage_level"]].copy()
    df["damage_level"] = df["damage_level"].str.upper().str.strip()

    # Dédupliquer sur ID (garder le premier)
    n_before = len(df)
    df = df.drop_duplicates(subset="ID", keep="first")
    if len(df) < n_before:
        logger.warning("Doublons sur ID supprimés : %d lignes → %d", n_before, len(df))

    invalid = set(df["damage_level"].unique()) - _VALID_DAMAGE_LEVELS
    if invalid:
        raise ValueError(
            f"Niveau(x) de dommage inconnu(s) : {invalid}. "
            f"Valeurs acceptées : {sorted(_VALID_DAMAGE_LEVELS)}"
        )

    return df


def _load_population(path: "str | Path") -> pd.DataFrame:
    """Lit le GeoPackage de population, retourne les 3 colonnes nécessaires."""
    path = Path(path)
    gdf = gpd.read_file(path)

    for col in ("ID", "population_allouee", "code_iris"):
        if col not in gdf.columns:
            raise ValueError(
                f"Colonne '{col}' manquante dans {path}. "
                f"Colonnes présentes : {list(gdf.columns)}"
            )

    return pd.DataFrame(gdf[["ID", "population_allouee", "code_iris"]])


# ── Calcul ────────────────────────────────────────────────────────────────────

def _apply_damage_matrix(df: pd.DataFrame) -> pd.DataFrame:
    """Applique la matrice RISK-UE et ajuste les résidus (largest-remainder)."""
    result = df.copy()

    for px in _CASUALTY_COLS:
        coeffs = pd.Series(
            {lvl: _DAMAGE_MATRIX[px][lvl] for lvl in _VALID_DAMAGE_LEVELS}
        )
        result[px] = (
            result["damage_level"].map(coeffs) * result["population_allouee"]
        ).fillna(0.0)

    # Ajustement des résidus ligne par ligne
    result[_CASUALTY_COLS] = result.apply(_adjust_row_residuals, axis=1)

    # Sans-abris
    result["sans_abris"] = result.apply(
        lambda r: int(r["population_allouee"])
        if r["damage_level"] in _HOMELESS_LEVELS
        else 0,
        axis=1,
    )

    return result[
        ["ID", "code_iris", "damage_level", "population_allouee"]
        + _CASUALTY_COLS
        + ["sans_abris"]
    ]


def _adjust_row_residuals(row: pd.Series) -> pd.Series:
    """Largest-remainder sur P0..P4 pour que leur somme == population_allouee."""
    total = int(row["population_allouee"])
    raw = row[_CASUALTY_COLS].astype(float)

    floored = raw.apply(int)
    remainders = raw - floored
    deficit = total - int(floored.sum())

    if deficit > 0:
        top_up = remainders.nlargest(deficit).index
        floored.loc[top_up] += 1
    elif deficit < 0:
        # Sur-allocation rare (arrondi ≥ 0.5 sur toutes les catégories)
        trim = (-floored).nsmallest(-deficit).index  # ceux qui ont le plus de marge
        for idx in trim:
            if floored.loc[idx] > 0:
                floored.loc[idx] -= 1

    return floored.astype(int)


# ── Agrégation ────────────────────────────────────────────────────────────────

def _aggregate_by_iris(df: pd.DataFrame) -> pd.DataFrame:
    """Agrège les victimes par code_iris."""
    agg = (
        df.groupby("code_iris")[["population_allouee"] + _CASUALTY_COLS + ["sans_abris"]]
        .sum()
        .reset_index()
    )
    agg = agg.rename(
        columns={
            "population_allouee": "pop_exposee",
            "P0": "P0_indemnes",
            "P1": "P1_blesses_legers",
            "P2": "P2_hospitalises",
            "P3": "P3_blesses_graves",
            "P4": "P4_morts",
        }
    )
    return agg
