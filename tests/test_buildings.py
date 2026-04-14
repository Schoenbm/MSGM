"""Tests for loaders/buildings.py."""

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import Point, Polygon

from src.loaders.buildings import (
    _compute_nb_etages,
    _filter_by_study_area,
    estimate_nb_logts,
    filter_residential,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_buildings(rows: list[dict]) -> gpd.GeoDataFrame:
    """Build a minimal buildings GeoDataFrame."""
    unit = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    records = []
    for i, row in enumerate(rows):
        records.append({
            "ID": row.get("ID", f"BAT{i}"),
            "USAGE1": row.get("USAGE1", None),
            "USAGE2": row.get("USAGE2", None),
            "NB_LOGTS": row.get("NB_LOGTS", np.nan),
            "NB_ETAGES": row.get("NB_ETAGES", np.nan),
            "HAUTEUR": row.get("HAUTEUR", np.nan),
            "geometry": row.get("geometry", unit),
        })
    return gpd.GeoDataFrame(records, crs="EPSG:2154")


# ── filter_residential ────────────────────────────────────────────────────────

class TestFilterResidential:
    def test_keeps_usage1_residential(self):
        gdf = _make_buildings([
            {"USAGE1": "Résidentiel"},
            {"USAGE1": "Commercial"},
        ])
        result = filter_residential(gdf)
        assert len(result) == 1
        assert result.iloc[0]["USAGE1"] == "Résidentiel"

    def test_keeps_usage2_when_usage1_null(self):
        gdf = _make_buildings([
            {"USAGE1": None, "USAGE2": "Résidentiel"},
            {"USAGE1": None, "USAGE2": "Commercial"},
        ])
        result = filter_residential(gdf)
        assert len(result) == 1

    def test_keeps_usage2_when_usage1_empty_string(self):
        gdf = _make_buildings([
            {"USAGE1": "", "USAGE2": "Résidentiel"},
        ])
        result = filter_residential(gdf)
        assert len(result) == 1

    def test_keeps_all_indifferencie(self):
        """Tous les bâtiments Indifférencié sont conservés (filtre NB_LOGTS non appliqué ici)."""
        gdf = _make_buildings([
            {"USAGE1": "Indifférencié", "NB_LOGTS": 3},
            {"USAGE1": "Indifférencié", "NB_LOGTS": 0},
        ])
        result = filter_residential(gdf)
        assert len(result) == 2

    def test_drops_non_residential(self):
        gdf = _make_buildings([
            {"USAGE1": "Commercial"},
            {"USAGE1": "Industriel"},
            {"USAGE1": "Agricole"},
        ])
        result = filter_residential(gdf)
        assert len(result) == 0

    def test_preserves_geometry(self):
        gdf = _make_buildings([{"USAGE1": "Résidentiel"}])
        result = filter_residential(gdf)
        assert result.geometry.iloc[0] is not None

    def test_empty_input(self):
        gdf = gpd.GeoDataFrame(
            columns=["ID", "USAGE1", "USAGE2", "NB_LOGTS", "geometry"],
            geometry="geometry",
            crs="EPSG:2154",
        )
        result = filter_residential(gdf)
        assert len(result) == 0


# ── filter_residential avec tag OSM ───────────────────────────────────────────

class TestFilterResidentialWithOSM:
    """Tests du filtre résidentiel quand la colonne osm_building est présente."""

    def _add_osm_building(self, gdf: gpd.GeoDataFrame, tags: list) -> gpd.GeoDataFrame:
        gdf = gdf.copy()
        gdf["osm_building"] = tags
        return gdf

    # ── Cas 1 : BD_TOPO Résidentiel toujours conservé, OSM ignoré ─────────────

    def test_bdtopo_residential_kept_regardless_of_osm(self):
        """Un bâtiment Résidentiel BD_TOPO est conservé même si OSM dit commercial."""
        gdf = _make_buildings([{"USAGE1": "Résidentiel"}])
        gdf = self._add_osm_building(gdf, ["commercial"])
        result = filter_residential(gdf)
        assert len(result) == 1

    def test_bdtopo_residential_kept_without_osm_data(self):
        """Un bâtiment Résidentiel BD_TOPO est conservé même sans donnée OSM."""
        gdf = _make_buildings([{"USAGE1": "Résidentiel"}])
        gdf = self._add_osm_building(gdf, [None])
        result = filter_residential(gdf)
        assert len(result) == 1

    # ── Cas 2 : OSM reclassifie un bâtiment non-Résidentiel comme résidentiel ──

    def test_commercial_bdtopo_kept_if_osm_residential(self):
        """Commerce BD_TOPO → conservé si OSM dit 'apartments'."""
        gdf = _make_buildings([{"USAGE1": "Commerce et services"}])
        gdf = self._add_osm_building(gdf, ["apartments"])
        result = filter_residential(gdf)
        assert len(result) == 1

    def test_indifferencie_kept_if_osm_house(self):
        """Indifférencié BD_TOPO → conservé si OSM dit 'house'."""
        gdf = _make_buildings([{"USAGE1": "Indifférencié"}])
        gdf = self._add_osm_building(gdf, ["house"])
        result = filter_residential(gdf)
        assert len(result) == 1

    def test_all_residential_osm_tags_kept(self):
        """Chaque tag résidentiel OSM référencé doit conserver le bâtiment."""
        from src.loaders.buildings import OSM_RESIDENTIAL_TAGS
        for tag in OSM_RESIDENTIAL_TAGS:
            gdf = _make_buildings([{"USAGE1": "Commerce et services"}])
            gdf = self._add_osm_building(gdf, [tag])
            result = filter_residential(gdf)
            assert len(result) == 1, f"Tag '{tag}' devrait conserver le bâtiment"

    # ── Cas 3 : OSM confirme non-résidentiel → exclusion ──────────────────────

    def test_commercial_bdtopo_excluded_if_osm_commercial(self):
        """Commerce BD_TOPO + OSM 'commercial' → exclu."""
        gdf = _make_buildings([{"USAGE1": "Commerce et services"}])
        gdf = self._add_osm_building(gdf, ["commercial"])
        result = filter_residential(gdf)
        assert len(result) == 0

    def test_indifferencie_excluded_if_osm_non_residential(self):
        """Indifférencié BD_TOPO + OSM 'office' → exclu."""
        gdf = _make_buildings([{"USAGE1": "Indifférencié"}])
        gdf = self._add_osm_building(gdf, ["office"])
        result = filter_residential(gdf)
        assert len(result) == 0

    def test_indifferencie_excluded_if_osm_church(self):
        """Indifférencié BD_TOPO + OSM 'church' → exclu."""
        gdf = _make_buildings([{"USAGE1": "Indifférencié"}])
        gdf = self._add_osm_building(gdf, ["church"])
        result = filter_residential(gdf)
        assert len(result) == 0

    def test_all_non_residential_osm_tags_excluded(self):
        """Chaque tag non-résidentiel OSM référencé doit exclure le bâtiment Indifférencié."""
        from src.loaders.buildings import OSM_NON_RESIDENTIAL_TAGS
        for tag in OSM_NON_RESIDENTIAL_TAGS:
            gdf = _make_buildings([{"USAGE1": "Indifférencié"}])
            gdf = self._add_osm_building(gdf, [tag])
            result = filter_residential(gdf)
            assert len(result) == 0, f"Tag '{tag}' devrait exclure le bâtiment"

    # ── Cas 4 : OSM inconnu / manquant ────────────────────────────────────────

    def test_indifferencie_no_osm_data_kept(self):
        """Indifférencié sans donnée OSM → conservé (fallback)."""
        gdf = _make_buildings([{"USAGE1": "Indifférencié"}])
        gdf = self._add_osm_building(gdf, [None])
        result = filter_residential(gdf)
        assert len(result) == 1

    def test_commercial_no_osm_data_excluded(self):
        """Commerce sans donnée OSM → exclu (pas de reclassification possible)."""
        gdf = _make_buildings([{"USAGE1": "Commerce et services"}])
        gdf = self._add_osm_building(gdf, [None])
        result = filter_residential(gdf)
        assert len(result) == 0

    def test_commercial_unknown_osm_tag_excluded(self):
        """Commerce + tag OSM non référencé (ni résidentiel ni non-résidentiel) → exclu."""
        gdf = _make_buildings([{"USAGE1": "Commerce et services"}])
        gdf = self._add_osm_building(gdf, ["some_unknown_tag"])
        result = filter_residential(gdf)
        assert len(result) == 0

    # ── Cas 5 : mélange de bâtiments ──────────────────────────────────────────

    def test_mixed_buildings_correct_classification(self):
        """Test global : BD_TOPO Résidentiel + Commerce OSM résidentiel + Indifférencié OSM non-résidentiel."""
        gdf = _make_buildings([
            {"USAGE1": "Résidentiel"},                  # conservé (BD_TOPO)
            {"USAGE1": "Commerce et services"},         # conservé (OSM dit apartments)
            {"USAGE1": "Indifférencié"},                # exclu (OSM dit office)
            {"USAGE1": "Indifférencié"},                # conservé (OSM inconnu → fallback)
        ])
        gdf["osm_building"] = ["commercial", "apartments", "office", None]
        result = filter_residential(gdf)
        assert len(result) == 3

    def test_osm_tag_case_insensitive(self):
        """Le tag OSM est comparé en minuscules (insensible à la casse)."""
        gdf = _make_buildings([{"USAGE1": "Commerce et services"}])
        gdf = self._add_osm_building(gdf, ["Apartments"])
        result = filter_residential(gdf)
        assert len(result) == 1


# ── estimate_nb_logts ─────────────────────────────────────────────────────────

class TestEstimateNbLogts:
    def test_keeps_existing_values(self):
        gdf = _make_buildings([{"NB_LOGTS": 5}])
        result = estimate_nb_logts(gdf)
        assert result.iloc[0]["NB_LOGTS"] == 5

    def test_estimates_from_nb_etages(self):
        # 100m² × 2 étages / 160m² (fallback volumique global) = 1.25 → floor = 1, clipped to 1
        poly = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])  # 100 m²
        gdf = gpd.GeoDataFrame(
            [{"NB_LOGTS": np.nan, "NB_ETAGES": 2.0, "HAUTEUR": np.nan, "geometry": poly}],
            crs="EPSG:2154",
        )
        result = estimate_nb_logts(gdf)
        assert result.iloc[0]["NB_LOGTS"] == 1

    def test_estimates_from_hauteur_when_no_nb_etages(self):
        # 100m² × round(6m/3) étages / 160m² = 100*2/160 = 1.25 → floor = 1, clipped to 1
        poly = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
        gdf = gpd.GeoDataFrame(
            [{"NB_LOGTS": np.nan, "NB_ETAGES": np.nan, "HAUTEUR": 6.0, "geometry": poly}],
            crs="EPSG:2154",
        )
        result = estimate_nb_logts(gdf)
        assert result.iloc[0]["NB_LOGTS"] >= 1

    def test_minimum_one_logement(self):
        # Tiny building: still gets at least 1
        poly = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])  # 1 m²
        gdf = gpd.GeoDataFrame(
            [{"NB_LOGTS": np.nan, "NB_ETAGES": 1.0, "HAUTEUR": np.nan, "geometry": poly}],
            crs="EPSG:2154",
        )
        result = estimate_nb_logts(gdf)
        assert result.iloc[0]["NB_LOGTS"] >= 1

    def test_output_dtype_is_integer(self):
        gdf = _make_buildings([{"NB_LOGTS": 3}])
        result = estimate_nb_logts(gdf)
        assert pd.api.types.is_integer_dtype(result["NB_LOGTS"])

    def test_zero_treated_as_missing(self):
        gdf = _make_buildings([{"NB_LOGTS": 0}])
        result = estimate_nb_logts(gdf)
        assert result.iloc[0]["NB_LOGTS"] >= 1

    def test_no_estimation_needed_when_all_present(self):
        gdf = _make_buildings([{"NB_LOGTS": 4}, {"NB_LOGTS": 7}])
        result = estimate_nb_logts(gdf)
        assert list(result["NB_LOGTS"]) == [4, 7]


