"""Tests for loaders/iris.py — download, extraction and data loading logic."""

import io
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Polygon

from src.loaders.iris import (
    _download,
    _load_csv_from_zip,
    load_iris,
    _TAILLE_MEN_DEFAUT,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_iris_shp() -> gpd.GeoDataFrame:
    polys = [Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]),
             Polygon([(1, 0), (2, 0), (2, 1), (1, 1)])]
    return gpd.GeoDataFrame(
        {
            "CODE_IRIS": ["381230000", "381231000"],
            "INSEE_COM": ["38123", "38123"],
            "NOM_IRIS": ["IRIS A", "IRIS B"],
            "geometry": polys,
        },
        crs="EPSG:2154",
    )


def _make_pop_csv(sep=";") -> bytes:
    content = sep.join(["IRIS", "P22_POP", "P22_PMEN"]) + "\n"
    content += sep.join(["381230000", "1000", "950"]) + "\n"
    content += sep.join(["381231000", "500",  "480"]) + "\n"
    return content.encode("utf-8")


def _make_log_csv(sep=";") -> bytes:
    content = sep.join(["IRIS", "P22_MEN"]) + "\n"
    content += sep.join(["381230000", "400"]) + "\n"
    content += sep.join(["381231000", "200"]) + "\n"
    return content.encode("utf-8")


