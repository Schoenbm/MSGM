"""Loader des flux domicile-travail INSEE (MOBPRO, base de flux agrégée 2022).

Base de flux **agrégée** commune de résidence → commune de travail (et non le
fichier détail individuel de 7,4 M lignes). ~11 Mo, tous les flux, France hors
Mayotte. Population concernée : actifs occupés de 15 ans ou plus.

Usage prévu (à venir) : caler le modèle gravitaire d'affectation des lieux de
travail (cf. matching/workplaces.py) sur des distances/flux réels plutôt que sur
un `decay_m` posé au doigt mouillé.

Téléchargement via le pipeline de cache unique (`ensure_cached`) comme les autres
sources INSEE/IGN. Couplé au millésime 2022 (URL + noms de colonnes).
"""

import logging
import zipfile
from pathlib import Path

import pandas as pd

from .cache import ensure_cached, valid_zip

logger = logging.getLogger(__name__)

# Base de flux agrégée 2022 (CSV, tous les flux). Page : insee.fr/fr/statistiques/8582949
_MOBPRO_URL = (
    "https://www.insee.fr/fr/statistiques/fichier/8582949/"
    "base-flux-mobilite-domicile-lieu-travail-2022_csv.zip"
)

_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "cache"

# Colonnes de la base de flux MOBPRO 2022 (millésime couplé) :
#   CODGEO  : code commune de résidence
#   DCLT    : code commune du lieu de travail
#   NBFLUX_C22_ACTOCC15P : nombre d'actifs occupés 15+ effectuant ce trajet
COL_RESIDENCE = "CODGEO"
COL_TRAVAIL = "DCLT"
COL_FLUX = "NBFLUX_C22_ACTOCC15P"


def _stream_to_file(url: str, dest: Path) -> None:
    """Stream HTTP `url` → `dest`. Lève si transfert incomplet (Content-Length)."""
    import requests

    logger.info("Téléchargement MOBPRO : %s", url)
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        downloaded = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
    if total and downloaded != total:
        raise IOError(
            f"Téléchargement MOBPRO incomplet : {downloaded}/{total} octets ({url})."
        )


def load_mobpro(
    communes: "list[str] | None" = None,
    url: str = _MOBPRO_URL,
    cache_dir: "Path | None" = None,
) -> pd.DataFrame:
    """Charge la base de flux domicile-travail MOBPRO 2022.

    Args:
        communes:  codes communes (5 chiffres) à conserver côté RÉSIDENCE. Si None,
                   renvoie tous les flux (France entière). Filtre uniquement la
                   commune de résidence (le lieu de travail reste libre, pour
                   capter les flux sortants de la zone).
        url:       URL de la base de flux CSV (millésime 2022 par défaut).
        cache_dir: répertoire de cache (défaut : data/cache).

    Returns:
        DataFrame avec au moins CODGEO (résidence), DCLT (travail) et
        NBFLUX_C22_ACTOCC15P (nombre d'actifs).
    """
    cache_dir = cache_dir or _CACHE_DIR
    archive = ensure_cached(
        cache_dir / "mobpro-flux-2022.zip",
        produce=lambda tmp: _stream_to_file(url, tmp),
        validate=valid_zip,
        label="mobpro-flux-2022.zip",
    )

    with zipfile.ZipFile(archive) as z:
        csv_name = next(
            n for n in z.namelist()
            if n.lower().endswith(".csv") and not n.lower().startswith("meta")
        )
        with z.open(csv_name) as f:
            df = pd.read_csv(
                f, sep=";", dtype={COL_RESIDENCE: str, COL_TRAVAIL: str},
                low_memory=False,
            )
    logger.info("MOBPRO chargé : %d flux (France)", len(df))

    if communes:
        codes = {str(c).strip() for c in communes}
        df = df[df[COL_RESIDENCE].isin(codes)].copy()
        logger.info("MOBPRO filtré sur %d communes de résidence : %d flux",
                    len(codes), len(df))
    return df
