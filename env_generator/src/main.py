"""Pipeline CLI — distribue la population INSEE aux bâtiments résidentiels.

Usage:
    python -m src.main --step all                                   # Filosofi
    python -m src.main --step all --source iris --communes 38185,38151,...
    python -m src.main --step all --source iris --communes-file data/communes.txt
    python -m src.main --step load --source iris --communes 38185
    python -m src.main --step match
    python -m src.main --step export
    python -m src.main --step visualize
    python -m src.main --step compare
    python -m src.main --step all --verbose
"""

import argparse
import sys
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PROCESSED_DIR = DATA_DIR / "processed"

BUILDINGS_SHP = DATA_DIR / "batim_grenoble.shp"
INSEE_SHP = DATA_DIR / "insee_metro_grenoble.shp"

# Fichiers intermédiaires — communs aux deux sources
BUILDINGS_GPKG = PROCESSED_DIR / "buildings.gpkg"
BUILDINGS_ALL_GPKG = PROCESSED_DIR / "buildings_all.gpkg"
STUDY_AREA_GPKG = PROCESSED_DIR / "study_area.gpkg"

# Fichiers intermédiaires — spécifiques à chaque source
INSEE_GPKG = PROCESSED_DIR / "insee.gpkg"
IRIS_GPKG = PROCESSED_DIR / "iris.gpkg"

# Résultats finaux
RESULT_FILOSOFI_GPKG = PROCESSED_DIR / "result_filosofi.gpkg"
RESULT_IRIS_GPKG = PROCESSED_DIR / "result_iris.gpkg"


def _setup_logging(verbose: bool) -> None:
    from src.utils.logging_config import setup_logging
    setup_logging(verbose)


def _source_paths(source: str) -> tuple[Path, Path]:
    """Return (grid_gpkg, result_gpkg) for the given source."""
    if source == "iris":
        return IRIS_GPKG, RESULT_IRIS_GPKG
    return INSEE_GPKG, RESULT_FILOSOFI_GPKG


# ── Steps ─────────────────────────────────────────────────────────────────────

def step_load(
    verbose: bool = False,
    source: str = "filosofi",
    iris_codes: list[str] | None = None,
    args_iris_shp: "str | None" = None,
) -> None:
    """Load + filter buildings and population grid, save intermediates.

    Avec --source iris, la zone d'étude est définie par les IRIS fournis via
    --iris-shp (shapefile) ou --iris / --iris-file (codes texte).
    """
    import logging
    _setup_logging(verbose)
    log = logging.getLogger(__name__)

    import geopandas as gpd
    from src.loaders.buildings import load_buildings
    log.info("=== STEP load (source=%s) ===", source)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    # ── Grille de population (chargée en premier pour définir la zone d'étude)
    grid_gpkg, _ = _source_paths(source)
    study_area = None

    if source == "iris":
        from src.loaders.iris import load_iris
        if args_iris_shp:
            log.info("Chargement des IRIS depuis shapefile : %s", args_iris_shp)
            grid = load_iris(shp_path=args_iris_shp)
        else:
            log.info("Chargement des IRIS INSEE 2022 (téléchargement auto si nécessaire)...")
            if iris_codes:
                log.info("%d codes IRIS fournis", len(iris_codes))
            grid = load_iris(iris_codes=iris_codes)
        grid.to_file(grid_gpkg, driver="GPKG")
        log.info("%d IRIS sauvegardés -> %s", len(grid), grid_gpkg)

        # Zone d'étude = union des IRIS sélectionnés
        study_area = gpd.GeoDataFrame(geometry=[grid.union_all()], crs=grid.crs)
        study_area.to_file(STUDY_AREA_GPKG, driver="GPKG")
        log.info("Zone d'étude sauvegardée -> %s", STUDY_AREA_GPKG)

    else:
        from src.loaders.insee import load_insee
        log.info("Chargement des carreaux INSEE Filosofi : %s", INSEE_SHP)
        grid = load_insee(INSEE_SHP)
        grid.to_file(grid_gpkg, driver="GPKG")
        log.info("%d carreaux sauvegardés -> %s", len(grid), grid_gpkg)

    # ── Bâtiments : enrichissement OSM puis chargement ────────────────────────
    from src.loaders.osm import fetch_osm_buildings
    from src.loaders.buildings import load_all_buildings

    # Zone de référence pour la bbox OSM : study_area si disponible, sinon grille entière
    osm_bbox_area = study_area if study_area is not None else gpd.GeoDataFrame(
        geometry=[grid.union_all()], crs=grid.crs
    )
    osm_gdf = fetch_osm_buildings(osm_bbox_area, cache_dir=PROCESSED_DIR)

    log.info("Chargement des bâtiments : %s", BUILDINGS_SHP)
    buildings = load_buildings(BUILDINGS_SHP, study_area=study_area, osm_gdf=osm_gdf)
    buildings.to_file(BUILDINGS_GPKG, driver="GPKG")
    log.info("%d bâtiments résidentiels sauvegardés -> %s", len(buildings), BUILDINGS_GPKG)

    # Tous les bâtiments (résidentiels + non-résidentiels) pour les lieux de travail
    buildings_all = load_all_buildings(BUILDINGS_SHP, study_area=study_area)
    buildings_all.to_file(BUILDINGS_ALL_GPKG, driver="GPKG")
    log.info("%d bâtiments (tous usages) sauvegardés -> %s", len(buildings_all), BUILDINGS_ALL_GPKG)