def _zip_bytes(filename: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(filename, data)
    return buf.getvalue()


# ── _download ─────────────────────────────────────────────────────────────────

class TestDownload:
    def test_skips_if_cached(self, tmp_path):
        dest = tmp_path / "file.zip"
        dest.write_bytes(b"cached")
        with patch("src.loaders.iris.requests.get") as mock_get:
            result = _download("http://example.com/file.zip", dest)
        mock_get.assert_not_called()
        assert result == dest

    def test_downloads_when_missing(self, tmp_path):
        dest = tmp_path / "file.zip"
        mock_response = MagicMock()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.raise_for_status = MagicMock()
        mock_response.headers = {"content-length": "5"}
        mock_response.iter_content = MagicMock(return_value=[b"hello"])

        with patch("src.loaders.iris.requests.get", return_value=mock_response):
            result = _download("http://example.com/file.zip", dest)

        assert dest.exists()
        assert dest.read_bytes() == b"hello"
        assert result == dest

    def test_creates_parent_directories(self, tmp_path):
        dest = tmp_path / "subdir" / "nested" / "file.zip"
        mock_response = MagicMock()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.raise_for_status = MagicMock()
        mock_response.headers = {}
        mock_response.iter_content = MagicMock(return_value=[b"data"])

        with patch("src.loaders.iris.requests.get", return_value=mock_response):
            _download("http://example.com/file.zip", dest)

        assert dest.parent.exists()


# ── _load_csv_from_zip ────────────────────────────────────────────────────────

class TestLoadCsvFromZip:
    def test_reads_csv_and_filters_by_dep(self, tmp_path):
        csv_data = (
            b"IRIS;P22_POP\n"
            b"381230000;1000\n"
            b"690010000;2000\n"
        )
        zip_data = _zip_bytes("data.csv", csv_data)
        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(zip_data)

        with patch("src.loaders.iris._download", return_value=zip_path):
            df = _load_csv_from_zip("http://x", "test.zip", dep_code="38")

        assert len(df) == 1
        assert df.iloc[0]["IRIS"] == "381230000"

    def test_iris_column_kept_as_str(self, tmp_path):
        csv_data = b"IRIS;P22_POP\n011110000;500\n"
        zip_data = _zip_bytes("data.csv", csv_data)
        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(zip_data)

        with patch("src.loaders.iris._download", return_value=zip_path):
            df = _load_csv_from_zip("http://x", "test.zip", dep_code="01")

        assert pd.api.types.is_string_dtype(df["IRIS"])
        assert df.iloc[0]["IRIS"] == "011110000"

    def test_empty_result_when_no_match(self, tmp_path):
        csv_data = b"IRIS;P22_POP\n690010000;2000\n"
        zip_data = _zip_bytes("data.csv", csv_data)
        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(zip_data)

        with patch("src.loaders.iris._download", return_value=zip_path):
            df = _load_csv_from_zip("http://x", "test.zip", dep_code="38")

        assert df.empty


# ── load_iris ─────────────────────────────────────────────────────────────────

class TestLoadIris:
    def _patch_all(self, tmp_path):
        iris_shp = _make_iris_shp()
        pop_zip = tmp_path / "pop.zip"
        pop_zip.write_bytes(_zip_bytes("pop.csv", _make_pop_csv()))
        log_zip = tmp_path / "log.zip"
        log_zip.write_bytes(_zip_bytes("log.csv", _make_log_csv()))
        return iris_shp, pop_zip, log_zip

    def test_output_columns_present(self, tmp_path):
        iris_shp, pop_zip, log_zip = self._patch_all(tmp_path)
        with (
            patch("src.loaders.iris._download", side_effect=[tmp_path / "fake.7z", pop_zip, log_zip]),
            patch("src.loaders.iris._extract_contours_7z", return_value="unused"),
            patch("src.loaders.iris.gpd.read_file", return_value=iris_shp),
        ):
            result = load_iris(dep_code="38")
        assert "Ind_total" in result.columns
        assert "taille_moy_menage" in result.columns
        assert "geometry" in result.columns

    def test_filter_by_iris_codes(self, tmp_path):
        iris_shp, pop_zip, log_zip = self._patch_all(tmp_path)
        with (
            patch("src.loaders.iris._download", side_effect=[tmp_path / "fake.7z", pop_zip, log_zip]),
            patch("src.loaders.iris._extract_contours_7z", return_value="unused"),
            patch("src.loaders.iris.gpd.read_file", return_value=iris_shp),
        ):
            result = load_iris(iris_codes=["381230000"])
        assert len(result) == 1  # un seul IRIS correspondant

    def test_ind_total_matches_p22_pop(self, tmp_path):
        iris_shp, pop_zip, log_zip = self._patch_all(tmp_path)
        with (
            patch("src.loaders.iris._download", side_effect=[tmp_path / "fake.7z", pop_zip, log_zip]),
            patch("src.loaders.iris._extract_contours_7z", return_value="unused"),
            patch("src.loaders.iris.gpd.read_file", return_value=iris_shp),
        ):
            result = load_iris(dep_code="38")
        assert result.loc[result["CODE_IRIS"] == "381230000", "Ind_total"].iloc[0] == 1000.0
        assert result.loc[result["CODE_IRIS"] == "381231000", "Ind_total"].iloc[0] == 500.0

    def test_taille_moy_menage_computed(self, tmp_path):
        iris_shp, pop_zip, log_zip = self._patch_all(tmp_path)
        with (
            patch("src.loaders.iris._download", side_effect=[tmp_path / "fake.7z", pop_zip, log_zip]),
            patch("src.loaders.iris._extract_contours_7z", return_value="unused"),
            patch("src.loaders.iris.gpd.read_file", return_value=iris_shp),
        ):
            result = load_iris(dep_code="38")
        tmm_a = result.loc[result["CODE_IRIS"] == "381230000", "taille_moy_menage"].iloc[0]
        assert abs(tmm_a - 1000 / 400) < 0.01

    def test_fallback_taille_when_zero_menages(self, tmp_path):
        iris_shp = _make_iris_shp().iloc[:1].copy()
        log_csv = b"IRIS;P22_MEN\n381230000;0\n"
        pop_csv = b"IRIS;P22_POP;P22_PMEN\n381230000;300;280\n"
        pop_zip = tmp_path / "pop.zip"
        log_zip = tmp_path / "log.zip"
        pop_zip.write_bytes(_zip_bytes("pop.csv", pop_csv))
        log_zip.write_bytes(_zip_bytes("log.csv", log_csv))
        with (
            patch("src.loaders.iris._download", side_effect=[tmp_path / "fake.7z", pop_zip, log_zip]),
            patch("src.loaders.iris._extract_contours_7z", return_value="unused"),
            patch("src.loaders.iris.gpd.read_file", return_value=iris_shp),
        ):
            result = load_iris(dep_code="38")
        assert result.iloc[0]["taille_moy_menage"] == _TAILLE_MEN_DEFAUT

    def test_missing_census_data_gives_zero_pop(self, tmp_path):
        iris_shp = _make_iris_shp()
        pop_csv = b"IRIS;P22_POP;P22_PMEN\n381230000;1000;950\n"
        log_csv = b"IRIS;P22_MEN\n381230000;400\n"
        pop_zip = tmp_path / "pop.zip"
        log_zip = tmp_path / "log.zip"
        pop_zip.write_bytes(_zip_bytes("pop.csv", pop_csv))
        log_zip.write_bytes(_zip_bytes("log.csv", log_csv))
        with (
            patch("src.loaders.iris._download", side_effect=[tmp_path / "fake.7z", pop_zip, log_zip]),
            patch("src.loaders.iris._extract_contours_7z", return_value="unused"),
            patch("src.loaders.iris.gpd.read_file", return_value=iris_shp),
        ):
            result = load_iris(dep_code="38")
        missing = result.loc[result["CODE_IRIS"] == "381231000", "Ind_total"].iloc[0]
        assert missing == 0.0

    def test_returns_geodataframe(self, tmp_path):
        iris_shp, pop_zip, log_zip = self._patch_all(tmp_path)
        with (
            patch("src.loaders.iris._download", side_effect=[tmp_path / "fake.7z", pop_zip, log_zip]),
            patch("src.loaders.iris._extract_contours_7z", return_value="unused"),
            patch("src.loaders.iris.gpd.read_file", return_value=iris_shp),
        ):
            result = load_iris(dep_code="38")
        assert isinstance(result, gpd.GeoDataFrame)
        assert result.crs is not None


# ── Sélecteur multi-niveaux (fusion ex-zone.py) ───────────────────────────────

from shapely.geometry import box as _box  # noqa: E402
from src.loaders.iris import (  # noqa: E402
    Selector,
    MissingIrisError,
    resolve_zone,
    validate_subset,
    _filter_contours,
    _selector_from_legacy,
    _resolve_geometry,
)


def _make_france_with_999() -> gpd.GeoDataFrame:
    polys = [Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]),
             Polygon([(2, 0), (3, 0), (3, 1), (2, 1)])]
    return gpd.GeoDataFrame(
        {"CODE_IRIS": ["381230000", "999999999"], "geometry": polys},
        crs="EPSG:2154",
    )


