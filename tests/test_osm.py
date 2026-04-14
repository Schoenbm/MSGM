"""Tests for loaders/osm.py — OSM matching logic."""

import numpy as np
import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Polygon

from src.loaders.osm import (
    _max_not_null,
    _mode_not_null,
    _sum_not_null,
    _to_float,
    _to_int,
    match_osm_to_bdtopo,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_buildings(polys: list[Polygon], **extra_cols) -> gpd.GeoDataFrame:
    """Build a minimal BD_TOPO GeoDataFrame."""
    records = [{"geometry": p} for p in polys]
    gdf = gpd.GeoDataFrame(records, crs="EPSG:2154")
    for col, values in extra_cols.items():
        gdf[col] = values
    return gdf


def _make_osm(rows: list[dict]) -> gpd.GeoDataFrame:
    """Build a minimal OSM GeoDataFrame (EPSG:2154 for simplicity in tests)."""
    records = []
    for row in rows:
        records.append({
            "osm_id": row.get("osm_id", 1),
            "building": row.get("building", "yes"),
            "building_flats": row.get("building_flats", None),
            "building_levels": row.get("building_levels", None),
            "geometry": row["geometry"],
        })
    return gpd.GeoDataFrame(records, crs="EPSG:2154")


# Polygones de référence
_BLD_SQUARE = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])      # 100 m²
_OSM_SAME   = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])      # 100% overlap
_OSM_HALF   = Polygon([(0, 0), (10, 0), (10, 20), (0, 20)])      # 50% overlap (100/200)
_OSM_THIRD  = Polygon([(0, 0), (10, 0), (10, 30), (0, 30)])      # 33% overlap (100/300)
_OSM_FAR    = Polygon([(50, 50), (60, 50), (60, 60), (50, 60)])  # 0% overlap


# ── match_osm_to_bdtopo — seuil de chevauchement ─────────────────────────────

class TestMatchOverlapThreshold:
    def test_full_overlap_matched(self):
        """OSM polygon identical to BD_TOPO polygon → matched (ratio = 1.0)."""
        bld = _make_buildings([_BLD_SQUARE])
        osm = _make_osm([{"geometry": _OSM_SAME, "building_flats": 5}])
        result = match_osm_to_bdtopo(bld, osm, overlap_threshold=0.5)
        assert result.iloc[0]["osm_flats"] == 5

    def test_exact_threshold_matched(self):
        """OSM polygon with exactly 50% inside BD_TOPO → matched at default threshold."""
        bld = _make_buildings([_BLD_SQUARE])
        osm = _make_osm([{"geometry": _OSM_HALF, "building_flats": 3}])
        result = match_osm_to_bdtopo(bld, osm, overlap_threshold=0.5)
        assert result.iloc[0]["osm_flats"] == 3

    def test_below_threshold_not_matched(self):
        """OSM polygon with ~33% overlap → not matched at default threshold."""
        bld = _make_buildings([_BLD_SQUARE])
        osm = _make_osm([{"geometry": _OSM_THIRD, "building_flats": 4}])
        result = match_osm_to_bdtopo(bld, osm, overlap_threshold=0.5)
        assert pd.isna(result.iloc[0]["osm_flats"])

    def test_no_intersection_not_matched(self):
        """OSM polygon far from BD_TOPO → no match."""
        bld = _make_buildings([_BLD_SQUARE])
        osm = _make_osm([{"geometry": _OSM_FAR, "building_flats": 7}])
        result = match_osm_to_bdtopo(bld, osm, overlap_threshold=0.5)
        assert pd.isna(result.iloc[0]["osm_flats"])

    def test_custom_threshold_lower(self):
        """Lowering threshold to 0.3 captures the 33%-overlap polygon."""
        bld = _make_buildings([_BLD_SQUARE])
        osm = _make_osm([{"geometry": _OSM_THIRD, "building_flats": 2}])
        result = match_osm_to_bdtopo(bld, osm, overlap_threshold=0.3)
        assert result.iloc[0]["osm_flats"] == 2

    def test_adjacent_neighbor_not_matched(self):
        """An OSM polygon with only 10% overlap (neighboring building) is excluded."""
        bld = _make_buildings([_BLD_SQUARE])
        # OSM rectangle, 90% of it is outside the BD_TOPO building → 10/100 = 10% overlap
        osm_neighbor = Polygon([(0, 0), (10, 0), (10, 100), (0, 100)])  # area=1000, inter=100 → 10%
        osm = _make_osm([{"geometry": osm_neighbor, "building_flats": 8}])
        result = match_osm_to_bdtopo(bld, osm, overlap_threshold=0.5)
        assert pd.isna(result.iloc[0]["osm_flats"])


