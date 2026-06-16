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
from dataclasses import dataclass
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

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

TARGET_CRS = "EPSG:2154"      # Lambert-93, imposé (DT + standard France)
CODE_COL = "CODE_IRIS"        # colonne identifiant dans CONTOURS-IRIS
VALID_TYPES = {"iris", "commune", "departement"}


class MissingIrisError(ValueError):
    """Codes IRIS demandés absents du shapefile local (sélecteur iris)."""

    def __init__(self, missing):
        self.missing = sorted(missing)
        super().__init__(
            f"{len(self.missing)} code(s) IRIS absent(s) du shapefile local : "
            f"{self.missing}"
        )


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


# ── Résolution de zone (sélecteur multi-niveaux) ──────────────────────────────
# Les trois niveaux se résolvent sur la MÊME source (CONTOURS-IRIS), par préfixe
# du CODE_IRIS (9 chiffres = commune[5] + suffixe[4] ; commune[5] = dept[2-3]+...) :
#   - iris        : CODE_IRIS ∈ codes
#   - commune     : CODE_IRIS commence par un code commune  (5 chiffres)
#   - departement : CODE_IRIS commence par un code dept     (2-3 chiffres)

@dataclass(frozen=True)
class Selector:
    """Sélecteur de zone : un niveau administratif + une liste de codes INSEE."""
    type: str
    codes: tuple[str, ...]

    @classmethod
    def from_dict(cls, d: dict) -> "Selector":
        t = str(d["type"]).strip().lower()
        if t not in VALID_TYPES:
            raise ValueError(f"type de sélecteur inconnu : {t!r} (attendu {VALID_TYPES})")
        codes = tuple(str(c).strip() for c in d.get("codes", []))
        if not codes:
            raise ValueError(f"sélecteur '{t}' sans code")
        return cls(type=t, codes=codes)


def _selector_from_legacy(iris_codes: "list[str] | None", dep_code: str) -> Selector:
    """Reconstruit un Selector depuis les anciens paramètres iris_codes / dep_code."""
    if iris_codes:
        return Selector("iris", tuple(str(c).strip() for c in iris_codes))
    return Selector("departement", (str(dep_code).strip(),))


def _coerce_selector(selector: "Selector | dict | None") -> "Selector | None":
    if selector is None or isinstance(selector, Selector):
        return selector
    return Selector.from_dict(selector)


def _match_mask(codes_series: pd.Series, selector: Selector):
    if selector.type == "iris":
        return codes_series.isin(set(selector.codes))
    # commune / departement → match par préfixe (str.startswith accepte un tuple)
    return codes_series.str.startswith(tuple(selector.codes))


def _load_contours_raw(
    shp_path: "str | Path | None" = None,
    contours_url: str = _CONTOURS_URL,
) -> gpd.GeoDataFrame:
    """Charge les contours IRIS bruts : shapefile fourni, sinon téléchargement IGN.

    Sans shp_path, télécharge/extrait CONTOURS-IRIS (France entière, Lambert-93)
    depuis contours_url. Normalise CODE_IRIS en str (zéros de tête préservés).
    """
    if shp_path is not None:
        gdf = gpd.read_file(Path(shp_path))
        if "fid" in gdf.columns:
            gdf = gdf.drop(columns=["fid"])
        if "code_iris" in gdf.columns and CODE_COL not in gdf.columns:
            gdf = gdf.rename(columns={"code_iris": CODE_COL})
        logger.info("IRIS chargés depuis shapefile : %d IRIS", len(gdf))
    else:
        archive = _download(contours_url, _CACHE_DIR / "contours-iris-2024.7z")
        contours_shp = _extract_contours_7z(archive, _CACHE_DIR / "contours-iris-2024")
        gdf = gpd.read_file(contours_shp)
        logger.info("CONTOURS-IRIS chargés : %d IRIS (France entière)", len(gdf))
    if CODE_COL in gdf.columns:
        gdf[CODE_COL] = gdf[CODE_COL].astype(str).str.strip()
    return gdf


def _filter_contours(gdf: gpd.GeoDataFrame, selector: Selector) -> gpd.GeoDataFrame:
    """Filtre les contours selon le sélecteur. Tolérant : warn + vide si rien."""
    selected = gdf.loc[_match_mask(gdf[CODE_COL], selector)].copy()
    if selected.empty:
        logger.warning("Aucun IRIS pour le sélecteur %s=%s", selector.type, selector.codes)
        return selected
    if selector.type == "iris":
        missing = set(selector.codes) - set(selected[CODE_COL])
        if missing:
            logger.warning("Codes IRIS sans correspondance : %s", sorted(missing))
    else:
        for code in selector.codes:
            if not selected[CODE_COL].str.startswith(code).any():
                logger.warning("Code %s (%s) sans IRIS correspondant", code, selector.type)
    logger.info("%d IRIS retenus (%s=%s)", len(selected), selector.type, selector.codes)
    return selected


