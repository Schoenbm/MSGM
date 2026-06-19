"""Loader des équipements éducatifs INSEE (BPE — Base Permanente des Équipements).

Source **autoritaire et géolocalisée** des équipements (vs heuristique de nom OSM)
pour les destinations des agents enfants. Le code `TYPEQU` distingue exactement
les niveaux ; les coordonnées `LAMBERT_X/Y` sont en Lambert-93 (métropole, pas de
reprojection).

BPE 2024, fichier détail unique (~157 Mo, tous équipements France). On télécharge
via le pipeline de cache puis on filtre aux types éducatifs et au département.
Couplé au millésime 2024 (URL + noms de colonnes).
"""

import logging
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd

from .cache import ensure_cached, valid_zip

logger = logging.getLogger(__name__)

# Fichier détail géolocalisé BPE 2024. Page : insee.fr/fr/statistiques/8217525
_BPE_URL = "https://www.insee.fr/fr/statistiques/fichier/8217525/BPE24.zip"

_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "cache"

# TYPEQU (nomenclature BPE 2024) → niveau scolaire utilisé par les agents.
BPE_EDUCATION_TYPEQU: dict[str, str] = {
    "C107": "ecole",    # école maternelle
    "C108": "ecole",    # école primaire
    "C109": "ecole",    # école élémentaire
    "C201": "college",
    "C301": "lycee",    # lycée général/technologique
    "C302": "lycee",    # lycée professionnel
    "C303": "lycee",    # lycée technique/professionnel agricole
    "D502": "creche",   # établissement d'accueil du jeune enfant
}

_USECOLS = ["TYPEQU", "DEP", "DEPCOM", "LAMBERT_X", "LAMBERT_Y", "EPSG",
            "CAPACITE_D_ACCUEIL", "NOMRS"]
_CHUNK = 500_000
TARGET_CRS = "EPSG:2154"


def _stream_to_file(url: str, dest: Path) -> None:
    """Stream HTTP `url` → `dest`. Lève si transfert incomplet (Content-Length)."""
    import requests

    logger.info("Téléchargement BPE (~157 Mo) : %s", url)
    with requests.get(url, stream=True, timeout=600) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        downloaded = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
    if total and downloaded != total:
        raise IOError(f"Téléchargement BPE incomplet : {downloaded}/{total} octets ({url}).")


def load_bpe_education(
    departement: str = "38",
    url: str = _BPE_URL,
    cache_dir: "Path | None" = None,
) -> gpd.GeoDataFrame:
    """Charge les équipements éducatifs BPE géolocalisés d'un département.

    Args:
        departement: code département (2-3 chiffres) à conserver.
        url:         URL du fichier détail BPE (millésime 2024 par défaut).
        cache_dir:   répertoire de cache (défaut : data/cache).

    Returns:
        GeoDataFrame (EPSG:2154), une ligne par équipement, colonnes :
        - equip_id : identifiant stable (``bpe-<n>``)
        - kind     : "creche" | "ecole" | "college" | "lycee"
        - capacity : capacité d'accueil (élèves) si connue, sinon 1.0
        - nom      : nom de l'établissement
        - geometry : Point (Lambert-93)
    """
    cache_dir = cache_dir or _CACHE_DIR
    archive = ensure_cached(
        cache_dir / "bpe24.zip",
        produce=lambda tmp: _stream_to_file(url, tmp),
        validate=valid_zip,
        label="bpe24.zip",
    )

    dep = str(departement).strip()
    types = set(BPE_EDUCATION_TYPEQU)
    parts: list[pd.DataFrame] = []
    with zipfile.ZipFile(archive) as z:
        csv_name = next(
            n for n in z.namelist()
            if n.lower().endswith(".csv") and not n.lower().startswith("meta")
        )
        with z.open(csv_name) as f:
            for chunk in pd.read_csv(f, sep=";", usecols=_USECOLS, dtype=str,
                                     chunksize=_CHUNK, low_memory=False):
                keep = chunk[chunk["TYPEQU"].isin(types) & (chunk["DEP"] == dep)]
                if not keep.empty:
                    parts.append(keep)

    if not parts:
        logger.warning("BPE : aucun équipement éducatif pour le département %s", dep)
        return gpd.GeoDataFrame(
            columns=["equip_id", "kind", "capacity", "nom", "geometry"],
            geometry="geometry", crs=TARGET_CRS,
        )

    df = pd.concat(parts, ignore_index=True)
    df["kind"] = df["TYPEQU"].map(BPE_EDUCATION_TYPEQU)

    # Coordonnées Lambert-93 (métropole). On écarte les rares lignes non géolocalisées
    # ou dans un autre système (DOM) pour rester cohérent en EPSG:2154.
    x = pd.to_numeric(df["LAMBERT_X"].str.replace(",", ".", regex=False), errors="coerce")
    y = pd.to_numeric(df["LAMBERT_Y"].str.replace(",", ".", regex=False), errors="coerce")
    valid = x.notna() & y.notna() & (df["EPSG"].fillna("2154") == "2154")
    n_drop = int((~valid).sum())
    if n_drop:
        logger.info("BPE : %d équipements écartés (coordonnées absentes ou hors Lambert-93)", n_drop)

    cap = pd.to_numeric(df["CAPACITE_D_ACCUEIL"], errors="coerce")
    out = gpd.GeoDataFrame(
        {
            "equip_id": [f"bpe-{i}" for i in range(int(valid.sum()))],
            "kind": df.loc[valid, "kind"].to_numpy(),
            "capacity": cap[valid].fillna(1.0).clip(lower=1.0).to_numpy(),
            "nom": df.loc[valid, "NOMRS"].to_numpy(),
        },
        geometry=gpd.points_from_xy(x[valid], y[valid]),
        crs=TARGET_CRS,
    )
    logger.info("BPE éducation (dept %s) : %s", dep, out["kind"].value_counts().to_dict())
    return out
