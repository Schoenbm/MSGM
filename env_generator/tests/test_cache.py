"""Tests for loaders/cache.py — pipeline de cache unique + validateurs."""

import zipfile

import geopandas as gpd
import pytest
from shapely.geometry import Point

from src.loaders.cache import (
    ensure_cached,
    valid_dir_with,
    valid_geofile,
    valid_zip,
)


# ── ensure_cached ───────────────────────────────────────────────────────────

class TestEnsureCached:
    def test_produces_when_missing(self, tmp_path):
        dest = tmp_path / "data.txt"
        ensure_cached(dest, produce=lambda p: p.write_text("hello"))
        assert dest.read_text() == "hello"

    def test_reuses_valid_cache_without_producing(self, tmp_path):
        dest = tmp_path / "data.txt"
        dest.write_text("cached")
        calls = []
        ensure_cached(
            dest,
            produce=lambda p: calls.append(p) or p.write_text("new"),
            validate=lambda p: True,
        )
        assert calls == []                  # produce jamais appelé
        assert dest.read_text() == "cached"

    def test_regenerates_when_cache_invalid(self, tmp_path):
        dest = tmp_path / "data.txt"
        dest.write_text("corrompu")
        ensure_cached(
            dest,
            produce=lambda p: p.write_text("frais"),
            validate=lambda p: p.read_text() == "frais",
        )
        assert dest.read_text() == "frais"

    def test_failed_produce_leaves_nothing(self, tmp_path):
        dest = tmp_path / "data.txt"

        def boom(p):
            p.write_text("partiel")
            raise RuntimeError("coupure")

        with pytest.raises(RuntimeError):
            ensure_cached(dest, produce=boom)
        assert not dest.exists()
        assert list(tmp_path.iterdir()) == []   # ni cache ni .part

    def test_invalid_produced_data_raises_and_cleans(self, tmp_path):
        dest = tmp_path / "data.txt"
        with pytest.raises(IOError):
            ensure_cached(
                dest,
                produce=lambda p: p.write_text("vide"),
                validate=lambda p: False,        # données produites jugées invalides
            )
        assert not dest.exists()
        assert list(tmp_path.iterdir()) == []

    def test_invalid_cache_then_failed_produce_removes_old(self, tmp_path):
        # Cache existant mais invalide + production qui échoue → l'ancien cache
        # corrompu doit avoir été retiré (pas réutilisé silencieusement).
        dest = tmp_path / "data.txt"
        dest.write_text("vieux-corrompu")

        def boom(p):
            raise RuntimeError("coupure")

        with pytest.raises(RuntimeError):
            ensure_cached(dest, produce=boom, validate=lambda p: False)
        assert not dest.exists()

    def test_directory_cache(self, tmp_path):
        dest = tmp_path / "extracted"

        def produce(d):
            d.mkdir(parents=True, exist_ok=True)
            (d / "inner.shp").write_text("x")

        ensure_cached(
            dest,
            produce=produce,
            validate=valid_dir_with(glob="*.shp"),
        )
        assert (dest / "inner.shp").exists()


# ── Validateurs ─────────────────────────────────────────────────────────────

class TestValidators:
    def test_valid_zip_true_for_real_zip(self, tmp_path):
        z = tmp_path / "ok.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("a.txt", "content")
        assert valid_zip(z) is True

    def test_valid_zip_false_for_truncated(self, tmp_path):
        # Un vrai zip tronqué (sans end-of-central-directory) → invalide.
        z = tmp_path / "ok.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("a.txt", "content" * 1000)
        data = z.read_bytes()
        z.write_bytes(data[: len(data) // 2])    # on coupe la fin
        assert valid_zip(z) is False

    def test_valid_geofile_true_for_real_gpkg(self, tmp_path):
        g = tmp_path / "pts.gpkg"
        gdf = gpd.GeoDataFrame({"v": [1]}, geometry=[Point(0, 0)], crs="EPSG:2154")
        gdf.to_file(g, driver="GPKG")
        assert valid_geofile(g) is True

    def test_valid_geofile_false_for_garbage(self, tmp_path):
        g = tmp_path / "broken.gpkg"
        g.write_bytes(b"not a geopackage at all")
        assert valid_geofile(g) is False

    def test_valid_dir_with_predicate(self, tmp_path):
        d = tmp_path / "d"
        d.mkdir()
        (d / "EMPRISE.shp").write_text("x")
        # seul EMPRISE.shp présent → predicate l'exclut → invalide
        v = valid_dir_with(glob="*.shp", predicate=lambda p: p.stem != "EMPRISE")
        assert v(d) is False
        (d / "CONTOURS-IRIS.shp").write_text("x")
        assert v(d) is True
