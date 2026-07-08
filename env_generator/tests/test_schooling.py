"""Tests de CONTRAT — affectation collège calée sur la carte scolaire (chantier
carte scolaire, cf. échange du 2026-07-08).

Décisions verrouillées :
  - Lycée/école : 100% gravité, inchangé — pas de carte scolaire officielle
    disponible pour l'Isère/AURA (ni au national pour les lycées). Rien à tester
    ici, aucun changement de comportement pour ces niveaux.
  - Collège : chaque agent collégien tire un statut public/privé
    (Bernoulli, taux fixe `private_rate=0.20`, DEPP national — pas de taux
    communal disponible).
      - PRIVÉ  -> gravité restreinte aux collèges `secteur == "Privé"` ; repli
        gravité globale (tous collèges) si aucun collège privé dans la zone.
      - PUBLIC -> résolution déterministe via la carte scolaire (commune +
        adresse du domicile) ; repli gravité restreinte aux collèges
        `secteur == "Public"` (ou globale si aucun collège public) si :
          (a) le bâtiment domicile n'a pas d'adresse BAN à moins de max_dist_m,
          (b) la rue ne correspond à aucune ligne de la table pour la commune,
          (c) le numéro est hors plage ou parité incompatible,
          (d) le `code_rne` résolu n'est présent dans aucun équipement collège
              de la zone (donnée carte scolaire hors zone d'étude).

Trois fonctions pures à implémenter dans `src.matching.schooling` :
  - `attach_building_addresses(buildings, ban_addresses, max_dist_m=50.0)`
  - `resolve_college_sector(code_insee, nom_voie_norm, numero, secteurs)`
  - `assign_colleges_carte_scolaire(workers, addresses, secteurs, colleges,
                                    decay_m=1200.0, private_rate=0.20, seed=42)`

Skippe tant que ces fonctions n'existent pas (test-first).
"""

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import Point

sch = pytest.importorskip("src.matching.schooling")
_NEEDED = ("attach_building_addresses", "resolve_college_sector",
           "assign_colleges_carte_scolaire")
if not all(hasattr(sch, f) for f in _NEEDED):
    pytest.skip("chantier carte scolaire pas encore implémenté", allow_module_level=True)

from src.matching.schooling import (  # noqa: E402
    attach_building_addresses, resolve_college_sector, assign_colleges_carte_scolaire,
)

CRS = "EPSG:2154"


# ── attach_building_addresses ─────────────────────────────────────────────────

def _buildings(rows):
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=CRS)


def _addresses(rows):
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=CRS)


def test_nearest_address_within_radius_is_attached():
    buildings = _buildings([{"ID": "b1", "geometry": Point(0, 0)}])
    addresses = _addresses([
        {"code_insee": "38185", "nom_voie_norm": "RUE DES FLEURS", "numero": 12,
         "geometry": Point(10, 0)},   # 10 m
        {"code_insee": "38185", "nom_voie_norm": "RUE LOINTAINE", "numero": 3,
         "geometry": Point(200, 0)},  # 200 m, hors rayon
    ])
    out = attach_building_addresses(buildings, addresses, max_dist_m=50.0)
    row = out.loc[out["ID"] == "b1"].iloc[0]
    assert row["nom_voie_norm"] == "RUE DES FLEURS"
    assert row["numero"] == 12


def test_no_address_within_radius_is_none():
    buildings = _buildings([{"ID": "b1", "geometry": Point(0, 0)}])
    addresses = _addresses([
        {"code_insee": "38185", "nom_voie_norm": "RUE LOINTAINE", "numero": 3,
         "geometry": Point(200, 0)},
    ])
    out = attach_building_addresses(buildings, addresses, max_dist_m=50.0)
    row = out.loc[out["ID"] == "b1"].iloc[0]
    assert pd.isna(row["nom_voie_norm"]) and pd.isna(row["numero"])


# ── resolve_college_sector ────────────────────────────────────────────────────

def _secteurs(rows):
    return pd.DataFrame(rows)


def test_secteur_unique_ignores_street():
    secteurs = _secteurs([
        {"code_insee": "38175", "nom_voie_norm": None, "n_de_voie_debut": np.nan,
         "n_de_voie_fin": np.nan, "parite": None, "code_rne": "0381778B",
         "secteur_unique": "O"},
    ])
    assert resolve_college_sector("38175", "N IMPORTE QUOI", 42, secteurs) == "0381778B"


def test_street_range_and_parity_match():
    secteurs = _secteurs([
        {"code_insee": "38013", "nom_voie_norm": "CHEMIN DE LA TURBINE",
         "n_de_voie_debut": 1.0, "n_de_voie_fin": 9999.0, "parite": "PI",
         "code_rne": "0380026X", "secteur_unique": "N"},
        {"code_insee": "38013", "nom_voie_norm": "LE RIVIER",
         "n_de_voie_debut": 1.0, "n_de_voie_fin": 9999.0, "parite": "PI",
         "code_rne": "0382266G", "secteur_unique": "N"},
    ])
    assert resolve_college_sector("38013", "CHEMIN DE LA TURBINE", 7, secteurs) == "0380026X"
    assert resolve_college_sector("38013", "LE RIVIER", 2, secteurs) == "0382266G"


