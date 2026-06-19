"""Tests de l'affectation spatiale des équipements (matching/workplaces.py)."""

import geopandas as gpd
import numpy as np
from shapely.geometry import Point, Polygon

from src.matching.workplaces import assign_facilities, identify_workplaces

CRS = "EPSG:2154"


def _square(x: float, y: float, size: float = 10.0) -> Polygon:
    return Polygon([(x, y), (x + size, y), (x + size, y + size), (x, y + size)])


def _agents(home_points: list, home_ids: list) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"agent_id": np.arange(len(home_points)), "home_id": home_ids},
        geometry=[Point(*p) for p in home_points],
        crs=CRS,
    )


def _facilities(specs: list) -> gpd.GeoDataFrame:
    """specs : (ID, x, y, capacity)."""
    rows = [{"ID": s[0], "capacity": s[3], "geometry": _square(s[1], s[2])} for s in specs]
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=CRS)


def _all_buildings(specs: list) -> gpd.GeoDataFrame:
    rows = []
    for spec in specs:
        bid, usage1, x, y = spec[:4]
        size = spec[4] if len(spec) > 4 else 10.0
        rows.append({
            "ID": bid, "USAGE1": usage1, "USAGE2": None,
            "NB_ETAGES": 1, "HAUTEUR": 3.0, "geometry": _square(x, y, size),
        })
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=CRS)


def test_identify_workplaces_filters_and_capacity():
    buildings = _all_buildings([
        ("W1", "Commercial et services", 100, 0, 20),  # 20x20 = 400 m²
        ("R1", "Résidentiel", 0, 0),
        ("X1", "Indifférencié", 50, 0),                # exclu par défaut
    ])
    wp = identify_workplaces(buildings)
    assert list(wp["ID"]) == ["W1"]
    assert wp["capacity"].iloc[0] == 400.0


def test_identify_workplaces_usage2_matches():
    buildings = _all_buildings([("W1", "Indifférencié", 0, 0)])
    buildings.loc[0, "USAGE2"] = "Industriel"
    wp = identify_workplaces(buildings)
    assert list(wp["ID"]) == ["W1"]


def test_assign_fills_dest_columns():
    agents = _agents([(5, 5), (5, 5)], ["HOME1", "HOME1"])
    fac = _facilities([("F1", 100, 0, 50.0)])
    out = assign_facilities(agents, fac, seed=1)
    assert (out["dest_id"] == "F1").all()
    assert out["dist_m"].notna().all()
    assert len(out) == 2


def test_assign_na_when_no_facility():
    agents = _agents([(5, 5)], ["HOME1"])
    empty = gpd.GeoDataFrame({"ID": [], "capacity": []}, geometry=[], crs=CRS)
    out = assign_facilities(agents, empty)
    assert out["dest_id"].isna().all()
    assert "dest_id" in out.columns


def test_assign_empty_agents():
    agents = _agents([], [])
    fac = _facilities([("F1", 100, 0, 50.0)])
    out = assign_facilities(agents, fac)
    assert out.empty
    assert "dest_id" in out.columns


def test_distance_decay_prefers_closer():
    agents = _agents([(0, 0)] * 200, ["H"] * 200)
    fac = _facilities([("NEAR", 100, 0, 1.0), ("FAR", 10000, 0, 1.0)])
    out = assign_facilities(agents, fac, decay_m=3000.0, seed=0)
    counts = out["dest_id"].value_counts()
    assert counts.get("NEAR", 0) > counts.get("FAR", 0)


def test_capacity_weight_prefers_bigger():
    agents = _agents([(1000, 100)] * 300, ["H"] * 300)
    fac = _facilities([("BIG", 1000, 0, 10000.0), ("SMALL", 1000, 200, 25.0)])
    out = assign_facilities(agents, fac, decay_m=50000.0, seed=0)
    counts = out["dest_id"].value_counts()
    assert counts.get("BIG", 0) > counts.get("SMALL", 0)


def test_reproducible_with_seed():
    agents = _agents([(0, 0)] * 50, ["H"] * 50)
    fac = _facilities([("F1", 100, 0, 1.0), ("F2", 500, 0, 1.0)])
    a = assign_facilities(agents, fac, seed=7)
    b = assign_facilities(agents, fac, seed=7)
    assert list(a["dest_id"]) == list(b["dest_id"])


def test_custom_id_col():
    agents = _agents([(5, 5)], ["HOME1"])
    fac = _facilities([("F1", 100, 0, 1.0)]).rename(columns={"ID": "osm_id"})
    out = assign_facilities(agents, fac, seed=1, id_col="osm_id")
    assert out["dest_id"].iloc[0] == "F1"


def test_preserves_input_columns():
    agents = _agents([(5, 5)], ["HOME1"])
    agents["csp"] = "csp_cadres"
    fac = _facilities([("F1", 100, 0, 1.0)])
    out = assign_facilities(agents, fac, seed=1)
    assert out["csp"].iloc[0] == "csp_cadres"
    assert out.crs == agents.crs
