"""Loader for IRIS geographic boundaries + INSEE 2022 census data.

Downloads automatically from IGN and INSEE if not already cached in data/cache/.
Filters by commune codes (preferred) or department code fallback.
"""

import logging
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests

logger = logging.getLogger(__name__)

# ── URLs ──────────────────────────────────────────────────────────────────────
_CONTOURS_URL = (
    "https://data.geopf.fr/telechargement/download/CONTOURS-IRIS/"
    "CONTOURS-IRIS_3-0__SHP_LAMB93_FXX_2024-01-01/"
    "CONTOURS-IRIS_3-0__SHP_LAMB93_FXX_2024-01-01.7z"
)
_POP_URL = (
    "https://www.insee.fr/fr/statistiques/fichier/8647014/"
    "base-ic-evol-struct-pop-2022_csv.zip"
)
_LOG_URL = (
    "https://www.insee.fr/fr/statistiques/fichier/8647012/"
    "base-ic-logement-2022_csv.zip"
)

_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "cache"
_DEFAULT_DEP = "38"           # Isère
_TAILLE_MEN_DEFAUT = 2.3      # moyenne nationale de secours


# ── Download / cache ──────────────────────────────────────────────────────────

def _download(url: str, dest: Path) -> Path:
    """Download url → dest. Skip if already cached."""
    if dest.exists():
        logger.info("Cache trouvé : %s", dest.name)
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Téléchargement : %s", url)
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        downloaded = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded / total * 100
                    if pct % 10 < (downloaded - len(chunk)) / total * 100 % 10 or pct >= 99:
                        logger.debug("  %.0f%%", pct)
    logger.info("Téléchargé : %s (%.1f MB)", dest.name, dest.stat().st_size / 1e6)
    return dest


def _extract_contours_7z(archive: Path, extract_dir: Path) -> Path:
    """Extract 7z archive and return path to the CONTOURS-IRIS .shp file."""
    if not extract_dir.exists():
        try:
            import py7zr
        except ImportError as e:
            raise ImportError(
                "py7zr est nécessaire pour extraire les contours IRIS.\n"
                "Installez-le avec : pip install py7zr"
            ) from e
        logger.info("Extraction de l'archive 7z (peut prendre une minute)...")
        with py7zr.SevenZipFile(archive, mode="r") as z:
            z.extractall(path=extract_dir)
        logger.info("Extraction terminée dans %s", extract_dir)

    # Chercher CONTOURS-IRIS.shp en priorité (ignorer EMPRISE.shp)
    shp_files = [p for p in extract_dir.rglob("*.shp") if p.stem == "CONTOURS-IRIS"]
    if not shp_files:
        shp_files = [p for p in extract_dir.rglob("*.shp") if p.stem != "EMPRISE"]
    if not shp_files:
        raise FileNotFoundError(f"Aucun fichier CONTOURS-IRIS.shp trouvé dans {extract_dir}")
    return shp_files[0]


def _load_csv_from_zip(url: str, cache_name: str, dep_code: str) -> pd.DataFrame:
    """Download ZIP containing a CSV, return as DataFrame filtered to dep_code."""
    archive = _download(url, _CACHE_DIR / cache_name)
    with zipfile.ZipFile(archive) as z:
        csv_name = next(
            n for n in z.namelist()
            if n.lower().endswith(".csv") and not n.lower().startswith("meta")
        )
        with z.open(csv_name) as f:
            df = pd.read_csv(f, sep=";", dtype={"IRIS": str}, low_memory=False)
    df = df[df["IRIS"].str.startswith(dep_code)].copy()
    logger.debug("CSV %s — %d lignes pour dept %s", cache_name, len(df), dep_code)
    return df


# ── Public loader ─────────────────────────────────────────────────────────────