def step_match(verbose: bool = False, source: str = "filosofi") -> None:
    """Spatial join + population allocation, save result."""
    import logging
    _setup_logging(verbose)
    log = logging.getLogger(__name__)

    import geopandas as gpd
    from src.matching.spatial_join import join_buildings_to_insee
    from src.matching.allocator import allocate_population

    log.info("=== STEP match (source=%s) ===", source)
    grid_gpkg, result_gpkg = _source_paths(source)

    _require(BUILDINGS_GPKG, "load")
    _require(grid_gpkg, f"load --source {source}")

    buildings = gpd.read_file(BUILDINGS_GPKG)
    grid = gpd.read_file(grid_gpkg)

    joined = join_buildings_to_insee(buildings, grid)
    result = allocate_population(joined)

    result.to_file(result_gpkg, driver="GPKG")
    log.info("Résultat sauvegardé -> %s", result_gpkg)


def step_export(verbose: bool = False, source: str = "filosofi") -> None:
    """Export GeoJSON and CSV files."""
    import logging
    _setup_logging(verbose)
    log = logging.getLogger(__name__)

    import geopandas as gpd
    from src.output.export import export_results, export_all_buildings

    log.info("=== STEP export (source=%s) ===", source)
    _, result_gpkg = _source_paths(source)
    _require(result_gpkg, f"match --source {source}")

    result = gpd.read_file(result_gpkg)
    out_dir = PROCESSED_DIR / source
    export_results(result, out_dir)

    if BUILDINGS_ALL_GPKG.exists():
        buildings_all = gpd.read_file(BUILDINGS_ALL_GPKG)
        export_all_buildings(buildings_all, out_dir)
    else:
        log.warning("buildings_all.gpkg absent — relancer --step load pour l'obtenir")


def step_visualize(verbose: bool = False, source: str = "filosofi") -> None:
    """Generate static PNG map."""
    import logging
    _setup_logging(verbose)
    log = logging.getLogger(__name__)

    import geopandas as gpd
    from src.output.visualize import make_map

    log.info("=== STEP visualize (source=%s) ===", source)
    _, result_gpkg = _source_paths(source)
    _require(result_gpkg, f"match --source {source}")

    result = gpd.read_file(result_gpkg)
    out = make_map(result, PROCESSED_DIR / source)
    log.info("Carte disponible : %s", out)


