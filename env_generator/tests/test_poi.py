"""Tests for loaders/poi.py — POI stratégiques + éducation → footprint."""

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import Point, Polygon

from src.loaders.poi import (
    EDUCATION_FONCTIONS,
    STRATEGIC_FONCTIONS,
    _build_overpass_query,
    _resolve_fonction,
    match_education_to_buildings,
    match_pois_to_buildings,
    merge_fonctions,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

_POLY_A = Polygon([(0, 0), (20, 0), (20, 20), (0, 20)])    # 400 m²
_POLY_B = Polygon([(30, 0), (50, 0), (50, 20), (30, 20)])  # 400 m², disjoint
_POLY_C = Polygon([(60, 0), (80, 0), (80, 20), (60, 20)])  # 400 m²


def _make_buildings(ids=None, polys=None):
    ids = ids or ["BAT1", "BAT2", "BAT3"]
    polys = polys or [_POLY_A, _POLY_B, _POLY_C]
    return gpd.GeoDataFrame(
        {"ID": ids, "geometry": polys},
        crs="EPSG:2154",
    )


def _make_pois(rows):
    """Build a GeoDataFrame of POI (points and/or polygons) in EPSG:2154."""
    data = []
    for r in rows:
        data.append({
            "osm_id": r.get("osm_id", 1),
            "osm_type": r.get("osm_type", "node"),
            "fonction": r["fonction"],
            "name": r.get("name", ""),
            "geometry": r["geometry"],
        })
    return gpd.GeoDataFrame(data, crs="EPSG:2154")


# ── _resolve_fonction ────────────────────────────────────────────────────────

class TestResolveFonction:
    def test_hospital(self):
        assert _resolve_fonction({"amenity": "hospital"}) == "hopital"

    def test_townhall(self):
        assert _resolve_fonction({"amenity": "townhall"}) == "mairie"

    def test_fire_station(self):
        assert _resolve_fonction({"amenity": "fire_station"}) == "caserne"

    def test_police(self):
        assert _resolve_fonction({"amenity": "police"}) == "police"

    def test_place_of_worship(self):
        assert _resolve_fonction({"amenity": "place_of_worship"}) == "culte"

    def test_stadium(self):
        assert _resolve_fonction({"leisure": "stadium"}) == "stade"

    def test_sports_centre(self):
        assert _resolve_fonction({"leisure": "sports_centre"}) == "gymnase"

    def test_station(self):
        assert _resolve_fonction({"railway": "station"}) == "gare"

    def test_social_facility_wildcard(self):
        assert _resolve_fonction({"social_facility": "nursing_home"}) == "ehpad"

    def test_mall(self):
        assert _resolve_fonction({"shop": "mall"}) == "centre_commercial"

    def test_unknown_returns_none(self):
        assert _resolve_fonction({"building": "yes"}) is None

    def test_empty_tags(self):
        assert _resolve_fonction({}) is None


# ── match_pois_to_buildings (points) ─────────────────────────────────────────

class TestMatchPoisPoints:
    def test_point_inside_building(self):
        buildings = _make_buildings()
        pois = _make_pois([{
            "fonction": "hopital",
            "geometry": Point(10, 10),  # inside POLY_A
        }])
        result = match_pois_to_buildings(buildings, pois)
        assert result == {"BAT1": "hopital"}

    def test_point_outside_all_buildings(self):
        buildings = _make_buildings()
        pois = _make_pois([{
            "fonction": "mairie",
            "geometry": Point(100, 100),  # outside all
        }])
        result = match_pois_to_buildings(buildings, pois)
        assert result == {}

    def test_multiple_points_different_buildings(self):
        buildings = _make_buildings()
        pois = _make_pois([
            {"osm_id": 1, "fonction": "hopital", "geometry": Point(10, 10)},
            {"osm_id": 2, "fonction": "mairie", "geometry": Point(40, 10)},
        ])
        result = match_pois_to_buildings(buildings, pois)
        assert result == {"BAT1": "hopital", "BAT2": "mairie"}

    def test_first_poi_wins_on_same_building(self):
        buildings = _make_buildings()
        pois = _make_pois([
            {"osm_id": 1, "fonction": "hopital", "geometry": Point(10, 10)},
            {"osm_id": 2, "fonction": "mairie", "geometry": Point(15, 15)},
        ])
        result = match_pois_to_buildings(buildings, pois)
        # Both points fall in BAT1 — first one wins
        assert result["BAT1"] == "hopital"

    def test_empty_pois(self):
        buildings = _make_buildings()
        pois = gpd.GeoDataFrame(
            columns=["osm_id", "osm_type", "fonction", "name", "geometry"],
            geometry="geometry", crs="EPSG:2154",
        )
        assert match_pois_to_buildings(buildings, pois) == {}

    def test_none_pois(self):
        buildings = _make_buildings()
        assert match_pois_to_buildings(buildings, None) == {}


# ── match_pois_to_buildings (polygones) ──────────────────────────────────────

class TestMatchPoisPolygons:
    def test_overlapping_polygon(self):
        buildings = _make_buildings()
        # POI polygon covers most of POLY_A
        poi_poly = Polygon([(0, 0), (20, 0), (20, 20), (0, 20)])
        pois = _make_pois([{
            "osm_type": "way",
            "fonction": "stade",
            "geometry": poi_poly,
        }])
        result = match_pois_to_buildings(buildings, pois)
        assert result.get("BAT1") == "stade"

    def test_non_overlapping_polygon(self):
        buildings = _make_buildings()
        poi_poly = Polygon([(200, 200), (210, 200), (210, 210), (200, 210)])
        pois = _make_pois([{
            "osm_type": "way",
            "fonction": "stade",
            "geometry": poi_poly,
        }])
        result = match_pois_to_buildings(buildings, pois)
        assert "BAT1" not in result


# ── match_education_to_buildings ─────────────────────────────────────────────

class TestMatchEducation:
    def test_point_inside_building(self):
        buildings = _make_buildings()
        edu = gpd.GeoDataFrame(
            {"equip_id": ["bpe-0"], "kind": ["ecole"], "capacity": [1.0],
             "nom": ["Ecole X"], "geometry": [Point(10, 10)]},
            crs="EPSG:2154",
        )
        result = match_education_to_buildings(buildings, edu)
        assert result == {"BAT1": "ecole"}

    def test_multiple_kinds(self):
        buildings = _make_buildings()
        edu = gpd.GeoDataFrame(
            {"equip_id": ["bpe-0", "bpe-1"], "kind": ["ecole", "college"],
             "capacity": [1.0, 1.0], "nom": ["A", "B"],
             "geometry": [Point(10, 10), Point(40, 10)]},
            crs="EPSG:2154",
        )
        result = match_education_to_buildings(buildings, edu)
        assert result == {"BAT1": "ecole", "BAT2": "college"}

    def test_empty_education(self):
        buildings = _make_buildings()
        assert match_education_to_buildings(buildings, None) == {}

    def test_outside_building(self):
        buildings = _make_buildings()
        edu = gpd.GeoDataFrame(
            {"equip_id": ["bpe-0"], "kind": ["lycee"], "capacity": [1.0],
             "nom": ["Lycee Y"], "geometry": [Point(100, 100)]},
            crs="EPSG:2154",
        )
        result = match_education_to_buildings(buildings, edu)
        assert result == {}


# ── merge_fonctions ──────────────────────────────────────────────────────────

class TestMergeFonctions:
    def test_first_source_wins(self):
        osm = {"BAT1": "hopital"}
        bdnb = {"BAT1": "centre_commercial"}
        result = merge_fonctions(osm, bdnb)
        assert result["BAT1"] == "hopital"

    def test_fallback_on_missing(self):
        osm = {"BAT1": "hopital"}
        bdnb = {"BAT2": "centre_commercial"}
        result = merge_fonctions(osm, bdnb)
        assert result == {"BAT1": "hopital", "BAT2": "centre_commercial"}

    def test_three_sources(self):
        osm = {"BAT1": "hopital"}
        edu = {"BAT2": "ecole"}
        bdnb = {"BAT1": "centre_commercial", "BAT3": "stade"}
        result = merge_fonctions(osm, edu, bdnb)
        assert result == {"BAT1": "hopital", "BAT2": "ecole", "BAT3": "stade"}

    def test_empty_sources(self):
        assert merge_fonctions({}, {}) == {}


# ── Taxonomie ────────────────────────────────────────────────────────────────

class TestTaxonomy:
    def test_no_overlap(self):
        assert EDUCATION_FONCTIONS & STRATEGIC_FONCTIONS == set()

    def test_education_contains_expected(self):
        assert {"ecole", "college", "lycee", "creche"} <= EDUCATION_FONCTIONS

    def test_strategic_contains_expected(self):
        assert {"hopital", "mairie", "caserne", "police", "gare"} <= STRATEGIC_FONCTIONS


# ── _build_overpass_query ────────────────────────────────────────────────────

class TestBuildOverpassQuery:
    def test_contains_bbox(self):
        q = _build_overpass_query((5.7, 45.1, 5.8, 45.2))
        assert "45.1" in q and "5.7" in q

    def test_contains_amenity_tags(self):
        q = _build_overpass_query((5.7, 45.1, 5.8, 45.2))
        assert '"amenity"="hospital"' in q
        assert '"amenity"="townhall"' in q
        assert '"leisure"="stadium"' in q