# ── match_osm_to_bdtopo — agrégation ─────────────────────────────────────────

class TestMatchAggregation:
    def test_multiple_osm_flats_summed(self):
        """Multiple OSM polygons matched to one BD_TOPO building → flats summed."""
        bld_poly = Polygon([(0, 0), (20, 0), (20, 20), (0, 20)])  # 400 m²
        bld = _make_buildings([bld_poly])
        # Two OSM polygons, each fully inside the BD_TOPO building
        osm1 = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])   # 100 m², 100% inside
        osm2 = Polygon([(10, 0), (20, 0), (20, 10), (10, 10)]) # 100 m², 100% inside
        osm = _make_osm([
            {"geometry": osm1, "building_flats": 4},
            {"geometry": osm2, "building_flats": 6},
        ])
        result = match_osm_to_bdtopo(bld, osm, overlap_threshold=0.5)
        assert result.iloc[0]["osm_flats"] == 10

    def test_multiple_osm_levels_takes_max(self):
        """Multiple OSM polygons matched → osm_levels = max."""
        bld_poly = Polygon([(0, 0), (20, 0), (20, 20), (0, 20)])
        bld = _make_buildings([bld_poly])
        osm1 = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
        osm2 = Polygon([(10, 0), (20, 0), (20, 10), (10, 10)])
        osm = _make_osm([
            {"geometry": osm1, "building_levels": 3.0},
            {"geometry": osm2, "building_levels": 5.0},
        ])
        result = match_osm_to_bdtopo(bld, osm, overlap_threshold=0.5)
        assert result.iloc[0]["osm_levels"] == 5.0

    def test_flats_nan_in_some_osm_polygons_ignored(self):
        """NaN building_flats in one OSM polygon is skipped in summation."""
        bld_poly = Polygon([(0, 0), (20, 0), (20, 20), (0, 20)])
        bld = _make_buildings([bld_poly])
        osm1 = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
        osm2 = Polygon([(10, 0), (20, 0), (20, 10), (10, 10)])
        osm = _make_osm([
            {"geometry": osm1, "building_flats": None},
            {"geometry": osm2, "building_flats": 6},
        ])
        result = match_osm_to_bdtopo(bld, osm, overlap_threshold=0.5)
        assert result.iloc[0]["osm_flats"] == 6

    def test_all_osm_flats_nan_gives_nan(self):
        """If all matched OSM polygons have building_flats=None → osm_flats = NaN."""
        bld_poly = Polygon([(0, 0), (20, 0), (20, 20), (0, 20)])
        bld = _make_buildings([bld_poly])
        osm1 = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
        osm = _make_osm([{"geometry": osm1, "building_flats": None, "building_levels": 4.0}])
        result = match_osm_to_bdtopo(bld, osm, overlap_threshold=0.5)
        assert pd.isna(result.iloc[0]["osm_flats"])
        assert result.iloc[0]["osm_levels"] == 4.0

    def test_multiple_buildings_independent(self):
        """Each BD_TOPO building is matched independently."""
        bld1 = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
        bld2 = Polygon([(20, 0), (30, 0), (30, 10), (20, 10)])
        bld = _make_buildings([bld1, bld2])
        osm1 = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])   # matches bld1
        osm2 = Polygon([(20, 0), (30, 0), (30, 10), (20, 10)]) # matches bld2
        osm = _make_osm([
            {"geometry": osm1, "building_flats": 3},
            {"geometry": osm2, "building_flats": 7},
        ])
        result = match_osm_to_bdtopo(bld, osm, overlap_threshold=0.5)
        assert result.iloc[0]["osm_flats"] == 3
        assert result.iloc[1]["osm_flats"] == 7


# ── match_osm_to_bdtopo — cas limites ─────────────────────────────────────────