def step_compare(verbose: bool = False, source: str = "filosofi") -> None:
    """Validate allocation against IRIS 2022 census data."""
    import logging
    _setup_logging(verbose)
    log = logging.getLogger(__name__)

    import geopandas as gpd

    log.info("=== STEP compare (source=%s) ===", source)
    _require(IRIS_GPKG, "load --source iris")
    iris = gpd.read_file(IRIS_GPKG)

    if source == "iris":
        from src.output.compare import compare_iris_results
        _require(RESULT_IRIS_GPKG, "match --source iris")
        result = gpd.read_file(RESULT_IRIS_GPKG)
        out = compare_iris_results(result, iris, PROCESSED_DIR / "compare_iris")
    else:
        from src.output.compare import compare_results
        _require(RESULT_FILOSOFI_GPKG, "match --source filosofi")
        result = gpd.read_file(RESULT_FILOSOFI_GPKG)
        out = compare_results(result, iris, PROCESSED_DIR / "compare")

    log.info("Validation terminée : %s", out)


def step_casualties(
    verbose: bool = False,
    source: str = "iris",
    damage_csv: "str | None" = None,
) -> None:
    """Calcule les victimes et sans-abris à partir d'un CSV de dommages bâtiment."""
    import logging
    _setup_logging(verbose)
    log = logging.getLogger(__name__)

    from src.output.casualties import compute_casualties

    log.info("=== STEP casualties ===")
    _require(RESULT_IRIS_GPKG, "match --source iris")

    if damage_csv is None:
        print(
            "[ERREUR] --damage-csv est obligatoire pour l'étape casualties.",
            file=sys.stderr,
        )
        sys.exit(1)

    out_dir = PROCESSED_DIR / "casualties"
    iris_csv = compute_casualties(damage_csv, RESULT_IRIS_GPKG, out_dir)
    log.info("Résultats disponibles dans : %s", out_dir)
    log.info("Synthèse IRIS : %s", iris_csv)


def step_all(
    verbose: bool = False,
    source: str = "filosofi",
    iris_codes: list[str] | None = None,
    args_iris_shp: "str | None" = None,
) -> None:
    """Run the full pipeline end-to-end."""
    import logging
    _setup_logging(verbose)
    logging.getLogger(__name__).info("=== PIPELINE COMPLET (source=%s) ===", source)

    step_load(verbose, source, iris_codes, args_iris_shp)
    step_match(verbose, source)
    step_export(verbose, source)
    step_visualize(verbose, source)


def _confirm_iris_download(missing: list, assume_yes: bool) -> bool:
    """Demande confirmation avant de basculer sur le téléchargement IGN.

    --yes (assume_yes) → True sans prompt. Non interactif sans --yes → False
    (jamais de download silencieux). Sinon, prompt y/N.
    """
    head = sorted(missing)[:10]
    suffix = " ..." if len(missing) > 10 else ""
    print(f"[IRIS] {len(missing)} code(s) absent(s) du shapefile local : {head}{suffix}",
          file=sys.stderr)
    if assume_yes:
        print("[IRIS] --yes : téléchargement IGN (source France) autorisé.", file=sys.stderr)
        return True
    if not sys.stdin.isatty():
        print("[IRIS] Mode non interactif. Relancez avec --yes pour autoriser le "
              "téléchargement, ou corrigez les codes.", file=sys.stderr)
        return False
    resp = input("[IRIS] Télécharger ces IRIS depuis l'IGN (France entière) ? [y/N] ")
    return resp.strip().lower() in ("y", "yes", "o", "oui")


