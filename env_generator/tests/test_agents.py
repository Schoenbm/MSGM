"""Tests de la génération de population d'agents (matching/agents.py)."""

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point, Polygon

from src.matching.agents import (
    ACT_AUCUNE,
    ACT_COLLEGE,
    ACT_CRECHE,
    ACT_ECOLE,
    ACT_LYCEE,
    ACT_TRAVAIL,
    RETIREMENT_AGE,
    _classify_activity,
    generate_agents,
)

CRS = "EPSG:2154"


def _square(x: float, y: float, size: float = 10.0) -> Polygon:
    return Polygon([(x, y), (x + size, y), (x + size, y + size), (x, y + size)])


def _residential(pop: int, ages: dict, csps: dict, bid: str = "HOME1",
                 x: float = 0.0, y: float = 0.0) -> gpd.GeoDataFrame:
    row = {"ID": bid, "geometry": _square(x, y), "population_allouee": pop}
    for col, val in ages.items():
        row[col] = val
    for col, val in csps.items():
        row[col] = val
    return gpd.GeoDataFrame([row], geometry="geometry", crs=CRS)


def _all_buildings(specs: list) -> gpd.GeoDataFrame:
    rows = []
    for bid, usage1, x, y in specs:
        rows.append({
            "ID": bid, "USAGE1": usage1, "USAGE2": None,
            "NB_ETAGES": 1, "HAUTEUR": 3.0, "geometry": _square(x, y, 20),
        })
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=CRS)


def _education(specs: list) -> gpd.GeoDataFrame:
    """specs : (equip_id, kind, x, y)."""
    rows = [{"equip_id": s[0], "kind": s[1], "geometry": Point(s[2], s[3])} for s in specs]
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=CRS)


def test_one_row_per_individual():
    res = _residential(10, {"age_25_39": 10}, {"csp_employes": 10})
    buildings = _all_buildings([("W1", "Industriel", 100, 0)])
    agents = generate_agents(res, buildings, seed=1)
    assert len(agents) == 10
    assert {"agent_id", "home_id", "age", "age_band", "csp", "activity",
            "is_worker", "dest_id"}.issubset(agents.columns)


def test_children_go_to_creche_and_school():
    # 0-2 ans (crèche) + 6-10 ans (école)
    res = _residential(20, {"age_0_2": 10, "age_6_10": 10}, {})
    buildings = _all_buildings([("W1", "Industriel", 100, 0)])
    edu = _education([("c1", ACT_CRECHE, 50, 0), ("e1", ACT_ECOLE, 80, 0)])
    agents = generate_agents(res, buildings, education=edu, seed=2)

    creche = agents[agents["activity"] == ACT_CRECHE]
    ecole = agents[agents["activity"] == ACT_ECOLE]
    assert (creche["age"] <= 2).all()
    assert ecole["age"].between(3, 17).all()
    assert (creche["dest_id"] == "c1").all()
    assert (ecole["dest_id"] == "e1").all()


def test_college_and_lycee_use_specific_facilities():
    # tranche 11-17 → collège (11-14) + lycée (15-17)
    res = _residential(40, {"age_11_17": 40}, {})
    buildings = _all_buildings([("W1", "Industriel", 100, 0)])
    edu = _education([("col", ACT_COLLEGE, 50, 0), ("lyc", ACT_LYCEE, 80, 0)])
    agents = generate_agents(res, buildings, education=edu, seed=5)

    col = agents[agents["activity"] == ACT_COLLEGE]
    lyc = agents[agents["activity"] == ACT_LYCEE]
    assert col["age"].between(11, 14).all()
    assert lyc["age"].between(15, 17).all()
    assert (col["dest_id"] == "col").all()
    assert (lyc["dest_id"] == "lyc").all()


def test_college_lycee_fall_back_to_ecole_pool():
    # OSM ne fournit que des écoles génériques → collège/lycée y sont rattachés
    res = _residential(40, {"age_11_17": 40}, {})
    buildings = _all_buildings([("W1", "Industriel", 100, 0)])
    edu = _education([("e1", ACT_ECOLE, 50, 0)])
    agents = generate_agents(res, buildings, education=edu, seed=5)
    school_age = agents[agents["activity"].isin([ACT_COLLEGE, ACT_LYCEE])]
    assert (school_age["dest_id"] == "e1").all()


def test_children_without_education_have_activity_but_no_dest():
    res = _residential(8, {"age_6_10": 8}, {})
    buildings = _all_buildings([("W1", "Industriel", 100, 0)])
    agents = generate_agents(res, buildings, education=None, seed=1)
    assert (agents["activity"] == ACT_ECOLE).all()
    assert agents["dest_id"].isna().all()


def test_retirees_over_62_do_not_work():
    # actifs (csp_cadres) mais tous âgés (80+) → retraités, pas de travail
    res = _residential(20, {"age_80p": 20}, {"csp_cadres": 20})
    buildings = _all_buildings([("W1", "Industriel", 100, 0)])
    agents = generate_agents(res, buildings, seed=1)
    assert (agents["age"] > RETIREMENT_AGE).all()
    assert (agents["activity"] == ACT_AUCUNE).all()
    assert not agents["is_worker"].any()
    assert agents["dest_id"].isna().all()


