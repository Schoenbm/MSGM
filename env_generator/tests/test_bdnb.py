"""Tests du loader BDNB (récupération des lieux de travail)."""

import pandas as pd

from src.loaders.bdnb import BDNB_EMPLOYMENT_USAGES, _employment_ids, load_bdnb_employment_ids


def test_employment_ids_filters_by_usage():
    comp = pd.DataFrame({
        "batiment_groupe_id": ["g1", "g2", "g3", "g4"],
        "usage_principal_bdnb_open": ["Tertiaire", "Résidentiel individuel", "Secondaire", None],
    })
    rel = pd.DataFrame({
        "batiment_groupe_id": ["g1", "g2", "g3", "g4"],
        "bdtopo_bat_cleabs": ["BAT_T", "BAT_R", "BAT_S", "BAT_NA"],
    })
    ids = _employment_ids(rel, comp, set(BDNB_EMPLOYMENT_USAGES))
    assert ids == {"BAT_T", "BAT_S"}   # tertiaire + secondaire ; pas résidentiel ni NaN


def test_employment_ids_maps_group_to_multiple_buildings():
    # un groupe d'emploi peut couvrir plusieurs bâtiments BD TOPO
    comp = pd.DataFrame({"batiment_groupe_id": ["g1"], "usage_principal_bdnb_open": ["Tertiaire"]})
    rel = pd.DataFrame({
        "batiment_groupe_id": ["g1", "g1", "g2"],
        "bdtopo_bat_cleabs": ["BAT_A", "BAT_B", "BAT_C"],
    })
    assert _employment_ids(rel, comp, set(BDNB_EMPLOYMENT_USAGES)) == {"BAT_A", "BAT_B"}


def test_load_returns_empty_when_file_absent(tmp_path):
    assert load_bdnb_employment_ids(tmp_path / "nope.gpkg") == set()