def step_env(verbose: bool = False, config_path: str = "config.yaml", assume_yes: bool = False) -> None:
    """Pipeline complet piloté par config.yaml.

    Enchaîne : zone population (IRIS + INSEE) → emprise région (évacuation) →
    réseau routier (walk/drive) → bâtiments (BD TOPO + OSM) → allocation de
    population → export, le tout dans output.dir pour consommation par GAMA.
    """
    import logging
    _setup_logging(verbose)
    log = logging.getLogger(__name__)

    import geopandas as gpd
    from shapely.ops import unary_union

    from src.config import load_config
    from src.loaders.iris import load_iris, resolve_zone, validate_subset, MissingIrisError
    from src.loaders.roads import fetch_road_network
    from src.loaders.osm import fetch_osm_buildings
    from src.loaders.bpe import load_bpe_education
    from src.loaders.buildings import load_buildings, load_all_buildings
    from src.matching.spatial_join import join_buildings_to_insee
    from src.matching.allocator import allocate_population
    from src.matching.agents import generate_agents
    from src.output.export import export_results, export_all_buildings, export_agents

    cfg = load_config(config_path)
    out_dir = cfg.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    log.info("=== STEP env -> %s ===", out_dir)

    # Source des contours : shapefile local si présent (évite le download France),
    # sinon téléchargement IGN. URLs INSEE surchargées par la config si fournies.
    contours_local = cfg.sources.get("contours_iris")
    shp = str(contours_local) if contours_local and contours_local.exists() else None
    iris_urls = {}
    if cfg.contours_url:
        iris_urls["contours_url"] = cfg.contours_url
    if cfg.insee_pop_url:
        iris_urls["pop_url"] = cfg.insee_pop_url
    if cfg.insee_logement_url:
        iris_urls["log_url"] = cfg.insee_logement_url

    # 1. Zone population (IRIS + INSEE)
    log.info("[1/6] Zone population (IRIS + INSEE)")
    try:
        grid = load_iris(selector=cfg.population.selector, shp_path=shp,
                         on_missing="error", **iris_urls)
    except MissingIrisError as e:
        if not _confirm_iris_download(e.missing, assume_yes):
            log.error("Abandon : codes IRIS population absents du shapefile local.")
            return
        grid = load_iris(selector=cfg.population.selector, shp_path=shp,
                         on_missing="download", **iris_urls)
    grid.to_file(out_dir / "population_iris.gpkg", driver="GPKG")

    # 2. Emprise région (évacuation, plus large que la population)
    log.info("[2/6] Emprise région")
    if cfg.region.same_as == "population":
        region_fp = unary_union(grid.geometry.values)
        if cfg.region.buffer_m > 0:
            region_fp = region_fp.buffer(cfg.region.buffer_m)
    else:
        zone_urls = {"contours_url": cfg.contours_url} if cfg.contours_url else {}
        try:
            region_fp, _ = resolve_zone(
                selector=cfg.region.selector, shp_path=shp,
                buffer_m=cfg.region.buffer_m, on_missing="error", **zone_urls,
            )
        except MissingIrisError as e:
            if not _confirm_iris_download(e.missing, assume_yes):
                log.error("Abandon : codes IRIS région absents du shapefile local.")
                return
            region_fp, _ = resolve_zone(
                selector=cfg.region.selector, shp_path=shp,
                buffer_m=cfg.region.buffer_m, on_missing="download", **zone_urls,
            )
    region_gdf = gpd.GeoDataFrame(geometry=[region_fp], crs="EPSG:2154")
    region_gdf.to_file(out_dir / "region.gpkg", driver="GPKG")
    validate_subset(grid, region_fp)  # garde-fou population ⊆ région

    # 3. Réseau routier (walk + drive)
    log.info("[3/6] Réseau routier %s", cfg.network_types)
    roads = fetch_road_network(
        region_fp, network_types=cfg.network_types,
        cache_dir=out_dir, simplify=cfg.network_simplify,
    )
    for nt, edges in roads.items():
        edges.to_file(out_dir / f"roads_{nt}.gpkg", driver="GPKG")
        log.info("  réseau '%s' : %d arêtes", nt, len(edges))

    # 4. Bâtiments dans l'emprise région (BD TOPO + enrichissement OSM)
    log.info("[4/6] Bâtiments (région)")
    buildings_shp = cfg.sources.get("buildings", DATA_DIR / "batim_grenoble.shp")
    osm_gdf = fetch_osm_buildings(region_gdf, cache_dir=out_dir)
    buildings = load_buildings(buildings_shp, study_area=region_gdf, osm_gdf=osm_gdf)
    buildings_all = load_all_buildings(buildings_shp, study_area=region_gdf)

    # 5. Allocation de la population aux bâtiments (sur la zone population)
    log.info("[5/7] Allocation population")
    joined = join_buildings_to_insee(buildings, grid)
    result = allocate_population(joined)

    # 6. Génération des agents (âge + CSP + domicile + destination travail/école/crèche)
    log.info("[6/7] Génération des agents")
    departement = grid["CODE_IRIS"].iloc[0][:2] if not grid.empty else "38"
    education = load_bpe_education(departement=departement)
    agents = generate_agents(
        result, buildings_all, education=education,
        usages=cfg.workplace_usages,
        decay_m=cfg.workplace_decay_m,
        education_decay_m=cfg.education_decay_m,
        seed=cfg.workplace_seed,
    )
    if not agents.empty:
        agents.to_file(out_dir / "agents.gpkg", driver="GPKG")

    # 7. Export pour GAMA
    log.info("[7/7] Export")
    export_results(result, out_dir)
    export_all_buildings(buildings_all, out_dir)
    export_agents(agents, out_dir)
    log.info("=== Environnement généré dans %s ===", out_dir)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _require(path: Path, prerequisite_step: str) -> None:
    if not path.exists():
        print(
            f"[ERREUR] Fichier intermédiaire manquant : {path}\n"
            f"         Lancez d'abord : python -m src.main --step {prerequisite_step}",
            file=sys.stderr,
        )
        sys.exit(1)