class TestResolveGeometryFallback:
    """Source unique : local complet → local ; local incomplet → error|download."""

    def test_error_when_local_incomplete(self):
        local = _make_iris_shp()  # 381230000, 381231000 (pas 999999999)
        sel = Selector("iris", ("381230000", "999999999"))
        with patch("src.loaders.iris._load_contours_raw", return_value=local):
            with pytest.raises(MissingIrisError) as exc:
                _resolve_geometry(sel, "dummy.shp", "URL", "error")
        assert "999999999" in exc.value.missing

    def test_download_fallback_when_incomplete(self):
        local = _make_iris_shp()              # 1er appel (local) — incomplet
        france = _make_france_with_999()      # 2e appel (download) — complet
        sel = Selector("iris", ("381230000", "999999999"))
        with patch("src.loaders.iris._load_contours_raw", side_effect=[local, france]):
            out = _resolve_geometry(sel, "dummy.shp", "URL", "download")
        assert set(out["CODE_IRIS"]) == {"381230000", "999999999"}

    def test_local_complete_uses_local(self):
        local = _make_iris_shp()
        sel = Selector("iris", ("381230000", "381231000"))
        with patch("src.loaders.iris._load_contours_raw", return_value=local) as m:
            out = _resolve_geometry(sel, "dummy.shp", "URL", "error")
        assert len(out) == 2
        assert m.call_count == 1  # pas de download

    def test_no_shp_does_not_raise(self):
        # sans shp local, on est en mode download : codes manquants juste warn
        france = _make_iris_shp()
        sel = Selector("iris", ("381230000", "999999999"))
        with patch("src.loaders.iris._load_contours_raw", return_value=france):
            out = _resolve_geometry(sel, None, "URL", "error")
        assert len(out) == 1


class TestSelector:
    def test_from_dict_valid(self):
        sel = Selector.from_dict({"type": "commune", "codes": ["38185", "38151"]})
        assert sel.type == "commune"
        assert sel.codes == ("38185", "38151")

    def test_from_dict_unknown_type(self):
        with pytest.raises(ValueError):
            Selector.from_dict({"type": "canton", "codes": ["38"]})

    def test_from_dict_empty_codes(self):
        with pytest.raises(ValueError):
            Selector.from_dict({"type": "iris", "codes": []})

    def test_legacy_iris_codes(self):
        assert _selector_from_legacy(["381230000"], "38") == Selector("iris", ("381230000",))

    def test_legacy_dep_fallback(self):
        assert _selector_from_legacy(None, "38") == Selector("departement", ("38",))


