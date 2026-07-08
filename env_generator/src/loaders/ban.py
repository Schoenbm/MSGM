"""Loader BAN (Base Adresse Nationale) — adresses ponctuelles par département.

Export département entier (`adresses-<dep>.csv.gz`, ~15 Mo compressé pour
l'Isère), une ligne par adresse : numéro + nom de voie + commune + point.
Les colonnes `x`/`y` sont DÉJÀ en Lambert-93 — pas de reprojection (les
`lon`/`lat` sont ignorées).

Sert au chantier carte scolaire : la carte des collèges publics est à la
granularité de la RUE (voie + plage de numéros), donc chaque bâtiment
résidentiel doit être rattaché à une adresse (jointure plus-proche-voisin,
cf. matching/schooling.attach_building_addresses).

Téléchargement via le pipeline de cache unique (`ensure_cached`), parsing
séparé (`_parse_ban_csv` pur, testable ; pandas décompresse nativement le
`.gz` par extension de fichier).
"""

import logging
from pathlib import Path

import geopandas as gpd
import pandas as pd

from src.text_utils import normalize_voie

from .cache import ensure_cached, valid_nonempty

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "cache"

_BAN_URL = "https://adresse.data.gouv.fr/data/ban/adresses/latest/csv/adresses-{dep}.csv.gz"


def _stream_to_file(url: str, dest: Path) -> None:
    """Stream HTTP `url` → `dest`. Lève si transfert incomplet (Content-Length)."""
    import requests

    logger.info("Téléchargement BAN : %s", url)
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


def _parse_ban_csv(
    path: Path,
    communes: "set[str] | None" = None,
) -> gpd.GeoDataFrame:
    """Parse un export BAN département (pur, testable ; `.gz` géré par pandas).

    Args:
        path:     CSV BAN (`;`), éventuellement gzippé.
        communes: si fourni, ne garde que ces codes INSEE (le fichier est au
                  niveau département, la zone d'étude ne l'est pas toujours).

    Returns:
        GeoDataFrame (EPSG:2154) : code_insee, nom_voie_norm (normalisé),
        numero (int), geometry (Point depuis x/y, déjà en Lambert-93).
    """
    df = pd.read_csv(
        path, sep=";", dtype={"code_insee": str},
        usecols=["numero", "nom_voie", "code_insee", "x", "y"],
        low_memory=False,
    )
    if communes is not None:
        codes = {str(c).strip() for c in communes}
        df = df[df["code_insee"].isin(codes)]

    numero = pd.to_numeric(df["numero"], errors="coerce")
    x = pd.to_numeric(df["x"], errors="coerce")
    y = pd.to_numeric(df["y"], errors="coerce")
    ok = numero.notna() & x.notna() & y.notna()
    if (~ok).any():
        logger.debug("Adresses BAN sans numéro ou sans coordonnées écartées : %d",
                     int((~ok).sum()))
    df, numero, x, y = df[ok], numero[ok], x[ok], y[ok]
    return gpd.GeoDataFrame(
        {
            "code_insee": df["code_insee"],
            "nom_voie_norm": df["nom_voie"].map(normalize_voie),
            "numero": numero.astype(int),
        },
        geometry=gpd.points_from_xy(x, y),
        crs="EPSG:2154",
    ).reset_index(drop=True)


def load_ban_addresses(
    departement: str = "38",
    communes: "set[str] | None" = None,
    cache_dir: "Path | None" = None,
) -> gpd.GeoDataFrame:
    """Charge (download + cache + parse) les adresses BAN d'un département.

    Args:
        departement: code département sur 2 caractères (ex. "38").
        communes:    codes INSEE à conserver (None = tout le département).
        cache_dir:   répertoire de cache (défaut : data/cache).

    Returns:
        GeoDataFrame des adresses (cf. _parse_ban_csv).
    """
    cache_dir = cache_dir or _CACHE_DIR
    path = ensure_cached(
        cache_dir / f"adresses_{departement}.csv.gz",
        produce=lambda tmp: _stream_to_file(_BAN_URL.format(dep=departement), tmp),
        validate=valid_nonempty,
    )
    gdf = _parse_ban_csv(path, communes=communes)
    logger.info("Adresses BAN (dép. %s) : %d points sur %d commune(s)",
                departement, len(gdf), gdf["code_insee"].nunique())
    return gdf
