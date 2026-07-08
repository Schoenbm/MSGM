"""Tests de CONTRAT — affectation domicile-travail calée sur MOBPRO (2 étapes).

Chantier MOBPRO. Deux fonctions pures, testables sur fixture synthétique :
  - `build_commute_matrix(mobpro_df, region_communes)` : matrice P(c'|c) des flux
    domicile→travail, CLIPPÉE aux communes de la région + RENORMALISÉE par ligne
    (décision D2a : on garde les actifs dans la zone). Résidence ET travail hors
    zone écartés.
  - `assign_workplaces_mobpro(workers, workplaces, commute_matrix, …)` : pour chaque
    actif, tire une commune de travail c' via P(c'|c) (D1=B), puis le bâtiment par
    gravité restreinte à c' ; repli gravité globale si commune absente de la matrice
    ou sans lieu de travail (D3).

Skippe tant que les fonctions n'existent pas (test-first).
"""

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import Point, Polygon

wp = pytest.importorskip("src.matching.workplaces")
if not all(hasattr(wp, f) for f in ("build_commute_matrix", "assign_workplaces_mobpro")):
    pytest.skip("MOBPRO pas encore implémenté (build_commute_matrix / "
                "assign_workplaces_mobpro)", allow_module_level=True)

from src.matching.workplaces import build_commute_matrix, assign_workplaces_mobpro  # noqa: E402

CRS = "EPSG:2154"
# Noms de colonnes MOBPRO (millésime couplé, cf. loaders/mobpro.py).
C_RES, C_TRA, C_FLUX = "CODGEO", "DCLT", "NBFLUX_C22_ACTOCC15P"


def _mobpro(rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=[C_RES, C_TRA, C_FLUX])


# ── build_commute_matrix : clip + renormalisation (D2a) ───────────────────────

def test_rows_sum_to_one_after_clip_and_renorm():
    # 38185 : 700 vers 38185, 300 vers 38186, 1000 vers 69123 (HORS région).
    # Après clip du hors-région et renormalisation : 0.7 / 0.3.
    df = _mobpro([("38185", "38185", 700), ("38185", "38186", 300),
                  ("38185", "69123", 1000)])
    M = build_commute_matrix(df, region_communes={"38185", "38186"})
    row = M.loc["38185"]
    assert abs(float(row.sum()) - 1.0) < 1e-9
    assert abs(float(row["38185"]) - 0.7) < 1e-9
    assert abs(float(row["38186"]) - 0.3) < 1e-9


def test_residence_out_of_region_excluded():
    # Un actif qui RÉSIDE hors zone n'a pas à figurer (on ne peuple que la région).
    df = _mobpro([("69123", "38185", 500)])
    M = build_commute_matrix(df, region_communes={"38185"})
    assert "69123" not in M.index


def test_commune_with_no_in_region_work_is_absent():
    # 38185 ne travaille QUE hors zone → aucune destination en région → commune
    # absente de la matrice (l'appelant basculera en gravité globale, D3).
    df = _mobpro([("38185", "69123", 1000)])
    M = build_commute_matrix(df, region_communes={"38185"})
    assert "38185" not in M.index


def test_matrix_columns_are_region_only():
    df = _mobpro([("38185", "38185", 100), ("38185", "69123", 900)])
    M = build_commute_matrix(df, region_communes={"38185", "38186"})
    assert set(M.columns).issubset({"38185", "38186"})
    assert "69123" not in M.columns


# ── assign_workplaces_mobpro : tirage 2 étapes + replis ───────────────────────

def _square(cx, cy, r=15.0):
    return Polygon([(cx - r, cy - r), (cx + r, cy - r), (cx + r, cy + r), (cx - r, cy + r)])


def _workplaces():
    # Un lieu de travail par commune, éloignés (la gravité ne mélange pas les communes).
    rows = [
        {"ID": "wA", "commune": "A", "capacity": 100.0, "geometry": _square(0, 0)},
        {"ID": "wB", "commune": "B", "capacity": 100.0, "geometry": _square(10000, 0)},
    ]
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=CRS)


def _workers(commune, n, x):
    rows = [{"home_id": f"{commune}", "commune": commune, "geometry": Point(x, 0)}
            for _ in range(n)]
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=CRS)


def _matrix(d):
    return pd.DataFrame(d).T.fillna(0.0)  # index=home, cols=work


def test_workers_go_to_drawn_commune():
    # A → 100 % commune A ; B → 70 % A / 30 % B. On vérifie la répartition des
    # communes de travail effectives (via la commune du lieu de travail assigné).
    M = _matrix({"A": {"A": 1.0, "B": 0.0}, "B": {"A": 0.3, "B": 0.7}})
    workers = pd.concat([_workers("A", 200, 0), _workers("B", 400, 10000)])
    workers = gpd.GeoDataFrame(workers, geometry="geometry", crs=CRS)
    out = assign_workplaces_mobpro(workers, _workplaces(), M, decay_m=3000.0, seed=1)
    wp_commune = _workplaces().set_index("ID")["commune"].to_dict()
    out_commune = out["dest_id"].map(wp_commune)
    a = out_commune[workers["commune"].values == "A"]
    b = out_commune[workers["commune"].values == "B"]
    assert (a == "A").all()                       # A ne va qu'en A
    share_b_to_b = float((b == "B").mean())
    assert 0.6 < share_b_to_b < 0.8               # ~0.7 (tirage, seed fixe)


def test_fallback_when_commune_absent_from_matrix():
    # Commune "Z" hors matrice → repli gravité globale : dest non nulle (pas d'orphelin).
    M = _matrix({"A": {"A": 1.0}})
    workers = _workers("Z", 50, 0)
    out = assign_workplaces_mobpro(workers, _workplaces(), M, decay_m=1e9, seed=1)
    assert out["dest_id"].notna().all()


def test_fallback_when_drawn_commune_has_no_workplace():
    # B → 100 % commune C, mais aucun lieu de travail en C → repli gravité globale.
    M = _matrix({"B": {"C": 1.0}})
    workers = _workers("B", 50, 10000)
    out = assign_workplaces_mobpro(workers, _workplaces(), M, decay_m=1e9, seed=1)
    assert out["dest_id"].notna().all()
    assert out["dest_id"].isin(["wA", "wB"]).all()  # un vrai lieu de travail existant


def test_deterministic_with_seed():
    M = _matrix({"A": {"A": 0.5, "B": 0.5}})
    workers = _workers("A", 100, 0)
    o1 = assign_workplaces_mobpro(workers, _workplaces(), M, seed=7)
    o2 = assign_workplaces_mobpro(workers, _workplaces(), M, seed=7)
    pd.testing.assert_series_equal(o1["dest_id"], o2["dest_id"])


def test_dest_is_always_a_real_workplace():
    M = _matrix({"A": {"A": 1.0, "B": 0.0}})
    workers = _workers("A", 30, 0)
    out = assign_workplaces_mobpro(workers, _workplaces(), M, seed=3)
    assert out.loc[out["dest_id"].notna(), "dest_id"].isin(["wA", "wB"]).all()
