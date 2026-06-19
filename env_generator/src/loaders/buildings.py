import logging
from pathlib import Path

import numpy as np
import geopandas as gpd

logger = logging.getLogger(__name__)

SURFACE_MOY_DEFAULT: float = 160.0  # m² par logement volumique (surface × nb_étages / NB_LOGTS, médiane BDTopo Grenoble)
HAUTEUR_PAR_ETAGE: float = 3.0     # m par étage (fallback)
SURFACE_MIN_IRIS: int = 5          # nb min de bâtiments connus pour calibrer localement

# Tags OSM indiquant un bâtiment résidentiel
OSM_RESIDENTIAL_TAGS: frozenset[str] = frozenset({
    "residential", "apartments", "house", "detached", "semidetached_house",
    "terrace", "dormitory", "bungalow", "static_caravan", "cabin",
    "yes",  # tag générique — conservé car souvent résidentiel
})

# Tags OSM indiquant explicitement un bâtiment non-résidentiel
OSM_NON_RESIDENTIAL_TAGS: frozenset[str] = frozenset({
    "commercial", "retail", "office", "industrial", "warehouse",
    "shop", "kiosk", "supermarket", "hotel", "civic", "public",
    "church", "cathedral", "chapel", "mosque", "synagogue", "temple",
    "sports_hall", "stadium", "train_station", "transportation",
    "hospital", "school", "university", "kindergarten", "college",
    "fire_station", "police", "post_office", "government",
    "garage", "garages", "parking", "shed", "greenhouse", "barn",
    "farm_auxiliary", "manufacture", "service",
})


