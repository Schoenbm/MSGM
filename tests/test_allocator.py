"""Tests for matching/allocator.py — population allocation logic."""

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Polygon

from src.matching.allocator import allocate_population


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_gdf(rows: list[dict]) -> gpd.GeoDataFrame:
    """Build a minimal GeoDataFrame with the columns allocator expects (fallback mode)."""
    unit_square = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    records = []
    for i, row in enumerate(rows):
        records.append({
            "ID": row.get("ID", f"BAT{i}"),
            "NB_LOGTS": row["NB_LOGTS"],
            "Ind_total": row.get("Ind_total", None),
            "cell_idx": row.get("cell_idx", None),
            "geometry": unit_square,
        })
    return gpd.GeoDataFrame(records, crs="EPSG:2154")


def _make_gdf_menages(rows: list[dict]) -> gpd.GeoDataFrame:
    """Build a GeoDataFrame with P22_MEN + taille_moy_menage (household mode)."""
    unit_square = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    records = []
    for i, row in enumerate(rows):
        records.append({
            "ID": row.get("ID", f"BAT{i}"),
            "NB_LOGTS": row["NB_LOGTS"],
            "Ind_total": row.get("Ind_total", None),
            "P22_MEN": row.get("P22_MEN", None),
            "taille_moy_menage": row.get("taille_moy_menage", 2.3),
            "cell_idx": row.get("cell_idx", None),
            "geometry": unit_square,
        })
    return gpd.GeoDataFrame(records, crs="EPSG:2154")


# ── Conservation ──────────────────────────────────────────────────────────────

def test_conservation_exact():
    """Sum of allocated population equals Ind_total for each cell."""
    gdf = _make_gdf([
        {"NB_LOGTS": 10, "Ind_total": 100.0, "cell_idx": 1},
        {"NB_LOGTS": 20, "Ind_total": 100.0, "cell_idx": 1},
        {"NB_LOGTS": 30, "Ind_total": 100.0, "cell_idx": 1},
    ])
    result = allocate_population(gdf)
    assert result["population_allouee"].sum() == 100


def test_conservation_multiple_cells():
    """Conservation holds independently for each cell."""
    gdf = _make_gdf([
        {"NB_LOGTS": 3, "Ind_total": 10.0, "cell_idx": 1},
        {"NB_LOGTS": 7, "Ind_total": 10.0, "cell_idx": 1},
        {"NB_LOGTS": 5, "Ind_total": 50.0, "cell_idx": 2},
        {"NB_LOGTS": 5, "Ind_total": 50.0, "cell_idx": 2},
    ])
    result = allocate_population(gdf)
    cell1 = result[result["cell_idx"] == 1]["population_allouee"].sum()
    cell2 = result[result["cell_idx"] == 2]["population_allouee"].sum()
    assert cell1 == 10
    assert cell2 == 50


# ── Proportionality ───────────────────────────────────────────────────────────

def test_proportional_allocation():
    """Buildings with equal NB_LOGTS get equal population."""
    gdf = _make_gdf([
        {"NB_LOGTS": 10, "Ind_total": 60.0, "cell_idx": 1},
        {"NB_LOGTS": 10, "Ind_total": 60.0, "cell_idx": 1},
        {"NB_LOGTS": 10, "Ind_total": 60.0, "cell_idx": 1},
    ])
    result = allocate_population(gdf)
    assert list(result["population_allouee"]) == [20, 20, 20]


def test_proportional_allocation_unequal():
    """Building with double NB_LOGTS gets double population."""
    gdf = _make_gdf([
        {"NB_LOGTS": 10, "Ind_total": 30.0, "cell_idx": 1},
        {"NB_LOGTS": 20, "Ind_total": 30.0, "cell_idx": 1},
    ])
    result = allocate_population(gdf)
    pops = list(result["population_allouee"])
    assert pops[1] == 2 * pops[0]
    assert sum(pops) == 30


# ── Residual adjustment ───────────────────────────────────────────────────────

