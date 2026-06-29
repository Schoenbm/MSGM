"""Tests de non-régression sur DONNÉES RÉELLES (cache local requis).

Les tests unitaires mockés n'ont attrapé AUCUNE des deux régressions réelles de ce
chantier, parce qu'elles sont **émergentes** (visibles seulement à l'échelle du
vrai fichier) :
  - des « ménages » fabriqués depuis la population des communautés (un EHPAD = un
    faux ménage de 22 personnes, NUMMI placeholder partagé) ;
  - un clip d'âge des centenaires qui rendait le calage IPU **infaisable**
    (médiane d'erreur 0,12 % → 6 %), invisible sur une fixture.

Ces tests chargent le vrai fichier détail INSEE (+ les marges IRIS) et vérifient
les invariants structurels et la qualité de calage. Ils sont **skippés si le cache
est absent** : la suite par défaut reste rapide et hors-réseau (cf. convention repo
« loaders réseau mockés ; correctness réelle = smoke test »).

Lents (chargement d'un zip de ~130 Mo) : le chargement est partagé via une fixture
de portée module.
"""

from pathlib import Path

import numpy as np
import pytest

from src.loaders.rp_detail import _CACHE_DIR, load_rp_households

_RP_ZIP = _CACHE_DIR / "RP2022_indcvize.zip"
_ROOT = Path(__file__).resolve().parent.parent
_SHP = _ROOT / "data" / "contour_iris.shp"

pytestmark = pytest.mark.skipif(
    not _RP_ZIP.exists(),
    reason=f"cache RP détail absent ({_RP_ZIP.name}) — test données réelles ignoré",
)


@pytest.fixture(scope="module")
def households():
    """Échantillon réel de ménages de l'Isère (chargé une fois pour le module)."""
    return load_rp_households("38")


# ── Invariants structurels (auraient attrapé les faux ménages de communautés) ──

def test_no_household_spans_multiple_iris(households):
    # Un ménage occupe un logement = un seul IRIS. >1 IRIS = collision d'identifiant
    # (symptôme des communautés LPRM=Z groupées par un NUMMI placeholder partagé).
    multi = households.groupby("hh_id")["iris"].nunique()
    n_bad = int((multi > 1).sum())
    assert n_bad == 0, f"{n_bad} ménages s'étalent sur plusieurs IRIS"


def test_no_orphan_children(households):
    # Aucun ménage avec un enfant mais sans adulte : garantit le lien parent→enfant.
    g = households.groupby("hh_id")
    kid = g["role"].apply(lambda s: (s == "enfant").any())
    adult = g["age"].apply(lambda s: (s >= 18).any())
    assert int((kid & ~adult).sum()) == 0


def test_mean_household_size_matches_insee(households):
    # Taille moyenne pondérée ≈ INSEE Isère (~2,2). Un changement qui casserait la
    # reconstruction des ménages (ex. faux ménages géants) ferait dériver ce chiffre.
    g = households.groupby("hh_id")
    mean = np.average(g.size(), weights=g["weight"].first())
    assert 2.0 < mean < 2.4, f"taille moyenne ménage = {mean:.2f} (attendu ≈2,2)"


def test_ordinary_households_have_single_referent(households):
    # Les ménages ORDINAIRES (hh_id non « Zc_ ») ont exactement une personne de
    # référence (LPRM=1). Les singletons de communautés en sont exclus (sans référent).
    ordinary = households[~households["hh_id"].str.startswith("Zc_")]
    nref = ordinary.groupby("hh_id")["role"].apply(lambda s: (s == "referent").sum())
    n_bad = int((nref != 1).sum())
    assert n_bad == 0, f"{n_bad} ménages ordinaires sans référent unique"


def test_collective_singletons_are_size_one(households):
    # Les individus de communautés (hh_id « Zc_ ») sont des singletons : un EHPAD ne
    # doit JAMAIS reformer un ménage multi-personnes.
    zc = households[households["hh_id"].str.startswith("Zc_")]
    if len(zc):
        assert zc.groupby("hh_id").size().max() == 1


# ── Filtre commune (aurait attrapé la confusion CANTVILLE vs commune) ──

def test_communes_filter_returns_only_requested():
    # Le filtre porte sur la commune = IRIS[:5], PAS sur le CANTVILLE (pseudo-canton).
    d = load_rp_households("38", communes=["38185"])  # Grenoble
    assert len(d) > 0
    assert (d["iris"].str[:5] == "38185").all()


# ── Qualité de calage IPU (aurait attrapé le clip centenaire infaisable) ──

@pytest.mark.skipif(not _SHP.exists(), reason="contour_iris.shp absent")
def test_ipu_fit_quality_on_region(households):
    # Cale le pool sur les vraies marges IRIS de la région et vérifie que les marges
    # pondérées obtenues collent aux cibles. C'est CE test qui aurait fait échouer le
    # clip centenaire (il faisait exploser la médiane d'erreur 0,12 % → 6 %).
    import yaml

    from src.matching.households import (AGE_COLS, CSP_COLS,
                                         HouseholdReweighter, fit_quality)

    try:
        from src.loaders.iris import load_iris
        cfg = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
        codes = cfg["zones"]["region"]["selector"]["codes"]
        g = load_iris(selector={"type": "iris", "codes": codes}, shp_path=str(_SHP))
    except Exception as exc:  # base-ic non caché / réseau indispo → on n'échoue pas
        pytest.skip(f"marges IRIS indisponibles ({exc})")

    rw = HouseholdReweighter(households)
    # Sous-échantillon d'IRIS pas minuscules (sur les très petits, 30 % d'erreur sur
    # ~5 personnes est du bruit, pas une régression).
    sample = g[g["Ind_total"] > 800].head(40)
    errs = []
    for _, r in sample.iterrows():
        at = {c: r[c] for c in AGE_COLS if c in g}
        ct = {c: r[c] for c in CSP_COLS if c in g}
        w = rw.weights_for(at, ct)
        tv = np.array([at.get(c, 0) for c in AGE_COLS] + [ct.get(c, 0) for c in CSP_COLS])
        errs.append(fit_quality(rw.A, w, tv)["rel_err"].abs().max())

    median_err = float(np.median(errs))
    # Sain ≈ 0,001 ; le clip donnait ≈ 0,06. Seuil 0,01 = séparateur net avec marge.
    assert median_err < 0.01, f"calage IPU dégradé : médiane d'erreur = {median_err:.4f}"