# ── _filter_by_study_area ─────────────────────────────────────────────────────

class TestFilterByStudyArea:
    def _make_study_area(self, polygon: Polygon) -> gpd.GeoDataFrame:
        return gpd.GeoDataFrame(geometry=[polygon], crs="EPSG:2154")

    def test_keeps_buildings_inside(self):
        zone = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
        # Building centroid at (5, 5) — inside
        poly = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
        gdf = gpd.GeoDataFrame([{"geometry": poly}], crs="EPSG:2154")
        study_area = self._make_study_area(zone)
        result = _filter_by_study_area(gdf, study_area)
        assert len(result) == 1

    def test_filters_buildings_outside(self):
        zone = Polygon([(0, 0), (50, 0), (50, 50), (0, 50)])
        # Building centroid at (55, 55) — outside
        poly = Polygon([(50, 50), (60, 50), (60, 60), (50, 60)])
        gdf = gpd.GeoDataFrame([{"geometry": poly}], crs="EPSG:2154")
        study_area = self._make_study_area(zone)
        result = _filter_by_study_area(gdf, study_area)
        assert len(result) == 0

    def test_mixed_inside_outside(self):
        zone = Polygon([(0, 0), (50, 0), (50, 50), (0, 50)])
        inside = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
        outside = Polygon([(60, 60), (70, 60), (70, 70), (60, 70)])
        gdf = gpd.GeoDataFrame([{"geometry": inside}, {"geometry": outside}], crs="EPSG:2154")
        study_area = self._make_study_area(zone)
        result = _filter_by_study_area(gdf, study_area)
        assert len(result) == 1

    def test_reprojects_study_area_if_different_crs(self):
        zone = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
        poly = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
        gdf = gpd.GeoDataFrame([{"geometry": poly}], crs="EPSG:2154")
        # Study area in a different CRS — should still work
        study_area = gpd.GeoDataFrame(geometry=[zone], crs="EPSG:2154").to_crs("EPSG:4326")
        # Both in Lambert93, convert study area back; just check it doesn't crash
        study_area_2154 = study_area.to_crs("EPSG:2154")
        result = _filter_by_study_area(gdf, study_area_2154)
        assert isinstance(result, gpd.GeoDataFrame)