def test_residual_goes_to_largest():
    """Rounding residual is assigned to building with largest NB_LOGTS."""
    # 3 equal buildings, Ind=10 → each gets 3.33, rounded to 3 → sum=9, residual=+1
    gdf = _make_gdf([
        {"ID": "A", "NB_LOGTS": 5, "Ind_total": 10.0, "cell_idx": 1},
        {"ID": "B", "NB_LOGTS": 10, "Ind_total": 10.0, "cell_idx": 1},  # largest
        {"ID": "C", "NB_LOGTS": 5, "Ind_total": 10.0, "cell_idx": 1},
    ])
    result = allocate_population(gdf)
    assert result["population_allouee"].sum() == 10
    # Building B (largest NB_LOGTS) absorbs the residual
    pop_b = result.loc[result["ID"] == "B", "population_allouee"].iloc[0]
    pop_a = result.loc[result["ID"] == "A", "population_allouee"].iloc[0]
    pop_c = result.loc[result["ID"] == "C", "population_allouee"].iloc[0]
    assert pop_b >= pop_a and pop_b >= pop_c


def test_residual_negative():
    """Negative residual (over-count after rounding) is also handled."""
    # Designed so rounding gives sum > Ind_total
    gdf = _make_gdf([
        {"ID": "A", "NB_LOGTS": 1, "Ind_total": 2.0, "cell_idx": 1},
        {"ID": "B", "NB_LOGTS": 1, "Ind_total": 2.0, "cell_idx": 1},
        {"ID": "C", "NB_LOGTS": 1, "Ind_total": 2.0, "cell_idx": 1},
    ])
    result = allocate_population(gdf)
    assert result["population_allouee"].sum() == 2


# ── Single building ───────────────────────────────────────────────────────────

def test_single_building_gets_all():
    """A cell with one building receives 100% of population."""
    gdf = _make_gdf([
        {"NB_LOGTS": 5, "Ind_total": 42.0, "cell_idx": 1},
    ])
    result = allocate_population(gdf)
    assert result["population_allouee"].iloc[0] == 42


# ── Buildings outside grid ────────────────────────────────────────────────────

def test_outside_grid_gets_zero():
    """Buildings with Ind_total = NaN receive population = 0."""
    gdf = _make_gdf([
        {"NB_LOGTS": 3, "Ind_total": None, "cell_idx": None},
        {"NB_LOGTS": 5, "Ind_total": None, "cell_idx": None},
    ])
    result = allocate_population(gdf)
    assert (result["population_allouee"] == 0).all()


def test_mixed_inside_outside():
    """Inside-grid buildings are allocated normally; outside-grid get zero."""
    gdf = _make_gdf([
        {"NB_LOGTS": 10, "Ind_total": 20.0, "cell_idx": 1},
        {"NB_LOGTS": 10, "Ind_total": 20.0, "cell_idx": 1},
        {"NB_LOGTS": 5,  "Ind_total": None, "cell_idx": None},
    ])
    result = allocate_population(gdf)
    assert result.iloc[2]["population_allouee"] == 0
    assert result.iloc[0]["population_allouee"] + result.iloc[1]["population_allouee"] == 20


# ── Half-integer Ind_total (INSEE noise) ──────────────────────────────────────

def test_half_integer_ind_total():
    """Ind_total = 10.5 is rounded to 11 before allocation, sum is conserved."""
    gdf = _make_gdf([
        {"NB_LOGTS": 1, "Ind_total": 10.5, "cell_idx": 1},
        {"NB_LOGTS": 1, "Ind_total": 10.5, "cell_idx": 1},
    ])
    result = allocate_population(gdf)
    assert result["population_allouee"].sum() == round(10.5)


# ── Zero NB_LOGTS ─────────────────────────────────────────────────────────────

def test_zero_nb_logts_equal_split():
    """When all NB_LOGTS are 0, population is split equally."""
    gdf = _make_gdf([
        {"NB_LOGTS": 0, "Ind_total": 9.0, "cell_idx": 1},
        {"NB_LOGTS": 0, "Ind_total": 9.0, "cell_idx": 1},
        {"NB_LOGTS": 0, "Ind_total": 9.0, "cell_idx": 1},
    ])
    result = allocate_population(gdf)
    assert result["population_allouee"].sum() == 9
    assert list(result["population_allouee"]) == [3, 3, 3]


# ── Output dtype ─────────────────────────────────────────────────────────────

def test_output_dtype_is_integer():
    """population_allouee column must be integer dtype."""
    gdf = _make_gdf([
        {"NB_LOGTS": 2, "Ind_total": 10.0, "cell_idx": 1},
    ])
    result = allocate_population(gdf)
    assert pd.api.types.is_integer_dtype(result["population_allouee"])


