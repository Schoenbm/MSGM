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
