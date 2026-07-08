"""Validation MOBPRO sur DONNÉES RÉELLES (cache local requis).

Vérifie `build_commute_matrix` sur la vraie base de flux INSEE 2022 : c'est là que
se cachent les pièges (noms de colonnes, format des codes commune, clip). Complète
les tests synthétiques de `test_commute.py`.

Skippé si le cache MOBPRO est absent ou tant que la fonction n'existe pas.
"""

from pathlib import Path

import numpy as np
import pytest

wp = pytest.importorskip("src.matching.workplaces")
if not hasattr(wp, "build_commute_matrix"):
    pytest.skip("build_commute_matrix pas encore implémenté (chantier MOBPRO)",
                allow_module_level=True)

from src.loaders.mobpro import _CACHE_DIR, COL_FLUX, COL_RESIDENCE, COL_TRAVAIL, load_mobpro
from src.matching.workplaces import build_commute_matrix

_ROOT = Path(__file__).resolve().parent.parent
_MOBPRO_ZIP = _CACHE_DIR / "mobpro-flux-2022.zip"

pytestmark = pytest.mark.skipif(
    not _MOBPRO_ZIP.exists(),
    reason="cache MOBPRO absent (mobpro-flux-2022.zip) — test données réelles ignoré",
)


def _region_communes() -> set[str]:
    import yaml
    cfg = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
    codes = cfg["zones"]["region"]["selector"]["codes"]
    return {str(c)[:5] for c in codes}


@pytest.fixture(scope="module")
def matrix_and_df():
    communes = _region_communes()
    df = load_mobpro(communes=communes)          # filtre résidence
    if df.empty:
        pytest.skip("aucun flux MOBPRO pour les communes de la région")
    M = build_commute_matrix(df, region_communes=communes)
    return M, df, communes


def test_rows_sum_to_one(matrix_and_df):
    M, _, _ = matrix_and_df
    assert len(M) > 0
    sums = M.sum(axis=1).to_numpy(dtype=float)
    assert np.allclose(sums, 1.0, atol=1e-6), "des lignes P(c'|c) ne somment pas à 1"


def test_indices_and_columns_within_region(matrix_and_df):
    M, _, communes = matrix_and_df
    assert set(M.index).issubset(communes)       # résidence en région
    assert set(M.columns).issubset(communes)     # travail clippé à la région (D2a)


def test_matches_manual_renormalization(matrix_and_df):
    # Recalcule à la main P(c'|c) pour une commune (clip + renorm) et compare :
    # attrape un bug de colonne / d'agrégation sur les vraies données.
    M, df, communes = matrix_and_df
    home = M.index[0]
    sub = df[(df[COL_RESIDENCE] == home) & (df[COL_TRAVAIL].isin(communes))]
    flux = sub.groupby(COL_TRAVAIL)[COL_FLUX].sum()
    expected = flux / flux.sum()
    got = M.loc[home].reindex(expected.index).fillna(0.0)
    assert np.allclose(got.to_numpy(float), expected.to_numpy(float), atol=1e-6)


def test_local_work_is_plausible(matrix_and_df):
    # Plausibilité : sur une grande commune (Grenoble 38185 si présente), une part
    # substantielle des actifs travaille dans sa propre commune (auto-flux dominant
    # ou quasi). Garde-fou de sanité sur le sens des flux.
    M, _, _ = matrix_and_df
    home = "38185" if "38185" in M.index else M.sum(axis=1).index[0]
    if home not in M.columns:
        pytest.skip("commune de référence sans auto-flux en région")
    self_share = float(M.loc[home, home])
    assert self_share > 0.25, f"auto-flux {home} = {self_share:.2f} (attendu > 0,25)"