class TestMatchEdgeCases:
    def test_empty_osm_returns_nan_columns(self):
        """Empty OSM GeoDataFrame → buildings unchanged with NaN columns."""
        bld = _make_buildings([_BLD_SQUARE])
        osm = gpd.GeoDataFrame(
            columns=["osm_id", "building", "building_flats", "building_levels", "geometry"],
            crs="EPSG:2154",
        )
        result = match_osm_to_bdtopo(bld, osm)
        assert "osm_flats" in result.columns
        assert "osm_levels" in result.columns
        assert pd.isna(result.iloc[0]["osm_flats"])
        assert pd.isna(result.iloc[0]["osm_levels"])

    def test_none_osm_returns_nan_columns(self):
        """osm=None → buildings unchanged with NaN columns."""
        bld = _make_buildings([_BLD_SQUARE])
        result = match_osm_to_bdtopo(bld, None)
        assert pd.isna(result.iloc[0]["osm_flats"])

    def test_output_has_osm_columns(self):
        """Output always contains osm_flats and osm_levels regardless of match."""
        bld = _make_buildings([_BLD_SQUARE])
        osm = _make_osm([{"geometry": _OSM_FAR}])
        result = match_osm_to_bdtopo(bld, osm)
        assert "osm_flats" in result.columns
        assert "osm_levels" in result.columns

    def test_original_index_preserved(self):
        """Original buildings index labels are preserved after matching."""
        bld = _make_buildings([_BLD_SQUARE, Polygon([(20, 0), (30, 0), (30, 10), (20, 10)])])
        bld.index = [100, 200]  # index non-contiguous
        osm = _make_osm([{"geometry": _OSM_SAME, "building_flats": 5}])
        result = match_osm_to_bdtopo(bld, osm)
        assert list(result.index) == [100, 200]
        assert result.loc[100, "osm_flats"] == 5
        assert pd.isna(result.loc[200, "osm_flats"])

    def test_original_columns_preserved(self):
        """Existing building columns are not modified by matching."""
        bld = _make_buildings([_BLD_SQUARE], NB_LOGTS=[5], USAGE1=["Résidentiel"])
        osm = _make_osm([{"geometry": _OSM_SAME, "building_flats": 3}])
        result = match_osm_to_bdtopo(bld, osm)
        assert result.iloc[0]["NB_LOGTS"] == 5
        assert result.iloc[0]["USAGE1"] == "Résidentiel"

    def test_crs_reprojection(self):
        """OSM in WGS84 is reprojected to buildings CRS before matching."""
        bld = _make_buildings([_BLD_SQUARE])  # EPSG:2154
        # Build an OSM GDF in EPSG:2154 then convert to 4326 to simulate real usage
        osm_2154 = _make_osm([{"geometry": _OSM_SAME, "building_flats": 4}])
        osm_4326 = osm_2154.to_crs("EPSG:4326")
        result = match_osm_to_bdtopo(bld, osm_4326)
        # After reprojection, coordinates differ slightly but should still match
        assert result.iloc[0]["osm_flats"] == 4

    def test_unmatched_building_gets_nan(self):
        """BD_TOPO buildings with no matching OSM polygon get NaN."""
        bld1 = _BLD_SQUARE
        bld2 = Polygon([(50, 50), (60, 50), (60, 60), (50, 60)])  # far away
        bld = _make_buildings([bld1, bld2])
        osm = _make_osm([{"geometry": _OSM_SAME, "building_flats": 5}])
        result = match_osm_to_bdtopo(bld, osm)
        assert result.iloc[0]["osm_flats"] == 5
        assert pd.isna(result.iloc[1]["osm_flats"])


# ── Helpers internes ──────────────────────────────────────────────────────────

class TestToInt:
    def test_valid_string(self):
        assert _to_int("12") == 12

    def test_valid_float_string(self):
        assert _to_int("3.0") == 3

    def test_none(self):
        assert _to_int(None) is None

    def test_non_numeric(self):
        assert _to_int("abc") is None

    def test_integer(self):
        assert _to_int(5) == 5