# ── Mode ménages : activation ─────────────────────────────────────────────────

def test_menages_mode_activates_when_p22_men_present():
    """menages_alloues column is created when P22_MEN is available."""
    gdf = _make_gdf_menages([
        {"NB_LOGTS": 5, "Ind_total": 10.0, "P22_MEN": 4.0, "taille_moy_menage": 2.5, "cell_idx": 1},
    ])
    result = allocate_population(gdf)
    assert "menages_alloues" in result.columns


def test_menages_mode_absent_without_p22_men():
    """menages_alloues column is NOT created in fallback mode."""
    gdf = _make_gdf([
        {"NB_LOGTS": 5, "Ind_total": 10.0, "cell_idx": 1},
    ])
    result = allocate_population(gdf)
    assert "menages_alloues" not in result.columns


# ── Mode ménages : conservation ───────────────────────────────────────────────

def test_menages_conservation_single_cell():
    """Sum of menages_alloues equals P22_MEN for the cell."""
    gdf = _make_gdf_menages([
        {"NB_LOGTS": 10, "Ind_total": 50.0, "P22_MEN": 20.0, "taille_moy_menage": 2.5, "cell_idx": 1},
        {"NB_LOGTS": 10, "Ind_total": 50.0, "P22_MEN": 20.0, "taille_moy_menage": 2.5, "cell_idx": 1},
    ])
    result = allocate_population(gdf)
    assert result["menages_alloues"].sum() == 20


def test_population_conservation_in_menages_mode():
    """Sum of population_allouee equals Ind_total even in household mode (drift corrected)."""
    gdf = _make_gdf_menages([
        {"NB_LOGTS": 10, "Ind_total": 47.0, "P22_MEN": 20.0, "taille_moy_menage": 2.35, "cell_idx": 1},
        {"NB_LOGTS": 10, "Ind_total": 47.0, "P22_MEN": 20.0, "taille_moy_menage": 2.35, "cell_idx": 1},
        {"NB_LOGTS": 10, "Ind_total": 47.0, "P22_MEN": 20.0, "taille_moy_menage": 2.35, "cell_idx": 1},
    ])
    result = allocate_population(gdf)
    assert result["population_allouee"].sum() == 47


def test_menages_conservation_multiple_cells():
    """Household conservation holds independently per cell."""
    gdf = _make_gdf_menages([
        {"NB_LOGTS": 5, "Ind_total": 10.0, "P22_MEN": 4.0,  "taille_moy_menage": 2.5, "cell_idx": 1},
        {"NB_LOGTS": 5, "Ind_total": 10.0, "P22_MEN": 4.0,  "taille_moy_menage": 2.5, "cell_idx": 1},
        {"NB_LOGTS": 8, "Ind_total": 30.0, "P22_MEN": 12.0, "taille_moy_menage": 2.5, "cell_idx": 2},
        {"NB_LOGTS": 4, "Ind_total": 30.0, "P22_MEN": 12.0, "taille_moy_menage": 2.5, "cell_idx": 2},
    ])
    result = allocate_population(gdf)
    cell1 = result[result["cell_idx"] == 1]["menages_alloues"].sum()
    cell2 = result[result["cell_idx"] == 2]["menages_alloues"].sum()
    assert cell1 == 4
    assert cell2 == 12


# ── Mode ménages : proportionnalité ──────────────────────────────────────────

def test_menages_proportional_to_nb_logts():
    """Building with double NB_LOGTS receives double menages."""
    gdf = _make_gdf_menages([
        {"ID": "A", "NB_LOGTS": 5,  "Ind_total": 30.0, "P22_MEN": 12.0, "taille_moy_menage": 2.5, "cell_idx": 1},
        {"ID": "B", "NB_LOGTS": 10, "Ind_total": 30.0, "P22_MEN": 12.0, "taille_moy_menage": 2.5, "cell_idx": 1},
    ])
    result = allocate_population(gdf)
    men_a = result.loc[result["ID"] == "A", "menages_alloues"].iloc[0]
    men_b = result.loc[result["ID"] == "B", "menages_alloues"].iloc[0]
    assert men_b == 2 * men_a
    assert men_a + men_b == 12


