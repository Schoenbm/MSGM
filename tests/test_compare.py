"""Tests for output/compare.py — validation Filosofi vs IRIS 2022."""

from pathlib import Path
from unittest.mock import patch

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Polygon

from src.output.compare import compare_results


# ── Helpers ───────────────────────────────────────────────────────────────────

def _poly(x0, y0, x1, y1):
    return Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])


def _make_filosofi_result(buildings: list[dict]) -> gpd.GeoDataFrame:
    """Buildings with population_allouee, centroids placed inside IRIS polygons."""
    records = []
    for b in buildings:
        cx, cy = b["cx"], b["cy"]
        records.append({
            "ID": b.get("ID", "BAT"),
            "population_allouee": b["pop"],
            "geometry": _poly(cx - 1, cy - 1, cx + 1, cy + 1),
        })
    return gpd.GeoDataFrame(records, crs="EPSG:2154")


def _make_iris(iris_list: list[dict]) -> gpd.GeoDataFrame:
    records = []
    for iris in iris_list:
        records.append({
            "CODE_IRIS": iris["code"],
            "Ind_total": iris["pop"],
            "geometry": iris["geometry"],
        })
    return gpd.GeoDataFrame(records, crs="EPSG:2154")


def _no_map(merged, out_path):
    """Stub that skips the matplotlib/contextily map generation."""
    pass


# ── Métriques de base ─────────────────────────────────────────────────────────

class TestCompareMetrics:
    def test_csv_written(self, tmp_path):
        iris1_poly = _poly(0, 0, 100, 100)
        filosofi = _make_filosofi_result([{"cx": 50, "cy": 50, "pop": 80}])
        iris = _make_iris([{"code": "381000001", "pop": 100.0, "geometry": iris1_poly}])
        with patch("src.output.compare._make_validation_map", _no_map):
            csv = compare_results(filosofi, iris, tmp_path)
        assert csv.exists()

    def test_csv_contains_expected_columns(self, tmp_path):
        iris1_poly = _poly(0, 0, 100, 100)
        filosofi = _make_filosofi_result([{"cx": 50, "cy": 50, "pop": 80}])
        iris = _make_iris([{"code": "381000001", "pop": 100.0, "geometry": iris1_poly}])
        with patch("src.output.compare._make_validation_map", _no_map):
            csv = compare_results(filosofi, iris, tmp_path)
        df = pd.read_csv(csv, dtype={"CODE_IRIS": str})
        for col in ("CODE_IRIS", "pop_filosofi", "pop_iris_2022", "diff", "erreur_rel"):
            assert col in df.columns

    def test_diff_computed_correctly(self, tmp_path):
        # 2 buildings in IRIS, total filosofi = 30 + 70 = 100. IRIS says 120. diff = -20
        iris1_poly = _poly(0, 0, 100, 100)
        filosofi = _make_filosofi_result([
            {"cx": 25, "cy": 50, "pop": 30},
            {"cx": 75, "cy": 50, "pop": 70},
        ])
        iris = _make_iris([{"code": "381000001", "pop": 120.0, "geometry": iris1_poly}])
        with patch("src.output.compare._make_validation_map", _no_map):
            csv = compare_results(filosofi, iris, tmp_path)
        df = pd.read_csv(csv, dtype={"CODE_IRIS": str})
        row = df[df["CODE_IRIS"] == "381000001"].iloc[0]
        assert row["pop_filosofi"] == 100.0
        assert row["diff"] == pytest.approx(-20.0)

    def test_erreur_rel_computed_correctly(self, tmp_path):
        # filosofi=80, iris=100 → erreur_rel = (80-100)/100*100 = -20%
        iris1_poly = _poly(0, 0, 100, 100)
        filosofi = _make_filosofi_result([{"cx": 50, "cy": 50, "pop": 80}])
        iris = _make_iris([{"code": "381000001", "pop": 100.0, "geometry": iris1_poly}])
        with patch("src.output.compare._make_validation_map", _no_map):
            csv = compare_results(filosofi, iris, tmp_path)
        df = pd.read_csv(csv, dtype={"CODE_IRIS": str})
        row = df[df["CODE_IRIS"] == "381000001"].iloc[0]
        assert row["erreur_rel"] == pytest.approx(-20.0)

    def test_iris_with_no_buildings_gets_zero_filosofi(self, tmp_path):
        # IRIS A has buildings, IRIS B has none
        iris_a = _poly(0, 0, 50, 100)
        iris_b = _poly(50, 0, 100, 100)
        filosofi = _make_filosofi_result([{"cx": 25, "cy": 50, "pop": 60}])
        iris = _make_iris([
            {"code": "381000001", "pop": 60.0, "geometry": iris_a},
            {"code": "381000002", "pop": 80.0, "geometry": iris_b},
        ])
        with patch("src.output.compare._make_validation_map", _no_map):
            csv = compare_results(filosofi, iris, tmp_path)
        df = pd.read_csv(csv, dtype={"CODE_IRIS": str})
        b_row = df[df["CODE_IRIS"] == "381000002"].iloc[0]
        assert b_row["pop_filosofi"] == 0.0

    def test_multiple_iris_independent(self, tmp_path):
        iris_a = _poly(0, 0, 50, 100)
        iris_b = _poly(50, 0, 100, 100)
        filosofi = _make_filosofi_result([
            {"cx": 25, "cy": 50, "pop": 100},
            {"cx": 75, "cy": 50, "pop": 200},
        ])
        iris = _make_iris([
            {"code": "381000001", "pop": 100.0, "geometry": iris_a},
            {"code": "381000002", "pop": 200.0, "geometry": iris_b},
        ])
        with patch("src.output.compare._make_validation_map", _no_map):
            csv = compare_results(filosofi, iris, tmp_path)
        df = pd.read_csv(csv, dtype={"CODE_IRIS": str})
        for _, row in df.iterrows():
            assert row["diff"] == pytest.approx(0.0)

    def test_erreur_rel_zero_for_iris_with_no_population(self, tmp_path):
        # IRIS with pop=0: erreur_rel should be 0 (no division by zero)
        iris1_poly = _poly(0, 0, 100, 100)
        filosofi = _make_filosofi_result([{"cx": 50, "cy": 50, "pop": 0}])
        iris = _make_iris([{"code": "381000001", "pop": 0.0, "geometry": iris1_poly}])
        with patch("src.output.compare._make_validation_map", _no_map):
            csv = compare_results(filosofi, iris, tmp_path)
        df = pd.read_csv(csv, dtype={"CODE_IRIS": str})
        assert df.iloc[0]["erreur_rel"] == pytest.approx(0.0)

    def test_creates_output_dir_if_missing(self, tmp_path):
        out = tmp_path / "compare_output"
        iris1_poly = _poly(0, 0, 100, 100)
        filosofi = _make_filosofi_result([{"cx": 50, "cy": 50, "pop": 50}])
        iris = _make_iris([{"code": "381000001", "pop": 50.0, "geometry": iris1_poly}])
        with patch("src.output.compare._make_validation_map", _no_map):
            compare_results(filosofi, iris, out)
        assert out.exists()