def test_parity_mismatch_returns_none():
    secteurs = _secteurs([
        {"code_insee": "38999", "nom_voie_norm": "RUE PAIRE",
         "n_de_voie_debut": 1.0, "n_de_voie_fin": 9999.0, "parite": "P",
         "code_rne": "AAAA", "secteur_unique": "N"},
    ])
    assert resolve_college_sector("38999", "RUE PAIRE", 3, secteurs) is None   # impair, secteur pair
    assert resolve_college_sector("38999", "RUE PAIRE", 4, secteurs) == "AAAA"


def test_out_of_range_number_returns_none():
    secteurs = _secteurs([
        {"code_insee": "38999", "nom_voie_norm": "RUE COURTE",
         "n_de_voie_debut": 1.0, "n_de_voie_fin": 20.0, "parite": "PI",
         "code_rne": "BBBB", "secteur_unique": "N"},
    ])
    assert resolve_college_sector("38999", "RUE COURTE", 500, secteurs) is None


def test_unknown_street_returns_none():
    secteurs = _secteurs([
        {"code_insee": "38999", "nom_voie_norm": "RUE CONNUE",
         "n_de_voie_debut": 1.0, "n_de_voie_fin": 9999.0, "parite": "PI",
         "code_rne": "CCCC", "secteur_unique": "N"},
    ])
    assert resolve_college_sector("38999", "RUE INCONNUE", 5, secteurs) is None


def test_unknown_commune_returns_none():
    secteurs = _secteurs([
        {"code_insee": "38999", "nom_voie_norm": None, "n_de_voie_debut": np.nan,
         "n_de_voie_fin": np.nan, "parite": None, "code_rne": "DDDD",
         "secteur_unique": "O"},
    ])
    assert resolve_college_sector("38000", "PEU IMPORTE", 5, secteurs) is None


# ── assign_colleges_carte_scolaire ────────────────────────────────────────────

def _colleges():
    rows = [
        {"equip_id": "college-pub", "code_rne": "PUB1", "secteur": "Public",
         "capacity": 1.0, "geometry": Point(0, 0)},
        {"equip_id": "college-priv", "code_rne": "PRIV1", "secteur": "Privé",
         "capacity": 1.0, "geometry": Point(5000, 0)},
    ]
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=CRS)


def _workers(n, home_id="h1", x=0, y=0):
    rows = [{"home_id": home_id, "geometry": Point(x, y)} for _ in range(n)]
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=CRS)


def _addr_lookup(rows):
    # Indexé par home_id : code_insee, nom_voie_norm, numero.
    return pd.DataFrame(rows).set_index("home_id")


def test_private_rate_is_approximately_20_percent():
    workers = _workers(2000, home_id="h1")
    addresses = _addr_lookup([{"home_id": "h1", "code_insee": "38175",
                               "nom_voie_norm": None, "numero": np.nan}])
    secteurs = _secteurs([
        {"code_insee": "38175", "nom_voie_norm": None, "n_de_voie_debut": np.nan,
         "n_de_voie_fin": np.nan, "parite": None, "code_rne": "PUB1",
         "secteur_unique": "O"},
    ])
    out = assign_colleges_carte_scolaire(workers, addresses, secteurs, _colleges(),
                                         private_rate=0.20, seed=1)
    share_private = float((out["dest_id"] == "college-priv").mean())
    assert 0.15 < share_private < 0.25


def test_private_status_is_shared_within_household():
    # Le tirage public/privé est PAR MÉNAGE : les collégiens d'un même
    # household_id partagent le statut (une fratrie n'est pas éclatée
    # public/privé), tout en respectant le taux global.
    rows = []
    for h in range(500):
        for _ in range(2):  # 2 collégiens par ménage
            rows.append({"home_id": "h1", "household_id": f"hh{h}",
                         "geometry": Point(0, 0)})
    workers = gpd.GeoDataFrame(rows, geometry="geometry", crs=CRS)
    addresses = _addr_lookup([{"home_id": "h1", "code_insee": "38175",
                               "nom_voie_norm": None, "numero": np.nan}])
    secteurs = _secteurs([
        {"code_insee": "38175", "nom_voie_norm": None, "n_de_voie_debut": np.nan,
         "n_de_voie_fin": np.nan, "parite": None, "code_rne": "PUB1",
         "secteur_unique": "O"},
    ])
    out = assign_colleges_carte_scolaire(workers, addresses, secteurs, _colleges(),
                                         private_rate=0.20, seed=1)
    is_priv = (out["dest_id"] == "college-priv").to_numpy()
    # Aucun ménage ne doit avoir un statut mixte.
    mixed = pd.Series(is_priv).groupby(out["household_id"].values).nunique()
    assert (mixed == 1).all(), "un ménage a des collégiens à la fois public et privé"
    share_private = float(is_priv.mean())
    assert 0.15 < share_private < 0.25


