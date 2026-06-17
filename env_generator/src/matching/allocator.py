import logging

import geopandas as gpd
import pandas as pd

logger = logging.getLogger(__name__)


def allocate_population(joined: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Distribute INSEE population to residential buildings.

    If P22_MEN (household count) is available, allocates households first then
    derives population via taille_moy_menage — closer to Genstar's approach.
    Otherwise falls back to direct proportional allocation by NB_LOGTS.

    For each cell, buildings receive households proportionally to their NB_LOGTS.
    Integer rounding residuals are assigned to the building with the most logements.

    Buildings outside the population grid receive population = 0 (expected: the
    map/region is deliberately larger than the simulated-population zone).

    Args:
        joined: GeoDataFrame from spatial_join, with columns NB_LOGTS, Ind_total,
                cell_idx, and optionally P22_MEN and taille_moy_menage.

    Returns:
        Same GeoDataFrame with additional integer columns:
        - population_allouee
        - menages_alloues  (only when P22_MEN is available)
    """
    result = joined.copy()
    result["population_allouee"] = 0

    csp_cols = [c for c in joined.columns if c.startswith("csp_") or c.startswith("age_")]
    use_menages = "P22_MEN" in joined.columns and joined["P22_MEN"].notna().any()
    if use_menages:
        result["menages_alloues"] = 0
        logger.info("Mode ménages : allocation par foyers puis dérivation population")
    else:
        logger.info("Mode individus : allocation proportionnelle directe")

    in_grid = result[result["Ind_total"].notna()].copy()

    if in_grid.empty:
        logger.warning("Aucun bâtiment dans un carreau INSEE — population = 0 partout")
        return result

    if use_menages:
        pop_series, men_series = _allocate_by_menages(in_grid)
        result.loc[pop_series.index, "population_allouee"] = pop_series
        result.loc[men_series.index, "menages_alloues"] = men_series
    else:
        allocated = _allocate_by_cell(in_grid)
        result.loc[allocated.index, "population_allouee"] = allocated

    if csp_cols:
        _allocate_csp_columns(in_grid, csp_cols, result)
        csp_totals = {c: result[c].sum() for c in csp_cols}
        logger.info("CSP alloués (totaux) : %s", csp_totals)

    total_allocated = result["population_allouee"].sum()
    total_insee = round(in_grid.groupby("cell_idx")["Ind_total"].first().sum())
    logger.info(
        "Population totale allouée : %d  |  Population INSEE totale : %d",
        total_allocated,
        total_insee,
    )

    n_outside = result["Ind_total"].isna().sum()
    if n_outside > 0:
        logger.info(
            "%d bâtiments hors zone population -> population = 0 "
            "(attendu : carte/région plus grande que la zone simulée)",
            n_outside,
        )

    return result


def _largest_remainder(raw: pd.Series, total: int) -> pd.Series:
    """Allocate `total` integers proportionally to `raw` using largest remainder method.

    Guarantees: all values >= 0, sum == total exactly.
    Avoids negative values that can occur when naive round() over-allocates
    and the residual is subtracted from a single building.
    """
    floored = raw.apply(int)  # floor for each building
    remainders = raw - floored
    deficit = total - floored.sum()
    # Top-up the buildings with the largest fractional parts
    top_up_idx = remainders.nlargest(int(deficit)).index
    floored.loc[top_up_idx] += 1
    return floored


def _allocate_by_menages(in_grid: gpd.GeoDataFrame) -> tuple[pd.Series, pd.Series]:
    """Household-first allocation: distribute menages proportionally to NB_LOGTS,
    then derive population = menages × taille_moy_menage.

    Uses largest remainder method to guarantee non-negative integer allocations.

    Returns (population_series, menages_series) both as integer Series.
    """
    pop_result = pd.Series(0, index=in_grid.index, dtype="int64")
    men_result = pd.Series(0, index=in_grid.index, dtype="int64")

    for cell_idx, group in in_grid.groupby("cell_idx", sort=False):
        total_men = round(group["P22_MEN"].iloc[0])
        taille = group["taille_moy_menage"].iloc[0]
        total_logts = group["NB_LOGTS"].sum()

        if len(group) == 1:
            men_result.loc[group.index[0]] = total_men
            pop_result.loc[group.index[0]] = round(total_men * taille)
            continue

        if total_logts == 0:
            per_building = total_men // len(group)
            remainder = total_men - per_building * len(group)
            men_result.loc[group.index] = per_building
            men_result.loc[group.index[0]] += remainder
        else:
            raw = group["NB_LOGTS"] / total_logts * total_men
            men_result.loc[group.index] = _largest_remainder(raw, total_men)

        # Derive population from households × mean household size
        total_pop = round(group["Ind_total"].iloc[0])
        raw_pop = men_result.loc[group.index] * taille
        pop_result.loc[group.index] = _largest_remainder(raw_pop, total_pop)

    return pop_result, men_result


def _allocate_csp_columns(
    in_grid: gpd.GeoDataFrame,
    csp_cols: list[str],
    result: gpd.GeoDataFrame,
) -> None:
    """Distribute each CSP column proportionally to NB_LOGTS (in-place).

    Uses the same largest remainder method as the main population allocation
    to guarantee non-negative integer values summing exactly to the IRIS total.
    """
    for col in csp_cols:
        result[col] = 0
        for _, group in in_grid.groupby("cell_idx", sort=False):
            total = round(group[col].iloc[0])
            if total == 0:
                continue
            if len(group) == 1:
                result.loc[group.index[0], col] = total
                continue
            total_logts = group["NB_LOGTS"].sum()
            if total_logts == 0:
                per = total // len(group)
                result.loc[group.index, col] = per
                result.loc[group.index[0], col] += total - per * len(group)
            else:
                raw = group["NB_LOGTS"] / total_logts * total
                result.loc[group.index, col] = _largest_remainder(raw, total)


def _allocate_by_cell(in_grid: gpd.GeoDataFrame) -> pd.Series:
    """Fallback: direct proportional allocation by NB_LOGTS.

    Uses largest remainder method to guarantee non-negative integer allocations.
    """
    result = pd.Series(0, index=in_grid.index, dtype="int64")

    for cell_idx, group in in_grid.groupby("cell_idx", sort=False):
        pop = round(group["Ind_total"].iloc[0])

        if len(group) == 1:
            result.loc[group.index[0]] = pop
            continue

        total_logts = group["NB_LOGTS"].sum()

        if total_logts == 0:
            per_building = pop // len(group)
            remainder = pop - per_building * len(group)
            result.loc[group.index] = per_building
            result.loc[group.index[0]] += remainder
            continue

        raw = group["NB_LOGTS"] / total_logts * pop
        result.loc[group.index] = _largest_remainder(raw, pop)

    return result
