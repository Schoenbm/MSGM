"""Tests de CONTRAT — parsing des sources carte scolaire (chantier carte scolaire).

Deux fonctions de parsing PURES (séparées du téléchargement, comme
`iris._load_csv_from_zip`), testables sur un extrait CSV réel figé en fixture :

  - `_parse_secteurs_csv(path)` : export CSV du dataset national "carte scolaire
    des collèges publics" (data.education.gouv.fr) -> table commune/rue/plage de
    numéros/parité -> code_rne. Colonnes source (séparateur `;`) : code_insee,
    type_et_libelle, n_de_voie_debut, n_de_voie_fin, parite, code_rne,
    secteur_unique. Vraies lignes reprises telles que vues sur l'export réel
    (communes FROGES en secteur unique, GRENOBLE multi-secteur).

  - `_parse_etablissements_csv(path)` : export CSV du dataset "adresse et
    géolocalisation des établissements 1er/2nd degré", filtré collèges (public
    ET privé, ouverts). Colonnes source utilisées : numero_uai,
    appellation_officielle, secteur_public_prive_libe, coordonnee_x,
    coordonnee_y, nature_uai_libe, etat_etablissement_libe, code_departement.

Sortie normalisée attendue (colonnes du DataFrame/GeoDataFrame en sortie de
`load_secteurs_colleges` / `load_colleges_publics_prives`) :

  secteurs   : code_insee, nom_voie_norm, n_de_voie_debut, n_de_voie_fin,
               parite, code_rne, secteur_unique
  colleges   : equip_id, code_rne, kind="college", secteur, capacity, nom,
               geometry (EPSG:2154, depuis coordonnee_x/y qui sont DÉJÀ en
               Lambert-93 — pas de reprojection)

Skippe tant que `src.loaders.carte_scolaire` n'existe pas (test-first).
"""

from pathlib import Path

import pytest

cs = pytest.importorskip("src.loaders.carte_scolaire")
if not all(hasattr(cs, f) for f in ("_parse_secteurs_csv", "_parse_etablissements_csv")):
    pytest.skip("carte_scolaire pas encore implémenté (_parse_secteurs_csv / "
                "_parse_etablissements_csv)", allow_module_level=True)

from src.loaders.carte_scolaire import _parse_secteurs_csv, _parse_etablissements_csv  # noqa: E402

# Extrait RÉEL de l'export data.education.gouv.fr (fr-en-carte-scolaire-colleges-publics),
# département 038 — une commune en secteur unique, une en multi-secteur.
_SECTEURS_CSV = (
    "code_region;libelle_region;code_academie;libelle_academie;code_departement;"
    "libelle_departement;code_postal;code_insee;libelle_commune;lieu_dit;"
    "type_et_libelle;n_de_voie_debut;indice_de_repetition_debut;n_de_voie_fin;"
    "indice_de_repetition_fin;parite;code_rne;secteur_unique;no_ligne\n"
    '84;AUVERGNE-RHONE-ALPES;08;GRENOBLE;038;ISERE;;38175;FROGES;;;;;;;;0381778B;O;20010\n'
    '84;AUVERGNE-RHONE-ALPES;08;GRENOBLE;038;ISERE;38100;38185;GRENOBLE;;'
    'ALLEE DE LA SYLPHIDE;1.0;;9999.0;;PI;0381780D;N;20029\n'
    '84;AUVERGNE-RHONE-ALPES;08;GRENOBLE;038;ISERE;38100;38185;GRENOBLE;;'
    'ALLEE DES FRENES;1.0;;9999.0;;PI;0382032C;N;20032\n'
)

