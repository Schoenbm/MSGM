"""Tests for output/export.py."""

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Polygon

from src.output.export import export_results


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