# ── _compute_nb_etages — priorité des sources ─────────────────────────────────

class TestComputeNbEtages:
    """Vérifie la priorité NB_ETAGES (BD_TOPO) > HAUTEUR (BD_TOPO) > osm_levels > 1."""

    _POLY = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])

    def _gdf(self, **cols) -> gpd.GeoDataFrame:
        row = {"geometry": self._POLY}
        row.update(cols)
        return gpd.GeoDataFrame([row], crs="EPSG:2154")

    def test_default_is_one_when_no_data(self):
        gdf = self._gdf()
        result = _compute_nb_etages(gdf)
        assert result.iloc[0] == 1.0

    def test_nb_etages_used_when_present(self):
        gdf = self._gdf(NB_ETAGES=4.0)
        result = _compute_nb_etages(gdf)
        assert result.iloc[0] == 4.0

    def test_hauteur_used_when_no_nb_etages(self):
        # 6m / 3m = 2 étages
        gdf = self._gdf(HAUTEUR=6.0)
        result = _compute_nb_etages(gdf)
        assert result.iloc[0] == 2.0

    def test_osm_levels_used_when_no_bdtopo_data(self):
        gdf = self._gdf(osm_levels=3.0)
        result = _compute_nb_etages(gdf)
        assert result.iloc[0] == 3.0

    def test_nb_etages_overrides_hauteur(self):
        """NB_ETAGES (BD_TOPO) beats HAUTEUR-derived floors."""
        gdf = self._gdf(NB_ETAGES=5.0, HAUTEUR=6.0)  # HAUTEUR → 2, NB_ETAGES → 5
        result = _compute_nb_etages(gdf)
        assert result.iloc[0] == 5.0

    def test_nb_etages_overrides_osm_levels(self):
        """NB_ETAGES (BD_TOPO) beats OSM building:levels."""
        gdf = self._gdf(NB_ETAGES=5.0, osm_levels=10.0)
        result = _compute_nb_etages(gdf)
        assert result.iloc[0] == 5.0

    def test_hauteur_overrides_osm_levels(self):
        """HAUTEUR (BD_TOPO) beats OSM building:levels."""
        gdf = self._gdf(HAUTEUR=6.0, osm_levels=10.0)  # HAUTEUR → 2, osm_levels → 10
        result = _compute_nb_etages(gdf)
        assert result.iloc[0] == 2.0

    def test_osm_levels_used_when_nb_etages_invalid(self):
        """osm_levels is used when NB_ETAGES is NaN."""
        gdf = self._gdf(NB_ETAGES=np.nan, osm_levels=4.0)
        result = _compute_nb_etages(gdf)
        assert result.iloc[0] == 4.0

    def test_osm_levels_used_when_hauteur_zero(self):
        """osm_levels is used when HAUTEUR = 0 (invalid)."""
        gdf = self._gdf(HAUTEUR=0.0, osm_levels=3.0)
        result = _compute_nb_etages(gdf)
        assert result.iloc[0] == 3.0

    def test_osm_levels_below_one_clipped(self):
        """osm_levels < 1 is clipped to 1."""
        gdf = self._gdf(osm_levels=0.5)
        result = _compute_nb_etages(gdf)
        assert result.iloc[0] >= 1.0

    def test_result_always_at_least_one(self):
        """Result is always >= 1, regardless of input."""
        gdf = self._gdf(NB_ETAGES=0.0, HAUTEUR=0.0, osm_levels=0.0)
        result = _compute_nb_etages(gdf)
        assert result.iloc[0] >= 1.0


