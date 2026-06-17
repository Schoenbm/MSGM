"""Tests for loaders/roads.py — réseau routier OSM (osmnx mocké, sans réseau)."""

from unittest.mock import patch

import geopandas as gpd
import pytest
from shapely.geometry import LineString, box

from src.loaders import roads
from src.loaders.roads import fetch_road_network, _to_wgs84_polygon, _area_hash


# ── Fake osmnx ────────────────────────────────────────────────────────────────

def _fake_edges() -> gpd.GeoDataFrame:
    """Arêtes factices en WGS84, à la manière de osmnx.graph_to_gdfs(nodes=False)."""
    return gpd.GeoDataFrame(
        {
            "highway": [["residential", "service"]],   # liste OSM → à stringifier
            "name": ["Rue Test"],
            "length": [42.0],
            "geometry": [LineString([(5.72, 45.18), (5.73, 45.19)])],
        },
        crs="EPSG:4326",
    )


class _FakeOx:
    def __init__(self):
        self.networks_seen = []

    def graph_from_polygon(self, poly, network_type, simplify):
        self.networks_seen.append(network_type)
        return ("fake-graph", network_type)

    def graph_to_gdfs(self, graph, nodes):
        assert nodes is False
        return _fake_edges()


def _area_2154() -> "object":
    """Petite emprise autour de Grenoble, géométrie Lambert-93."""
    poly_wgs84 = box(5.71, 45.17, 5.74, 45.20)
    return gpd.GeoSeries([poly_wgs84], crs="EPSG:4326").to_crs("EPSG:2154").iloc[0]


# ── _to_wgs84_polygon ─────────────────────────────────────────────────────────

class TestToWgs84:
    def test_reprojects_geometry(self):
        poly = _area_2154()
        out = _to_wgs84_polygon(poly)
        minx, miny, maxx, maxy = out.bounds
        assert 5.6 < minx < 5.8 and 45.1 < miny < 45.3

    def test_accepts_geodataframe(self):
        gdf = gpd.GeoDataFrame(geometry=[_area_2154()], crs="EPSG:2154")
        out = _to_wgs84_polygon(gdf)
        assert 5.6 < out.bounds[0] < 5.8


# ── fetch_road_network ────────────────────────────────────────────────────────

class TestFetchRoadNetwork:
    def test_returns_one_gdf_per_network(self, tmp_path):
        fake = _FakeOx()
        with patch("src.loaders.roads._ox", return_value=fake):
            out = fetch_road_network(
                _area_2154(), network_types=("walk", "drive"), cache_dir=tmp_path
            )
        assert set(out) == {"walk", "drive"}
        assert fake.networks_seen == ["walk", "drive"]

    def test_edges_reprojected_to_lambert93(self, tmp_path):
        with patch("src.loaders.roads._ox", return_value=_FakeOx()):
            out = fetch_road_network(_area_2154(), network_types=("walk",), cache_dir=tmp_path)
        assert out["walk"].crs.to_string() == "EPSG:2154"

    def test_list_columns_stringified(self, tmp_path):
        with patch("src.loaders.roads._ox", return_value=_FakeOx()):
            out = fetch_road_network(_area_2154(), network_types=("walk",), cache_dir=tmp_path)
        # la liste OSM doit être convertie en chaîne (sérialisable shapefile/gpkg)
        assert isinstance(out["walk"]["highway"].iloc[0], str)

    def test_cache_written_and_reused(self, tmp_path):
        # 1er appel : télécharge (mock) et écrit le cache
        with patch("src.loaders.roads._ox", return_value=_FakeOx()):
            fetch_road_network(_area_2154(), network_types=("walk",), cache_dir=tmp_path)
        h = _area_hash(_to_wgs84_polygon(_area_2154()))
        assert (tmp_path / f"roads_walk_{h}.gpkg").exists()

        # 2e appel : doit lire le cache sans toucher osmnx
        def _boom():
            raise AssertionError("osmnx ne doit pas être appelé quand le cache existe")
        with patch("src.loaders.roads._ox", side_effect=_boom):
            out = fetch_road_network(_area_2154(), network_types=("walk",), cache_dir=tmp_path)
        assert len(out["walk"]) == 1
