"""Tests for loaders/insee.py — compute_ind_total logic."""

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Polygon

from src.loaders.insee import compute_ind_total


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_gdf(data: dict) -> gpd.GeoDataFrame:
    unit = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    n = len(next(iter(data.values())))
    df = pd.DataFrame(data)
    df["geometry"] = unit
    return gpd.GeoDataFrame(df, crs="EPSG:2154")


# ── Colonne Ind présente ───────────────────────────────────────────────────────

class TestComputeIndTotalWithInd:
    def test_uses_ind_directly(self):
        gdf = _make_gdf({"Ind": [100.0, 200.0]})
        result = compute_ind_total(gdf)
        assert list(result["Ind_total"]) == [100.0, 200.0]

    def test_ind_total_column_created(self):
        gdf = _make_gdf({"Ind": [50.0]})
        result = compute_ind_total(gdf)
        assert "Ind_total" in result.columns

    def test_ind_all_nan_triggers_fallback(self):
        gdf = _make_gdf({
            "Ind": [float("nan"), float("nan")],
            "Ind_0_3": [10.0, 5.0],
            "Ind_4_5": [8.0, 3.0],
        })
        result = compute_ind_total(gdf)
        # Should sum Ind_0_3 + Ind_4_5
        assert result["Ind_total"].iloc[0] == 18.0
        assert result["Ind_total"].iloc[1] == 8.0


# ── Colonne Ind absente — fallback ────────────────────────────────────────────

class TestComputeIndTotalFallback:
    def test_sums_ind_prefixed_columns(self):
        gdf = _make_gdf({
            "Ind_0_3": [10.0, 5.0],
            "Ind_4_5": [20.0, 15.0],
            "Ind_18_24": [30.0, 25.0],
        })
        result = compute_ind_total(gdf)
        assert result["Ind_total"].iloc[0] == 60.0
        assert result["Ind_total"].iloc[1] == 45.0

    def test_known_fallback_cols_included(self):
        gdf = _make_gdf({
            "Ind_80p": [5.0, 2.0],
            "Ind_inc": [1.0, 0.0],
        })
        result = compute_ind_total(gdf)
        assert result["Ind_total"].iloc[0] == 6.0

    def test_non_ind_columns_excluded_from_fallback(self):
        gdf = _make_gdf({
            "Ind_0_3": [10.0],
            "Men": [5.0],     # should not be summed
            "Superficie": [200.0],  # should not be summed
        })
        result = compute_ind_total(gdf)
        assert result["Ind_total"].iloc[0] == 10.0


# ── Carreaux à zéro ───────────────────────────────────────────────────────────

class TestZeroCarreaux:
    def test_zero_ind_total_allowed(self):
        """Carreaux with Ind_total=0 are not filtered out."""
        gdf = _make_gdf({"Ind": [0.0, 100.0]})
        result = compute_ind_total(gdf)
        assert len(result) == 2
        assert result["Ind_total"].iloc[0] == 0.0

    def test_returns_geodataframe(self):
        gdf = _make_gdf({"Ind": [50.0]})
        result = compute_ind_total(gdf)
        assert isinstance(result, gpd.GeoDataFrame)