class TestFilterContours:
    def test_iris_exact(self):
        gdf = _make_iris_shp()
        out = _filter_contours(gdf, Selector("iris", ("381230000",)))
        assert list(out["CODE_IRIS"]) == ["381230000"]

    def test_commune_prefix(self):
        gdf = _make_iris_shp()
        out = _filter_contours(gdf, Selector("commune", ("38123",)))
        assert len(out) == 2  # les deux IRIS de la commune 38123

    def test_departement_prefix(self):
        gdf = _make_iris_shp()
        out = _filter_contours(gdf, Selector("departement", ("38",)))
        assert len(out) == 2

    def test_no_match_returns_empty(self):
        gdf = _make_iris_shp()
        out = _filter_contours(gdf, Selector("departement", ("69",)))
        assert out.empty


class TestResolveZone:
    def test_footprint_unions_iris(self, tmp_path):
        gdf = _make_iris_shp()
        with patch("src.loaders.iris._load_contours_raw", return_value=gdf):
            footprint, iris_gdf = resolve_zone(selector={"type": "commune", "codes": ["38123"]})
        assert len(iris_gdf) == 2
        assert abs(footprint.area - 2.0) < 1e-9  # deux carrés unitaires accolés

    def test_buffer_increases_area(self):
        gdf = _make_iris_shp()
        with patch("src.loaders.iris._load_contours_raw", return_value=gdf):
            base, _ = resolve_zone(selector=Selector("departement", ("38",)))
            buffered, _ = resolve_zone(selector=Selector("departement", ("38",)), buffer_m=0.5)
        assert buffered.area > base.area

    def test_empty_zone_raises(self):
        gdf = _make_iris_shp()
        with patch("src.loaders.iris._load_contours_raw", return_value=gdf):
            with pytest.raises(ValueError):
                resolve_zone(selector=Selector("departement", ("69",)))

    def test_shp_path_with_selector_filters(self):
        # shp local + sélecteur → on filtre le shp (zone population subset)
        gdf = _make_iris_shp()
        with patch("src.loaders.iris._load_contours_raw", return_value=gdf):
            _, iris_gdf = resolve_zone(selector=Selector("iris", ("381230000",)), shp_path="dummy.shp")
        assert len(iris_gdf) == 1

    def test_shp_path_without_selector_uses_all(self):
        # shp local sans sélecteur → toutes les lignes (zone region = shp entier)
        gdf = _make_iris_shp()
        with patch("src.loaders.iris._load_contours_raw", return_value=gdf):
            _, iris_gdf = resolve_zone(shp_path="dummy.shp")
        assert len(iris_gdf) == 2


class TestValidateSubset:
    def test_inner_inside_outer_true(self):
        outer = _box(0, 0, 10, 10)
        inner = gpd.GeoDataFrame(geometry=[_box(1, 1, 2, 2)], crs="EPSG:2154")
        assert validate_subset(inner, outer) is True

    def test_inner_outside_outer_false(self):
        outer = _box(0, 0, 1, 1)
        inner = gpd.GeoDataFrame(geometry=[_box(5, 5, 6, 6)], crs="EPSG:2154")
        assert validate_subset(inner, outer) is False


class TestLoadIrisSelector:
    def test_selector_dict_commune(self, tmp_path):
        iris_shp = _make_iris_shp()
        pop_zip = tmp_path / "pop.zip"
        pop_zip.write_bytes(_zip_bytes("pop.csv", _make_pop_csv()))
        log_zip = tmp_path / "log.zip"
        log_zip.write_bytes(_zip_bytes("log.csv", _make_log_csv()))
        with (
            patch("src.loaders.iris._download", side_effect=[tmp_path / "fake.7z", pop_zip, log_zip]),
            patch("src.loaders.iris._extract_contours_7z", return_value="unused"),
            patch("src.loaders.iris.gpd.read_file", return_value=iris_shp),
        ):
            result = load_iris(selector={"type": "commune", "codes": ["38123"]})
        assert len(result) == 2
        assert "Ind_total" in result.columns

    def test_custom_insee_urls_passed(self):
        # les URLs INSEE de la config sont bien transmises au téléchargement
        gdf = _make_iris_shp()
        captured = []

        def _fake_csv(url, cache_name, dep_code):
            captured.append(url)
            return pd.DataFrame({
                "IRIS": ["381230000", "381231000"],
                "P22_POP": [1, 1], "P22_PMEN": [1, 1], "P22_MEN": [1, 1],
            })

        with (
            patch("src.loaders.iris._load_contours_raw", return_value=gdf),
            patch("src.loaders.iris._load_csv_from_zip", side_effect=_fake_csv),
        ):
            load_iris(selector=Selector("departement", ("38",)), pop_url="POP", log_url="LOG")
        assert captured == ["POP", "LOG"]
