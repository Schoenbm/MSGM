"""Tests du loader MOBPRO (flux domicile-travail) — sans réseau (cache mocké)."""

import io
import zipfile
from pathlib import Path

import src.loaders.mobpro as mobpro


def _make_zip(tmp_path: Path) -> Path:
    """Crée une archive zip contenant un CSV MOBPRO minimal."""
    csv = (
        "CODGEO;LIBGEO;DCLT;L_DCLT;NBFLUX_C22_ACTOCC15P\n"
        "38185;Grenoble;38185;Grenoble;100.0\n"
        "38185;Grenoble;38421;Saint-Martin-d'Heres;30.0\n"
        "38151;Echirolles;38185;Grenoble;25.0\n"
    )
    zpath = tmp_path / "mobpro.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("base-flux-mobilite-2022.csv", csv)
    return zpath


def test_load_all_flux(tmp_path, monkeypatch):
    zpath = _make_zip(tmp_path)
    monkeypatch.setattr(mobpro, "ensure_cached", lambda *a, **k: zpath)
    df = mobpro.load_mobpro()
    assert len(df) == 3
    assert set(df.columns) >= {"CODGEO", "DCLT", "NBFLUX_C22_ACTOCC15P"}
    # codes communes conservés en chaîne (zéros de tête préservés)
    import pandas as pd
    assert pd.api.types.is_string_dtype(df["CODGEO"])
    assert df["CODGEO"].iloc[0] == "38185"


def test_filter_by_commune_residence(tmp_path, monkeypatch):
    zpath = _make_zip(tmp_path)
    monkeypatch.setattr(mobpro, "ensure_cached", lambda *a, **k: zpath)
    df = mobpro.load_mobpro(communes=["38185"])
    # seuls les flux dont la RÉSIDENCE est Grenoble (le travail reste libre)
    assert set(df["CODGEO"]) == {"38185"}
    assert len(df) == 2
    assert round(df["NBFLUX_C22_ACTOCC15P"].sum()) == 130


def test_filter_empty_when_unknown_commune(tmp_path, monkeypatch):
    zpath = _make_zip(tmp_path)
    monkeypatch.setattr(mobpro, "ensure_cached", lambda *a, **k: zpath)
    df = mobpro.load_mobpro(communes=["99999"])
    assert df.empty
