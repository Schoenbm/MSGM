"""Tests de CONTRAT — parsing du fichier BAN (Base Adresse Nationale), chantier
carte scolaire.

`_parse_ban_csv(path, communes=None)` : parseur PUR (séparé du téléchargement,
même pattern que `iris._load_csv_from_zip` / `carte_scolaire._parse_secteurs_csv`)
de l'export département BAN (`adresses-<dept>.csv.gz`, colonnes réelles vues sur
le fichier Isère : id;id_fantoir;numero;rep;nom_voie;code_postal;code_insee;
nom_commune;...;x;y;lon;lat;...). `x`/`y` sont DÉJÀ en Lambert-93 — pas de
reprojection.

Sortie normalisée attendue (colonnes du GeoDataFrame) : code_insee, nom_voie_norm,
numero (int), geometry (EPSG:2154). Filtrage optionnel à un ensemble de communes
(le fichier est au niveau département, notre zone d'étude ne l'est pas toujours).

Skippe tant que `src.loaders.ban` n'existe pas (test-first).
"""

from pathlib import Path

import pytest

ban = pytest.importorskip("src.loaders.ban")
if not hasattr(ban, "_parse_ban_csv"):
    pytest.skip("_parse_ban_csv pas encore implémenté (chantier carte scolaire)",
                allow_module_level=True)

from src.loaders.ban import _parse_ban_csv  # noqa: E402

# Extrait RÉEL du fichier adresses-38.csv.gz (dé-gzippé), deux communes.
_BAN_CSV = (
    "id;id_fantoir;numero;rep;nom_voie;code_postal;code_insee;nom_commune;"
    "code_insee_ancienne_commune;nom_ancienne_commune;x;y;lon;lat;type_position;"
    "alias;nom_ld;libelle_acheminement;nom_afnor;source_position;source_nom_voie;"
    "certification_commune;cad_parcelles\n"
    "38003_b030_00001;38003_B030;1;;Le Sorbier Sud;38150;38003;Agnin;;;"
    "845232.07;6473205.58;4.854581;45.342886;entrée;;;AGNIN;LE SORBIER SUD;"
    "commune;commune;0;380030000A1585\n"
    "38185_xyz_00350;38185_XYZ;350;;Rue d'Italie;38000;38185;Grenoble;;;"
    "912000.5;6457000.2;5.72;45.19;entrée;;;GRENOBLE;RUE D ITALIE;"
    "commune;commune;0;\n"
)


@pytest.fixture()
def ban_csv(tmp_path: Path) -> Path:
    p = tmp_path / "adresses-38.csv"
    p.write_text(_BAN_CSV, encoding="utf-8")
    return p


def test_columns_and_crs(ban_csv):
    gdf = _parse_ban_csv(ban_csv)
    for col in ("code_insee", "nom_voie_norm", "numero", "geometry"):
        assert col in gdf.columns
    assert str(gdf.crs).endswith("2154")


def test_geometry_uses_lambert93_xy_directly(ban_csv):
    gdf = _parse_ban_csv(ban_csv)
    row = gdf[gdf["code_insee"] == "38003"].iloc[0]
    assert row.geometry.x == pytest.approx(845232.07)
    assert row.geometry.y == pytest.approx(6473205.58)


def test_nom_voie_is_normalized(ban_csv):
    gdf = _parse_ban_csv(ban_csv)
    row = gdf[gdf["code_insee"] == "38185"].iloc[0]
    assert row["nom_voie_norm"] == "RUE D ITALIE"


def test_numero_is_integer(ban_csv):
    gdf = _parse_ban_csv(ban_csv)
    assert gdf["numero"].dtype.kind in ("i", "u")


def test_filter_by_communes(ban_csv):
    gdf = _parse_ban_csv(ban_csv, communes={"38185"})
    assert set(gdf["code_insee"]) == {"38185"}