def load_iris(
    iris_codes: list[str] | None = None,
    dep_code: str = _DEFAULT_DEP,
    shp_path: "str | Path | None" = None,
) -> gpd.GeoDataFrame:
    """Load IRIS geometries + 2022 census population.

    Downloads and caches data automatically from IGN and INSEE.
    Returns a GeoDataFrame compatible with the existing pipeline:
    - geometry          : IRIS polygon (Lambert-93 / EPSG:2154)
    - Ind_total         : total population (P22_POP)
    - taille_moy_menage : average household size (P22_POP / P22_MEN)

    Args:
        iris_codes: Liste de codes IRIS à 9 chiffres (ex. ["381850101", "381850102"]).
                    Si fournie, filtre sur ces IRIS via téléchargement IGN.
        dep_code:   Code département de secours si ni iris_codes ni shp_path fournis.
        shp_path:   Shapefile utilisateur contenant les IRIS (colonne code_iris ou
                    CODE_IRIS). Si fourni, évite le téléchargement IGN.

    Returns:
        GeoDataFrame avec une ligne par IRIS.
    """
    # 1. Contours IRIS
    if shp_path is not None:
        gdf = gpd.read_file(Path(shp_path))
        # Drop fid column that conflicts with GeoPackage format
        if "fid" in gdf.columns:
            gdf = gdf.drop(columns=["fid"])
        # Normaliser la casse de la colonne code IRIS
        if "code_iris" in gdf.columns and "CODE_IRIS" not in gdf.columns:
            gdf = gdf.rename(columns={"code_iris": "CODE_IRIS"})
        logger.info("IRIS chargés depuis shapefile : %d IRIS", len(gdf))
    else:
        archive = _download(_CONTOURS_URL, _CACHE_DIR / "contours-iris-2024.7z")
        contours_shp = _extract_contours_7z(archive, _CACHE_DIR / "contours-iris-2024")
        gdf = gpd.read_file(contours_shp)
        logger.info("CONTOURS-IRIS chargés : %d IRIS (France entière)", len(gdf))

        if iris_codes:
            gdf = gdf[gdf["CODE_IRIS"].isin(iris_codes)].copy()
            logger.info("Filtré sur %d codes IRIS : %d trouvés", len(iris_codes), len(gdf))
            if len(gdf) < len(iris_codes):
                missing = set(iris_codes) - set(gdf["CODE_IRIS"])
                logger.warning("%d codes IRIS non trouvés : %s", len(missing), sorted(missing))
        else:
            gdf = gdf[gdf["CODE_IRIS"].str.startswith(dep_code)].copy()
            logger.info("Filtré dept %s : %d IRIS", dep_code, len(gdf))

    # 2. Population par IRIS (INSEE RP 2022) — filtrage CSV par département
    csv_dep = gdf["CODE_IRIS"].iloc[0][:2] if not gdf.empty else dep_code
    pop = _load_csv_from_zip(_POP_URL, "base-ic-pop-2022.zip", csv_dep)
    log = _load_csv_from_zip(_LOG_URL, "base-ic-logement-2022.zip", csv_dep)

    # 3. Fusion des tables statistiques
    stats = pop[["IRIS", "P22_POP", "P22_PMEN"]].merge(
        log[["IRIS", "P22_MEN"]], on="IRIS", how="left"
    )

    # 4. Jointure géométrie + stats
    gdf = gdf.merge(stats, left_on="CODE_IRIS", right_on="IRIS", how="left")

    # 5. Colonnes de sortie compatibles avec le pipeline
    gdf["Ind_total"] = gdf["P22_POP"].fillna(0)
    gdf["taille_moy_menage"] = (
        gdf["P22_POP"] / gdf["P22_MEN"].replace(0, float("nan"))
    ).fillna(_TAILLE_MEN_DEFAUT)

    n_missing = gdf["P22_POP"].isna().sum()
    if n_missing > 0:
        logger.warning("%d IRIS sans données de population (Ind_total=0)", n_missing)

    logger.info(
        "Ind_total — min=%.1f  max=%.1f  mean=%.1f  total=%.0f",
        gdf["Ind_total"].min(), gdf["Ind_total"].max(),
        gdf["Ind_total"].mean(), gdf["Ind_total"].sum(),
    )
    logger.info(
        "taille_moy_menage — min=%.2f  max=%.2f  mean=%.2f",
        gdf["taille_moy_menage"].min(),
        gdf["taille_moy_menage"].max(),
        gdf["taille_moy_menage"].mean(),
    )

    return gdf