def _parse_iris(args: argparse.Namespace) -> list[str] | None:
    """Resolve --iris / --iris-file into a list of IRIS codes."""
    if args.iris_file:
        path = Path(args.iris_file)
        return [l.strip() for l in path.read_text().splitlines() if l.strip()]
    if args.iris:
        return [c.strip() for c in args.iris.split(",") if c.strip()]
    return None


# ── CLI ───────────────────────────────────────────────────────────────────────

_STEPS = {
    "load": step_load,
    "match": step_match,
    "export": step_export,
    "visualize": step_visualize,
    "compare": step_compare,
    "casualties": step_casualties,
    "all": step_all,
    "env": step_env,
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Distribue la population INSEE aux bâtiments résidentiels."
    )
    parser.add_argument(
        "--step",
        choices=list(_STEPS),
        required=True,
        help="Étape du pipeline à exécuter.",
    )
    parser.add_argument(
        "--source",
        choices=["filosofi", "iris"],
        default="filosofi",
        help="Source de données de population (default: filosofi).",
    )
    parser.add_argument(
        "--iris",
        default=None,
        help="Codes IRIS à 9 chiffres séparés par virgule (ex: 381850101,381850102). "
             "Utilisé avec --source iris pour définir la zone d'étude.",
    )
    parser.add_argument(
        "--iris-file",
        default=None,
        metavar="FILE",
        help="Fichier texte avec un code IRIS par ligne.",
    )
    parser.add_argument(
        "--iris-shp",
        default=None,
        metavar="FILE",
        help="Shapefile contenant les IRIS de la zone d'étude (colonne code_iris)."
             " Utilisé avec --source iris. Évite le téléchargement IGN.",
    )
    parser.add_argument(
        "--damage-csv",
        default=None,
        metavar="FILE",
        help="CSV contenant ID et damage_level (D1..D5) des bâtiments endommagés. "
             "Requis pour --step casualties.",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        metavar="FILE",
        help="Fichier de configuration YAML pour --step env (default: config.yaml).",
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Autorise sans prompt le téléchargement IGN si des IRIS manquent du "
             "shapefile local (--step env).",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Active les logs DEBUG.",
    )
    args = parser.parse_args()
    iris_codes = _parse_iris(args)

    step_fn = _STEPS[args.step]
    if args.step in ("load", "all"):
        step_fn(
            verbose=args.verbose,
            source=args.source,
            iris_codes=iris_codes,
            args_iris_shp=args.iris_shp,
        )
    elif args.step == "casualties":
        step_fn(verbose=args.verbose, source=args.source, damage_csv=args.damage_csv)
    elif args.step == "env":
        step_fn(verbose=args.verbose, config_path=args.config, assume_yes=args.yes)
    else:
        step_fn(verbose=args.verbose, source=args.source)


if __name__ == "__main__":
    main()
