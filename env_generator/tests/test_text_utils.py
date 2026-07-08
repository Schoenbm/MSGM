"""Tests de CONTRAT — normalisation des noms de voie (chantier carte scolaire).

`normalize_voie` doit rendre comparables deux graphies de la même rue issues de
sources différentes : la BAN ("Rue de l'Église", casse mixte, apostrophe) et la
carte scolaire des collèges publics ("RUE DE L EGLISE", tout majuscule, sans
accent, apostrophe déjà remplacée par un espace). Sans cette normalisation
identique des deux côtés, le rattachement adresse -> secteur collège échoue
silencieusement (aucune correspondance trouvée).

Skippe tant que `src.text_utils` n'existe pas (test-first).
"""

import pytest

tu = pytest.importorskip("src.text_utils")
if not hasattr(tu, "normalize_voie"):
    pytest.skip("normalize_voie pas encore implémenté (chantier carte scolaire)",
                allow_module_level=True)

from src.text_utils import normalize_voie  # noqa: E402


def test_case_and_accents_are_folded():
    assert normalize_voie("Rue de l'Église") == normalize_voie("RUE DE L EGLISE")


def test_apostrophe_and_hyphen_become_space():
    assert normalize_voie("Rue de l'Église") == "RUE DE L EGLISE"
    assert normalize_voie("Chemin des Grandes-Vignes") == "CHEMIN DES GRANDES VIGNES"


def test_multiple_spaces_collapsed():
    assert normalize_voie("Rue   des   Fleurs") == "RUE DES FLEURS"


def test_leading_trailing_whitespace_stripped():
    assert normalize_voie("  Rue des Fleurs  ") == "RUE DES FLEURS"


def test_none_and_nan_become_empty_string():
    assert normalize_voie(None) == ""
    assert normalize_voie(float("nan")) == ""


def test_already_normalized_is_idempotent():
    assert normalize_voie("CHEMIN DE LA TURBINE") == "CHEMIN DE LA TURBINE"
    assert normalize_voie("IMPASSE DE L USINE") == "IMPASSE DE L USINE"
