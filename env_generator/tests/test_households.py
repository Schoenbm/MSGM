"""Tests du reweighting IPU des ménages (matching/households.py)."""

import numpy as np
import pandas as pd

from src.matching.households import (
    AGE_COLS, CSP_COLS, HouseholdReweighter, fit_quality, ipu,
)


def _member(hh, iris, age, role, csp, w=1.0, sexe="1"):
    return {"hh_id": hh, "iris": iris, "age": age, "sexe": sexe,
            "role": role, "csp": csp, "is_minor": age < 18, "weight": w}


def test_ipu_recovers_feasible_targets():
    # 2 types de ménages, 2 contraintes : système exactement résoluble.
    # Ménage A contribue (1,0), ménage B contribue (0,1). Cibles (30, 70).
    A = np.array([[1.0, 0.0], [0.0, 1.0]])
    w = ipu(A, np.array([30.0, 70.0]), np.array([1.0, 1.0]))
    assert np.allclose(w, [30.0, 70.0], rtol=1e-3)


def test_ipu_couples_constraints():
    # Ménages partageant des catégories → l'IPU doit itérer vers un compromis.
    # 3 ménages, 2 contraintes. Vérifie que les marges pondérées atteignent la cible.
    A = np.array([[2.0, 1.0], [1.0, 0.0], [0.0, 3.0]])
    targets = np.array([100.0, 120.0])
    w = ipu(A, targets, np.ones(3), n_iter=200, tol=1e-8)
    fitted = (w[:, None] * A).sum(axis=0)
    assert np.allclose(fitted, targets, rtol=1e-3)


def test_ipu_ignores_zero_target():
    # Une cible nulle ne doit ni planter ni annuler les poids des autres contraintes.
    A = np.array([[1.0, 1.0], [0.0, 1.0]])
    w = ipu(A, np.array([0.0, 50.0]), np.ones(2))
    assert np.all(np.isfinite(w))
    fitted_c2 = (w * A[:, 1]).sum()
    assert np.isclose(fitted_c2, 50.0, rtol=1e-3)


def test_reweighter_builds_contribution_matrix():
    # Un couple (cadre + employé) + 2 enfants → contributions âge et CSP correctes.
    members = pd.DataFrame([
        _member("h1", "i1", 40, "referent", "csp_cadres"),
        _member("h1", "i1", 38, "conjoint", "csp_employes"),
        _member("h1", "i1", 8, "enfant", "mineur"),
        _member("h1", "i1", 5, "enfant", "mineur"),
    ])
    rw = HouseholdReweighter(members)
    assert list(rw.hh_ids) == ["h1"]
    row = dict(zip(rw.constraint_cols, rw.A[0]))
    # 2 enfants (tranches 6-10 et 3-5) ; 2 adultes dans 2 tranches (40 → 40-54, 38 → 25-39)
    assert row["age_6_10"] == 1 and row["age_3_5"] == 1
    assert row["age_40_54"] == 1 and row["age_25_39"] == 1
    # CSP : 1 cadre + 1 employé, 0 mineur dans les colonnes csp_*
    assert row["csp_cadres"] == 1 and row["csp_employes"] == 1
    assert sum(row[c] for c in CSP_COLS) == 2  # les mineurs ne comptent pas


def test_reweighter_hits_iris_targets():
    # Pool de 2 types de ménages ; cibles atteignables → marges pondérées = cibles.
    members = pd.DataFrame([
        _member("h1", "i", 30, "referent", "csp_cadres"),       # ménage actif
        _member("h2", "i", 70, "referent", "csp_chomeurs_inactifs"),  # ménage retraité
    ])
    rw = HouseholdReweighter(members)
    age_t = {c: 0.0 for c in AGE_COLS}
    age_t["age_25_39"] = 40.0
    age_t["age_65_79"] = 60.0
    csp_t = {c: 0.0 for c in CSP_COLS}
    csp_t["csp_cadres"] = 40.0
    csp_t["csp_chomeurs_inactifs"] = 60.0
    w = rw.weights_for(age_t, csp_t)
    tv = np.array([age_t[c] for c in AGE_COLS] + [csp_t[c] for c in CSP_COLS])
    q = fit_quality(rw.A, w, tv)
    assert q["rel_err"].abs().max() < 1e-3


def test_centenarian_lands_in_top_age_band():
    # AGED va jusqu'à ~108 ; la tranche age_80p plafonne à 99. Un centenaire doit
    # être rabattu sur age_80p, pas disparaître du calage.
    members = pd.DataFrame([
        _member("h1", "i", 103, "referent", "csp_chomeurs_inactifs"),
    ])
    rw = HouseholdReweighter(members)
    row = dict(zip(rw.constraint_cols, rw.A[0]))
    assert row["age_80p"] == 1
    assert sum(row[c] for c in AGE_COLS) == 1  # compté une fois, pas perdu


def test_reweighter_referent_weight_used_as_init():
    # Le poids initial du ménage = IPONDI du référent.
    members = pd.DataFrame([
        _member("h1", "i", 40, "referent", "csp_cadres", w=2.5),
        _member("h1", "i", 10, "enfant", "mineur", w=9.9),
    ])
    rw = HouseholdReweighter(members)
    assert np.isclose(rw.init_weight[0], 2.5)
