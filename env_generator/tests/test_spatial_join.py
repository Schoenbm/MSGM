"""Tests for matching/spatial_join.py."""

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Polygon

from src.matching.spatial_join import join_buildings_to_insee


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_grid(cells: list[dict]) -> gpd.GeoDataFrame:
    records = []
    for cell in cells:
        records.append({
            "Ind_total": cell["Ind_total"],
            **{k: v for k, v in cell.items() if k not in ("Ind_total",)},
            "geometry": cell.get("geometry"),
        })
    return gpd.GeoDataFrame(records, crs="EPSG:2154")


def _cell(x0, y0, x1, y1, ind=100.0, **extra):
    return {"Ind_total": ind, "geometry": Polygon([(x0,y0),(x1,y0),(x1,y1),(x0,y1)]), **extra}


def _building(cx, cy, nb_logts=5):
    half = 1
    return gpd.GeoDataFrame(
        [{"NB_LOGTS": nb_logts, "geometry": Polygon([
            (cx-half, cy-half), (cx+half, cy-half),
            (cx+half, cy+half), (cx-half, cy+half),
        ])}],
        crs="EPSG:2154",
    )


# ── Jointure de base ──────────────────────────────────────────────────────────

class TestJoinBuildingsToInsee:
    def test_building_inside_cell_gets_ind_total(self):
        grid = _make_grid([_cell(0, 0, 100, 100, ind=500.0)])
        buildings = _building(50, 50)
        result = join_buildings_to_insee(buildings, grid)
        assert result["Ind_total"].iloc[0] == 500.0

    def test_building_outside_grid_gets_nan(self):
        grid = _make_grid([_cell(0, 0, 50, 50, ind=100.0)])
        buildings = _building(80, 80)
        result = join_buildings_to_insee(buildings, grid)
        assert pd.isna(result["Ind_total"].iloc[0])

    def test_cell_idx_column_created(self):
        grid = _make_grid([_cell(0, 0, 100, 100)])
        buildings = _building(50, 50)
        result = join_buildings_to_insee(buildings, grid)
        assert "cell_idx" in result.columns

    def test_multiple_buildings_same_cell(self):
        grid = _make_grid([_cell(0, 0, 100, 100, ind=300.0)])
        b1 = _building(20, 20)
        b2 = _building(50, 50)
        b3 = _building(80, 80)
        buildings = pd.concat([b1, b2, b3], ignore_index=True)
        buildings = gpd.GeoDataFrame(buildings, crs="EPSG:2154")
        result = join_buildings_to_insee(buildings, grid)
        assert (result["Ind_total"] == 300.0).all()
        assert result["cell_idx"].nunique() == 1

    def test_buildings_in_different_cells(self):
        grid = _make_grid([
            _cell(0, 0, 50, 100, ind=100.0),
            _cell(50, 0, 100, 100, ind=200.0),
        ])
        b1 = _building(25, 50)
        b2 = _building(75, 50)
        buildings = pd.concat([b1, b2], ignore_index=True)
        buildings = gpd.GeoDataFrame(buildings, crs="EPSG:2154")
        result = join_buildings_to_insee(buildings, grid)
        assert set(result["Ind_total"].tolist()) == {100.0, 200.0}

    def test_geometry_restored_to_polygon(self):
        """Result geometry should be the original building polygon, not centroid."""
        grid = _make_grid([_cell(0, 0, 100, 100)])
        buildings = _building(50, 50)
        result = join_buildings_to_insee(buildings, grid)
        assert result.geometry.iloc[0].geom_type == "Polygon"

    def test_nb_logts_preserved(self):
        grid = _make_grid([_cell(0, 0, 100, 100)])
        buildings = _building(50, 50, nb_logts=12)
        result = join_buildings_to_insee(buildings, grid)
        assert result["NB_LOGTS"].iloc[0] == 12


# ── Passage de P22_MEN ────────────────────────────────────────────────────────

class TestP22MenPassthrough:
    def test_p22_men_passed_through_when_present(self):
        grid = _make_grid([{
            **_cell(0, 0, 100, 100, ind=100.0),
            "P22_MEN": 40.0,
            "taille_moy_menage": 2.5,
        }])
        buildings = _building(50, 50)
        result = join_buildings_to_insee(buildings, grid)
        assert "P22_MEN" in result.columns
        assert result["P22_MEN"].iloc[0] == 40.0

    def test_taille_moy_menage_passed_through_when_present(self):
        grid = _make_grid([{
            **_cell(0, 0, 100, 100, ind=100.0),
            "P22_MEN": 40.0,
            "taille_moy_menage": 2.5,
        }])
        buildings = _building(50, 50)
        result = join_buildings_to_insee(buildings, grid)
        assert "taille_moy_menage" in result.columns
        assert result["taille_moy_menage"].iloc[0] == 2.5

    def test_no_p22_men_column_when_absent_from_grid(self):
        grid = _make_grid([_cell(0, 0, 100, 100, ind=100.0)])
        buildings = _building(50, 50)
        result = join_buildings_to_insee(buildings, grid)
        assert "P22_MEN" not in result.columns


# ── Alignement CRS ────────────────────────────────────────────────────────────

class TestCrsAlignment:
    def test_buildings_reprojected_when_crs_differs(self):
        """join should not raise even when CRS differ (buildings get reprojected)."""
        grid = _make_grid([_cell(0, 0, 100, 100, ind=50.0)])
        # Give buildings a different CRS — same geometry, different label
        buildings = _building(50, 50)
        buildings = buildings.set_crs("EPSG:2154", allow_override=True)
        # Manually tag grid as different CRS for the test
        grid_diff = grid.copy()
        grid_diff.crs = None
        grid_diff = grid_diff.set_crs("EPSG:4326", allow_override=True)
        # Should not raise — buildings are reprojected
        result = join_buildings_to_insee(buildings, grid_diff)
        assert "Ind_total" in result.columns
