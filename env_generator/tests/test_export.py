"""Tests for output/export.py (couche bâtiment fusionnée)."""

import geopandas as gpd
import pandas as pd
from shapely.geometry import Polygon

from src.output.export import merge_buildings, export_buildings


# ── Helpers ───────────────────────────────────────────────────────────────────

_POLY = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])


def _make_all_buildings() -> gpd.GeoDataFrame:
    """Tous les bâtiments de la région (résidentiels + non-résidentiels)."""
    return gpd.GeoDataFrame(
        {
            "ID": ["BAT1", "BAT2", "BAT3"],
            "USAGE1": ["Résidentiel", "Commercial", "Indifférencié"],
            "usage_bdnb": ["residentiel", "travail", None],
            "NB_LOGTS": [0, 0, 0],
            "geometry": [_POLY, _POLY, _POLY],
        },
        crs="EPSG:2154",
    )


def _make_result(**extra_cols) -> gpd.GeoDataFrame:
    """Bâtiments résidentiels avec population allouée (sous-ensemble par ID)."""
    data = {
        "ID": ["BAT1"],
        "NB_LOGTS": [3],
        "population_allouee": [10],
        "menages_alloues": [4],
        "csp_cadres": [6],
        "age_25_39": [5],
        "cell_idx": [1],
        "geometry": [_POLY],
    }
    data.update(extra_cols)
    return gpd.GeoDataFrame(data, crs="EPSG:2154")


# ── merge_buildings ───────────────────────────────────────────────────────────

class TestMergeBuildings:
    def test_keeps_all_buildings(self):
        merged = merge_buildings(_make_all_buildings(), _make_result())
        assert set(merged["ID"]) == {"BAT1", "BAT2", "BAT3"}

    def test_residentiel_flag(self):
        merged = merge_buildings(_make_all_buildings(), _make_result())
        flag = dict(zip(merged["ID"], merged["residentiel"]))
        assert flag["BAT1"] is True or flag["BAT1"] == True  # noqa: E712
        assert not flag["BAT2"] and not flag["BAT3"]

    def test_population_filled_zero_for_non_residential(self):
        merged = merge_buildings(_make_all_buildings(), _make_result())
        pop = dict(zip(merged["ID"], merged["population_allouee"]))
        assert pop["BAT1"] == 10
        assert pop["BAT2"] == 0 and pop["BAT3"] == 0

    def test_estimated_nb_logts_wins_over_raw(self):
        merged = merge_buildings(_make_all_buildings(), _make_result())
        nb = dict(zip(merged["ID"], merged["NB_LOGTS"]))
        assert nb["BAT1"] == 3            # valeur estimée (result), pas la brute (0)

    def test_usage_bdnb_preserved(self):
        merged = merge_buildings(_make_all_buildings(), _make_result())
        assert "usage_bdnb" in merged.columns

    def test_csp_and_age_present(self):
        merged = merge_buildings(_make_all_buildings(), _make_result())
        assert "csp_cadres" in merged.columns and "age_25_39" in merged.columns
        assert dict(zip(merged["ID"], merged["csp_cadres"]))["BAT2"] == 0


# ── export_buildings ──────────────────────────────────────────────────────────

class TestExportBuildings:
    def _export(self, tmp_path):
        export_buildings(merge_buildings(_make_all_buildings(), _make_result()), tmp_path)

    def test_creates_buildings_files(self, tmp_path):
        self._export(tmp_path)
        for ext in ("geojson", "csv", "shp"):
            assert (tmp_path / f"buildings.{ext}").exists()

    def test_creates_light_files(self, tmp_path):
        self._export(tmp_path)
        for ext in ("geojson", "csv", "shp"):
            assert (tmp_path / f"buildings_light.{ext}").exists()

    def test_no_legacy_filenames(self, tmp_path):
        self._export(tmp_path)
        assert not (tmp_path / "buildings_full.geojson").exists()
        assert not (tmp_path / "buildings_all.geojson").exists()

    def test_buildings_csv_has_all_rows(self, tmp_path):
        self._export(tmp_path)
        df = pd.read_csv(tmp_path / "buildings.csv")
        assert len(df) == 3
        assert "geometry" not in df.columns

    def test_light_csv_is_minimal(self, tmp_path):
        self._export(tmp_path)
        df = pd.read_csv(tmp_path / "buildings_light.csv")
        assert {"ID", "residentiel", "population_allouee"}.issubset(df.columns)
        assert "USAGE1" not in df.columns

    def test_shp_column_names_max_10_chars(self, tmp_path):
        self._export(tmp_path)
        gdf = gpd.read_file(tmp_path / "buildings.shp")
        for col in gdf.columns:
            assert len(col) <= 10, f"Colonne trop longue : '{col}'"

    def test_creates_output_dir_if_missing(self, tmp_path):
        out = tmp_path / "new_subdir"
        export_buildings(merge_buildings(_make_all_buildings(), _make_result()), out)
        assert out.exists()
