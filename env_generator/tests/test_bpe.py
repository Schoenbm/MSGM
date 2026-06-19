"""Tests du loader BPE (équipements éducatifs) — sans réseau (cache mocké)."""

import zipfile
from pathlib import Path

import src.loaders.bpe as bpe

# En-tête minimal reprenant les colonnes utilisées par le loader.
_HEADER = "TYPEQU;DEP;DEPCOM;LAMBERT_X;LAMBERT_Y;EPSG;CAPACITE_D_ACCUEIL;NOMRS"


def _make_zip(tmp_path: Path, rows: list[str]) -> Path:
    csv = _HEADER + "\n" + "\n".join(rows) + "\n"
    zpath = tmp_path / "bpe.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("BPE24.csv", csv)
    return zpath


def test_maps_typequ_to_levels(tmp_path, monkeypatch):
    rows = [
        "C107;38;38185;910000;6500000;2154;;ECOLE MAT",     # ecole
        "C201;38;38185;910100;6500100;2154;;COLLEGE X",     # college
        "C301;38;38185;910200;6500200;2154;;LYCEE Y",       # lycee
        "D502;38;38185;910300;6500300;2154;;CRECHE Z",      # creche
    ]
    monkeypatch.setattr(bpe, "ensure_cached", lambda *a, **k: _make_zip(tmp_path, rows))
    edu = bpe.load_bpe_education(departement="38")
    assert edu["kind"].value_counts().to_dict() == {
        "ecole": 1, "college": 1, "lycee": 1, "creche": 1
    }
    assert edu.crs.to_epsg() == 2154
    assert {"equip_id", "kind", "capacity", "nom"}.issubset(edu.columns)


def test_filters_department_and_drops_other_equipment(tmp_path, monkeypatch):
    rows = [
        "C107;38;38185;910000;6500000;2154;;ECOLE 38",
        "C107;69;69123;800000;6500000;2154;;ECOLE 69",   # autre département
        "B101;38;38185;910000;6500000;2154;;COMMERCE",   # pas un équipement éducatif
    ]
    monkeypatch.setattr(bpe, "ensure_cached", lambda *a, **k: _make_zip(tmp_path, rows))
    edu = bpe.load_bpe_education(departement="38")
    assert len(edu) == 1
    assert edu["nom"].iloc[0] == "ECOLE 38"


def test_uses_capacity_when_present(tmp_path, monkeypatch):
    rows = [
        "C301;38;38185;910000;6500000;2154;1200;LYCEE GRAND",
        "C107;38;38185;910100;6500100;2154;;ECOLE SANS CAPA",
    ]
    monkeypatch.setattr(bpe, "ensure_cached", lambda *a, **k: _make_zip(tmp_path, rows))
    edu = bpe.load_bpe_education(departement="38")
    caps = dict(zip(edu["nom"], edu["capacity"]))
    assert caps["LYCEE GRAND"] == 1200.0
    assert caps["ECOLE SANS CAPA"] == 1.0   # capacité absente → 1


def test_drops_non_lambert93_coords(tmp_path, monkeypatch):
    rows = [
        "C107;38;38185;910000;6500000;2154;;ECOLE METRO",
        "C107;971;97101;;;5490;;ECOLE DOM",   # coords absentes / autre EPSG
    ]
    monkeypatch.setattr(bpe, "ensure_cached", lambda *a, **k: _make_zip(tmp_path, rows))
    # département métropole : la ligne DOM est de toute façon filtrée par DEP
    edu = bpe.load_bpe_education(departement="38")
    assert len(edu) == 1
    assert edu.geometry.iloc[0].x == 910000.0
