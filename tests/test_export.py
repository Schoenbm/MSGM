"""Tests for output/export.py."""

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Polygon

from src.output.export import export_results, export_all_buildings


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_result(**extra_cols) -> gpd.GeoDataFrame:
    poly = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    data = {
        "ID": ["BAT1", "BAT2"],
        "NB_LOGTS": [3, 5],
        "population_allouee": [10, 20],
        "cell_idx": [1, 1],
        "geometry": [poly, poly],
    }
    data.update(extra_cols)
    return gpd.GeoDataFrame(data, crs="EPSG:2154")


# ── Fichiers créés ────────────────────────────────────────────────────────────

class TestExportFiles:
    def test_creates_four_files(self, tmp_path):
        export_results(_make_result(), tmp_path)
        assert (tmp_path / "buildings_light.geojson").exists()
        assert (tmp_path / "buildings_light.csv").exists()
        assert (tmp_path / "buildings_full.geojson").exists()
        assert (tmp_path / "buildings_full.csv").exists()

    def test_creates_output_dir_if_missing(self, tmp_path):
        out = tmp_path / "new_subdir"
        assert not out.exists()
        export_results(_make_result(), out)
        assert out.exists()


# ── Export allégé ─────────────────────────────────────────────────────────────

class TestLightExport:
    def test_light_csv_has_id_and_population(self, tmp_path):
        export_results(_make_result(), tmp_path)
        df = pd.read_csv(tmp_path / "buildings_light.csv")
        assert "ID" in df.columns
        assert "population_allouee" in df.columns

    def test_light_csv_has_no_geometry(self, tmp_path):
        export_results(_make_result(), tmp_path)
        df = pd.read_csv(tmp_path / "buildings_light.csv")
        assert "geometry" not in df.columns

    def test_light_csv_has_no_extra_columns(self, tmp_path):
        export_results(_make_result(), tmp_path)
        df = pd.read_csv(tmp_path / "buildings_light.csv")
        assert set(df.columns) == {"ID", "population_allouee"}

    def test_light_geojson_row_count(self, tmp_path):
        export_results(_make_result(), tmp_path)
        gdf = gpd.read_file(tmp_path / "buildings_light.geojson")
        assert len(gdf) == 2


# ── Export complet ────────────────────────────────────────────────────────────

class TestFullExport:
    def test_full_csv_drops_cell_idx(self, tmp_path):
        export_results(_make_result(), tmp_path)
        df = pd.read_csv(tmp_path / "buildings_full.csv")
        assert "cell_idx" not in df.columns

    def test_full_csv_keeps_nb_logts(self, tmp_path):
        export_results(_make_result(), tmp_path)
        df = pd.read_csv(tmp_path / "buildings_full.csv")
        assert "NB_LOGTS" in df.columns

    def test_full_csv_has_no_geometry(self, tmp_path):
        export_results(_make_result(), tmp_path)
        df = pd.read_csv(tmp_path / "buildings_full.csv")
        assert "geometry" not in df.columns

    def test_full_csv_row_count(self, tmp_path):
        export_results(_make_result(), tmp_path)
        df = pd.read_csv(tmp_path / "buildings_full.csv")
        assert len(df) == 2

    def test_menages_alloues_in_full_when_present(self, tmp_path):
        result = _make_result(menages_alloues=[4, 8])
        export_results(result, tmp_path)
        df = pd.read_csv(tmp_path / "buildings_full.csv")
        assert "menages_alloues" in df.columns

    def test_datetime_columns_sanitized(self, tmp_path):
        result = _make_result()
        result["date_col"] = pd.to_datetime(["2024-01-01", "2024-06-01"])
        export_results(result, tmp_path)
        # Should not raise — datetime converted to string
        gdf = gpd.read_file(tmp_path / "buildings_full.geojson")
        assert "date_col" in gdf.columns


# ── Export shapefile ──────────────────────────────────────────────────────────

class TestShapefileExport:
    def test_creates_light_shp(self, tmp_path):
        export_results(_make_result(), tmp_path)
        assert (tmp_path / "buildings_light.shp").exists()

    def test_creates_full_shp(self, tmp_path):
        export_results(_make_result(), tmp_path)
        assert (tmp_path / "buildings_full.shp").exists()

    def test_shp_sidecar_files_created(self, tmp_path):
        export_results(_make_result(), tmp_path)
        assert (tmp_path / "buildings_light.dbf").exists()
        assert (tmp_path / "buildings_light.shx").exists()
        assert (tmp_path / "buildings_light.prj").exists()

    def test_population_column_renamed_in_shp(self, tmp_path):
        export_results(_make_result(), tmp_path)
        gdf = gpd.read_file(tmp_path / "buildings_light.shp")
        assert "pop_allou" in gdf.columns
        assert "population_allouee" not in gdf.columns

    def test_all_column_names_max_10_chars(self, tmp_path):
        export_results(_make_result(), tmp_path)
        gdf = gpd.read_file(tmp_path / "buildings_full.shp")
        for col in gdf.columns:
            assert len(col) <= 10, f"Colonne trop longue : '{col}' ({len(col)} chars)"

    def test_shp_row_count_matches_input(self, tmp_path):
        export_results(_make_result(), tmp_path)
        gdf = gpd.read_file(tmp_path / "buildings_light.shp")
        assert len(gdf) == 2

    def test_zero_population_buildings_included_in_shp(self, tmp_path):
        """Bâtiments avec population = 0 présents dans le shapefile."""
        result = _make_result()
        result.loc[0, "population_allouee"] = 0
        export_results(result, tmp_path)
        gdf = gpd.read_file(tmp_path / "buildings_light.shp")
        assert len(gdf) == 2
        assert (gdf["pop_allou"] == 0).any()


# ── export_all_buildings ──────────────────────────────────────────────────────

def _make_all_buildings() -> gpd.GeoDataFrame:
    """GeoDataFrame mixte résidentiel + non-résidentiel."""
    poly = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    return gpd.GeoDataFrame(
        {
            "ID": ["BAT1", "BAT2", "BAT3"],
            "USAGE1": ["Résidentiel", "Commercial", "Industriel"],
            "geometry": [poly, poly, poly],
        },
        crs="EPSG:2154",
    )


class TestExportAllBuildings:
    def test_creates_shp(self, tmp_path):
        export_all_buildings(_make_all_buildings(), tmp_path)
        assert (tmp_path / "buildings_all.shp").exists()

    def test_creates_geojson(self, tmp_path):
        export_all_buildings(_make_all_buildings(), tmp_path)
        assert (tmp_path / "buildings_all.geojson").exists()

    def test_creates_output_dir_if_missing(self, tmp_path):
        out = tmp_path / "new_dir"
        export_all_buildings(_make_all_buildings(), out)
        assert out.exists()

    def test_all_buildings_present_in_shp(self, tmp_path):
        """Les 3 bâtiments (résidentiel + commercial + industriel) sont exportés."""
        export_all_buildings(_make_all_buildings(), tmp_path)
        gdf = gpd.read_file(tmp_path / "buildings_all.shp")
        assert len(gdf) == 3

    def test_usage_column_preserved(self, tmp_path):
        export_all_buildings(_make_all_buildings(), tmp_path)
        gdf = gpd.read_file(tmp_path / "buildings_all.shp")
        assert "USAGE1" in gdf.columns

    def test_column_names_max_10_chars(self, tmp_path):
        export_all_buildings(_make_all_buildings(), tmp_path)
        gdf = gpd.read_file(tmp_path / "buildings_all.shp")
        for col in gdf.columns:
            assert len(col) <= 10, f"Colonne trop longue : '{col}' ({len(col)} chars)"
