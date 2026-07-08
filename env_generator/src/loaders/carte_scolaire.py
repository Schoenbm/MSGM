"""Loaders carte scolaire des collèges (data.education.gouv.fr), millésime live.

Deux datasets nationaux, filtrés au département côté API :

1. **Secteurs de recrutement des collèges publics**
   (`fr-en-carte-scolaire-colleges-publics`) : pour chaque commune, soit un
   secteur unique (`secteur_unique="O"`, tout le territoire communal), soit une
   partition DÉTERMINISTE par rue (`"N"` : nom de voie + plage de numéros +
   parité → un seul `code_rne`). La granularité est la RUE, pas l'IRIS (une même
   commune peut être coupée en plusieurs secteurs par rue — ex. Apprieu, 38013).

2. **Adresse et géolocalisation des établissements 1er/2nd degré**
   (`fr-en-adresse-et-geolocalisation-etablissements-premier-et-second-degre`),
   filtré collèges OUVERTS (public ET privé) : ce dataset porte le `numero_uai`
   (= `code_rne` de la carte scolaire, la clé de jointure que le BPE n'a pas —
   c'est pour ça qu'il remplace le BPE pour le niveau collège) et des coordonnées
   DÉJÀ en Lambert-93 (`epsg` = EPSG:2154, pas de reprojection).

Aucune capacité fiable dans aucune source (le `CAPACITE_D_ACCUEIL` du BPE est
toujours `_Z` pour les collèges/lycées) : `capacity=1.0` constante, comme le
reste du pipeline éducation.

Téléchargement via le pipeline de cache unique (`ensure_cached`), parsing séparé
du téléchargement (fonctions `_parse_*` pures, testables sur fixture).
"""

import logging
from pathlib import Path

import geopandas as gpd
import pandas as pd

from src.text_utils import normalize_voie

from .cache import ensure_cached, valid_nonempty

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "cache"

# Exports CSV de l'API Explore v2.1 (`;`), filtrés au département côté serveur.
_SECTEURS_URL = (
    "https://data.education.gouv.fr/api/explore/v2.1/catalog/datasets/"
    "fr-en-carte-scolaire-colleges-publics/exports/csv"
    "?where=code_departement%3D%22{dep}%22&limit=-1"
)
_ETABLISSEMENTS_URL = (
    "https://data.education.gouv.fr/api/explore/v2.1/catalog/datasets/"
    "fr-en-adresse-et-geolocalisation-etablissements-premier-et-second-degre/"
    "exports/csv"
    "?where=code_departement%3D%22{dep}%22%20AND%20nature_uai_libe%3D%22COLLEGE%22"
    "&limit=-1"
)


def _stream_to_file(url: str, dest: Path) -> None:
    """Stream HTTP `url` → `dest`. Lève si transfert incomplet (Content-Length)."""
    import requests

    logger.info("Téléchargement carte scolaire : %s", url)
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        downloaded = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
    if total and downloaded != total:
        raise IOError(f"Téléchargement incomplet : {downloaded}/{total} octets ({url}).")


def _parse_secteurs_csv(path: Path) -> pd.DataFrame:
    """Parse l'export CSV des secteurs de collèges publics (pur, testable).

    Returns:
        DataFrame : code_insee, nom_voie_norm (normalisé, vide si secteur
        unique), n_de_voie_debut, n_de_voie_fin (float), parite (P/I/PI),
        code_rne, secteur_unique (O/N).
    """
    df = pd.read_csv(
        path, sep=";",
        dtype={"code_insee": str, "type_et_libelle": str, "parite": str,
               "code_rne": str, "secteur_unique": str},
        low_memory=False,
    )
    out = pd.DataFrame({
        "code_insee": df["code_insee"],
        "nom_voie_norm": df["type_et_libelle"].map(normalize_voie),
        "n_de_voie_debut": pd.to_numeric(df["n_de_voie_debut"], errors="coerce"),
        "n_de_voie_fin": pd.to_numeric(df["n_de_voie_fin"], errors="coerce"),
        "parite": df["parite"],
        "code_rne": df["code_rne"],
        "secteur_unique": df["secteur_unique"],
    })
    return out