# Extrait RÉEL de l'export "adresse et géolocalisation des établissements",
# filtré collèges dept 038 (un public, un privé).
_ETABLISSEMENTS_CSV = (
    "numero_uai;appellation_officielle;denomination_principale;patronyme_uai;"
    "secteur_public_prive_libe;adresse_uai;lieu_dit_uai;boite_postale_uai;"
    "code_postal_uai;localite_acheminement_uai;libelle_commune;coordonnee_x;"
    "coordonnee_y;epsg;latitude;longitude;appariement;localisation;nature_uai;"
    "nature_uai_libe;etat_etablissement;etat_etablissement_libe;"
    "code_departement;code_region;code_academie;code_commune;libelle_departement;"
    "libelle_region;libelle_academie;position;secteur_prive_code_type_contrat;"
    "secteur_prive_libelle_type_contrat;code_ministere;libelle_ministere;"
    "date_ouverture;sigle;rnb\n"
    "0380004Y;Collège Arc en Ciers;COLLEGE;ARC EN CIERS;Public;19 avenue De Carouge;"
    ";;38630;LES AVENIERES;Les Avenières Veyrins-Thuellin;900580.1;6507116.4;"
    "EPSG:2154;45.634;5.575;Parfaite;Numéro de rue;340;COLLEGE;1;OUVERT;038;84;08;"
    "38022;Isère;Auvergne-Rhône-Alpes;Grenoble;pos;99;SANS OBJET;06;MENJ;"
    "1965-05-01;CLG;\n"
    "0380018N;Lycée technologique privé Iser;LYCEE;ISER;Privé;;;;38000;GRENOBLE;"
    "Grenoble;909000.0;6459000.0;EPSG:2154;45.17;5.72;Parfaite;Numéro de rue;340;"
    "LYCEE;1;OUVERT;038;84;08;38185;Isère;Auvergne-Rhône-Alpes;Grenoble;pos;1;"
    "SOUS CONTRAT;06;MENJ;1970-12-01;LYC;\n"
    "0380099Z;Collège Privé Notre-Dame;COLLEGE;NOTRE DAME;Privé;;;;38000;GRENOBLE;"
    "Grenoble;908000.0;6458000.0;EPSG:2154;45.16;5.71;Parfaite;Numéro de rue;340;"
    "COLLEGE;1;OUVERT;038;84;08;38185;Isère;Auvergne-Rhône-Alpes;Grenoble;pos;1;"
    "SOUS CONTRAT;06;MENJ;1970-12-01;CLG;\n"
)


@pytest.fixture()
def secteurs_csv(tmp_path: Path) -> Path:
    p = tmp_path / "secteurs.csv"
    p.write_text(_SECTEURS_CSV, encoding="utf-8")
    return p


@pytest.fixture()
def etablissements_csv(tmp_path: Path) -> Path:
    p = tmp_path / "etablissements.csv"
    p.write_text(_ETABLISSEMENTS_CSV, encoding="utf-8")
    return p


# ── _parse_secteurs_csv ───────────────────────────────────────────────────────

def test_secteur_unique_row_has_no_street_range(secteurs_csv):
    df = _parse_secteurs_csv(secteurs_csv)
    row = df[df["code_insee"] == "38175"].iloc[0]
    assert row["secteur_unique"] == "O"
    assert row["code_rne"] == "0381778B"
    assert pd_isna(row["nom_voie_norm"]) or row["nom_voie_norm"] == ""


def test_multi_secteur_rows_are_normalized_and_kept_separate(secteurs_csv):
    df = _parse_secteurs_csv(secteurs_csv)
    sub = df[df["code_insee"] == "38185"]
    assert len(sub) == 2
    assert set(sub["nom_voie_norm"]) == {"ALLEE DE LA SYLPHIDE", "ALLEE DES FRENES"}
    assert set(sub["code_rne"]) == {"0381780D", "0382032C"}
    assert (sub["parite"] == "PI").all()
    assert (sub["n_de_voie_debut"] == 1.0).all()
    assert (sub["n_de_voie_fin"] == 9999.0).all()


def test_expected_columns_present(secteurs_csv):
    df = _parse_secteurs_csv(secteurs_csv)
    for col in ("code_insee", "nom_voie_norm", "n_de_voie_debut", "n_de_voie_fin",
                "parite", "code_rne", "secteur_unique"):
        assert col in df.columns


# ── _parse_etablissements_csv ─────────────────────────────────────────────────

def test_only_colleges_kept_lycee_excluded(etablissements_csv):
    df = _parse_etablissements_csv(etablissements_csv)
    assert set(df["code_rne"]) == {"0380004Y", "0380099Z"}  # pas le lycée 0380018N


def test_public_prive_flag_and_geometry(etablissements_csv):
    df = _parse_etablissements_csv(etablissements_csv)
    row_pub = df[df["code_rne"] == "0380004Y"].iloc[0]
    row_priv = df[df["code_rne"] == "0380099Z"].iloc[0]
    assert row_pub["secteur"] == "Public"
    assert row_priv["secteur"] == "Privé"
    assert row_pub["kind"] == "college"
    assert row_pub.geometry.x == pytest.approx(900580.1)
    assert row_pub.geometry.y == pytest.approx(6507116.4)
    assert df.crs is not None and str(df.crs).endswith("2154")


def test_equip_id_is_stable_and_prefixed(etablissements_csv):
    df = _parse_etablissements_csv(etablissements_csv)
    assert all(str(i).startswith("college-") for i in df["equip_id"])
    assert df["equip_id"].is_unique


def pd_isna(v) -> bool:
    import pandas as pd
    return pd.isna(v)
