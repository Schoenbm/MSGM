"""Tests du loader BDNB (qualification des bâtiments)."""

import pandas as pd

from src.loaders.bdnb import _usage_map, employment_ids, load_bdnb_building_usage


def test_usage_map_categories():
    comp = pd.DataFrame({
        "batiment_groupe_id": ["g1", "g2", "g3", "g4", "g5"],
        "usage_principal_bdnb_open": [
            "Tertiaire", "Résidentiel collectif", "Secondaire", "Dépendance", None,
        ],
    })
    rel = pd.DataFrame({
        "batiment_groupe_id": ["g1", "g2", "g3", "g4", "g5"],
        "bdtopo_bat_cleabs": ["BAT_T", "BAT_R", "BAT_S", "BAT_D", "BAT_NA"],
    })
    m = _usage_map(rel, comp)
    assert m == {
        "BAT_T": "travail", "BAT_R": "residentiel",
        "BAT_S": "travail", "BAT_D": "annexe",
    }  # le groupe sans usage mappable (g5) est ignoré


def test_usage_map_one_group_many_buildings():
    comp = pd.DataFrame({"batiment_groupe_id": ["g1"], "usage_principal_bdnb_open": ["Tertiaire"]})
    rel = pd.DataFrame({
        "batiment_groupe_id": ["g1", "g1", "g2"],
        "bdtopo_bat_cleabs": ["A", "B", "C"],
    })
    assert _usage_map(rel, comp) == {"A": "travail", "B": "travail"}


def test_employment_ids_from_usage():
    usage = {"A": "travail", "B": "residentiel", "C": "travail", "D": "annexe"}
    assert employment_ids(usage) == {"A", "C"}


def test_load_returns_empty_when_file_absent(tmp_path):
    assert load_bdnb_building_usage(tmp_path / "nope.gpkg") == {}