class TestToFloat:
    def test_valid_string(self):
        assert _to_float("3.5") == 3.5

    def test_none(self):
        assert _to_float(None) is None

    def test_non_numeric(self):
        assert _to_float("abc") is None

    def test_integer(self):
        assert _to_float(4) == 4.0


class TestSumNotNull:
    def test_sums_non_nan(self):
        s = pd.Series([2.0, np.nan, 3.0])
        assert _sum_not_null(s) == 5

    def test_all_nan_returns_nan(self):
        s = pd.Series([np.nan, np.nan])
        assert pd.isna(_sum_not_null(s))

    def test_single_value(self):
        s = pd.Series([7.0])
        assert _sum_not_null(s) == 7

    def test_returns_int(self):
        s = pd.Series([2.0, 3.0])
        result = _sum_not_null(s)
        assert isinstance(result, int)


class TestMaxNotNull:
    def test_returns_max(self):
        s = pd.Series([1.0, np.nan, 5.0, 3.0])
        assert _max_not_null(s) == 5.0

    def test_all_nan_returns_nan(self):
        s = pd.Series([np.nan, np.nan])
        assert pd.isna(_max_not_null(s))

    def test_single_value(self):
        s = pd.Series([4.5])
        assert _max_not_null(s) == 4.5

    def test_returns_float(self):
        s = pd.Series([3.0])
        result = _max_not_null(s)
        assert isinstance(result, float)


class TestModeNotNull:
    def test_returns_most_frequent(self):
        s = pd.Series(["apartments", "apartments", "house"])
        assert _mode_not_null(s) == "apartments"

    def test_single_value(self):
        s = pd.Series(["residential"])
        assert _mode_not_null(s) == "residential"

    def test_all_nan_returns_nan(self):
        s = pd.Series([np.nan, np.nan])
        assert pd.isna(_mode_not_null(s))

    def test_ignores_nan_in_mode(self):
        s = pd.Series(["apartments", np.nan, "apartments"])
        assert _mode_not_null(s) == "apartments"

    def test_returns_str(self):
        s = pd.Series(["house"])
        result = _mode_not_null(s)
        assert isinstance(result, str)


# ── match_osm_to_bdtopo — colonne osm_building ────────────────────────────────

class TestMatchOsmBuilding:
    def test_building_tag_transferred(self):
        """Le tag building OSM est transféré dans la colonne osm_building."""
        bld = _make_buildings([_BLD_SQUARE])
        osm = _make_osm([{"geometry": _OSM_SAME, "building": "apartments"}])
        result = match_osm_to_bdtopo(bld, osm, overlap_threshold=0.5)
        assert result.iloc[0]["osm_building"] == "apartments"

    def test_no_match_gives_nan_building(self):
        """Sans appariement, osm_building est NaN."""
        bld = _make_buildings([_BLD_SQUARE])
        osm = _make_osm([{"geometry": _OSM_FAR, "building": "commercial"}])
        result = match_osm_to_bdtopo(bld, osm, overlap_threshold=0.5)
        assert pd.isna(result.iloc[0]["osm_building"])

    def test_multiple_osm_takes_mode(self):
        """Plusieurs polygones OSM → osm_building = valeur la plus fréquente."""
        bld_poly = Polygon([(0, 0), (20, 0), (20, 20), (0, 20)])
        bld = _make_buildings([bld_poly])
        osm1 = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
        osm2 = Polygon([(10, 0), (20, 0), (20, 10), (10, 10)])
        osm3 = Polygon([(0, 10), (10, 10), (10, 20), (0, 20)])
        osm = _make_osm([
            {"geometry": osm1, "building": "apartments"},
            {"geometry": osm2, "building": "apartments"},
            {"geometry": osm3, "building": "residential"},
        ])
        result = match_osm_to_bdtopo(bld, osm, overlap_threshold=0.5)
        assert result.iloc[0]["osm_building"] == "apartments"

    def test_output_always_has_osm_building_column(self):
        """La colonne osm_building est toujours présente dans le résultat."""
        bld = _make_buildings([_BLD_SQUARE])
        osm = gpd.GeoDataFrame(
            columns=["osm_id", "building", "building_flats", "building_levels", "geometry"],
            crs="EPSG:2154",
        )
        result = match_osm_to_bdtopo(bld, osm)
        assert "osm_building" in result.columns