def test_menages_equal_logts_equal_menages():
    """Buildings with equal NB_LOGTS receive equal menages."""
    gdf = _make_gdf_menages([
        {"NB_LOGTS": 10, "Ind_total": 60.0, "P22_MEN": 24.0, "taille_moy_menage": 2.5, "cell_idx": 1},
        {"NB_LOGTS": 10, "Ind_total": 60.0, "P22_MEN": 24.0, "taille_moy_menage": 2.5, "cell_idx": 1},
        {"NB_LOGTS": 10, "Ind_total": 60.0, "P22_MEN": 24.0, "taille_moy_menage": 2.5, "cell_idx": 1},
    ])
    result = allocate_population(gdf)
    assert list(result["menages_alloues"]) == [8, 8, 8]


# ── Mode ménages : cas limites ────────────────────────────────────────────────

def test_menages_single_building_gets_all():
    """Single building in a cell receives all menages."""
    gdf = _make_gdf_menages([
        {"NB_LOGTS": 10, "Ind_total": 25.0, "P22_MEN": 10.0, "taille_moy_menage": 2.5, "cell_idx": 1},
    ])
    result = allocate_population(gdf)
    assert result["menages_alloues"].iloc[0] == 10
    assert result["population_allouee"].iloc[0] == 25


def test_menages_outside_grid_gets_zero():
    """Buildings outside grid get menages_alloues = 0 in a mixed dataset."""
    gdf = _make_gdf_menages([
        {"NB_LOGTS": 10, "Ind_total": 25.0, "P22_MEN": 10.0, "taille_moy_menage": 2.5, "cell_idx": 1},
        {"NB_LOGTS": 5,  "Ind_total": None, "P22_MEN": None,  "taille_moy_menage": 2.5, "cell_idx": None},
    ])
    result = allocate_population(gdf)
    assert result["menages_alloues"].iloc[1] == 0
    assert result["population_allouee"].iloc[1] == 0


def test_menages_residual_on_largest():
    """Rounding residual on menages goes to building with largest NB_LOGTS."""
    # 3 equal buildings, 10 menages → each gets 3.33, rounded to 3 → residual +1 to largest
    gdf = _make_gdf_menages([
        {"ID": "A", "NB_LOGTS": 5,  "Ind_total": 24.0, "P22_MEN": 10.0, "taille_moy_menage": 2.4, "cell_idx": 1},
        {"ID": "B", "NB_LOGTS": 10, "Ind_total": 24.0, "P22_MEN": 10.0, "taille_moy_menage": 2.4, "cell_idx": 1},
        {"ID": "C", "NB_LOGTS": 5,  "Ind_total": 24.0, "P22_MEN": 10.0, "taille_moy_menage": 2.4, "cell_idx": 1},
    ])
    result = allocate_population(gdf)
    assert result["menages_alloues"].sum() == 10
    men_b = result.loc[result["ID"] == "B", "menages_alloues"].iloc[0]
    men_a = result.loc[result["ID"] == "A", "menages_alloues"].iloc[0]
    men_c = result.loc[result["ID"] == "C", "menages_alloues"].iloc[0]
    assert men_b >= men_a and men_b >= men_c


def test_menages_dtype_is_integer():
    """menages_alloues column must be integer dtype."""
    gdf = _make_gdf_menages([
        {"NB_LOGTS": 5, "Ind_total": 10.0, "P22_MEN": 4.0, "taille_moy_menage": 2.5, "cell_idx": 1},
    ])
    result = allocate_population(gdf)
    assert pd.api.types.is_integer_dtype(result["menages_alloues"])


def test_menages_zero_nb_logts_equal_split():
    """When NB_LOGTS=0 for all, menages are split equally."""
    gdf = _make_gdf_menages([
        {"NB_LOGTS": 0, "Ind_total": 9.0, "P22_MEN": 3.0, "taille_moy_menage": 3.0, "cell_idx": 1},
        {"NB_LOGTS": 0, "Ind_total": 9.0, "P22_MEN": 3.0, "taille_moy_menage": 3.0, "cell_idx": 1},
        {"NB_LOGTS": 0, "Ind_total": 9.0, "P22_MEN": 3.0, "taille_moy_menage": 3.0, "cell_idx": 1},
    ])
    result = allocate_population(gdf)
    assert result["menages_alloues"].sum() == 3
    assert list(result["menages_alloues"]) == [1, 1, 1]