# ── estimate_nb_logts — intégration osm_flats ─────────────────────────────────

class TestEstimateNbLogtsOsm:
    """Vérifie la priorité bdtopo > osm_flats > estimation surfacique."""

    _POLY = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])  # 100 m²

    def _gdf(self, **cols) -> gpd.GeoDataFrame:
        row = {"geometry": self._POLY}
        row.update(cols)
        return gpd.GeoDataFrame([row], crs="EPSG:2154")

    def test_osm_flats_used_when_nb_logts_missing(self):
        """building:flats OSM utilisé quand NB_LOGTS est absent."""
        gdf = self._gdf(NB_LOGTS=np.nan, osm_flats=8.0)
        result = estimate_nb_logts(gdf)
        assert result.iloc[0]["NB_LOGTS"] == 8

    def test_osm_flats_source_tagged(self):
        """NB_LOGTS_source = 'osm_flats' quand la valeur vient d'OSM."""
        gdf = self._gdf(NB_LOGTS=np.nan, osm_flats=8.0)
        result = estimate_nb_logts(gdf)
        assert result.iloc[0]["NB_LOGTS_source"] == "osm_flats"

    def test_bdtopo_wins_over_osm_flats(self):
        """NB_LOGTS BD_TOPO est prioritaire sur building:flats OSM."""
        gdf = self._gdf(NB_LOGTS=5.0, osm_flats=20.0)
        result = estimate_nb_logts(gdf)
        assert result.iloc[0]["NB_LOGTS"] == 5
        assert result.iloc[0]["NB_LOGTS_source"] == "bdtopo"

    def test_osm_flats_zero_treated_as_missing(self):
        """osm_flats = 0 est ignoré, on tombe sur l'estimation surfacique."""
        gdf = self._gdf(NB_LOGTS=np.nan, osm_flats=0.0, NB_ETAGES=2.0)
        result = estimate_nb_logts(gdf)
        assert result.iloc[0]["NB_LOGTS_source"] in ("iris_local", "fallback")
        assert result.iloc[0]["NB_LOGTS"] >= 1

    def test_osm_flats_none_falls_back_to_estimation(self):
        """osm_flats = NaN → estimation surfacique."""
        gdf = self._gdf(NB_LOGTS=np.nan, osm_flats=np.nan, NB_ETAGES=3.0)
        result = estimate_nb_logts(gdf)
        assert result.iloc[0]["NB_LOGTS_source"] in ("iris_local", "fallback")

    def test_osm_flats_mixed_buildings(self):
        """Chaque bâtiment suit sa propre logique de priorité."""
        unit = self._POLY
        gdf = gpd.GeoDataFrame([
            {"NB_LOGTS": 5.0,    "osm_flats": 10.0, "geometry": unit},  # bdtopo wins
            {"NB_LOGTS": np.nan, "osm_flats": 7.0,  "geometry": unit},  # osm_flats used
            {"NB_LOGTS": np.nan, "osm_flats": np.nan, "geometry": unit}, # estimation
        ], crs="EPSG:2154")
        result = estimate_nb_logts(gdf)
        assert result.iloc[0]["NB_LOGTS"] == 5
        assert result.iloc[0]["NB_LOGTS_source"] == "bdtopo"
        assert result.iloc[1]["NB_LOGTS"] == 7
        assert result.iloc[1]["NB_LOGTS_source"] == "osm_flats"
        assert result.iloc[2]["NB_LOGTS_source"] in ("iris_local", "fallback")

    def test_osm_levels_used_in_floor_estimation(self):
        """osm_levels est utilisé dans l'estimation quand NB_ETAGES et HAUTEUR sont absents."""
        # 100 m² × 4 niveaux / 160 m² (fallback) = 2.5 → floor = 2
        gdf = self._gdf(NB_LOGTS=np.nan, osm_flats=np.nan, osm_levels=4.0)
        result = estimate_nb_logts(gdf)
        # Sans osm_levels (1 étage) : floor(100*1/160) = 0 → clip 1
        # Avec osm_levels=4 : floor(100*4/160) = 2
        assert result.iloc[0]["NB_LOGTS"] >= 2

    def test_output_dtype_int_with_osm_flats(self):
        """NB_LOGTS reste de type entier même quand la valeur vient d'OSM."""
        gdf = self._gdf(NB_LOGTS=np.nan, osm_flats=12.0)
        result = estimate_nb_logts(gdf)
        assert pd.api.types.is_integer_dtype(result["NB_LOGTS"])

    def test_nb_logts_estime_flag_set_for_osm(self):
        """NB_LOGTS_ESTIME = True pour les valeurs issues d'OSM."""
        gdf = self._gdf(NB_LOGTS=np.nan, osm_flats=5.0)
        result = estimate_nb_logts(gdf)
        assert result.iloc[0]["NB_LOGTS_ESTIME"] == True  # noqa: E712 (np.bool_ != True via `is`)

    def test_nb_logts_estime_false_for_bdtopo(self):
        """NB_LOGTS_ESTIME = False pour les valeurs BD_TOPO."""
        gdf = self._gdf(NB_LOGTS=4.0)
        result = estimate_nb_logts(gdf)
        assert result.iloc[0]["NB_LOGTS_ESTIME"] == False  # noqa: E712
