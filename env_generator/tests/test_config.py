"""Tests for src/config.py — lecture et validation de config.yaml."""

import pytest

from src.config import load_config, Config, ZoneConfig, _parse_zone
from src.loaders.iris import Selector


_YAML = """
crs: "EPSG:2154"
sources:
  buildings: "./data/batim.shp"
zones:
  population:
    selector:
      type: iris
      codes: ["381850102", "381850103"]
  region:
    same_as: population
    buffer_m: 1500
network:
  types: [walk, drive]
  simplify: true
output:
  dir: "./out"
  format: gpkg
"""


def _write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


class TestLoadConfig:
    def test_parses_population_selector(self, tmp_path):
        cfg = load_config(_write(tmp_path, _YAML))
        assert isinstance(cfg, Config)
        assert cfg.population.selector == Selector("iris", ("381850102", "381850103"))

    def test_region_same_as_population(self, tmp_path):
        cfg = load_config(_write(tmp_path, _YAML))
        assert cfg.region.same_as == "population"
        assert cfg.region.buffer_m == 1500.0

    def test_network_types(self, tmp_path):
        cfg = load_config(_write(tmp_path, _YAML))
        assert cfg.network_types == ("walk", "drive")
        assert cfg.network_simplify is True

    def test_paths_resolved_relative_to_config(self, tmp_path):
        cfg = load_config(_write(tmp_path, _YAML))
        assert cfg.sources["buildings"] == (tmp_path / "data/batim.shp").resolve()
        assert cfg.output_dir == (tmp_path / "out").resolve()

    def test_missing_population_raises(self, tmp_path):
        bad = "crs: EPSG:2154\nzones:\n  region:\n    same_as: population\n"
        with pytest.raises(ValueError):
            load_config(_write(tmp_path, bad))

    def test_region_with_own_selector(self, tmp_path):
        text = (
            'crs: "EPSG:2154"\n'
            "zones:\n"
            "  region:\n"
            "    selector:\n"
            "      type: iris\n"
            '      codes: ["381850102", "381850103"]\n'
            "    buffer_m: 300\n"
            "  population:\n"
            "    selector:\n"
            "      type: iris\n"
            '      codes: ["381850102"]\n'
        )
        cfg = load_config(_write(tmp_path, text))
        assert cfg.region.selector.type == "iris"
        assert cfg.region.buffer_m == 300.0
        assert cfg.region.same_as is None

    def test_datasets_urls_parsed(self, tmp_path):
        text = _YAML + (
            "datasets:\n"
            '  insee_pop_url: "http://x/pop.zip"\n'
            '  insee_logement_url: "http://x/log.zip"\n'
        )
        cfg = load_config(_write(tmp_path, text))
        assert cfg.insee_pop_url == "http://x/pop.zip"
        assert cfg.insee_logement_url == "http://x/log.zip"

    def test_datasets_absent_gives_none(self, tmp_path):
        cfg = load_config(_write(tmp_path, _YAML))
        assert cfg.insee_pop_url is None
        assert cfg.contours_url is None

    def test_region_defaults_to_population_when_absent(self, tmp_path):
        text = (
            'crs: "EPSG:2154"\n'
            "zones:\n"
            "  population:\n"
            "    selector:\n"
            "      type: commune\n"
            '      codes: ["38185"]\n'
        )
        cfg = load_config(_write(tmp_path, text))
        assert cfg.region.same_as == "population"


class TestParseZone:
    def test_selector_zone(self):
        z = _parse_zone({"selector": {"type": "commune", "codes": ["38185"]}})
        assert z.selector == Selector("commune", ("38185",))
        assert z.same_as is None

    def test_same_as_zone(self):
        z = _parse_zone({"same_as": "population", "buffer_m": 500})
        assert z.same_as == "population"
        assert z.buffer_m == 500.0

    def test_empty_zone_raises(self):
        with pytest.raises(ValueError):
            _parse_zone({})