def test_working_age_actives_work():
    res = _residential(20, {"age_40_54": 20}, {"csp_cadres": 20})
    buildings = _all_buildings([("W1", "Industriel", 100, 0)])
    agents = generate_agents(res, buildings, seed=1)
    assert (agents["activity"] == ACT_TRAVAIL).all()
    assert agents["is_worker"].all()
    assert (agents["dest_id"] == "W1").all()


def test_inactives_have_no_destination():
    res = _residential(15, {"age_40_54": 15}, {"csp_autres_inactifs": 15})
    buildings = _all_buildings([("W1", "Industriel", 100, 0)])
    agents = generate_agents(res, buildings, seed=1)
    assert (agents["activity"] == ACT_AUCUNE).all()
    assert agents["dest_id"].isna().all()


def test_age_drawn_within_band():
    res = _residential(30, {"age_25_39": 30}, {"csp_employes": 30})
    buildings = _all_buildings([("W1", "Industriel", 100, 0)])
    agents = generate_agents(res, buildings, seed=1)
    assert agents["age"].between(25, 39).all()
    assert (agents["age_band"] == "age_25_39").all()


def test_population_conserved_across_buildings():
    res1 = _residential(15, {"age_25_39": 15}, {"csp_employes": 15}, bid="A", x=0, y=0)
    res2 = _residential(25, {"age_40_54": 25}, {"csp_cadres": 25}, bid="B", x=500, y=0)
    res = gpd.GeoDataFrame(pd.concat([res1, res2], ignore_index=True),
                           geometry="geometry", crs=CRS)
    buildings = _all_buildings([("W1", "Industriel", 100, 0)])
    agents = generate_agents(res, buildings, seed=1)
    assert len(agents) == 40
    assert set(agents["home_id"]) == {"A", "B"}


def test_empty_when_no_population():
    res = _residential(0, {"age_25_39": 0}, {"csp_employes": 0})
    buildings = _all_buildings([("W1", "Industriel", 100, 0)])
    agents = generate_agents(res, buildings)
    assert agents.empty
    assert "activity" in agents.columns


def test_reproducible_with_seed():
    res = _residential(40, {"age_25_39": 20, "age_6_10": 10, "age_0_2": 10},
                       {"csp_cadres": 12, "csp_employes": 8})
    buildings = _all_buildings([("W1", "Industriel", 100, 0)])
    edu = _education([("c1", ACT_CRECHE, 50, 0), ("e1", ACT_ECOLE, 80, 0)])
    a1 = generate_agents(res, buildings, education=edu, seed=11)
    a2 = generate_agents(res, buildings, education=edu, seed=11)
    assert list(a1["age"]) == list(a2["age"])
    assert list(a1["csp"]) == list(a2["csp"])
    assert list(a1["dest_id"].fillna("none")) == list(a2["dest_id"].fillna("none"))


def test_retirement_boundary_exactly_62_and_63():
    # 62 ans actif → travail ; 63 ans actif → aucune (retraité)
    agents = gpd.GeoDataFrame(
        {"age": [62, 63], "csp": ["csp_cadres", "csp_cadres"]},
        geometry=[Point(0, 0), Point(0, 0)], crs=CRS,
    )
    activity = _classify_activity(agents)
    assert activity[0] == ACT_TRAVAIL
    assert activity[1] == ACT_AUCUNE


def test_activity_age_boundaries():
    # 2→crèche, 3 et 10→école, 11 et 14→collège, 15 et 17→lycée, 18 inactif→aucune
    ages = [2, 3, 10, 11, 14, 15, 17, 18]
    agents = gpd.GeoDataFrame(
        {"age": ages, "csp": ["mineur"] * 7 + ["csp_autres_inactifs"]},
        geometry=[Point(0, 0)] * len(ages), crs=CRS,
    )
    activity = _classify_activity(agents)
    assert list(activity) == [
        ACT_CRECHE, ACT_ECOLE, ACT_ECOLE, ACT_COLLEGE, ACT_COLLEGE,
        ACT_LYCEE, ACT_LYCEE, ACT_AUCUNE,
    ]


def test_global_fallback_avoids_unknown_age():
    # Bâtiment A : pop=1 sans aucune tranche d'âge (toutes à 0) — sans repli il
    # produirait age=-1. Bâtiment B fournit la distribution globale.
    a = _residential(1, {"age_25_39": 0}, {"csp_employes": 0}, bid="A", x=0, y=0)
    b = _residential(50, {"age_25_39": 50}, {"csp_employes": 50}, bid="B", x=300, y=0)
    res = gpd.GeoDataFrame(pd.concat([a, b], ignore_index=True),
                           geometry="geometry", crs=CRS)
    buildings = _all_buildings([("W1", "Industriel", 100, 0)])
    agents = generate_agents(res, buildings, seed=1)
    assert (agents["age"] >= 0).all()           # aucun âge inconnu
    assert "inconnu" not in set(agents["csp"])  # aucune CSP inconnue


def test_geometry_is_home_point():
    res = _residential(5, {"age_25_39": 5}, {"csp_employes": 5})  # carré [0,10]² -> (5,5)
    buildings = _all_buildings([("W1", "Industriel", 100, 0)])
    agents = generate_agents(res, buildings, seed=1)
    assert np.isclose(agents.geometry.iloc[0].x, 5.0)
    assert np.isclose(agents.geometry.iloc[0].y, 5.0)
