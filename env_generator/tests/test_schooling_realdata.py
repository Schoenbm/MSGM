"""Validation carte scolaire sur DONNÉES RÉELLES (cache local requis).

Vérifie le pipeline complet (BAN + carte scolaire + établissements) sur les vraies
sources : c'est là que se cachent les pièges (normalisation des voies, communes
absentes, doublons de code_rne). Complète les tests synthétiques de
`test_schooling.py`, `test_carte_scolaire_loader.py`, `test_ban_loader.py`.

Skippé si les caches sont absents ou tant que les fonctions n'existent pas.
"""

from pathlib import Path

import pandas as pd
import pytest

cs = pytest.importorskip("src.loaders.carte_scolaire")
ban_mod = pytest.importorskip("src.loaders.ban")
sch = pytest.importorskip("src.matching.schooling")
_NEEDED_CS = ("load_secteurs_colleges", "load_colleges_publics_prives", "_CACHE_DIR")
_NEEDED_BAN = ("load_ban_addresses",)
_NEEDED_SCH = ("attach_building_addresses", "resolve_college_sector")
if not (all(hasattr(cs, f) for f in _NEEDED_CS)
        and all(hasattr(ban_mod, f) for f in _NEEDED_BAN)
        and all(hasattr(sch, f) for f in _NEEDED_SCH)):
    pytest.skip("chantier carte scolaire pas encore implémenté", allow_module_level=True)

from src.loaders.carte_scolaire import load_secteurs_colleges, load_colleges_publics_prives
from src.loaders.ban import load_ban_addresses
from src.matching.schooling import attach_building_addresses, resolve_college_sector

_ROOT = Path(__file__).resolve().parent.parent
# Les trois caches sont téléchargés séparément (secteurs + établissements +
# adresses BAN) ; on ne valide QUE si les trois sont présents.
_CACHES = [
    cs._CACHE_DIR / "carte_scolaire_colleges_038.csv",
    cs._CACHE_DIR / "etablissements_colleges_038.csv",
    cs._CACHE_DIR / "adresses_38.csv.gz",
]

pytestmark = pytest.mark.skipif(
    not all(p.exists() for p in _CACHES),
    reason="caches carte scolaire / BAN absents — test données réelles ignoré",
)


@pytest.fixture(scope="module")
def secteurs():
    return load_secteurs_colleges(departement="038")


@pytest.fixture(scope="module")
def colleges():
    return load_colleges_publics_prives(departement="038")


@pytest.fixture(scope="module")
def ban():
    # Grenoble : commune multi-secteur connue (cf. échange), volumineuse mais
    # rapide à filtrer depuis le cache département déjà en local.
    return load_ban_addresses(departement="38", communes={"38185"})


def test_secteurs_has_both_secteur_unique_and_multi(secteurs):
    assert set(secteurs["secteur_unique"]) <= {"O", "N"}
    assert (secteurs["secteur_unique"] == "N").any()
    assert (secteurs["secteur_unique"] == "O").any()


def test_grenoble_is_multi_secteur(secteurs):
    # Grenoble (38185) est une grande commune multi-collèges — sert de garde-fou
    # de sens : si elle apparaissait en secteur unique, la lecture des colonnes
    # source serait probablement décalée.
    sub = secteurs[secteurs["code_insee"] == "38185"]
    assert not sub.empty
    assert (sub["secteur_unique"] == "N").all()
    assert sub["nom_voie_norm"].notna().all()


def test_colleges_has_public_and_private(colleges):
    assert set(colleges["secteur"]) <= {"Public", "Privé"}
    assert (colleges["secteur"] == "Public").sum() > 0


def test_college_code_rne_are_unique(colleges):
    assert colleges["code_rne"].is_unique


def test_ban_addresses_nonempty_for_grenoble(ban):
    assert len(ban) > 0
    assert set(ban["code_insee"]) == {"38185"}


def test_end_to_end_resolution_finds_a_match_for_most_addresses(ban, secteurs):
    # Prend un échantillon d'adresses réelles de Grenoble et vérifie qu'une
    # majorité résout vers un code_rne (sinon : bug de normalisation des voies,
    # à corriger avant de considérer le chantier fiable).
    sample = ban.sample(n=min(200, len(ban)), random_state=0)
    resolved = sample.apply(
        lambda r: resolve_college_sector(r["code_insee"], r["nom_voie_norm"],
                                         r["numero"], secteurs),
        axis=1,
    )
    match_rate = float(resolved.notna().mean())
    assert match_rate > 0.6, f"taux de résolution carte scolaire = {match_rate:.2f} (attendu > 0.6)"
