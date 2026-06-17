"""Tests unitaires pour src/output/casualties.py."""

import math
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point

from src.output.casualties import (
    _aggregate_by_iris,
    _apply_damage_matrix,
    _load_damage_csv,
    _load_population,
    compute_casualties,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_pop_gpkg(tmp_path: Path) -> Path:
    """GeoPackage minimal avec 4 bâtiments répartis sur 2 IRIS."""
    gdf = gpd.GeoDataFrame(
        {
            "ID": ["B001", "B002", "B003", "B004"],
            "population_allouee": [10, 20, 5, 100],
            "code_iris": ["IRIS_A", "IRIS_A", "IRIS_B", "IRIS_B"],
        },
        geometry=[Point(0, 0), Point(1, 0), Point(2, 0), Point(3, 0)],
        crs="EPSG:2154",
    )
    out = tmp_path / "result_iris.gpkg"
    gdf.to_file(out, driver="GPKG")
    return out


def _make_damage_csv(tmp_path: Path, rows: list[tuple[str, str]]) -> Path:
    """CSV damage minimal."""
    out = tmp_path / "damage.csv"
    df = pd.DataFrame(rows, columns=["ID", "damage_level"])
    df.to_csv(out, index=False)
    return out


# ── Tests de chargement ───────────────────────────────────────────────────────

def test_missing_id_column_raises(tmp_path):
    bad_csv = tmp_path / "bad.csv"
    bad_csv.write_text("foo,damage_level\nB001,D1\n")
    with pytest.raises(ValueError, match="Colonne 'ID'"):
        _load_damage_csv(bad_csv)


def test_missing_damage_level_column_raises(tmp_path):
    bad_csv = tmp_path / "bad.csv"
    bad_csv.write_text("ID,level\nB001,D1\n")
    with pytest.raises(ValueError, match="Colonne 'damage_level'"):
        _load_damage_csv(bad_csv)


def test_invalid_damage_level_raises(tmp_path):
    csv = _make_damage_csv(tmp_path, [("B001", "D6"), ("B002", "D1")])
    with pytest.raises(ValueError, match="inconnu"):
        _load_damage_csv(csv)


def test_load_damage_csv_normalizes_case(tmp_path):
    csv = tmp_path / "dmg.csv"
    csv.write_text("ID,damage_level\nB001,d3\nB002,D1\n")
    df = _load_damage_csv(csv)
    assert list(df["damage_level"]) == ["D3", "D1"]


def test_load_damage_csv_deduplicates(tmp_path):
    csv = tmp_path / "dmg.csv"
    csv.write_text("ID,damage_level\nB001,D1\nB001,D3\n")
    df = _load_damage_csv(csv)
    assert len(df) == 1
    assert df.iloc[0]["damage_level"] == "D1"


# ── Tests métier ──────────────────────────────────────────────────────────────

def test_sum_categories_equals_population(tmp_path):
    """P0+P1+P2+P3+P4 doit exactement égaler population_allouee pour chaque bâtiment."""
    pop_gpkg = _make_pop_gpkg(tmp_path)
    damage_csv = _make_damage_csv(
        tmp_path,
        [("B001", "D1"), ("B002", "D3"), ("B003", "D5"), ("B004", "D4")],
    )
    compute_casualties(damage_csv, pop_gpkg, tmp_path / "out")
    result = pd.read_csv(tmp_path / "out" / "casualties_buildings.csv")
    row_sums = result[["P0", "P1", "P2", "P3", "P4"]].sum(axis=1)
    pd.testing.assert_series_equal(
        row_sums.reset_index(drop=True),
        result["population_allouee"].reset_index(drop=True),
        check_names=False,
    )


def test_homeless_only_for_d3_d4_d5(tmp_path):
    """D1 et D2 → sans_abris = 0 ; D3/D4/D5 → sans_abris = population_allouee."""
    pop_gpkg = _make_pop_gpkg(tmp_path)
    damage_csv = _make_damage_csv(
        tmp_path,
        [("B001", "D1"), ("B002", "D2"), ("B003", "D3"), ("B004", "D5")],
    )
    compute_casualties(damage_csv, pop_gpkg, tmp_path / "out")
    result = pd.read_csv(tmp_path / "out" / "casualties_buildings.csv")

    for _, row in result.iterrows():
        if row["damage_level"] in ("D1", "D2"):
            assert row["sans_abris"] == 0, f"{row['ID']} D1/D2 devrait avoir sans_abris=0"
        else:
            assert row["sans_abris"] == row["population_allouee"], (
                f"{row['ID']} {row['damage_level']} devrait avoir sans_abris==pop"
            )


def test_aggregate_by_iris(tmp_path):
    """Vérifier la somme des colonnes dans casualties_iris.csv."""
    pop_gpkg = _make_pop_gpkg(tmp_path)
    damage_csv = _make_damage_csv(
        tmp_path,
        [("B001", "D1"), ("B002", "D2"), ("B003", "D3"), ("B004", "D5")],
    )
    compute_casualties(damage_csv, pop_gpkg, tmp_path / "out")
    bldg = pd.read_csv(tmp_path / "out" / "casualties_buildings.csv")
    iris = pd.read_csv(tmp_path / "out" / "casualties_iris.csv")

    for iris_code, grp in bldg.groupby("code_iris"):
        row = iris[iris["code_iris"] == iris_code].iloc[0]
        assert row["pop_exposee"] == grp["population_allouee"].sum()
        assert row["P4_morts"] == grp["P4"].sum()
        assert row["sans_abris"] == grp["sans_abris"].sum()


def test_unmatched_ids_are_logged_not_crashed(tmp_path):
    """Des IDs inconnus dans le CSV de dommages ne doivent pas planter."""
    pop_gpkg = _make_pop_gpkg(tmp_path)
    damage_csv = _make_damage_csv(
        tmp_path,
        [("B001", "D1"), ("INCONNU_XYZ", "D3")],
    )
    # Ne doit pas lever d'exception
    compute_casualties(damage_csv, pop_gpkg, tmp_path / "out")
    result = pd.read_csv(tmp_path / "out" / "casualties_buildings.csv")
    assert len(result) == 1
    assert result.iloc[0]["ID"] == "B001"


def test_all_damage_levels_produce_valid_fractions(tmp_path):
    """Toutes les valeurs P0..P4 doivent être >= 0 pour chaque niveau de dommage."""
    pop_gpkg = _make_pop_gpkg(tmp_path)
    damage_csv = _make_damage_csv(
        tmp_path,
        [("B001", "D1"), ("B002", "D2"), ("B003", "D3"), ("B004", "D4")],
    )
    compute_casualties(damage_csv, pop_gpkg, tmp_path / "out")
    result = pd.read_csv(tmp_path / "out" / "casualties_buildings.csv")
    for col in ["P0", "P1", "P2", "P3", "P4", "sans_abris"]:
        assert (result[col] >= 0).all(), f"Valeurs négatives dans {col}"


def test_iris_csv_columns(tmp_path):
    """Vérifier que casualties_iris.csv contient exactement les bonnes colonnes."""
    pop_gpkg = _make_pop_gpkg(tmp_path)
    damage_csv = _make_damage_csv(tmp_path, [("B001", "D3")])
    compute_casualties(damage_csv, pop_gpkg, tmp_path / "out")
    iris = pd.read_csv(tmp_path / "out" / "casualties_iris.csv")
    expected = {
        "code_iris", "pop_exposee", "P0_indemnes", "P1_blesses_legers",
        "P2_hospitalises", "P3_blesses_graves", "P4_morts", "sans_abris",
    }
    assert set(iris.columns) == expected