def _parse_etablissements_csv(path: Path) -> gpd.GeoDataFrame:
    """Parse l'export CSV des établissements, filtré collèges OUVERTS (pur).

    Returns:
        GeoDataFrame (EPSG:2154) : equip_id ("college-<UAI>"), code_rne (= UAI),
        kind="college", secteur ("Public"/"Privé"), capacity=1.0, nom, geometry
        (Point depuis coordonnee_x/y, déjà en Lambert-93 — pas de reprojection).
    """
    df = pd.read_csv(
        path, sep=";",
        dtype={"numero_uai": str, "appellation_officielle": str,
               "secteur_public_prive_libe": str, "nature_uai_libe": str,
               "etat_etablissement_libe": str},
        low_memory=False,
    )
    df = df[(df["nature_uai_libe"] == "COLLEGE")
            & (df["etat_etablissement_libe"] == "OUVERT")].copy()
    x = pd.to_numeric(df["coordonnee_x"], errors="coerce")
    y = pd.to_numeric(df["coordonnee_y"], errors="coerce")
    ok = x.notna() & y.notna()
    if (~ok).any():
        logger.warning("Collèges sans coordonnées écartés : %d", int((~ok).sum()))
    df, x, y = df[ok], x[ok], y[ok]
    return gpd.GeoDataFrame(
        {
            "equip_id": "college-" + df["numero_uai"],
            "code_rne": df["numero_uai"],
            "kind": "college",
            "secteur": df["secteur_public_prive_libe"],
            "capacity": 1.0,
            "nom": df["appellation_officielle"],
        },
        geometry=gpd.points_from_xy(x, y),
        crs="EPSG:2154",
    ).reset_index(drop=True)


def load_secteurs_colleges(
    departement: str = "038",
    cache_dir: "Path | None" = None,
) -> pd.DataFrame:
    """Charge (download + cache + parse) les secteurs de collèges publics.

    Args:
        departement: code département sur 3 caractères (ex. "038").
        cache_dir:   répertoire de cache (défaut : data/cache).

    Returns:
        DataFrame des secteurs (cf. _parse_secteurs_csv).
    """
    cache_dir = cache_dir or _CACHE_DIR
    path = ensure_cached(
        cache_dir / f"carte_scolaire_colleges_{departement}.csv",
        produce=lambda tmp: _stream_to_file(_SECTEURS_URL.format(dep=departement), tmp),
        validate=valid_nonempty,
    )
    df = _parse_secteurs_csv(path)
    logger.info("Carte scolaire collèges (dép. %s) : %d lignes de secteur, "
                "%d communes", departement, len(df), df["code_insee"].nunique())
    return df


def load_colleges_publics_prives(
    departement: str = "038",
    cache_dir: "Path | None" = None,
) -> gpd.GeoDataFrame:
    """Charge (download + cache + parse) les collèges géolocalisés (public + privé).

    Args:
        departement: code département sur 3 caractères (ex. "038").
        cache_dir:   répertoire de cache (défaut : data/cache).

    Returns:
        GeoDataFrame des collèges ouverts (cf. _parse_etablissements_csv).
    """
    cache_dir = cache_dir or _CACHE_DIR
    path = ensure_cached(
        cache_dir / f"etablissements_colleges_{departement}.csv",
        produce=lambda tmp: _stream_to_file(
            _ETABLISSEMENTS_URL.format(dep=departement), tmp),
        validate=valid_nonempty,
    )
    gdf = _parse_etablissements_csv(path)
    logger.info("Collèges (dép. %s) : %d ouverts (%d publics, %d privés)",
                departement, len(gdf), int((gdf["secteur"] == "Public").sum()),
                int((gdf["secteur"] == "Privé").sum()))
    return gdf