def _fix_encoding(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Corrige les colonnes texte doublement encodées (UTF-8 interprété en Latin-1)."""
    for col in gdf.select_dtypes(include="object").columns:
        if col == "geometry":
            continue
        try:
            gdf[col] = gdf[col].apply(
                lambda v: v.encode("latin-1").decode("utf-8") if isinstance(v, str) else v
            )
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass  # colonne déjà correctement encodée
    return gdf


def load_all_buildings(
    path: str | Path,
    study_area: "gpd.GeoDataFrame | None" = None,
) -> gpd.GeoDataFrame:
    """Charge tous les bâtiments de la zone d'étude, sans filtre résidentiel.

    Utilisé pour générer les lieux de travail ou tout autre usage nécessitant
    l'ensemble du bâti (résidentiel + non-résidentiel).

    Args:
        path:       Chemin vers le shapefile bâtiments.
        study_area: Si fourni, filtre par centroïde dans la zone d'étude.
    """
    path = Path(path)
    gdf = gpd.read_file(path)
    gdf = _fix_encoding(gdf)
    logger.info("Tous bâtiments — %d au total avant filtre zone", len(gdf))

    if study_area is not None:
        gdf = _filter_by_study_area(gdf, study_area)

    logger.info("%d bâtiments dans la zone d'étude (tous usages)", len(gdf))
    return gdf


def load_buildings(
    path: str | Path,
    study_area: "gpd.GeoDataFrame | None" = None,
    osm_gdf: "gpd.GeoDataFrame | None" = None,
    bdnb_usage: "dict[str, str] | None" = None,
) -> gpd.GeoDataFrame:
    """Charge le shapefile bâtiments, filtre les résidentiels et estime NB_LOGTS.

    Args:
        path:       Chemin vers le shapefile bâtiments.
        study_area: GeoDataFrame d'une seule géométrie (union des IRIS choisis).
                    Si fourni, seuls les bâtiments dont le centroïde se trouve
                    dans la zone d'étude sont conservés.
        osm_gdf:    GeoDataFrame des bâtiments OSM (issu de fetch_osm_buildings).
                    Si fourni, enrichit les bâtiments avec building:flats et
                    building:levels via match_osm_to_bdtopo.
        bdnb_usage: carte ID bâtiment → catégorie BDNB (`travail`/`residentiel`/
                    `annexe`, cf. loaders/bdnb.py). Affine le filtre résidentiel
                    pour les « Indifférencié ».
    """
    path = Path(path)
    logger.info("Chargement des bâtiments depuis %s", path)

    gdf = gpd.read_file(path)
    gdf = _fix_encoding(gdf)
    logger.info("CRS : %s — %d bâtiments au total", gdf.crs, len(gdf))

    if study_area is not None:
        gdf = _filter_by_study_area(gdf, study_area)

    # Appariement OSM en premier : fournit osm_building pour le filtre résidentiel
    if osm_gdf is not None:
        from src.loaders.osm import match_osm_to_bdtopo
        gdf = match_osm_to_bdtopo(gdf, osm_gdf)

    gdf = filter_residential(gdf, bdnb_usage=bdnb_usage)
    logger.info("%d bâtiments résidentiels conservés", len(gdf))

    gdf = estimate_nb_logts(gdf)
    return gdf


def _filter_by_study_area(
    gdf: gpd.GeoDataFrame,
    study_area: gpd.GeoDataFrame,
) -> gpd.GeoDataFrame:
    """Filtre les bâtiments dont le centroïde est dans la zone d'étude."""
    if gdf.crs != study_area.crs:
        study_area = study_area.to_crs(gdf.crs)

    centroids = gdf.copy()
    centroids["geometry"] = gdf.geometry.centroid

    mask = centroids.sjoin(
        study_area[["geometry"]],
        how="inner",
        predicate="within",
    ).index

    filtered = gdf.loc[gdf.index.isin(mask)].copy()
    logger.info(
        "Filtre zone d'etude : %d -> %d batiments", len(gdf), len(filtered)
    )
    return filtered


def filter_residential(
    gdf: gpd.GeoDataFrame,
    bdnb_usage: "dict[str, str] | None" = None,
) -> gpd.GeoDataFrame:
    """Garde les bâtiments résidentiels selon USAGE1/USAGE2, OSM puis BDNB.

    Logique :
      1. ``USAGE1 == "Résidentiel"`` → toujours conservé (source BD_TOPO fiable)
      2. Tous les autres bâtiments → croisement OSM + BDNB :
         - tag OSM résidentiel **ou** BDNB ``residentiel``         → conservé
         - tag OSM non-résidentiel **ou** BDNB ``travail``/``annexe`` → exclu
         - sinon (usage inconnu) → conservé uniquement si ``Indifférencié``

    `bdnb_usage` (carte ID → catégorie) affine le sort des « Indifférencié » :
    sans elle, le comportement est strictement l'ancien (OSM seul).
    """
    import pandas as pd

    usage1 = gdf.get("USAGE1", pd.Series(dtype=str, index=gdf.index))
    usage2 = gdf.get("USAGE2", pd.Series(dtype=str, index=gdf.index))

    usage1_null = usage1.isna() | (usage1.astype(str).str.strip() == "")

    # Cas 1 : BD_TOPO dit explicitement "Résidentiel"
    mask_bdtopo = (usage1 == "Résidentiel") | (usage1_null & (usage2 == "Résidentiel"))
    others = ~mask_bdtopo

    # Signal BDNB (vide si non fourni → n'altère pas l'ancien comportement).
    if bdnb_usage and "ID" in gdf.columns:
        cat = gdf["ID"].map(bdnb_usage)
        bdnb_residential = cat == "residentiel"
        bdnb_non_residential = cat.isin(["travail", "annexe"])
    else:
        bdnb_residential = pd.Series(False, index=gdf.index)
        bdnb_non_residential = pd.Series(False, index=gdf.index)

    if "osm_building" in gdf.columns:
        osm_tag = gdf["osm_building"].fillna("").str.lower().str.strip()
        osm_is_residential = osm_tag.isin(OSM_RESIDENTIAL_TAGS)
        osm_is_non_residential = osm_tag.isin(OSM_NON_RESIDENTIAL_TAGS)
        osm_unknown = ~osm_is_residential & ~osm_is_non_residential
    else:
        osm_is_residential = pd.Series(False, index=gdf.index)
        osm_is_non_residential = pd.Series(False, index=gdf.index)
        osm_unknown = pd.Series(True, index=gdf.index)

    # Conserve si un signal dit résidentiel, ou si usage inconnu (Indifférencié)
    # sans aucun signal non-résidentiel (OSM ou BDNB).
    mask_others = others & (
        osm_is_residential
        | bdnb_residential
        | (osm_unknown & (usage1 == "Indifférencié")
           & ~osm_is_non_residential & ~bdnb_non_residential)
    )

    logger.info(
        "Filtre résidentiel (others) : +%d OSM-rés, +%d BDNB-rés, "
        "-%d OSM-non-rés, -%d BDNB-non-rés",
        int((others & osm_is_residential).sum()),
        int((others & bdnb_residential).sum()),
        int((others & osm_is_non_residential).sum()),
        int((others & bdnb_non_residential).sum()),
    )

    mask = mask_bdtopo | mask_others
    return gdf[mask].copy()


def _compute_nb_etages(gdf: gpd.GeoDataFrame) -> gpd.pd.Series:
    """Calcule le nombre d'étages par bâtiment.

    Priorité décroissante :
      1. ``NB_ETAGES`` BD_TOPO  (valeur explicite)
      2. ``HAUTEUR``   BD_TOPO  (dérivé : floor(H / 3 m))
      3. ``osm_levels``          (tag OSM building:levels)
      4. Défaut : 1 étage
    """
    import pandas as pd

    result = pd.Series(1.0, index=gdf.index)

    # Priorité 3 (la plus basse) : osm_levels
    if "osm_levels" in gdf.columns:
        lev = gdf["osm_levels"]
        valid = lev.notna() & (lev >= 1)
        result = result.where(~valid, lev.clip(lower=1))

    # Priorité 2 : HAUTEUR BD_TOPO → nb étages dérivé
    if "HAUTEUR" in gdf.columns:
        from_hauteur = (gdf["HAUTEUR"].fillna(0) / HAUTEUR_PAR_ETAGE).round().clip(lower=1)
        valid = gdf["HAUTEUR"].notna() & (gdf["HAUTEUR"] > 0)
        result = result.where(~valid, from_hauteur)

    # Priorité 1 (la plus haute) : NB_ETAGES BD_TOPO
    if "NB_ETAGES" in gdf.columns:
        nb = gdf["NB_ETAGES"].fillna(0).clip(lower=0)
        valid = nb >= 1
        result = result.where(~valid, nb)

    return result.clip(lower=1)


def _surface_par_logt_par_iris(gdf: gpd.GeoDataFrame) -> gpd.pd.Series:
    """Calcule la surface volumique médiane par logement par IRIS sur les bâtiments connus.

    Surface volumique = (surface_sol × nb_étages) / NB_LOGTS — cohérente avec la
    formule d'estimation qui multiplie aussi par nb_étages. Utiliser la surface
    au sol seule (surface_sol / NB_LOGTS) introduirait un biais ×nb_étages systématique.

    Pour chaque bâtiment à estimer, retourne la surface volumique médiane par logement
    observée sur les bâtiments avec NB_LOGTS connu dans le même IRIS.
    Fallback sur SURFACE_MOY_DEFAULT si l'IRIS n'a pas assez de bâtiments connus.

    Nécessite la colonne `code_iris` dans gdf (présente dans batim_grenoble.shp).
    """
    if "code_iris" not in gdf.columns:
        logger.debug("Colonne code_iris absente — calibration globale uniquement")
        return gpd.pd.Series(SURFACE_MOY_DEFAULT, index=gdf.index)

    known = gdf[gdf["NB_LOGTS"].notna() & (gdf["NB_LOGTS"] > 0)].copy()
    # Surface volumique (cohérente avec l'estimation) : surface_sol × nb_étages / NB_LOGTS
    nb_etages_known = _compute_nb_etages(known)
    known["_surf_logt"] = (known.geometry.area * nb_etages_known) / known["NB_LOGTS"].replace(0, np.nan)

    # Médiane par IRIS sur les bâtiments connus
    iris_median = (
        known.groupby("code_iris")["_surf_logt"]
        .agg(["median", "count"])
        .rename(columns={"median": "surf_med", "count": "n_known"})
    )

    # Mapper sur chaque bâtiment
    surf_series = gdf["code_iris"].map(iris_median["surf_med"])
    n_known_series = gdf["code_iris"].map(iris_median["n_known"]).fillna(0)

    # Fallback global pour les IRIS avec trop peu de bâtiments connus
    fallback_mask = (n_known_series < SURFACE_MIN_IRIS) | surf_series.isna()
    surf_series = surf_series.where(~fallback_mask, SURFACE_MOY_DEFAULT)

    n_local = (~fallback_mask).sum()
    n_fallback = fallback_mask.sum()
    logger.debug(
        "Calibration surface/logt : %d bâtiments avec IRIS local, %d avec fallback %.0fm²",
        n_local, n_fallback, SURFACE_MOY_DEFAULT,
    )
    return surf_series


def estimate_nb_logts(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Estime NB_LOGTS pour les bâtiments où la valeur est manquante ou nulle.

    Priorité décroissante :
      1. ``NB_LOGTS``      BD_TOPO  → source ``"bdtopo"``
      2. ``osm_flats``     OSM      → source ``"osm_flats"``  (tag building:flats)
      3. Estimation surfacique avec calibration locale ou fallback :
         - nb_étages : NB_ETAGES (BD_TOPO) > HAUTEUR (BD_TOPO) > osm_levels (OSM) > 1
         - surface/logement : médiane IRIS (``code_iris``) ou SURFACE_MOY_DEFAULT
         → source ``"iris_local"`` ou ``"fallback"``
    """
    if "NB_LOGTS" not in gdf.columns:
        gdf["NB_LOGTS"] = np.nan

    needs_estimate = gdf["NB_LOGTS"].isna() | (gdf["NB_LOGTS"] == 0)
    gdf["NB_LOGTS_source"] = "bdtopo"
    gdf["NB_LOGTS_ESTIME"] = False

    # ── Étape 2 : building:flats OSM ──────────────────────────────────────────
    if "osm_flats" in gdf.columns:
        has_flats = needs_estimate & gdf["osm_flats"].notna() & (gdf["osm_flats"] > 0)
        if has_flats.any():
            gdf.loc[has_flats, "NB_LOGTS"] = gdf.loc[has_flats, "osm_flats"]
            gdf.loc[has_flats, "NB_LOGTS_source"] = "osm_flats"
            gdf.loc[has_flats, "NB_LOGTS_ESTIME"] = True
            needs_estimate = needs_estimate & ~has_flats
            logger.info("%d valeurs NB_LOGTS issues de OSM building:flats", has_flats.sum())

    # ── Étape 3 : estimation surfacique ───────────────────────────────────────
    n_to_estimate = needs_estimate.sum()
    if n_to_estimate == 0:
        logger.info("Aucune estimation NB_LOGTS nécessaire par surface")
        gdf["NB_LOGTS"] = gdf["NB_LOGTS"].astype(int)
        return gdf

    nb_etages = _compute_nb_etages(gdf)
    surf_par_logt = _surface_par_logt_par_iris(gdf)

    area = gdf.geometry.area
    estimated = np.floor(area * nb_etages / surf_par_logt).clip(lower=1)

    gdf.loc[needs_estimate, "NB_LOGTS"] = estimated[needs_estimate]
    gdf.loc[needs_estimate, "NB_LOGTS_ESTIME"] = True

    is_local = needs_estimate & (surf_par_logt != SURFACE_MOY_DEFAULT)
    is_fallback = needs_estimate & (surf_par_logt == SURFACE_MOY_DEFAULT)
    gdf.loc[is_local, "NB_LOGTS_source"] = "iris_local"
    gdf.loc[is_fallback, "NB_LOGTS_source"] = "fallback"

    logger.info(
        "%d valeurs NB_LOGTS estimées par surface — iris_local : %d, fallback %.0fm² : %d",
        n_to_estimate, is_local.sum(), SURFACE_MOY_DEFAULT, is_fallback.sum(),
    )

    gdf["NB_LOGTS"] = gdf["NB_LOGTS"].astype(int)
    return gdf
