"""Lecture de la configuration de génération d'environnement (config.yaml).

Deux zones :
  - population : où vivent les agents (IRIS + INSEE).
  - region    : emprise d'évacuation, plus large (réseau + bâtiments).
    Peut être définie soit par son propre sélecteur, soit par ``same_as:
    population`` (= même emprise que la population, élargie par ``buffer_m``).

Les chemins de ``sources`` et ``output.dir`` sont résolus en absolu, relativement
à l'emplacement du fichier config.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from src.loaders.iris import Selector
from src.matching.workplaces import DEFAULT_WORKPLACE_USAGES

logger = logging.getLogger(__name__)


@dataclass
class ZoneConfig:
    """Définition d'une zone : un sélecteur OU une référence à une autre zone."""
    selector: "Selector | None" = None
    same_as: "str | None" = None
    buffer_m: float = 0.0


@dataclass
class Config:
    crs: str
    sources: "dict[str, Path]"
    population: ZoneConfig
    region: ZoneConfig
    network_types: "tuple[str, ...]"
    network_simplify: bool
    buildings_source: str
    buildings_min_floor_area: float
    buildings_absorb_slivers: bool
    output_dir: Path
    output_format: str
    raw: dict
    # URLs des sources distantes (None = défauts codés dans les loaders).
    # NB : liées au millésime 2022 (les noms de colonnes INSEE P22_/C22_ le sont aussi).
    contours_url: "str | None" = None
    insee_pop_url: "str | None" = None
    insee_logement_url: "str | None" = None
    # Lieux de travail (modèle gravitaire). Voir matching/workplaces.py.
    workplace_usages: "tuple[str, ...]" = DEFAULT_WORKPLACE_USAGES
    workplace_decay_m: float = 3000.0
    workplace_seed: int = 42
    education_decay_m: float = 1200.0


def _parse_zone(d: dict) -> ZoneConfig:
    same_as = d.get("same_as")
    sel = None
    if d.get("selector"):
        sel = Selector.from_dict(d["selector"])
    if sel is None and same_as is None:
        raise ValueError("zone invalide : ni 'selector' ni 'same_as' fourni")
    return ZoneConfig(selector=sel, same_as=same_as, buffer_m=float(d.get("buffer_m", 0.0)))


def load_config(path: "str | Path") -> Config:
    """Charge et valide config.yaml. Résout les chemins relatifs au fichier."""
    path = Path(path).resolve()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    base = path.parent

    crs = data.get("crs", "EPSG:2154")
    if crs != "EPSG:2154":
        logger.warning("CRS %s != EPSG:2154 (Lambert-93 attendu pour le DT)", crs)

    sources = {k: (base / v).resolve() for k, v in (data.get("sources") or {}).items()}

    zones = data.get("zones") or {}
    if "population" not in zones:
        raise ValueError("config : zone 'population' manquante")
    population = _parse_zone(zones["population"])
    region = _parse_zone(zones["region"]) if "region" in zones else ZoneConfig(
        same_as="population"
    )
    if region.same_as not in (None, "population"):
        raise ValueError(f"region.same_as inconnu : {region.same_as!r} (attendu 'population')")

    network = data.get("network") or {}
    network_types = tuple(network.get("types", ("walk", "drive")))

    output = data.get("output") or {}
    output_dir = (base / output.get("dir", "./data/processed/env")).resolve()

    datasets = data.get("datasets") or {}

    workplaces = data.get("workplaces") or {}
    workplace_usages = tuple(workplaces.get("usages", DEFAULT_WORKPLACE_USAGES))
    edu = data.get("education") or {}

    cfg = Config(
        crs=crs,
        sources=sources,
        population=population,
        region=region,
        network_types=network_types,
        network_simplify=bool(network.get("simplify", True)),
        buildings_source=(data.get("buildings") or {}).get("source", "bdtopo"),
        buildings_min_floor_area=float(
            (data.get("buildings") or {}).get("min_dwelling_floor_area_m2", 25.0)
        ),
        buildings_absorb_slivers=bool(
            (data.get("buildings") or {}).get("absorb_slivers", True)
        ),
        output_dir=output_dir,
        output_format=output.get("format", "gpkg"),
        raw=data,
        contours_url=datasets.get("contours_iris_url"),
        insee_pop_url=datasets.get("insee_pop_url"),
        insee_logement_url=datasets.get("insee_logement_url"),
        workplace_usages=workplace_usages,
        workplace_decay_m=float(workplaces.get("decay_m", 3000.0)),
        workplace_seed=int(workplaces.get("seed", 42)),
        education_decay_m=float(edu.get("decay_m", 1200.0)),
    )
    logger.info(
        "Config chargée : population=%s, region=%s(buffer=%.0fm), réseaux=%s",
        population.selector.type if population.selector else "?",
        region.same_as or (region.selector.type if region.selector else "?"),
        region.buffer_m,
        network_types,
    )
    return cfg