def test_public_secteur_unique_is_deterministic_not_gravity():
    # Domicile en secteur unique, très proche du collège privé (5000 m du public)
    # mais la carte scolaire doit renvoyer le PUBLIC malgré la distance.
    workers = _workers(50, home_id="h1", x=5000, y=0)
    addresses = _addr_lookup([{"home_id": "h1", "code_insee": "38175",
                               "nom_voie_norm": None, "numero": np.nan}])
    secteurs = _secteurs([
        {"code_insee": "38175", "nom_voie_norm": None, "n_de_voie_debut": np.nan,
         "n_de_voie_fin": np.nan, "parite": None, "code_rne": "PUB1",
         "secteur_unique": "O"},
    ])
    out = assign_colleges_carte_scolaire(workers, addresses, secteurs, _colleges(),
                                         private_rate=0.0, seed=1)  # force 100% public
    assert (out["dest_id"] == "college-pub").all()


def test_fallback_to_gravity_when_address_missing():
    # Pas d'entrée dans `addresses` pour ce home_id -> repli gravité (public
    # dispo dans la zone), pas d'orphelin.
    workers = _workers(30, home_id="h_no_addr")
    addresses = _addr_lookup([{"home_id": "OTHER", "code_insee": "38175",
                               "nom_voie_norm": None, "numero": np.nan}])
    secteurs = _secteurs([
        {"code_insee": "38175", "nom_voie_norm": None, "n_de_voie_debut": np.nan,
         "n_de_voie_fin": np.nan, "parite": None, "code_rne": "PUB1",
         "secteur_unique": "O"},
    ])
    out = assign_colleges_carte_scolaire(workers, addresses, secteurs, _colleges(),
                                         private_rate=0.0, seed=1)
    assert out["dest_id"].notna().all()
    assert out["dest_id"].isin(["college-pub", "college-priv"]).all()


def test_fallback_when_code_rne_not_in_zone_equipment():
    # La rue résout vers un UAI hors de nos équipements (carte scolaire dept
    # entier, notre `colleges` local ne couvre que la zone d'étude) -> repli.
    workers = _workers(30, home_id="h1")
    addresses = _addr_lookup([{"home_id": "h1", "code_insee": "38999",
                               "nom_voie_norm": "RUE CONNUE", "numero": 5}])
    secteurs = _secteurs([
        {"code_insee": "38999", "nom_voie_norm": "RUE CONNUE",
         "n_de_voie_debut": 1.0, "n_de_voie_fin": 9999.0, "parite": "PI",
         "code_rne": "UAI-HORS-ZONE", "secteur_unique": "N"},
    ])
    out = assign_colleges_carte_scolaire(workers, addresses, secteurs, _colleges(),
                                         private_rate=0.0, seed=1)
    assert out["dest_id"].notna().all()
    assert out["dest_id"].isin(["college-pub", "college-priv"]).all()


def test_private_fallback_to_global_gravity_when_no_private_college():
    colleges_pub_only = _colleges()[lambda d: d["secteur"] == "Public"].copy()
    workers = _workers(30, home_id="h1")
    addresses = _addr_lookup([{"home_id": "h1", "code_insee": "38175",
                               "nom_voie_norm": None, "numero": np.nan}])
    secteurs = _secteurs([
        {"code_insee": "38175", "nom_voie_norm": None, "n_de_voie_debut": np.nan,
         "n_de_voie_fin": np.nan, "parite": None, "code_rne": "PUB1",
         "secteur_unique": "O"},
    ])
    out = assign_colleges_carte_scolaire(workers, addresses, secteurs, colleges_pub_only,
                                         private_rate=1.0, seed=1)  # force 100% privé
    assert out["dest_id"].notna().all()
    assert (out["dest_id"] == "college-pub").all()  # seul équipement dispo


def test_deterministic_with_seed():
    workers = _workers(100, home_id="h1")
    addresses = _addr_lookup([{"home_id": "h1", "code_insee": "38175",
                               "nom_voie_norm": None, "numero": np.nan}])
    secteurs = _secteurs([
        {"code_insee": "38175", "nom_voie_norm": None, "n_de_voie_debut": np.nan,
         "n_de_voie_fin": np.nan, "parite": None, "code_rne": "PUB1",
         "secteur_unique": "O"},
    ])
    o1 = assign_colleges_carte_scolaire(workers, addresses, secteurs, _colleges(), seed=7)
    o2 = assign_colleges_carte_scolaire(workers, addresses, secteurs, _colleges(), seed=7)
    pd.testing.assert_series_equal(o1["dest_id"], o2["dest_id"])
