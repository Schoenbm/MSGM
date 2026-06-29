"""Tests du loader RP détail (ménages) — sans réseau (cache mocké)."""

import zipfile
from pathlib import Path

import src.loaders.rp_detail as rp

# En-tête minimal reprenant les colonnes lues par le loader (_USECOLS).
_HEADER = "CANTVILLE;NUMMI;DEPT;IRIS;AGED;SEXE;LPRM;STAT_GSEC;TACT;IPONDI"


def _row(cant, nummi, dept, iris, age, sexe, lprm, gsec, ipondi):
    return f"{cant};{nummi};{dept};{iris};{age};{sexe};{lprm};{gsec};11;{ipondi}"


def _make_zip(tmp_path: Path, rows: list[str]) -> Path:
    csv = _HEADER + "\n" + "\n".join(rows) + "\n"
    zpath = tmp_path / "rp.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr(rp._CSV_IN_ZIP, csv)
    return zpath


def _load(tmp_path, monkeypatch, rows, **kwargs):
    monkeypatch.setattr(rp, "ensure_cached", lambda *a, **k: _make_zip(tmp_path, rows))
    return rp.load_rp_households(**kwargs)


def test_reconstructs_household_with_roles(tmp_path, monkeypatch):
    # Couple (cadre + employé) avec 2 enfants, même NUMMI.
    rows = [
        _row("3801", "100", "38", "385010000", "040", "1", "1", "13", "1.0"),
        _row("3801", "100", "38", "385010000", "038", "2", "2", "15", "1.0"),
        _row("3801", "100", "38", "385010000", "010", "1", "3", "ZZ", "1.0"),
        _row("3801", "100", "38", "385010000", "007", "2", "3", "ZZ", "1.0"),
    ]
    df = _load(tmp_path, monkeypatch, rows)
    assert df["hh_id"].nunique() == 1
    hh = df[df["hh_id"] == "3801_100"]
    assert sorted(hh["role"]) == ["conjoint", "enfant", "enfant", "referent"]
    assert set(hh.loc[hh["role"] == "enfant", "csp"]) == {"mineur"}
    assert hh.loc[hh["role"] == "referent", "csp"].iloc[0] == "csp_cadres"


def test_hh_id_unique_across_cantons(tmp_path, monkeypatch):
    # Même NUMMI dans deux cantons différents → deux ménages distincts.
    rows = [
        _row("3801", "1", "38", "385010000", "030", "1", "1", "15", "1.0"),
        _row("3802", "1", "38", "385020000", "045", "2", "1", "13", "1.0"),
    ]
    df = _load(tmp_path, monkeypatch, rows)
    assert df["hh_id"].nunique() == 2
    assert set(df["hh_id"]) == {"3801_1", "3802_1"}


def test_minor_overrides_csp(tmp_path, monkeypatch):
    # Un actif occupé de 17 ans reste "mineur" (seuil adulte 18, cf. agents.py).
    rows = [_row("3801", "5", "38", "385010000", "017", "1", "3", "15", "1.0")]
    df = _load(tmp_path, monkeypatch, rows)
    assert df["csp"].iloc[0] == "mineur"
    assert bool(df["is_minor"].iloc[0]) is True


def test_stat_gsec_csp_mapping(tmp_path, monkeypatch):
    # Emploi (16) et chômeur ayant travaillé (26) → même CSP ouvriers.
    rows = [
        _row("3801", "1", "38", "385010000", "040", "1", "1", "16", "1.0"),
        _row("3801", "2", "38", "385010000", "041", "1", "1", "26", "1.0"),
    ]
    df = _load(tmp_path, monkeypatch, rows)
    assert set(df["csp"]) == {"csp_ouvriers"}


def test_filters_department(tmp_path, monkeypatch):
    rows = [
        _row("3801", "1", "38", "385010000", "030", "1", "1", "15", "1.0"),
        _row("6901", "1", "69", "690010000", "030", "1", "1", "15", "1.0"),  # Rhône
    ]
    df = _load(tmp_path, monkeypatch, rows, departement="38")
    assert len(df) == 1
    assert df["hh_id"].iloc[0] == "3801_1"


def test_filters_communes_by_iris_prefix(tmp_path, monkeypatch):
    # La commune = IRIS[:5] (PAS le CANTVILLE). Ici deux pseudo-cantons distincts,
    # on ne garde que la commune 38185 (IRIS 381850000).
    rows = [
        _row("3801", "1", "38", "385010000", "030", "1", "1", "15", "1.0"),  # commune 38501
        _row("3802", "1", "38", "381850000", "030", "1", "1", "15", "1.0"),  # commune 38185
    ]
    df = _load(tmp_path, monkeypatch, rows, departement="38", communes=["38185"])
    assert len(df) == 1
    assert df["hh_id"].iloc[0] == "3802_1"
    assert df["iris"].iloc[0].startswith("38185")


def test_weight_preserved_as_float(tmp_path, monkeypatch):
    rows = [_row("3801", "1", "38", "385010000", "030", "1", "1", "15", "1.234567")]
    df = _load(tmp_path, monkeypatch, rows)
    assert abs(df["weight"].iloc[0] - 1.234567) < 1e-9


def test_collective_housing_become_singletons(tmp_path, monkeypatch):
    # LPRM=Z (communautés : EHPAD, foyers...) : NON exclus (ils comptent dans les
    # marges base-ic), mais chacun devient un ménage SINGLETON — l'EHPAD ne forme
    # plus un faux ménage de N personnes via un NUMMI placeholder partagé.
    rows = [
        _row("3801", "100", "38", "385010000", "040", "1", "1", "13", "1.0"),  # ménage ordinaire
        _row("3801", "900", "38", "385010000", "077", "2", "Z", "32", "1.0"),  # collectif 1
        _row("3801", "900", "38", "385020000", "078", "2", "Z", "32", "1.0"),  # collectif 2 (même NUMMI)
    ]
    df = _load(tmp_path, monkeypatch, rows, departement="38")
    # 3 ménages : 1 ordinaire + 2 singletons collectifs (PAS 1 faux ménage de 2)
    assert df["hh_id"].nunique() == 3
    z = df[df["role"] == "hors_menage"]
    assert len(z) == 2 and z["hh_id"].nunique() == 2  # ids distincts
    # le ménage ordinaire reste groupé normalement
    assert (df["hh_id"] == "3801_100").sum() == 1


def test_empty_when_department_absent(tmp_path, monkeypatch):
    rows = [_row("6901", "1", "69", "690010000", "030", "1", "1", "15", "1.0")]
    df = _load(tmp_path, monkeypatch, rows, departement="38")
    assert df.empty
    assert list(df.columns) == ["hh_id", "iris", "age", "sexe", "role", "csp",
                                "is_minor", "weight"]