def _ensure_crs(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if gdf.crs is None:
        raise ValueError("Contours IRIS sans CRS défini — fichier suspect")
    if gdf.crs.to_string() != TARGET_CRS:
        gdf = gdf.to_crs(TARGET_CRS)
    return gdf


def _resolve_geometry(
    selector: "Selector | None",
    shp_path: "str | Path | None",
    contours_url: str,
    on_missing: str,
) -> gpd.GeoDataFrame:
    """Charge les contours filtrés par le sélecteur, **source unique**.

    - Sans shp_path : téléchargement France + filtrage.
    - Avec shp_path : on filtre le shapefile local. Si le sélecteur est de type
      'iris' et que des codes manquent dans le local :
        * on_missing='error'    → lève MissingIrisError (codes manquants listés) ;
        * on_missing='download' → bascule ENTIÈREMENT sur le download France
          (jamais de fusion local+download : une seule édition d'IRIS par run).
    """
    if shp_path is None:
        gdf = _load_contours_raw(None, contours_url)
        return _filter_contours(gdf, selector) if selector is not None else gdf

    local = _load_contours_raw(shp_path)
    filtered = _filter_contours(local, selector) if selector is not None else local

    if selector is not None and selector.type == "iris":
        missing = set(selector.codes) - set(filtered[CODE_COL])
        if missing:
            if on_missing == "download":
                logger.warning(
                    "%d code(s) absent(s) du shapefile local %s → bascule sur le "
                    "téléchargement France (source unique)", len(missing), sorted(missing),
                )
                gdf = _load_contours_raw(None, contours_url)
                return _filter_contours(gdf, selector)
            raise MissingIrisError(missing)
    return filtered


def resolve_zone(
    selector: "Selector | dict | None" = None,
    *,
    iris_codes: "list[str] | None" = None,
    dep_code: str = _DEFAULT_DEP,
    shp_path: "str | Path | None" = None,
    buffer_m: float = 0.0,
    contours_url: str = _CONTOURS_URL,
    on_missing: str = "error",
) -> "tuple[BaseGeometry, gpd.GeoDataFrame]":
    """Résout une zone en emprise unifiée (Lambert-93), SANS données INSEE.

    Sert à définir la zone *region* (emprise d'évacuation) : seule la géométrie
    compte, pas la population. Pour la zone *population* (avec INSEE), utiliser
    ``load_iris``.

    Args:
        selector:   Selector ou dict {type, codes}. Prioritaire si fourni.
        iris_codes: ancien style — équivaut à Selector("iris", iris_codes).
        dep_code:   ancien style — fallback Selector("departement", [dep_code]).
        shp_path:   shapefile d'IRIS local. Filtré par le sélecteur s'il y en a
                    un, sinon utilisé tel quel (toutes les lignes).
        buffer_m:   buffer en mètres sur l'emprise (réseau de bordure). Valide
                    en Lambert-93.
        contours_url: URL de téléchargement CONTOURS-IRIS si pas de shp_path.

    Returns:
        (emprise, iris_gdf) : emprise = union (buffer optionnel) ; iris_gdf =
        géométrie par IRIS (SANS buffer), Lambert-93.
    """
    sel = _coerce_selector(selector)
    if sel is None and shp_path is None:
        sel = _selector_from_legacy(iris_codes, dep_code)
    gdf = _resolve_geometry(sel, shp_path, contours_url, on_missing)
    if gdf.empty:
        raise ValueError("resolve_zone : aucune géométrie pour la zone demandée")
    gdf = _ensure_crs(gdf).reset_index(drop=True)
    footprint = unary_union(gdf.geometry.values)
    if buffer_m and buffer_m > 0:
        footprint = footprint.buffer(buffer_m)     # mètres : valide en Lambert-93
        logger.info("Buffer de %.0f m appliqué à l'emprise", buffer_m)
    return footprint, gdf


def validate_subset(
    inner: gpd.GeoDataFrame,
    outer_footprint: BaseGeometry,
    name_inner: str = "population",
    name_outer: str = "region",
) -> bool:
    """Vérifie population ⊆ region. Tolérant : log + False si débordement."""
    inner_fp = unary_union(inner.geometry.values)
    if outer_footprint.covers(inner_fp):
        return True
    overflow = inner_fp.difference(outer_footprint).area
    logger.warning(
        "%s déborde de %s (≈ %.0f m² hors emprise) — vérifier les codes",
        name_inner, name_outer, overflow,
    )
    return False


# ── Public loader ─────────────────────────────────────────────────────────────

def load_iris(
    iris_codes: list[str] | None = None,
    dep_code: str = _DEFAULT_DEP,
    shp_path: "str | Path | None" = None,
    selector: "Selector | dict | None" = None,
    contours_url: str = _CONTOURS_URL,
    pop_url: str = _POP_URL,
    log_url: str = _LOG_URL,
    on_missing: str = "error",
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
        selector:   Selector ou dict {type, codes} — sélecteur multi-niveaux
                    (iris | commune | departement). Prioritaire sur iris_codes /
                    dep_code s'il est fourni.

    Returns:
        GeoDataFrame avec une ligne par IRIS.
    """
    # 1. Contours IRIS (source unique : shapefile local OU download France)
    sel = _coerce_selector(selector)
    if sel is None and shp_path is None:
        sel = _selector_from_legacy(iris_codes, dep_code)
    gdf = _resolve_geometry(sel, shp_path, contours_url, on_missing)

    # 2. Population par IRIS (INSEE RP 2022) — filtrage CSV par département
    csv_dep = gdf["CODE_IRIS"].iloc[0][:2] if not gdf.empty else dep_code
    pop = _load_csv_from_zip(pop_url, "base-ic-pop-2022.zip", csv_dep)
    log = _load_csv_from_zip(log_url, "base-ic-logement-2022.zip", csv_dep)

    # 3. Fusion des tables statistiques
    _AGE_RAW = [
        "P22_POP0002", "P22_POP0305", "P22_POP0610", "P22_POP1117",
        "P22_POP1824", "P22_POP2539", "P22_POP4054", "P22_POP5564",
        "P22_POP6579", "P22_POP80P",
    ]
    _AGE_RENAME = {
        "P22_POP0002": "age_0_2",
        "P22_POP0305": "age_3_5",
        "P22_POP0610": "age_6_10",
        "P22_POP1117": "age_11_17",
        "P22_POP1824": "age_18_24",
        "P22_POP2539": "age_25_39",
        "P22_POP4054": "age_40_54",
        "P22_POP5564": "age_55_64",
        "P22_POP6579": "age_65_79",
        "P22_POP80P":  "age_80p",
    }
    age_available = [c for c in _AGE_RAW if c in pop.columns]
    if len(age_available) < len(_AGE_RAW):
        missing_age = set(_AGE_RAW) - set(age_available)
        logger.warning("Colonnes age absentes du fichier pop : %s", sorted(missing_age))

    _CSP_RAW = [
        "C22_POP15P_STAT_GSEC11_21",
        "C22_POP15P_STAT_GSEC12_22",
        "C22_POP15P_STAT_GSEC13_23",
        "C22_POP15P_STAT_GSEC14_24",
        "C22_POP15P_STAT_GSEC15_25",
        "C22_POP15P_STAT_GSEC16_26",
        "C22_POP15P_STAT_GSEC32",
        "C22_POP15P_STAT_GSEC40",
    ]
    _CSP_RENAME = {
        "C22_POP15P_STAT_GSEC11_21": "csp_agriculteurs",
        "C22_POP15P_STAT_GSEC12_22": "csp_artisans_commercants",
        "C22_POP15P_STAT_GSEC13_23": "csp_cadres",
        "C22_POP15P_STAT_GSEC14_24": "csp_prof_intermediaires",
        "C22_POP15P_STAT_GSEC15_25": "csp_employes",
        "C22_POP15P_STAT_GSEC16_26": "csp_ouvriers",
        "C22_POP15P_STAT_GSEC32":    "csp_chomeurs_inactifs",
        "C22_POP15P_STAT_GSEC40":    "csp_autres_inactifs",
    }
    csp_available = [c for c in _CSP_RAW if c in pop.columns]
    if len(csp_available) < len(_CSP_RAW):
        missing_csp = set(_CSP_RAW) - set(csp_available)
        logger.warning("Colonnes CSP absentes du fichier pop : %s", sorted(missing_csp))

    pop_cols = ["IRIS", "P22_POP", "P22_PMEN"] + age_available + csp_available
    stats = pop[pop_cols].merge(
        log[["IRIS", "P22_MEN"]], on="IRIS", how="left"
    )

    # 4. Jointure géométrie + stats
    gdf = gdf.merge(stats, left_on="CODE_IRIS", right_on="IRIS", how="left")

    # 5. Colonnes de sortie compatibles avec le pipeline
    gdf["Ind_total"] = gdf["P22_POP"].fillna(0)
    gdf["taille_moy_menage"] = (
        gdf["P22_POP"] / gdf["P22_MEN"].replace(0, float("nan"))
    ).fillna(_TAILLE_MEN_DEFAUT)

    # Renommage age → noms lisibles, valeurs manquantes → 0
    for raw, friendly in _AGE_RENAME.items():
        if raw in gdf.columns:
            gdf[friendly] = gdf[raw].fillna(0)
            gdf = gdf.drop(columns=[raw])

    # Renommage CSP → noms lisibles, valeurs manquantes → 0
    for raw, friendly in _CSP_RENAME.items():
        if raw in gdf.columns:
            gdf[friendly] = gdf[raw].fillna(0)
            gdf = gdf.drop(columns=[raw])

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
    age_cols_out = [c for c in _AGE_RENAME.values() if c in gdf.columns]
    if age_cols_out:
        age_totals = {c: gdf[c].sum() for c in age_cols_out}
        logger.info("Tranches age (RP 2022) : %s", age_totals)

    csp_cols_out = [c for c in _CSP_RENAME.values() if c in gdf.columns]
    if csp_cols_out:
        csp_totals = {c: gdf[c].sum() for c in csp_cols_out}
        logger.info("CSP (pop 15+, GSEC 2022) : %s", csp_totals)

    return gdf
